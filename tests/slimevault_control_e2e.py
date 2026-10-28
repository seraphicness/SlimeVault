#!/usr/bin/env python3
"""Real-server lease, fencing, immutable snapshot, retention and SRF tests."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import struct
import time
import unittest
from urllib.parse import quote, urlsplit, unquote
from urllib.request import Request, build_opener, ProxyHandler
from urllib.error import HTTPError
import xml.etree.ElementTree as ET
import zstandard
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials
from botocore.exceptions import ClientError
from slimevault_e2e import WorldStorage


def fixture(extra=b''):
    # Empty v13 world plus optional arbitrary byte-array NBT data.
    nbt = b'\x0a\0\0'
    if extra: nbt += b'\x07\0\x04data' + struct.pack('>I', len(extra)) + extra
    nbt += b'\0'
    out = b'\xb1\x0b\x0d' + struct.pack('>I', 4657) + b'\0'
    for section in [b'\0\0\0\0', nbt]:
        compressed = zstandard.ZstdCompressor().compress(section)
        out += struct.pack('>II', len(compressed), len(section)) + compressed
    return out


def control(endpoint, credentials, bucket, key, operation, args=None):
    body = json.dumps(args or {}).encode()
    url = endpoint + '/' + bucket + '/' + quote(key, safe='/')
    request = AWSRequest(method='PUT', url=url, data=body, headers={
        'x-slimevault-operation': operation, 'Content-Type': 'application/json',
        'x-amz-content-sha256': hashlib.sha256(body).hexdigest(),
    })
    SigV4Auth(Credentials(credentials['accessKeyId'], credentials['secretAccessKey']), 's3', 'slimevault').add_auth(request)
    req = Request(url, data=body, headers=dict(request.headers), method='PUT')
    try:
        with build_opener(ProxyHandler({})).open(req, timeout=20) as response:
            return json.load(response)
    except HTTPError as error:
        root = ET.fromstring(error.read())
        raise ClientError({'Error': {'Code': root.findtext('Code'), 'Message': root.findtext('Message')},
            'ResponseMetadata': {'HTTPStatusCode': error.code}}, operation)


class ControlledStorage(WorldStorage):
    require_lease = True
    validate_format = True

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.leases = {}; cls.expected = {}
        def headers(request, operation_name, **kwargs):
            path = unquote(urlsplit(request.url).path).split('/', 2)
            key = path[2] if len(path) == 3 else ''
            if operation_name in ['PutObject', 'CopyObject', 'DeleteObject', 'CreateMultipartUpload', 'CompleteMultipartUpload', 'AbortMultipartUpload', 'UploadPart', 'UploadPartCopy']:
                if key in cls.leases: request.headers['x-slimevault-lease-token'] = cls.leases[key]
                if key in cls.expected: request.headers['x-slimevault-if-revision'] = cls.expected[key]
        cls.s3.meta.events.register('before-sign.s3', headers)

    def op(self, key, operation, args=None):
        return control(self.endpoint, self.credentials, self.bucket, key, operation, args)

    def lease(self, key, ttl=30_000):
        state = self.op(key, 'lease-acquire', {'ttl_ms': ttl})['State']
        self.leases[key] = state['lease']['token']
        return state['lease']

    def test_writer_lease_expiry_renewal_and_stale_fencing(self):
        key = 'worlds/lease.slime'
        with self.assertRaises(ClientError): self.put(key, fixture())
        first = self.lease(key, 300)
        with self.assertRaises(ClientError): self.lease(key)
        revision = self.put(key, fixture(b'one'))
        renewed = self.op(key, 'lease-renew', {'token': first['token'], 'ttl_ms': 300})['State']['lease']
        self.assertGreaterEqual(renewed['expires_at'], first['expires_at'])
        time.sleep(.4)
        with self.assertRaises(ClientError): self.put(key, fixture(b'expired'))
        second = self.lease(key)
        self.assertGreater(second['fence'], first['fence'])
        self.leases[key] = first['token']
        with self.assertRaises(ClientError): self.put(key, fixture(b'stale'))
        self.assertEqual(self.read(key)[1], revision)
        self.leases[key] = second['token']; self.put(key, fixture(b'new-owner'))
        self.op(key, 'lease-release', {'token': second['token']})
        with self.assertRaises(ClientError): self.put(key, fixture())

    def test_acquire_race_has_one_winner(self):
        key = 'worlds/race.slime'
        def attempt(_):
            try: return self.op(key, 'lease-acquire')['State']['lease']
            except ClientError as e: self.assertEqual(e.response['Error']['Code'], 'LeaseConflict'); return None
        with ThreadPoolExecutor(max_workers=8) as pool: results = list(pool.map(attempt, range(8)))
        self.assertEqual(sum(value is not None for value in results), 1)

    def test_snapshot_freeze_and_explicit_owner_unprotect(self):
        source = 'templates/arena.slime'; snapshot = 'snapshots/release.slime'
        self.lease(source); v = self.put(source, fixture(b'template'))
        self.lease(snapshot)
        snap = self.s3.copy_object(Bucket=self.bucket, Key=snapshot, CopySource={'Bucket': self.bucket, 'Key': source, 'VersionId': v})['VersionId']
        self.op(snapshot, 'protect', {'revision': snap})
        for operation in [lambda: self.put(snapshot, fixture()), lambda: self.s3.delete_object(Bucket=self.bucket, Key=snapshot),
                lambda: self.s3.create_multipart_upload(Bucket=self.bucket, Key=snapshot), lambda: self.op(snapshot, 'purge', {'revision': snap})]:
            with self.assertRaises(ClientError): operation()
        self.assertEqual(self.read(snapshot)[1], snap)
        self.op(snapshot, 'unprotect'); self.lease(snapshot); self.put(snapshot, fixture(b'unfrozen'))

    def test_retention_dry_run_purge_and_no_resurrection(self):
        key = 'worlds/retention.slime'; self.lease(key)
        versions = [self.put(key, fixture(str(n).encode())) for n in range(4)]
        self.op(key, 'retention', {'keep_last': 1, 'max_age_ms': 0})
        dry = self.op(key, 'collect')['Collected']
        self.assertTrue(dry['dry_run']); self.assertEqual(set(dry['revisions']), set(versions[:-1]))
        self.assertEqual(self.read(key, versions[0])[1], versions[0])
        self.op(key, 'collect', {'dry_run': False})
        for old in versions[:-1]:
            with self.assertRaises(ClientError): self.read(key, old)
        with self.assertRaises(ClientError): self.op(key, 'purge', {'revision': versions[-1]})
        self.s3.delete_object(Bucket=self.bucket, Key=key)
        self.op(key, 'collect', {'dry_run': False})
        with self.assertRaises(ClientError): self.read(key, versions[-1])
        self.put(key, fixture(b'after-gc'))
        for old in versions:
            with self.assertRaises(ClientError): self.read(key, old)

    def test_compare_revision_and_invalid_format_do_not_publish(self):
        key = 'worlds/validate.slime'; self.lease(key)
        v1 = self.put(key, fixture(b'old'))
        for invalid in [b'not-a-world', fixture()[:-1], fixture() + b'trailing', b'\xb1\x0b\xff']:
            with self.assertRaises(ClientError): self.put(key, invalid)
            self.assertEqual(self.read(key)[1], v1)
        self.expected[key] = '0' * 64
        with self.assertRaises(ClientError): self.put(key, fixture(b'conflict'))
        self.assertEqual(self.read(key)[1], v1)
        self.expected[key] = v1; self.put(key, fixture(b'valid-cas')); del self.expected[key]

    def test_restart_revokes_old_tokens_and_preserves_protection(self):
        key = 'worlds/reboot.slime'; self.lease(key); v = self.put(key, fixture())
        protected = 'snapshots/reboot.slime'; self.lease(protected); pv = self.put(protected, fixture())
        self.op(protected, 'protect', {'revision': pv})
        self.process.kill(); self.process.wait(timeout=10); type(self).start()
        with self.assertRaises(ClientError): self.put(key, fixture())
        self.assertEqual(self.read(key)[1], v)
        self.lease(key); self.put(key, fixture(b'restarted'))
        with self.assertRaises(ClientError): self.lease(protected)

    def test_multipart_validation_and_part_write_guard(self):
        key='worlds/strict-multipart.slime'; lease=self.lease(key); old=self.put(key,fixture(b'old'))
        upload=self.s3.create_multipart_upload(Bucket=self.bucket,Key=key)['UploadId']
        del self.leases[key]
        with self.assertRaises(ClientError): self.s3.upload_part(Bucket=self.bucket,Key=key,UploadId=upload,PartNumber=1,Body=fixture())
        self.leases[key]=lease['token']; data=fixture(os.urandom(1_100_000))
        part=self.s3.upload_part(Bucket=self.bucket,Key=key,UploadId=upload,PartNumber=1,Body=data)
        current=self.s3.complete_multipart_upload(Bucket=self.bucket,Key=key,UploadId=upload,MultipartUpload={'Parts':[{'PartNumber':1,'ETag':part['ETag']}]})['VersionId']
        self.assertEqual(self.read(key)[0],data); self.assertEqual(self.read(key,old)[0],fixture(b'old'))
        upload=self.s3.create_multipart_upload(Bucket=self.bucket,Key=key)['UploadId']
        part=self.s3.upload_part(Bucket=self.bucket,Key=key,UploadId=upload,PartNumber=1,Body=b'not-a-world')
        with self.assertRaises(ClientError): self.s3.complete_multipart_upload(Bucket=self.bucket,Key=key,UploadId=upload,MultipartUpload={'Parts':[{'PartNumber':1,'ETag':part['ETag']}]})
        self.assertEqual(self.read(key)[1],current)
        self.s3.abort_multipart_upload(Bucket=self.bucket,Key=key,UploadId=upload)
        self.assertEqual(list((self.root/'meta'/'world-validation').iterdir()),[])

    def test_encrypted_world_validation(self):
        key='worlds/encrypted.slime'; self.lease(key); secret=os.urandom(32); data=fixture(b'encrypted-world')
        options={'SSECustomerAlgorithm':'AES256','SSECustomerKey':secret}
        version=self.s3.put_object(Bucket=self.bucket,Key=key,Body=data,**options)['VersionId']
        self.assertEqual(self.s3.get_object(Bucket=self.bucket,Key=key,VersionId=version,**options)['Body'].read(),data)
        with self.assertRaises(ClientError): self.s3.put_object(Bucket=self.bucket,Key=key,Body=b'bad',**options)
        self.assertEqual(self.s3.head_object(Bucket=self.bucket,Key=key,**options)['VersionId'],version)

    def test_owner_permissions_actor_binding_and_control_limits(self):
        key='worlds/permissions.slime'; lease=self.lease(key); version=self.put(key,fixture())
        other=json.loads(self.cli('json-api','CreateKey','{"name":"non-owner"}'))
        self.cli('bucket','allow','--read','--write',self.bucket,'--key',other['accessKeyId'])
        with self.assertRaises(ClientError) as error:
            control(self.endpoint,other,self.bucket,key,'protect',{'revision':version})
        self.assertEqual(error.exception.response['ResponseMetadata']['HTTPStatusCode'],403)
        with self.assertRaises(ClientError): control(self.endpoint,other,self.bucket,key,'lease-renew',{'token':lease['token']})
        self.assertEqual(self.op(key,'inspect')['State']['lease']['token'],'')
        for operation,args in [('lease-acquire',{'ttl_ms':-1}),('collect',{'dry_run':'false'}),('inspect',{'padding':'x'*17000})]:
            with self.assertRaises(ClientError) as error: self.op(key,operation,args)
            self.assertEqual(error.exception.response['ResponseMetadata']['HTTPStatusCode'],400)

    def test_background_retention_runs_without_manual_collection(self):
        key='worlds/automatic-retention.slime'; self.lease(key)
        old=self.put(key,fixture(b'old')); current=self.put(key,fixture(b'current'))
        self.op(key,'retention',{'keep_last':1,'max_age_ms':0})
        deadline=time.monotonic()+50
        while True:
            versions=self.s3.list_object_versions(Bucket=self.bucket,Prefix=key).get('Versions',[])
            if len(versions)==1: break
            if time.monotonic()>deadline: self.fail('Background retention did not collect the old revision')
            time.sleep(.2)
        self.assertEqual(versions[0]['VersionId'],current)
        with self.assertRaises(ClientError): self.read(key,old)

if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('--binary', type=Path, default=WorldStorage.binary); parser.add_argument('--keep', action='store_true')
    args, remaining = parser.parse_known_args()
    ControlledStorage.binary = args.binary.resolve(); ControlledStorage.keep = args.keep
    names = [name for name in ControlledStorage.__dict__ if name.startswith('test_')]
    result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(ControlledStorage(name) for name in names))
    raise SystemExit(not result.wasSuccessful())

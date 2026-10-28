#!/usr/bin/env python3
"""Start a real single-node SlimeVault and test S3 via boto3. No external server needed."""
import argparse
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import tempfile
import time
import unittest
import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

class WorldStorage(unittest.TestCase):
    binary = Path('target/debug/slimevault').resolve()

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='slimevault-e2e-')
        cls.root = Path(cls.temp.name)
        # Reserve ports until the configuration is ready.
        ports = []
        for _ in range(3):
            sock = socket.socket(); sock.bind(('127.0.0.1', 0)); ports.append(sock)
        s3, rpc, admin = [s.getsockname()[1] for s in ports]
        cls.config = cls.root / 'slimevault.toml'
        cls.config.write_text(f'''metadata_dir = "{cls.root}/meta"
data_dir = "{cls.root}/data"
db_engine = "sqlite"
replication_factor = 1
rpc_bind_addr = "127.0.0.1:{rpc}"
rpc_public_addr = "127.0.0.1:{rpc}"
rpc_secret = "{secrets.token_hex(32)}"
[s3_api]
api_bind_addr = "127.0.0.1:{s3}"
s3_region = "slimevault"
world_require_lease = {str(getattr(cls, "require_lease", False)).lower()}
world_validate_format = {str(getattr(cls, "validate_format", False)).lower()}
[admin]
api_bind_addr = "127.0.0.1:{admin}"
admin_token = "{secrets.token_hex(32)}"
''')
        cls.log = open(cls.root / 'server.log', 'w+')
        for sock in ports: sock.close()
        cls.process = None
        cls.addClassCleanup(cls.cleanup)
        cls.start()
        node = cls.cli('node', 'id').strip().split('@')[0]
        cls.cli('layout', 'assign', node, '-c', '1G', '-z', 'local')
        cls.cli('layout', 'apply', '--version', '1')
        key = json.loads(cls.cli('json-api', 'CreateKey', '{"name":"e2e"}'))
        cls.bucket = 'slimevault-e2e'
        cls.cli('bucket', 'create', cls.bucket)
        cls.cli('bucket', 'allow', '--read', '--write', '--owner', cls.bucket, '--key', key['accessKeyId'])
        cls.credentials = key
        cls.endpoint = f'http://127.0.0.1:{s3}'
        cls.s3 = boto3.client('s3', endpoint_url=f'http://127.0.0.1:{s3}', region_name='slimevault',
            aws_access_key_id=key['accessKeyId'], aws_secret_access_key=key['secretAccessKey'],
            config=Config(signature_version='s3v4', retries={'mode': 'standard', 'max_attempts': 2}, proxies={}, s3={'addressing_style': 'path'}))

    @classmethod
    def cli(cls, *args):
        p = subprocess.run([str(cls.binary), '-c', str(cls.config), *args], capture_output=True, text=True, timeout=30)
        if p.returncode: raise RuntimeError(p.stderr)
        return p.stdout

    @classmethod
    def start(cls):
        cls.process = subprocess.Popen([str(cls.binary), '-c', str(cls.config), 'server'], stdout=cls.log, stderr=cls.log)
        for _ in range(100):
            if cls.process.poll() is not None: raise RuntimeError('Server exited; inspect server.log')
            try: cls.cli('status'); return
            except RuntimeError: time.sleep(.1)
        raise RuntimeError('Server boot timed out')

    @classmethod
    def cleanup(cls):
        if cls.process and cls.process.poll() is None:
            cls.process.terminate()
            try: cls.process.wait(timeout=10)
            except subprocess.TimeoutExpired: cls.process.kill(); cls.process.wait()
        if not getattr(cls, 'keep', False): cls.temp.cleanup()
        else: print('Artifacts:', cls.root); cls.temp._finalizer.detach()
        cls.log.close()

    def put(self, key, data):
        return self.s3.put_object(Bucket=self.bucket, Key=key, Body=data)['VersionId']

    def read(self, key, version=None):
        kwargs = {'VersionId': version} if version else {}
        res = self.s3.get_object(Bucket=self.bucket, Key=key, **kwargs)
        return res['Body'].read(), res['VersionId']

    def test_01_revisions_restore_clone_delete_restart(self):
        key = 'worlds/player-1.slime'
        # Synthetic binary payloads exercise transport; ASP serialization is tested separately with real fixtures.
        v1 = self.put(key, b'world-one\x00\xff')
        v2 = self.put(key, os.urandom(2_200_000))
        self.assertNotEqual(v1, v2)
        self.assertEqual(self.read(key, v1)[0], b'world-one\x00\xff')
        self.assertEqual(self.s3.head_object(Bucket=self.bucket, Key=key, VersionId=v1)['VersionId'], v1)
        versions = self.s3.list_object_versions(Bucket=self.bucket, Prefix=key)['Versions']
        self.assertEqual([v['VersionId'] for v in versions], [v2, v1])
        self.assertTrue(versions[0]['IsLatest']); self.assertFalse(versions[1]['IsLatest'])
        # Restore is a new revision; it never rewrites the historical revision.
        restored = self.s3.copy_object(Bucket=self.bucket, Key=key, CopySource={'Bucket': self.bucket, 'Key': key, 'VersionId': v1})['VersionId']
        self.assertNotIn(restored, [v1, v2]); self.assertEqual(self.read(key)[0], b'world-one\x00\xff')
        clone = 'instances/game-1.slime'
        self.s3.copy_object(Bucket=self.bucket, Key=clone, CopySource={'Bucket': self.bucket, 'Key': key, 'VersionId': v2})
        self.assertEqual(self.read(clone)[0], self.read(key, v2)[0])
        self.s3.delete_object(Bucket=self.bucket, Key=key)
        for method in [self.s3.get_object, self.s3.head_object]:
            with self.assertRaises(ClientError) as caught: method(Bucket=self.bucket, Key=key)
            self.assertEqual(caught.exception.response['ResponseMetadata']['HTTPStatusCode'], 404)
        listing = self.s3.list_objects_v2(Bucket=self.bucket, Prefix=key)
        self.assertEqual(listing.get('Contents', []), [])
        self.assertEqual(self.read(key, v1)[0], b'world-one\x00\xff')
        all_versions = self.s3.list_object_versions(Bucket=self.bucket, Prefix=key)
        self.assertEqual(len(all_versions['DeleteMarkers']), 1)
        self.process.kill(); self.process.wait(timeout=10)
        type(self).start()
        self.assertEqual(self.read(key, v1)[0], b'world-one\x00\xff')
        self.assertEqual(self.read(key, v2)[0], self.read(clone)[0])

    def test_02_reject_nonworld_writes_and_destructive_options(self):
        for key in ['notes.txt', 'worlds/../bad.slime', 'worlds/.slime']:
            with self.assertRaises(ClientError): self.put(key, b'bad')
        with self.assertRaises(ClientError):
            self.s3.put_bucket_versioning(Bucket=self.bucket, VersioningConfiguration={'Status': 'Suspended'})
        self.assertEqual(self.s3.get_bucket_versioning(Bucket=self.bucket)['Status'], 'Enabled')
        self.s3.put_bucket_versioning(Bucket=self.bucket, VersioningConfiguration={'Status': 'Enabled'})
        key = 'worlds/protected.slime'; version = self.put(key, b'keep')
        with self.assertRaises(ClientError): self.s3.delete_object(Bucket=self.bucket, Key=key, VersionId=version)
        self.assertEqual(self.read(key, version)[0], b'keep')
        with self.assertRaises(ClientError):
            self.s3.delete_objects(Bucket=self.bucket, Delete={'Objects': [{'Key': key}]})

    def test_03_pagination_and_escaped_keys(self):
        key = 'worlds/a&b.slime'
        expected = [self.put(key, f'revision-{n}'.encode()) for n in range(5)][::-1]
        actual = []
        for page in self.s3.get_paginator('list_object_versions').paginate(Bucket=self.bucket, Prefix=key, PaginationConfig={'PageSize': 2}):
            actual.extend(v['VersionId'] for v in page.get('Versions', []))
        self.assertEqual(actual, expected)
        self.assertEqual(self.read(key, expected[-1])[0], b'revision-0')

    def test_04_multipart_and_aborted_upload(self):
        key = 'worlds/multipart.slime'; old = self.put(key, b'old')
        upload = self.s3.create_multipart_upload(Bucket=self.bucket, Key=key)['UploadId']
        data = os.urandom(5 * 1024 * 1024)
        part = self.s3.upload_part(Bucket=self.bucket, Key=key, UploadId=upload, PartNumber=1, Body=data)
        self.assertEqual(self.read(key)[0], b'old')
        result = self.s3.complete_multipart_upload(Bucket=self.bucket, Key=key, UploadId=upload,
            MultipartUpload={'Parts': [{'PartNumber': 1, 'ETag': part['ETag']}]})
        self.assertEqual(self.read(key)[0], data)
        self.assertEqual(self.read(key, old)[0], b'old')
        upload = self.s3.create_multipart_upload(Bucket=self.bucket, Key=key)['UploadId']
        self.s3.abort_multipart_upload(Bucket=self.bucket, Key=key, UploadId=upload)
        self.assertEqual(self.read(key)[1], result['VersionId'])

    def test_05_many_keys_pagination_boundary(self):
        prefix = 'instances/boundary-'
        expected = [f'{prefix}{n:03d}.slime' for n in range(130)]
        for key in expected: self.put(key, b'world')
        actual = []
        for page in self.s3.get_paginator('list_object_versions').paginate(Bucket=self.bucket, Prefix=prefix, PaginationConfig={'PageSize': 127}):
            actual.extend(v['Key'] for v in page.get('Versions', []))
        self.assertEqual(actual, expected)

    def test_06_failed_checksum_does_not_publish_revision(self):
        key = 'worlds/checksum.slime'; old = self.put(key, b'valid')
        with self.assertRaises(ClientError):
            self.s3.put_object(Bucket=self.bucket, Key=key, Body=b'corrupted', ContentMD5='AAAAAAAAAAAAAAAAAAAAAA==')
        self.assertEqual(self.read(key), (b'valid', old))
        versions = self.s3.list_object_versions(Bucket=self.bucket, Prefix=key)['Versions']
        self.assertEqual([v['VersionId'] for v in versions], [old])
        with self.assertRaises(ClientError) as caught:
            self.s3.get_object(Bucket=self.bucket, Key=key, VersionId='0' * 64)
        self.assertEqual(caught.exception.response['Error']['Code'], 'NoSuchVersion')

    def test_07_metadata_and_versioned_copy_of_escaped_key(self):
        key = 'templates/a?b & 한글.slime'
        version = self.s3.put_object(Bucket=self.bucket, Key=key, Body=b'template',
            Metadata={'world-id': 'example-id', 'save-reason': 'template'})['VersionId']
        self.put(key, b'new-template')
        copied = 'instances/escaped-key.slime'
        self.s3.copy_object(Bucket=self.bucket, Key=copied,
            CopySource={'Bucket': self.bucket, 'Key': key, 'VersionId': version})
        result = self.s3.get_object(Bucket=self.bucket, Key=copied)
        self.assertEqual(result['Body'].read(), b'template')
        self.assertEqual(result['Metadata']['world-id'], 'example-id')

if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('--binary', type=Path, default=WorldStorage.binary); parser.add_argument('--keep', action='store_true')
    args, remaining = parser.parse_known_args()
    WorldStorage.binary = args.binary.resolve(); WorldStorage.keep = args.keep
    unittest.main(argv=[__file__, *remaining], verbosity=2)

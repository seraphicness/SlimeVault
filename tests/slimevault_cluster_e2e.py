#!/usr/bin/env python3
"""Three real processes: shared authority, concurrent ingress, failure and recovery."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import tempfile
import time
import unittest
import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from slimevault_control_e2e import control, fixture

class ClusterStorage(unittest.TestCase):
    binary = Path('target/debug/slimevault').resolve()
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='slimevault-cluster-'); cls.root = Path(cls.temp.name)
        cls.processes = [None]*3; cls.logs=[]; cls.configs=[]; cls.ports=[]; cls.ids=[]
        cls.secret = secrets.token_hex(32)
        cls.addClassCleanup(cls.cleanup)
        for i in range(3):
            ports=[]
            for _ in range(3):
                s=socket.socket(); s.bind(('127.0.0.1',0)); ports.append(s.getsockname()[1]); s.close()
            cls.ports.append(ports)
            directory=cls.root/str(i); directory.mkdir()
            cls.configs.append(directory/'config.toml'); cls.logs.append(open(directory/'server.log','w+'))
        cls.configure(False)
        for i in range(3): cls.start(i); cls.ids.append(cls.cli(i,'node','id').strip().split('@')[0])
        for i in range(3): cls.stop(i)
        cls.configure(True)
        for i in range(3): cls.start(i)
        for i in range(1,3): cls.cli(0,'node','connect',f'{cls.ids[i]}@127.0.0.1:{cls.ports[i][1]}')
        for i in range(3): cls.cli(0,'layout','assign',cls.ids[i],'-c','1G','-z',f'zone{i}')
        cls.cli(0,'layout','apply','--version','1')
        cls.credentials=json.loads(cls.cli(0,'json-api','CreateKey','{"name":"cluster-e2e"}'))
        cls.bucket='cluster-worlds'; cls.cli(0,'bucket','create',cls.bucket)
        cls.cli(0,'bucket','allow','--read','--write','--owner',cls.bucket,'--key',cls.credentials['accessKeyId'])
        cls.clients=[]; cls.tokens={}
        for i in range(3):
            client=boto3.client('s3',endpoint_url=cls.endpoint(i),region_name='slimevault',
                aws_access_key_id=cls.credentials['accessKeyId'],aws_secret_access_key=cls.credentials['secretAccessKey'],
                config=Config(signature_version='s3v4',proxies={},retries={'mode':'standard','max_attempts':0},connect_timeout=3,read_timeout=15,s3={'addressing_style':'path'}))
            def headers(request, operation_name, **kwargs):
                if operation_name in ['PutObject','CopyObject','DeleteObject']:
                    request.headers['x-slimevault-lease-token']=cls.tokens.get('current','invalid')
            client.meta.events.register('before-sign.s3',headers); cls.clients.append(client)
        deadline=time.monotonic()+40
        while True:
            try:
                for client in cls.clients: client.head_bucket(Bucket=cls.bucket)
                break
            except ClientError:
                if time.monotonic()>deadline: raise
                time.sleep(.2)

    @classmethod
    def endpoint(cls,i): return f'http://127.0.0.1:{cls.ports[i][0]}'
    @classmethod
    def configure(cls,known):
        peers=[f'{cls.ids[i]}@127.0.0.1:{cls.ports[i][1]}' for i in range(3)] if known else []
        for i in range(3):
            s3,rpc,admin=cls.ports[i]; directory=cls.configs[i].parent
            coordinator=f'world_coordinator = "{cls.ids[0]}"' if known else ''
            cls.configs[i].write_text(f'''metadata_dir = "{directory}/meta"
data_dir = "{directory}/data"
db_engine = "sqlite"
metadata_fsync = true
data_fsync = true
replication_factor = 3
rpc_bind_addr = "127.0.0.1:{rpc}"
rpc_public_addr = "127.0.0.1:{rpc}"
rpc_secret = "{cls.secret}"
rpc_timeout_msec = 2000
rpc_ping_timeout_msec = 1000
bootstrap_peers = {json.dumps(peers)}
[s3_api]
api_bind_addr = "127.0.0.1:{s3}"
s3_region = "slimevault"
{coordinator}
[admin]
api_bind_addr = "127.0.0.1:{admin}"
admin_token = "{secrets.token_hex(32)}"
''')
    @classmethod
    def cli(cls,i,*args):
        p=subprocess.run([str(cls.binary),'-c',str(cls.configs[i]),*args],capture_output=True,text=True,timeout=20)
        if p.returncode: raise RuntimeError(p.stderr)
        return p.stdout
    @classmethod
    def start(cls,i):
        cls.processes[i]=subprocess.Popen([str(cls.binary),'-c',str(cls.configs[i]),'server'],stdout=cls.logs[i],stderr=cls.logs[i])
        for _ in range(100):
            if cls.processes[i].poll() is not None: raise RuntimeError(f'Node {i} exited')
            try: cls.cli(i,'status'); return
            except RuntimeError: time.sleep(.1)
        raise RuntimeError('Node boot timed out')
    @classmethod
    def stop(cls,i):
        p=cls.processes[i]
        if p and p.poll() is None: p.kill(); p.wait(timeout=10)
    @classmethod
    def cleanup(cls):
        for i in range(3): cls.stop(i)
        for log in cls.logs: log.close()
        if not getattr(cls,'keep',False): cls.temp.cleanup()
        else: print('Cluster artifacts:',cls.root); cls.temp._finalizer.detach()
    def op(self,i,key,operation,args=None): return control(self.endpoint(i),self.credentials,self.bucket,key,operation,args)
    def put(self,i,key,payload): return self.clients[i].put_object(Bucket=self.bucket,Key=key,Body=payload)['VersionId']
    def wait_control(self):
        for i in range(1,3):
            if self.processes[i].poll() is None:
                self.cli(0,'node','connect',f'{self.ids[i]}@127.0.0.1:{self.ports[i][1]}')
        deadline=time.monotonic()+30
        while True:
            try:
                for i in range(3):
                    if self.processes[i].poll() is None: self.op(i,'worlds/readiness.slime','inspect')
                return
            except ClientError:
                if time.monotonic()>deadline: raise
                time.sleep(.2)

    def test_01_shared_authority_and_cross_node_fencing(self):
        key='worlds/shared.slime'
        def attempt(i):
            try: return self.op(i%3,key,'lease-acquire')['State']['lease']
            except ClientError as e: self.assertEqual(e.response['Error']['Code'],'LeaseConflict'); return None
        with ThreadPoolExecutor(max_workers=6) as pool: results=list(pool.map(attempt,range(6)))
        winners=[r for r in results if r]; self.assertEqual(len(winners),1)
        self.tokens['current']=winners[0]['token']; revision=self.put(1,key,fixture(b'cluster'))
        read=self.clients[2].get_object(Bucket=self.bucket,Key=key,VersionId=revision)
        self.assertEqual(read['Body'].read(),fixture(b'cluster'))
        self.op(2,key,'lease-release',{'token':winners[0]['token']})
        replacement=self.op(2,key,'lease-acquire')['State']['lease']
        self.assertGreater(replacement['fence'],winners[0]['fence'])
        with self.assertRaises(ClientError): self.put(1,key,fixture(b'stale'))
        self.tokens['current']=replacement['token']; self.put(2,key,fixture(b'new'))

    def test_02_authority_failure_fails_closed_and_recovers(self):
        key='worlds/outage.slime'; lease=self.op(1,key,'lease-acquire')['State']['lease']; self.tokens['current']=lease['token']
        revision=self.put(1,key,fixture(b'before-failure'))
        self.stop(0)
        with self.assertRaises(ClientError): self.op(2,'worlds/no-fallback.slime','lease-acquire')
        with self.assertRaises(ClientError): self.put(1,key,fixture(b'without-authority'))
        self.start(0)
        self.wait_control()
        with self.assertRaises(ClientError): self.put(1,key,fixture(b'old-epoch'))
        new=self.op(2,key,'lease-acquire')['State']['lease']; self.tokens['current']=new['token']
        self.assertGreater(new['fence'],lease['fence'])
        self.put(2,key,fixture(b'after-recovery'))
        self.assertEqual(self.clients[1].get_object(Bucket=self.bucket,Key=key,VersionId=revision)['Body'].read(),fixture(b'before-failure'))

    def test_03_replica_failure_quorum_and_rejoin(self):
        key='worlds/replica.slime'; self.tokens['current']=self.op(1,key,'lease-acquire')['State']['lease']['token']
        self.stop(2)
        self.put(1,key,fixture(b'one-replica-down'))
        self.stop(1)
        with self.assertRaises(ClientError): self.put(0,key,fixture(b'no-quorum'))
        self.start(1); self.start(2)
        deadline=time.monotonic()+30
        while True:
            try:
                self.assertEqual(self.clients[2].get_object(Bucket=self.bucket,Key=key)['Body'].read(),fixture(b'one-replica-down'))
                break
            except ClientError:
                if time.monotonic()>deadline: raise
                time.sleep(.2)

if __name__=='__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('--binary',type=Path,default=ClusterStorage.binary); parser.add_argument('--keep',action='store_true')
    args=parser.parse_args(); ClusterStorage.binary=args.binary.resolve(); ClusterStorage.keep=args.keep
    result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(ClusterStorage))
    raise SystemExit(not result.wasSuccessful())

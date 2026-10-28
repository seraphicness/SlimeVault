#!/usr/bin/env python3
"""Real Docker network disconnection: three nodes, no fail-open authority fallback."""
import argparse
import os
from pathlib import Path
import secrets
import shlex
import subprocess
import time
import unittest
from botocore.exceptions import ClientError
from slimevault_cluster_e2e import ClusterStorage
from slimevault_control_e2e import fixture

class DockerProcess:
    def __init__(self, cls, i): self.cls=cls; self.i=i
    def poll(self):
        try:
            self.cls.docker('exec',self.cls.containers[self.i],'/bin/sh','-c',f'kill -0 $(cat {shlex.quote(str(self.cls.configs[self.i].parent/"pid"))})')
            return None
        except RuntimeError: return 1

class PartitionStorage(ClusterStorage):
    @classmethod
    def docker(cls,*args):
        env=os.environ.copy()
        for name in ['DOCKER_HOST','DOCKER_CONTEXT','DOCKER_TLS','DOCKER_TLS_VERIFY','DOCKER_CERT_PATH']: env.pop(name,None)
        p=subprocess.run(['docker','--host=unix:///var/run/docker.sock',*args],capture_output=True,text=True,env=env,timeout=45)
        if p.returncode: raise RuntimeError(p.stderr)
        return p.stdout.strip()
    @classmethod
    def setUpClass(cls):
        cls.containers=[None]*3; cls.ips=[None]*3
        cls.network='slimevault-partition-'+secrets.token_hex(4)
        cls.docker('network','create','--subnet',f'172.29.{100+secrets.randbelow(100)}.0/24',cls.network)
        cls.addClassCleanup(cls.remove_docker)
        super().setUpClass()
    @classmethod
    def configure(cls,known):
        super().configure(known)
        if known:
            for i in range(3): cls.patch_config(i)
    @classmethod
    def patch_config(cls,i):
        p=cls.configs[i]; text=p.read_text(); s3,rpc,_=cls.ports[i]
        text=text.replace(f'rpc_bind_addr = "127.0.0.1:{rpc}"',f'rpc_bind_addr = "0.0.0.0:{rpc}"')
        text=text.replace(f'rpc_public_addr = "127.0.0.1:{rpc}"',f'rpc_public_addr = "{cls.ips[i]}:{rpc}"')
        text=text.replace(f'api_bind_addr = "127.0.0.1:{s3}"',f'api_bind_addr = "0.0.0.0:{s3}"')
        for n in range(len(cls.ids)):
            if cls.ips[n]: text=text.replace(f'{cls.ids[n]}@127.0.0.1:{cls.ports[n][1]}',f'{cls.ids[n]}@{cls.ips[n]}:{cls.ports[n][1]}')
        p.write_text(text)
    @classmethod
    def start(cls,i):
        if cls.containers[i] is None:
            name=f'{cls.network}-{i}'; cls.containers[i]=name
            cls.docker('run','-d','--name',name,'--network',cls.network,'--user',f'{os.getuid()}:{os.getgid()}',
                '-p',f'127.0.0.1:{cls.ports[i][0]}:{cls.ports[i][0]}',
                '--mount',f'type=bind,src={cls.root},dst={cls.root}',
                '--mount',f'type=bind,src={cls.binary},dst={cls.binary},readonly',
                'debian:trixie-slim','sleep','infinity')
            cls.ips[i]=cls.docker('inspect','-f','{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}',name)
        cls.patch_config(i)
        directory=cls.configs[i].parent
        command=f'echo $$ > {shlex.quote(str(directory/"pid"))}; exec {shlex.quote(str(cls.binary))} -c {shlex.quote(str(cls.configs[i]))} server >> {shlex.quote(str(directory/"server.log"))} 2>&1'
        cls.docker('exec','-d',cls.containers[i],'/bin/sh','-c',command)
        cls.processes[i]=DockerProcess(cls,i)
        for _ in range(100):
            try: cls.cli(i,'status'); return
            except RuntimeError: time.sleep(.1)
        raise RuntimeError(f'Container node {i} did not boot')
    @classmethod
    def cli(cls,i,*args):
        changed=[]
        for arg in args:
            for n in range(len(cls.ids)):
                if cls.ips[n]: arg=arg.replace(f'{cls.ids[n]}@127.0.0.1:{cls.ports[n][1]}',f'{cls.ids[n]}@{cls.ips[n]}:{cls.ports[n][1]}')
            changed.append(arg)
        return cls.docker('exec',cls.containers[i],str(cls.binary),'-c',str(cls.configs[i]),*changed)
    @classmethod
    def stop(cls,i):
        if cls.containers[i] and cls.processes[i] and cls.processes[i].poll() is None:
            cls.docker('exec',cls.containers[i],'/bin/sh','-c',f'kill -KILL $(cat {shlex.quote(str(cls.configs[i].parent/"pid"))})')
    @classmethod
    def remove_docker(cls):
        for name in cls.containers:
            if name:
                try: cls.docker('rm','-f',name)
                except RuntimeError: pass
        try: cls.docker('network','rm',cls.network)
        except RuntimeError: pass
    def test_actual_network_partition_and_recovery(self):
        key='worlds/partition.slime'
        self.tokens['current']=self.op(1,key,'lease-acquire',{'ttl_ms':300_000})['State']['lease']['token']
        revision=self.put(1,key,fixture(b'before-partition'))
        self.docker('network','disconnect',self.network,self.containers[0])
        try:
            for operation in [lambda:self.op(2,'worlds/no-fallback.slime','lease-acquire'), lambda:self.put(1,key,fixture(b'during-partition'))]:
                with self.assertRaises(ClientError) as error: operation()
                self.assertEqual(error.exception.response['ResponseMetadata']['HTTPStatusCode'],503)
        finally:
            self.docker('network','connect','--ip',self.ips[0],self.network,self.containers[0])
        for i in range(1,3): self.cli(0,'node','connect',f'{self.ids[i]}@{self.ips[i]}:{self.ports[i][1]}')
        deadline=time.monotonic()+40
        while True:
            try: self.op(1,'worlds/readiness.slime','inspect'); break
            except ClientError:
                if time.monotonic()>deadline: raise
                time.sleep(.2)
        self.assertEqual(self.clients[2].get_object(Bucket=self.bucket,Key=key)['VersionId'],revision)
        self.put(1,key,fixture(b'after-partition'))
        self.assertEqual(self.clients[2].get_object(Bucket=self.bucket,Key=key)['Body'].read(),fixture(b'after-partition'))

if __name__=='__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('--binary',type=Path,default=ClusterStorage.binary); parser.add_argument('--keep',action='store_true')
    args=parser.parse_args(); PartitionStorage.binary=args.binary.resolve(); PartitionStorage.keep=args.keep
    result=unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite([PartitionStorage('test_actual_network_partition_and_recovery')]))
    raise SystemExit(not result.wasSuccessful())

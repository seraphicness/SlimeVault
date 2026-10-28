#!/usr/bin/env python3
"""Use the official ASP utility plugin and console; no custom Java integration plugin.
Run an actual server, persist its real SRF through SlimeVault, restore and reopen it.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import tempfile
import time
from urllib.parse import urlsplit
from slimevault_e2e import WorldStorage
from slimevault_control_e2e import control

ASP_COMMIT = '3ab54bd48c7ecd28335d88e193b9c87af9275c49'
ASP_VERSION = '26.3.build.191-stable'

class Vault(WorldStorage):
    require_lease = True
    validate_format = True

class Server:
    def __init__(self, java, jar, plugin, directory, truststore):
        self.directory=directory; self.directory.mkdir(parents=True)
        self.log_path=directory/'console.log'; self.log=open(self.log_path,'a',buffering=1)
        self.process=None
        (directory/'plugins').mkdir()
        (directory/'plugins'/'bStats').mkdir()
        (directory/'plugins'/'bStats'/'config.yml').write_text('enabled: false\n')
        shutil.copy2(plugin,directory/'plugins'/'asp-official-plugin.jar')
        (directory/'eula.txt').write_text('eula=true\n')
        sock=socket.socket(); sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]; sock.close()
        generator_settings=json.dumps({'biome':'minecraft:plains','layers':[{'height':1,'block':'minecraft:bedrock'},{'height':2,'block':'minecraft:dirt'},{'height':1,'block':'minecraft:grass_block'}]})
        (directory/'server.properties').write_text(f'''server-ip=127.0.0.1
server-port={port}
online-mode=false
level-type=minecraft:flat
generator-settings={generator_settings}
spawn-protection=0
view-distance=2
simulation-distance=2
max-players=1
allow-nether=false
enable-command-block=true
motd=Isolated ASP compatibility test
''')
        options=['-Xms512m','-Xmx3g','-Dterminal.jline=false','-Dterminal.ansi=false']
        if truststore: options += [f'-Djavax.net.ssl.trustStore={truststore}','-Djavax.net.ssl.trustStorePassword=changeit']
        proxy=urlsplit(os.environ.get('HTTPS_PROXY',os.environ.get('HTTP_PROXY','')))
        if proxy.hostname:
            options += [f'-Dhttps.proxyHost={proxy.hostname}',f'-Dhttps.proxyPort={proxy.port or 80}',f'-Dhttp.proxyHost={proxy.hostname}',f'-Dhttp.proxyPort={proxy.port or 80}']
        self.args=[str(java),*options,'-jar',str(jar),'--nogui']
    def text(self):
        return re.sub(r'\x1b\[[0-9;]*m','',self.log_path.read_text(errors='replace'))
    def wait(self,needle,after=0,timeout=120):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            text=self.text()[after:]
            if needle in text: return text
            if self.process.poll() is not None: raise RuntimeError(f'ASP exited before {needle!r}; see {self.log_path}')
            time.sleep(.2)
        raise TimeoutError(f'ASP did not log {needle!r}; see {self.log_path}')
    def start(self):
        offset=len(self.text())
        self.process=subprocess.Popen(self.args,cwd=self.directory,stdin=subprocess.PIPE,stdout=self.log,stderr=self.log,text=True)
        self.wait('Done (',offset,timeout=300)
        startup=self.text()[offset:]
        if 'ASPaperPlugin' not in startup: raise AssertionError('Official ASP utility plugin was not enabled')
        if '@'+ASP_COMMIT[:7] not in startup or ASP_COMMIT not in startup:
            raise AssertionError('ASP server/plugin source commit does not match the pinned test target')
        self.reported_build=next(line for line in startup.splitlines() if '[bootstrap] Loading AdvancedSlimePaper' in line)
    def command(self,command,expected,timeout=120):
        offset=len(self.text()); self.process.stdin.write(command+'\n'); self.process.stdin.flush()
        return self.wait(expected,offset,timeout)
    def force_chunk(self,dimension):
        self.process.stdin.write(f'execute in {dimension} run forceload add 0 0\n'); self.process.stdin.flush()
        deadline=time.monotonic()+60
        while time.monotonic()<deadline:
            offset=len(self.text())
            self.process.stdin.write(f'execute in {dimension} if loaded 0 64 0 run say ASP_VAULT_CHUNK_READY\n'); self.process.stdin.flush()
            time.sleep(.25)
            if '[Server] ASP_VAULT_CHUNK_READY' in self.text()[offset:]: return
            if self.process.poll() is not None: raise RuntimeError('ASP exited while loading chunk')
        raise TimeoutError('ASP did not load the test chunk')
    def stop(self):
        if self.process and self.process.poll() is None:
            try:
                self.process.stdin.write('stop\n'); self.process.stdin.flush(); self.process.wait(timeout=90)
            except (BrokenPipeError,subprocess.TimeoutExpired): self.process.kill(); self.process.wait(timeout=10)
    def close(self): self.stop(); self.log.close()

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--java',type=Path,required=True)
    parser.add_argument('--server-jar',type=Path,required=True)
    parser.add_argument('--plugin-jar',type=Path,required=True)
    parser.add_argument('--slimevault',type=Path,default=WorldStorage.binary)
    parser.add_argument('--truststore',type=Path)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--accept-eula',action='store_true')
    parser.add_argument('--keep-runtime',action='store_true')
    args=parser.parse_args()
    if not args.accept_eula: parser.error('Pass --accept-eula only after accepting Minecraft EULA for this local test')
    output=args.output.resolve(); output.mkdir(parents=True,exist_ok=True)
    runtime=Path(tempfile.mkdtemp(prefix='asp-live-e2e-'))
    report={'target_source_commit':ASP_COMMIT,'target_source_version':ASP_VERSION,
        'server_jar_sha256':hashlib.sha256(args.server_jar.read_bytes()).hexdigest(),
        'slimevault_binary_sha256':hashlib.sha256(args.slimevault.read_bytes()).hexdigest(),
        'checks':[],'passed':False}
    server=None; vault_started=False
    def checked(name): report['checks'].append(name); print('PASS:',name,flush=True)
    try:
        Vault.binary=args.slimevault.resolve(); Vault.setUpClass(); vault_started=True
        leases={}
        def headers(request,operation_name,**kwargs):
            if operation_name in ['PutObject','CopyObject','DeleteObject']:
                # This test has one active destination at a time.
                request.headers['x-slimevault-lease-token']=leases['active']
        Vault.s3.meta.events.register('before-sign.s3',headers)
        def lease(key):
            leases['active']=control(Vault.endpoint,Vault.credentials,Vault.bucket,key,'lease-acquire',{'ttl_ms':300000})['State']['lease']['token']
        server=Server(args.java.resolve(),args.server_jar.resolve(),args.plugin_jar.resolve(),runtime/'asp',args.truststore)
        server.start(); checked('ASP server and official utility plugin boot')
        server.command('version','Implementing API version '+ASP_VERSION)
        checked('Server reports Minecraft 26.3')
        world='asp_vault_e2e'; dimension='minecraft:'+world
        server.command(f'aswm create {world} file',f'{world} created in')
        server.force_chunk(dimension)
        server.command(f'execute in {dimension} run setblock 0 64 0 minecraft:diamond_block','Changed the block')
        server.command(f'execute in {dimension} run setblock 1 64 0 minecraft:chest','Changed the block')
        server.command(f'execute in {dimension} run data merge block 1 64 0 {{Items:[{{Slot:0b,id:"minecraft:diamond",count:7}}]}}','Modified block data')
        server.command(f'aswm save {world}',f'{world} saved.')
        checked('ASP generated and serialized a real world with blocks and block-entity NBT')
        source=server.directory/'slime_worlds'/f'{world}.slime'
        v1=source.read_bytes(); (output/'asp-world-v1.slime').write_bytes(v1)
        key='worlds/asp-real-world.slime'; lease(key)
        first=Vault.s3.put_object(Bucket=Vault.bucket,Key=key,Body=v1,Metadata={'asp-version':ASP_VERSION,'slime-format-version':'13'})['VersionId']
        checked('Strict SRF validation accepts ASP-produced world and S3 stores first revision')
        server.command(f'execute in {dimension} run setblock 0 64 0 minecraft:emerald_block','Changed the block')
        server.command(f'aswm save {world}',f'{world} saved.')
        v2=source.read_bytes(); assert v1!=v2; (output/'asp-world-v2.slime').write_bytes(v2)
        second=Vault.s3.put_object(Bucket=Vault.bucket,Key=key,Body=v2)['VersionId']; assert first!=second
        assert Vault.s3.get_object(Bucket=Vault.bucket,Key=key)['Body'].read()==v2
        restored=Vault.s3.get_object(Bucket=Vault.bucket,Key=key,VersionId=first)['Body'].read(); assert restored==v1
        checked('Two real ASP revisions retain exact bytes and historical download restores v1')
        # Stop before replacing the file: ASP unload may still have an asynchronous save pending.
        server.stop()
        source.write_bytes(restored)
        server.start()
        check=f'execute in {dimension} if block 0 64 0 minecraft:diamond_block if data block 1 64 0 Items[{{id:"minecraft:diamond",count:7}}] run say ASP_VAULT_RESTORE_OK'
        server.force_chunk(dimension)
        server.command(check,'[Server] ASP_VAULT_RESTORE_OK')
        checked('ASP deserializes restored SRF and confirms original block and chest item NBT')
        clone_key='instances/asp-restored.slime'; lease(clone_key)
        cloned=Vault.s3.copy_object(Bucket=Vault.bucket,Key=clone_key,CopySource={'Bucket':Vault.bucket,'Key':key,'VersionId':first})['VersionId']
        clone_data=Vault.s3.get_object(Bucket=Vault.bucket,Key=clone_key)['Body'].read(); assert clone_data==v1
        clone='asp_vault_clone'; (server.directory/'slime_worlds'/f'{clone}.slime').write_bytes(clone_data)
        server.command(f'aswm add {clone} file',f'{clone} added in')
        server.force_chunk('minecraft:'+clone)
        server.command(f'execute in minecraft:{clone} if block 0 64 0 minecraft:diamond_block run say ASP_VAULT_CLONE_OK','[Server] ASP_VAULT_CLONE_OK')
        checked('Version-specific S3 clone loads as a separate ASP world with original contents')
        server.stop(); server.start()
        server.force_chunk(dimension)
        server.command(check,'[Server] ASP_VAULT_RESTORE_OK')
        checked('Restored ASP world and contents survive a complete server restart')
        report.update({'passed':True,'observed_runtime_build':server.reported_build,'revision_v1':first,'revision_v2':second,'clone_revision':cloned,
            'v1_sha256':hashlib.sha256(v1).hexdigest(),'v2_sha256':hashlib.sha256(v2).hexdigest(),
            'v1_bytes':len(v1),'v2_bytes':len(v2),'source_format':v1[2]})
    except Exception as error:
        report['error']=str(error); raise
    finally:
        if server:
            server.close(); shutil.copy2(server.log_path,output/'asp-console.log')
        if vault_started: Vault.doClassCleanups()
        (output/'result.json').write_text(json.dumps(report,indent=2,ensure_ascii=False)+'\n')
        if args.keep_runtime: print('Runtime:',runtime)
        else: shutil.rmtree(runtime)

if __name__=='__main__': main()

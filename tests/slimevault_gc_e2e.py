#!/usr/bin/env python3
"""Slow real-disk GC test: observes Garage's production 600-second safety grace."""
import argparse
from pathlib import Path
import os
import time
import unittest
from botocore.exceptions import ClientError
from slimevault_e2e import WorldStorage
from slimevault_control_e2e import control

class DiskCollection(WorldStorage):
    def test_physical_reclamation_preserves_shared_blocks(self):
        key = 'worlds/disposable.slime'; clone = 'worlds/shared.slime'
        payload = os.urandom(2_100_000)
        old = self.put(key, payload)
        self.s3.copy_object(Bucket=self.bucket, Key=clone, CopySource={'Bucket': self.bucket, 'Key': key, 'VersionId': old})
        self.put(key, b'current')
        def purge(k, revision): return control(self.endpoint, self.credentials, self.bucket, k, 'purge', {'revision': revision})
        blocks = {p for p in (self.root / 'data').rglob('*') if p.is_file() and len(p.name.split('.')[0]) == 64}
        self.assertTrue(blocks)
        purge(key, old)
        self.assertEqual(self.read(clone)[0], payload)
        self.assertTrue(all(p.exists() for p in blocks), 'Shared blocks were removed while still referenced')
        shared = self.s3.head_object(Bucket=self.bucket, Key=clone)['VersionId']
        self.put(clone, b'current-clone'); purge(clone, shared)
        deadline = time.monotonic() + 720
        print('Waiting for the production block-GC safety grace (600 seconds).', flush=True)
        while time.monotonic() < deadline:
            if all(not p.exists() for p in blocks): break
            time.sleep(2)
        self.assertTrue(all(not p.exists() for p in blocks), 'Unreferenced block files were not reclaimed')
        self.assertEqual(self.read(key)[0], b'current')
        self.assertEqual(self.read(clone)[0], b'current-clone')
        with self.assertRaises(ClientError): self.read(key, old)

if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('--binary', type=Path, default=WorldStorage.binary); parser.add_argument('--keep', action='store_true')
    args = parser.parse_args(); DiskCollection.binary = args.binary.resolve(); DiskCollection.keep = args.keep
    result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite([DiskCollection('test_physical_reclamation_preserves_shared_blocks')]))
    raise SystemExit(not result.wasSuccessful())

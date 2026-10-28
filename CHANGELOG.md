# Changelog

## 0.2.0-dev

- Add durable fixed-authority world leases, renewal/release, fencing and conditional revision commits over authenticated cluster RPC.
- Require leases on the dedicated S3 write path, including multipart part writes, and recheck before final publication.
- Add owner-only immutable snapshots, explicit unprotect, retention policies, dry-run collection and permanent historical revision purge.
- Preserve newest/protected revisions and shared block references; reuse Garage's delayed physical block GC.
- Validate SRF 13 headers, zstd sections, chunk/NBT structure and bounded sizes before publishing worlds, including encrypted and multipart uploads.
- Add real multi-process cluster, Docker network partition, control API and slow physical-GC tests.
- Add an actual ASP 26.3 source-build round-trip test, real SRF fixtures and build/log evidence for commit 3ab54bd. Keep Java integration product code out of scope.

## 0.1.0-dev

- Add the `slimevault` executable and default SlimeWorld-only object-write policy.
- Preserve `.slime` revisions and their block references across completed saves and deletes.
- Add version-aware GET/HEAD, paginated ListObjectVersions, and version-aware CopyObject restore/clone.
- Return revision IDs for multipart completion and delete markers.
- Hide deleted worlds from normal HEAD/GET and object listings.
- Reject destructive version operations and skip inherited lifecycle expiration for worlds.
- Add real-server boto3 tests and document configuration, plugin contract, limitations and upstream ancestry.

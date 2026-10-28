# 월드 제어 API — 0.2

월드 데이터의 전송은 표준 S3입니다. 임대, 보호, 보존 정책은 같은 endpoint와 같은 S3 Signature V4 인증을 사용하는 제어 요청으로 처리합니다. Java 연동 코드는 포함하지 않습니다.

## 요청 형식

```http
PUT /<bucket>/<world-key>.slime
x-slimevault-operation: lease-acquire
Content-Type: application/json
Authorization: <S3 Signature V4>
x-amz-content-sha256: <JSON body SHA-256>

{"ttl_ms":30000}
```

본문은 최대 16 KiB입니다. 모든 변경 작업은 PUT을 사용합니다. `inspect`만 GET도 허용합니다. 제어 응답은 JSON입니다. 성공한 임대 발급의 형태는 다음과 같습니다. 그 외 상태 필드는 생략한 예시입니다.

```json
{"State":{"lease":{"token":"<opaque-token>","actor":"<access-key-id>","fence":1,"expires_at":1790000000000}}}
```

`fence`는 저장된 단조 증가 세대 번호입니다. 클라이언트는 token을 불투명한 값으로 취급합니다. 일반 S3 쓰기 요청에는 `x-slimevault-lease-token` 헤더를 포함해야 합니다. PUT, COPY, DELETE, multipart 생성·part 쓰기·완료·취소가 모두 대상입니다. 서로 다른 키에는 각각 임대가 필요합니다.

| operation | JSON 본문 | 권한·동작 |
| --- | --- | --- |
| `lease-acquire` | `{"ttl_ms":30000}` | 쓰기 권한. 100~300000 ms. 활성 임대가 있으면 충돌 |
| `lease-renew` | `{"token":"...","ttl_ms":30000}` | 같은 access key와 현재 token으로만 갱신 |
| `lease-release` | `{"token":"..."}` | 현재 임대 반납 |
| `inspect` | `{}` | 임대·보호·보존 상태. token은 가려짐 |
| `protect` | `{"revision":"<version-id>"}` | 버킷 owner. 해당 리비전을 보호하고 키 전체를 동결 |
| `unprotect` | `{}` | 버킷 owner. 명시적으로 동결·보호 해제 |
| `retention` | `{"keep_last":10,"max_age_ms":604800000}` | 버킷 owner. 보존 정책 저장 |
| `collect` | `{"dry_run":true}` | 버킷 owner. 정책상 정리 후보 확인. 기본값 dry run |
| `collect` | `{"dry_run":false}` | 버킷 owner. 후보 리비전 영구 정리 |
| `purge` | `{"revision":"<version-id>"}` | 버킷 owner. 특정 과거 리비전 영구 정리 |

owner 제어 작업은 PUT의 쓰기 권한과 owner 권한을 모두 요구합니다.

`inspect`를 PUT으로 호출하면 S3 라우팅상 쓰기 권한이 필요하고 GET으로 호출하면 읽기 권한이 필요합니다. 획득·갱신 응답 외의 상태 조회에서는 활성 token을 공개하지 않습니다.

## 쓰기 순서

1. 키에 대한 임대를 얻습니다.
2. 업로드 중에도 만료되지 않도록 충분히 일찍 갱신합니다.
3. 일반 S3 쓰기에 `x-slimevault-lease-token`을 포함합니다.
4. 필요하면 `x-slimevault-if-revision: <현재 version-id>`로 조건부 저장합니다. 최초 저장의 조건은 `none`입니다.
5. 저장 성공 응답의 VersionId를 확인하고, 작업이 끝나면 임대를 반납합니다.

임대는 access key와 키에 묶입니다. 다른 access key, 만료 token, 이전 세대 token, 조정 노드 재시작 전 token은 거부됩니다. 입력 단계와 최종 확정 단계 모두에서 다시 검증합니다. 데이터 전송 도중 만료됐다면 완료 저장이 거부되며 갱신 후 재시도해야 합니다.

조건부 저장은 조정 노드가 직렬화한 최종 확정 경로에서 비교합니다. 리비전이 바뀌면 HTTP 412입니다. 멀티파트를 완료할 때도 적용합니다. 같은 임대에서 동시에 여러 저장을 실행하는 클라이언트는 이 헤더로 변경 충돌을 감지할 수 있습니다.

## 보호 사본

특정 버전을 `snapshots/<name>.slime`에 CopyObject로 복제한 뒤, 반환된 새 VersionId를 `protect`에 전달합니다. 보호된 키에는 PUT·COPY·DELETE·multipart 변경을 할 수 없습니다. 기존 임대도 폐기합니다. 다운로드와 과거 버전 읽기는 가능합니다.

`unprotect`는 owner의 명시적인 해제입니다. 해제 후 수정하려면 새 임대를 받아야 합니다. 일반 읽기·쓰기 키는 보호를 해제하거나 보존 정책을 바꿀 수 없습니다.

## 보존 정책과 물리 삭제

최신 완료 리비전은 항상 보존합니다. `keep_last`는 삭제 마커를 포함한 완료 리비전 개수이며 최대 100000입니다. `max_age_ms`를 지정하면 최신 N개 밖이면서 해당 기간보다 오래된 리비전만 정리합니다. 생략하면 최신 N개 밖의 리비전이 후보입니다. 최신 리비전과 보호 리비전은 항상 제외합니다.

정책은 조정 노드의 관리 작업자가 약 30초마다 적용합니다. **정책을 설정하면 자동 정리가 활성화됩니다.** `collect` dry run은 현재 정책에서 지금 정리할 후보를 확인하며, 이미 활성화된 작업자의 정리를 중지시키지는 않습니다. 실제 운영 정책은 별도 테스트 버킷에서 먼저 검증하세요. 현재 정책 적용을 사실상 중지하려면 충분히 큰 keep_last와 max_age_ms를 설정합니다.

정리는 CRDT의 단조 증가 `Aborted` 상태로 전파됩니다. 이후 과거 데이터가 늦게 복제되어도 리비전이 되살아나지 않습니다. 실제 블록 파일은 참조 카운트가 0이 된 후 Garage의 **600초 안전 대기와 비동기 정리**를 거쳐 회수됩니다. 공유 블록은 다른 월드·사본이 참조하는 동안 유지됩니다. 정리 요청 성공은 디스크 공간이 즉시 반환됐다는 의미가 아닙니다.

최신 데이터까지 회수하려면 먼저 일반 DELETE로 삭제 마커를 만든 다음 과거 데이터 리비전을 정리합니다. 삭제 마커와 최소 이력 메타데이터는 남습니다. 영구 정리는 복구할 수 없으므로 실제 적용 전에 dry run과 독립 백업을 사용하세요.

## 클러스터 운영

```toml
metadata_fsync = true
data_fsync = true
replication_factor = 3

[s3_api]
world_coordinator = "<조정 노드의 전체 64자리 node ID>"
world_require_lease = true
world_validate_format = true
```

처음 구성할 때는 coordinator 없이 노드를 시작해 각 node ID를 확인할 수 있습니다. 이 상태에서는 다중 노드 월드 쓰기가 거부됩니다. 그다음 모든 노드에 같은 coordinator ID와 bootstrap_peers를 설정해 재시작하고, 저장 노드의 layout을 등록·적용한 뒤 버킷과 키를 만듭니다.

`world_coordinator`는 모든 노드에서 같은 값이어야 합니다. 단일 노드는 생략하면 자기 자신을 사용합니다. 다중 노드에서 생략하면 월드 쓰기를 거부합니다. 기존 네트워크용 암호화·인증 RPC로 조정 노드에 연결합니다. 데이터는 기존 Garage 복제 경로에 저장됩니다.

조정 노드는 임대·보호·보존 상태를 자신의 metadata DB에 영구 저장합니다. **이 제어 상태는 다른 노드에 자동 복제되지 않으며, 조정 노드의 자동 승격·자동 장애 전환은 없습니다.** 조정 노드가 응답하지 않으면 다른 노드가 임의로 쓰기를 허용하지 않고 503을 반환합니다. 저장 데이터는 과반수 접근이 가능한 동안 기존 읽기를 계속할 수 있습니다.

조정 노드의 metadata를 별도로 백업하고 같은 node ID·metadata로 복구해야 합니다. 조정 노드를 바꾸거나 과거 metadata 백업으로 되돌릴 때는 전체 클러스터의 쓰기를 중단하고 제어 상태를 일관되게 이전해야 합니다. 서로 다른 조정 노드를 지정한 클러스터를 동시에 운영하지 마세요.

재시작하면 기존 임대는 모두 무효화됩니다. 보호·보존 정책과 fencing 세대는 유지됩니다. 모든 노드에서 같은 SlimeVault 빌드를 사용해야 합니다. 원본 Garage 또는 서로 다른 제어 RPC 버전의 빌드를 혼합하지 마세요.

타임아웃은 요청이 확정되지 않았다는 보장이 아닙니다. 성공 응답을 잃었을 수 있으므로 현재 버전 확인과 조건부 저장을 사용합니다. 요청 ID 기반 완전한 멱등성은 별도 기능입니다.

## 오류

| 코드 | HTTP | 의미 |
| --- | --- | --- |
| `LeaseConflict` | 409 | 활성 writer 존재 |
| `LeaseExpired` | 409 | 임대 없음·만료·다른 actor·이전 세대 |
| `WorldProtected` | 403 | 보호된 키 또는 삭제 금지 리비전 |
| `PreconditionFailed` | 412 | 기대한 최신 리비전과 다름 |
| `NoSuchVersion` | 404 | 없거나 영구 정리된 리비전 |
| `InvalidRequest` | 400 | 잘못된 제어 본문·TTL·SRF 구조 |
| `ServiceUnavailable` | 503 | 조정 노드 연결 불가 또는 저장 quorum 미달 |

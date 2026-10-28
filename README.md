# SlimeVault

**슬라임월드 전용 S3 저장소.** Garage의 Rust 분산 저장 엔진을 기반으로 월드 파일과 저장 이력을 보관합니다. 영구 월드와 템플릿에서 복제하는 일회성 월드를 같은 S3 인터페이스로 관리합니다.

현재 단계는 **0.2.0 개발 버전**입니다. 저장소 기능을 구현·검증하는 단계이며 ASP 26.3 실서버 호환 인증을 의미하지 않습니다.

## 구현된 기능

- 기본적으로 `.slime` 월드 키만 쓰기 허용. 일반 파일, 빈 이름, 경로 이동 표현을 거부합니다.
- 월드 저장마다 기존 S3 버전 ID를 리비전으로 보존합니다. 새 저장이 완료되어도 과거 월드 데이터와 블록 참조를 제거하지 않습니다.
- `GetObject` / `HeadObject`의 `versionId`로 과거 리비전을 읽습니다.
- `ListObjectVersions`로 최신순 리비전과 삭제 마커를 조회합니다. prefix, key/version marker, max-keys, URL 인코딩과 페이지 순회를 지원합니다.
- `CopyObject`의 버전 지정으로 과거 월드를 **새 리비전으로 복원**하거나 다른 키에 복제합니다.
- 일반 DELETE는 최신 상태를 삭제 마커로 바꿉니다. 과거 리비전은 계속 읽을 수 있습니다.
- 멀티파트 업로드, 업로드 중 기존 완료 버전 읽기, 완료 응답의 버전 ID, 업로드 취소를 지원합니다.
- 임대 획득·갱신·반납과 fencing으로 만료된 writer의 저장을 거부합니다. 최종 확정은 지정된 조정 노드에서 직렬화합니다.
- 기대 리비전을 지정하는 조건부 저장, owner 전용 보호 사본·명시적 보호 해제를 지원합니다.
- 최신 N개와 보존 기간에 따른 자동 정리, dry run, 특정 과거 버전 영구 정리를 지원합니다. 참조가 없는 블록은 안전 대기 후 물리 회수합니다.
- 일반 S3 영구 삭제·일괄 삭제·버전 관리 중단은 계속 거부합니다. 영구 정리는 별도 owner 제어 API로 수행합니다.
- AWS Signature V4, 권한 관리, 블록 저장·복제는 Garage 기반을 사용합니다.

기본적으로 **SRF 13 바이너리 구조를 검사**합니다. 헤더, zstd, 청크 필드와 NBT 경계를 검증하고 잘못된 파일을 거부합니다. [포맷 검증 범위](docs/SLIME-FORMAT.md)를 참고하세요. 고정된 ASP 26.3 소스 빌드에서 실제 월드 저장·복원·재로드도 확인했습니다. [실제 서버 검증](docs/ASP-26.3-VALIDATION.md)을 참고하세요.

0.2에서는 저장 전에 writer 임대를 얻고 S3 쓰기에 `x-slimevault-lease-token`을 포함해야 합니다. [월드 제어 API](docs/WORLD-CONTROL.md)에 임대·조건부 저장·보호·보존 정책 사용법을 정리했습니다.

## 빌드

Rust stable과 C 빌드 도구가 필요합니다. 재현 가능한 의존성 버전은 `Cargo.lock`으로 고정합니다.

```sh
cargo build --locked -p garage --bin slimevault --no-default-features --features bundled-libs,sqlite
./target/debug/slimevault --version
```

내부 crate와 `garage` 호환 실행 파일 이름은 upstream 변경 추적을 위해 유지합니다. SlimeVault 제품 버전과 Garage 내부 버전은 [VERSION](VERSION), [버전 관리 정책](docs/VERSIONING.md)을 참고하세요.

## 로컬 시작

[config/slimevault.toml](config/slimevault.toml)을 복사하고 RPC secret을 생성해 넣습니다. 아래 예시는 단일 노드 개발용이며 TLS는 앞단 프록시에서 구성합니다.

```sh
python3 -c 'import secrets; print(secrets.token_hex(32))'
./target/debug/slimevault -c config/slimevault.toml server
```

별도 터미널에서 노드를 등록합니다.

```sh
./target/debug/slimevault -c config/slimevault.toml node id
# 출력된 노드 ID를 아래 NODE_ID에 대입
./target/debug/slimevault -c config/slimevault.toml layout assign NODE_ID -z local -c 1G
./target/debug/slimevault -c config/slimevault.toml layout apply --version 1
./target/debug/slimevault -c config/slimevault.toml key create world-server
./target/debug/slimevault -c config/slimevault.toml bucket create worlds
# 위에서 생성한 키 ID를 KEY_ID에 대입
./target/debug/slimevault -c config/slimevault.toml bucket allow worlds --read --write --owner --key KEY_ID
```

S3 endpoint는 `http://127.0.0.1:3900`, region은 `slimevault`이며 path-style을 사용합니다. SDK에 발급한 access key와 secret key를 설정하세요.

## 월드 운영 규칙

버킷은 권한 경계입니다. 키의 디렉터리처럼 보이는 prefix는 접근 권한을 분리하지 않습니다. 서로 다른 접근 권한이 필요하면 버킷·키를 분리하세요.

| 용도 | 키 예시 | 동작 |
| --- | --- | --- |
| 영구 월드 | `worlds/player-uuid.slime` | 저장할 때마다 새 리비전 |
| 템플릿 | `templates/arena.slime` | 특정 리비전을 복제 원본으로 지정 |
| 실행 인스턴스 | `instances/match-uuid.slime` | 템플릿 복제 후 저장 또는 논리 삭제 |
| 이름 붙인 사본 | `snapshots/before-update.slime` | 원하는 리비전을 CopyObject로 복사 |

이름 붙인 사본에 `protect`를 적용하면 수정·삭제를 막는 보호 스냅샷이 됩니다. 인스턴스 삭제는 논리 삭제이며, 보존 정책이나 명시적 정리를 적용해야 과거 데이터가 회수됩니다. 기본값은 이력 무기한 보존입니다.

[라이브러리·플러그인 연동 계약](docs/PLUGIN-CONTRACT.md)에 저장, 복원, 복제 예시와 책임 범위를 정리했습니다.

## 실제 서버 테스트

```sh
python3 -m pip install -r tests/requirements.txt
python3 tests/slimevault_e2e.py --binary target/debug/slimevault
python3 tests/slimevault_control_e2e.py --binary target/debug/slimevault
python3 tests/slimevault_cluster_e2e.py --binary target/debug/slimevault
# Docker daemon and debian:trixie-slim are needed for actual network isolation.
python3 tests/slimevault_partition_e2e.py --binary target/debug/slimevault
# Slow: observes the actual 600-second block-GC safety grace.
python3 tests/slimevault_gc_e2e.py --binary target/debug/slimevault
```

테스트는 임시 디렉터리에 실제 단일 노드 서버를 실행하고 boto3로 S3 요청을 보냅니다. 외부 서비스나 기존 서버를 변경하지 않습니다. `--keep`을 붙이면 진단용 서버 로그와 임시 데이터를 보존합니다.

검증 범위와 결과는 [검증 기록](docs/VALIDATION.md)을 참고하세요. 기본 데이터 경로 회귀 테스트는 합성 바이트와 명시적 호환 설정을, 제어 경로 테스트는 기본 임대·SRF 검사와 합성 SRF 13 fixture를 사용합니다. 실제 ASP 26.3 시나리오와 재현 명령은 [별도 검증 문서](docs/ASP-26.3-VALIDATION.md)에 있습니다.

## 운영상 경계

- 조정 노드는 고정된 단일 authority입니다. 연결이 끊기면 쓰기를 거부하며 **자동 장애 전환은 없습니다**. 제어 상태는 조정 노드 metadata에 저장되므로 별도 백업이 필요합니다.
- 모든 노드는 같은 조정 노드와 같은 SlimeVault 빌드를 사용해야 합니다. 원본 Garage와 혼합하거나 백업 없이 다운그레이드하지 마세요.
- 리비전 정리 후 블록 파일 회수에는 기존 엔진의 600초 안전 대기와 비동기 처리 시간이 걸립니다. 공유 블록은 마지막 참조가 없어질 때까지 유지됩니다.
- `ListObjectVersions`의 delimiter 그룹화는 지원하지 않습니다.
- ASP 26.3은 `3ab54bd` 소스 빌드의 실제 생성 월드로 검증했습니다. 모든 빌드·데이터 조합에 대한 보증은 아닙니다. Java 연동과 Minecraft 데이터 변환은 포함하지 않습니다.
- 타임아웃 후 요청 결과는 불확정일 수 있습니다. 상태 확인·조건부 저장으로 재시도를 제어하세요.

## 기반 프로젝트와 라이선스

[Garage](https://garagehq.deuxfleurs.fr/)를 기반으로 합니다. 원저작권 표시와 [AGPL-3.0 라이선스](LICENSE)를 유지합니다. 기존 Garage 안내는 [upstream README](docs/GARAGE-UPSTREAM-README.md)에 보존했습니다.

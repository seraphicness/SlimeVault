# SlimeVault

**슬라임월드 파일을 위한 S3 저장소.** SlimeVault는 Minecraft 서버의 `.slime` 월드를 저장하고, 이전 리비전을 보관·복원하는 Rust 기반 저장 서비스입니다. S3 호환 API를 사용하므로 게임 서버와 저장소를 분리해 운영할 수 있습니다.

> **개발 버전 0.2.0** — 현재 기능을 구현하고 검증하는 단계입니다. ASP 26.3의 실제 월드 저장·복원 시나리오를 확인했지만, 모든 ASP 빌드와 데이터에 대한 호환성을 보증하지는 않습니다.

## 무엇을 할 수 있나요?

- 월드 저장 때마다 새 리비전을 만들어 이전 상태를 유지합니다. 특정 리비전을 읽거나 새 최신 리비전으로 복원할 수 있습니다.
- 임대(lease)와 fencing으로 같은 월드에 대한 동시 쓰기를 제어합니다. 조건부 저장으로 최신 리비전이 바뀐 경우도 감지합니다.
- SRF 13 파일 구조를 기본 검사해 잘못되거나 손상된 월드 파일을 거부합니다.
- 스냅샷 보호, 리비전 보존 정책, 명시적 정리를 제공합니다.
- Garage의 분산 저장 엔진을 사용해 S3 인증, 권한 관리, 블록 복제와 저장을 처리합니다.

## 빠른 시작

아래는 로컬 개발을 위한 단일 노드 구성입니다. Rust stable과 C 빌드 도구가 필요합니다. `Cargo.lock`에 의존성 버전이 고정되어 있습니다.

### 1. 빌드

```sh
git clone https://github.com/seraphicness/SlimeVault.git
cd SlimeVault
cargo build --locked -p garage --bin slimevault --no-default-features --features bundled-libs,sqlite
```

Windows에서는 실행 파일 경로가 `target\debug\slimevault.exe`입니다. macOS/Linux에서는 `target/debug/slimevault`를 사용합니다.

### 2. 설정 및 서버 시작

개발 설정을 복사하고 `rpc_secret`에 임의의 64자리 16진수 값을 넣습니다. 생성한 secret은 공개하거나 저장소에 커밋하지 마세요.

```sh
cp config/slimevault.toml config/local.toml
python3 -c 'import secrets; print(secrets.token_hex(32))'
```

`config/local.toml`의 `rpc_secret`을 생성한 값으로 바꾼 뒤 서버를 실행합니다.

```sh
./target/debug/slimevault -c config/local.toml server
```

Windows PowerShell에서는 다음을 실행합니다.

```powershell
.\target\debug\slimevault.exe -c config\local.toml server
```

### 3. 노드와 버킷 준비

서버를 켜 둔 채 다른 터미널에서 노드 레이아웃과 S3 접근 키를 준비합니다.

```sh
./target/debug/slimevault -c config/local.toml node id
```

출력된 노드 ID를 `NODE_ID`에 넣고 다음 명령을 실행합니다.

```sh
./target/debug/slimevault -c config/local.toml layout assign NODE_ID -z local -c 1G
./target/debug/slimevault -c config/local.toml layout apply --version 1
./target/debug/slimevault -c config/local.toml key create world-server
./target/debug/slimevault -c config/local.toml bucket create worlds
./target/debug/slimevault -c config/local.toml bucket allow worlds --read --write --owner --key KEY_ID
```

`KEY_ID`는 `key create`에서 출력된 키 ID로 바꾸세요. 함께 출력되는 secret key는 안전한 곳에 보관합니다. `bucket allow` 명령의 `KEY_ID`도 같은 값입니다.

### 4. S3 클라이언트 연결

클라이언트에 다음 연결 정보를 설정합니다.

| 설정 | 값 |
| --- | --- |
| Endpoint | `http://127.0.0.1:3900` |
| Region | `slimevault` |
| 인증 | AWS Signature Version 4 |
| 주소 방식 | Path-style |
| Bucket 예시 | `worlds` |
| 객체 키 예시 | `worlds/player-uuid.slime` |

S3 쓰기 전에 해당 객체 키의 writer 임대를 획득해야 합니다. 업로드 요청에는 응답으로 받은 `x-slimevault-lease-token`을 포함하세요. 자세한 요청 형식과 boto3 연동 계약은 [월드 제어 API](docs/WORLD-CONTROL.md)와 [플러그인 연동 가이드](docs/PLUGIN-CONTRACT.md)를 참고하세요.

## 월드 저장 방식

| 용도 | 키 예시 | 동작 |
| --- | --- | --- |
| 영구 월드 | `worlds/player-uuid.slime` | 저장할 때마다 새 리비전 생성 |
| 템플릿 | `templates/arena.slime` | 지정한 리비전을 새 월드로 복제 |
| 실행 인스턴스 | `instances/match-uuid.slime` | 템플릿에서 생성하고 필요하면 논리 삭제 |
| 보존 스냅샷 | `snapshots/before-update.slime` | 원하는 리비전을 복사해 보호 가능 |

일반 S3 DELETE는 최신 월드를 숨기는 삭제 마커를 만듭니다. 이전 리비전은 유지되며, 일반 S3 요청으로 영구 삭제할 수 없습니다. 기본 보존 기간은 무기한입니다. 자동 정리나 영구 정리 전에는 [보존 정책 설명](docs/WORLD-CONTROL.md#보존-정책과-물리-삭제)을 읽고 백업을 준비하세요.

## 기능과 문서

- [월드 제어 API](docs/WORLD-CONTROL.md): 임대, 조건부 저장, 보호, 보존 정책
- [SRF 13 검사 범위](docs/SLIME-FORMAT.md): 파일 구조 검증과 제한 크기
- [ASP 26.3 실제 서버 검증](docs/ASP-26.3-VALIDATION.md): 테스트 대상과 검증 범위
- [전체 검증 기록](docs/VALIDATION.md): 단위 테스트, 실제 서버 및 클러스터 시나리오
- [버전 관리 정책](docs/VERSIONING.md): 업그레이드와 호환성 주의사항

## 운영 시 주의사항

- 다중 노드에서는 모든 노드가 같은 `world_coordinator`와 SlimeVault 빌드를 사용해야 합니다.
- 조정 노드의 제어 metadata는 다른 노드에 자동 복제되지 않습니다. 자동 조정 노드 장애 조치는 없으므로 metadata를 별도로 백업하세요.
- 만료되거나 조정 노드 재시작 이전에 발급된 임대 토큰은 사용할 수 없습니다. 요청 시간 초과 뒤에는 현재 리비전을 확인하고 조건부 저장을 사용하세요.
- 리비전 정리 후 실제 블록 회수까지는 600초 안전 대기와 비동기 작업 시간이 걸릴 수 있습니다.
- 저장소에는 Java 라이브러리나 Minecraft 플러그인이 포함되지 않습니다. 월드 직렬화와 게임 내 로딩은 연동 측에서 처리해야 합니다.

## 테스트

실제 서버 기반 테스트는 빌드된 바이너리와 Python 의존성이 필요합니다.

```sh
python3 -m pip install -r tests/requirements.txt
python3 tests/slimevault_e2e.py --binary target/debug/slimevault
python3 tests/slimevault_control_e2e.py --binary target/debug/slimevault
python3 tests/slimevault_cluster_e2e.py --binary target/debug/slimevault
```

네트워크 단절 테스트에는 Docker가 필요합니다. 물리 블록 회수 테스트는 600초 안전 대기를 포함합니다. 전체 재현 명령은 [검증 기록](docs/VALIDATION.md)에 있습니다.

## 기반 프로젝트와 라이선스

SlimeVault는 [Garage](https://garagehq.deuxfleurs.fr/)를 기반으로 하며 [AGPL-3.0](LICENSE) 라이선스를 따릅니다. Garage 기반 코드의 원저작권 고지는 유지됩니다.

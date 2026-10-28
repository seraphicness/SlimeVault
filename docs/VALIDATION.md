# 검증 기록 — 0.2.0-dev

검증 환경: Linux x86_64, Rust 1.99.1, Python 3.12, boto3 1.43.120, zstandard 0.25.0. 단일 노드·3개 독립 프로세스·3개 Docker 컨테이너에서 실제 서버를 실행했습니다.

| 검사 | 결과 |
| --- | --- |
| Cargo.lock 고정 SQLite + bundled libs 빌드 | 통과 |
| S3·SRF 단위 테스트 | 41개 통과 (실제 ASP 26.3 fixture 회귀 포함) |
| 데이터 모델 병합 단위 테스트 | 2개 통과 |
| 기존 월드 S3 실제 서버 E2E, 명시적 호환 설정 | 7개 통과 |
| 기본 임대·SRF 검사가 켜진 제어·보호·보존 E2E | 10개 통과 (자동 보존 별도 실행 포함) |
| 실제 3노드 동시성·노드 장애·복구 | 3개 통과 |
| Docker 네트워크 실제 단절·복구 | 1개 통과 |
| 실제 디스크 블록 회수, 600초 안전 대기 포함 | 1개 통과, 약 621초 |
| 기존 Garage S3 통합 회귀 | 38개 통과 |

**회귀 테스트 누적 103개가 통과했습니다.** 실제 ASP fixture 검사 1개를 추가했고, 이번에 단위 테스트 43개를 재실행해 통과했습니다. 자동 보존 작업자는 수동 collect 호출 없이 과거 리비전을 정리하는 별도 실제 서버 테스트로 확인했습니다.

## 0.2에서 확인한 내용

- 8개 클라이언트와 여러 S3 진입 노드가 경쟁해도 활성 임대 하나만 획득합니다.
- 만료·반납·이전 세대·다른 access key·재시작 이전 token의 쓰기를 거부합니다.
- 기대 리비전 불일치는 기존 월드를 바꾸지 않습니다.
- 보호 사본의 수정·삭제·multipart 변경과 비 owner의 보호 작업을 거부합니다.
- 보존 정책 dry run, 과거 리비전 영구 정리, 이후 저장에서도 정리된 리비전이 되살아나지 않음을 확인했습니다.
- 잘린 SRF, 잘못된 magic/version, trailing data, 압축·NBT 구조 오류를 검출합니다.
- SSE-C 데이터와 조립된 multipart 데이터를 공개 전에 검증합니다. 잘못된 multipart는 기존 완료 리비전을 유지합니다.
- 조정 노드 장애 시 대체 노드가 임의로 쓰기를 허용하지 않습니다. 같은 조정 노드 복구 후 이전 임대를 거부하고 다시 저장할 수 있습니다.
- 저장 노드 하나가 없어도 과반수로 동작하고 과반수를 잃으면 쓰기를 거부합니다. 노드 복귀 후 기존 데이터를 읽습니다.
- Docker bridge에서 조정 노드 컨테이너의 네트워크를 실제 disconnect합니다. 다른 노드의 새 임대·쓰기가 503으로 실패하고 재연결 후 저장이 재개됨을 확인했습니다.
- 원본 리비전을 정리해도 사본이 참조하는 블록은 유지됩니다. 마지막 참조를 정리한 뒤 600초 지연 GC가 실제 파일을 삭제하고 최신 월드는 정상적으로 읽힙니다.

## 재현 명령

```sh
cargo build --locked -p garage --no-default-features --features bundled-libs,sqlite
cargo test --locked -p garage_model -p garage_api_s3 --lib --features garage_db/bundled-libs
python3 -m pip install -r tests/requirements.txt
python3 tests/slimevault_e2e.py
python3 tests/slimevault_control_e2e.py
python3 tests/slimevault_cluster_e2e.py
# Docker와 이미지가 필요합니다.
docker pull debian:trixie-slim
python3 tests/slimevault_partition_e2e.py
# 약 10분 이상의 실제 안전 대기를 포함합니다.
python3 tests/slimevault_gc_e2e.py
GARAGE_TEST_INTEGRATION_DB_ENGINE=sqlite cargo test --locked -p garage \
  --no-default-features --features bundled-libs,sqlite --test integration s3::
```

테스트 실행 중 바이너리를 다른 빌드로 교체하지 마세요. 클러스터의 모든 프로세스·컨테이너는 같은 빌드여야 합니다.

## ASP 26.3 실제 서버 후속 검증

기존 102개 테스트와 실제 fixture 회귀 1개에 더해, 고정된 공식 26.3 소스 빌드를 실제로 실행한 8단계 시나리오가 통과했습니다. [대상 커밋·빌드·블록/NBT 검증 결과](ASP-26.3-VALIDATION.md)를 참고하세요.

## 한계

기본 데이터 경로 테스트는 합성 fixture를 사용합니다. 후속 실제 서버 테스트는 ASP 26.3의 `3ab54bd` 소스 빌드와 실제 생성 월드로 저장·복원·재로드를 검증했습니다. 공식 CI 배포 JAR와 모든 ASP 빌드에 대한 검증은 아닙니다. Java 연동 제품 코드는 요청에 따라 제외했습니다.

조정 노드 제어 metadata의 자동 복제·자동 장애 전환은 제공하지 않습니다. 여기서 확인한 것은 고정 조정 노드의 안전한 쓰기 중단과 복구입니다. 장기간 부하·대규모 클러스터 성능 보증, 실제 전원 차단·디스크 손상 시험은 수행하지 않았습니다. CI 구성은 추가했지만 원격 실행 결과는 아닙니다.

---

## 이전 0.1 검증 기록

# 검증 기록 — 0.1.0-dev

검증 환경: Linux x86_64, Rust 1.99.1, Python 3.12, boto3 1.43.120, 단일 노드 SQLite. 대상은 실제 로컬 SlimeVault 프로세스이며 S3 요청은 SDK로 서명해 전송했습니다.

| 검사 | 결과 |
| --- | --- |
| Cargo.lock 고정 빌드, SQLite + bundled libs | 통과 |
| S3 crate 단위 테스트 | 38개 통과 |
| 월드 이력 CRDT 병합·일반 객체 압축 회귀 단위 테스트 | 2개 통과 |
| SlimeVault 실제 서버 E2E | 7개 통과 |
| 기존 Garage S3 통합 회귀 테스트, 호환 모드 | 38개 통과 |
| rustfmt, diff whitespace, 문서 내부 링크, Python 구문 | 통과 |

## 실행 명령

```sh
cargo build --locked -p garage --no-default-features --features bundled-libs,sqlite
cargo test --locked -p garage_model -p garage_api_s3 --lib --features garage_db/bundled-libs
python3 -m pip install -r tests/requirements.txt
python3 tests/slimevault_e2e.py --binary target/debug/slimevault
GARAGE_TEST_INTEGRATION_DB_ENGINE=sqlite cargo test --locked -p garage \
  --no-default-features --features bundled-libs,sqlite --test integration s3::
cargo fmt --all --check
git diff --check
```

## E2E에서 확인한 시나리오

1. 작은 바이너리와 블록 저장이 필요한 큰 바이너리 저장, 과거 GET/HEAD, 최신순 이력, 새 리비전으로 복원, 버전 지정 복제, 논리 삭제, 삭제 후 목록/HEAD, 강제 종료 후 재기동과 과거 데이터 복구.
2. 일반 파일·잘못된 경로 거부, 버전 관리 활성 상태, 중단·영구 버전 삭제·일괄 삭제 거부.
3. XML 이스케이프가 필요한 키, 한 월드의 여러 리비전 페이지 순회.
4. 멀티파트 업로드 중 이전 완료 버전 유지, 업로드 완료 후 버전 ID와 데이터 확인, 취소 후 완료 버전 유지.
5. 130개 월드와 내부 스캔 경계를 넘는 버전 목록 페이지 순회.
6. 체크섬 불일치 업로드 거부, 이전 최신 버전과 버전 목록 유지, 없는 리비전의 NoSuchVersion 응답.
7. 물음표·공백·한글을 포함하는 키의 버전 지정 복사와 메타데이터 보존.

## 반복 과정에서 수정한 항목

- 클라이언트가 자동으로 요청하는 `encoding-type=url` 처리.
- 멀티파트 완료 응답에서 누락되던 `x-amz-version-id` 추가.
- 이력을 유지할 때 과거 데이터가 최신 월드처럼 노출되지 않도록 HEAD와 목록 선택 수정.
- 강제 종료 재시작 테스트로 완료된 저장의 지속성 검증.

## 검증 한계

테스트 바이너리는 합성 데이터입니다. ASP 26.3의 실제 월드 역직렬화·로드, Java 라이브러리·플러그인, 분산 임대·동시 쓰기 충돌 방지, 다중 노드 네트워크 분할, 대규모 장기 운영 및 안전한 물리 삭제는 검증하지 않았습니다. 원본 Garage의 일부 경고와 이중 실행 파일 대상 경고는 남아 있지만 빌드·테스트 실패는 없습니다.

GitHub Actions workflow는 추가했으나 원격에서 실행하지 않았습니다. 로컬 검증과 CI 실행 결과를 혼동하지 않습니다.

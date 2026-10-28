# ASP 26.3 실제 서버 검증

2026-10-28 기준, **실제 ASP 서버에서 8단계 저장·복원 검증이 통과했습니다.** 기존 SlimeVault 102개 테스트와 별도로 수행한 실제 서버 시나리오입니다.

## 정확한 대상

| 항목 | 값 |
| --- | --- |
| 공식 저장소 | InfernalSuite/AdvancedSlimePaper |
| 브랜치 | `dev/26.3` |
| 고정 커밋 | `3ab54bd48c7ecd28335d88e193b9c87af9275c49` |
| 소스 버전 | `26.3.build.191-stable` |
| 실제 서버 표기 | `AdvancedSlimePaper 26.3-DEV-HEAD@3ab54bd` |
| 구현 API | `26.3.build.191-stable` |
| ASP API | `4.2.0-SNAPSHOT` |
| 포맷 | SRF 13 |
| 실행 Java | Temurin 25.0.5+8-LTS |
| 빌드 도구 | Gradle 9.4.1 |

공식 API/CI 다운로드는 이 환경에서 403을 반환했습니다. 따라서 **공식 소스의 해당 커밋을 수정하지 않고 직접 빌드**한 서버와 공식 ASP 관리 플러그인을 사용했습니다. 공식 CI 배포 JAR 자체를 내려받아 검증한 것은 아닙니다.

서버 JAR SHA-256:

```text
e20bd78e554662850126ae0892453370a351723b314cafd130464095e943bd69
```

## 실제로 수행한 검증

1. ASP 서버와 공식 ASPaperPlugin 부팅, 서버·플러그인 커밋 식별 확인.
2. 서버가 Minecraft 26.3과 API `26.3.build.191-stable`을 보고하는지 확인.
3. 새 슬라임월드를 만들고 다이아몬드 블록·상자·다이아몬드 7개를 배치한 뒤 ASP로 직렬화.
4. 쓰기 임대와 SRF 구조 검사가 활성화된 SlimeVault에 실제 월드 업로드.
5. 블록을 에메랄드로 바꾼 두 번째 버전 저장, 최신/과거 버전 바이트·SHA-256 비교.
6. 과거 버전을 다운로드해 ASP에서 다시 로드하고 다이아몬드 블록과 상자 아이템 7개를 콘솔 조건문으로 확인.
7. 특정 버전의 S3 CopyObject 결과를 별도 ASP 월드로 로드해 원래 블록 확인.
8. ASP 서버 전체 종료·재시작 후 복원 월드의 블록·상자 아이템 유지 확인.

실제 생성한 파일은 v1 732 bytes, v2 743 bytes이며 [fixture와 결과](../tests/fixtures/asp-26.3/README.md)에 보존했습니다. 작은 실제 월드를 대상으로 한 기능 검증이며 대형 월드 성능 시험은 아닙니다.

콘솔의 성공 표식:

```text
[Server] ASP_VAULT_RESTORE_OK
[Server] ASP_VAULT_CLONE_OK
[Server] ASP_VAULT_RESTORE_OK
```

실제 콘솔에는 오프라인 모드 표시 때문에 `[Not Secure]` 접두사가 함께 기록됩니다. 블록/상자 NBT 조건이 만족될 때에만 이 표식을 출력하도록 실행했습니다.

## 재현

먼저 Java 25로 고정된 공식 소스를 빌드합니다.

```sh
git clone https://github.com/InfernalSuite/AdvancedSlimePaper.git asp-source
cd asp-source
git checkout 3ab54bd48c7ecd28335d88e193b9c87af9275c49
./gradlew --max-workers=2 applyAllPatches
./gradlew --max-workers=2 :aspaper-server:createPaperclipJar :plugin:shadowJar
```

SlimeVault 저장소에서 다음을 실행합니다. Minecraft EULA를 수락한 경우에만 `--accept-eula`를 지정합니다.

```sh
python3 -m pip install -r tests/requirements.txt
python3 tests/asp_26_3_e2e.py \
  --java /path/to/jdk25/bin/java \
  --server-jar /path/to/asp-source/aspaper-server/build/libs/aspaper-paperclip-26.3.build.191-stable.jar \
  --plugin-jar /path/to/asp-source/plugin/build/libs/asp-plugin-4.2.0-SNAPSHOT.jar \
  --slimevault target/debug/slimevault \
  --output /tmp/asp-26.3-result \
  --accept-eula
```

프로세스는 루프백 주소만 사용합니다. 프록시 환경에서는 Java truststore를 `--truststore`로 전달할 수 있습니다. 테스트는 공개 서버를 만들거나 운영 월드를 수정하지 않습니다. 새 Java 라이브러리·플러그인은 추가하지 않습니다.

## 범위와 한계

이 고정 커밋의 소스 빌드와 실제 생성 월드에 대한 저장·복원·재로드 호환을 확인했습니다. 다른 ASP 빌드 전체, 모든 Minecraft 데이터 조합, 대규모 월드, 다수 플레이어 부하를 인증하는 결과는 아닙니다.

콘솔에서 외부 버전 조회와 Mojang 서비스 검색의 네트워크 오류가 관찰됐습니다. 테스트는 로컬 오프라인 모드로 진행했으며 플레이어 로그인·온라인 인증은 검증 범위가 아닙니다. 해당 외부 조회 오류와 별개로 월드 생성·저장·복원·재로드·재시작 검증은 모두 통과했습니다.

초기 테스트에서는 새 월드 청크가 로드되지 않아 블록 설정이 거부됐습니다. 테스트를 수정해 `forceload`와 `execute if loaded`로 청크 로드를 확인한 후 검증했습니다. 또한 unload의 비동기 저장과 파일 교체가 경쟁하지 않도록 서버를 완전히 종료한 후 복원 파일을 배치합니다.

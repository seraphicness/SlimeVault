# 슬라임월드 바이너리 검증

0.2의 기본 쓰기 경로는 SRF 13 파일을 구조 검증합니다. `.slime` 확장자만 보고 일반 파일을 월드로 받아들이던 0.1과 다릅니다.

기준 소스는 [AdvancedSlimePaper](https://github.com/InfernalSuite/AdvancedSlimePaper/tree/031913915008f865e13746ae6e661899542b72cf)의 SlimeFormat, SlimeSerializer, v13SlimeWorldDeSerializer입니다. 이 공개 빌드는 `26.2.build.134-stable`이며 SRF 버전은 13입니다. 이후 공식 `dev/26.3`의 `3ab54bd` 커밋(`26.3.build.191-stable`)도 확인했으며 직렬화·역직렬화 코드가 이 기준과 같았습니다. 해당 소스 빌드로 실제 생성한 파일을 저장·복원·재로드했습니다.

검사 항목:

- `B1 0B` 파일 식별자, SRF 버전 13, Minecraft data version 필드.
- 압축된 두 섹션의 크기, zstd 해제 오류, 해제 후 길이 일치, 파일 끝 경계.
- 청크 개수·좌표 중복·섹션 개수·라이트 배열 크기.
- 청크의 block state, biome, heightmap, 추가 데이터, 엔티티·타일 엔티티·extra NBT 필드 경계.
- NBT root compound, 태그 종류·배열 길이·리스트 개수, 최대 중첩 64와 요소 예산.
- 잘린 파일과 추가로 붙인 데이터 거부.

기본 제한은 압축된 월드 파일 256 MiB, 압축 해제 총합 1 GiB입니다. `world_max_bytes`, `world_max_expanded_bytes`로 조정합니다. 검증 시 임시 파일을 사용하고 Linux에서는 열린 직후 unlink하여 프로세스가 중단되어도 임시 파일이 남지 않도록 합니다. 무거운 해제·파싱 작업은 동시에 2개까지 수행합니다.

일반 PUT과 폼 업로드는 입력 바이트를, CopyObject는 복호화한 원본을, 멀티파트는 조립된 전체 바이트를 최종 공개 전에 검사합니다. SSE-C 암호화 전의 데이터를 검증합니다. 검증 실패 시 이전 완료 리비전을 유지합니다.

범용 S3 회귀 테스트용으로 `slime_world_only = false`를 사용하거나, 신뢰하는 직렬화기의 별도 포맷을 저장하려면 `world_validate_format = false`로 구조 검사를 끌 수 있습니다. 이 설정을 끈 데이터는 정상 ASP 월드로 검증됐다고 볼 수 없습니다.

구조 검증은 Minecraft 레지스트리·블록 이름·게임 로직의 의미 검증이나 월드 버전 변환을 대신하지 않습니다. 합성 fixture·손상 입력 테스트에 더해 고정된 ASP 26.3 소스 빌드의 실제 월드 로드도 확인했습니다. [실제 서버 검증](ASP-26.3-VALIDATION.md)의 범위와 한계를 참고하세요. Java 라이브러리와 플러그인은 이 변경에 포함하지 않습니다.

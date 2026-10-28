# ASP 라이브러리·플러그인 연동 계약 v1

대상은 ASP 26.3입니다. 공식 `dev/26.3` 커밋 `3ab54bd48c7ecd28335d88e193b9c87af9275c49`를 직접 빌드한 서버에서 실제 생성 월드를 저장·복원·재로드하는 테스트를 수행했습니다. 자세한 범위는 [실제 서버 검증](ASP-26.3-VALIDATION.md)을 참고하세요. 저장소는 Minecraft나 슬라임 포맷 버전을 추측하거나 변환하지 않습니다.

## 연결과 저장

AWS S3 SDK의 endpoint override, path-style, Signature V4, region `slimevault`를 사용합니다. 키는 대소문자를 구분하며 `.slime`으로 끝나야 합니다. 예: `worlds/<uuid>.slime`, `templates/<id>.slime`, `instances/<uuid>.slime`. 월드 바이너리 하나가 객체 하나입니다.

플러그인은 서버에서 일관된 월드 저장 시점을 확보한 뒤 ASP로 직렬화합니다. 저장할 바이트가 확정된 후 PutObject 또는 multipart upload를 수행하고, 성공 응답의 `VersionId`를 저장 확인값으로 기록합니다. HTTP 요청 전송 시작이나 part 업로드 성공은 월드 저장 완료가 아닙니다.

다음 S3 사용자 메타데이터를 권장합니다. 아직 필수 검증하거나 스키마를 강제하지 않습니다.

| 메타데이터 | 예시 | 의미 |
| --- | --- | --- |
| `minecraft-version` | 실제 서버 버전 | 월드 호환성 확인 |
| `slime-format-version` | 실제 포맷 버전 | ASP 역직렬화 호환성 확인 |
| `asp-version` | 실제 ASP 빌드 | 라이브러리 호환성 확인 |
| `world-id` | UUID | 논리 월드 식별자 |
| `save-reason` | `autosave` | 저장 목적 |

## 표준 S3 호출 예시

아래는 boto3 표기이며 Java SDK에서도 같은 S3 작업을 사용합니다.

```python
saved = s3.put_object(
    Bucket="worlds", Key="worlds/player.slime", Body=serialized_world,
    Metadata={"world-id": world_id, "save-reason": "autosave"},
)
revision = saved["VersionId"]

historical = s3.get_object(
    Bucket="worlds", Key="worlds/player.slime", VersionId=revision,
)

# 과거 버전을 새로운 최신 리비전으로 복원
restored = s3.copy_object(
    Bucket="worlds", Key="worlds/player.slime",
    CopySource={"Bucket": "worlds", "Key": "worlds/player.slime", "VersionId": revision},
)

# 특정 템플릿 리비전에서 실행 인스턴스 생성
s3.copy_object(
    Bucket="worlds", Key="instances/match-uuid.slime",
    CopySource={"Bucket": "worlds", "Key": "templates/arena.slime", "VersionId": template_revision},
)
```

리비전 ID는 저장소에서 발급한 64자리 hex 문자열입니다. 복원은 새 리비전을 발급하며 예전 ID의 내용을 바꾸지 않습니다. 과거 버전을 읽으려면 해당 버킷에 읽기 권한이 필요합니다.

## 삭제와 이력

DELETE without versionId는 최신 월드를 숨기는 삭제 마커를 만듭니다. 일반 GET/HEAD는 404를 반환하고 ListObjects에서 빠지지만 과거 versionId로는 읽을 수 있습니다. 일반 S3의 versionId 영구 삭제와 일괄 삭제는 거부합니다. owner 제어 API의 보존 정책·명시적 정리를 사용하면 과거 데이터와 미참조 블록을 회수합니다.

ListObjectVersions는 key 순서, 각 key 내부에서는 최신 리비전 순서로 반환합니다. SDK paginator 또는 NextKeyMarker와 NextVersionIdMarker를 함께 사용하세요. delimiter 그룹화는 지원하지 않습니다. `encoding-type=url`은 지원합니다. 존재하지 않는 리비전은 NoSuchVersion, 잘못된 형식의 ID는 InvalidRequest로 반환합니다.

## 0.2 저장 계약

저장 전에 [월드 제어 API](WORLD-CONTROL.md)로 writer 임대를 얻고 일반 S3 쓰기에 `x-slimevault-lease-token`을 포함합니다. 긴 업로드는 완료 전까지 갱신해야 합니다. `x-slimevault-if-revision`으로 기대 최신 리비전을 지정할 수 있습니다.

보호 사본·보존 기간·영구 정리는 버킷 owner의 명시적인 제어 작업입니다. [SRF 13 구조 검사](SLIME-FORMAT.md)는 기본 활성화입니다. 요청 재시도에 대한 완전한 멱등성은 제공하지 않으므로 현재 버전을 확인하고 조건부 저장을 사용합니다.

0.1 예시의 S3 데이터 호출은 유지되지만 0.2 기본 설정에서는 앞선 임대와 token 헤더가 필요합니다. Java SDK의 헤더 전달 방법과 플러그인 코드는 이 저장소에 추가하지 않았습니다.

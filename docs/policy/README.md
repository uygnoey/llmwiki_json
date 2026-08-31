# 정책·동의 문서

공유 위키 서비스의 이용약관·개인정보 처리방침·공유 및 탈퇴 동의서다.
한국어본이 원본이고 나머지는 번역본이다. 내용이 어긋나면 한국어본을 따른다.

| 언어 | 이용약관 | 개인정보 | 공유·재사용·탈퇴 동의 |
|---|---|---|---|
| 한국어 | [ko/terms.md](ko/terms.md) | [ko/privacy.md](ko/privacy.md) | [ko/sharing-and-deletion.md](ko/sharing-and-deletion.md) |
| English | [en/terms.md](en/terms.md) | [en/privacy.md](en/privacy.md) | [en/sharing-and-deletion.md](en/sharing-and-deletion.md) |
| Español | [es/terms.md](es/terms.md) | [es/privacy.md](es/privacy.md) | [es/sharing-and-deletion.md](es/sharing-and-deletion.md) |
| 日本語 | [ja/terms.md](ja/terms.md) | [ja/privacy.md](ja/privacy.md) | [ja/sharing-and-deletion.md](ja/sharing-and-deletion.md) |

## 상태

**전부 초안(v1.0-draft)이다.** 서비스를 실제로 개시하기 전에 법률 검토를 받아야 한다.
특히 다음 두 가지는 관할에 따라 판단이 갈릴 수 있다.

1. **탈퇴 후에도 남는 파생 문장** — GDPR 삭제권(제17조)과 어디까지 양립하는지.
   원문 복제가 아닌 제3자의 새로운 저작물이라는 점이 핵심 논거다.
2. **삭제 표식(tombstone) 무기한 보관** — 문서 식별자와 시각만 담고 개인정보를
   포함하지 않는다는 전제가 실제 구현에서 지켜져야 한다.

## 채워야 할 자리표시자

| 자리표시자 | 내용 |
|---|---|
| `{{EFFECTIVE_DATE}}` | 시행일 |
| `{{OPERATOR}}` | 운영 주체명·주소 |
| `{{CONTACT}}` | 문의 창구 (이메일 등) |
| `{{GOVERNING_LAW}}` | 준거법 |
| `{{JURISDICTION}}` | 관할 법원 |
| `{{STORAGE_PROVIDER}}` | 객체 스토리지 제공자 (AWS S3 / MinIO 자체 운영 등) |
| `{{REGION}}` | 저장 리전 |
| `{{SUBPROCESSORS}}` | 처리 위탁 업체 목록 |
| `{{LOG_RETENTION_DAYS}}` | 접근 로그 보관 일수 |
| `{{RESPONSE_DAYS}}` | 권리 행사 요청 응답 기한 |

## 구현과의 대응

정책 문안은 아래 구현 지점과 짝을 이룬다. 한쪽만 바꾸면 안 된다.

| 정책 조항 | 구현 지점 |
|---|---|
| raw 원본 미업로드 | 업로드 경로 allowlist (`wiki/`, `index/` 만 통과) |
| 원문 스냅샷 제거 | 공유 페이로드에서 `source_snapshot.text` 스트립 |
| 비공개 그룹 격리 | 빌드 단계 링크 방향 검사 (public → private 참조 거부) |
| 문서별 공유 설정 | page `sharing.{links,citation,derivation,backlink}` |
| 그룹보다 느슨할 수 없음 | 빌드 단계 설정 상한 검사 |
| 초대 링크 | 토큰 해시·만료·사용 횟수 (`groups/{id}.json`) |
| 탈퇴 시 부분 삭제 | block 단위 `author` 필드 |
| 삭제 표식 | tombstone object + lint 의 "삭제된 소스" 판정 |

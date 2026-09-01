# llmwiki 클라우드·다중 그룹 공유 상세 설계

- 상태: 설계 확정 (구현 전)
- 교차 검증: Claude Opus 5 ↔ Codex(gpt-5.6-sol) **9라운드**, 지적 44건 반영, 최종 판정 **이슈 없음**
- 결정 원본: [cloud-decisions.md](cloud-decisions.md) (D1~D28)
- 정책 문서: [policy/](policy/README.md) (ko·en·es·ja)

## 0. 요구사항 → 결정 추적

| # | 요구사항 | 이를 덮는 결정 |
|---|---|---|
| 1 | 객체 스토리지 저장 | D3 D4 D5 D9 D10 D26 D28 |
| 2 | 내 위키 공유 + 남의 위키 read-only | D6 D7 D15 D22 |
| 3 | 탈퇴 시 삭제/유지 선택 | D1 D12 D13 D13a D14 D23 D28 |
| 4 | 여러 그룹 조합 | D2 D7 D11 D24 D25 |
| 5 | 비공개 그룹 외부 비누출 | D7 D8 D15 D18 D19 D25 D27 |
| 6 | raw 원본파일 업로드 금지 | D4 D5 D19 D26 |

요구사항 5의 확정된 의미: **private → public 참조는 private artifact 내부에만 존재하고,
public 관측면(그래프·색인·통계·검색·로그·캐시·리퍼러)에는 어떠한 영향도 주지 않는다.**

## 1. 구성 요소

```
┌ 클라이언트 (기존 CLI + viewer) ────────────────────────────┐
│ wiki/**/*.json 로컬 정본        remote DTO projection      │
│ workspace compiler (재-projection)   background sync       │
└──────────────┬─────────────────────────────────────────────┘
               │ ① 인증 API (권한 판정·CAS·capability 발급)
┌──────────────▼─────────────────────────────────────────────┐
│ Postgres  tenant · member · grant · subscription           │
│           manifest row (논리적 CAS 권위)                    │
│           contribution event store / deletion ledger        │
└──────────────┬─────────────────────────────────────────────┘
               │ ② 인증 proxy (private) / presigned GET (public)
┌──────────────▼─────────────────────────────────────────────┐
│ Object Storage (S3 · MinIO)  immutable generation + manifest│
└────────────────────────────────────────────────────────────┘
```

권한은 **스토리지 ACL 이 아니라 Postgres 에서** 판정한다. 스토리지는 내용만 담는다.

## 2. 저장 레이아웃

```
s3://llmwiki/t/{owner}/gen/{generation}/pages/{owner}/{slug}.json   published (remote DTO)
s3://llmwiki/t/{owner}/gen/{generation}/index/*.json                파생물
s3://llmwiki/t/{owner}/gen/{generation}/manifest.json               generation 내용물 목록
s3://llmwiki/t/{owner}/current.json                                 manifest pointer (비권위 cache)
s3://llmwiki/t/{owner}/tombstones/{page-uuid}.json                  삭제 표식
s3://llmwiki/e/{entitlement-epoch}/{workspace}/...                  entitlement 별 private artifact
```

**용어를 분리한다(I16).** `wiki/**/*.json` 는 **canonical authority**(정본)이고, 위 S3
객체는 projection 을 거친 **published representation**(발행본)이다. 둘은 내용도 rev 계산
대상도 다르므로 같은 의미의 "정본" 으로 부르지 않는다.

- `raw/` 는 어떤 경로로도 올라가지 않는다. 그러나 경로 차단만으로는 부족하다 — §6 참조.
- reader 는 **API 가 돌려주는 manifest row 를 권위로** 읽는다. `current.json` 은 그 row 로부터
  재생성 가능한 **cache** 이며 권위가 아니다(I15). 두 값이 갈리면 DB row 가 이긴다.
- private artifact 는 entitlement 별 keyspace 에 두고 public CDN·cache 와 절대 공유하지 않는다.

## 3. 식별자와 주소 (D1 · D2 · D3)

**영속 ID 와 주소를 분리한다.** 이것이 이 설계의 중심축이다.

| 층 | 값 | 성질 |
|---|---|---|
| 영속 ID | `page:<uuid>` / `block:<uuid>` | 불변. 소유권이 이전돼도 그대로 |
| 주소(alias) | `owner/slug` | 가변. 이전·개명 가능 |
| 저장 위치 | `pages/{owner}/{slug}.json` | 가변. alias 를 따라감 |

- 소유권 이전(D23)이 ID 를 깨뜨리지 않는다. `owner` 를 ID 에 박으면 이전 시 모든 링크·
  source ref·tombstone·캐시가 끊긴다. 프로젝트 규칙 "모든 page와 block은 영속 ID를
  가진다" 와도 이 분리가 맞는다.
- 본문 링크는 `[[owner/slug]]`. **콜론은 relation prefix 문법 전용**(`page:` `source:`
  `raw:` `user:`)이며 owner 구분자로 쓰지 않는다. 콜론을 양쪽에 쓰면 `SOURCE_REF` 검증과
  ingest 의 `source:` 보정 때문에 같은 표기가 경로마다 다르게 해석된다.
- canonical relation 에는 alias 가 아니라 `page:<uuid>` 를 저장한다. 사람이 입력한
  `owner/slug` 는 publish 전에 UUID 로 resolve 한다.
- shard 는 `pages/{owner}/{slug}.json` 중첩 경로. 현행 `safe_name()` 은 손실 변환이라
  `page:a/b-c` 와 `page:a-b/c` 가 같은 파일명이 되고 build 는 충돌을 검사하지 않은 채
  덮어쓴다. owner·slug 에 canonical alphabet 을 강제하고(slug 내 슬래시 금지, 유니코드
  정규화), same-owner alias 유일성을 검사하며, 남는 충돌은 hash suffix 로 막는다.

## 4. 스키마 1.1 (D4)

`tools/schema/page.schema.json` 은 `additionalProperties: false` 다. 필드를 하나라도 늘리면
스키마를 함께 고치지 않는 한 `validate` 가 전부 깨진다.

```jsonc
{
  "schema_version": "1.1",
  "id": "page:018f...uuid",          // owner-independent, 불변
  "owner": "acme",                    // 현재 소유 그룹 (가변)
  "slug": "release-process",
  "title": "...",
  "type": "source|entity|concept|synthesis|project|home|index|log",
  "created": "2026-08-31",
  "updated": "2026-08-31",
  "rev": "sha256:...",                // = sha256(canonical(page - rev))
  "authors": [{"id":"u_1","at":"2026-08-31","action":"created"}],
  "visibility": "public|group|private",
  "audience_group_ids": ["g_acme"],   // 한 문서를 특정 그룹들에만 공유할 때
  "sharing": {"links":true,"citation":true,"derivation":false,"backlink":true},
  "tags": [], "projects": [], "sources": [],
  "blocks": {}, "block_order": [], "links": [], "history": []
}
```

block 필수 필드에 `author` 추가. 단, **author 만으로 탈퇴 삭제를 판정할 수 없다** — §10.

**base schema + profile validator 2종:**

| profile | `source_snapshot` |
|---|---|
| base (공통) | `required = [format, sha256]` |
| local canonical | snapshot 이 있으면 `text` **필수** (원문 대조 능력 보존) |
| remote/shared | `text` **금지** |

`render_markdown(exact=True)` 는 text 부재 시 `KeyError` 가 아니라 명시적 `WikiError` 를 낸다.

마이그레이션(D20): 1.0/1.1 혼재 rolling 허용, 구버전 client 는 read-only, downgrade 금지,
실패 복구 절차를 함께 정의한다.

## 5. rev 와 조건부 쓰기 (D9)

- `rev = sha256(canonical(page **without** rev))`. rev 를 포함해 해시하면 자기참조로
  안정된 값이 없다.
- **논리적 CAS 권위는 서버 DB 의 manifest row 하나로 확정.** transaction 안에서
  `expected_manifest_rev` 와 entitlement 를 함께 검사하고 새 immutable generation ID 를
  발행한다. **page 의 `rev` 는 CAS 값이 아니다**(R6-08).
- object store 의 ETag·version ID 는 업로드 무결성·재시도·물리 객체 식별용 metadata 로만
  DB 에 기록한다. page body hash 를 그대로 `If-Match` 에 넣는다고 가정하지 않는다
  (multipart·SSE·provider 차이).

**`rev` 와 `payload_sha256` 을 분리한다(I03).** remote DTO 는 projection 과 pseudonymization
을 거쳐 local canonical 과 바이트가 다르므로, 같은 `rev` 를 실으면 수신 측에서
`sha256(canonical(page - rev))` 검증이 통과할 수 없다.

| 값 | 계산 대상 | 쓰임 |
|---|---|---|
| `rev` (= `page_rev`) | local canonical page (rev 제외) | draft stage 시 stale-page 검사와 provenance. **발행 CAS 값이 아니다**(R6-08) |
| `manifest_rev` | owner 의 manifest row | **발행 CAS 의 expected 값.** generation commit 하나에만 쓴다 |
| `payload_sha256` | remote DTO 자신 (payload_sha256 제외) | 수신 측 무결성 검증 |

수신 측은 `payload_sha256` 으로 검증하고, `rev` 는 참조·추적용으로만 읽는다.

## 6. 원격 payload projection (D5 · D19 · D26)

**경로 allowlist 는 필요조건일 뿐이다.** 원본 파일을 막아도 원문은 다른 필드로 새어 나간다.

| 유출 경로 | 실측/근거 | 처리 |
|---|---|---|
| `source_snapshot.text` | 개인 위키 페이지 JSON 7.56MB 중 731KB(10%)가 원문 전문 | remote DTO 에서 **제거** |
| `raw_ref` | 파일명만으로 고객사·과제명이 드러남 | 해시 치환 또는 제거 |
| `history.note` | ingest 가 raw 경로를 여기에 남김 | raw 경로 **금지** |
| 로컬 절대경로 | `$HOME` 노출 | 금지 |
| `search.json` block 전문 | 전 블록 원문 포함 | public shared index 에 **미포함**, entitlement 별 private index 에만 |
| ingest log 의 source path | append log 에 raw 경로 | remote log DTO 에서 금지 |
| `block.source_text` | 위키 본문 그 자체 | 제거 불가 — 동의 문서에 명시 |

따라서 업로드는 **필드 allowlist projection(object type 별 remote DTO)** 으로 새 객체를
구성하고 업로드 직전 재검증한다. DTO 는 page 뿐 아니라 index·append log·tombstone·manifest
각각에 둔다.

### 6.1 relation 의 정본 표현부터 확정한다 (I02 · D2)

현행 스키마 top-level 에는 `supersedes` 와 `related` 가 **없다**. `additionalProperties: false`
이고, markdown ingest 도 이 값들을 top-level 에 저장하지 않고 `links[]` 로만 변환한다.
`implied_links()` 가 top-level 에서 재구성하는 것은 `sources` 뿐이다.

**파생식 (R6-09 · R7-10):**

```
sources = 기존 sources 중 non-page ref (user:, raw:)  ∪  links[kind=source].targets
```

순수 재생성이 아니라 **합집합**이다. 그렇지 않으면 `user:2026-08-31` 같은 non-page
provenance 가 build 때마다 사라진다. validator 는 `sources` 의 page ref 집합과
`links[kind=source]` 의 target 집합이 다르면 오류로 잡는다.

→ **relation 의 정본 표현은 `links[]` 하나다.** frontmatter 의 `sources`·`supersedes`·
`related` 는 *입력 문법*이며 정본 필드가 아니다. `sources` 만 top-level 에 남는 것은 근거
추적용이고, 그 값도 publish 전에 `page:<uuid>` 로 resolve 한다(D2). DTO 표에서
`supersedes`/`related` 를 top-level 필드로 다루지 않는다.

### 6.2 page remote DTO 필드 allowlist

`blocks` 는 배열이 아니라 block ID 를 key 로 한 object 이므로 `blocks.*.<field>` 로 적는다.
현행 스키마의 필수 필드를 하나라도 빠뜨리면 발행본이 자기 스키마와 viewer 계약을 깬다(I01).

| 필드 | local canonical | remote/shared | 비고 |
|---|---|---|---|
| `schema_version` `id` `slug` `title` `type` | 유지 | 유지 | |
| `owner` `created` `updated` | 유지 | 유지 | |
| `rev` | 유지 | 유지 | canonical 세대 참조값 |
| `payload_sha256` | — | **추가** | 발행본 자신의 해시 (I03) |
| `authors[].id` | 유지 | **pseudonymous 로 치환** | scope 는 workspace, epoch 마다 rotation |
| `authors[].at` `.action` | 유지 | 유지 | |
| `visibility` `audience_group_ids` `sharing` | 유지 | 유지 | 수신 측 정책 판정에 필요 |
| `tags` `projects` | 유지 | 유지 | |
| `sources[]` | 유지 | 유지 (UUID resolve 후) | `raw:` ref 는 제거 |
| `blocks.*.id` `.kind` `.fingerprint` | 유지 | 유지 | **스키마 필수** |
| `blocks.*.data` | 유지 | 유지 | 문자열 전수 검사 대상 |
| `blocks.*.refs` | 유지 | 유지 (UUID resolve 후) | **스키마 필수** |
| `blocks.*.source_text` | 유지 | 유지 | **스키마 필수**. 위키 본문 자체 — 제거 불가 |
| `blocks.*.author` | 유지 | pseudonymous | 1.1 신규 |
| `blocks.*.resolution` | 유지 | 유지 | conflict block 전용 (선택) |
| `block_order` | 유지 | 유지 | |
| `links[].target` `.kind` | 유지 | 유지 (UUID resolve 후) | **스키마 필수** |
| `links[].label` `.anchor` `.block_id` | 유지 | 유지 | label 은 문자열 검사 대상 |
| `history[].at` `.action` | 유지 | 유지 | **스키마 필수** |
| `history[].actor` | 유지 | pseudonymous | **스키마 필수** |
| `history[].note` | 유지 | **raw 경로·절대경로 포함 시 제거** | ingest 가 경로를 남김 |
| `summary` | 유지 | 유지 | 문자열 전수 검사 대상 |
| `raw_ref` | 유지 | **제거** (또는 sha256 치환) | 파일명이 곧 내부 정보 |
| `source_snapshot.text` | **필수** | **금지** | 원문 전문 |
| `source_snapshot.format` `.sha256` | 유지 | 유지 | 대조용 |

remote DTO 는 위 표대로 **새 객체를 구성**하는 방식이며 원본에서 필드를 지우는 방식이
아니다. 새 필드가 스키마에 추가돼도 allowlist 에 명시하지 않는 한 자동으로 나가지 않는다.

### 6.3 유지되는 문자열의 전수 재귀 검사 (I04 · D5)

필드 allowlist 만으로는 부족하다. `title`, `summary`, `blocks.*.data`, `blocks.*.source_text`,
`links[].label` 처럼 **유지되는 임의 문자열** 안에 raw 경로나 자격 증명이 들어 있을 수 있다.
현행 ingest 의 secret 검사는 제한된 정규식 하나이고, JSON 을 직접 작성한 정본과 이미
존재하는 페이지의 발행에는 적용되지 않는다.

→ 발행 직전 remote validator 가 **DTO 의 모든 문자열 값을 재귀 순회**해 검사한다.

검사는 **field-aware** 다. 해시·ID 를 담는 필드는 애초에 고엔트로피 검사 대상이 아니다 —
그렇지 않으면 `source_snapshot.sha256`, `payload_sha256`, page·block UUID, manifest 해시가
전부 토큰으로 오판된다(R6-05).

| 판정 | 기준 | 검사 대상 | 조치 |
|---|---|---|---|
| 절대경로 | `/Users/`, `/home/`, `C:\\` 로 시작하거나 저장소 루트 접두사 | 자유 문자열 필드 전부 | 차단 |
| raw 경로 | `raw/` 로 시작하거나 `raw_ref` 값과 일치 | 자유 문자열 필드 전부 | **차단. 예외 불가** |
| 자격 증명 | 기존 secret 정규식 + 고엔트로피 휴리스틱 | 자유 문자열 필드만 (해시·ID 필드 제외) | 차단 |

**예외는 작성자 입력으로 만들 수 없다(R6-05).** 본문에 주석을 넣어 검사를 끄는 방식은
D5·D18 의 server-side rejection 을 작성자가 해제하는 통로가 된다. 예외는 별도 권한을 가진
승인자가 남긴 **승인 record**(대상 page·필드·문자열 해시·승인자·사유·만료)로만 성립하며,
**raw 경로에는 어떤 예외도 허용하지 않는다.** 차단 사유는 발행 응답에 실린다.

### 6.4 object type 별 DTO (I05 · D26)

page 외에도 발행되는 object 가 넷이다. 각각 allowlist schema 를 둔다.

| object | 허용 필드 | 비고 |
|---|---|---|
| `catalog.json` | `id` `slug` `title` `type` `updated` `projects` `tags` `summary`, `sources` (**`raw:` ref 제거 후**) | page DTO 에서 파생시킨다 — canonical page 에서 직접 복사하면 `raw:` ref 가 재노출된다 (R7-05) |
| `map.json` | `schema_version`, `pages.*.{data_url,pointer,sha256}`, `blocks.*.{data_url,pointer,kind,page_id}` | **`pages.*.source` 제외** (로컬 저장소 경로) |
| `graph.json` | `schema_version` `groups`, `nodes[].{id,slug,label,type,created,updated,projects,tags,group,summary,incoming,outgoing,degree,orphan,unresolved_conflicts,data_url,x,y}`, `edges[].{id,kind,source,target}` | 전 필드 허용 |
| `stats.json` | `pages` `blocks` `edges` `unresolved_conflicts` | 전 필드 허용 |
| `routes.json` | group key → page id 배열 | 전 필드 허용 |
| `revision.json` | `schema_version` `revision` | 전 필드 허용 |
| `search.json` (public) | `id` `slug` `title` `type` `summary` | **`text` 제외** |
| `search.json` (private) | `id` `slug` `title` `type` `summary` `text` | entitlement 별 private keyspace 에만 발행. §6.3 재귀 검사를 **동일하게** 적용한다 (R7-06) |
| append log | `at` `action` `page_id` `mode` | **`source`·`dest` 제외** (ingest 가 기록하는 경로) |
| tombstone | `id`(pseudonymous stable) `deleted_at` `schema_version` | title·author·경로 없음 (D14) |
| manifest | `generation` `created_at` `entries[].{key,sha256,bytes}` | 로컬 경로 없음 |

**denylist 가 아니라 allowlist 다.** 위 표에 없는 필드는 새로 생겨도 반출되지 않는다.
`data_url` 은 발행 경로로 재작성한다.

## 7. 권한·가시성·격리

### 7.1 정본 위치 (D6)

- 접근제어 그룹·membership·invite·role 은 **Postgres 가 정본**이다.
- 현행 `tools/config/groups.json` 은 **그래프 시각화 설정 그대로 유지**한다. 이 파일은
  권한 DB 가 아니라 project/type/tag 색상 설정이고, build 와 viewer 가 그 구조를 전제로
  읽는다. 여기에 membership 을 넣으면 기존 계약이 깨진다. 이름과 역할을 분리한다.
- object storage 에는 권한 판정에 쓰이지 않는 **signed snapshot** 만 둔다(D27):
  issuer, audience/workspace, entitlement epoch, issued/expiry, key ID 를 포함하고
  manifest 적용 전에 검증하며 실패 시 fail-closed.

### 7.2 링크 판정 (D7 · D8 · D25)

**deny-by-default.** 다음 규칙이 `(source, target, relation kind, 방향)` 전 칸을 생성한다.

1. 대상이 출처보다 좁으면 **항상 거부**. (public 문서가 private 문서를 참조하면 존재와
   제목이 샌다)
2. 같거나 넓으면: 같은 그룹 내 허용. **다른 그룹이면 대상이 `public` 일 때 출처 그룹의
   `linking` 에 따르고, 대상이 `group` 이면 `linking` 이 `open` 일 때만 허용한다.**
3. 대상 문서의 `sharing.links` / `sharing.citation` 이 false 면 거부.
4. 개인 `private` 페이지는 어떤 외부 참조의 대상도 될 수 없다.
5. backlink 를 대상 쪽 artifact 에 기록하는 것은 `vis(source) ≥ vis(target)` 이고
   `sharing.backlink` 가 true 일 때만.

**전수 행렬 (D25).** `vis(page)` 는 `public > group > private` 순으로 넓다. `A` 는 출처
문서, `B` 는 대상 문서이며 값은 `A → B` 참조(edge 생성) 허용 여부다.

| # | A 가시성 | B 가시성 | 그룹 관계 | 판정 |
|---|---|---|---|---|
| 1 | public | public | 같은 그룹 | 허용 |
| 2 | public | public | 다른 그룹 | 허용 |
| 3 | public | group | — | **거부** (좁은 쪽 참조) |
| 4 | public | private | — | **거부** |
| 5 | group | public | 같은 그룹 | 허용 |
| 6 | group | public | 다른 그룹 | A 그룹 `linking` 이 `isolated` 면 거부, 아니면 허용 |
| 7 | group | group | 같은 그룹 | 허용 (audience 규칙 적용) |
| 8 | group | group | 다른 그룹 | A 그룹 `linking` 이 `open` 일 때만 허용, 그 외 **거부** |
| 9 | group | private | — | **거부** |
| 10 | private | public | — | `linking` 이 `isolated` 면 거부, 아니면 허용 |
| 11 | private | group | A 의 principal 이 B 를 읽을 entitlement 보유 | 허용 |
| 12 | private | group | entitlement 없음 | **거부** |
| 13 | private | private | A 의 principal 이 B 를 읽을 entitlement 보유 | 허용 |
| 14 | private | private | entitlement 없음 | **거부** |

네 relation kind(`wiki`·`related`·`source`·`supersedes`)에 이 표를 동일하게 적용한다.
kind 별 차이는 아래 gate 에서만 난다.

**audience 부분 교집합 (7행 보강).** `audience_group_ids` 가 여러 개일 때는
`audience(B) ⊇ audience(A)` 일 때만 허용한다. **대상의 독자 범위가 출처를 포함해야** 한다.
부분 교집합은 거부다 — A 만 볼 수 있는 사람에게 B 의 존재가 드러나기 때문이다.
`audience` 가 비어 있으면 소속 그룹 전체로 본다.

`linking` 값 (R6-07):

| 값 | 밖의 `public` 참조 | 밖의 `group` 참조 (8행) | backlink |
|---|---|---|---|
| `isolated` | 금지 | 금지 | — |
| `outbound_only` | 허용 | **금지** | 억제 |
| `open` | 허용 | **허용** (양쪽 audience 규칙 통과 시) | 노출 |

`open` 이 `outbound_only` 보다 실제로 더 허용하는 지점은 8행 하나다. 이 차이가 없으면
`open` 은 존재 이유가 없다.

**gate 3종을 분리한다 (I07).** 위 표를 통과한 뒤 다음 셋을 각각 검사한다.

| gate | 검사 대상 | 소유자 | 실패 시 |
|---|---|---|---|
| source gate | A 그룹의 `linking` | 출처 그룹 | edge 생성 거부 |
| target gate | **B** 의 `sharing.links`(`wiki`·`related`) / `sharing.citation`(`source`·`supersedes`) | 대상 문서 | edge 생성 거부 |
| backlink gate | **B** 의 `sharing.backlink` **및** `vis(A) ≥ vis(B)` | 대상 문서 | edge 는 만들되 B 쪽 artifact 에 역링크를 **기록하지 않음** |

relation kind 가 만들어지는 곳: `wiki` = 본문 `[[owner/slug]]`, `related`·`source`·
`supersedes` = frontmatter 입력 문법(정본 표현은 §6.1 대로 `links[]`).

D7 의 "문서별 sharing 은 그룹 정책보다 조일 수만 있다" 는 **target gate 에 적용**된다.
출처 문서가 자기 outbound 를 더 조이려면 `sharing` 이 아니라 그 문서를 더 좁은
`visibility` 로 두는 것으로 표현한다. **`sharing` 네 필드는 모두 "그 문서가 대상이 될 때"의
정책이다.**

**비공개 그룹 기본값은 `outbound_only`** 이며, 다음 비간섭 조건 전부를 설계 계약으로
강제하는 것이 조건이다:

- public build 입력에서 private canonical·edge·manifest 전면 배제
- graph·index·shard·stats·degree·orphan·search·unresolved label·backlink·revision 값과
  갱신 시각·cache key 까지 private 입력 무영향
- private artifact 는 entitlement 별 private keyspace, public CDN·cache 와 미공유
- 교차 링크 resolution 은 로컬 authorized corpus 에서 수행, 서버 요청에는 public target
  ID 만 전송 (private source ID·title·workspace ID 미전송)
- public owner 대상 알림·analytics·access log 에 private source 미기록
- private viewer 에서 remote image·script·embed 자동 로드 금지
- `Referrer-Policy: no-referrer` 등으로 private node ID 가 든 URL 이 header·log 로
  나가지 않게 통제 (현행 viewer 는 선택 ID 를 URL query 에 넣는다)

`isolated` 도 그룹 설정으로 선택 가능하다.

### 7.3 role × action 행렬 (D22)

**자기 owner namespace**

| action | owner | editor | reader |
|---|---|---|---|
| read | ○ | ○ | ○ |
| create | ○ | ○ | ✗ |
| update | ○ | ○ | ✗ |
| delete | ○ | ○ | ✗ |
| publish | ○ | ○ | ✗ |
| share (권한 부여·초대 발행) | ○ | ✗ | ✗ |
| transfer (소유권 이전) | ○ | ✗ | ✗ |
| approve (발행 예외 승인) | ✗ | ✗ | ✗ |

`approve` 는 어떤 그룹 role 로도 얻을 수 없는 **별도 `approver` 권한**이다(R7-07). 자기
문서의 예외를 자기가 승인하면 §6.3 의 보안 경계가 무너지므로, 승인자는 대상 문서의
작성자·소유자가 아니어야 한다. 발급·회수는 감사 로그에 남는다.

**구독한 남의 owner namespace**

| action | 모든 role |
|---|---|
| read | ○ — **단 §7.2 page policy 와 entitlement 를 통과한 object 에 한함** |
| create · update · delete · publish · share · transfer | **✗ (capability 자체를 발급하지 않는다)** |

**결합 규칙 (I08).** 이 표는 **상한**이지 허용 목록이 아니다. 실효 권한은
`role×action ∧ §7.2 page policy ∧ entitlement epoch` 의 교집합이다. 자기 namespace 의
`reader` 도 같은 규칙을 받으므로 다른 사람의 개인 `private` 문서는 읽지 못한다. 어느 한
층이라도 거부하면 거부다(deny-overrides).

이것이 요구사항 2의 read-only 불변식이다. 구독 관계에서는 write capability 를 **발급할 수
있는 코드 경로가 존재하지 않아야** 하며, 권한 검사 분기로 막는 것으로는 부족하다.
공동 편집이 필요하면 "남의 위키 read-only" 와 섞지 않고 별도 collaboration workspace 로
분리한다 — 그 workspace 는 참여자 전원이 editor 인 별개 owner 다.

### 7.4 전달 경로 (D15 · D16)

presigned URL 은 만료 전까지 **bearer capability** 다. "짧은 TTL" 과 "즉시 차단" 은 같은
메커니즘으로 동시에 보장되지 않는다.

| 대상 | 전달 | 보장 |
|---|---|---|
| private object | 매 요청 entitlement 를 검사하는 **인증 proxy** | 즉시 차단 |
| public object | 직접 presigned GET (단일 object·단일 method) | 최대 TTL 이내 무효화 |

API 계약에 두 보장 수준을 명시 구분한다. 로컬 캐시는 entitlement epoch 기반 강제 GC,
캐시 암호화와 키 폐기를 적용하고, **offline 최대 잔존 시간은 기본 24시간**으로 확정한다.
강한 격리가 필요한 private group 은 offline cache 금지 또는 짧은 lease 를 선택할 수 있다.

### 7.5 강제 지점 (D18 · D21)

- 보안 경계는 **lint 경고가 아니라 server-side publish rejection** 이다. 현행 lint 와
  분리된 policy validator 를 둔다.
- `sharing.derivation` 은 기술적으로 강제할 수 없다(복사된 문장을 자동 판별 불가).
  접근 제어가 아니라 이용정책·동의 영역임을 문서와 코드 주석에 명시하고 강제 가능한
  것과 분리한다.

## 8. 발행 프로토콜 (D10 · D28)

한 번의 논리적 commit 이 page·log·tombstone·index·manifest 여러 object 에 걸친다. 중간에
실패하면 서로 다른 세대가 노출된다.

1. 새 `generation` prefix 에 모든 object 를 올린다 (기존 세대는 그대로).
2. `manifest.json` 을 그 prefix 에 올린다.
3. **DB transaction 에서만** CAS 로 manifest row 를 교체한다. 이 transaction 이 커밋되는
   순간이 발행 시점이다.
4. `current.json` 갱신은 같은 transaction 의 **outbox** 로 큐에 넣고 비동기로 반영한다.
5. reader 는 API 가 돌려주는 manifest row 를 권위로 읽는다.

**Postgres 와 S3 사이에는 원자적 transaction 이 없다(I15).** 3단계와 4단계를 한 트랜잭션인
것처럼 쓰면 중간 crash 때 DB 권위와 reader 가 보는 pointer 가 갈린다. 그래서 권위 pointer 는
DB row 하나뿐이고, `current.json` 은 outbox 가 뒤늦게 맞추는 cache 다. 불일치 복구는 outbox
재시도와 주기적 reconciliation 이 담당하며, 어느 쪽이 옳은지는 항상 DB row 로 판정한다.
`current.json` 만 읽는 경로(정적 호스팅 fallback)를 두려면 그 경로가 **최신이 아닐 수 있음**을
API 계약에 명시한다.

GC(D28): manifest 에서 끊긴 generation 의 삭제 기준(활성 capability 만료 대기, subscriber
acknowledgement 상한), 법정 backup retention 과 crypto-erasure 적용 방식을 확정한다.

## 9. 여러 그룹 조합 (D11 · D24)

**index 병합으로는 안 된다.** 현행 edge 는 build 시점의 corpus 전체 lookup 에서 target 을
찾을 때만 생성되므로, owner 별 단독 build 에서는 교차 owner edge 가 그냥 버려진다.
incoming/outgoing/degree/orphan/routes 도 전부 틀린 값이 된다.

- owner 별 build 는 **unresolved cross-edge 를 버리지 않고 기록**해 보존한다.
- **workspace compiler** 가 사용자의 authorized owner set 전체를 대상으로 link
  resolution, visibility filtering, backlink suppression, degree·stats·routes 재계산을
  수행한다.
- 정본은 `workspace manifest = personal owner + 선택한 group/owner 구독 + pinned revision`.
- qualified `owner/slug` 는 항상 유일하게 resolve. bare slug 는 self-owner 우선, 그래도
  모호하면 **결정적 ambiguity error**(임의 선택 금지). 현행 `page_lookup` 의 title 기반
  fallback 은 교차 owner 에서 비활성화한다.

## 10. 탈퇴와 삭제 (D12 · D13 · D13a · D14 · D23)

### 왜 author 필드만으로는 안 되나

현행 block ID 는 내용 fingerprint 기반이라 **내용을 고치면 ID 가 바뀐다**. ingest update 는
page history 를 합칠 뿐 block lineage 를 보존하지 않는다. 따라서 "내가 쓴 블록" 을 현재
상태의 `author` 필드만으로 판정할 수 없다.

→ 삭제 판정 근거는 **append-only contribution event store + block lineage** 다. 공동 저작
정책과 분쟁 처리 규칙을 함께 정의하고, 삭제는 preview 후 idempotent workflow 로 실행한다.

### log 를 둘로 나눈다 (D13a)

append-only 를 유지하면서 그 사용자의 기록을 지우는 것은 그대로는 양립하지 않는다.

| 저장소 | 내용 | 탈퇴 시 |
|---|---|---|
| contribution event store | 암호화된 actor mapping + content delta | content 삭제 또는 키 폐기 |
| compliance deletion ledger | pseudonymous request ID·단계·시각·결과만 (content·title·path·직접 사용자 ID 없음) | 보존 |

### 삭제 범위 (D13)

canonical page, owner index, viewer shard, search body, graph metadata, qmd index,
구독자 로컬 cache, object versions·backups, 발급된 capability, contribution event store.
상태 머신·재시도·감사 가능한 완료 증명·backup 보존기한을 둔다.

### 두 분기

**A. 전체 삭제** — 위 범위를 삭제하고 자리에 tombstone 을 남긴다. tombstone 의 허용 필드는
`schema_version` · `id`(pseudonymous stable) · `deleted_at` 셋뿐이며 **title·author·경로를
담지 않는다**(R6-12). 공개 범위·접근권한·보존 근거를 명시한다. 개인정보 해당 여부는 법률
판단 영역으로 남긴다.

**B. 소유권 이전 후 유지** (D23) — user-facing state machine 의 명시적 분기. 결정할 것:
새 `auth_owner`, storage locator·alias 이전(**ID 는 D1 대로 불변**), 수용 그룹의 수락 권한,
`authors` 익명화·pseudonymization, 기존 links 와 manifest 갱신, 이전 실패 시 롤백, 사용자
최종 동의 시점.

### 탈퇴 상태 머신

| 현재 상태 | 사건 | 다음 상태 |
|---|---|---|
| — | `POST /account/close` | `requested` |
| `requested` | preview 산출 완료 | `preview_built` |
| `preview_built` | `confirm` (mode=delete) | `user_confirmed` |
| `preview_built` | `confirm` (mode=transfer) | `user_confirmed` |
| `user_confirmed` | mode=delete | `deleting` |
| `user_confirmed` | mode=transfer, 제안 발송 | `offer_sent` |
| `offer_sent` | 수용 그룹 owner `accept` | `target_accepted` |
| `offer_sent` | 수용 그룹 owner `reject` | **`user_confirmed`** (다른 그룹 재선택 또는 삭제 전환) |
| `offer_sent` | 만료 | **`user_confirmed`** |
| `target_accepted` | — | `awaiting_user_reconfirm` |
| `awaiting_user_reconfirm` | `reconfirm` | `transferring` |
| `awaiting_user_reconfirm` | 재동의 거절 | `cancelled` |
| `transferring` | 성공 | `transferred` |
| `transferring` | 실패 | `awaiting_user_reconfirm` (롤백) |
| `deleting` | online canonical 삭제 완료 | `deleted` |
| `deleting` | 부분 실패 | `delete_retrying` → `deleting` |
| `deleting` | 자동 재시도로 해소 불가 | `manual_review` |
| `deleted` | — | `purge_pending` |
| `purge_pending` | capability·cache·backup 만료 완료 | `purged` |
| `purge_pending` | 부분 실패 | `purge_retrying` → `purge_pending` |
| `purge_pending` | legal hold | `blocked` |
| `purge_pending` | 자동 재시도로 해소 불가 | `manual_review` |
| `manual_review` | `retry` | 원래 phase (`delete_retrying` 또는 `purge_retrying`) |
| `requested`·`preview_built`·`user_confirmed`·`offer_sent`·`awaiting_user_reconfirm` | `cancel` | `cancelled` |

**`target_rejected` 와 `offer_expired` 는 `cancelled` 가 아니라 `user_confirmed` 로
돌아간다(R7-08).** 수용 그룹이 거절했다고 탈퇴 자체가 취소되는 것은 아니다 — 사용자는 다른
그룹을 고르거나 삭제로 전환할 수 있다.

**이전은 2단계다 (I09).** `user_confirmed` 에서 곧바로 옮기지 않는다. 수용 그룹에 제안을
보내고(`offer_sent`), 그 그룹의 owner 가 수락해야(`target_accepted`) 비로소 locator·alias 를
바꾼다. 거절·만료는 `user_confirmed` 로 되돌아가 다른 그룹을 고르거나 삭제로 전환할 수 있다.
사용자 최종 동의는 별도 상태 `awaiting_user_reconfirm` 으로 둔다(R6-03) — 어느 그룹이
받는지 확정된 뒤여야 의미 있는 동의이므로, 최초 `confirm` 과 다른 전이다. 재동의를 거절하면
`cancelled` 로 간다. `transferring` 실패는 롤백해 `awaiting_user_reconfirm` 으로 돌아가며
부분 이전 상태를 남기지 않는다. 이전 중에도 page ID 는 D1 대로 불변이다.

**이전이 실제로 바꾸는 것 (R6-13).** 아래는 확정 사항이며 미결이 아니다.

| 대상 | 처리 |
|---|---|
| `auth_owner` | 수용 그룹 ID 로 교체 |
| `owner` 필드·storage locator·alias | `target_accepted` **이후에만** 변경 |
| page ID · block ID | **불변** (D1) |
| `authors[]` · `blocks.*.author` | 익명 식별자로 치환 (필수) |
| `links[]` · 남의 `sources[]` | UUID 를 가리키므로 **갱신 불필요** |
| workspace manifest · index | 다음 generation 재-projection 에서 자동 반영 |
| 실패 시 | `awaiting_user_reconfirm` 으로 롤백, 부분 상태 없음 |

**`deleted` 와 `purged` 를 나눈다 (I10).** 삭제 범위에는 즉시 지울 수 없는 것이 섞여 있다.

| 상태 | 의미 | 완료된 것 |
|---|---|---|
| `deleted` | **online canonical 삭제 완료** | 정본·발행본·index·shard·search·graph·manifest 에서 제거, 신규 접근 차단 |
| `purge_pending` | 시간이 필요한 대상 대기 | 발급된 capability 의 TTL 만료, 구독자 offline cache(기본 24시간), object version·backup retention |
| `purged` | **전 범위 삭제 완료** | 위 전부 + crypto-erasure 적용 |
| `blocked` | legal hold 로 진행 불가 | 사유·해제 조건을 사용자에게 고지 |
| `manual_review` | 자동 재시도로 해소되지 않음 | 운영자 개입 대기 |

**완료 증명은 `purged` 에서만 발급한다.** `deleted` 시점에 증명을 주면 아직 남아 있는
캐시·백업에 대해 거짓말이 된다. 사용자에게는 두 시점을 각각 통지한다.
**retry 는 단계를 나눈다(R7-04).** `delete_retrying` 은 `deleting` 으로, `purge_retrying` 은
`purge_pending` 으로 복귀한다. 하나로 합치면 purge 실패가 이미 끝난 삭제를 다시 돌린다.
둘 다 실패한 대상만 다시 시도하며 이미 끝난 대상을 되돌리지 않는다(idempotent).

되돌릴 수 없는 것 — 남의 파생 문서, tombstone, 법정 보존 기록, 이미 배포된 캐시 — 는
[공유·재사용 및 탈퇴 동의](policy/ko/sharing-and-deletion.md)에서 가입 시 별도 동의를 받는다.

## 11. API 표면

아래가 **전체 계약**이다(예시 목록이 아니다). 상태 머신을 구동하는 전이 endpoint 를 모두
포함한다.

### 발행

| 엔드포인트 | 용도 | 비고 |
|---|---|---|
| `POST /v1/{owner}/generations` | generation 열기 → draft ID 반환 | |
| `PUT /v1/{owner}/generations/{gid}/pages/{slug}` | draft 에 page **stage** | 발행 아님. body 의 `page_rev` 로 stale 검사만 |
| `DELETE /v1/{owner}/generations/{gid}/pages/{slug}` | draft 에서 page 삭제 표시 | |
| `POST /v1/{owner}/generations/{gid}/commit` | **유일한 발행 CAS** | body 에 `expected_manifest_rev`. 불일치 시 **409** |
| `GET /v1/{owner}/manifest` | 현재 manifest row (권위) | reader 의 델타 판정 기준 |

**page CAS 와 generation CAS 를 하나로 합쳤다 (I13).** page 를 개별 발행하는 endpoint 를
따로 두면 D9 의 "manifest row 단일 CAS 권위" 와 D10 의 "단일 논리적 commit" 이 둘 다 깨진다.
page 쓰기는 draft 에 stage 만 하고, CAS 는 `commit` 하나뿐이다.

### 읽기와 권한

| 엔드포인트 | 용도 | 비고 |
|---|---|---|
| `GET /v1/objects/{key}` | **private object 인증 proxy** | 매 요청 entitlement 검사 → 즉시 차단 |
| `POST /v1/{owner}/capabilities` | public object presigned GET 발급 | 단일 object·단일 method·TTL |
| `GET /v1/entitlements` | 현재 entitlement epoch | 로컬 캐시 GC 트리거 |

### workspace 조합 (D24)

| 엔드포인트 | 용도 |
|---|---|
| `GET /v1/workspaces/{id}` | manifest 조회 (구독 목록 + pinned revision) |
| `POST /v1/workspaces/{id}/subscriptions` | owner·group 구독 추가 |
| `DELETE /v1/workspaces/{id}/subscriptions/{owner}` | 구독 해제 |
| `PATCH /v1/workspaces/{id}/subscriptions/{owner}` | pinned revision 변경 |

이 넷이 없으면 사용자가 요구사항 4의 "여러 그룹 조합"을 실제로 구성할 수 없다(I12).

### 그룹과 초대

| 엔드포인트 | 용도 | 비고 |
|---|---|---|
| `POST /v1/groups/{id}/invites` | 초대 발행 (owner 만) | 토큰 원문은 응답 1회만, 저장은 해시 |
| `DELETE /v1/groups/{id}/invites/{iid}` | 초대 폐기 | |
| `POST /v1/invites/{token}/accept` | 초대 수락 | 만료·사용횟수 검사, transaction |
| `GET /v1/groups/{id}/members` | 구성원·역할 조회 | |
| `PATCH /v1/groups/{id}/members/{uid}` | **역할 변경** | entitlement epoch 증가 |
| `DELETE /v1/groups/{id}/members/{uid}` | **구성원 제외** | entitlement epoch 증가 → 즉시 차단 |
| `DELETE /v1/{owner}/grants/{gid}` | **공유 회수** | entitlement epoch 증가 |
| `GET /v1/approvals` | 발행 예외 승인 record 조회 | `approver` 권한 |
| `POST /v1/approvals` | 승인 record 발급 (대상 page·필드·문자열 해시·사유·만료) | `approver` 권한. 본인 문서에는 발급 불가 |
| `DELETE /v1/approvals/{aid}` | 승인 회수 | 회수 즉시 다음 발행부터 차단 |

member 제거·역할 변경·공유 회수가 없으면 D6·D15 의 "권한 회수 후 즉시 차단" 을 실행할
정상 경로가 없다(R6-11). 이 셋은 모두 entitlement epoch 를 올리고, §13.1 의 sync 가 다음
세대에서 해당 데이터를 제외한다.

### 탈퇴 (§10 상태 머신)

| 엔드포인트 | 전이 |
|---|---|
| `POST /v1/account/close` | → `requested`, preview 생성 시작 |
| `GET /v1/account/close` | 현재 상태·preview 조회 (**부수효과 없는 GET**) |
| `POST /v1/account/close/confirm` | `preview_built` → `user_confirmed` (body 에 `mode: delete\|transfer`, `target_group?`) |
| `POST /v1/account/close/cancel` | **`requested`·`preview_built`·`user_confirmed`·`offer_sent`·`awaiting_user_reconfirm` 에서만** → `cancelled` |
| `POST /v1/account/close/retry` | 실패 phase 에 따라 `delete_retrying` 또는 `purge_retrying` 으로 전이. `manual_review` 에서도 원래 phase 로 돌아간다 |
| `POST /v1/transfers/{tid}/accept` | `offer_sent` → `target_accepted` (수용 그룹 owner) |
| `POST /v1/transfers/{tid}/reject` | `offer_sent` → `target_rejected` (수용 그룹 owner) |
| `POST /v1/account/close/reconfirm` | `awaiting_user_reconfirm` → `transferring` (본인) |
| `DELETE /v1/account/close/reconfirm` | `awaiting_user_reconfirm` → `cancelled` (본인) |

**취소는 되돌릴 수 있는 단계에서만 가능하다(R6-10).** `deleting` 이후에는 이미 지워진 것을
되살릴 수 없으므로 취소를 받지 않는다. retry 도 삭제 단계와 purge 단계를 나눠 각자의
단계로 복귀한다 — 하나로 합치면 purge 실패가 이미 끝난 삭제를 다시 돌린다.

`GET /v1/account/close` 는 상태를 바꾸지 않는다 — preview 생성은 `POST` 가 시작하고 GET 은
결과만 읽는다(I11).

### 충돌 코드

CAS 충돌은 **409 Conflict** 다. 근거는 상태가 DB 에 있어서가 아니라, `expected_manifest_rev`
를 **request body 에 담는 application-level state conflict** 이기 때문이다(I14). 나중에
`If-Match` 헤더 계약으로 바꾸면 그때는 `412 Precondition Failed` 가 맞다.

## 12. 동기화와 훅 (D17)

`llmwiki_context.py` 는 `UserPromptSubmit` 마다 돈다. **prompt 경로에서 network I/O 를 하지
않는다.** 별도 background sync 가 원격 revision 과 entitlement 를 pull 해 local manifest 를
원자적으로 교체하고, hook 은 그 manifest 만 읽는다. 현행 fail-open·저지연 특성을 유지한다.

## 13. 코드 변경 지점

| 대상 | 파일 | 변경 |
|---|---|---|
| 경로 해석 | `scripts/llmwiki.py:124` `Workspace` | storage 백엔드 추상화 |
| 원자 쓰기 | `scripts/llmwiki.py:103` `dump()` | 로컬 유지 + 원격 CAS 경로 분리 |
| shard 이름 | `scripts/llmwiki.py:113` `safe_name()` | `page_data_url(page)` 로 통일, 중첩 경로 |
| data_url 3곳 | `scripts/llmwiki.py:829` `:869` `:900` | 같은 함수로 일원화 |
| ID 검증 | `scripts/llmwiki.py:660` | `id == "page:"+slug` 강제 해제, UUID 체계 |
| block ID 생성 | `scripts/llmwiki.py:296-307` | page ID 종속 제거 + lineage |
| 링크 해석 | `scripts/llmwiki.py:453-471` `page_lookup` | qualified alias 색인, title fallback 제한 |
| 스키마 | `tools/schema/page.schema.json` | 1.1 + profile 분리 |
| validator | `scripts/llmwiki.py:557` `SchemaValidator` | profile 지원 (`if/then` 미지원이므로 Python 측 보강) |
| exact render | `scripts/llmwiki.py:926-934` | text 부재 시 WikiError |
| ingest | `scripts/llmwiki.py:1207` | source path 를 remote log DTO 에서 배제 |
| viewer 타입 | `viewer/src/types.ts:47-52` `WikiPage` | 1.1 의 `owner` `rev` `payload_sha256` `authors` `visibility` `audience_group_ids` `sharing` `blocks.*.author` 추가 |
| viewer fetch | `viewer/src/App.tsx:61-90` | 무인증 static fetch → 인증 proxy / capability 경유. `setPage` 전에 검증된 세대에서만 읽는다 |
| payload 검증 | background sync (신규) | manifest entry 해시와 `payload_sha256` 을 **세대 확정 전에** 검증. 불일치 세대는 `current` 로 승격하지 않는다 (R7-09) |
| viewer 링크 | `viewer/src/App.tsx:104` `navigateSlug` | alias 비교 |
| hook 조회 | `scripts/llmwiki_context.py:270-309` | 로컬 `rglob` → background sync 가 만든 manifest 경유 |
| build projection | `scripts/llmwiki.py:867-914` | index·shard 생성 시 DTO projection 분기 (§6.4) |
| append log | `scripts/llmwiki.py:1280-1283` | remote log DTO 에서 `source`·`dest` 경로 배제 |

### 13.1 두 소비자의 원자적 전환 (I17)

`viewer` 와 `llmwiki_context.py` 는 지금 로컬 파일을 직접 읽는다. 클라우드에서는 background
sync 가 authorized workspace 를 로컬에 materialize 하고 **두 소비자 모두 그 결과만** 읽는다.

```
~/.local/share/llmwiki/workspaces/{id}/
  gen-{n}/            ← 완성된 세대 (index/ pages/)
  current  →  gen-{n} ← symlink. 교체가 곧 전환
```

- sync 는 새 세대를 `gen-{n+1}` 에 완성한 뒤 **symlink 를 원자적으로 교체**한다. 읽는 쪽이
  반쯤 갱신된 상태를 보지 않는다.
- entitlement epoch 가 바뀌면 sync 는 권한을 잃은 owner 의 데이터를 **새 세대에서 제외**하고
  교체한다. 교체 후 이전 세대를 삭제하는 것이 캐시 GC 다(D16).
- hook 은 `current` 만 따라가므로 prompt 경로에서 network I/O 가 없다(D17). 현행 fail-open
  특성도 유지된다 — `current` 가 없으면 조용히 빈 컨텍스트를 낸다.

## 14. 단계

1. **스키마 1.1 + ID 분리** — 데이터가 적을 때 해야 가장 싸다. ingest·validator·block
   ID·resolver·link key·test fixture 전부의 주소 체계 마이그레이션이다.
2. **remote DTO + storage 추상화** — 단일 사용자로 검증. 요구사항 6이 여기서 실제로 지켜진다.
3. **권한 DB + 인증 proxy + capability 발급** — 요구사항 2.
4. **generation·manifest CAS 발행** — 요구사항 1.
5. **workspace compiler + background sync** — 요구사항 4.
6. **격리 policy validator (server-side rejection)** — 요구사항 5.
7. **탈퇴 workflow + tombstone + GC** — 요구사항 3.

## 15. 미결 사항 (open decisions)

아래는 **확정이 아니라 미결**이다. provider·법률에 따라 값이 달라져 지금 정할 수 없는
것들이며, 확정 항목과 섞어 두면 "설계 확정" 이 거짓말이 된다(I18). 각 항목의 결정 주체와
기한을 정해야 한다.

| 항목 | 근거 결정 | 정해야 할 값 | 주체 |
|---|---|---|---|
| key rotation·폐기·replay 방어 절차 | D27 | rotation 주기, 폐기 전파 시간, nonce 수명 | 운영 |
| capability 만료 대기 상한 | D28 | 최대 TTL, purge 대기 시간 | 운영 |
| backup retention·crypto-erasure 기준 | D28 | 보존 일수, 키 폐기 시점 | 법무 + 운영 |
| downgrade 실패 복구 절차 | D20 | 롤백 범위와 판정 기준 | 개발 |
| tombstone 공개 범위·보존 근거 | D14 | 누구에게 보이는가, 보존 기간 | 법무 |
| offline cache 기본 24시간의 근거 | D16 | 그룹별 상한 조정 정책 | 제품 |

## 16. 교차 검증 기록

Codex(gpt-5.6-sol)와 4라운드. 코덱스가 잡아낸 내 초안의 사실 오류:

1. `source_snapshot.text` 스트립이 "영향 없다"는 주장 — `sourceSnapshot.required` 에
   `text` 가 있어 스키마 검증에서 실패하고, build 는 page 전체를 viewer shard 로 복제한다.
2. `id: "page:{owner}/{slug}"` — 현행 id pattern 이 `/` 를 불허하고 `id == "page:"+slug` 를
   강제한다.
3. `groups/{id}.json` 을 현행 `groups.json` 의 분할판으로 본 것 — 그 파일은 권한 DB 가
   아니라 그래프 시각화 설정이다.
4. "owner 별 build 후 index 병합" — 교차 edge 가 버려지고 stats 가 틀린다.

내가 잡아낸 것: `safe_name()` 의 파일명 충돌(`page:a/b-c` ≡ `page:a-b/c`), 그리고 5~9라운드에서
내 편집 실수로 중복된 탈퇴 절.

5~9라운드에서 추가로 잡힌 것(44건 중 주요): `supersedes`·`related` 가 스키마 top-level 에
없어 relation 정본을 `links[]` 하나로 확정해야 했던 것, projection 을 거친 발행본에 같은
`rev` 를 실으면 수신 측 검증이 성립하지 않는 것, page CAS 와 generation CAS 를 둘 다 두면
단일 CAS 권위가 깨지는 것, `deleted` 시점에 삭제 완료 증명을 주면 아직 남은 캐시·백업에
대해 거짓이 되는 것, 가시성 행렬의 판정축이 작성자가 아니라 entitlement 여야 하는 것,
발행 예외를 작성자 입력으로 만들 수 있으면 보안 경계가 무너지는 것.

내가 틀렸고 코덱스가 맞은 것: `sources: ["acme:release-process"]` 가 owner 를 잃는다는
진단. `SOURCE_REF` 가 그 형태를 거부하고 ingest 는 `source:` 를 붙여 owner 를 보존한다.

코덱스 반대로 뒤집은 결정: 영속 ID 에 owner 를 넣지 않는다(소유권 이전이 ID 를 깨뜨림).
사용자 결정으로 유지한 것: 비공개 그룹 기본값 `outbound_only` — §7.2 의 비간섭 조건
전부를 계약으로 포함한다는 전제로 코덱스도 조건부 합의.

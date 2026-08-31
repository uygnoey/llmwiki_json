# llmwiki 클라우드·다중 그룹 공유 상세 설계

- 상태: 설계 확정 (구현 전)
- 교차 검증: Claude Opus 5 ↔ Codex(gpt-5.6-sol) 4라운드, 최종 판정 **100% 정합**
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
s3://llmwiki/t/{owner}/gen/{generation}/pages/{owner}/{slug}.json   정본(remote DTO)
s3://llmwiki/t/{owner}/gen/{generation}/index/*.json                파생물
s3://llmwiki/t/{owner}/gen/{generation}/manifest.json               generation 내용물 목록
s3://llmwiki/t/{owner}/current.json                                 manifest pointer (CAS 대상)
s3://llmwiki/t/{owner}/tombstones/{page-uuid}.json                  삭제 표식
s3://llmwiki/e/{entitlement-epoch}/{workspace}/...                  entitlement 별 private artifact
```

- `raw/` 는 어떤 경로로도 올라가지 않는다. 그러나 경로 차단만으로는 부족하다 — §6 참조.
- reader 는 `current.json` 이 가리키는 generation 만 읽는다. 미완성 generation 은 보이지 않는다.
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
- **논리적 CAS 권위는 서버 DB 의 manifest row 하나로 확정.** transaction 안에서 expected
  application rev 와 entitlement 를 함께 검사하고 새 immutable generation ID 를 발행한다.
- object store 의 ETag·version ID 는 업로드 무결성·재시도·물리 객체 식별용 metadata 로만
  DB 에 기록한다. page body hash 를 그대로 `If-Match` 에 넣는다고 가정하지 않는다
  (multipart·SSE·provider 차이).

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

### 6.1 page remote DTO 필드 allowlist

| 필드 | local canonical | remote/shared | 비고 |
|---|---|---|---|
| `schema_version` `id` `slug` `title` `type` | 유지 | 유지 | |
| `owner` `created` `updated` `rev` | 유지 | 유지 | |
| `authors` | 유지 | **pseudonymous id 로 치환** | 표시명·이메일 미전송 |
| `visibility` `audience_group_ids` `sharing` | 유지 | 유지 | 수신 측 정책 판정에 필요 |
| `tags` `projects` | 유지 | 유지 | |
| `sources` `supersedes` `related` | 유지 | 유지 (UUID 로 resolve 된 값) | `raw:` ref 는 제거 |
| `blocks[].data` `block_order` `links` | 유지 | 유지 | |
| `blocks[].source_text` | 유지 | 유지 | 위키 본문 자체 — 제거 불가 |
| `blocks[].author` | 유지 | pseudonymous | |
| `summary` | 유지 | 유지 | |
| `raw_ref` | 유지 | **제거** (또는 sha256 치환) | 파일명이 곧 내부 정보 |
| `source_snapshot.text` | **필수** | **금지** | 원문 전문 |
| `source_snapshot.format` `.sha256` | 유지 | 유지 | 대조용 |
| `history[].note` | 유지 | **raw 경로 포함 시 제거** | ingest 가 경로를 남김 |
| `history[].actor` | 유지 | pseudonymous | |

remote DTO 는 위 표대로 **새 객체를 구성**하는 방식이며, 원본에서 필드를 지우는 방식이
아니다. 새 필드가 스키마에 추가돼도 allowlist 에 명시하지 않는 한 자동으로 나가지 않는다.

검사 대상 링크 표면 전수(D19): `[[wikilink]]`, block `refs`,
`sources`/`supersedes`/`related`, markdown `[label](url)`, bare URL, `raw_ref`,
`history.note`, `source_snapshot`. **결과는 차단이며 경고가 아니다.**

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
2. 같거나 넓으면: 같은 그룹 내 허용. 다른 그룹이면 출처 그룹의 `linking` 에 따름.
3. 대상 문서의 `sharing.links` / `sharing.citation` 이 false 면 거부.
4. 개인 `private` 페이지는 어떤 외부 참조의 대상도 될 수 없다.
5. backlink 를 대상 쪽 artifact 에 기록하는 것은 `vis(source) ≥ vis(target)` 이고
   `sharing.backlink` 가 true 일 때만.

| 출처 \ 대상 | public | 같은 그룹 | 다른 그룹 | 개인 private |
|---|---|---|---|---|
| public | 허용 | 거부 | 거부 | 거부 |
| 같은 그룹 | linking 따름 | 허용 | 거부 | 거부 |
| 개인 private | linking 따름 | 거부 | 거부 | 본인 것만 |

`linking` 값: `isolated`(밖으로 나가는 링크 전면 금지) / `outbound_only`(밖의 공개 문서
참조 가능, **backlink 억제**) / `open`(양방향).

relation kind 별 gate 대응:

| relation kind | 만드는 곳 | gate 하는 `sharing` 필드 |
|---|---|---|
| `wiki` | 본문 `[[owner/slug]]` | `links` |
| `related` | frontmatter `related` | `links` |
| `source` | frontmatter `sources` (인용·근거) | `citation` |
| `supersedes` | frontmatter `supersedes` | `citation` |

네 kind 모두 위 1~5 규칙을 동일하게 통과해야 하며, 추가로 각자의 gate 필드가 false 면
거부한다. backlink 기록 여부만 `sharing.backlink` 가 따로 판정한다.

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

**구독한 남의 owner namespace**

| action | 모든 role |
|---|---|
| read | ○ |
| create · update · delete · publish · share · transfer | **✗ (capability 자체를 발급하지 않는다)** |

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
3. DB transaction 에서 CAS 로 manifest row 를 교체하고 `current.json` pointer 를 갱신한다.
4. reader 는 `current.json` 이 가리키는 generation 만 읽는다.

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

**A. 전체 삭제** — 위 범위를 삭제하고 자리에 tombstone 을 남긴다. tombstone 은
pseudonymous stable ID 만 담고 **title·author 를 담지 않는다**. 공개 범위·접근권한·보존
근거를 명시한다. 개인정보 해당 여부는 법률 판단 영역으로 남긴다.

**B. 소유권 이전 후 유지** (D23) — user-facing state machine 의 명시적 분기. 결정할 것:
새 `auth_owner`, storage locator·alias 이전(**ID 는 D1 대로 불변**), 수용 그룹의 수락 권한,
`authors` 익명화·pseudonymization, 기존 links 와 manifest 갱신, 이전 실패 시 롤백, 사용자
최종 동의 시점.

### 탈퇴 상태 머신

```
requested ──▶ preview_built ──▶ user_confirmed ──┬──▶ deleting ──▶ deleted
     │              │                            │       │
     │              │                            │       └──(부분 실패)──▶ retrying ──┘
     │              │                            │
     │              │                            └──▶ transferring ──▶ transferred
     │              │                                     │
     └──(취소)──────┴─────────────────────────────────────┴──▶ cancelled
```

- `preview_built` — contribution event store 와 block lineage 로 삭제 대상 목록을 산출하고
  사용자에게 보여 준다. 공동 저작·인용·법정 보존 예외를 여기서 드러낸다.
- `deleting` 은 idempotent 하다. 각 대상(§10 삭제 범위)마다 완료 표시를 남기고 재시도해도
  같은 결과가 된다.
- `transferring` 실패 시 롤백해 `user_confirmed` 로 돌아간다. 부분 이전 상태를 남기지 않는다.
- 종료 상태(`deleted`/`transferred`)에서 compliance deletion ledger 에 완료 증명을 기록한다.

되돌릴 수 없는 것 — 남의 파생 문서, tombstone, 법정 보존 기록, 이미 배포된 캐시 — 는
[공유·재사용 및 탈퇴 동의](policy/ko/sharing-and-deletion.md)에서 가입 시 별도 동의를 받는다.

## 11. API 표면

| 엔드포인트 | 용도 | 비고 |
|---|---|---|
| `POST /v1/{owner}/pages/{slug}` | page 발행 (CAS) | 본문에 `expected_rev`. 불일치 시 **409** + 현재 rev·본문 반환 |
| `POST /v1/{owner}/generations` | generation 발행 후 manifest CAS 교체 | §8 프로토콜 |
| `GET /v1/{owner}/manifest` | 현재 generation pointer | 구독자의 델타 판정 기준 |
| `GET /v1/workspaces/{id}` | workspace manifest (구독 목록 + pinned revision) | D24 정본 |
| `GET /v1/objects/{key}` | **private object 인증 proxy** | 매 요청 entitlement 검사 → 즉시 차단 |
| `POST /v1/{owner}/capabilities` | public object 용 presigned GET 발급 | 단일 object·단일 method·TTL |
| `GET /v1/entitlements` | 현재 entitlement epoch | 로컬 캐시 GC 트리거 |
| `POST /v1/groups/{id}/invites` | 초대 발행 (owner 만) | 토큰 원문은 응답 1회만, 저장은 해시 |
| `POST /v1/invites/{token}/accept` | 초대 수락 | 만료·사용횟수 검사, transaction |
| `POST /v1/account/close` | 탈퇴 | `{mode: "delete" \| "transfer", target_group?}` |
| `GET /v1/account/close/preview` | 삭제 대상 preview | 상태 머신 `preview_built` |

`409` 를 쓰는 이유: HTTP `412` 는 조건부 헤더(`If-Match`)에 대응하는 코드인데 CAS 권위가
object store 가 아니라 DB 이므로(D9) 의미가 어긋난다. 충돌은 리소스 상태 충돌이다.

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
| viewer | `viewer/src/App.tsx:104` `navigateSlug` | alias 비교 |

## 14. 단계

1. **스키마 1.1 + ID 분리** — 데이터가 적을 때 해야 가장 싸다. ingest·validator·block
   ID·resolver·link key·test fixture 전부의 주소 체계 마이그레이션이다.
2. **remote DTO + storage 추상화** — 단일 사용자로 검증. 요구사항 6이 여기서 실제로 지켜진다.
3. **권한 DB + 인증 proxy + capability 발급** — 요구사항 2.
4. **generation·manifest CAS 발행** — 요구사항 1.
5. **workspace compiler + background sync** — 요구사항 4.
6. **격리 policy validator (server-side rejection)** — 요구사항 5.
7. **탈퇴 workflow + tombstone + GC** — 요구사항 3.

## 15. 교차 검증 기록

Codex(gpt-5.6-sol)와 4라운드. 코덱스가 잡아낸 내 초안의 사실 오류:

1. `source_snapshot.text` 스트립이 "영향 없다"는 주장 — `sourceSnapshot.required` 에
   `text` 가 있어 스키마 검증에서 실패하고, build 는 page 전체를 viewer shard 로 복제한다.
2. `id: "page:{owner}/{slug}"` — 현행 id pattern 이 `/` 를 불허하고 `id == "page:"+slug` 를
   강제한다.
3. `groups/{id}.json` 을 현행 `groups.json` 의 분할판으로 본 것 — 그 파일은 권한 DB 가
   아니라 그래프 시각화 설정이다.
4. "owner 별 build 후 index 병합" — 교차 edge 가 버려지고 stats 가 틀린다.

내가 잡아낸 것: `safe_name()` 의 파일명 충돌(`page:a/b-c` ≡ `page:a-b/c`).

내가 틀렸고 코덱스가 맞은 것: `sources: ["acme:release-process"]` 가 owner 를 잃는다는
진단. `SOURCE_REF` 가 그 형태를 거부하고 ingest 는 `source:` 를 붙여 owner 를 보존한다.

코덱스 반대로 뒤집은 결정: 영속 ID 에 owner 를 넣지 않는다(소유권 이전이 ID 를 깨뜨림).
사용자 결정으로 유지한 것: 비공개 그룹 기본값 `outbound_only` — §7.2 의 비간섭 조건
전부를 계약으로 포함한다는 전제로 코덱스도 조건부 합의.

# 클라우드 설계 결정

위키를 클라우드에 올려 여러 사람과 그룹이 나눠 쓸 때 지키는 **확정 결정**을 모았다. 이 문서만 읽고도 각 결정이 무엇이고 왜인지 알 수 있게 썼다(상세 근거는
[cloud-design.md](cloud-design.md)). 미결은 §7 에만 있어 확정과 섞이지 않는다. 설계를 바꾸면 이 문서의 해당 항목을 함께 고친다. 용어는 처음 나오는 자리에서
정의한다. 기본 셋: **owner** 는 위키 하나를 소유하는 사용자 또는 그룹, **page** 는 위키 문서 하나, **block** 은 page 안의 문단 단위, **publish(발행)** 는
서버에 올리는 일, **workspace** 는 한 사용자가 자기 위키와 구독한 위키를 합쳐 보는 묶음이다.

## 1. 식별과 주소

- **page 의 영속 ID 는 `page:<uuid>` 이고 owner 와 무관하다.** block ID 도 page ID 에 종속되지 않게 바꾼다. 소유권이 넘어가도 ID 는 불변이므로 기존
  링크가 깨지지 않는다.
- **사람이 쓰는 주소는 `owner/slug` 이며 ID 가 아니라 바꿀 수 있는 별명(alias)이다.** 본문 링크는 `[[owner/slug]]` 로 쓴다. 콜론은 relation(page
  사이의 관계 항목 `sources`·`supersedes`·`related`) prefix 문법(`source:`,`page:`,`raw:`,`user:`) 전용으로 남긴다.
  `page_lookup` 에 owner 가 붙은(qualified) alias 를 색인하고 viewer 의 `navigateSlug` 도 alias 를 비교한다. 이유: 콜론을 relation
  prefix 와 alias 구분자 양쪽에 쓰면 문법이 중첩되어 검증 경로별로 해석이 갈린다 — 본문 wikilink 는 `page_links()` 가 target 전체를 보존하지만, relation
  문자열은 `SOURCE_REF` 가 `page|source|raw|user` prefix 를 요구하고 ingest(소스를 page 로 넣는 단계)가 `source:` 를 덧붙인다.
- **정본 relation 에는 alias 가 아니라 `page:<uuid>` 를 저장한다.** 사람이 입력한 `owner/slug` 는 publish 전에 UUID 로 resolve(해석해
  치환)한다. `sources`·`supersedes`·`related` 전체의 relation 문자열 문법을 하나로 명시한다.
- **relation 의 정본 표현은 `links[]` 하나다.** 현행 스키마 top-level 에는 `supersedes`·`related` 가
  없고(`additionalProperties: false`), ingest 도 두 값을 top-level 에 저장하지 않고 `links[]` 로만 변환한다. frontmatter(markdown
  머리의 메타데이터)의 `sources`·`supersedes`·`related` 는 *입력 문법*이며 정본 필드가 아니다. `sources` 만 근거 추적용으로 top-level 에 남고, 그 값도
  publish 전에 UUID 로 resolve 한다.
- **`sources[]` 중 page 를 가리키는 ref 의 집합은 `links[kind=source]` 의 target 집합과 정확히 같아야 한다.** relation 이 두 곳에 표현되므로
  불변식이 필요하다. build 가 `links[]` 에서 파생시키고 validator 가 불일치를 오류로 잡는다. `user:`·`raw:` ref 는 대상이 아니다.
- **저장 위치(shard locator)는 `pages/{owner}/{slug}.json` 이다.** shard 는 viewer 가 page 하나씩 내려받는 정적 JSON 파일이다. build 의
  세 군데 `safe_name` 호출을 단일 `page_data_url(page)` 로 통일한다. owner·slug 에 canonical alphabet 을 강제하고(slug 내 슬래시 금지,
  유니코드 정규화), same-owner alias 유일성을 검사한다. 손실 변환 충돌은 alias 검증 또는 hash suffix 로 막는다. 정적 host 의 nested path 제공 여부는
  배포 시 별도 검증 대상.

## 2. 누가 무엇을 보나

### 2.1 권한의 정본과 역할

- **접근제어 그룹·membership·invite·role 은 Postgres 가 정본이다.** 현행 `tools/config/groups.json` 은 그래프 시각화 설정 그대로 유지하고 이름과
  역할을 분리한다. object storage(S3 같은 파일 저장소)에는 권한 판정에 쓰이지 않는 **signed snapshot**(서명된 권한 사본)만 둔다.
- **signed snapshot 에 issuer, audience/workspace, entitlement epoch, issued/expiry, key ID 를 포함하고, client 와
  compiler(§4 의 workspace compiler)는 manifest 적용 전에 검증해 실패 시 fail-closed(거부) 한다.** manifest 는 "지금 어느 발행본이 현재인가"
  를 가리키는 pointer 다(§3.1). entitlement 는 "이 principal(사용자 또는 그 client)이 이 자료를 읽을 자격이 있다" 는 권한 DB 의 판정이고, epoch 는
  권한이 바뀔 때마다 오르는 번호다. key rotation·폐기·replay 방어 절차를 정의한다(실제 값은 §7 미결).
- **role/action 행렬: 구독한 남의 owner namespace 에 대해 reader 에게는 GET capability 만 발급하고 PUT·DELETE·publish capability 를
  어떤 경로로도 발급하지 않는다.** capability 는 가진 것만으로 접근이 되는 토큰이다. 행렬 축은
  `(주체 role: owner/editor/reader) x (대상 owner: 자기/구독) x (action: read/create/update/delete/publish/share/transfer)`
  이며 전 칸을 값으로 고정한다. 공동 편집이 필요하면 "남의 위키 read-only" 와 섞지 않고 별도 collaboration workspace 로 분리한다.

### 2.2 가시성과 링크

- **page 에 `audience_group_ids`(볼 수 있는 그룹 목록)를 둔다.** `(source audience, target audience, relation kind)` 전수 행렬을
  deny-by-default(명시 허용만 허용)로 정의한다. 문서별 `sharing` 은 그룹 정책보다 조일 수만 있다(deny-overrides, 거부가 우선).
- **가시성 행렬의 판정 기준은 작성자가 아니라 entitlement 다.** "같은 작성자" 가 아니라 "A 의 principal 이 B 를 읽을 entitlement 를 보유" 로 판정한다.
  그렇지 않으면 정상 reader 가 거부되고 membership 을 잃은 과거 author 가 허용된다.
- **`linking` 세 값의 실질 차이: `isolated` 는 밖으로 나가는 링크 전면 금지, `outbound_only` 는 밖의 `public` 문서만 참조 가능하고 backlink(역링크)
  억제, `open` 은 밖의 `group` 문서까지 참조 가능하고 backlink 노출.** `open` 이 `outbound_only` 보다 더 허용하는 지점은 cross-group
  `group → group` 하나뿐이다.
- **비공개 그룹 기본값은 `outbound_only`, `isolated` 도 선택 가능.** 다음 비간섭 조건 7개를 설계 계약에 포함한다: (1) public build 입력에서 private
  전면 배제. (2) graph·index·shard·stats·degree·orphan·search·unresolved label·backlink·revision 값과 갱신시각·cache key
  까지 private 입력 무영향. (3) private artifact(build 산출물)는 entitlement 별 private keyspace(별도 저장 경로)이며 public
  CDN·cache 와 공유하지 않음. (4) 교차 링크 resolution 은 로컬 authorized corpus(권한 있는 page 전체)에서 수행하고 서버 요청에는 public target
  ID 만 전송. (5) public owner 대상 알림·analytics·access log 에 private source 미기록. (6) private viewer 에서 remote
  image·script·embed 자동 로드 금지. (7) `Referrer-Policy: no-referrer` 등으로 private node ID 가 든 URL 이 header·log 로 나가지
  않게 통제.
- **visibility x relation 행렬의 전 칸을 값으로 확정한다.** 축은 `public|group|private` x `same-group|cross-group` x
  `wiki|source|supersedes|related` x `inbound|outbound` 이고 각 칸이 allow/deny 다. 위 비공개 그룹 기본값이 허용하는 private->public
  outbound 와 public artifact 비간섭도 이 표에 직접 들어간다. 작업 결정이 아니라 값으로 확정한다.

### 2.3 전달과 강제

- **권한 DB 가 먼저이고 presigned URL 은 전달 수단이다. private object 는 매 요청 entitlement 를 검사하는 인증 proxy / signed request 로
  전달해 즉시 차단을 보장한다.** presigned URL 은 만료 전까지 URL 을 가진 누구나 쓸 수 있는 bearer capability 이므로 "짧은 TTL(time-to-live, URL
  이 유효한 최대 시간)" 과 "즉시 차단" 은 같은 메커니즘으로 동시에 보장되지 않는다. 즉시성이 필요 없는 경로만 직접 presigned GET 을 쓰고, 그때는 API 계약에 "최대 TTL 이내
  무효화" 로 보장 수준을 명시 구분한다. 단일 object·단일 method scope, entitlement epoch 를 artifact key 에 포함, 감사 로그는 그대로 둔다.
- **보안 경계는 lint(로컬 점검 도구) 경고가 아니라 server-side publish rejection(서버의 발행 거부)이다.** 현행 lint 와 분리된 policy validator 를
  둔다.
- **`sharing` 의 `derivation`(파생 이용 조건)은 기술적으로 강제할 수 없으므로 접근 제어가 아니라 이용정책·동의 영역이다.** 문서와 코드 주석에 명시하고, 강제 가능한 것과
  분리한다.

## 3. 쓰기와 발행

### 3.1 발행 단위와 조건부 갱신

generation 은 한 번의 발행이 만든 불변(immutable) object 묶음이고, §2.1 의 manifest 는 현재 generation 을 가리킨다.
CAS(compare-and-swap)는 "내가 본 값이 그대로면 바꾸고 아니면 실패" 하는 조건부 갱신이다.

- **`rev = sha256(canonical(page without rev))`. 논리적 CAS 권위는 서버 DB 의 manifest row 하나로 확정한다.** transaction 안에서
  `expected_manifest_rev` 와 entitlement 를 함께 검사하고 새 immutable generation ID 를 발행한다. object store 의 ETag·version
  ID 는 업로드 무결성·재시도·물리 객체 식별용 metadata 로 DB 에 기록하되 논리적 page CAS 권위로 쓰지 않는다. DB 없는 배포형이 필요해지면 그때 별도
  object-store-only protocol 을 설계한다(현 범위 밖).
- **CAS 값은 `manifest_rev` 다.** 여러 page 가 한 generation 으로 commit 되므로 개별 page 의 `rev`(= `page_rev`)는 CAS 값이 될 수
  없다. `page_rev` 는 draft(발행 전 작업본) stage 의 stale(낡은 판) 검사와 provenance(출처 추적) 용이고, 발행 CAS 는
  `expected_manifest_rev` 하나로 한다.
- **다중 object 발행은 immutable generation prefix 에 전부 올린 뒤 manifest pointer 하나만 CAS 로 교체한다.** reader 는 manifest 가
  가리키는 generation 만 읽는다.
- **권위 pointer 는 DB manifest row 하나뿐이고, object store 의 `current.json` 은 cache 다.** Postgres 와 object store 사이에는
  원자적 transaction 이 없으므로 `current.json` 은 outbox(transaction 안에 적어 두고 나중에 처리하는 큐)가 뒤늦게 맞추며 권위가 아니다. 두 값이 갈리면 DB
  row 가 이긴다. page 를 개별 발행하는 endpoint 를 따로 두지 않는다 — page 쓰기는 draft 에 stage 만 하고 CAS 는 generation commit 하나뿐이다.
- **`rev` 와 `payload_sha256` 을 분리한다.** projection(허용 필드만 골라내기)을 거친 발행본은 local canonical(로컬 정본)과 바이트가 다르므로 같은
  `rev` 로는 수신 측 검증이 성립하지 않는다. `rev` 는 canonical 세대 참조값, `payload_sha256` 은 발행본 자신의 해시다.

### 3.2 스키마

- **schema 1.1 base 는 `sourceSnapshot.required = [format, sha256]` 이고 그 위에 profile validator 둘을 둔다.** local
  canonical 은 source snapshot(원문 사본)이 있으면 `text` 필수, remote/shared 는 `text` 금지. `render_markdown(exact=True)` 는
  text 부재 시 KeyError 가 아니라 명시적 WikiError 를 낸다.
- **마이그레이션은 1.0/1.1 혼재 rolling 을 허용하고, 구버전 client 는 read-only, downgrade 금지다.** 실패 복구 절차를 정의한다(절차 자체는 §7 미결).

### 3.3 원격 payload

- **원격 업로드 payload(서버에 올리는 데이터)는 경로 allowlist(허용 목록)가 아니라 필드 allowlist projection(remote DTO)으로 새 객체를 구성하고 업로드
  직전 재검증한다.** DTO 는 허용 필드만 골라 새로 만든 전송용 객체다. 필수 제거/치환 대상: `source_snapshot.text`, `raw_ref`, raw 경로가 든
  `history.note`, 로컬 절대경로, secret-like 값.
- **object type 별 remote DTO: page 뿐 아니라 index, append log(추가 전용 기록), tombstone(삭제된 page 자리에 남는 표식), manifest
  각각에 allowlist schema 를 둔다.** 특히 ingest log 는 현재 source path 를 담으므로 remote log DTO 에서 raw filename·path 를 금지한다.
  `search.json` 의 block 전문은 **공개 shared index 에 넣지 않고 entitlement 별 private index 에만** 넣는다.
- **격리·유출 검사 대상 링크 표면 전수: `[[wikilink]]`, block `refs`, `sources`/`supersedes`/`related`, markdown
  `[label](url)`, bare URL, `raw_ref`, `history.note`, `source_snapshot`.** 검사 결과는 차단이며 경고가 아니다.
- **필드 allowlist 만으로 부족하므로 발행 직전 remote validator 가 DTO 의 모든 문자열 값을 재귀 순회해 검사한다.** 유지되는 임의 문자열(`title`,
  `summary`, `blocks.*.data`, `blocks.*.source_text`, `links[].label`)에도 raw 경로·절대경로·자격 증명이 들어갈 수 있다. 오탐도 차단이
  기본이며, 예외는 아래 승인 record 로만 성립한다(작성자 입력으로는 만들 수 없다).
- **remote validator 의 예외는 작성자 입력으로 만들 수 없다.** 별도 권한을 가진 승인자의 승인 record(대상 page·필드·문자열 해시·승인자·사유·만료)로만 성립하며, raw
  경로에는 어떤 예외도 허용하지 않는다. 고엔트로피 검사(비밀키처럼 무작위해 보이는 문자열 탐지)는 field-aware(필드별 규칙)로 하고 해시·ID 필드는 대상에서 제외한다.

## 4. 여러 위키 합치기

- **여러 owner 조합은 index 병합이 아니라 재-projection 이다.** workspace compiler 가 authorized owner set 전체를 대상으로 link
  resolution, visibility filtering, backlink suppression, degree·stats·routes 재계산을 수행한다. owner 별 build 는
  unresolved cross-edge(대상을 못 찾은 다른 owner 로의 링크)를 버리지 않고 기록해 보존한다.
- **workspace 의 정본은 `workspace manifest = personal owner + 선택한 group/owner 구독 + pinned revision` 이다.** qualified
  `owner/slug` 는 항상 유일하게 resolve 한다. bare slug 는 self-owner 우선, 그래도 모호하면 **결정적 ambiguity error** 를 낸다(임의 선택 금지).
  현행 `page_lookup` 의 title 기반 fallback 은 교차 owner 에서 비활성화한다.

## 5. 삭제와 탈퇴

### 5.1 삭제

- **삭제 판정 근거는 append-only contribution event store(추가만 하는 기여 기록)와 block lineage(블록 계보)다.** block ID 가 내용
  fingerprint 기반이라 수정 시 바뀌므로 현재 `author` 필드만으로는 판정할 수 없다. 공동 저작 정책과 분쟁 처리 규칙을 함께 정의하고, 삭제는 preview 후
  idempotent(반복 실행해도 결과가 같은) workflow 로 실행한다.
- **삭제 범위를 목록으로 고정한다: canonical page, owner index, viewer shard, search body, graph metadata, qmd index(로컬 검색
  색인), 구독자 로컬 cache, object versions·backups, 발급된 capability, contribution event store.** 상태 머신·재시도·감사 가능한 완료
  증명·backup 보존기한을 둔다.
- **log 를 둘로 나눈다.** (1) contribution event store — 암호화된 actor mapping(가명↔실제 사용자 대응)과 content delta(변경 내용)를 담고,
  탈퇴 workflow 가 해당 content 를 삭제하거나 키를 폐기한다. (2) compliance deletion ledger(삭제 처리 대장) — content·title·path·직접 사용자
  ID 없이 pseudonymous(가명) request ID, 처리 단계, 시각, 결과만 보존한다. 법정 보존이 필요한 mapping 은 별도 접근통제와 보존기한을 적용한다. deletion
  preview 가 참조하는 snapshot 과 완료 증명이 각각 무엇을 남기는지 명시한다. 이로써 위 append-only 요구와 삭제 요구가 양립한다.
- **`deleted` 와 `purged` 를 나눈다.** `deleted` 는 online canonical 삭제 완료이고, 발급된 capability·구독자 offline cache·object
  version·backup 은 `purge_pending` 을 거쳐 `purged` 에서 끝난다. **완료 증명은 `purged` 에서만 발급한다** — `deleted` 시점에 주면 아직 남은
  캐시·백업에 대해 거짓이 된다. `blocked`(legal hold, 법적 사유의 삭제 보류)와 `manual_review` 를 별도 상태로 둔다.
- **tombstone 의 허용 필드는 `schema_version` · `id`(pseudonymous stable) · `deleted_at` 셋뿐이며 title·author·경로를 담지
  않는다.** 공개 범위·접근권한·보존 근거를 명시한다. 개인정보 해당 여부는 법률 판단 영역으로 남긴다(§7).
- **generation garbage collection: manifest 에서 끊긴 immutable generation 의 삭제 기준(활성 capability 만료 대기, subscriber
  acknowledgement 대기 상한), 법정 backup retention 과 crypto-erasure(키를 폐기해 읽을 수 없게 만드는 삭제) 적용 방식을 확정한다.** §3.1 의
  immutable 원칙과 위 삭제 요구는 이 GC 규칙으로 연결한다(기준값은 §7 미결).

### 5.2 탈퇴 시 소유권 이전 후 유지

- **"소유권 이전 후 유지" 를 user-facing state machine(사용자에게 보이는 상태·전이 목록)의 명시적 분기로 둔다.** 결정할 것: 새 `auth_owner`, storage
  locator·alias 이전(ID 는 §1 대로 불변), 수용 그룹의 수락 권한, `authors` 익명화·pseudonymization, 기존 links 와 manifest 갱신, 이전 실패 시
  롤백, 사용자 최종 동의 시점. 아래 두 결정이 이 목록의 답이다.
- **이전은 2단계다: 수용 그룹에 제안을 보내고(`offer_sent`) 그 그룹 owner 가 수락해야(`target_accepted`) locator·alias 를 바꾼다.** 사용자 최종
  동의는 수락 직후 한 번 더 받는다 — 어느 그룹이 받는지 확정된 뒤여야 의미 있는 동의다. 거절·만료는 `user_confirmed` 로 되돌아간다.
- **이전이 바꾸는 것: `auth_owner` 는 수용 그룹 ID 로 교체, `owner`·storage locator·alias 는 `target_accepted` 이후에만 변경,
  page·block ID 는 불변, `authors[]` 와 `blocks.*.author` 는 익명 식별자로 필수 치환, `links[]` 와 남의 `sources[]` 는 UUID 를 가리키므로
  갱신 불필요, index 는 다음 generation 재-projection 에서 자동 반영, 실패 시 `awaiting_user_reconfirm` 으로 롤백.** 사용자 최종 동의는 별도 상태
  `awaiting_user_reconfirm` 이며 최초 confirm 과 다른 전이다.

## 6. 동기화와 캐시

- **로컬 cache 는 entitlement epoch 기반 강제 GC, 캐시 암호화와 키 폐기를 적용한다.** online access 는 §2.3 의 proxy 로 즉시 차단하고,
  **offline access 는 별도 보장으로 분리해 최대 잔존 시간을 출시 정책에서 숫자로 확정한다**(기본값 24시간). 강한 격리가 필요한 private group 은 offline
  cache 금지 또는 짧은 lease(짧은 유효기간의 임대)를 선택할 수 있다. "정의한다" 로 미루지 않는다.
- **hook 은 prompt 경로에서 network I/O 를 하지 않는다.** hook 은 LLM prompt 마다 로컬에서 실행되는 스크립트다. 별도 background sync 가 원격
  revision 과 entitlement 를 pull 해 local manifest 를 원자적으로 교체하고, hook 은 그 manifest 만 읽는다. 현행 fail-open(실패해도 막지
  않음)·저지연 특성을 유지한다.

## 7. 아직 미결

확정이 아니라 미결이다. 결정 주체와 기한은 [cloud-design.md](cloud-design.md) §15 에서 추적한다.

- signed snapshot 의 key rotation·폐기·replay 방어 절차의 실제 값 (§2.1)
- generation GC 의 capability 만료 대기 상한, backup retention, crypto-erasure 기준 (§5.1)
- 스키마 downgrade 실패 복구 절차 (§3.2)
- tombstone 공개 범위·보존 근거 (법률 판단) (§5.1)
- offline cache 24시간을 그룹별로 조정하는 정책 (§6)

## 옛 D번호 ↔ 새 절

| 옛 D번호 | 새 절 |
|---|---|
| D1 · D2 · D2a · D3 · D26c | §1 식별과 주소 |
| D4 · D20 | §3.2 스키마 |
| D5 · D19 · D26 · D26a · D26d | §3.3 원격 payload |
| D6 · D22 · D27 | §2.1 권한의 정본과 역할 |
| D7 · D8 · D8a · D25 · D25a | §2.2 가시성과 링크 |
| D9 · D9a · D10 · D10a · D26b | §3.1 발행 단위와 조건부 갱신 |
| D11 · D24 | §4 여러 위키 합치기 |
| D12 · D13 · D13a · D13b · D14 · D28 | §5.1 삭제 |
| D15 · D18 · D21 | §2.3 전달과 강제 |
| D16 · D17 | §6 동기화와 캐시 |
| D23 · D23a · D23b | §5.2 탈퇴 시 소유권 이전 후 유지 |
| 미결 5개 (D27·D28·D20·D14·D16 의 값) | §7 아직 미결 |

검토 이력: 원 결정표는 Claude Opus 5 와 Codex(gpt-5.6-sol) 4라운드 교차 검증으로 100% 정합 판정. 2026-09-02 claude 재작성 후 codex·grok 교차
검토 3라운드 — 라운드 1 지적 codex 3건·grok 2건(용어 정의 위치), 라운드 2 반영, 라운드 3 둘 다 합의 0건.

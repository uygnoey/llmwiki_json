# 자동 컨텍스트 주입 (Codex · Claude Code)

이 Mac 의 Codex 와 Claude Code 가 **모든 질문을 처리하기 전에** `llmwiki_json` 정본에서
관련 근거를 찾아 프롬프트에 붙인다. 구현은 `scripts/llmwiki_context.py` 하나이고,
두 클라이언트 모두 `UserPromptSubmit` hook 으로 같은 실행 파일을 부른다.

## 동작

```
사용자 질문
  └─ UserPromptSubmit hook  (Codex / Claude Code 공통)
       └─ /usr/bin/python3 scripts/llmwiki_context.py hook
            1. index/search.sqlite 를 읽기 전용(immutable)으로 연다          [mode=index]
               — 없거나 revision.json 과 다르거나 정본 파일이 더 새거나 열 수 없으면
                 ⇢ 정본 wiki/**/*.json 에서 메모리 색인을 만들어 2~5 를 똑같이 돈다
                   (stats.mode=memory, stats.fallback=이유, stats.memory_build_ms)
               — 그 build 마저 실패하면(정본 손상 등)
                 ⇢ 최후 스캔 경로(옛 <llmwiki-context> 형식)로 exit 0 을 지킨다 [mode=scan]
            2. 색인 검색: 한글 2-gram + 라틴 낱말 + `_ . - : /` 조각, BM25 impact posting,
               간선 가중 확산 2 step, supersedes 체인은 head 로 접는다
            3. hit page 의 정본 sha 를 index/map.json 의 값과 대조 — 다르면 그 page 만 정본 재독
            4. 부분 그래프 투영: page 10개(1위 대비 50% 컷), page 당 근거 block ≤ 4, 큐레이션 간선
            5. P/B/E 렌더: 긴 block 은 질문 토큰 idf² 로 행을 골라 320자 안에, 바이트·토큰 상한 둘 다
            └─ {"hookSpecificOutput":{"hookEventName":"UserPromptSubmit",
                                       "additionalContext":"<llmwiki-context v=3>…"}}
```

### 색인이 정본을 대신 읽는다

색인은 `python3 scripts/llmwiki.py build` 만 만든다(`scripts/llmwiki_index.py`, 표준 라이브러리).
훅은 색인을 읽기 전용으로 열고 정본은 두 경우에만 읽는다 — hit page 의 sha 가 색인과 다를 때
그 page 하나, 그리고 `llmwiki_get`. 신선도는 세 겹으로 확인한다: `meta.revision` 이
`index/revision.json` 과 같아야 하고, `wiki/` 아래에 색인보다 새 json 이 없어야 하고, hit page 의
sha 가 정본과 같아야 한다. 앞의 둘이 어긋나면 메모리 색인으로, 셋째가 어긋나면 그 page 만 재독이다.
그래서 `index/` 가 stale 해도 낡은 근거가 나가지 않는다.

증분 build(`build --changed <파일…>`, 감시자와 `ingest` 가 부른다) 는 `index/search.work.sqlite` 에서
바뀐 page 의 행만 갈아 끼우고, 빈 파일에 같은 DDL 순서·PK 순으로 표를 다시 써 `index/search.sqlite` 를
발행한다(`VACUUM INTO` 가 아니다 — 그것은 작업 DB 의 schema cookie 를 헤더에 복사해 compact 횟수가 발행본
바이트에 남았다). 그래서 발행본은 이력과 무관하게 cold build 와 헤더까지 같은 바이트다. 훅이 보는
것은 발행본뿐이고 revision 비교는 그대로다 — 증분 뒤에도 `revision.json` 이 새 값으로 바뀌어 훅이
새 색인을 쓴다. 발행이 `os.replace` 라 갱신 중의 조회는 옛 파일을 끝까지 읽고 오류가 없다.

작업 DB 의 compact(`VACUUM`) 는 **free page 비율이 10% 를 넘을 때만** build 안에서 돈다. posting 을
제자리에서 교체하는 한 층 구조라 delta/tomb 층이 없고, 본문 수정은 free page 를 만들지 않아 compact
대상이 아니다 — free page 는 page 삭제가 만든다. 힌트 밖 변경은 지난 build 가 파일마다 기록한
`(mtime_ns, size)`(`index/search.work.json` 의 `files`) 와 다르기만 하면 — mtime 을 과거로 돌린 편집,
timestamp 를 보존한 복사도 — sha 로 확인해 전량으로 떨어진다.

### 세 경로 — 호출자는 언제나 같은 문법을 받는다

| mode | 언제 | 검색·투영·렌더 | 출력 |
|---|---|---|---|
| `index` | `index/search.sqlite` 가 신선할 때 | `llmwiki_index` (BM25·간선 확산·P/B/E) | `<llmwiki-context v=3>` |
| `memory` | 색인이 없거나(`no-index`) revision 이 다르거나(`revision-mismatch`) 정본이 더 새거나(`stale-mtime`) 열 수 없을 때(`open-error:*`, `index-error:*`), 또는 `LLMWIKI_CONTEXT_INDEX=0` (`disabled`) | 정본에서 `llmwiki_index.build_memory` 로 메모리 색인을 만들어 **index 와 같은 코드** 를 돈다 (`llmwiki.py query` 가 낡은 색인 앞에서 하는 것과 같다). `stats.fallback` 에 디스크 색인을 못 쓴 이유, `stats.memory_build_ms` 에 build 시간 | `<llmwiki-context v=3>` — index 와 바이트 단위로 같다 |
| `scan` | 메모리 색인 build 마저 실패했을 때(`…;memory-error:*`), 정본이 비었을 때(`…;memory-empty-corpus`), 또는 `LLMWIKI_CONTEXT_MEMORY=0` / `--scan` (`disabled;memory-disabled` — 이때는 디스크 색인도 보지 않는다) | 옛 스캔 검색(`retrieve`/`rank`/`rank_blocks`, 문턱 `MIN_SCORE` 6.0 · `MIN_COVERAGE` 0.34 · hint 주소 3줄)과 옛 `render` | `<llmwiki-context>` (옛 형식) — 훅이 exit 0 을 지키기 위한 바닥 |

메모리 색인은 매 호출마다 정본 전체를 읽어 만든다. 훅 프로세스 전체(인터프리터 기동 포함)
p50 은 6 page 53 ms, 296 page 개인 위키 517 ms, 334 page 자연 세트 399 ms 로 워치독 6초
안이다(bench/IMPL_REPORT.md §6). 색인 경로(≈60 ms)보다 느리므로 정상 상태는 `build` 로 색인을
두는 것이고, 메모리 경로는 색인이 잠시 없거나 낡은 사이의 다리다. `scan` 경로 전용 상수·함수는
최후 수단으로만 남아 있고 새 기능은 붙지 않는다.

측정(bench/IMPL_REPORT.md): 296 page 개인 위키에서 훅 프로세스 전체가 정본 스캔 120~500 ms →
색인 60~90 ms(인터프리터 기동 포함), 자연 세트 334 page 의 검색 p50 은 62 ms → 2.4 ms.

### 주입 내용 (P/B/E)

```
<llmwiki-context v=3>
정본(wiki/**/*.json) 부분 그래프. P=page(slug type updated src=근거 sources) B=block(...) E=간선(...) ...
P 폐기-icd-코드 concept 2026-08-12 src=source:beta-폐기-icd-코드-정책-비교,user:2026-08-12
B 폐기-icd-코드#cf60869ae505ae70:1 cur | 전체 모집단은 324건이며 …
B 폐기-icd-코드#9b1d…:1 conflict | ⚠️ 상충: OSE_DATA_03 문서는 149건이라고 …
E 폐기-icd-코드#cf60869ae505ae70:1 related→ose-이슈-목록#…
P 옛-정책 concept 2026-07-01 sup→폐기-icd-코드
</llmwiki-context>
```

- `P` page 한 줄: slug · type · updated · `src=` sources. 제목·summary·tags 는 싣지 않는다(slug 로
  `llmwiki_get` 이 푼다).
- `B` block: `<slug>#<id 꼬리> 상태 | 본문`. 상태는 `cur` / `conflict`(미판정 상충, 양쪽 병기) 다.
  320자를 넘는 block 은 앞부분이 아니라 **질문과 겹치는 행**(표 행·목록 항목·문장)만 고르고
  표는 머리 행을 붙인다. `…` 는 생략 표시다. 결과는 반드시 320자 이하다.
- `E` 간선: `related` · `supersedes` 만. wiki 링크는 본문의 `[[…]]` 에 이미 있다.
- `sup→X`: X 로 대체된 낡은 page. **본문을 싣지 않는다** — 낡은 주장이 섞일 수 없다. 한 page 를
  둘이 대체했거나(fork) 서로 대체하면(cycle) 접지 않고 `sup?fork` / `sup?cycle` 로 표시한다.
  옛 본문을 묻는 질문에는 **head 가 본문과 함께** 나온다 — 근거 block 은 (1) head 의 supersedes 링크를
  든 anchor block(`links[].block_id`), (2) 없으면 head 에서 질문 토큰과 가장 맞는 block, (3) 그것도 없으면
  head 의 첫 본문 block(heading·제목 제외) 순이다. 옛 page 는 head 바로 뒤에 `sup→head` 한 줄로 따라오며,
  점수가 cut 아래거나 상위 k 밖이어도 head 가 실리면 실린다.
- `A` 주소만: 무주입 문턱을 켰을 때 약한 신호 구간에서만 나온다.
- heading·제목 block 은 page 를 찾는 데는 쓰이지만 근거로는 나가지 않는다.

예산은 page 단위가 아니라 **block(줄) 단위**로 채운다 — gold block 하나가 들어갈 자리가 있으면
그 page 의 나머지가 커도 버리지 않는다. block 이 하나도 못 들어가는 page 의 `P` 줄은 싣지 않는다 —
낡은 page 의 `sup→` 줄은 예외다(본문이 없는 것이 정보다). 접힌 head 는 위 순서로 근거 block 을 반드시
갖는다.

### 늘 붙는 것 (고정 주입)

`tools/config/context.json` 의 `always` 에 적은 page 는 검색 점수와 상관없이 매 질문에
붙는다. 이 사람이 일하는 방식처럼 매번 알아야 하는 것을 여기 둔다.

```json
{ "always": ["ai-작업-규칙"], "always_max_bytes": 800 }
```

- 예산(`always_max_bytes`, 기본 800B)은 검색 몫과 **따로** 잡는다 — 근거를 밀어내지 않는다
- 싣는 것은 page 제목·요약과 앞쪽 block 몇 줄뿐이다. 자세한 것은 검색에 맡긴다
- 고정한 page 가 검색에도 걸리면 중복으로 다시 싣지 않는다
- 검색이 아무것도 못 찾아도 고정분은 나간다 — 그것이 '항상' 의 뜻이다
- 설정 파일이 없거나 slug 를 못 찾으면 아무것도 고정하지 않는다

### 무주입 문턱은 옵션이다

기본은 **문턱 없음**이다: 정본과 한 토큰도 겹치지 않으면 `no-match` 로 아무것도 넣지 않고,
겹치는 것이 있으면 넣는다. 동결 자연 세트(334 page·284문항)에서 어떤 무주입 신호도 오탐 5% 를
지키면서 answerable 주입을 0.47 이상 올리지 못했고, 문턱을 켜면 정답 전달이 0.543 → 0.478 로
떨어졌다(bench/FINAL_PROPOSAL.md §4). 무관 질문에 3.4 KB 를 쓰는 비용은 하루 약 $0.2 수준이다.

켜고 싶으면 환경변수로 켠다.

```bash
export LLMWIKI_CONTEXT_SILENCE=770   # raw_top × coverage 가 이 값 미만이면 무주입
export LLMWIKI_CONTEXT_HINT=500      # (선택) HINT ≤ 신호 < SILENCE 면 본문 대신 주소(A) 줄만
```

770 은 동결 자연 세트에서 고른 값이다. **위키마다 질문 로그로 다시 보정해야 한다** — 다른
위키에서 같은 값이 맞는다고 가정하지 않는다. `context "질문" --json` 의 `signals.raw_x_cov` 로
자기 위키의 분포를 볼 수 있다.

색인이 없을 때의 메모리 색인 경로도 같은 문턱을 쓴다. 최후 스캔 경로(`mode=scan`)만 옛 규칙
(점수 6.0 · 커버리지 0.34 · hint 주소 3줄)이 그대로 동작한다.

### 안전 장치

| 요구 | 구현 |
|---|---|
| 토큰/바이트 상한 | `--max-bytes` 기본 6000, `--max-tokens` 기본 2000. 줄을 더할 때마다 둘 다 검사하고, 마지막에 전체를 다시 검사해 넘치면 page 를 덜어낸다 |
| 낮은 관련도 무주입 | 정본과 한 토큰도 겹치지 않으면 무주입. 문턱은 옵션(`LLMWIKI_CONTEXT_SILENCE`, 기본 꺼짐) |
| 색인 fail-open | 색인 없음·revision 불일치·정본이 더 새 것·sqlite 오류 전부 정본에서 만든 메모리 색인으로(같은 P/B/E). 그 build 마저 실패하면 최후 스캔으로. hit page 가 정본과 다르면 그 page 만 재독, 지워졌으면 뺀다. stats 에 `mode`(index/memory/scan)·`fallback`·`reread`·`memory_build_ms` 를 남긴다 |
| 실행 fail-open | stdin 파싱 실패·정본 손상·예외·타임아웃 전부 stdout 없이 `exit 0`. 워치독 6초(색인 열기·메모리 색인 build 포함) |
| 자격증명 미출력 | `api_key`/`token`/`password`/`secret`·Bearer·PEM·알려진 토큰 접두·URL 계정을 `(접속 정보 생략)` 으로 치환. 색인에 저장되는 본문도 같은 규칙으로 지운 뒤 저장한다 |
| 낡은 주장 미출력 | supersedes 로 대체된 page 는 `sup→head` 한 줄만. 자연 세트 낡은 본문 누출 0.63 → 0.00 |
| 전역 동작 | hook 명령이 인터프리터(`/usr/bin/python3`)와 스크립트를 절대경로로 못박는다. 어느 cwd 에서도 같은 결과 |
| 슬래시 커맨드 | `/` 로 시작하는 프롬프트는 질문이 아니므로 건너뛴다 |
| 중복 주입 | 프롬프트에 이미 `<llmwiki-context` 가 있으면(v=3 포함) 건너뛴다 |

## 설치

설치·검증·제거는 [`scripts/install.sh`](../scripts/install.sh) 하나로 한다 —
clone 경로 독립, 의존성/클라이언트 감지, dry-run, 멱등 설치, 백업, 롤백,
Codex hook 신뢰 안내까지 그 안에 있다. 옵션과 지원 범위는
[`docs/install.md`](install.md) 를 보라. 아래는 그 스크립트가 이 Mac 에서 실제로
바꾼 것의 기록이다.

## 전역 설정 변경 내역

모든 변경은 자동 백업을 남겼다.

| 파일 | 변경 | 백업 |
|---|---|---|
| `~/.codex/hooks.json` | `hooks.UserPromptSubmit` 배열에 그룹 **1개 추가** (기존 Orca 그룹은 index 0 그대로) | `~/.codex/hooks.json.llmwiki-bak` |
| `~/.claude/settings.json` | 같음 | `~/.claude/settings.json.llmwiki-bak` |
| `~/.codex/AGENTS.md` | 빈 파일에 `<!-- llmwiki-context:start/end -->` 섹션 추가 | `~/.codex/AGENTS.md.llmwiki-bak` |
| `~/.claude/CLAUDE.md` | 새로 생성 (기존 파일 없었음) | — |
| `~/.codex/config.toml` | `codex mcp add` 로 `[mcp_servers.llmwiki]` 추가 | `~/.codex/config.toml.llmwiki-bak` |
| `~/.claude.json` | `claude mcp add --scope user` 로 `mcpServers.llmwiki` 추가 | `~/.claude.json.llmwiki-bak` |
| qmd collection | (2026-09-02 이전) `llmwiki_json` collection 을 만들었다. **지금은 쓰지 않는다** — 남아 있으면 `qmd collection remove llmwiki_json` 으로 지워도 된다 | — |

`hooks.json` / `settings.json` 은 `UserPromptSubmit` 배열만 늘었고 다른 hook event 와
`hooks` 밖 설정은 바이트 단위로 동일함을 확인했다. `config.toml` 은 `codex mcp add` 가
파일 전체를 자기 형식으로 다시 쓴다 — 의미는 동일하고, `node_repl` 의 `args = []`(기본값)
가 사라지고 `startup_timeout_sec` 이 `120` → `120.0` 이 된 것이 전부다.

### 롤백

```bash
# hook + 전역 지침만 되돌린다 (설치한 그룹/섹션만 제거, 나머지는 보존)
python3 $REPO/scripts/llmwiki_context.py install --remove

# 또는 백업 파일로 통째 복원
cp ~/.codex/hooks.json.llmwiki-bak      ~/.codex/hooks.json
cp ~/.claude/settings.json.llmwiki-bak  ~/.claude/settings.json
cp ~/.codex/AGENTS.md.llmwiki-bak       ~/.codex/AGENTS.md
rm -f ~/.claude/CLAUDE.md

# MCP 등록 해제
codex mcp remove llmwiki
claude mcp remove --scope user llmwiki
```

일시 정지만 하고 싶으면 설정을 건드리지 말고 환경변수를 쓴다.

```bash
export LLMWIKI_CONTEXT_DISABLE=1     # hook 이 즉시 아무것도 하지 않는다
```

## MCP (읽기 전용)

두 클라이언트에 `llmwiki` stdio MCP 서버를 전역 등록했다. 노출 도구는 셋 뿐이고 전부 읽기 전용이다.

- `llmwiki_search(query, limit)` — 색인으로 page 후보를 점수순으로 (색인이 없으면 정본 스캔). `mode`·`fallback` 을 함께 돌려준다
- `llmwiki_context(query, max_bytes)` — 주입용 P/B/E 부분 그래프를 그대로
- `llmwiki_get(selector, blocks, fields, mode)` — **필요한 object 단위** 조회. 색인은 주소 힌트로만 쓰고 최종 object 는 정본 파일 하나에서 읽는다

`llmwiki_get` 의 기본은 page 통짜가 아니라 block 목차(`outline`)다.

| 부르는 법 | 돌아오는 것 |
|---|---|
| `llmwiki_get(selector="<slug>")` | 메타 + block 목록(`id`·`kind`·미리보기 120자). 실측 40KB → 8KB |
| `llmwiki_get(selector, blocks=["<block id>"])` | 그 block object 만 (정본 그대로, 1~2KB) |
| `llmwiki_get(selector="<slug>#<block id>")` | 위와 같다 — 주소 하나로 쓰는 형태 |
| `llmwiki_get(selector="<block id>")` | block id 만으로 소속 page 를 찾아 그 block |
| `llmwiki_get(selector, fields=["summary"])` | 머리말에서 그 필드만 |
| `llmwiki_get(selector, mode="page")` | page 전체 JSON — **명시할 때만**, 보통은 필요 없다 |

`selector` 는 slug · `page:slug` · 제목 아무거나 받는다. `blocks` 는 hook 이 준 full id 도,
꼬리만 딴 축약형(`res`)도 받는다. 없는 block 은 예외가 아니라 `missing` 으로 돌아온다.
같은 일을 CLI 로도 한다 — `llmwiki_context.py get <slug> [--block <id>] [--mode page]`.

MCP 는 보조 수단이고, **자동 주입의 필수 경로는 hook** 이다. MCP 가 꺼져 있어도 주입은 동작한다.

## 직접 써 보기

```bash
S=$REPO/scripts/llmwiki_context.py

python3 $S doctor                          # 해석된 경로·예산·정본 페이지 수·색인 신선도
python3 $S search "폐기 ICD 코드"           # 후보 page 점수순 (JSON, mode=index|memory|scan)
python3 $S context "폐기 ICD 코드"          # 실제 주입될 본문 (P/B/E)
python3 $S context "질문" --json           # 본문 + 바이트/토큰/projection/signals
python3 $S context "질문" --no-index       # 디스크 색인을 끄고 정본에서 메모리 색인을 만든다 (같은 P/B/E)
python3 $S context "질문" --scan           # 디스크·메모리 색인 모두 끄고 최후 스캔 경로(옛 형식)로
python3 $S context "질문" --silence 770    # 무주입 문턱을 이 호출에만 켠다
python3 $S get 폐기-icd-코드                # block 목차 (page 통짜가 아니다)
python3 $S get 폐기-icd-코드 --block res    # 그 block object 만
python3 $S get 폐기-icd-코드 --mode page    # page 전체 — 필요할 때만
echo '{"prompt":"질문"}' | python3 $S hook  # hook 출력 그대로
python3 $S install --dry-run               # 설치될 hook 명령 미리보기
```

## 환경변수

| 변수 | 기본 | 뜻 |
|---|---|---|
| `LLMWIKI_ROOT` | 스크립트 위치 기준 저장소 | 정본 루트 override |
| `LLMWIKI_CONTEXT_DISABLE` | `0` | `1` 이면 hook 무동작 |
| `LLMWIKI_CONTEXT_MAX_BYTES` | `6000` | 주입 본문 UTF-8 바이트 상한 |
| `LLMWIKI_CONTEXT_MAX_TOKENS` | `2000` | 주입 본문 추정 토큰 상한 |
| `LLMWIKI_CONTEXT_INDEX` | `1` | `0` 이면 디스크 색인을 쓰지 않고 정본에서 메모리 색인을 만든다 (`mode=memory`) |
| `LLMWIKI_CONTEXT_MEMORY` | `1` | `0` 이면 디스크 색인도 메모리 색인도 쓰지 않고 최후 스캔 경로로 간다 (`mode=scan`, `fallback=disabled;memory-disabled`, 옛 형식) |
| `LLMWIKI_CONTEXT_SILENCE` | `0` (끔) | 무주입 문턱 `raw_top × coverage`. 위키마다 재보정 |
| `LLMWIKI_CONTEXT_HINT` | `0` (끔) | 주소만(A) 구간 문턱. SILENCE 와 함께 쓴다 |
| `LLMWIKI_CONTEXT_TIMEOUT` | `6` | hook 워치독 초 |
| `LLMWIKI_CONTEXT_LOG` | 없음 | 주입 통계 JSONL 경로 (질문 원문은 기록하지 않는다) |

## Codex hook 신뢰 (완료)

Codex 0.148.0 은 새로 추가되거나 바뀐 hook 을 **신뢰하기 전까지 그 출력을 무시한다**.
TUI 첫 실행에서 `Hooks need review — 1 hook is new or changed` 프롬프트가 뜨고,
신뢰 전에는 `codex exec` 에서도 `additionalContext` 가 대화에 들어가지 않는다.

이 Mac 에서는 review 화면에서 대기 중인 hook 이 정확히 1개이고 그것이
`/usr/bin/python3 …/llmwiki_context.py hook` 임을 확인한 뒤 신뢰를 부여했다.
현재 `UserPromptSubmit` 은 `Installed 2 / Active 2` 이고 대기 중인 review 는 없다.

**스크립트 경로나 hook 명령을 바꾸면 신뢰가 무효화된다.** 그때는 Codex 를 한 번
띄워 review 프롬프트에서 다시 신뢰를 주면 된다(`t` = trust). 자동화에서 한 번만
우회하려면 `codex exec --dangerously-bypass-hook-trust` 를 쓸 수 있지만, 상시 사용은 권하지 않는다.

Claude Code 2.1.237 에는 이런 신뢰 게이트가 없어 설치 즉시 동작한다.

## 실측 검증

로컬 실제 클라이언트에서 확인한 결과다.

| 질문 | 클라이언트 | 결과 |
|---|---|---|
| "폐기 ICD 코드는 전체 몇 건인가?" | Codex 0.148.0 | 6,713 tokens (기본 대비 +약 1,100) · `page:폐기-icd-코드 — 전체 324건` |
| "파스타 삶는 법" | Codex 0.148.0 | 5,639 tokens (기본선) · 무주입, 정상 답변 |
| "폐기 ICD 코드는 전체 몇 건인가?" | Claude Code 2.1.237 | 주입됨 · `block:폐기-icd-코드:cf60869ae505ae70:1` 근거로 324건 |

## 클라이언트별 hook 스키마 차이

로컬 `claude 2.1.237` 과 `codex-cli 0.148.0` 의 `UserPromptSubmit` 계약을 실제 바이너리에
박힌 스키마로 확인했다.

| | Claude Code 2.1.237 | Codex 0.148.0 |
|---|---|---|
| 설정 파일 | `~/.claude/settings.json` → `hooks.UserPromptSubmit[]` | `~/.codex/hooks.json` → `hooks.UserPromptSubmit[]` |
| 그룹 모양 | `{hooks:[{type,command,timeout}]}` (이 이벤트는 matcher 없음) | 동일 |
| stdin 필수 필드 | `session_id`, `transcript_path`, `cwd`, `permission_mode`, `hook_event_name`, `prompt` | 위 + `model`, `turn_id` (`transcript_path` 는 nullable) |
| stdout | `hookSpecificOutput.additionalContext` 또는 평문 | `hookSpecificOutput.additionalContext` |
| stdout 제약 | 여유로움 | `additionalProperties: false` — `continue`/`decision`/`hookSpecificOutput`/`reason`/`stopReason`/`suppressOutput`/`systemMessage` 만 허용 |

두 스키마의 **교집합**만 쓴다. 출력은 항상
`{"hookSpecificOutput":{"hookEventName":"UserPromptSubmit","additionalContext":…},"suppressOutput":true}`
이고, 이 모양은 양쪽 모두에서 유효하다. `turn_id` 유무로 어느 클라이언트인지만 구분해
통계 로그에 남긴다(동작은 동일).

## 테스트

```bash
python3 -m unittest discover -s tests -t .   # 전체
python3 -m unittest tests.test_context       # 주입 CLI 만
```

`tests/test_context.py` 가 다루는 것(in-process 검사는 최후 스캔 경로 `use_memory=False` 고정):
한국어 조사 처리, 영어 질의, 무관 질문 무주입, 상충 노출, 자격증명 마스킹, 바이트/토큰 상한,
malformed stdin 10종, 다른 cwd, 두 클라이언트 페이로드, Codex 출력 스키마 키 제약, MCP
JSON-RPC, hook 설치의 멱등성·비파괴성·되돌리기. `tests/test_index.py` 가 다루는 것(색인·메모리
경로): build 결정성과 `search_root`, 색인 없음·revision 불일치·더 새 정본·깨진 색인 파일에서
메모리 색인 폴백(형식 v3, index 와 바이트 동일, 워치독 안), 메모리 build 실패·`LLMWIKI_CONTEXT_MEMORY=0`
에서 최후 스캔, `search` 의 같은 세 경로, 바뀐 hit page 재독, 지워진 page 제외, 행 단위 선택의
320자 상한, heading 제외, 낡은 page 본문 생략, fork/cycle, 바이트·토큰 상한, 무주입 옵션,
redact(색인 파일까지), 워치독, installer `verify` 의 probe(본문 block 에서 질문, 검색·주입 별도 검사).

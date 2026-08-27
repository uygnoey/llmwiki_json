# 자동 컨텍스트 주입 (Codex · Claude Code)

이 Mac 의 Codex 와 Claude Code 가 **모든 질문을 처리하기 전에** `llmwiki_json` 정본에서
관련 근거를 찾아 프롬프트에 붙인다. 구현은 `scripts/llmwiki_context.py` 하나이고,
두 클라이언트 모두 `UserPromptSubmit` hook 으로 같은 실행 파일을 부른다.

## 동작

```
사용자 질문
  └─ UserPromptSubmit hook  (Codex / Claude Code 공통)
       └─ /usr/bin/python3 scripts/llmwiki_context.py hook
            1. 질문 토큰화 (한국어 조사·어미 흡수, 불용어 제거)
            2. wiki/**/*.json 정본 212개를 직접 읽어 idf 가중 점수 계산
            3. 점수가 약할 때만 qmd 로 후보 slug 확장 (본문은 가져오지 않는다)
            4. 문턱 미달이면 아무것도 주입하지 않는다
            5. 통과하면 page/block/field projection 만 골라 렌더
            └─ {"hookSpecificOutput":{"hookEventName":"UserPromptSubmit",
                                       "additionalContext":"<llmwiki-context>…"}}
```

### 정본 우선

검색과 주입 본문 모두 `wiki/**/*.json` 정본에서만 나온다. 212개 shard 전체를 매번
읽어도 22ms 라 파생 index 를 캐시로 쓸 이유가 없고, 덕분에 `index/` 가 stale 해도
주입된 근거는 절대 낡지 않는다. qmd 는 **후보 slug 목록만** 돌려주고, 그 slug 의
본문은 다시 정본 JSON 에서 읽는다.

### 주입 내용

page 마다 다음이 실린다.

- `page id` / 제목 / `type` / `updated` / `projects` / `tags` / 점수와 경로(`canonical` · `qmd`)
- 로컬 파일 근거: `wiki/...json` 상대경로, `raw_ref` 가 있으면 원본 경로
- `summary`, `sources` (source ref 목록)
- 질문과 맞물린 block 만: `block id`, `kind`, 본문(320자 클립), conflict 면 `resolution` 상태
- 미판정 상충이 있으면 `⚠️ 미판정 상충 N건 — 양쪽을 병기해서 답하라`

`heading` block 은 제목과 중복이라 예산을 쓰지 않는다. 페이지 전체(`history`, `links`,
`block_order`)는 절대 싣지 않는다.

머리말은 이어서 조회하는 법도 함께 준다 — `llmwiki_get(selector, blocks=[...])` 로 그
block object 만 가져오고 page 전체 JSON 은 읽지 말라는 지시다.

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

### 주소만 주는 경우 (hint)

본문 문턱은 못 넘었지만 짚이는 page 는 있는 질문에는 **본문 대신 주소만** 준다.

- 조건: 최고 점수 `>= 2.5` · 커버리지 `>= 0.3` · 겹친 토큰이 숫자만은 아닐 것
- 내용: 상위 3개 page 의 `page id` / `type` / `updated` / 제목 / 요약 110자 + 가져오는 법
- 크기: 1.5KB 안쪽. block 본문은 한 글자도 싣지 않는다
- `reason` 은 `hint:canonical` 처럼 원래 경로를 뒤에 남긴다

침묵하면 클라이언트는 위키가 있다는 사실조차 모르고 지나간다. 주소만 주면 필요한 쪽이
`llmwiki_get` 으로 정확히 집어 올 수 있다. 무관한 질문은 여전히 완전 무주입이다.

### 안전 장치

| 요구 | 구현 |
|---|---|
| 토큰/바이트 상한 | `--max-bytes` 기본 6000, `--max-tokens` 기본 2000. 둘 다 초과 불가(렌더 후 재검증하며 page 를 덜어낸다). 잘린 경우 "예산 초과로 N개 page 생략"을 명시한다 |
| 낮은 관련도 무주입 | 최고 점수 `< 6.0` 이거나 질문 토큰 커버리지 `< 0.34` 면 본문을 싣지 않는다. 그중 점수 `>= 2.5` · 커버리지 `>= 0.3` 인 것만 주소 3줄(hint)로 나가고, 나머지는 빈 출력 |
| fail-open | stdin 파싱 실패·정본 손상·예외·타임아웃 전부 stdout 없이 `exit 0`. 워치독 6초 |
| 자격증명 미출력 | 출력 직전 `api_key`/`token`/`password`/`secret` 패턴을 `(접속 정보 생략)` 으로 치환 |
| 전역 동작 | hook 명령이 인터프리터(`/usr/bin/python3`)와 스크립트를 절대경로로 못박는다. 어느 cwd 에서도 같은 결과 |
| 슬래시 커맨드 | `/` 로 시작하는 프롬프트는 질문이 아니므로 건너뛴다 |
| 중복 주입 | 프롬프트에 이미 `<llmwiki-context>` 가 있으면 건너뛴다 |

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
| qmd collection | 낡은 `llmwiki`(→ `$REPO/wiki`) 제거, `llmwiki_json`(→ `$REPO/index/markdown`) 추가 | 아래 롤백 참고 |

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

# qmd collection 원복
qmd collection remove llmwiki_json
qmd collection add $REPO/wiki --name llmwiki
```

일시 정지만 하고 싶으면 설정을 건드리지 말고 환경변수를 쓴다.

```bash
export LLMWIKI_CONTEXT_DISABLE=1     # hook 이 즉시 아무것도 하지 않는다
```

## qmd collection

`llmwiki` collection 은 예전 저장소 `$REPO/wiki` 의 markdown 을 가리키고
있었다(낡음). 현재 저장소의 markdown 은 `index/markdown/` 이고 파생물이므로,
collection 을 `llmwiki_json` 으로 새로 만들고 낡은 것을 제거했다.

```bash
python3 scripts/llmwiki.py export-md   # index/markdown 재생성 (212 page)
qmd update -c llmwiki_json             # 재색인
```

`index/markdown/` 은 gitignore 대상이라 정본을 바꾼 뒤에는 위 두 명령을 다시 돌려야
qmd 후보 탐색이 최신 상태가 된다. **정본 검색은 qmd 와 무관하게 항상 최신이다.**

## MCP (읽기 전용)

두 클라이언트에 `llmwiki` stdio MCP 서버를 전역 등록했다. 노출 도구는 셋 뿐이고 전부 읽기 전용이다.

- `llmwiki_search(query, limit)` — 정본 page 후보를 점수순으로
- `llmwiki_context(query, max_bytes)` — 주입용 근거 블록을 그대로
- `llmwiki_get(selector, blocks, fields, mode)` — **필요한 object 단위** 조회

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

python3 $S doctor                          # 해석된 경로·예산·정본 페이지 수
python3 $S search "폐기 ICD 코드"           # 후보 page 점수순 (JSON)
python3 $S context "폐기 ICD 코드"          # 실제 주입될 본문
python3 $S context "질문" --json           # 본문 + 바이트/토큰/projection
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
| `LLMWIKI_CONTEXT_QMD_COLLECTION` | `llmwiki_json` | qmd collection 이름 |
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

`tests/test_context.py` 가 다루는 것: 한국어 조사 처리, 영어 질의, 무관 질문 무주입,
상충 노출, 자격증명 마스킹, 바이트/토큰 상한, malformed stdin 10종, 다른 cwd,
두 클라이언트 페이로드, Codex 출력 스키마 키 제약, MCP JSON-RPC, hook 설치의
멱등성·비파괴성·되돌리기.

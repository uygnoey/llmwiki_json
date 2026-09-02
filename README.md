# llmwiki_json

AI가 유지보수하는 JSON-canonical 개인 지식 위키입니다. JSON이 정본이며 index/map/search/graph와 화면용 Markdown/HTML은 모두 파생물입니다.

Karpathy의 LLM Wiki 패턴을 따릅니다. 질문할 때마다 원본을 다시 조립하는 RAG 앱이 아니라, LLM이 지식을 한 번 통합해 두고 계속 갱신하는 누적형 위키입니다.

이 저장소에 담긴 `wiki/` 내용은 구조를 보여주기 위한 **데모 데이터**입니다. `raw/demo/`의 원본을 그대로 ingest해 만든 것이라 지우고 자기 소스로 다시 시작하면 됩니다.

## 핵심 구조

```text
wiki/sources/          소스별 JSON 요약 페이지
wiki/entities/         엔티티 JSON 페이지
wiki/concepts/         개념 JSON 페이지
wiki/syntheses/        종합·비교·분석 JSON 페이지
wiki/projects/         프로젝트 허브 JSON 페이지
wiki/log.jsonl         append-only 작업 로그
raw/                   불변 원본 소스 큐 + 기존 live source symlink
index/                 재생성 가능한 catalog/map/graph/routes/stats/revision + search.sqlite(검색 색인)
tools/schema/          JSON Schema
tools/config/          프로젝트 그룹·색상 설정
scripts/llmwiki.py     ingest/build/query/get/render/outline/lint/log CLI
scripts/llmwiki_index.py  검색 색인(`index/search.sqlite`) build·검색·부분 그래프 렌더 (표준 라이브러리만)
scripts/llmwiki_context.py  Codex·Claude 자동 컨텍스트 주입 hook/MCP CLI
scripts/llmwiki_routine.py  주기 ingest 루틴 (스케줄러 등록·git 연동)
scripts/install.sh     자동 주입 설치·검증·제거 (POSIX, macOS/Linux)
scripts/install.ps1    같은 것의 Windows 판 (PowerShell 5.1+)
tests/                 백엔드 unittest 스위트
tests/fixtures/pages/  기능 검증용 데모 데이터 (실제 위키 문서 아님)
viewer/                Obsidian-style 2D/3D Graph View
```

Graph View는 이 웹앱 자체 기능이며 Obsidian 설치나 vault가 필요하지 않습니다. 프로젝트·타입·태그 색상 모드를 전환할 수 있고 검색, 그룹 필터, 고아·상충 필터, 1-hop 강조, 노드 상세(JSON/MD/렌더 보기)를 제공합니다.

좌표는 어디에도 저장되지 않습니다. 노드는 군집 바깥 임의의 지점에서 생겨나 반발력·링크 장력·구형 containment만으로 안쪽으로 끌려 들어오고, **두 노드가 실제로 충돌한 뒤에야** 둘 사이에 선이 그려집니다. 아직 만나지 못한 연결은 그려지지 않습니다. 군집은 그래프가 배선을 마칠 때까지 계속 저어지다가 그제야 가라앉습니다. 우측 상단 재생 버튼으로 처음부터 다시 볼 수 있습니다.

개발 서버는 `wiki/**/*.json` 변경을 감시해 index/map/graph를 자동 재생성하고 열린 화면도 즉시 갱신합니다.

## 시작

```bash
python3 scripts/llmwiki.py validate
python3 scripts/llmwiki.py build
cd viewer
bun install
bun run dev
```

`index/`와 `viewer/public/data/`는 커밋하지 않는 파생물이라 clone 직후에는 비어 있습니다. 위 `build`가 채우고, 개발 서버는 뜰 때와 `wiki/**/*.json`이 바뀔 때마다 다시 만듭니다 — 이때는 바뀐 파일 경로를 `build --changed`로 넘겨 색인의 그 page 행만 갈아 끼웁니다(증분). 증분 build는 `index/search.work.sqlite`(작업 DB, WAL)를 고친 뒤 빈 파일에 같은 DDL·PK 순으로 다시 써 `index/search.sqlite`를 발행하므로 cold build와 헤더까지 바이트가 같고, 훅은 발행본만 읽어 갱신 중에도 막히지 않습니다. 힌트는 힌트일 뿐이라 지난 build가 파일마다 기록한 (mtime, size)와 다른 파일은 sha로 확인하고, 힌트 밖의 변경이 보이면 스스로 전량으로 떨어집니다.

viewer가 CLI를 부를 때 쓰는 Python은 `LLMWIKI_PYTHON` → `python3` → `python` → `py` 순으로 찾습니다. `python3`라는 이름이 없는 기계(Windows, uv로 받은 Python만 있는 기계)에서는 `LLMWIKI_PYTHON`에 실행 파일 절대경로를 주거나 `scripts/install.sh`(Windows는 `scripts\install.ps1`)로 마련하십시오.

Docker로 정적 빌드를 실행하려면 저장소 루트에서 다음 명령을 사용합니다. 빌드 단계에서 정본 `wiki/**/*.json`으로 파생 데이터를 다시 만든 뒤 Nginx가 `http://localhost:4173`에서 제공합니다.

```bash
docker compose -f viewer/compose.yml up -d --build
```

이 컴퓨터에는 `com.oprimed.llmwiki-json-viewer` LaunchAgent를 설치해 로그인 시 Docker Desktop과 프론트 컨테이너가 자동으로 시작되도록 운영합니다. 컨테이너에는 `restart: unless-stopped`도 적용됩니다.

실데이터를 넣은 뒤에는:

```bash
python3 scripts/llmwiki.py ingest raw/example.md --type source --project beta
python3 scripts/llmwiki.py build
python3 scripts/llmwiki.py lint
python3 scripts/llmwiki.py query "검색어"
```

## CLI

모든 명령은 `--root`(또는 `LLMWIKI_ROOT`)로 대상 저장소를 바꿀 수 있고, `build`/`validate`/`lint`/`query`/`get`/`render`/`outline`은 `--fixtures`로 데모 트리를 읽습니다.

| 명령 | 하는 일 |
| --- | --- |
| `ingest <파일>` | raw 소스 1건을 정본 page로 기록. `--type` `--project` `--summary` `--update` `--dry-run` |
| `build` | 정본 → `index/*.json` + `index/search.sqlite` + `viewer/public/data/**` 결정적 재생성. `--changed <파일…>`(바뀐 파일 힌트 — 그 page 만 색인에 갈아 끼우는 증분, 힌트 밖 변경이 보이면 전량), `--full`, `--heading-paths`(H6, 기본 꺼짐) |
| `validate` | 스키마·구조 검증 (오류만) |
| `lint` | 오류 + 경고: 미존재 링크, 미판정 상충, 고아, 빈 summary, stale index. `--json` |
| `query "질의"` | 검색 색인으로 page 순위 (신선한 `index/search.sqlite`, 없거나 낡으면 정본에서 메모리 색인). `--limit` |
| `get <주소>` | page·block projection. `--block` `--field` `--pointer` |
| `render <주소>` | `--format md\|html\|json`, `--exact`, `--section <heading block id>` |
| `outline <주소>` | heading block 목록 — 페이지 통독 없이 섹션만 고를 때 |
| `log` | `--action/--page/--note`로 append, 인자 없으면 `--show N`으로 조회 |

### 소스 frontmatter

`ingest`가 읽는 YAML 표기입니다. flow 리스트(`[a, b]`)와 블록 시퀀스(줄바꿈 + `- a`) 둘 다 같은 결과가 되고, 값의 따옴표는 벗겨집니다. 하나짜리 값은 리스트로 승격하고 중복은 제거합니다.

```yaml
---
type: source            # source | entity | concept | synthesis | project | home | index | log
created: 2026-08-19     # 없으면 오늘
updated: 2026-08-19
projects:               # 그래프 프로젝트 그룹. 2개 이상이면 '다중 프로젝트'
  - OSE
  - 공통
tags: [주간보고, 검색]   # 태그 색상·범례 기준
sources:                # 근거. page:/source:/raw:/user:YYYY-MM-DD
  - page:질환-통합검색
  - user:2026-08-19
supersedes: [옛-페이지]  # 뒤집은 주장을 지우지 않고 관계로 남긴다
related: [beta-search]
raw: raw/2026-08/원본.md  # 없으면 ingest 한 파일 경로
---
```

`sources`·`supersedes`·`related`는 본문 `[[위키링크]]`와 같은 자격의 그래프 선이 됩니다 — 위키 밖 근거(`user:`, `raw:`)만 선이 되지 않습니다. `groups.json`이 모르는 프로젝트 값은 ingest가 새 그룹으로 등록하고, 등록 전이라도 `build`가 같은 키·같은 색으로 그룹을 만들어 줍니다.

`[[링크]]`는 slug 정확 일치만 찾지 않습니다. 대소문자, 공백·`_`와 `-`, `page:` 접두, 그리고 페이지 **제목**까지 같은 곳으로 해석합니다 — `[[Alpha Platform]]`, `[[alpha_platform]]`, `[[page:alpha-platform]]`은 모두 `alpha-platform`입니다.

### 주소 지정

page와 block은 위치가 아니라 영속 ID로 지정합니다.

```bash
llmwiki.py get sample-topic                          # page 전체
llmwiki.py get sample-topic --field title            # 필드 하나
llmwiki.py get 'sample-topic#block:sample-topic:ab12:1'  # page 안의 block
llmwiki.py get block:sample-topic:ab12:1             # block 단독 (page 자동 탐색)
llmwiki.py get sample-topic --pointer /blocks/block:sample-topic:ab12:1/data/text
llmwiki.py outline sample-topic                      # 섹션 목록
llmwiki.py render sample-topic --section block:sample-topic:ab12:1   # 그 섹션만
```

block id는 `block:<slug>:<내용 지문>:<중복 순번>` 형태라, 페이지 다른 곳이 바뀌어도 유지됩니다.

## 자동 컨텍스트 주입

`scripts/llmwiki_context.py`는 Codex와 Claude Code의 `UserPromptSubmit` hook으로 붙어, 질문마다 근거를 찾아 `<llmwiki-context v=3>` 블록(P/B/E 부분 그래프)으로 주입합니다. 검색은 `build`가 정본에서 굽는 `index/search.sqlite`로 하고, 색인이 없거나 낡으면 정본 `wiki/**/*.json`에서 메모리 색인을 만들어 같은 형식으로 주입하며, 그 build마저 실패하면 옛 스캔 경로로 떨어집니다(fail-open, `docs/context-injection.md`의 세 경로 표). 주입되는 것은 질문과 맞물린 block들이지 page 통짜가 아니고, 긴 block은 질문과 겹치는 행만 320자 안에서 싣습니다. supersedes로 대체된 낡은 page는 `sup→새 page` 한 줄만 나옵니다. 정본과 한 토큰도 겹치지 않으면 아무것도 주입하지 않고, 무주입 문턱은 옵션(`LLMWIKI_CONTEXT_SILENCE`)입니다. 어떤 오류에서도 질문을 막지 않습니다.

설치는 스크립트 하나로 끝납니다. clone 경로는 어디여도 되고, 스크립트가 자기 위치에서 repo root를 찾습니다.

```bash
./scripts/install.sh --dry-run   # 무엇이 바뀔지 먼저 본다 (아무것도 쓰지 않음)
./scripts/install.sh             # 설치 (여러 번 돌려도 결과가 같음)
./scripts/install.sh verify      # 상태 점검
./scripts/install.sh uninstall   # 넣은 것만 정확히 제거
```

Windows에서는 같은 명령·같은 옵션의 PowerShell 판을 씁니다 (WSL 안이라면 위쪽 `install.sh`가 맞습니다):

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -DryRun
powershell -ExecutionPolicy Bypass -File scripts\install.ps1
```

기존 hook 그룹·설정·지침 본문은 읽은 그대로 되돌려 쓰고, 이미 등록된 MCP 서버는 건드리지 않습니다. 설치 직전 상태는 `<파일>.llmwiki-bak`으로 남습니다.

필요한 도구는 사용자 영역에 받아 옵니다 — Python 3.9+가 없으면 uv로, viewer가 쓰는 Bun이 없으면 공식 설치기로 정확히 1.3.14를 `~/.bun`에. 검색 색인은 별도 도구 없이 `build`가 만듭니다 (qmd는 더 이상 쓰지 않습니다). shell profile과 시스템 경로는 건드리지 않고, 이미 있는 것은 갈아치우지 않습니다. viewer를 쓰지 않으면 `--no-bun`으로 Bun 단계만 건너뛰고, `--no-bootstrap`으로 네트워크 설치를 전면 금지할 수 있으며 `--dry-run`은 네트워크조차 타지 않습니다.

조회만 직접 하고 싶을 때:

```bash
python3 scripts/llmwiki_context.py search "폐기 ICD 코드"   # 후보 page 점수순
python3 scripts/llmwiki_context.py context "폐기 ICD 코드"  # 실제 주입될 본문
python3 scripts/llmwiki_context.py get 폐기-icd-코드        # block 목차
python3 scripts/llmwiki_context.py get 폐기-icd-코드 --block <id>  # 그 block만
```

조회는 기본이 목차와 block 단위입니다. page 전체 JSON은 `--mode page`(또는 `llmwiki.py get`)를 명시할 때만 나옵니다.

## 늘 붙는 근거 (선택)

질문마다 앞에 고정으로 붙일 page를 `tools/config/context.json`에 적을 수 있습니다. 매번 알아야 하는 것 — 작업 규칙, 용어 기준 — 을 여기에 둡니다.

```json
{ "always": ["ai-작업-규칙"], "always_max_bytes": 800 }
```

검색 점수와 무관하게 늘 나가고, 예산은 검색 몫과 따로 잡아 근거를 밀어내지 않습니다(전체의 절반을 넘지 못합니다). 고정한 page는 검색 결과에서 중복으로 다시 싣지 않습니다. 설정이 없으면 아무것도 고정하지 않습니다.

## 주기 ingest 루틴 (선택)

`raw/`에 새 소스가 들어오면 에이전트가 알아서 위키에 넣도록 걸어 둘 수 있습니다. 설치 중에 물어보고, 답하지 않으면 걸지 않습니다.

```bash
./scripts/install.sh --ingest-routine claude      # 또는 codex, none
./scripts/install.sh --routine-interval 1800      # 주기(초, 기본 3600)
python3 scripts/llmwiki_routine.py status         # 등록 상태와 마지막 실행
python3 scripts/llmwiki_routine.py run --dry-run  # 무엇을 할지만 본다
```

한 번 돌 때의 순서는 **git pull → 미처리 확인 → ingest → build·validate → 커밋 → push**입니다. 미처리 소스가 없으면 에이전트를 아예 부르지 않고, 미처리 목록이 지난번과 같으면(에이전트가 이미 보고 판단한 것이면) 건너뜁니다. `raw/.llmwikiignore`로 소스가 아닌 파일을 제외할 수 있습니다. 워킹트리가 더럽거나 히스토리가 갈라졌으면 pull 단계에서 멈추고 아무것도 커밋하지 않습니다. 스케줄러는 OS에 맞춰 launchd·cron·schtasks를 씁니다.

## 개인 저장소로 밀어 올리기 (선택)

이 clone은 **코드 갱신을 받는 자리**로 두고, 자기 위키는 개인 private 저장소로 밀어 올릴 수 있습니다. `origin`은 건드리지 않고 remote 하나만 더 붙입니다.

```bash
./scripts/install.sh --private-remote git@github.com:me/my-wiki.git   # 이미 만든 저장소
./scripts/install.sh --gh-create my-wiki                              # gh 로 새로 만든다
./scripts/install.sh update                                           # origin 에서 코드 갱신
git push private HEAD:main                                            # 위키를 개인 저장소로
```

이름이 같은 remote가 이미 있으면 덮어쓰지 않고 그대로 둡니다. 루틴을 걸어 두었다면 커밋과 push까지 루틴이 합니다.

설치 옵션·지원 범위·Codex hook 신뢰 절차는 [`docs/install.md`](docs/install.md), 동작 원리와 hook 스키마 차이는 [`docs/context-injection.md`](docs/context-injection.md)에 있습니다.

## 테스트

```bash
cd viewer && bun run test                  # unittest + tsc --noEmit
cd ..
python3 -m unittest discover -s tests -v   # 백엔드만
```

테스트는 임시 디렉터리에 workspace를 만들어 돌기 때문에 실제 `wiki/`와 `raw/`를 건드리지 않습니다. 시계는 `LLMWIKI_NOW`로 고정합니다.

```bash
python3 tools/parity/parity.py build     # build 산출물의 바이트 결정성
python3 tools/parity/parity.py corpus    # Python↔Bun 원시 의미론 대조
```

`tools/parity/`는 정본 구현이 결정적인지, 그리고 다른 런타임으로 옮기면 어디가 갈라지는지를 숫자로 답합니다. 자세한 것은 [docs/parity.md](docs/parity.md)에 있습니다.

## 불변식

- `raw/`는 읽기 전용이다. ingest는 원본 해시를 전후로 대조해 변형되지 않았음을 확인한다.
- `wiki/`의 JSON 페이지만 정본이다. Markdown/HTML 화면과 모든 index/map/graph는 JSON에서 만든 파생물이다.
- 모든 페이지·블록은 위치가 아닌 영속 ID로 주소 지정한다.
- 원문 ingest 시 `source_snapshot`을 보존해 `render --exact`로 exact Markdown round-trip을 지원한다.
- `build`는 결정적이다 — 같은 입력이면 두 번 돌려도 바이트 단위로 같은 산출물이 나오고, 산출물에 담기는 경로는 저장소 상대 경로다. 증분 build(`--changed`)도 같은 정본의 cold build와 바이트가 같다. `python3 tools/parity/parity.py build`가 임시 저장소에서 둘 다 실제 산출물로 확인한다([docs/parity.md](docs/parity.md)).
- index/map/graph/viewer public data와 `index/search.sqlite`는 언제든 정본에서 재생성할 수 있다.
- 자격증명·접속정보·API 키·토큰·연결 문자열을 기록하지 않는다. ingest가 값이 붙은 형태를 발견하면 거부한다.

## 라이선스

MIT. `LICENSE` 참고.

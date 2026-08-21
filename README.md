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
index/                 재생성 가능한 catalog/map/search/graph/routes/stats
index/markdown/        qmd 인덱싱용 렌더 Markdown (선택, 파생물)
tools/schema/          JSON Schema
tools/config/          프로젝트 그룹·색상 설정
scripts/llmwiki.py     ingest/build/query/get/render/outline/export-md/lint/log CLI
scripts/llmwiki_context.py  Codex·Claude 자동 컨텍스트 주입 hook/MCP CLI
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

모든 명령은 `--root`(또는 `LLMWIKI_ROOT`)로 대상 저장소를 바꿀 수 있고, `build`/`validate`/`lint`/`query`/`get`/`render`/`outline`/`export-md`는 `--fixtures`로 데모 트리를 읽습니다.

| 명령 | 하는 일 |
| --- | --- |
| `ingest <파일>` | raw 소스 1건을 정본 page로 기록. `--type` `--project` `--summary` `--update` `--dry-run` |
| `build` | 정본 → `index/*.json` + `viewer/public/data/**` 결정적 재생성 |
| `validate` | 스키마·구조 검증 (오류만) |
| `lint` | 오류 + 경고: 미존재 링크, 미판정 상충, 고아, 빈 summary, stale index. `--json` |
| `query "질의"` | 정본에서 직접 점수화 (제목 8 / summary 2 / 본문 1). `--limit` |
| `get <주소>` | page·block projection. `--block` `--field` `--pointer` |
| `render <주소>` | `--format md\|html\|json`, `--exact`, `--section <heading block id>` |
| `outline <주소>` | heading block 목록 — 페이지 통독 없이 섹션만 고를 때 |
| `export-md` | 렌더 Markdown + manifest 생성 (qmd 인덱싱용). `--out` |
| `log` | `--action/--page/--note`로 append, 인자 없으면 `--show N`으로 조회 |

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

`scripts/llmwiki_context.py`는 Codex와 Claude Code의 `UserPromptSubmit` hook으로 붙어, 질문마다 정본에서 근거를 찾아 `<llmwiki-context>` 블록으로 주입합니다. 검색과 주입 본문 모두 `wiki/**/*.json`에서만 나오고, qmd는 후보 slug 탐색에만 씁니다. 관련도가 낮으면 아무것도 주입하지 않고, 어떤 오류에서도 질문을 막지 않습니다.

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

기존 hook 그룹·설정·지침 본문은 읽은 그대로 되돌려 쓰고, 남의 qmd collection과 이미 등록된 MCP 서버는 건드리지 않습니다. 설치 직전 상태는 `<파일>.llmwiki-bak`으로 남습니다.

필요한 도구는 사용자 영역에 받아 옵니다 — Python 3.9+가 없으면 uv로, qmd가 없으면 Bun 1.3.14 + `@tobilu/qmd`로. shell profile과 시스템 경로는 건드리지 않고, 이미 있는 것은 갈아치우지 않습니다. `--no-bootstrap`으로 네트워크 설치를 전면 금지할 수 있고 `--dry-run`은 네트워크조차 타지 않습니다.

조회만 직접 하고 싶을 때:

```bash
python3 scripts/llmwiki_context.py search "폐기 ICD 코드"   # 후보 page 점수순
python3 scripts/llmwiki_context.py context "폐기 ICD 코드"  # 실제 주입될 본문
```

설치 옵션·지원 범위·Codex hook 신뢰 절차는 [`docs/install.md`](docs/install.md), 동작 원리와 hook 스키마 차이는 [`docs/context-injection.md`](docs/context-injection.md)에 있습니다.

### qmd 인덱싱

`scripts/install.sh`가 기본으로 처리합니다(qmd가 없으면 설치까지). 직접 하려면:

```bash
python3 scripts/llmwiki.py export-md
qmd collection add "$PWD/index/markdown" --name llmwiki_json && qmd embed
```

`index/markdown/`은 매 실행마다 통째로 다시 만드는 파생물입니다 — 직접 수정하지 마세요.

## 테스트

```bash
cd viewer && bun run test                  # unittest + tsc --noEmit
cd ..
python3 -m unittest discover -s tests -v   # 백엔드만
```

테스트는 임시 디렉터리에 workspace를 만들어 돌기 때문에 실제 `wiki/`와 `raw/`를 건드리지 않습니다. 시계는 `LLMWIKI_NOW`로 고정합니다.

## 불변식

- `raw/`는 읽기 전용이다. ingest는 원본 해시를 전후로 대조해 변형되지 않았음을 확인한다.
- `wiki/`의 JSON 페이지만 정본이다. Markdown/HTML 화면과 모든 index/map/graph는 JSON에서 만든 파생물이다.
- 모든 페이지·블록은 위치가 아닌 영속 ID로 주소 지정한다.
- 원문 ingest 시 `source_snapshot`을 보존해 `render --exact`로 exact Markdown round-trip을 지원한다.
- `build`는 결정적이다 — 같은 입력이면 두 번 돌려도 바이트 단위로 같은 산출물이 나오고, 산출물에 담기는 경로는 저장소 상대 경로다.
- index/map/graph/search/viewer public data와 `index/markdown/`은 언제든 정본에서 재생성할 수 있다.
- 자격증명·접속정보·API 키·토큰·연결 문자열을 기록하지 않는다. ingest가 값이 붙은 형태를 발견하면 거부한다.

## 라이선스

MIT. `LICENSE` 참고.

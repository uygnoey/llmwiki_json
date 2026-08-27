# 설치 — `scripts/install.sh` · `scripts/install.ps1`

이 저장소를 clone 한 뒤 한 번 실행하면, Codex 와 Claude Code 가 모든 질문 앞에서
이 위키의 정본 근거를 자동으로 붙이기 시작한다.

```bash
git clone <repo> ~/anywhere/llmwiki_json
cd ~/anywhere/llmwiki_json
./scripts/install.sh --dry-run   # 무엇이 바뀔지 먼저 본다
./scripts/install.sh             # 실제 설치
```

경로는 어디여도 된다. 스크립트는 **자기 자신의 위치**에서 repo root 를 찾고,
설치 시점에 해석한 절대경로만 설정 파일에 적는다. 소스 어디에도 저장소 경로가
박혀 있지 않다(`tests/test_install.py::test_sources_hardcode_no_repository_path`).

## 지원 범위

| 플랫폼 | 상태 |
|---|---|
| macOS (Darwin) | 개발·검증 환경. 실제 Codex 0.148.0 / Claude Code 2.1.237 에서 확인 |
| Linux | 같은 POSIX 경로로 동작. `/usr/bin/python3` 를 우선 사용 |
| WSL | Linux 로 취급된다 — `install.sh` 를 쓴다 |
| 네이티브 Windows | `scripts/install.ps1`. Windows PowerShell 5.1 과 PowerShell 7 |
| 그 외 (*BSD) | 미검증. `--force` 로 강행할 수는 있다 |

POSIX 쪽은 `/bin/sh` 만 있으면 되고 bash 기능은 쓰지 않는다. macOS 의 `readlink`
에 `-f` 가 없는 것까지 감안해 symlink 를 직접 따라간다.

## Windows

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -DryRun
powershell -ExecutionPolicy Bypass -File scripts\install.ps1
powershell -ExecutionPolicy Bypass -File scripts\install.ps1 verify
```

명령과 옵션은 POSIX 쪽과 같다. `-DryRun` 처럼 PowerShell 식으로도, `--dry-run`
처럼 POSIX 식으로도 받는다 — 문서 한 벌로 양쪽을 설명하기 위해서다.
`tests/test_install_windows.py::ParityTest` 가 두 스크립트의 옵션 목록을 서로
대조하므로, 한쪽에만 옵션이 생기면 시험이 깨진다.

Windows 에서 달라지는 것은 hook 에 박히는 한 줄이다. 클라이언트가 hook 을
`cmd.exe` 로 돌리기 때문에 POSIX 의 `sh` 문법은 한 글자도 통하지 않는다.
`llmwiki_context.hook_command()` 가 플랫폼을 보고 둘 중 하나를 고르며, 설치와
`verify` 가 같은 함수를 쓰므로 설치한 것과 점검하는 것이 어긋날 수 없다.

```
POSIX    if [ -r <script> ] && [ -x <python> ]; then <python> <script> hook ...
Windows  ("<python>" "<script>" hook 2>nul) || (more>nul 2>nul)
```

양쪽 모두 인터프리터나 스크립트가 사라져도 stdin 을 비우고 조용히 성공한다 —
hook 이 프롬프트를 막는 일은 없다.

**검증 범위**: PowerShell 7.6.5 에서 구문·인자 처리·dry-run 까지 확인했다.
실제 Windows 기계의 Codex/Claude Code 를 상대로는 아직 돌려 보지 않았다.

**의존성**: 셸과 `curl` 또는 `wget` 뿐이다. Python 과 qmd 는 없으면 받아 온다.
`codex` / `claude` 는 있는 것만 대상으로 잡는다.

## 없는 도구는 받아 온다

기본 `install` 은 필요한 것을 **사용자 영역에** 마련한다. 시스템 경로도, shell
profile 도 건드리지 않고, 받은 절대경로를 그 자리에서 쓴다.

| 도구 | 없을 때 | 받는 곳 | 출처 |
|---|---|---|---|
| Python 3.9+ | uv 를 받아 `uv python install` | uv 관리 디렉터리 | 공식 Astral standalone installer |
| Bun | 정확히 **v1.3.14** | `$BUN_INSTALL` (기본 `~/.bun`) | 공식 Bun 설치기 |
| qmd | `bun install -g @tobilu/qmd` | Bun 전역 bin | 공식 npm 패키지 |

저장소 규칙상 package manager 는 Bun 하나뿐이라 npm·yarn·pnpm 은 쓰지 않는다.

**profile 을 건드리지 않는 법.** uv 설치기에는 `UV_NO_MODIFY_PATH=1` 과 옛 이름
`INSTALLER_NO_MODIFY_PATH=1` 을 함께 준다. Bun 설치기에는 그런 스위치가 없고
`case $(basename "$SHELL")` 로 고칠 profile 을 고르므로, `SHELL=/bin/sh` 로 불러
그 분기를 타지 않게 한다 — 설치기는 PATH 안내만 출력하고 파일은 그대로 둔다.

**이미 있는 것은 절대 갈아치우지 않는다.** Bun 이 1.3.14 가 아니어도 경고만 하고
그대로 쓴다. 남의 설치를 뒤엎는 쪽이 버전이 어긋나는 것보다 나쁘다.

### 인터프리터 선택 순서

```
1. --python 으로 준 경로            ← 언제나 최우선
2. 이미 설치된 uv 관리 Python        ← 시스템 Python 보다 먼저
3. 시스템 python3 3.9 이상
4. 위가 전부 없을 때만 uv 를 받아 마련한다
```

2번이 3번보다 앞인 이유는 재현성이다 — uv 관리 Python 은 버전이 고정돼 있어
hook 이 오래 살아남는다. 다만 **이미 있을 때만** 쓴다. 쓸 만한 시스템 Python 이
있는데 굳이 받아 오지는 않는다.

고른 인터프리터는 hook·전역 지침·MCP 등록에 모두 같은 것이 박힌다. 그래야
`verify` 가 검증한 것과 질문마다 실제로 도는 것이 같아진다.

viewer 는 이 설치와 별개로 돌기 때문에 자기 순서를 따로 가진다 —
`LLMWIKI_PYTHON` → `python3` → `python` → `py`. `python3` 라는 이름이 없는
기계(Windows, uv 로 받은 Python 만 있는 기계)에서는 `LLMWIKI_PYTHON` 에 이
스크립트가 고른 절대경로를 그대로 주면 된다.

### 받아 오지 않게 하려면

```bash
./scripts/install.sh --no-bootstrap   # 네트워크 설치 전면 금지
```

Python 이 없으면 명확한 오류로 멈추고(설정은 한 글자도 건드리지 않는다), qmd 가
없으면 경고하고 qmd 관련 작업만 건너뛴다. `--dry-run` 은 네트워크조차 타지 않고
무엇을 받을지 계획만 출력한다 — 설정 파일도, 받아 올 도구도, `index/markdown` 도
만들지 않는다. (다만 이미 설치된 `uv`·`bun` 에게 버전을 물어보기는 하므로, 그
도구들이 제 캐시 디렉터리를 만드는 것까지 막지는 않는다. 우리가 쓰는 것은 없다.)

도구 설치는 `install` 에서만 일어난다. `doctor` · `verify` · `uninstall` 은
아무것도 받아 오지 않는다.

### 실패하면

부트스트랩은 **설정 파일을 쓰기 전에** 전부 끝난다. 다운로드·설치가 실패하면
그 자리에서 명확한 오류로 멈추고, 그 시점까지 클라이언트 설정은 손대지 않은
상태다. 반쯤 바뀐 설정이 남는 일은 없다.

## 명령

```
scripts/install.sh [install|verify|uninstall|doctor|update] [옵션]
```

| 명령 | 하는 일 |
|---|---|
| `install` (기본) | hook · 전역 지침 · MCP 를 설치한다. 몇 번을 돌려도 결과가 같다 |
| `verify` | 설치 상태를 점검한다. 아무것도 고치지 않는다. 실패 시 exit 1 |
| `uninstall` | 이 스크립트가 넣은 것만 되돌린다 |
| `doctor` | 감지 결과와 해석된 경로만 출력한다 |
| `update` | upstream(`origin`)에서 코드 갱신만 ff-only 로 받아 온다. 개인 위키는 건드리지 않는다 |

| 옵션 | 뜻 |
|---|---|
| `-n`, `--dry-run` | 바뀔 내용만 보여주고 **아무것도 쓰지 않는다** |
| `--codex` / `--claude` | 한쪽만 대상으로 한다 (기본: 설치된 것 자동 감지) |
| `--no-qmd` | qmd 설치와 collection 준비를 건너뛴다 (기본은 준비한다) |
| `--with-qmd` | 기본값. 옛 이름을 남겨 둔 것뿐이다 |
| `--no-bootstrap` | 없는 도구를 받아 오지 않는다 |
| `--no-mcp` | MCP 서버 등록을 건너뛴다 |
| `--no-guides` | `~/.codex/AGENTS.md`, `~/.claude/CLAUDE.md` 를 건드리지 않는다 |
| `--python P` | 쓸 인터프리터 절대경로. 자동 선택보다 우선하고, 공백이 있어도 된다 |
| `--mcp-name N` / `--qmd-name N` | 이름 바꾸기 (기본 `llmwiki` / `llmwiki_json`) |
| `--force` | 미지원 OS 나 미감지 클라이언트에서도 강행한다 |
| `-q`, `--quiet` | 진행 로그를 줄인다 (클라이언트 실행 파일도 부르지 않는다) |
| `--ingest-routine C` | 주기 루틴을 걸 에이전트 — `claude` · `codex` · `none`. 주지 않으면 설치 중에 물어본다 |
| `--routine-interval S` | 루틴 주기(초, 기본 3600) |
| `--private-remote URL` | 개인 private 저장소를 remote 로 붙인다 |
| `--gh-create NAME` | `gh` 로 private 저장소를 새로 만들어 붙인다 |
| `--no-private` | 개인 저장소를 붙이지 않는다 (묻지도 않는다) |
| `--remote-name N` | 그 remote 의 이름 (기본 `private`). `origin` 은 손대지 않는다 |
| `-y`, `--yes` | 대화형 질문을 하지 않는다 — 플래그로 주지 않은 것은 건너뛴다 |

질문은 TTY 에서만 한다. 파이프·CI 처럼 입력이 없는 자리에서는 묻지 않고 그냥
건너뛰므로, 기존 무인 설치 흐름은 그대로다.

### 주기 ingest 루틴

`--ingest-routine` 을 고르면 OS 스케줄러(launchd · cron · schtasks)에 등록한다.
한 번 돌 때의 순서는 이렇다:

1. **`git pull`** — `private` remote 가 있으면 ff-only 로 맞춘다. 워킹트리가
   더럽거나 히스토리가 갈라졌으면 **여기서 멈춘다**.
2. 미처리 raw 소스 확인 — 없으면 에이전트를 부르지 않는다. 목록이 지난번과
   같아도(에이전트가 이미 보고 판단한 것이면) 건너뛴다.
3. 에이전트에게 ingest 를 맡긴다.
4. `build` · `validate` — 깨졌으면 커밋하지 않는다.
5. `wiki/` · `index/` · `viewer/public/data` 만 커밋하고 `private` 으로 push.

`raw/.llmwikiignore` 에 glob 을 적어 소스가 아닌 파일을 뺄 수 있다. 겹쳐 도는
것은 `$HOME/.llmwiki/routine.lock` 이 막고, 진행은 `$HOME/.llmwiki/routine.log`
에 쌓인다. 등록 사실은 `$HOME/.llmwiki/installed-routine` 에만 기록하며,
`uninstall` 은 그 기록이 있을 때만 스케줄러 항목을 지운다.

### 개인 저장소

`origin` 은 코드 갱신을 받는 자리로 그대로 둔다. 개인 위키는 `private` remote
하나를 더 붙여 그쪽으로만 민다. 이름이 같은 remote 가 이미 있고 다른 곳을
가리키면 덮어쓰지 않고 경고만 한다.

## 무엇을 건드리고 무엇을 건드리지 않는가

건드리는 것 — 전부 **덧붙이기**이고, 넣은 것만 정확히 도로 뺄 수 있다.

| 파일 | 방식 |
|---|---|
| `~/.codex/hooks.json` | `hooks.UserPromptSubmit` 배열 **끝에 그룹 하나 추가**. 기존 그룹(Orca 등)과 다른 이벤트는 읽은 그대로 되돌려 쓴다 |
| `~/.claude/settings.json` | 같음. `theme` · `permissions` 등 hook 밖 설정은 손대지 않는다 |
| `~/.codex/AGENTS.md`, `~/.claude/CLAUDE.md` | `<!-- llmwiki-context:start -->` … `end` 사이 섹션만 추가. 기존 본문은 그대로 |
| MCP 등록 | `claude mcp add --scope user` / `codex mcp add`. 같은 이름이 이미 있으면 **그대로 둔다**. 우리가 실제로 등록한 것만 `$HOME/.llmwiki/installed-mcp` 에 기록한다 |
| qmd collection | 기본으로 준비한다. 없으면 만들고, 우리 `index/markdown` 을 가리키고 있으면 재색인 |
| OS 스케줄러 | `--ingest-routine` 을 고른 경우에만. launchd `com.llmwiki.ingest` · cron `# llmwiki-routine` 줄 · schtasks 작업 하나. 우리가 등록한 기록이 있을 때만 지운다 |
| git remote | `--private-remote`/`--gh-create` 를 준 경우에만 remote 하나 **추가**. `origin` 과 기존 remote 는 그대로 |

절대 건드리지 않는 것:

- `wiki/**/*.json` 정본과 `raw/` — 읽기만 한다.
- `index/markdown/` 은 정식 `llmwiki.py export-md` 로만 다시 만든다 (`--no-qmd` 면 아예 만들지 않는다).
- **남의 qmd collection.** 이름이 겹치는 collection 이 다른 경로를 가리키면 경고만
  하고 손을 뗀다. `uninstall` 도 collection 을 지우지 않고 지우는 방법만 알려준다.
- 이미 등록된 같은 이름의 MCP 서버.

## 백업과 롤백

처음 손대기 직전 상태를 `<파일>.llmwiki-bak` 으로 한 번만 남긴다. 두 번째 설치는
백업을 덮어쓰지 않으므로, 백업은 언제나 "설치 이전"을 가리킨다.

```bash
./scripts/install.sh uninstall          # 넣은 것만 정확히 제거 (권장)
```

`uninstall` 은 우리 hook 그룹과 지침 섹션만 빼고, 우리가 만든 빈 파일은 지운다.
설치 이후에 사용자가 직접 넣은 다른 설정은 그대로 살아남는다.

### MCP 는 우리가 만든 것만 지운다

설치기는 자기가 **실제로 등록한** MCP 서버만 `$HOME/.llmwiki/installed-mcp` 에
`클라이언트 <TAB> 이름 <TAB> 스크립트` 한 줄로 기록한다(`LLMWIKI_STATE_DIR` 로 위치를
바꿀 수 있다). `uninstall` 은 그 기록이 있고 **지금 등록된 서버가 여전히 그 스크립트를
가리킬 때만** 제거한다.

| 상황 | uninstall 동작 |
|---|---|
| 설치기가 등록했고 그대로다 | 제거하고 기록을 지운다 |
| 이름은 같지만 설치 전부터 있던 남의 서버 | **그대로 둔다.** 직접 지우는 명령을 알려준다 |
| 설치 후 누군가 다른 서버로 바꿔 놨다 | **그대로 둔다.** 우리 기록만 지운다 |
| 이미 등록이 없다 | 기록만 지운다 |

`install` 쪽도 대칭이다 — 같은 이름의 남의 서버는 절대 덮어쓰지 않고, 우리가 등록한
것이 clone 이동으로 낡은 경로를 가리키면 그때만 다시 등록한다. 소유 기록은
`scripts/install.sh doctor` 에서 볼 수 있다.

통째로 되돌리고 싶으면 백업을 복사한다 — 이 경우 설치 이후의 다른 변경도 함께
사라진다는 점을 감안하라.

```bash
cp ~/.codex/hooks.json.llmwiki-bak      ~/.codex/hooks.json
cp ~/.claude/settings.json.llmwiki-bak  ~/.claude/settings.json
```

잠깐 끄기만 할 거면 설정을 건드리지 말고 환경변수를 쓴다.

```bash
export LLMWIKI_CONTEXT_DISABLE=1
```

## Codex hook 신뢰

Codex 0.148 이상은 **새로 추가되거나 바뀐 hook 을 신뢰하기 전까지 그 출력을 무시한다.**
설치 스크립트는 신뢰 상태를 읽어 필요한 안내를 출력한다.

```
== Codex hook 신뢰: review-required
  Codex 0.148 이상은 새 hook 을 신뢰하기 전까지 그 출력을 무시한다.
  한 번만 아래를 하면 된다:
    1) codex 를 실행한다
    2) 'Hooks need review' 프롬프트에서 hook 을 확인한다
       (명령이 …/llmwiki_context.py hook 인지 볼 것)
    3) t 를 눌러 신뢰한다
```

신뢰 기록은 `~/.codex/config.toml` 의
`[hooks.state."<hooks.json>:user_prompt_submit:<n>:0"]` 아래 `trusted_hash` 로 남는다.

**hook 명령이 바뀌면 그 신뢰는 무효가 된다** — 인터프리터가 바뀌거나 clone 을
옮기면 Codex 가 다시 묻는다. 해시 계산식은 공개돼 있지 않아 우리가 검산할 수는
없지만, 설치기가 명령을 바꿀 때 그 시점의 지문을 `$HOME/.llmwiki/codex-trust-stale`
에 적어 둔다. 저장된 지문이 아직 그대로면 사용자가 다시 신뢰를 주지 않은 것이므로
`verify` 가 `review-required` 로 보고한다. 다시 신뢰하면 Codex 가 새 지문을 쓰고,
`verify` 는 그때 `trusted` 로 바뀐다.

이 장치가 없으면 `verify` 는 명령이 바뀐 뒤에도 `trusted` 라고 답한다 —
실제로는 주입이 전혀 안 되는 상태인데도. 점검이 거짓을 말하지 않는 것이
점검의 존재 이유다.

Claude Code 에는 이런 게이트가 없어 설치 즉시 동작한다.

## verify

```bash
./scripts/install.sh verify
```

```
ok   python: /usr/bin/python3
ok   repo: <REPO_ROOT>
ok   corpus: 212 pages
ok   codex.hook: <HOME>/.codex/hooks.json (group 1)
ok   codex.hook-current: installed command matches this checkout
ok   codex.guide: <HOME>/.codex/AGENTS.md
ok   codex.trust: trusted
ok   claude.hook: <HOME>/.claude/settings.json (group 1)
ok   claude.hook-current: installed command matches this checkout
ok   claude.guide: <HOME>/.claude/CLAUDE.md
ok   probe-injects: query='…'
ok   probe-silent-on-noise: 무관 질문에는 주입하지 않는다
ok   fail-open: malformed stdin 은 조용히 통과한다
모든 점검 통과
```

`<REPO_ROOT>` 는 clone 위치, `<HOME>` 은 `$HOME` 이다 — 실제 출력에는 해석된
절대경로가 찍힌다.

`doctor` 는 도구마다 경로·버전·출처를 함께 보여 준다.

```
  다운로더        curl
  부트스트랩      허용
  python          <...>/cpython-3.12-.../bin/python3.12  [3.12.13, uv-관리]
  uv              /opt/homebrew/bin/uv  [uv 0.12.5, 기존]
  bun             /opt/homebrew/bin/bun  [1.3.14, 기존]
  qmd             <HOME>/.bun/bin/qmd  [qmd 2.8.3, 기존]
```

출처는 `--python`(사용자 지정) · `uv-관리`(이미 있던 uv Python) · `시스템` ·
`uv-설치함`(받아 옴) · `기존` · `설치함` 중 하나다.

점검 항목의 뜻:

- `hook-current` — 설치된 명령이 **지금 이 checkout** 을 가리키는가. clone 을 옮기면
  `stale command` 로 뜨고, 다시 `install` 하면 된다.
- `probe-injects` — 정본에서 뽑은 실제 page 제목으로 hook 을 끝까지 돌려 본다.
  저장소 내용을 하드코딩하지 않으므로 어떤 clone 에서도 같은 검사가 돈다.
- `probe-silent-on-noise` — 무관한 질문에 주입하지 않는지.
- `fail-open` — 깨진 stdin 에도 조용히 통과하는지(질문을 막지 않는지).

## 테스트

```bash
python3 -m unittest tests.test_install     # 설치 스크립트만
python3 -m unittest discover -s tests -t . # 전체
```

`tests/test_install.py` 는 매번 임시 clone(일부러 깊은 경로)과 가짜 `$HOME` 을 만들어
돌기 때문에 개발 머신의 실제 설정을 건드리지 않는다. 다루는 것:

clone 경로 독립성 · symlink 를 통한 실행 · dry-run 무부작용 · 남의 hook 그룹/설정/
지침 본문 보존 · 백업이 설치 이전을 가리키는지 · 3회 설치 멱등성 · clone 이동 뒤
재설치 · uninstall 의 의미 동등 복원 · 우리가 만든 파일만 삭제 · 클라이언트 개별
선택 · `--no-guides` · 미지원 OS 거부와 `--force` · 알 수 없는 인자 exit 2 ·
qmd 미설치 시 경고만 · 남의 collection 불가침 · uninstall 이 collection 을 지우지
않음 · verify 의 stale 감지 · Codex 신뢰 상태 판독과 명령 변경 시 신뢰 무효화.

MCP 소유권은 `mcp add|get|remove` 만 흉내 내는 가짜 `claude`/`codex` CLI 로 검증한다 —
등록·소유 기록, 남의 서버 불가침(설치·제거 양쪽), 설치 후 교체된 서버 보존, clone 이동
뒤 자기 등록만 갱신, 재설치 시 중복 없음, `--no-mcp`·dry-run 무기록, 이름별 분리 추적.

`--python` 공백 경로는 install/verify/uninstall/doctor 네 경로 모두와, 생성된 hook
명령을 실제로 `/bin/sh` 로 실행해 주입까지 되는지로 검증한다.

부트스트랩은 가짜 `curl`/`wget`/`uv`/`bun`/`qmd` 로 **네트워크 없이** 검증한다.
안전장치로 모든 테스트가 가짜 `curl`/`wget` 을 PATH 에 깔고 시작하므로, 어떤
테스트도 실수로 진짜 네트워크를 탈 수 없다. 다루는 것: uv·Python 받아오기,
`UV_NO_MODIFY_PATH` 전달, Bun v1.3.14 고정과 `SHELL=/bin/sh` 로 profile 미변경,
`bun install -g @tobilu/qmd`, 설치 후 collection 준비, 이미 있는 uv Python 우선,
시스템 Python 대체, `--python` 최우선, 잘못된 `--python` 거부, `--no-bootstrap`,
다운로드·설치기·`uv python install`·`bun install -g` 각각의 실패가 설정을 건드리기
전에 멈추는지, curl 없을 때 wget 대체, dry-run 무네트워크·무디스크, 재실행 멱등성,
공백 든 `$HOME` 에서의 전체 경로, doctor/verify 의 출처·버전 표시.

## 관련 문서

동작 원리, 주입 예산, 두 클라이언트의 hook 스키마 차이는
[`docs/context-injection.md`](context-injection.md) 에 있다.

#!/bin/sh
# llmwiki_json 자동 컨텍스트 주입 설치 스크립트.
#
# 이 저장소를 어디에 clone 하든, 이 스크립트가 있는 위치에서 repo root 를 찾아
# Codex 와 Claude Code 의 UserPromptSubmit hook 을 설치한다. 저장소 경로를
# 하드코딩하지 않으며, 설치 시점에 해석한 절대경로만 설정 파일에 박는다.
#
# 지원 범위
#   macOS (darwin)  — 개발·검증 환경
#   Linux           — 같은 POSIX 경로로 동작 (CI/서버)
#   그 외 (WSL 밖 Windows, *BSD) — 미검증. --force 로 강행할 수 있다.
#
# 없는 도구는 사용자 영역에 받아 온다 — Python 은 uv, qmd 는 Bun. 어느 쪽도
# shell profile 이나 시스템 경로를 건드리지 않고, 받은 절대경로를 바로 쓴다.
# --no-bootstrap 으로 네트워크 설치를 전부 끌 수 있고, --dry-run 은 계획만 보여준다.
#
# 이 스크립트는 wiki/ 정본과 raw/ 를 절대 건드리지 않는다. index/markdown 은
# 정식 `llmwiki.py export-md` 로만 다시 만든다.
#
# 사용법:  scripts/install.sh [install|verify|uninstall|doctor] [옵션]
set -eu

# --------------------------------------------------------------- 위치 해석
# macOS 의 readlink 에는 -f 가 없다. symlink 를 직접 따라간다.
resolve_dir() {
    _target=$1
    _guard=0
    while [ -L "$_target" ] && [ "$_guard" -lt 40 ]; do
        _link=$(readlink "$_target")
        case $_link in
            /*) _target=$_link ;;
            *) _target=$(dirname "$_target")/$_link ;;
        esac
        _guard=$((_guard + 1))
    done
    (cd "$(dirname "$_target")" && pwd -P)
}

SCRIPT_DIR=$(resolve_dir "$0")
REPO_ROOT=$(cd "$SCRIPT_DIR/.." && pwd -P)
CONTEXT_CLI=$SCRIPT_DIR/llmwiki_context.py
WIKI_CLI=$SCRIPT_DIR/llmwiki.py
ROUTINE_CLI=$SCRIPT_DIR/llmwiki_routine.py

# --------------------------------------------------------------- 기본값
COMMAND=install
DRY_RUN=0
FORCE=0
WANT_CODEX=0
WANT_CLAUDE=0
WITH_QMD=1
WITH_MCP=1
WITH_GUIDES=1
BOOTSTRAP=1
PYTHON=""
PYTHON_OPT=""
MCP_NAME=llmwiki
QMD_NAME=llmwiki_json
QUIET=0
# 주기 ingest 루틴과 개인 저장소. 기본은 "묻는다" 이고, 비대화형에서는 건너뛴다.
ROUTINE=ask
ROUTINE_INTERVAL=3600
PRIVATE_URL=""
GH_CREATE=""
PRIVATE=ask
ASK=1
REMOTE_NAME=private

# 부트스트랩 대상. 저장소 규칙상 package manager 는 Bun 뿐이고 버전은 고정이다
# (npm/yarn/pnpm 금지). qmd 는 공식 패키지에서만 받는다.
BUN_VERSION=1.3.14
QMD_PACKAGE=@tobilu/qmd
UV_INSTALLER_URL=https://astral.sh/uv/install.sh
BUN_INSTALLER_URL=https://bun.sh/install
UV_DIR=${UV_INSTALL_DIR:-$HOME/.local/bin}
BUN_DIR=${BUN_INSTALL:-$HOME/.bun}

# 해석 결과와 출처. doctor/verify 가 그대로 보여 준다.
PY=""
PY_SOURCE=""
PY_VERSION=""
UV_BIN=""
UV_SOURCE=""
BUN_BIN=""
BUN_SOURCE=""
QMD_BIN=""
QMD_SOURCE=""

usage() {
    cat <<'USAGE'
llmwiki_json 자동 컨텍스트 주입 설치

  scripts/install.sh [명령] [옵션]

명령
  install     (기본) hook · 전역 지침 · MCP 를 설치한다. 여러 번 돌려도 안전하다.
  verify      설치 상태를 점검한다. 아무것도 바꾸지 않는다.
  uninstall   이 스크립트가 넣은 것만 되돌린다. 남의 설정은 남긴다.
  doctor      감지 결과와 해석된 경로만 출력한다.
  update      upstream(origin) 에서 코드 갱신만 받아 온다. 개인 위키는 건드리지 않는다.

옵션
  -n, --dry-run       바뀔 내용만 보여주고 아무것도 쓰지 않는다 (네트워크도 안 탄다)
      --codex         Codex 만 대상으로 한다
      --claude        Claude Code 만 대상으로 한다
      --no-qmd        qmd 설치와 collection 준비를 건너뛴다
      --with-qmd      (기본값) qmd 를 준비한다 — 옛 이름, 남겨 둔다
      --no-bootstrap  없는 도구를 받아 오지 않는다 (네트워크 설치 전면 금지)
      --no-mcp        MCP 서버 등록을 건너뛴다
      --no-guides     ~/.codex/AGENTS.md, ~/.claude/CLAUDE.md 를 건드리지 않는다
      --python P      쓸 인터프리터 절대경로 — 자동 선택보다 항상 우선한다
      --mcp-name N    MCP 서버 이름 (기본 llmwiki)
      --qmd-name N    qmd collection 이름 (기본 llmwiki_json)
      --force         미지원 OS 나 미감지 클라이언트에서도 강행한다
  -q, --quiet         진행 로그를 줄인다
  -h, --help          이 도움말

주기 ingest 루틴 — raw/ 에 새 소스가 들어오면 에이전트가 위키에 넣는다
      --ingest-routine C  C 는 claude · codex · none. 주지 않으면 설치 중에 물어본다
      --routine-interval S  주기(초, 기본 3600)
  -y, --yes           대화형 질문을 하지 않는다 (지정하지 않은 것은 건너뛴다)

개인 저장소 — 이 clone 은 update 용으로 두고, 위키는 자기 private 저장소로 민다
      --private-remote URL  이미 만들어 둔 private 저장소를 remote 로 붙인다
      --gh-create NAME      gh 로 private 저장소를 새로 만들어 붙인다
      --no-private          개인 저장소를 붙이지 않는다 (묻지도 않는다)
      --remote-name N       그 remote 의 이름 (기본 private, origin 은 손대지 않는다)

클라이언트를 지정하지 않으면 설치된 것만 자동으로 고른다.

인터프리터 선택 순서
  1. --python 으로 준 경로
  2. 이미 설치돼 있는 uv 관리 Python  (시스템 Python 보다 우선)
  3. 시스템 python3 3.9 이상
  4. 아무것도 없으면 uv 를 받아 uv 관리 Python 을 설치해서 쓴다

받아 오는 것 — 전부 사용자 영역이고 shell profile 은 건드리지 않는다
  uv    공식 Astral standalone installer, UV_NO_MODIFY_PATH=1
  Bun   공식 설치기로 정확히 v1.3.14 를 $BUN_INSTALL(기본 ~/.bun) 에
  qmd   bun install -g @tobilu/qmd
USAGE
}

while [ $# -gt 0 ]; do
    case $1 in
        install | verify | uninstall | doctor | update) COMMAND=$1 ;;
        -n | --dry-run) DRY_RUN=1 ;;
        --codex) WANT_CODEX=1 ;;
        --claude) WANT_CLAUDE=1 ;;
        --with-qmd) WITH_QMD=1 ;;
        --no-qmd) WITH_QMD=0 ;;
        --no-bootstrap) BOOTSTRAP=0 ;;
        --no-mcp) WITH_MCP=0 ;;
        --no-guides) WITH_GUIDES=0 ;;
        --python) PYTHON_OPT=${2:?--python 에 경로가 필요하다}; shift ;;
        --python=*) PYTHON_OPT=${1#--python=} ;;
        --mcp-name) MCP_NAME=${2:?--mcp-name 에 이름이 필요하다}; shift ;;
        --qmd-name) QMD_NAME=${2:?--qmd-name 에 이름이 필요하다}; shift ;;
        --force) FORCE=1 ;;
        --ingest-routine) ROUTINE=${2:?--ingest-routine 에 claude/codex/none 이 필요하다}; shift ;;
        --ingest-routine=*) ROUTINE=${1#--ingest-routine=} ;;
        --routine-interval) ROUTINE_INTERVAL=${2:?--routine-interval 에 초가 필요하다}; shift ;;
        --routine-interval=*) ROUTINE_INTERVAL=${1#--routine-interval=} ;;
        --private-remote) PRIVATE_URL=${2:?--private-remote 에 URL 이 필요하다}; PRIVATE=yes; shift ;;
        --private-remote=*) PRIVATE_URL=${1#--private-remote=}; PRIVATE=yes ;;
        --gh-create) GH_CREATE=${2:?--gh-create 에 이름이 필요하다}; PRIVATE=yes; shift ;;
        --gh-create=*) GH_CREATE=${1#--gh-create=}; PRIVATE=yes ;;
        --no-private) PRIVATE=no ;;
        --remote-name) REMOTE_NAME=${2:?--remote-name 에 이름이 필요하다}; shift ;;
        --remote-name=*) REMOTE_NAME=${1#--remote-name=} ;;
        -y | --yes) ASK=0 ;;
        -q | --quiet) QUIET=1 ;;
        -h | --help) usage; exit 0 ;;
        *) printf 'error: 알 수 없는 인자 %s\n\n' "$1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

# --------------------------------------------------------------- 출력
say() { [ "$QUIET" -eq 1 ] || printf '%s\n' "$*"; }
step() { [ "$QUIET" -eq 1 ] || printf '\n== %s\n' "$*"; }
warn() { printf 'warn: %s\n' "$*" >&2; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }
plan() { printf '   [dry-run] %s\n' "$*"; }
has() { command -v "$1" >/dev/null 2>&1; }

# --------------------------------------------------------------- 감지
OS=$(uname -s 2>/dev/null || echo unknown)
case $OS in
    Darwin | Linux) ;;
    *)
        if [ "$FORCE" -eq 1 ]; then
            warn "$OS 는 검증되지 않았다 — --force 로 강행한다"
        else
            die "$OS 는 지원 범위 밖이다 (macOS/Linux). 강행하려면 --force"
        fi
        ;;
esac

[ -f "$CONTEXT_CLI" ] || die "$CONTEXT_CLI 가 없다 — 저장소 안에서 실행하라"
[ -d "$REPO_ROOT/wiki" ] || die "$REPO_ROOT/wiki 가 없다 — repo root 해석 실패"

# --------------------------------------------------------------- 부트스트랩
# 없는 도구를 사용자 영역에 받아 온다. 원칙 셋:
#   1. shell profile 과 시스템 경로는 건드리지 않는다. 받은 절대경로만 쓴다.
#   2. --dry-run 은 네트워크조차 타지 않는다. 계획만 출력한다.
#   3. 실패는 조용히 넘기지 않고 명확한 오류로 세운다 — 단, 설정 파일을
#      한 글자라도 쓰기 전에 세운다(부트스트랩은 전부 쓰기 앞에서 끝낸다).
WORK=""
cleanup_work() {
    if [ -n "$WORK" ]; then rm -rf "$WORK"; fi
}
trap cleanup_work EXIT INT TERM

workdir() {
    if [ -z "$WORK" ]; then
        WORK=$(mktemp -d "${TMPDIR:-/tmp}/llmwiki-install.XXXXXX")
    fi
    printf '%s' "$WORK"
}

download_to() { # url dest — curl 우선, 없으면 wget
    if has curl; then
        curl -fsSL "$1" -o "$2"
        return $?
    fi
    if has wget; then
        wget -q -O "$2" "$1"
        return $?
    fi
    return 127
}

downloader_name() {
    if has curl; then printf 'curl'
    elif has wget; then printf 'wget'
    else printf '없음'; fi
}

require_bootstrap() { # 무엇을 받으려는지
    if [ "$BOOTSTRAP" -eq 0 ]; then
        die "$1 이(가) 없는데 --no-bootstrap 이라 받아 올 수 없다"
    fi
    if ! has curl && ! has wget; then
        die "$1 을(를) 받으려면 curl 이나 wget 이 필요하다"
    fi
}

# ---------------------------------------------------------------------- uv
resolve_uv() { # 이미 있는 uv 를 찾는다. 네트워크를 타지 않는다.
    if [ -n "$UV_BIN" ]; then return 0; fi
    if has uv; then
        UV_BIN=$(command -v uv)
        UV_SOURCE=기존
        return 0
    fi
    if [ -x "$UV_DIR/uv" ]; then
        UV_BIN=$UV_DIR/uv
        UV_SOURCE=기존
        return 0
    fi
    return 1
}

bootstrap_uv() {
    if resolve_uv; then return 0; fi
    if [ "$ALLOW_TOOL_INSTALL" -eq 0 ]; then return 1; fi
    if [ "$DRY_RUN" -eq 1 ]; then
        plan "uv 설치: $(downloader_name) $UV_INSTALLER_URL -> UV_INSTALL_DIR=$UV_DIR (UV_NO_MODIFY_PATH=1)"
        return 0
    fi
    require_bootstrap uv
    say "  uv 를 받는다 ($UV_DIR, shell profile 은 건드리지 않는다)"
    _script=$(workdir)/uv-install.sh
    download_to "$UV_INSTALLER_URL" "$_script" ||
        die "uv 설치 스크립트를 받지 못했다 ($UV_INSTALLER_URL)"
    # UV_NO_MODIFY_PATH 는 현행 이름, INSTALLER_NO_MODIFY_PATH 는 예전 이름.
    # 둘 다 줘서 어느 세대의 설치기가 와도 profile 을 건드리지 않게 한다.
    env UV_INSTALL_DIR="$UV_DIR" UV_NO_MODIFY_PATH=1 INSTALLER_NO_MODIFY_PATH=1 \
        sh "$_script" >/dev/null 2>&1 || die "uv 설치가 실패했다"
    [ -x "$UV_DIR/uv" ] || die "uv 를 설치했는데 $UV_DIR/uv 가 없다"
    UV_BIN=$UV_DIR/uv
    UV_SOURCE=설치함
}

uv_managed_python() { # 이미 설치된 uv 관리 Python (다운로드 없음)
    [ -n "$UV_BIN" ] || return 1
    _found=$("$UV_BIN" python find --managed-python 2>/dev/null | head -1)
    python_ok "$_found" || return 1
    printf '%s' "$_found"
}

# ---------------------------------------------------------------------- python
python_ok() {
    [ -n "$1" ] || return 1
    "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' \
        >/dev/null 2>&1
}

python_version() {
    "$1" -c 'import sys;print("%d.%d.%d"%sys.version_info[:3])' 2>/dev/null || printf '?'
}

# 붙박이 절대경로를 먼저 본다. PATH 의 `python3` 는 pyenv/asdf shim 일 때가
# 많은데, shim 은 사용자가 버전을 바꾸면 가리키는 곳이 달라져 hook 에 못박기에
# 가장 불안정하다 — 그래서 맨 뒤다.
# 목록은 바꿀 수 있다 — 관리자가 검색 순서를 고정하거나, 테스트가 "시스템
# Python 이 없는 기계" 를 재현할 때 쓴다(빈 값 = 시스템 Python 없음).
SYSTEM_PYTHONS=${LLMWIKI_PYTHON_CANDIDATES-/usr/bin/python3 /opt/homebrew/bin/python3 /usr/local/bin/python3 python3}

find_system_python() {
    # shellcheck disable=SC2086 -- 공백으로 나뉜 후보 목록이라 분리가 목적이다
    for _candidate in $SYSTEM_PYTHONS; do
        _resolved=$(command -v "$_candidate" 2>/dev/null || printf '%s' "$_candidate")
        if python_ok "$_resolved"; then
            printf '%s' "$_resolved"
            return 0
        fi
    done
    return 1
}

resolve_python() {
    # 1. 사용자가 준 것이 언제나 최우선이다.
    if [ -n "$PYTHON_OPT" ]; then
        python_ok "$PYTHON_OPT" || die "--python $PYTHON_OPT 이 3.9 이상 Python 이 아니다"
        PY=$PYTHON_OPT
        PY_SOURCE=--python
        PY_VERSION=$(python_version "$PY")
        return 0
    fi
    # 2. 이미 있는 uv 관리 Python 을 시스템 Python 보다 먼저 쓴다.
    #    (재현 가능한 인터프리터라 hook 이 오래 살아남는다. 받아 오지는 않는다.)
    if resolve_uv; then
        _managed=$(uv_managed_python || printf '')
        if [ -n "$_managed" ]; then
            PY=$_managed
            PY_SOURCE=uv-관리
            PY_VERSION=$(python_version "$PY")
            return 0
        fi
    fi
    # 3. 시스템 Python.
    _system=$(find_system_python || printf '')
    if [ -n "$_system" ]; then
        PY=$_system
        PY_SOURCE=시스템
        PY_VERSION=$(python_version "$PY")
        return 0
    fi
    # 4. 쓸 만한 것이 하나도 없을 때만 받아 온다 — 그것도 install 에서만.
    PY=""
    PY_VERSION="-"
    if [ "$ALLOW_TOOL_INSTALL" -eq 0 ]; then
        PY_SOURCE=없음
        return 0
    fi
    if [ "$DRY_RUN" -eq 1 ]; then
        plan "Python 3.9+ 가 없다 — uv 를 받아 uv python install 로 마련한다"
        PY_SOURCE="uv-설치예정"
        return 0
    fi
    require_bootstrap "Python 3.9 이상"
    bootstrap_uv
    say "  uv 관리 Python 을 설치한다"
    "$UV_BIN" python install >/dev/null 2>&1 || die "uv python install 이 실패했다"
    _managed=$(uv_managed_python || printf '')
    [ -n "$_managed" ] || die "uv 로 Python 을 설치했는데 찾지 못했다"
    PY=$_managed
    PY_SOURCE=uv-설치함
    PY_VERSION=$(python_version "$PY")
}

# ---------------------------------------------------------------------- bun
bun_bin_dir() {
    if [ -n "$BUN_BIN" ] && "$BUN_BIN" pm bin -g >/dev/null 2>&1; then
        "$BUN_BIN" pm bin -g 2>/dev/null
        return 0
    fi
    printf '%s' "$BUN_DIR/bin"
}

resolve_bun() {
    if [ -n "$BUN_BIN" ]; then return 0; fi
    if has bun; then
        BUN_BIN=$(command -v bun)
        BUN_SOURCE=기존
        return 0
    fi
    if [ -x "$BUN_DIR/bin/bun" ]; then
        BUN_BIN=$BUN_DIR/bin/bun
        BUN_SOURCE=기존
        return 0
    fi
    return 1
}

bootstrap_bun() {
    if [ "$ALLOW_TOOL_INSTALL" -eq 0 ] && ! resolve_bun; then return 1; fi
    if resolve_bun; then
        _have=$("$BUN_BIN" --version 2>/dev/null || printf '?')
        if [ "$_have" != "$BUN_VERSION" ]; then
            # 이미 쓰고 있는 Bun 을 말없이 갈아치우지 않는다. 저장소 규칙은
            # 1.3.14 지만, 남의 설치를 뒤엎는 쪽이 더 나쁘다.
            warn "설치된 Bun 이 $_have 다 (저장소 규칙은 $BUN_VERSION) — 그대로 쓴다"
        fi
        return 0
    fi
    if [ "$DRY_RUN" -eq 1 ]; then
        plan "Bun $BUN_VERSION 설치: $(downloader_name) $BUN_INSTALLER_URL -> BUN_INSTALL=$BUN_DIR"
        return 0
    fi
    require_bootstrap "Bun $BUN_VERSION"
    has bash || die "Bun 공식 설치기는 bash 를 쓴다 — bash 가 필요하다"
    say "  Bun $BUN_VERSION 을 받는다 ($BUN_DIR)"
    _script=$(workdir)/bun-install.sh
    download_to "$BUN_INSTALLER_URL" "$_script" ||
        die "Bun 설치 스크립트를 받지 못했다 ($BUN_INSTALLER_URL)"
    # SHELL=/bin/sh 로 부르면 설치기의 profile 수정 분기가 `sh` 를 모르는
    # 셸로 보고 건너뛴다 — PATH 안내만 출력하고 파일은 건드리지 않는다.
    env BUN_INSTALL="$BUN_DIR" SHELL=/bin/sh bash "$_script" "bun-v$BUN_VERSION" \
        >/dev/null 2>&1 || die "Bun $BUN_VERSION 설치가 실패했다"
    [ -x "$BUN_DIR/bin/bun" ] || die "Bun 을 설치했는데 $BUN_DIR/bin/bun 이 없다"
    BUN_BIN=$BUN_DIR/bin/bun
    BUN_SOURCE=설치함
}

# ---------------------------------------------------------------------- qmd
resolve_qmd() {
    if [ -n "$QMD_BIN" ]; then return 0; fi
    if has qmd; then
        QMD_BIN=$(command -v qmd)
        QMD_SOURCE=기존
        return 0
    fi
    if [ -x "$BUN_DIR/bin/qmd" ]; then
        QMD_BIN=$BUN_DIR/bin/qmd
        QMD_SOURCE=기존
        return 0
    fi
    return 1
}

bootstrap_qmd() {
    if resolve_qmd; then return 0; fi
    if [ "$ALLOW_TOOL_INSTALL" -eq 0 ]; then return 1; fi
    if [ "$DRY_RUN" -eq 1 ]; then
        bootstrap_bun
        plan "bun install -g $QMD_PACKAGE"
        return 0
    fi
    if [ "$BOOTSTRAP" -eq 0 ]; then
        warn "qmd 가 없는데 --no-bootstrap 이라 받아 오지 않는다 — collection 준비를 건너뛴다"
        return 1
    fi
    bootstrap_bun
    say "  $QMD_PACKAGE 을(를) 설치한다 (bun install -g)"
    "$BUN_BIN" install -g "$QMD_PACKAGE" >/dev/null 2>&1 ||
        die "bun install -g $QMD_PACKAGE 이 실패했다"
    _dir=$(bun_bin_dir)
    [ -x "$_dir/qmd" ] || die "qmd 를 설치했는데 $_dir/qmd 가 없다"
    QMD_BIN=$_dir/qmd
    QMD_SOURCE=설치함
    return 0
}

qmd_cmd() { # 해석된 qmd 를 부른다
    "$QMD_BIN" "$@"
}

# `set -e` 아래에서 `cond && VAR=1` 은 cond 가 거짓일 때 스크립트를 끝낸다.
# 감지는 실패가 정상인 검사이므로 전부 if 로 쓴다.
HAVE_CODEX=0
HAVE_CLAUDE=0
if has codex; then HAVE_CODEX=1; fi
if has claude; then HAVE_CLAUDE=1; fi

# 클라이언트 선택: 플래그가 있으면 그대로, 없으면 감지된 것만.
if [ "$WANT_CODEX" -eq 0 ] && [ "$WANT_CLAUDE" -eq 0 ]; then
    WANT_CODEX=$HAVE_CODEX
    WANT_CLAUDE=$HAVE_CLAUDE
    AUTO_SELECTED=1
else
    AUTO_SELECTED=0
fi

# POSIX sh 에는 배열이 없다. 공통 인자를 문자열로 이어 붙이면 공백 있는
# --python 경로가 단어 분리로 깨지므로, 호출 지점마다 `set --` 로 인자
# 목록을 만들어 넘긴다.
run_context() {
    set -- "$@"
    if [ "$WANT_CODEX" -eq 1 ]; then set -- "$@" --client codex; fi
    if [ "$WANT_CLAUDE" -eq 1 ]; then set -- "$@" --client claude; fi
    if [ -n "$PYTHON" ]; then set -- "$@" --python "$PYTHON"; fi
    "$PY" "$CONTEXT_CLI" "$@"
}

# 사람이 읽는 미리보기용. 실제 실행에는 쓰지 않는다.
client_flags_text() {
    _text=""
    if [ "$WANT_CODEX" -eq 1 ]; then _text="$_text --client codex"; fi
    if [ "$WANT_CLAUDE" -eq 1 ]; then _text="$_text --client claude"; fi
    printf '%s' "$_text"
}

report_detection() {
    # --quiet 에서는 클라이언트 실행 파일을 부르지도 않는다. `codex --version`
    # 같은 호출도 $HOME 아래에 제 캐시를 만들기 때문이다.
    if [ "$QUIET" -eq 1 ]; then return 0; fi
    step "감지"
    printf '  os              %s\n' "$OS"
    printf '  repo root       %s\n' "$REPO_ROOT"
    printf '  다운로더        %s\n' "$(downloader_name)"
    printf '  부트스트랩      %s\n' "$([ "$BOOTSTRAP" -eq 1 ] && echo 허용 || echo '금지 (--no-bootstrap)')"
    printf '  python          %s  [%s, %s]\n' "${PY:-없음}" "$PY_VERSION" "$PY_SOURCE"
    if resolve_uv; then
        printf '  uv              %s  [%s, %s]\n' "$UV_BIN" \
            "$("$UV_BIN" --version 2>/dev/null | head -1)" "$UV_SOURCE"
    else
        printf '  uv              없음 (필요할 때만 받는다)\n'
    fi
    if resolve_bun; then
        printf '  bun             %s  [%s, %s]\n' "$BUN_BIN" \
            "$("$BUN_BIN" --version 2>/dev/null | head -1)" "$BUN_SOURCE"
    else
        printf '  bun             없음 (qmd 가 필요할 때만 받는다, v%s 고정)\n' "$BUN_VERSION"
    fi
    if resolve_qmd; then
        printf '  qmd             %s  [%s, %s]\n' "$QMD_BIN" \
            "$("$QMD_BIN" --version 2>/dev/null | head -1)" "$QMD_SOURCE"
    else
        printf '  qmd             없음 (%s 로 설치한다)\n' "$QMD_PACKAGE"
    fi
    if [ "$HAVE_CODEX" -eq 1 ]; then
        printf '  codex           %s\n' "$(codex --version 2>/dev/null | head -1)"
    else
        printf '  codex           없음\n'
    fi
    if [ "$HAVE_CLAUDE" -eq 1 ]; then
        printf '  claude          %s\n' "$(claude --version 2>/dev/null | head -1)"
    else
        printf '  claude          없음\n'
    fi
    printf '  대상            codex=%s claude=%s%s\n' "$WANT_CODEX" "$WANT_CLAUDE" \
        "$([ "$AUTO_SELECTED" -eq 1 ] && echo ' (자동 감지)')"
    if [ -f "$MCP_STATE" ]; then
        printf '  MCP 소유 기록   %s\n' "$MCP_STATE"
        sed 's/^/                  /' "$MCP_STATE"
    else
        printf '  MCP 소유 기록   없음 (%s)\n' "$MCP_STATE"
    fi
}

require_targets() {
    if [ "$WANT_CODEX" -eq 0 ] && [ "$WANT_CLAUDE" -eq 0 ]; then
        die "설치할 클라이언트가 없다. codex 나 claude 를 설치하거나 --codex/--claude 로 지정하라"
    fi
    if [ "$WANT_CODEX" -eq 1 ] && [ "$HAVE_CODEX" -eq 0 ] && [ "$AUTO_SELECTED" -eq 0 ]; then
        [ "$FORCE" -eq 1 ] || warn "codex 실행 파일이 없다 — 설정 파일만 준비한다"
    fi
    if [ "$WANT_CLAUDE" -eq 1 ] && [ "$HAVE_CLAUDE" -eq 0 ] && [ "$AUTO_SELECTED" -eq 0 ]; then
        [ "$FORCE" -eq 1 ] || warn "claude 실행 파일이 없다 — 설정 파일만 준비한다"
    fi
}

# --------------------------------------------------------------- hook / 지침
install_hooks() {
    step "hook · 전역 지침"
    if [ "$DRY_RUN" -eq 1 ]; then
        run_context install --dry-run
        return 0
    fi
    if [ "$WITH_GUIDES" -eq 0 ]; then
        set -- install --no-guides
    else
        set -- install
    fi
    if [ "$QUIET" -eq 1 ]; then
        run_context "$@" >/dev/null
    else
        run_context "$@"
    fi
}

uninstall_hooks() {
    step "hook · 전역 지침 제거"
    if [ "$DRY_RUN" -eq 1 ]; then
        plan "llmwiki_context.py install --remove$(client_flags_text)"
        return 0
    fi
    if [ "$QUIET" -eq 1 ]; then
        run_context install --remove >/dev/null
    else
        run_context install --remove
    fi
}

# --------------------------------------------------------------- MCP
hook_interpreter() {
    # 설치기가 고른 인터프리터를 hook 에도 그대로 박는다. 설치를 검증한 것과
    # 질문마다 실제로 도는 것이 같아야 verify 가 의미를 갖는다.
    printf '%s' "$PY"
}

# 우리가 실제로 등록한 MCP 서버만 기억한다. 이 기록이 없으면 이름이 같아도
# 남의 것으로 보고 손대지 않는다. 한 줄에 `클라이언트 <TAB> 이름 <TAB> 스크립트`.
STATE_DIR=${LLMWIKI_STATE_DIR:-$HOME/.llmwiki}
MCP_STATE=$STATE_DIR/installed-mcp

mcp_state_script() { # client name -> 우리가 등록할 때 쓴 스크립트 경로 (없으면 빈 문자열)
    [ -f "$MCP_STATE" ] || return 0
    awk -F'\t' -v c="$1" -v n="$2" '$1 == c && $2 == n { print $3; exit }' "$MCP_STATE"
}

mcp_state_forget() { # client name
    [ -f "$MCP_STATE" ] || return 0
    _tmp=$MCP_STATE.$$
    awk -F'\t' -v c="$1" -v n="$2" '!($1 == c && $2 == n)' "$MCP_STATE" > "$_tmp"
    mv "$_tmp" "$MCP_STATE"
    if [ ! -s "$MCP_STATE" ]; then
        rm -f "$MCP_STATE"
        rmdir "$STATE_DIR" 2>/dev/null || :
    fi
}

mcp_state_record() { # client name script
    mcp_state_forget "$1" "$2"
    mkdir -p "$STATE_DIR"
    printf '%s\t%s\t%s\n' "$1" "$2" "$3" >> "$MCP_STATE"
}

mcp_show() { # client name -> 등록 내용을 그대로 (없으면 비어 있고 exit 1)
    case $1 in
        claude) claude mcp get "$2" 2>/dev/null ;;
        codex) codex mcp get "$2" 2>/dev/null ;;
    esac
}

mcp_points_at() { # client name script -> 등록된 서버가 그 스크립트를 가리키는가
    mcp_show "$1" "$2" | grep -qF -- "$3"
}

mcp_add() { # client name script
    case $1 in
        claude) claude mcp add --scope user "$2" -- "$3" "$4" mcp >/dev/null 2>&1 ;;
        codex) codex mcp add "$2" -- "$3" "$4" mcp >/dev/null 2>&1 ;;
    esac
}

mcp_remove() { # client name
    case $1 in
        claude) claude mcp remove --scope user "$2" >/dev/null 2>&1 ;;
        codex) codex mcp remove "$2" >/dev/null 2>&1 ;;
    esac
}

install_mcp_for() { # client
    _client=$1
    _recorded=$(mcp_state_script "$_client" "$MCP_NAME")
    if ! mcp_show "$_client" "$MCP_NAME" >/dev/null 2>&1; then
        if [ "$DRY_RUN" -eq 1 ]; then
            plan "$_client mcp add $MCP_NAME -- $_py $CONTEXT_CLI mcp"
            return 0
        fi
        if mcp_add "$_client" "$MCP_NAME" "$_py" "$CONTEXT_CLI"; then
            mcp_state_record "$_client" "$MCP_NAME" "$CONTEXT_CLI"
            say "  $_client: 등록 완료"
        else
            warn "$_client mcp add 실패 — 건너뛴다"
        fi
        return 0
    fi
    if [ -z "$_recorded" ]; then
        warn "$_client: 같은 이름의 MCP '$MCP_NAME' 이 이미 있다 (우리가 만든 것이 아니다) — 그대로 둔다"
        return 0
    fi
    if mcp_points_at "$_client" "$MCP_NAME" "$CONTEXT_CLI"; then
        if mcp_points_at "$_client" "$MCP_NAME" "$_py"; then
            say "  $_client: 이미 우리 것 — 그대로 둔다"
            if [ "$DRY_RUN" -eq 0 ]; then
                mcp_state_record "$_client" "$MCP_NAME" "$CONTEXT_CLI"
            fi
            return 0
        fi
        # 우리 서버가 맞는데 인터프리터가 바뀌었다. hook 과 같은 것을 쓰게 맞춘다.
        if [ "$DRY_RUN" -eq 1 ]; then
            plan "$_client mcp remove/add $MCP_NAME — 인터프리터를 $_py 로 맞춘다"
            return 0
        fi
        mcp_remove "$_client" "$MCP_NAME" || :
        if mcp_add "$_client" "$MCP_NAME" "$_py" "$CONTEXT_CLI"; then
            mcp_state_record "$_client" "$MCP_NAME" "$CONTEXT_CLI"
            say "  $_client: 인터프리터를 갱신했다"
        else
            warn "$_client mcp 갱신 실패 — 건너뛴다"
        fi
        return 0
    fi
    if mcp_points_at "$_client" "$MCP_NAME" "$_recorded"; then
        # 우리가 등록한 것이 맞는데 clone 이 옮겨져 경로가 낡았다.
        if [ "$DRY_RUN" -eq 1 ]; then
            plan "$_client mcp remove/add $MCP_NAME — 낡은 경로 $_recorded 갱신"
            return 0
        fi
        mcp_remove "$_client" "$MCP_NAME" || :
        if mcp_add "$_client" "$MCP_NAME" "$_py" "$CONTEXT_CLI"; then
            mcp_state_record "$_client" "$MCP_NAME" "$CONTEXT_CLI"
            say "  $_client: 낡은 경로를 갱신했다"
        else
            warn "$_client mcp 갱신 실패 — 건너뛴다"
        fi
        return 0
    fi
    warn "$_client: MCP '$MCP_NAME' 이 설치 후 다른 서버로 바뀌었다 — 그대로 둔다"
    if [ "$DRY_RUN" -eq 0 ]; then mcp_state_forget "$_client" "$MCP_NAME"; fi
    return 0
}

install_mcp() {
    if [ "$WITH_MCP" -eq 0 ]; then return 0; fi
    step "MCP 서버 ($MCP_NAME, 읽기 전용)"
    _py=$(hook_interpreter)
    if [ "$WANT_CLAUDE" -eq 1 ] && [ "$HAVE_CLAUDE" -eq 1 ]; then
        install_mcp_for claude
    fi
    if [ "$WANT_CODEX" -eq 1 ] && [ "$HAVE_CODEX" -eq 1 ]; then
        install_mcp_for codex
    fi
}

uninstall_mcp_for() { # client
    _client=$1
    _recorded=$(mcp_state_script "$_client" "$MCP_NAME")
    if [ -z "$_recorded" ]; then
        if mcp_show "$_client" "$MCP_NAME" >/dev/null 2>&1; then
            warn "$_client: MCP '$MCP_NAME' 은 우리가 등록한 기록이 없다 — 그대로 둔다"
            case $_client in
                claude) warn "  직접 지우려면: claude mcp remove --scope user $MCP_NAME" ;;
                codex) warn "  직접 지우려면: codex mcp remove $MCP_NAME" ;;
            esac
        else
            say "  $_client: 등록돼 있지 않다"
        fi
        return 0
    fi
    if ! mcp_show "$_client" "$MCP_NAME" >/dev/null 2>&1; then
        say "  $_client: 이미 등록돼 있지 않다"
        if [ "$DRY_RUN" -eq 0 ]; then mcp_state_forget "$_client" "$MCP_NAME"; fi
        return 0
    fi
    if ! mcp_points_at "$_client" "$MCP_NAME" "$_recorded"; then
        warn "$_client: MCP '$MCP_NAME' 이 설치 후 다른 서버로 바뀌었다 — 그대로 둔다"
        if [ "$DRY_RUN" -eq 0 ]; then mcp_state_forget "$_client" "$MCP_NAME"; fi
        return 0
    fi
    if [ "$DRY_RUN" -eq 1 ]; then
        plan "$_client mcp remove $MCP_NAME"
        return 0
    fi
    if mcp_remove "$_client" "$MCP_NAME"; then
        say "  $_client: 해제됨"
    else
        warn "$_client mcp remove 실패 — 기록만 지운다"
    fi
    mcp_state_forget "$_client" "$MCP_NAME"
    return 0
}

uninstall_mcp() {
    if [ "$WITH_MCP" -eq 0 ]; then return 0; fi
    step "MCP 서버 등록 해제"
    if [ "$WANT_CLAUDE" -eq 1 ] && [ "$HAVE_CLAUDE" -eq 1 ]; then
        uninstall_mcp_for claude
    fi
    if [ "$WANT_CODEX" -eq 1 ] && [ "$HAVE_CODEX" -eq 1 ]; then
        uninstall_mcp_for codex
    fi
}

# --------------------------------------------------------------- qmd
# qmd 는 후보 탐색용 보조 색인일 뿐이다. 남이 만든 collection 은 이름이 겹쳐도
# 절대 지우거나 가리키는 곳을 바꾸지 않는다 — 경고하고 손을 뗀다.
qmd_path_of() {
    [ -n "$QMD_BIN" ] || return 0
    "$QMD_BIN" collection show "$1" 2>/dev/null | sed -n 's/^ *Path: *//p' | head -1
}

install_qmd() {
    if [ "$WITH_QMD" -eq 0 ]; then return 0; fi
    step "qmd collection ($QMD_NAME)"
    if ! bootstrap_qmd; then return 0; fi
    if [ "$DRY_RUN" -eq 1 ] && [ -z "$QMD_BIN" ]; then
        plan "qmd 설치 후 collection '$QMD_NAME' 준비"
        return 0
    fi
    _target=$REPO_ROOT/index/markdown
    if [ "$DRY_RUN" -eq 1 ]; then
        plan "llmwiki.py export-md  ->  $_target"
        _existing=$(qmd_path_of "$QMD_NAME")
        if [ -z "$_existing" ]; then
            plan "qmd collection add $_target --name $QMD_NAME"
        elif [ "$_existing" = "$_target" ]; then
            plan "qmd update -c $QMD_NAME  (이미 이 저장소를 가리킨다)"
        else
            plan "건너뜀 — '$QMD_NAME' 이 이미 $_existing 를 가리킨다"
        fi
        return 0
    fi
    "$PY" "$WIKI_CLI" export-md >/dev/null
    say "  markdown 재생성 완료: $_target"
    _existing=$(qmd_path_of "$QMD_NAME")
    if [ -z "$_existing" ]; then
        if qmd_cmd collection add "$_target" --name "$QMD_NAME" >/dev/null 2>&1; then
            say "  collection 생성 완료"
        else
            warn "qmd collection add 실패"
        fi
    elif [ "$_existing" = "$_target" ]; then
        if qmd_cmd update -c "$QMD_NAME" >/dev/null 2>&1; then
            say "  기존 collection 재색인 완료"
        else
            warn "qmd update 실패"
        fi
    else
        warn "'$QMD_NAME' 은 이미 $_existing 를 가리킨다 — 건드리지 않는다."
        warn "  다른 이름을 쓰려면: --qmd-name <새이름>"
    fi
}

# --------------------------------------------------------------- 주기 ingest 루틴
# 루틴은 raw/ 에 새 소스가 들어왔을 때만 에이전트를 부른다. 매시간 LLM 을
# 깨우는 것이 목적이 아니므로, 미처리 목록이 지난번과 같으면 그냥 넘어간다.
ask_line() { # 질문 -> 답 (비대화형이면 빈 값)
    [ "$ASK" -eq 1 ] || return 0
    [ -t 0 ] || return 0
    printf '%s' "$1" >&2
    IFS= read -r _reply || return 0
    printf '%s' "$_reply"
}

routine_choice() {
    # 플래그로 정해졌으면 묻지 않는다.
    case $ROUTINE in
        claude | codex | none) return 0 ;;
    esac
    if [ "$ASK" -eq 0 ] || [ ! -t 0 ]; then
        ROUTINE=none
        return 0
    fi
    say ""
    say "  raw/ 에 새 소스가 들어오면 에이전트가 자동으로 위키에 넣는 루틴을 걸 수 있다."
    say "  (git pull -> 미처리 확인 -> ingest -> build/validate -> 커밋 -> push 순서로 돈다)"
    _options=""
    if [ "$HAVE_CLAUDE" -eq 1 ]; then _options="claude"; fi
    if [ "$HAVE_CODEX" -eq 1 ]; then _options="${_options:+$_options/}codex"; fi
    if [ -z "$_options" ]; then
        say "  claude 도 codex 도 없다 — 루틴은 건너뛴다."
        ROUTINE=none
        return 0
    fi
    _answer=$(ask_line "  루틴을 걸까? [$_options/none] (기본 none): ")
    case $_answer in
        claude | codex) ROUTINE=$_answer ;;
        *) ROUTINE=none ;;
    esac
}

install_routine() {
    routine_choice
    [ "$ROUTINE" = none ] && return 0
    step "주기 ingest 루틴 ($ROUTINE, ${ROUTINE_INTERVAL}초)"
    if [ ! -f "$ROUTINE_CLI" ]; then
        warn "$ROUTINE_CLI 가 없다 — 루틴을 건너뛴다"
        return 0
    fi
    set -- run
    if [ "$DRY_RUN" -eq 1 ]; then
        "$PY" "$ROUTINE_CLI" --root "$REPO_ROOT" install --agent "$ROUTINE" \
            --interval "$ROUTINE_INTERVAL" --python "$PY" --remote "$REMOTE_NAME" --dry-run
        return 0
    fi
    "$PY" "$ROUTINE_CLI" --root "$REPO_ROOT" install --agent "$ROUTINE" \
        --interval "$ROUTINE_INTERVAL" --python "$PY" --remote "$REMOTE_NAME" ||
        warn "루틴 등록에 실패했다 — 나머지 설치는 그대로다"
}

uninstall_routine() {
    step "주기 ingest 루틴 해제"
    if [ ! -f "$ROUTINE_CLI" ]; then
        say "  루틴 스크립트가 없다 — 건너뛴다"
        return 0
    fi
    if [ "$DRY_RUN" -eq 1 ]; then
        "$PY" "$ROUTINE_CLI" uninstall --dry-run
        return 0
    fi
    "$PY" "$ROUTINE_CLI" uninstall || warn "루틴 해제에 실패했다"
}

# --------------------------------------------------------------- 개인 저장소
# origin 은 코드 갱신을 받는 자리로 그대로 둔다. 개인 위키는 별도 remote 로만
# 밀어 올린다. 이미 붙어 있는 remote 는 이름이 같아도 덮어쓰지 않는다.
private_choice() {
    case $PRIVATE in
        yes | no) return 0 ;;
    esac
    if [ "$ASK" -eq 0 ] || [ ! -t 0 ]; then
        PRIVATE=no
        return 0
    fi
    say ""
    say "  이 clone 은 origin 에서 코드 갱신을 받는 자리로 둔다."
    say "  자기 위키는 개인 private 저장소로 밀어 올릴 수 있다."
    _answer=$(ask_line "  개인 private 저장소를 붙일까? [y/N]: ")
    case $_answer in
        y | Y | yes | YES) PRIVATE=yes ;;
        *) PRIVATE=no; return 0 ;;
    esac
    _answer=$(ask_line "  이미 만든 저장소 주소가 있으면 붙여 넣어라 (없으면 그냥 Enter): ")
    if [ -n "$_answer" ]; then
        PRIVATE_URL=$_answer
        return 0
    fi
    if has gh; then
        _answer=$(ask_line "  gh 로 새로 만든다. 저장소 이름 (기본 llmwiki-private): ")
        GH_CREATE=${_answer:-llmwiki-private}
    else
        warn "gh 가 없어 새로 만들 수 없다 — 주소를 주거나 gh 를 설치해라"
        PRIVATE=no
    fi
}

install_private_remote() {
    private_choice
    [ "$PRIVATE" = yes ] || return 0
    step "개인 저장소 remote ($REMOTE_NAME)"
    if [ ! -f "$ROUTINE_CLI" ]; then
        warn "$ROUTINE_CLI 가 없다 — 건너뛴다"
        return 0
    fi
    set -- git-setup --remote "$REMOTE_NAME"
    if [ -n "$PRIVATE_URL" ]; then set -- "$@" --url "$PRIVATE_URL"; fi
    if [ -n "$GH_CREATE" ]; then set -- "$@" --gh-create "$GH_CREATE"; fi
    if [ "$DRY_RUN" -eq 1 ]; then set -- "$@" --dry-run; fi
    "$PY" "$ROUTINE_CLI" --root "$REPO_ROOT" "$@" ||
        warn "개인 저장소 연결에 실패했다 — 나머지 설치는 그대로다"
}

# --------------------------------------------------------------- update
do_update() {
    step "upstream 갱신 (origin)"
    if [ ! -d "$REPO_ROOT/.git" ]; then
        die "git 저장소가 아니다: $REPO_ROOT"
    fi
    _dirty=$(git -C "$REPO_ROOT" status --porcelain | head -n 5)
    if [ -n "$_dirty" ]; then
        warn "커밋되지 않은 변경이 있다 — 먼저 정리하라"
        printf '%s\n' "$_dirty" | sed 's/^/    /'
        return 1
    fi
    if ! git -C "$REPO_ROOT" remote get-url origin >/dev/null 2>&1; then
        warn "remote 'origin' 이 없다 — 이 clone 은 upstream 을 가리키고 있지 않다"
        warn "  붙이려면: git -C $REPO_ROOT remote add origin <upstream 주소>"
        return 1
    fi
    _branch=$(git -C "$REPO_ROOT" rev-parse --abbrev-ref HEAD)
    if [ "$DRY_RUN" -eq 1 ]; then
        plan "git fetch origin && git merge --ff-only origin/$_branch"
        return 0
    fi
    git -C "$REPO_ROOT" fetch origin "$_branch" || return 1
    if git -C "$REPO_ROOT" merge --ff-only "origin/$_branch"; then
        say "  최신으로 맞췄다"
    else
        warn "ff-only 로 합칠 수 없다 (갈라졌다). 직접 확인하라:"
        warn "  git -C $REPO_ROOT log --oneline HEAD..origin/$_branch"
        return 1
    fi
    say "  개인 위키를 밀어 올리려면: git push $REMOTE_NAME HEAD:$_branch"
}

# --------------------------------------------------------------- Codex 신뢰
codex_trust_notice() {
    if [ "$WANT_CODEX" -eq 0 ]; then return 0; fi
    _state=$("$PY" - "$CONTEXT_CLI" <<'EOF'
import importlib.util, sys
spec = importlib.util.spec_from_file_location("llmwiki_context", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
sys.modules["llmwiki_context"] = mod
spec.loader.exec_module(mod)
hooks, _ = mod.client_paths("codex")
print(mod.codex_trust(hooks, mod.installed_group_index(hooks)))
EOF
    )
    step "Codex hook 신뢰: $_state"
    case $_state in
        trusted) say "  이미 신뢰된 hook 이다. 그대로 동작한다." ;;
        *)
            say "  Codex 0.148 이상은 새 hook 을 신뢰하기 전까지 그 출력을 무시한다."
            say "  한 번만 아래를 하면 된다:"
            say "    1) codex 를 실행한다"
            say "    2) 'Hooks need review' 프롬프트에서 hook 을 확인한다"
            say "       (명령이 …/llmwiki_context.py hook 인지 볼 것)"
            say "    3) t 를 눌러 신뢰한다"
            say "  확인:  scripts/install.sh verify"
            ;;
    esac
}

# --------------------------------------------------------------- 명령 실행
# 도구를 받아 오는 것은 install 에서만이다. doctor 는 진단, verify 는 점검,
# uninstall 은 제거 — 어느 쪽도 사용자 기계에 무언가를 설치할 이유가 없다.
if [ "$COMMAND" = install ]; then
    ALLOW_TOOL_INSTALL=1
else
    ALLOW_TOOL_INSTALL=0
fi

# 인터프리터를 먼저 확정한다. 설정 파일을 쓰기 전에 필요한 도구를 전부
# 마련해 두어야, 중간에 실패해도 남의 설정이 반쯤 바뀐 채로 남지 않는다.
resolve_python
PYTHON=$PY

case $COMMAND in
    doctor)
        report_detection
        if [ -z "$PY" ]; then
            warn "Python 3.9 이상이 없다 — install 을 돌리면 uv 로 마련한다"
            exit 0
        fi
        step "해석된 설정"
        "$PY" "$CONTEXT_CLI" doctor
        ;;
    update)
        [ -n "$PY" ] || die "Python 3.9 이상이 없다"
        do_update
        ;;
    verify)
        report_detection
        [ -n "$PY" ] || die "Python 3.9 이상이 없어 점검할 수 없다 — 먼저 install 을 돌려라"
        step "점검"
        run_context verify || _verify_failed=1
        if [ -f "$ROUTINE_CLI" ]; then
            step "주기 ingest 루틴"
            "$PY" "$ROUTINE_CLI" --root "$REPO_ROOT" status
        fi
        [ -z "${_verify_failed-}" ]
        ;;
    install)
        report_detection
        require_targets
        # 네트워크가 필요한 일은 전부 여기서 끝낸다 — 아래부터는 설정 쓰기다.
        if [ "$WITH_QMD" -eq 1 ]; then
            step "도구 준비"
            bootstrap_qmd || :
        fi
        if [ -z "$PY" ]; then
            # dry-run 이면서 쓸 Python 이 아직 없는 경우다. 위에 계획은 다 찍었고,
            # 그 다음 계획은 Python 이 있어야 만들 수 있다.
            step "dry-run 종료 — 아무것도 바꾸지 않았다"
            say "  (자세한 hook 계획은 Python 이 마련된 뒤에 볼 수 있다)"
            exit 0
        fi
        install_hooks
        install_mcp
        install_qmd
        install_private_remote
        install_routine
        if [ "$DRY_RUN" -eq 1 ]; then
            step "dry-run 종료 — 아무것도 바꾸지 않았다"
            exit 0
        fi
        codex_trust_notice
        step "확인"
        run_context verify || true
        step "완료"
        say "  되돌리려면:  $SCRIPT_DIR/install.sh uninstall"
        say "  코드 갱신:   $SCRIPT_DIR/install.sh update   (origin 에서 받아 온다)"
        ;;
    uninstall)
        report_detection
        [ -n "$PY" ] || die "Python 3.9 이상이 없어 제거를 실행할 수 없다"
        uninstall_hooks
        uninstall_mcp
        uninstall_routine
        if [ "$WITH_QMD" -eq 1 ]; then
            step "qmd"
            warn "collection 과 받아 온 도구(uv/Bun/qmd)는 자동으로 지우지 않는다."
            warn "  collection 을 정말 지우려면: qmd collection remove $QMD_NAME"
        fi
        if [ "$DRY_RUN" -eq 0 ]; then
            step "남은 백업"
            for f in "$HOME/.codex/hooks.json.llmwiki-bak" \
                "$HOME/.claude/settings.json.llmwiki-bak" \
                "$HOME/.codex/AGENTS.md.llmwiki-bak" \
                "$HOME/.claude/CLAUDE.md.llmwiki-bak"; do
                if [ -f "$f" ]; then say "  $f"; fi
            done
            say "  설치 직전 상태로 통째 되돌리려면 위 파일을 원래 이름으로 복사하라."
        fi
        ;;
esac

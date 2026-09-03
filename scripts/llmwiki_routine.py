#!/usr/bin/env python3
"""주기적 ingest 루틴 — raw/ 에 새 소스가 들어오면 에이전트를 불러 위키에 넣는다.

설계 원칙은 hook 과 같다. 조용하고, 실패해도 남의 것을 망가뜨리지 않으며,
우리가 만든 것만 되돌린다.

  run        한 번 돈다 (스케줄러가 부르는 것). 지난 차례가 남긴 것을 먼저
             마무리하고, 그 다음이 언제나 git pull 이다.
  install    에이전트 자신의 주기 작업으로 등록한다
  uninstall  우리가 등록한 것만 지운다
  status     등록 상태와 마지막 실행 결과
  git-setup  개인 private 저장소를 remote 로 붙인다 (origin 은 그대로 둔다)
  pending    미처리 raw 소스 목록만 출력한다

에이전트는 사람이 고른다. claude 는 `/ingest` 스킬을, codex 는 같은 뜻의
프롬프트를 받는다. 미처리 소스가 없으면 에이전트를 아예 부르지 않는다 —
매시간 LLM 을 깨우는 것이 이 루틴의 목적이 아니다.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Iterable

DEFAULT_ROOT = Path(__file__).resolve().parents[1]

ENV_ROOT = "LLMWIKI_ROOT"
ENV_STATE_DIR = "LLMWIKI_STATE_DIR"
ENV_NOW = "LLMWIKI_NOW"
# 에이전트 주기 작업이 놓이는 홈. 시험이 진짜 ~/.claude 를 건드리지 않게 한다.
ENV_TASK_HOME = "LLMWIKI_TASK_HOME"

AGENTS = ("claude", "codex")

# 주기는 에이전트가 자기 자리에서 부른다. 인증·모델·권한이 이미 갖춰져 있고,
# 실패도 그 에이전트가 그대로 읽는다. OS 스케줄러는 쓰지 않는다.
AGENT_TASK_DIRS = {"claude": Path(".claude/scheduled-tasks"),
                   "codex": Path(".codex/scheduled-tasks")}
TASK_NAME = "llmwiki-ingest"
TASK_MARKER = "<!-- llmwiki-routine -->"
DEFAULT_INTERVAL = 3600
LABEL = "com.llmwiki.ingest"
REMOTE = "private"
LOCK_STALE_SECONDS = 6 * 3600

# 안 들어간 소스는 들어갈 때까지 다시 시도한다. 포기하는 자리는 없다.
# 간격만 늘려 매시간 같은 것으로 LLM 을 깨우지 않게 한다 — 하루에 한 번은 반드시.
RETRY_BASE_SECONDS = 3600
RETRY_MAX_SECONDS = 24 * 3600

# build·validate 가 깨졌을 때 에이전트에게 되돌리는 지시. 멈춰 세우는 대신
# 고쳐서 넣게 한다 — 깨진 채로 두면 그 소스는 영영 위키에 못 들어간다.
REPAIR_PROMPT = (
    "방금 ingest 한 결과가 {stage} 에서 깨졌다. 아래가 그 출력이다:\n\n{detail}\n\n"
    "wiki/**/*.json 정본만 고쳐서 통과하게 만들어라. tools/schema/page.schema.json 이 "
    "스키마 정본이다. raw/ 와 index/ · viewer/public/data 는 건드리지 마라 "
    "(파생물은 루틴이 다시 만든다). 고칠 수 없는 page 는 지우지 말고 무엇이 왜 "
    "안 되는지 마지막에 적어라."
)

# 에이전트에게 주는 지시. 스킬이 있으면 그것을 쓰고, 없으면 이 문장이 곧 명세다.
INGEST_PROMPT = (
    "raw/ 에 아직 위키에 들어가지 않은 소스가 있다. 저장소 규칙(CLAUDE.md / AGENTS.md)에 "
    "따라 ingest 해라: 원문을 읽고 page 를 만들거나 갱신하고, 관련 page 와 "
    "source/entity/concept/synthesis/project 관계를 잇고, wiki/log.jsonl 에 기록해라. "
    "page 마다 raw_ref 에 원본의 저장소 상대경로(raw/…)를 반드시 적어라 — 이 자리가 비면 "
    "루틴은 그 소스를 아직 넣지 않은 것으로 보고 계속 다시 부른다. "
    "build · validate 는 이 루틴이 네 뒤에 직접 돌린다. 무인 실행이라 셸 명령은 "
    "허용되지 않을 수 있으니 파일을 쓰는 것으로 끝내라. raw/ 는 수정하지 마라. "
    "보안 정보는 저장하지 말고 '(접속 정보 생략)' 으로 치환해라. "
    "판단이 서지 않는 소스는 건너뛰고 무엇을 왜 건너뛰었는지 마지막에 적어라."
)

# ingest 로 바뀌어야 하는 것만 커밋한다. index/ 와 viewer 산출물은 build 가 만든다.
# groups.json 은 모르는 프로젝트를 만났을 때 ingest 가 직접 등록하는 파일이라,
# 빼 두면 커밋되지 않은 채 남아 다음 차례가 dirty tree 에서 멈춘다.
COMMIT_PATHS = ("wiki", "index", "viewer/public/data", "tools/config/groups.json")

# raw/ 에 있어도 소스가 아닌 것들. `raw/.llmwikiignore` 로 더 넣을 수 있다.
DEFAULT_IGNORE = ("README*", "readme*", "*.gitkeep", "*.lock", "*.tmp", "*.part",
                  "*.ds_store", ".DS_Store")


# --------------------------------------------------------------------------- 기본
def now() -> float:
    override = os.environ.get(ENV_NOW)
    if override:
        try:
            return float(override)
        except ValueError:
            pass
    return time.time()


def stamp(value: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(value if value is not None else now()))


def state_dir() -> Path:
    override = os.environ.get(ENV_STATE_DIR)
    return Path(override) if override else Path.home() / ".llmwiki"


def state_file() -> Path:
    return state_dir() / "installed-routine"


def log_file() -> Path:
    return state_dir() / "routine.log"


def resolve_root(explicit: str | None) -> Path:
    return Path(explicit or os.environ.get(ENV_ROOT) or DEFAULT_ROOT).resolve()


def log(message: str) -> None:
    line = f"{stamp()} {message}"
    try:
        state_dir().mkdir(parents=True, exist_ok=True)
        with open(log_file(), "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass
    # 진행 로그는 stdout 이 아니다. --json 의 출력과 섞이면 파이프가 깨진다.
    print(line, file=sys.stderr)


def run(argv: list[str], *, cwd: Path | None = None, timeout: float | None = None,
        env: dict[str, str] | None = None) -> tuple[int, str]:
    """외부 명령 하나. 죽지 않고 (코드, 출력) 을 돌려준다."""
    try:
        proc = subprocess.run(argv, cwd=str(cwd) if cwd else None, capture_output=True,
                              text=True, timeout=timeout,
                              env={**os.environ, **(env or {})} if env else None)
    except FileNotFoundError:
        return 127, f"명령을 찾을 수 없다: {argv[0]}"
    except subprocess.TimeoutExpired:
        return 124, f"시간 초과: {' '.join(argv[:3])}"
    except OSError as exc:  # noqa: BLE001
        return 1, str(exc)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def git(root: Path, *args: str, timeout: float = 120.0) -> tuple[int, str]:
    return run(["git", *args], cwd=root, timeout=timeout)


# --------------------------------------------------------------------------- 미처리 소스
def norm_ref(value: Any) -> str:
    """경로처럼 생긴 값을 저장소 상대경로 모양으로 다듬는다."""
    if not isinstance(value, str):
        return ""
    text = value.strip().replace("\\", "/")
    if text.startswith("source:"):
        text = text[len("source:"):]
    while text.startswith("./"):
        text = text[2:]
    return text.strip()


def page_raw_refs(page: dict[str, Any]) -> set[str]:
    """한 page 가 원본으로 지목하는 경로들.

    `raw_ref` 한 자리만 보지 않는다. page 를 손으로 쓴 에이전트가 그 자리를
    비워 두는 일이 잦은데, 그러면 이미 넣은 소스가 영원히 '미처리' 로 남는다.
    """
    out = {norm_ref(page.get("raw_ref"))}
    snapshot = page.get("source_snapshot")
    if isinstance(snapshot, dict):
        out.update(norm_ref(snapshot.get(key)) for key in ("raw_ref", "path", "source"))
    for entry in page.get("history") or []:
        if isinstance(entry, dict):
            out.update(norm_ref(entry.get(key)) for key in ("note", "raw", "raw_ref", "source"))
    for value in page.get("sources") or []:
        out.add(norm_ref(value))
    out.discard("")
    return out


def is_ingested(rel: str, refs: set[str]) -> bool:
    """rel 은 'raw/…' 저장소 상대경로. 절대경로로 적힌 것도 같은 것으로 본다."""
    if rel in refs:
        return True
    tail = "/" + rel
    return any(ref.endswith(tail) for ref in refs)


def wiki_raw_refs(root: Path) -> set[str]:
    """정본이 이미 가리키고 있는 raw 경로. 깨진 shard 는 건너뛴다."""
    refs: set[str] = set()
    wiki = root / "wiki"
    if not wiki.is_dir():
        return refs
    for path in sorted(wiki.rglob("*.json")):
        if path.name.startswith("."):
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for page in value if isinstance(value, list) else [value]:
            if isinstance(page, dict):
                refs.update(page_raw_refs(page))
    return refs


def ignore_patterns(root: Path) -> list[str]:
    """`raw/.llmwikiignore` 의 glob 목록. 없으면 기본값만."""
    patterns = list(DEFAULT_IGNORE)
    path = root / "raw" / ".llmwikiignore"
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                patterns.append(line)
    except OSError:
        pass
    return patterns


def raw_sources(root: Path) -> list[str]:
    """raw/ 안의 실제 소스 파일. 숨김 파일과 안내 문서는 세지 않는다."""
    raw = root / "raw"
    if not raw.is_dir():
        return []
    patterns = ignore_patterns(root)
    out = []
    for path in sorted(raw.rglob("*")):
        # 숨김 파일뿐 아니라 숨김 폴더 안의 것도 소스가 아니다. raw/.git 이나
        # raw/.obsidian 을 세면 영원히 처리되지 않는 미처리가 계속 쌓인다.
        if not path.is_file() or any(part.startswith(".")
                                     for part in path.relative_to(raw).parts):
            continue
        rel = path.relative_to(root).as_posix()
        tail = path.relative_to(raw).as_posix()
        if any(fnmatch(rel, p) or fnmatch(tail, p) or fnmatch(path.name, p)
               for p in patterns):
            continue
        out.append(rel)
    return out


def pending_sources(root: Path) -> list[str]:
    refs = wiki_raw_refs(root)
    return [rel for rel in raw_sources(root) if not is_ingested(rel, refs)]


# --------------------------------------------------------------------------- git
def has_remote(root: Path, name: str) -> str:
    code, out = git(root, "remote", "get-url", name, timeout=15.0)
    return out.strip() if code == 0 else ""


def current_branch(root: Path) -> str:
    """붙어 있는 브랜치 이름. detached HEAD 면 빈 문자열."""
    code, out = git(root, "rev-parse", "--abbrev-ref", "HEAD", timeout=15.0)
    name = out.strip() if code == 0 else ""
    # detached 에서는 이 명령이 "HEAD" 를 준다. 그대로 쓰면 push HEAD:HEAD 가 된다.
    return "" if name == "HEAD" else name


def is_git_repo(root: Path) -> bool:
    code, out = git(root, "rev-parse", "--is-inside-work-tree", timeout=15.0)
    return code == 0 and out.strip() == "true"


def remote_branch_state(root: Path, remote: str, branch: str) -> str:
    """'present' · 'absent' · 'unknown'. 조회 실패를 '없음' 으로 읽지 않는다."""
    code, out = git(root, "ls-remote", "--heads", remote, branch, timeout=60.0)
    if code != 0:
        return "unknown"
    return "present" if out.strip() else "absent"


def pull_first(root: Path, remote: str, branch: str) -> tuple[bool, str]:
    """루틴의 첫 단계. 남의 커밋 위에서 일하게 만들고, 어긋나면 멈춘다."""
    # git 저장소가 아니면 git 단계 전체가 성립하지 않는다. 로컬 전용으로 쓰는
    # 경우라 진행은 하되, 아래 검사들은 건너뛴다.
    if not is_git_repo(root):
        return True, "git 저장소가 아니다 — pull·커밋 단계를 건너뛴다"
    # 더러운 워킹트리 검사가 remote 보다 먼저다. 사람이 편집하던 중이면
    # 에이전트를 그 위에 풀어놓지 않는다. status 자체가 실패하면 멈춘다.
    # 검사 범위는 루틴이 직접 쓰는 경로뿐이다. viewer/ 를 고치는 중이라고 해서
    # 위키 ingest 까지 세울 이유는 없다 — 그렇게 하면 손대는 김에 루틴이 멈춘다.
    code, out = git(root, "status", "--porcelain", "--", *COMMIT_PATHS)
    if code != 0:
        return False, f"git status 실패 — 상태를 알 수 없어 멈춘다: {out.splitlines()[-1] if out else code}"
    if out.strip():
        return False, "워킹트리에 커밋되지 않은 변경이 있다 — 이번 차례는 건너뛴다"
    if not has_remote(root, remote):
        return True, f"remote '{remote}' 없음 — pull 건너뜀"
    state = remote_branch_state(root, remote, branch)
    if state == "unknown":
        # 인증 실패·네트워크 단절이다. 낡은 트리 위에서 ingest 하지 않는다.
        return False, f"{remote} 를 조회할 수 없다 (인증·네트워크) — 이번 차례는 건너뛴다"
    if state == "absent":
        # 아직 한 번도 밀어 올린 적이 없는 저장소다. 여기서 막으면 최초 push 에
        # 영영 도달하지 못한다.
        return True, f"{remote} 에 {branch} 가 아직 없다 — 최초 push 로 만든다"
    code, out = git(root, "fetch", remote, branch, timeout=180.0)
    if code != 0:
        return False, f"fetch 실패: {out.splitlines()[-1] if out else code}"
    code, out = git(root, "merge", "--ff-only", f"{remote}/{branch}", timeout=120.0)
    if code != 0:
        return False, f"ff-only merge 실패(갈라졌다): {out.splitlines()[-1] if out else code}"
    return True, out.strip().splitlines()[-1] if out.strip() else "이미 최신"


def unpushed_commits(root: Path, remote: str, branch: str) -> int:
    """원격에 아직 없는 로컬 커밋 수. push 가 실패한 채로 잊히지 않게 한다."""
    if not is_git_repo(root) or not has_remote(root, remote):
        return 0
    if remote_branch_state(root, remote, branch) != "present":
        # 원격 브랜치가 없으면 로컬 커밋 전부가 아직 밀리지 않은 것이다.
        code, out = git(root, "rev-list", "--count", "HEAD", timeout=60.0)
        return int(out.strip() or 0) if code == 0 and out.strip().isdigit() else 0
    code, out = git(root, "rev-list", "--count", f"{remote}/{branch}..HEAD", timeout=60.0)
    return int(out.strip() or 0) if code == 0 and out.strip().isdigit() else 0


def push_pending(root: Path, remote: str, branch: str) -> tuple[bool, str]:
    code, out = git(root, "push", remote, f"HEAD:{branch}", timeout=300.0)
    if code != 0:
        return False, f"밀리지 않은 커밋 push 실패: {out.splitlines()[-1] if out else code}"
    return True, f"밀리지 않았던 커밋을 {remote}/{branch} 로 밀었다"


def changed_paths(root: Path) -> list[str]:
    code, out = git(root, "status", "--porcelain", "--", *COMMIT_PATHS)
    if code != 0:
        return []
    return [line[3:].strip() for line in out.splitlines() if line.strip()]


def commit_and_push(root: Path, remote: str, branch: str,
                    *, push: bool) -> tuple[bool, str]:
    """(성공 여부, 설명). 실패를 성공으로 보고하지 않는다."""
    if not is_git_repo(root):
        return True, "git 저장소가 아니다 — 커밋하지 않는다"
    changed = changed_paths(root)
    if not changed:
        return True, "바뀐 정본 없음 — 커밋하지 않는다"
    code, out = git(root, "add", "--", *COMMIT_PATHS)
    if code != 0:
        return False, f"git add 실패: {out}"
    message = f"위키를 자동으로 최신 소스에 맞춘다 ({stamp()})"
    code, out = git(root, "commit", "-m", message)
    if code != 0:
        return False, f"git commit 실패: {out.splitlines()[-1] if out else code}"
    if not push:
        return True, f"{len(changed)}개 경로 커밋 (push 안 함)"
    if not has_remote(root, remote):
        return True, f"{len(changed)}개 경로 커밋 — remote '{remote}' 가 없어 push 하지 않는다"
    code, out = git(root, "push", remote, f"HEAD:{branch}", timeout=300.0)
    if code != 0:
        return False, f"{len(changed)}개 경로 커밋 — push 실패: {out.splitlines()[-1] if out else code}"
    return True, f"{len(changed)}개 경로 커밋 후 {remote}/{branch} 로 push"


def rebuild(root: Path) -> tuple[str, str]:
    """파생물을 다시 만들고 검증한다. ('', 설명) 이면 통과."""
    for stage in ("build", "validate"):
        code, out = run([sys.executable, str(root / "scripts" / "llmwiki.py"), stage],
                        cwd=root, timeout=600.0)
        if code != 0:
            return stage, f"{stage} 실패: {out[-200:]}"
    return "", "build · validate 통과"


def rebuild_or_repair(root: Path, agent: str, timeout: float,
                      note: Any = None) -> tuple[str, str]:
    """파생물을 만들고, 깨졌으면 에이전트에게 고치게 한 뒤 한 번 더 본다."""
    stage, detail = rebuild(root)
    if not stage:
        return "", detail
    order = [agent] + [a for a in AGENTS if a != agent]
    for name in order:
        if not agent_available(name):
            continue
        if note:
            note("repair", f"{stage} 가 깨졌다 — {name} 에게 고치라고 되돌린다")
        code, out = run(agent_argv(name, REPAIR_PROMPT.format(stage=stage, detail=detail)),
                        cwd=root, timeout=timeout)
        stage, detail = rebuild(root)
        if not stage:
            return "", f"{name} 가 고쳤다 — {detail}"
        if code != 0:
            continue  # 이 에이전트가 실패했다 — 다른 쪽으로 한 번 더
        break
    return stage, detail


def run_agent(root: Path, agent: str, prompt: str, timeout: float,
              note: Any = None) -> tuple[int, str, str]:
    """고른 에이전트로 넣어 본다. 그쪽이 없거나 실패하면 다른 에이전트로 한 번 더.

    넣는 것이 목적이지 특정 에이전트를 부르는 것이 목적이 아니다.
    """
    order = [agent] + [a for a in AGENTS if a != agent]
    code, out, used = 127, f"쓸 수 있는 에이전트가 없다: {', '.join(order)}", agent
    for name in order:
        if not agent_available(name):
            if note and name == agent:
                note("agent", f"{name} 실행 파일이 없다 — 다른 에이전트를 찾는다")
            continue
        code, out = run(agent_argv(name, prompt), cwd=root, timeout=timeout)
        used = name
        if code == 0:
            return code, out, used
        if note:
            note("agent", f"{name} 종료 코드 {code} — 다른 에이전트로 한 번 더 해 본다")
    return code, out, used


def finish_leftover(root: Path, remote: str, branch: str, *, agent: str = "claude",
                    timeout: float = 3600.0, note: Any = None) -> tuple[bool, str]:
    """지난 차례가 끊기며 남긴 변경을 먼저 마무리한다.

    build 가 깨졌든 에이전트가 도중에 죽었든, 커밋되지 못한 변경이 남으면 그
    다음 차례부터는 전부 '더러운 트리' 에서 멈춘다. 한 번의 실패가 루틴을 영영
    세우고 미처리만 쌓이는 자리가 여기다. 직전에 에이전트를 불렀다는 표시가
    있을 때만 — 즉 우리가 남긴 것이 분명할 때만 — 이어서 끝낸다. push 는 하지
    않는다. pull 다음의 push-pending 이 순서를 지켜 민다.
    """
    if not is_git_repo(root):
        return True, ""
    dirty = changed_paths(root)
    if not dirty:
        clear_inflight(root)
        return True, ""
    if not read_inflight(root):
        return True, ""  # 우리 것이 아니다 — 아래 더러운 트리 검사에 맡긴다
    stage, detail = rebuild_or_repair(root, agent, timeout, note)
    if stage:
        return False, f"지난 차례가 남긴 {len(dirty)}개 경로를 끝내지 못했다 — {detail}"
    ok, detail = commit_and_push(root, remote, branch, push=False)
    if ok:
        clear_inflight(root)
    return ok, f"지난 차례가 남긴 {len(dirty)}개 경로 — {detail}"


# --------------------------------------------------------------------------- 에이전트
def agent_argv(agent: str, prompt: str) -> list[str]:
    if agent == "claude":
        return ["claude", "-p", prompt, "--permission-mode", "acceptEdits"]
    if agent == "codex":
        # 사용자 config 의 기본 sandbox 가 read-only 면 무인 ingest 는 한 줄도
        # 쓰지 못한 채 매번 조용히 끝난다. 쓸 수 있는 범위를 명시한다.
        return ["codex", "exec", "--skip-git-repo-check",
                "--sandbox", "workspace-write", prompt]
    raise ValueError(f"unknown agent: {agent}")


def agent_available(agent: str) -> bool:
    return shutil.which(agent) is not None


# --------------------------------------------------------------------------- 잠금
def lock_file(root: Path) -> Path:
    return state_dir() / f"routine-{root_key(root)}.lock"


def lock_owner_alive(path: Path) -> bool:
    """락을 쥔 프로세스가 아직 살아 있나. 확인할 수 없으면 살아 있다고 본다."""
    if os.name != "posix":
        # Windows 에서 os.kill(pid, 0) 은 확인이 아니라 종료다. 시간에 맡긴다.
        return True
    try:
        pid = int(path.read_text(encoding="utf-8").strip() or 0)
    except (OSError, ValueError):
        return True
    if pid <= 0 or pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def acquire_lock(root: Path) -> Path | None:
    """겹쳐 도는 것을 막는다. 주인이 죽은 락은 곧바로 회수한다."""
    path = lock_file(root)
    try:
        state_dir().mkdir(parents=True, exist_ok=True)
        if path.exists() and (now() - path.stat().st_mtime > LOCK_STALE_SECONDS
                              or not lock_owner_alive(path)):
            # 재부팅·강제 종료로 주인이 사라진 락 하나가 여섯 시간을 잡아먹는다.
            path.unlink()
        handle = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return None
    except OSError:
        return None
    with os.fdopen(handle, "w") as fh:
        fh.write(str(os.getpid()))
    return path


# --------------------------------------------------------------------------- run
def do_run(root: Path, *, agent: str, remote: str, push: bool, dry_run: bool,
           timeout: float) -> dict[str, Any]:
    report: dict[str, Any] = {"at": stamp(), "root": str(root), "agent": agent,
                              "steps": [], "ingested": False}

    def step(name: str, detail: str, ok: bool = True) -> None:
        report["steps"].append({"step": name, "ok": ok, "detail": detail})
        log(f"[{name}] {detail}")

    lock = None if dry_run else acquire_lock(root)
    if not dry_run and lock is None:
        step("lock", "다른 루틴이 돌고 있다 — 이번 차례는 건너뛴다", ok=False)
        report["skipped"] = "locked"
        return report
    try:
        branch = current_branch(root)
        if not branch and is_git_repo(root):
            step("branch", "detached HEAD 다 — 어디에 밀지 알 수 없어 멈춘다", ok=False)
            report["stopped"] = "detached-head"
            return report
        branch = branch or "main"
        report["branch"] = branch

        # 0. 지난 차례가 끊기며 남긴 변경을 먼저 마무리한다. 그대로 두면 아래
        # 더러운 트리 검사에 걸려, 한 번의 실패가 이후의 모든 차례를 세운다.
        if not dry_run:
            ok, detail = finish_leftover(root, remote, branch, agent=agent,
                                         timeout=timeout, note=step)
            if detail:
                step("leftover", detail, ok=ok)
            if not ok:
                report["stopped"] = "leftover"
                return report

        # 1. git pull 이 언제나 먼저다. 남의 최신 위에서 일해야 충돌이 쌓이지 않는다.
        if dry_run:
            step("pull", f"[dry-run] git fetch/merge --ff-only {remote}/{branch}")
        else:
            ok, detail = pull_first(root, remote, branch)
            step("pull", detail, ok=ok)
            if not ok:
                report["stopped"] = "pull"
                return report

        # 1-b. 지난번에 커밋은 됐는데 push 가 실패한 것이 있으면 먼저 민다.
        # 워킹트리는 깨끗하고 raw 도 처리된 상태라, 여기서 안 밀면 그 커밋은
        # 원격에 영영 올라가지 않는다.
        if push and not dry_run:
            behind = unpushed_commits(root, remote, branch)
            if behind:
                ok, detail = push_pending(root, remote, branch)
                step("push-pending", f"{behind}개 — {detail}", ok=ok)

        # 2. 할 일을 모은다. 하나도 없으면 에이전트를 부르지 않는다.
        tasks: list[tuple[str, str]] = []

        pending = pending_sources(root)
        report["pending"] = pending
        record = read_seen(root)
        verdict, why = ("go", "") if dry_run else seen_verdict(record, pending, now())
        report["verdict"] = verdict
        if not pending:
            step("pending", "미처리 raw 소스 없음")
        elif verdict != "go":
            # 판단이 끝난 목록은 매시간 다시 깨우지 않는다. 실패한 목록은
            # 묻어 두지 않고 간격을 늘려 가며 다시 시도한다.
            step("pending", f"미처리 {len(pending)}건 — {why}")
        else:
            step("pending", f"미처리 raw 소스 {len(pending)}건: " + ", ".join(pending[:5])
                 + (" …" if len(pending) > 5 else "") + (f" — {why}" if why else ""))
            tasks.append(("ingest", INGEST_PROMPT))

        if not tasks:
            report["skipped"] = "nothing-to-do"
            return report
        report["tasks"] = [name for name, _ in tasks]

        # 3. 에이전트에게 넘긴다.
        prompt = "\n\n".join(body for _, body in tasks)
        if dry_run:
            step("agent", f"[dry-run] {agent} — " + ", ".join(n for n, _ in tasks))
            return report
        if not any(agent_available(name) for name in AGENTS):
            step("agent", f"쓸 수 있는 에이전트가 없다: {', '.join(AGENTS)}", ok=False)
            report["stopped"] = "agent-missing"
            return report
        # 이 표시 뒤에 생긴 변경은 우리 것이다. 중간에 죽어도 다음 차례가
        # 그걸 알아보고 이어서 끝낸다.
        mark_inflight(root, branch)
        code, out, used = run_agent(root, agent, prompt, timeout, step)
        report["agent_used"] = used
        tail = out.strip().splitlines()[-1] if out.strip() else ""
        step("agent", f"{used} 종료 코드 {code}" + (f" — {tail[:200]}" if tail else ""),
             ok=code == 0)
        if any(name == "ingest" for name, _ in tasks):
            # 끝까지 돌고도 남은 것은 에이전트의 판단이니 다시 묻지 않는다.
            # 실패는 판단이 아니다 — 타임아웃·인증 오류 한 번이 그 소스들을
            # 영영 묻지 않도록 시도 횟수를 세어 두고 나중에 다시 부른다.
            # 남은 것은 남은 것이다 — 실패였는지 에이전트의 판단이었는지로
            # 갈라 묻어 두지 않는다. 같은 목록이 계속 남으면 간격만 늘린다.
            left = pending_sources(root)
            same = set(left) == set(record.get("pending") or [])
            write_seen(root, left, outcome="failed" if code else "done",
                       attempts=(as_int(record.get("attempts")) + 1) if same else 1)
        if code != 0:
            report["stopped"] = "agent-failed"
            return report
        report["ingested"] = True

        # 4. 파생물을 다시 만들고 검증한다. 깨졌으면 커밋하는 대신 에이전트에게
        # 되돌려 고치게 한다 — 깨진 채로 멈추면 그 소스는 영영 못 들어간다.
        stage, detail = rebuild_or_repair(root, agent, timeout, step)
        step("build", detail, ok=not stage)
        if stage:
            report["stopped"] = stage
            return report

        # 5. 실제로 바뀐 것이 있을 때만 커밋하고 밀어 올린다.
        ok, detail = commit_and_push(root, remote, branch, push=push)
        step("commit", detail, ok=ok)
        if not ok:
            report["stopped"] = "commit"
            return report
        clear_inflight(root)
        return report
    finally:
        if lock is not None:
            try:
                lock.unlink()
            except OSError:
                pass


# --------------------------------------------------------------------------- 스케줄러
# 주기는 에이전트 자신의 주기 작업으로만 건다. OS 스케줄러(launchd · cron ·
# schtasks)는 두지 않는다 — 부르는 자리가 둘이면 무엇이 돌았는지도 둘로 갈린다.
def hook_python() -> str:
    return sys.executable or "python3"


def routine_command(root: Path, agent: str, *, python: str | None = None,
                    remote: str = REMOTE, push: bool = True) -> list[str]:
    argv = [python or hook_python(), str(Path(__file__).resolve()), "run",
            "--root", str(root), "--agent", agent, "--remote", remote]
    if not push:
        argv.append("--no-push")
    return argv


def agent_task_dir(agent: str) -> Path | None:
    """에이전트가 자기 주기 작업을 두는 자리. 실제로 있을 때만 인정한다."""
    rel = AGENT_TASK_DIRS.get(agent)
    home = Path(os.environ.get(ENV_TASK_HOME) or Path.home())
    path = (home / rel) if rel else None
    return path if path and path.is_dir() else None


def task_file(agent: str) -> Path | None:
    base = agent_task_dir(agent)
    return base / TASK_NAME / "SKILL.md" if base else None


def ours_task(path: Path) -> bool:
    """이 작업이 우리가 쓴 것인가. 사람이 손으로 쓴 같은 이름은 손대지 않는다."""
    try:
        body = path.read_text(encoding="utf-8")
    except OSError:
        return False
    return TASK_MARKER in body and str(Path(__file__).resolve()) in body


def task_body(root: Path, argv: list[str], interval: int) -> str:
    """에이전트가 읽을 작업 정의. 판단은 루틴이 하고, 여기서는 부르기만 한다."""
    command = " ".join(shlex.quote(a) for a in argv)
    return (
        "---\n"
        f"name: {TASK_NAME}\n"
        "description: raw/ 의 새 소스를 JSON 위키에 통합하는 주기 루틴. "
        "새 소스가 없으면 아무것도 하지 않는다\n"
        "---\n\n"
        f"{TASK_MARKER}\n"
        f"<!-- 저장소: {root} · 주기: {interval}초 · "
        "이 파일은 llmwiki_routine.py install 이 쓴다 -->\n\n"
        "# 주기 ingest\n\n"
        "이 작업은 스스로 판단하지 않는다. 아래 한 줄을 실행하고 그 보고만 읽는다.\n"
        "루틴이 이월분 정리 · git pull · 미처리 판정 · ingest · build · validate ·\n"
        "커밋 · push 를 순서대로 하고, 어긋나면 그 자리에서 멈춘다.\n\n"
        f"    {command} --json\n\n"
        "- `\"skipped\": \"nothing-to-do\"` 는 정상이다. 새 소스가 없으면 아무것도\n"
        "  하지 않고 끝나는 것이 이 루틴의 정상 동작이다. 억지로 page 를 만들지 마라.\n"
        "- `\"stopped\"` 가 있으면 그 단계의 `detail` 을 그대로 사람에게 보고해라.\n"
        "  특히 `leftover` · `pull` 은 사람이 손봐야 풀리는 자리다.\n"
        "- raw/ 는 수정하지 마라. 이 파일도 고치지 마라 — install 이 다시 쓴다.\n"
    )


def install_agent_task(root: Path, agent: str, argv: list[str],
                       interval: int) -> dict[str, Any]:
    path = task_file(agent)
    if path is None:
        base = Path.home() / AGENT_TASK_DIRS.get(agent, Path(""))
        return {"ok": False,
                "detail": f"{agent} 에는 주기 작업 자리가 없다 ({base}) — 다른 데 걸지 않는다"}
    if path.exists() and not ours_task(path):
        # 사람이 직접 쓴 같은 이름의 작업이 있다. MCP 등록과 같은 규율이다.
        return {"file": str(path), "ok": False,
                "detail": f"{path} 는 우리가 만든 것이 아니다 — 그대로 둔다"}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(task_body(root, argv, interval), encoding="utf-8")
    except OSError as exc:  # noqa: BLE001
        return {"file": str(path), "ok": False, "detail": f"작업을 쓰지 못했다: {exc}"}
    return {"file": str(path), "ok": True, "detail": f"{agent} 자체 루틴에 등록: {path}"}


def uninstall_agent_task(agent: str) -> dict[str, Any]:
    path = task_file(agent)
    if path is None or not path.exists():
        return {"ok": True, "detail": f"{agent} 자체 루틴에 우리 작업이 없다"}
    if not ours_task(path):
        return {"ok": True, "detail": f"{path} 가 우리 것이 아니게 바뀌었다 — 그대로 둔다"}
    try:
        path.unlink()
        # 우리가 만든 빈 폴더만 치운다. 다른 파일이 있으면 그대로 둔다.
        if next(path.parent.iterdir(), None) is None:
            path.parent.rmdir()
    except OSError as exc:  # noqa: BLE001
        return {"ok": False, "detail": f"작업을 지우지 못했다: {exc}"}
    return {"ok": True, "detail": f"제거됨: {path}"}


def scheduler_kind(agent: str | None = None) -> str:
    """주기를 걸 자리. 에이전트 자신의 주기 작업뿐이다."""
    if agent and agent_task_dir(agent):
        return f"{agent}-task"
    return ""


def install_schedule(root: Path, agent: str, *, interval: int, python: str | None,
                     remote: str, push: bool, dry_run: bool) -> dict[str, Any]:
    """에이전트 자신의 주기 작업으로 등록한다.

    그 자리에서 부르면 인증·모델·권한이 이미 갖춰진 채로 돌고, 무엇이 왜
    멈췄는지도 그 에이전트가 그대로 읽는다.
    """
    argv = routine_command(root, agent, python=python, remote=remote, push=push)
    plan: dict[str, Any] = {"scheduler": f"{agent}-task", "interval": interval,
                            "command": argv}
    if dry_run:
        plan["dry_run"] = True
        return plan
    plan.update(install_agent_task(root, agent, argv, interval))
    if plan.get("ok"):
        record_state(root, agent, interval=interval, remote=remote, push=push,
                     kind=f"{agent}-task")
    return plan


def uninstall_schedule(*, dry_run: bool) -> dict[str, Any]:
    # 무엇을 지울지는 등록할 때 적어 둔 것을 따른다.
    state = read_state()
    kind = str(state.get("scheduler") or "")
    plan: dict[str, Any] = {"scheduler": kind}
    if dry_run:
        plan["dry_run"] = True
        return plan
    if not state_file().exists():
        plan.update(ok=True, detail="우리가 등록한 기록이 없다 — 아무것도 지우지 않는다")
        return plan
    plan.update(uninstall_agent_task(kind[: -len("-task")] if kind.endswith("-task")
                                     else str(state.get("agent") or "claude")))
    if plan.get("ok"):
        forget_state()
    return plan


def record_state(root: Path, agent: str, *, interval: int, remote: str, push: bool,
                 kind: str) -> None:
    state_dir().mkdir(parents=True, exist_ok=True)
    state_file().write_text(json.dumps({
        "root": str(root), "agent": agent, "interval": interval, "remote": remote,
        "push": push, "scheduler": kind, "installed_at": stamp(),
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def forget_state() -> None:
    try:
        state_file().unlink()
    except OSError:
        pass


def root_key(root: Path) -> str:
    """상태를 저장소별로 가른다. 두 clone 이 한 파일을 두고 다투지 않게."""
    return hashlib.sha1(str(Path(root).resolve()).encode("utf-8")).hexdigest()[:10]


def seen_file(root: Path) -> Path:
    return state_dir() / f"routine-seen-{root_key(root)}.json"


def legacy_seen_file() -> Path:
    return state_dir() / "routine-seen.json"


def as_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def read_seen(root: Path) -> dict[str, Any]:
    """지난번에 에이전트에게 넘긴 목록과 그 결과."""
    for path in (seen_file(root), legacy_seen_file()):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(value, list):  # 옛 형식 — 목록만 적었다
            return {"pending": [str(x) for x in value], "outcome": "done",
                    "attempts": 0, "at": 0.0}
        if isinstance(value, dict):
            return {"pending": [str(x) for x in value.get("pending") or []],
                    "outcome": "failed" if value.get("outcome") == "failed" else "done",
                    "attempts": as_int(value.get("attempts")),
                    "at": as_float(value.get("at"))}
    return {}


def write_seen(root: Path, pending: Iterable[str], *, outcome: str = "done",
               attempts: int = 0) -> None:
    try:
        state_dir().mkdir(parents=True, exist_ok=True)
        seen_file(root).write_text(json.dumps(
            {"pending": sorted(pending), "outcome": outcome, "attempts": attempts,
             "at": now(), "written": stamp()}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
    except OSError:
        pass


def retry_delay(attempts: int) -> float:
    """실패가 이어질수록 뜸하게, 그러나 언젠가는 반드시 다시 시도한다."""
    return min(RETRY_BASE_SECONDS * (2 ** max(0, attempts - 1)), RETRY_MAX_SECONDS)


def seen_verdict(record: dict[str, Any], pending: list[str],
                 moment: float) -> tuple[str, str]:
    """이번 목록으로 에이전트를 깨울까. ('go'|'waiting', 설명).

    안 들어간 소스를 영영 접는 자리는 없다. 실패였든 에이전트가 건너뛴
    판단이었든, 남아 있는 한 간격을 늘려 가며(최대 하루) 계속 다시 넣어 본다.
    """
    if not record or set(pending) != set(record.get("pending") or []):
        return "go", ""  # 목록이 달라졌다 — 새 소스다
    attempts = as_int(record.get("attempts"))
    delay = retry_delay(attempts)
    waited = moment - as_float(record.get("at"))
    if waited < delay:
        return "waiting", (f"{attempts}번 시도했다 — {int((delay - waited) // 60)}분 뒤에 "
                           "다시 넣어 본다")
    return "go", f"아직 안 들어간 목록을 다시 넣는다 ({attempts + 1}번째)"


def inflight_file(root: Path) -> Path:
    return state_dir() / f"routine-inflight-{root_key(root)}.json"


def mark_inflight(root: Path, branch: str) -> None:
    """에이전트를 부르기 직전에 남기는 표시. 이 뒤에 생긴 변경은 우리 것이다."""
    try:
        state_dir().mkdir(parents=True, exist_ok=True)
        inflight_file(root).write_text(json.dumps(
            {"at": stamp(), "branch": branch, "pid": os.getpid()},
            ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass


def read_inflight(root: Path) -> dict[str, Any]:
    try:
        value = json.loads(inflight_file(root).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def clear_inflight(root: Path) -> None:
    try:
        inflight_file(root).unlink()
    except OSError:
        pass


def last_run_at() -> float | None:
    """마지막으로 루틴이 실제로 돈 시각. 로그가 없으면 None."""
    try:
        lines = log_file().read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        head = line.split(" ", 1)[0]
        try:
            return time.mktime(time.strptime(head, "%Y-%m-%dT%H:%M:%S"))
        except ValueError:
            continue
    return None


def firing_check(interval: int) -> tuple[bool, str]:
    """스케줄러가 실제로 부르고 있나. 조용히 안 도는 것이 제일 위험하다.

    에이전트 자체 루틴은 그 에이전트가 깨어 있을 때만 도는 경우가 있어, 걸어
    두었다는 사실만으로는 돌고 있다고 말할 수 없다. 그래서 등록 상태가 아니라
    마지막 실행 시각으로 판정한다.
    """
    at = last_run_at()
    if at is None:
        return False, "실행 기록이 없다 — 아직 한 번도 돌지 않았다"
    idle = now() - at
    if idle > max(2 * interval, 2 * DEFAULT_INTERVAL):
        return False, (f"마지막 실행이 {int(idle // 3600)}시간 전이다 — 에이전트 주기 "
                       "작업이 부르지 않고 있다. 그 에이전트에서 작업이 살아 있는지 봐라")
    return True, f"마지막 실행 {int(idle // 60)}분 전"


def read_state() -> dict[str, Any]:
    try:
        return json.loads(state_file().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


# --------------------------------------------------------------------------- git-setup
GH_NAME = re.compile(r"^[A-Za-z0-9._-]{1,100}$")


def gh_available() -> bool:
    return shutil.which("gh") is not None


def gh_create(root: Path, name: str) -> tuple[bool, str]:
    if not GH_NAME.match(name):
        return False, f"저장소 이름이 이상하다: {name}"
    if not gh_available():
        return False, "gh 가 없다 — --url 로 이미 만든 저장소를 주거나 gh 를 설치해라"
    code, out = run(["gh", "repo", "create", name, "--private"], cwd=root, timeout=120.0)
    if code != 0:
        return False, f"gh repo create 실패: {out.splitlines()[-1] if out else code}"
    code, url = run(["gh", "repo", "view", name, "--json", "sshUrl", "-q", ".sshUrl"],
                    cwd=root, timeout=60.0)
    if code != 0 or not url.strip():
        return False, f"만들어진 저장소 주소를 못 읽었다: {url}"
    return True, url.strip()


def git_setup(root: Path, *, url: str, create: str, remote: str,
              dry_run: bool) -> dict[str, Any]:
    """origin 은 건드리지 않는다. 개인 저장소는 별도 remote 로만 붙인다."""
    report: dict[str, Any] = {"remote": remote, "root": str(root)}
    if not (root / ".git").exists():
        report.update(ok=False, detail="git 저장소가 아니다")
        return report
    existing = has_remote(root, remote)
    report["existing"] = existing
    target = url.strip()
    if not target and create:
        if dry_run:
            report.update(dry_run=True, detail=f"gh repo create {create} --private 후 remote 등록")
            return report
        ok, target = gh_create(root, create)
        if not ok:
            report.update(ok=False, detail=target)
            return report
        report["created"] = target
    if not target:
        report.update(ok=False, detail="--url 이나 --gh-create 중 하나가 필요하다")
        return report
    if existing and existing != target:
        report.update(ok=False,
                      detail=f"remote '{remote}' 가 이미 {existing} 를 가리킨다 — 그대로 둔다")
        return report
    if dry_run:
        report.update(dry_run=True, detail=f"git remote add {remote} {target}")
        return report
    if existing == target:
        report.update(ok=True, detail=f"이미 {target} 로 붙어 있다")
        return report
    code, out = git(root, "remote", "add", remote, target, timeout=30.0)
    report.update(ok=code == 0, detail=out or f"remote '{remote}' → {target}")
    return report


# --------------------------------------------------------------------------- CLI
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="llmwiki-routine",
        description="raw/ 의 새 소스를 주기적으로 위키에 넣는 루틴")
    parser.add_argument("--root", help=f"저장소 루트 (기본: ${ENV_ROOT} 또는 {DEFAULT_ROOT})")
    sub = parser.add_subparsers(dest="command", required=True)

    r = sub.add_parser("run", help="한 번 돈다 (git pull → 미처리 확인 → ingest → 커밋/push)")
    r.add_argument("--agent", choices=list(AGENTS), default="claude")
    r.add_argument("--remote", default=REMOTE, help=f"push/pull 대상 remote (기본 {REMOTE})")
    r.add_argument("--no-push", action="store_true", help="커밋만 하고 push 하지 않는다")
    r.add_argument("--timeout", type=float, default=3600.0, help="에이전트 실행 상한(초)")
    r.add_argument("-n", "--dry-run", action="store_true")
    r.add_argument("--json", action="store_true", dest="as_json")

    i = sub.add_parser("install", help="OS 스케줄러에 등록한다")
    i.add_argument("--agent", choices=list(AGENTS), required=True)
    i.add_argument("--interval", type=int, default=DEFAULT_INTERVAL, help="초 (기본 3600)")
    i.add_argument("--python", help="루틴에 못박을 인터프리터 절대경로")
    i.add_argument("--remote", default=REMOTE)
    i.add_argument("--no-push", action="store_true")
    i.add_argument("-n", "--dry-run", action="store_true")

    u = sub.add_parser("uninstall", help="우리가 등록한 것만 지운다")
    u.add_argument("-n", "--dry-run", action="store_true")

    sub.add_parser("status", help="등록 상태와 마지막 실행")
    sub.add_parser("pending", help="미처리 raw 소스 목록")

    g = sub.add_parser("git-setup", help="개인 private 저장소를 remote 로 붙인다")
    g.add_argument("--url", default="", help="이미 만들어 둔 private 저장소 주소")
    g.add_argument("--gh-create", default="", metavar="NAME",
                   help="gh 로 private 저장소를 새로 만든다")
    g.add_argument("--remote", default=REMOTE)
    g.add_argument("-n", "--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = resolve_root(args.root)

    if args.command == "run":
        report = do_run(root, agent=args.agent, remote=args.remote,
                        push=not args.no_push, dry_run=args.dry_run, timeout=args.timeout)
        if args.as_json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if all(s["ok"] for s in report["steps"]) else 1

    if args.command == "install":
        plan = install_schedule(root, args.agent, interval=args.interval, python=args.python,
                                remote=args.remote, push=not args.no_push,
                                dry_run=args.dry_run)
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0 if plan.get("ok", True) else 1

    if args.command == "uninstall":
        plan = uninstall_schedule(dry_run=args.dry_run)
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0 if plan.get("ok", True) else 1

    if args.command == "status":
        state = read_state()
        tail = ""
        try:
            tail = log_file().read_text(encoding="utf-8").splitlines()[-1]
        except (OSError, IndexError):
            pass
        pending = pending_sources(root)
        record = read_seen(root)
        verdict, why = seen_verdict(record, pending, now()) if pending else ("go", "")
        firing, firing_why = firing_check(as_int(state.get("interval")) or DEFAULT_INTERVAL)
        print(json.dumps({
            "installed": bool(state),
            "scheduler": state.get("scheduler") or scheduler_kind(state.get("agent")),
            "state_file": str(state_file()), "log": str(log_file()),
            "last_log_line": tail, "pending": len(pending),
            # 막혀 있으면 여기서 보인다. 조용히 쌓이게 두지 않는다.
            "verdict": verdict, "verdict_detail": why,
            "firing": firing, "firing_detail": firing_why,
            # 어느 에이전트가 자기 자리를 갖고 있는지. 빈 값이면 OS 폴백뿐이다.
            "agent_routines": {a: str(agent_task_dir(a) or "") for a in AGENTS},
            "attempts": as_int(record.get("attempts")),
            "seen_file": str(seen_file(root)),
            "leftover": read_inflight(root) or None, **state,
        }, ensure_ascii=False, indent=2))
        # 걸어 뒀는데 돌지 않는 것은 성공이 아니다. 스크립트가 알아채게 한다.
        return 0 if (not state or firing) else 1

    if args.command == "pending":
        pending = pending_sources(root)
        verdict, why = seen_verdict(read_seen(root), pending, now()) if pending else ("go", "")
        print(json.dumps({"root": str(root), "pending": pending,
                          "verdict": verdict, "verdict_detail": why},
                         ensure_ascii=False, indent=2))
        return 0

    report = git_setup(root, url=args.url, create=args.gh_create, remote=args.remote,
                       dry_run=args.dry_run)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("ok", True) else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (BrokenPipeError, KeyboardInterrupt):
        raise SystemExit(0)

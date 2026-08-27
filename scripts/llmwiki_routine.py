#!/usr/bin/env python3
"""주기적 ingest 루틴 — raw/ 에 새 소스가 들어오면 에이전트를 불러 위키에 넣는다.

설계 원칙은 hook 과 같다. 조용하고, 실패해도 남의 것을 망가뜨리지 않으며,
우리가 만든 것만 되돌린다.

  run        한 번 돈다 (스케줄러가 부르는 것). git pull 이 언제나 첫 단계다.
  install    OS 스케줄러에 등록한다 (launchd · cron · schtasks)
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
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import time
from fnmatch import fnmatch
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape
from typing import Any, Iterable

DEFAULT_ROOT = Path(__file__).resolve().parents[1]

ENV_ROOT = "LLMWIKI_ROOT"
ENV_STATE_DIR = "LLMWIKI_STATE_DIR"
ENV_NOW = "LLMWIKI_NOW"

AGENTS = ("claude", "codex")
DEFAULT_INTERVAL = 3600
LABEL = "com.llmwiki.ingest"
REMOTE = "private"
LOCK_STALE_SECONDS = 6 * 3600

# 에이전트에게 주는 지시. 스킬이 있으면 그것을 쓰고, 없으면 이 문장이 곧 명세다.
INGEST_PROMPT = (
    "raw/ 에 아직 위키에 들어가지 않은 소스가 있다. 저장소 규칙(CLAUDE.md / AGENTS.md)에 "
    "따라 ingest 해라: 원문을 읽고 page 를 만들거나 갱신하고, 관련 page 와 "
    "source/entity/concept/synthesis/project 관계를 잇고, wiki/log.jsonl 에 기록한 뒤 "
    "build · validate · lint 를 돌려라. raw/ 는 수정하지 마라. "
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
            if not isinstance(page, dict):
                continue
            ref = page.get("raw_ref")
            if isinstance(ref, str) and ref.strip():
                refs.add(ref.strip().replace("\\", "/"))
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
        if not path.is_file() or path.name.startswith("."):
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
    return [rel for rel in raw_sources(root) if rel not in refs]


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
    code, out = git(root, "status", "--porcelain")
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


# --------------------------------------------------------------------------- 에이전트
def agent_argv(agent: str, prompt: str) -> list[str]:
    if agent == "claude":
        return ["claude", "-p", prompt, "--permission-mode", "acceptEdits"]
    if agent == "codex":
        return ["codex", "exec", "--skip-git-repo-check", prompt]
    raise ValueError(f"unknown agent: {agent}")


def agent_available(agent: str) -> bool:
    return shutil.which(agent) is not None


# --------------------------------------------------------------------------- 잠금
def acquire_lock() -> Path | None:
    """겹쳐 도는 것을 막는다. 죽은 락은 시간이 지나면 스스로 풀린다."""
    path = state_dir() / "routine.lock"
    try:
        state_dir().mkdir(parents=True, exist_ok=True)
        if path.exists() and now() - path.stat().st_mtime > LOCK_STALE_SECONDS:
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

    lock = None if dry_run else acquire_lock()
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
        seen = read_seen()
        if not pending:
            step("pending", "미처리 raw 소스 없음")
        elif not dry_run and seen and set(pending) == set(seen):
            # 지난번에 이미 넘겼는데 그대로 남아 있는 것들은 에이전트가 보고
            # 판단한 결과다. 같은 목록으로 매시간 다시 깨우지 않는다.
            step("pending", f"미처리 {len(pending)}건이 지난번과 같다 — 새 소스가 아니다")
        else:
            step("pending", f"미처리 raw 소스 {len(pending)}건: " + ", ".join(pending[:5])
                 + (" …" if len(pending) > 5 else ""))
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
        if not agent_available(agent):
            step("agent", f"{agent} 실행 파일이 없다", ok=False)
            report["stopped"] = "agent-missing"
            return report
        code, out = run(agent_argv(agent, prompt), cwd=root, timeout=timeout)
        tail = out.strip().splitlines()[-1] if out.strip() else ""
        step("agent", f"{agent} 종료 코드 {code}" + (f" — {tail[:200]}" if tail else ""),
             ok=code == 0)
        if any(name == "ingest" for name, _ in tasks):
            # 성공이든 실패든 이 목록으로는 한 번 깨웠다. 에이전트가 "이건 못
            # 넣겠다" 고 판단한 것일 수도 있어 재시도는 값이 없다.
            write_seen(pending_sources(root))
        if code != 0:
            report["stopped"] = "agent-failed"
            return report
        report["ingested"] = True

        # 4. 파생물을 다시 만들고 검증한다. 깨졌으면 커밋하지 않는다.
        code, out = run([sys.executable, str(root / "scripts" / "llmwiki.py"), "build"],
                        cwd=root, timeout=600.0)
        step("build", "완료" if code == 0 else f"실패: {out[-200:]}", ok=code == 0)
        if code != 0:
            report["stopped"] = "build"
            return report
        code, out = run([sys.executable, str(root / "scripts" / "llmwiki.py"), "validate"],
                        cwd=root, timeout=600.0)
        step("validate", "통과" if code == 0 else f"실패: {out[-200:]}", ok=code == 0)
        if code != 0:
            report["stopped"] = "validate"
            return report

        # 5. 실제로 바뀐 것이 있을 때만 커밋하고 밀어 올린다.
        ok, detail = commit_and_push(root, remote, branch, push=push)
        step("commit", detail, ok=ok)
        if not ok:
            report["stopped"] = "commit"
        return report
    finally:
        if lock is not None:
            try:
                lock.unlink()
            except OSError:
                pass


# --------------------------------------------------------------------------- 스케줄러
def hook_python() -> str:
    return sys.executable or "python3"


def routine_command(root: Path, agent: str, *, python: str | None = None,
                    remote: str = REMOTE, push: bool = True) -> list[str]:
    argv = [python or hook_python(), str(Path(__file__).resolve()), "run",
            "--root", str(root), "--agent", agent, "--remote", remote]
    if not push:
        argv.append("--no-push")
    return argv


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def plist_body(argv: list[str], interval: int) -> str:
    # 경로에 & 나 < 가 있으면 escape 없이는 잘못된 plist 가 만들어진다.
    args = "\n".join(f"        <string>{xml_escape(a)}</string>" for a in argv)
    out = xml_escape(str(log_file()))
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        '<dict>\n'
        f'    <key>Label</key><string>{xml_escape(LABEL)}</string>\n'
        '    <key>ProgramArguments</key>\n'
        f'    <array>\n{args}\n    </array>\n'
        f'    <key>StartInterval</key><integer>{interval}</integer>\n'
        '    <key>RunAtLoad</key><false/>\n'
        f'    <key>StandardOutPath</key><string>{out}</string>\n'
        f'    <key>StandardErrorPath</key><string>{out}</string>\n'
        '</dict>\n'
        '</plist>\n'
    )


CRON_MARKER = "# llmwiki-routine"


def cron_spec(interval: int) -> str:
    """초 단위 주기를 crontab 표현으로.

    `*/N` 은 한 자리(분·시) 안에서만 도는 표현이라, N 이 그 자리의 약수가 아니면
    하루 경계에서 간격이 어긋난다. 그래서 분은 60, 시는 24 의 약수로만 내린다.
    하루를 넘는 주기는 crontab 으로 정확히 못 적으므로 매일 자정으로 둔다.
    """
    minutes = max(1, interval // 60)
    if minutes < 60:
        return f"*/{max((d for d in (1, 2, 3, 4, 5, 6, 10, 12, 15, 20, 30) if d <= minutes), default=1)} * * * *"
    if minutes == 60:
        return "@hourly"
    hours = minutes // 60
    if hours >= 24:
        return "0 0 * * *"
    return f"0 */{max((d for d in (1, 2, 3, 4, 6, 8, 12) if d <= hours), default=1)} * * *"


def crontab_read() -> tuple[bool, str]:
    """(읽을 수 있었나, 내용). 읽기 실패를 '빈 crontab' 으로 읽으면 남의 줄을 날린다."""
    # 메시지를 문자열로 판정하므로 로케일을 고정한다 — 한국어 환경에서
    # "no crontab" 이 안 나오면 신규 등록이 통째로 막힌다.
    env = {"LC_ALL": "C", "LANG": "C"}
    code, out = run(["crontab", "-l"], timeout=15.0, env=env)
    if code == 0:
        return True, out
    # crontab 이 아직 없는 것은 오류가 아니다. 그 외 실패는 손대지 않는다.
    if "no crontab" in out.lower() or not out.strip():
        return True, ""
    return False, ""


def crontab_write(text: str) -> tuple[int, str]:
    try:
        proc = subprocess.run(["crontab", "-"], input=text, capture_output=True,
                              text=True, timeout=15.0)
    except (OSError, subprocess.SubprocessError) as exc:  # noqa: BLE001
        return 1, str(exc)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def cron_lines(existing: str) -> list[str]:
    return [ln for ln in existing.splitlines() if CRON_MARKER not in ln]


def scheduler_kind() -> str:
    system = platform.system()
    if system == "Darwin":
        return "launchd"
    if system == "Windows":
        return "schtasks"
    return "cron"


def ours_launchd(path: Path) -> bool:
    """이 plist 가 우리가 쓴 것인가. 라벨이 같아도 남의 것이면 손대지 않는다."""
    try:
        body = path.read_text(encoding="utf-8")
    except OSError:
        return False
    # plist 안의 값은 escape 되어 있다. 검사도 같은 형태로 해야 자기 것을
    # 남의 것으로 오판하지 않는다.
    return xml_escape(LABEL) in body and xml_escape(str(Path(__file__).resolve())) in body


def ours_schtask() -> bool:
    code, out = run(["schtasks", "/Query", "/TN", LABEL, "/FO", "LIST", "/V"], timeout=30.0)
    if code != 0:
        return False
    return str(Path(__file__).resolve()) in out


def install_schedule(root: Path, agent: str, *, interval: int, python: str | None,
                     remote: str, push: bool, dry_run: bool) -> dict[str, Any]:
    argv = routine_command(root, agent, python=python, remote=remote, push=push)
    kind = scheduler_kind()
    plan = {"scheduler": kind, "interval": interval, "command": argv}
    if dry_run:
        plan["dry_run"] = True
        return plan

    if kind == "launchd":
        path = plist_path()
        # 같은 라벨의 남의 항목은 덮어쓰지 않는다 — MCP 등록과 같은 규율이다.
        if path.exists() and not ours_launchd(path):
            plan.update(file=str(path), ok=False,
                        detail=f"{path} 는 우리가 만든 것이 아니다 — 그대로 둔다")
            return plan
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            # 도는 것을 내리지 못한 채 plist 를 갈아치우면 옛 명령이 계속 돈다.
            code, out = run(["launchctl", "unload", str(path)], timeout=30.0)
            if code != 0:
                plan.update(file=str(path), ok=False,
                            detail=f"기존 항목을 내리지 못했다 — 그대로 둔다: {out or code}")
                return plan
        path.write_text(plist_body(argv, interval), encoding="utf-8")
        code, out = run(["launchctl", "load", str(path)], timeout=30.0)
        if code != 0:
            # load 실패면 등록되지 않은 plist 만 남는다. 흔적을 남기지 않는다.
            path.unlink(missing_ok=True)
            plan.update(file=str(path), ok=False, detail=f"launchctl load 실패: {out or code}")
            return plan
        plan.update(file=str(path), ok=True, detail=out or "load 완료")
    elif kind == "cron":
        spec = cron_spec(interval)
        readable, current = crontab_read()
        if not readable:
            plan.update(ok=False, detail="crontab 을 읽을 수 없다 — 덮어쓰지 않는다")
            return plan
        line = f"{spec} {' '.join(shlex.quote(a) for a in argv)} {CRON_MARKER}"
        text = "\n".join([*cron_lines(current), line]).strip() + "\n"
        code, out = crontab_write(text)
        plan.update(ok=code == 0, detail=out or f"crontab 등록: {spec}")
    else:
        existing = run(["schtasks", "/Query", "/TN", LABEL], timeout=30.0)[0] == 0
        if existing and not ours_schtask():
            plan.update(ok=False, detail=f"작업 '{LABEL}' 은 우리가 만든 것이 아니다 — 그대로 둔다")
            return plan
        code, out = run(["schtasks", "/Create", "/F", "/TN", LABEL, "/SC", "MINUTE",
                         "/MO", str(max(1, interval // 60)), "/TR",
                         " ".join(f'"{a}"' for a in argv)], timeout=30.0)
        plan.update(ok=code == 0, detail=out or "schtasks 등록")

    if plan.get("ok"):
        record_state(root, agent, interval=interval, remote=remote, push=push, kind=kind)
    return plan


def uninstall_schedule(*, dry_run: bool) -> dict[str, Any]:
    kind = scheduler_kind()
    plan: dict[str, Any] = {"scheduler": kind}
    if dry_run:
        plan["dry_run"] = True
        return plan
    if not state_file().exists():
        plan.update(ok=True, detail="우리가 등록한 기록이 없다 — 아무것도 지우지 않는다")
        return plan
    if kind == "launchd":
        path = plist_path()
        if not path.exists():
            plan.update(ok=True, detail=f"이미 없음: {path}")
        elif not ours_launchd(path):
            # 설치 후 누군가 같은 라벨로 갈아치웠다. 남의 것을 지우지 않는다.
            plan.update(ok=True, detail=f"{path} 가 우리 것이 아니게 바뀌었다 — 그대로 둔다")
            forget_state()
            return plan
        else:
            code, out = run(["launchctl", "unload", str(path)], timeout=30.0)
            if code != 0:
                # 내리지도 못했는데 plist 를 지우면, 도는 작업이 주인 없이 남는다.
                plan.update(ok=False,
                            detail=f"launchctl unload 실패 — 그대로 둔다: {out or code}")
                return plan
            path.unlink()
            plan.update(ok=True, detail=f"제거됨: {path}")
    elif kind == "cron":
        readable, current = crontab_read()
        if not readable:
            # crontab 을 읽지 못한 채로 쓰면 남의 줄까지 날린다.
            plan.update(ok=False, detail="crontab 을 읽을 수 없다 — 아무것도 지우지 않는다")
            return plan
        kept = cron_lines(current)
        if len(kept) == len(current.splitlines()):
            plan.update(ok=True, detail="crontab 에 우리 줄이 없다")
        else:
            code, out = crontab_write("\n".join(kept).strip() + ("\n" if kept else ""))
            plan.update(ok=code == 0, detail=out or "crontab 항목 제거")
    else:
        if not ours_schtask():
            plan.update(ok=True, detail=f"작업 '{LABEL}' 이 우리 것이 아니다 — 그대로 둔다")
            forget_state()
            return plan
        code, out = run(["schtasks", "/Delete", "/F", "/TN", LABEL], timeout=30.0)
        plan.update(ok=code == 0, detail=out or "schtasks 항목 제거")
    # 지우지 못했으면 소유권 기록을 남긴다 — 다음 uninstall 이 다시 시도해야 한다.
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


def seen_file() -> Path:
    return state_dir() / "routine-seen.json"


def read_seen() -> list[str]:
    try:
        value = json.loads(seen_file().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [str(x) for x in value] if isinstance(value, list) else []


def write_seen(pending: Iterable[str]) -> None:
    try:
        state_dir().mkdir(parents=True, exist_ok=True)
        seen_file().write_text(json.dumps(sorted(pending), ensure_ascii=False, indent=2) + "\n",
                               encoding="utf-8")
    except OSError:
        pass


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
        print(json.dumps({
            "installed": bool(state), "scheduler": state.get("scheduler", scheduler_kind()),
            "state_file": str(state_file()), "log": str(log_file()),
            "last_log_line": tail, "pending": len(pending_sources(root)), **state,
        }, ensure_ascii=False, indent=2))
        return 0

    if args.command == "pending":
        print(json.dumps({"root": str(root), "pending": pending_sources(root)},
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

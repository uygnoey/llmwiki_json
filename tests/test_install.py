"""scripts/install.sh: clone 경로 독립성, 비파괴 병합, 멱등성, 롤백.

모든 테스트는 임시 clone 과 가짜 `$HOME` 에서 돌기 때문에 개발 머신의 실제
Codex/Claude 설정을 건드리지 않는다.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.support import REPO, llmwiki_context as ctx, make_page

INSTALLER = REPO / "scripts" / "install.sh"

CORPUS = [
    make_page("폐기-icd-코드",
              "# 폐기 ICD 코드\n\n"
              "RefModel 기준으로 무효 처리된 RefCode-CM 코드 324건의 노출 정책이다.\n\n"
              "## 핵심 사실\n\n"
              "- 전체 모집단은 324건이며 Condition 도달 149건으로 나뉜다.\n",
              type="concept", projects=["beta"], tags=["폐기-코드"],
              sources=["source:beta-폐기-icd-코드-정책-비교"],
              summary="RefModel 무효 ICD 코드의 검색 노출 정책."),
    make_page("golden-set",
              "# Golden Set\n\n"
              "The golden set is the curated answer key for AlphaStd SDTM mapping checks.\n",
              type="concept", projects=["alpha"], tags=["검증"],
              summary="AlphaStd SDTM 매핑 회귀 검증용 정답지."),
]

FOREIGN_CODEX = {"hooks": {
    "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "other-tool.sh",
                                     "timeout": 10}]}],
    "Stop": [{"hooks": [{"type": "command", "command": "other-tool.sh"}]}]}}
FOREIGN_CLAUDE = {"theme": "dark", "permissions": {"allow": ["Bash"]},
                  "hooks": {"UserPromptSubmit": [
                      {"hooks": [{"type": "command", "command": "other-tool.sh"}]}]}}
FOREIGN_AGENTS = "# 내 기존 지침\n\n건드리면 안 되는 내용.\n"

FAKE_UNAME = "#!/bin/sh\nprintf 'Plan9\\n'\n"

# 네트워크를 타지 않는 가짜 다운로더. 요청한 URL 을 기록하고, 그 URL 에 맞는
# 가짜 설치 스크립트를 목적지에 써 준다. 그 스크립트가 실행되면 가짜 uv/bun 이
# 진짜 설치기와 같은 자리에 생긴다.
FAKE_CURL = """#!/bin/sh
url=""
dest=""
while [ $# -gt 0 ]; do
    case $1 in
        -o) dest=$2; shift ;;
        -*) ;;
        *) url=$1 ;;
    esac
    shift
done
printf 'curl %s -> %s\\n' "$url" "$dest" >> "$FAKE_NET_LOG"
if [ -n "$FAKE_NET_FAIL" ]; then exit 22; fi
case $url in
    *astral.sh/uv*) cat "$FAKE_PAYLOAD/uv-install.sh" > "$dest" 2>/dev/null || exit 22 ;;
    *bun.sh/install*) cat "$FAKE_PAYLOAD/bun-install.sh" > "$dest" 2>/dev/null || exit 22 ;;
    *) exit 22 ;;
esac
exit 0
"""

FAKE_WGET = """#!/bin/sh
url=""
dest=""
while [ $# -gt 0 ]; do
    case $1 in
        -O) dest=$2; shift ;;
        -*) ;;
        *) url=$1 ;;
    esac
    shift
done
printf 'wget %s -> %s\\n' "$url" "$dest" >> "$FAKE_NET_LOG"
if [ -n "$FAKE_NET_FAIL" ]; then exit 8; fi
case $url in
    *astral.sh/uv*) cat "$FAKE_PAYLOAD/uv-install.sh" > "$dest" 2>/dev/null || exit 8 ;;
    *bun.sh/install*) cat "$FAKE_PAYLOAD/bun-install.sh" > "$dest" 2>/dev/null || exit 8 ;;
    *) exit 8 ;;
esac
exit 0
"""

# 진짜 Astral 설치기처럼 UV_INSTALL_DIR 에 uv 를 놓는다. profile 을 건드리지
# 않았다는 것을 확인할 수 있도록, 무시하지 않은 env 를 로그에 남긴다.
FAKE_UV_INSTALLER = """#!/bin/sh
printf 'uv-installer UV_INSTALL_DIR=%s UV_NO_MODIFY_PATH=%s INSTALLER_NO_MODIFY_PATH=%s\\n' \
    "$UV_INSTALL_DIR" "$UV_NO_MODIFY_PATH" "$INSTALLER_NO_MODIFY_PATH" >> "$FAKE_NET_LOG"
if [ -n "$FAKE_UV_INSTALL_FAIL" ]; then exit 1; fi
mkdir -p "$UV_INSTALL_DIR"
cp "$FAKE_PAYLOAD/uv" "$UV_INSTALL_DIR/uv"
chmod +x "$UV_INSTALL_DIR/uv"
exit 0
"""

# 진짜 Bun 설치기처럼 $BUN_INSTALL/bin 에 bun 을 놓는다. 진짜 설치기는
# `case $(basename "$SHELL")` 로 profile 을 고르므로 SHELL 도 기록한다.
FAKE_BUN_INSTALLER = """#!/bin/sh
printf 'bun-installer tag=%s BUN_INSTALL=%s SHELL=%s\\n' \
    "$1" "$BUN_INSTALL" "$SHELL" >> "$FAKE_NET_LOG"
if [ -n "$FAKE_BUN_INSTALL_FAIL" ]; then exit 1; fi
mkdir -p "$BUN_INSTALL/bin"
cp "$FAKE_PAYLOAD/bun" "$BUN_INSTALL/bin/bun"
chmod +x "$BUN_INSTALL/bin/bun"
exit 0
"""

# 가짜 uv: python find/install 만 흉내 낸다. 관리 Python 은 $FAKE_UV_PYTHON
# 이 가리키는 경로에 있다고 본다.
FAKE_UV = """#!/bin/sh
printf 'uv %s\\n' "$*" >> "$FAKE_NET_LOG"
case "$1 $2" in
    "--version ") printf 'uv 0.0.0-fake\\n'; exit 0 ;;
esac
if [ "$1" = --version ]; then printf 'uv 0.0.0-fake\\n'; exit 0; fi
if [ "$1" = python ] && [ "$2" = find ]; then
    [ -n "$FAKE_UV_PYTHON" ] && [ -x "$FAKE_UV_PYTHON" ] || exit 1
    printf '%s\\n' "$FAKE_UV_PYTHON"
    exit 0
fi
if [ "$1" = python ] && [ "$2" = install ]; then
    [ -n "$FAKE_UV_INSTALLS_PYTHON" ] || exit 1
    mkdir -p "$(dirname "$FAKE_UV_PYTHON")"
    printf '#!/bin/sh\\nexec %s "$@"\\n' "$FAKE_REAL_PYTHON" > "$FAKE_UV_PYTHON"
    chmod +x "$FAKE_UV_PYTHON"
    exit 0
fi
exit 0
"""

# 가짜 bun: viewer 용이라 설치기는 --version 만 묻는다. 버전 질의는 네트워크가
# 아니므로 기록하지 않고, 그 밖의 호출(있으면 안 된다)만 남긴다.
FAKE_BUN = """#!/bin/sh
if [ "$1" = --version ]; then printf '%s\\n' "${FAKE_BUN_VERSION:-1.3.14}"; exit 0; fi
printf 'bun %s\\n' "$*" >> "$FAKE_NET_LOG"
exit 0
"""

# claude / codex 의 `mcp add|get|remove` 만 흉내 내는 가짜 CLI.
# 등록부는 `$FAKE_MCP_DB.<클라이언트>` 파일 한 줄에 `이름<TAB>명령줄` 로 둔다.
FAKE_MCP_CLI = """#!/bin/sh
self=$(basename "$0")
db="$FAKE_MCP_DB.$self"
case $1 in
  --version) printf 'fake %s 0.0.0\\n' "$self"; exit 0 ;;
  mcp) ;;
  *) exit 0 ;;
esac
shift
action=$1; shift
name=""
args=""
for a in "$@"; do
    case $a in
        --scope|user|--) continue ;;
    esac
    if [ -z "$name" ]; then name=$a; else args="$args $a"; fi
done
touch "$db"
case $action in
  get)
    line=$(awk -F'\\t' -v n="$name" '$1 == n { print $2; exit }' "$db")
    [ -n "$line" ] || exit 1
    printf '%s\\n  args: %s\\n' "$name" "$line"
    ;;
  add)
    if awk -F'\\t' -v n="$name" '$1 == n { found = 1 } END { exit !found }' "$db"; then
        exit 1
    fi
    printf '%s\\t%s\\n' "$name" "${args# }" >> "$db"
    ;;
  remove)
    awk -F'\\t' -v n="$name" '$1 == n { found = 1 } END { exit !found }' "$db" || exit 1
    tmp=$db.tmp
    awk -F'\\t' -v n="$name" '$1 != n' "$db" > "$tmp"
    mv "$tmp" "$db"
    ;;
  *) exit 0 ;;
esac
exit 0
"""


class InstallerCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="llmwiki-install-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        # 개발 저장소와 다른, 일부러 깊은 경로에 clone 한다.
        self.clone = self.tmp / "somewhere" / "else" / "my-wiki"
        (self.clone / "scripts").mkdir(parents=True)
        for name in ("llmwiki.py", "llmwiki_index.py", "llmwiki_context.py", "install.sh"):
            shutil.copy2(REPO / "scripts" / name, self.clone / "scripts" / name)
        (self.clone / "scripts" / "install.sh").chmod(0o755)
        for rel in ("tools/config/groups.json", "tools/schema/page.schema.json"):
            dest = self.clone / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO / rel, dest)
        pages = self.clone / "wiki" / "concepts"
        pages.mkdir(parents=True)
        (pages / "corpus.json").write_text(json.dumps(CORPUS, ensure_ascii=False, indent=2),
                                           encoding="utf-8")

        self.home = self.tmp / "home"
        (self.home / ".codex").mkdir(parents=True)
        (self.home / ".claude").mkdir(parents=True)
        self.codex_hooks = self.home / ".codex" / "hooks.json"
        self.claude_settings = self.home / ".claude" / "settings.json"
        self.codex_guide = self.home / ".codex" / "AGENTS.md"
        self.claude_guide = self.home / ".claude" / "CLAUDE.md"
        self.codex_hooks.write_text(json.dumps(FOREIGN_CODEX, indent=2), encoding="utf-8")
        self.claude_settings.write_text(json.dumps(FOREIGN_CLAUDE, indent=2), encoding="utf-8")
        self.codex_guide.write_text(FOREIGN_AGENTS, encoding="utf-8")

        # codex/claude/bun 이 없는 PATH — 감지 결과를 테스트가 통제한다.
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        # 어떤 테스트도 실수로 네트워크를 타지 못하게, curl/wget 을 항상
        # 가짜로 덮는다. payload 를 깔지 않은 테스트에서는 그냥 실패한다.
        self.stub("curl", FAKE_CURL)
        self.stub("wget", FAKE_WGET)
        self.mcp_db = self.tmp / "mcp"
        self.mcp_state = self.home / ".llmwiki" / "installed-mcp"
        self.net_log = self.tmp / "net.log"
        self.payload = self.tmp / "payload"
        self.payload.mkdir()
        # 부트스트랩이 만들어 낼 자리들 — 전부 가짜 HOME 안이다.
        self.uv_bin = self.home / ".local" / "bin" / "uv"
        self.uv_python = self.home / "uv-python" / "python3"
        self.bun_bin = self.home / ".bun" / "bin" / "bun"

    # ------------------------------------------------------------- helpers
    def stub(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)

    def payload_file(self, name: str, body: str) -> Path:
        path = self.payload / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)
        return path

    def net_calls(self) -> str:
        return self.net_log.read_text(encoding="utf-8") if self.net_log.exists() else ""

    def env(self, **extra: str) -> dict[str, str]:
        base = {
            "HOME": str(self.home),
            "PATH": os.pathsep.join([str(self.bin), str(Path(sys.executable).parent),
                                     "/usr/bin", "/bin"]),
            "FAKE_MCP_DB": str(self.mcp_db),
            "FAKE_NET_LOG": str(self.net_log),
            "FAKE_PAYLOAD": str(self.payload),
            "FAKE_REAL_PYTHON": sys.executable,
            "FAKE_UV_PYTHON": str(self.uv_python),
            "FAKE_UV_INSTALLS_PYTHON": "",
            "FAKE_BUN_VERSION": "",
            "FAKE_NET_FAIL": "",
            "FAKE_UV_INSTALL_FAIL": "",
            "FAKE_BUN_INSTALL_FAIL": "",
        }
        for key in ("LANG", "LC_ALL", "TMPDIR"):
            if key in os.environ:
                base[key] = os.environ[key]
        base.update(extra)
        return base

    def run_installer(self, *argv: str, expect: int = 0,
                      **env: str) -> subprocess.CompletedProcess[str]:
        proc = subprocess.run([str(self.clone / "scripts" / "install.sh"), *argv],
                              capture_output=True, text=True, env=self.env(**env),
                              cwd=str(self.tmp))
        self.assertEqual(proc.returncode, expect,
                         f"argv={argv}\nstdout={proc.stdout}\nstderr={proc.stderr}")
        return proc

    def install(self, *extra: str) -> subprocess.CompletedProcess[str]:
        return self.run_installer("install", "--codex", "--claude", "--no-mcp", "--no-bun",
                                  "-q", *extra)

    def snapshot(self) -> dict[str, bytes]:
        # 바이트로 읽는다 — 가짜 HOME 에는 인터프리터가 남긴 캐시 같은
        # 텍스트가 아닌 파일도 들어올 수 있다.
        return {str(p): p.read_bytes()
                for p in sorted(self.home.rglob("*")) if p.is_file()}

    def config_snapshot(self) -> dict[str, str]:
        """클라이언트 설정만. 받아 온 도구(uv/bun)는 설정이 아니다."""
        out = {}
        for path in (self.codex_hooks, self.claude_settings, self.codex_guide,
                     self.claude_guide, self.mcp_state):
            out[str(path)] = path.read_text(encoding="utf-8") if path.exists() else None
        return out

    def read(self, path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    # ---------------------------------------------------------- MCP 헬퍼
    def fake_clients(self) -> None:
        for name in ("claude", "codex"):
            self.stub(name, FAKE_MCP_CLI)

    def mcp_db_for(self, client: str) -> Path:
        return Path(f"{self.mcp_db}.{client}")

    def registered(self, client: str) -> dict[str, str]:
        path = self.mcp_db_for(client)
        if not path.exists():
            return {}
        rows = [line.split("\t", 1) for line in
                path.read_text(encoding="utf-8").splitlines() if line]
        return {name: command for name, command in rows}

    def register_foreign(self, client: str, name: str = "llmwiki",
                         command: str = "/bin/other-server --run") -> None:
        path = self.mcp_db_for(client)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{name}\t{command}\n")

    def state_rows(self) -> list[str]:
        if not self.mcp_state.exists():
            return []
        return [r for r in self.mcp_state.read_text(encoding="utf-8").splitlines() if r]


# --------------------------------------------------------------------------- 경로
class PathResolutionTest(InstallerCase):
    def test_doctor_resolves_the_repo_root_from_the_clone(self) -> None:
        out = self.run_installer("doctor", "-q").stdout
        self.assertEqual(json.loads(out)["root"], str(self.clone))
        self.assertNotIn(str(REPO), out)

    def test_installed_hook_points_at_the_clone_not_the_dev_repo(self) -> None:
        self.install()
        command = self.read(self.codex_hooks)["hooks"]["UserPromptSubmit"][1]["hooks"][0]["command"]
        self.assertIn(str(self.clone / "scripts" / "llmwiki_context.py"), command)
        self.assertNotIn(str(REPO / "scripts"), command)

    def test_sources_hardcode_no_repository_path(self) -> None:
        for name in ("install.sh", "llmwiki_context.py"):
            text = (REPO / "scripts" / name).read_text(encoding="utf-8")
            self.assertNotIn("/Users/", text, name)
            self.assertNotIn("llmwiki_json/", text, name)

    def test_installer_works_through_a_symlink(self) -> None:
        link = self.tmp / "install-link"
        link.symlink_to(self.clone / "scripts" / "install.sh")
        proc = subprocess.run([str(link), "doctor", "-q"], capture_output=True, text=True,
                              env=self.env(), cwd="/")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["root"], str(self.clone))


# --------------------------------------------------------------------------- dry-run
class DryRunTest(InstallerCase):
    def test_dry_run_changes_nothing(self) -> None:
        # 설정과 우리가 만들 산출물 기준으로 본다. 감지 과정에서 uv/bun 에게
        # 버전을 물으면 그 도구들이 제 캐시를 만드는데, 그건 우리가 쓴 것이
        # 아니고 사용자 설정도 아니다.
        before = self.config_snapshot()
        self.run_installer("install", "--codex", "--claude", "--no-mcp", "--dry-run")
        self.assertEqual(self.config_snapshot(), before)
        self.assertFalse(self.uv_bin.exists())
        self.assertFalse(self.bun_bin.exists())
        self.assertFalse((self.clone / "index" / "search.sqlite").exists())

    def test_dry_run_reports_the_planned_command_and_targets(self) -> None:
        out = self.run_installer("install", "--codex", "--claude", "--no-mcp", "--dry-run").stdout
        plan = json.loads(out[out.index("{"):out.rindex("}") + 1])
        self.assertIn(str(self.clone), plan["command"])
        self.assertEqual(plan["clients"]["codex"]["hooks_file"], str(self.codex_hooks))
        self.assertEqual(plan["clients"]["codex"]["installed_group"], -1)

    def test_legacy_qmd_flags_are_accepted_and_ignored(self) -> None:
        # 옛 설치 명령이 그대로 돌아야 한다 — qmd 는 더 이상 준비하지 않는다.
        before = self.config_snapshot()
        proc = self.run_installer("install", "--codex", "--claude", "--no-mcp", "--dry-run",
                                  "--with-qmd", "--qmd-name", "whatever")
        self.assertIn("qmd 옵션은 더 이상 쓰지 않는다", proc.stderr)
        self.assertEqual(self.config_snapshot(), before)
        self.assertEqual(self.net_calls(), "")


# --------------------------------------------------------------------------- 병합
class NonDestructiveTest(InstallerCase):
    def test_install_appends_one_group_and_keeps_the_foreign_one(self) -> None:
        self.install()
        codex = self.read(self.codex_hooks)
        groups = codex["hooks"]["UserPromptSubmit"]
        self.assertEqual(len(groups), 2)
        self.assertEqual(groups[0], FOREIGN_CODEX["hooks"]["UserPromptSubmit"][0])
        self.assertEqual(codex["hooks"]["Stop"], FOREIGN_CODEX["hooks"]["Stop"])

    def test_unrelated_settings_survive(self) -> None:
        self.install()
        claude = self.read(self.claude_settings)
        self.assertEqual(claude["theme"], "dark")
        self.assertEqual(claude["permissions"], {"allow": ["Bash"]})
        self.assertEqual(claude["hooks"]["UserPromptSubmit"][0],
                         FOREIGN_CLAUDE["hooks"]["UserPromptSubmit"][0])

    def test_existing_guide_body_is_kept(self) -> None:
        self.install()
        guide = self.codex_guide.read_text(encoding="utf-8")
        self.assertIn("건드리면 안 되는 내용.", guide)
        self.assertIn("llmwiki-context:start", guide)

    def test_backup_holds_the_pre_install_bytes(self) -> None:
        original = self.codex_hooks.read_text(encoding="utf-8")
        self.install()
        self.install()  # 두 번째 설치가 백업을 덮어쓰면 안 된다
        backup = self.home / ".codex" / "hooks.json.llmwiki-bak"
        self.assertEqual(backup.read_text(encoding="utf-8"), original)

    def test_wiki_is_never_written_to(self) -> None:
        pages = self.clone / "wiki" / "concepts" / "corpus.json"
        before = pages.read_bytes()
        self.install()
        self.run_installer("verify", "--codex", "--claude", "-q", expect=1)
        self.assertEqual(pages.read_bytes(), before)


# --------------------------------------------------------------------------- 멱등
class IdempotencyTest(InstallerCase):
    def test_three_installs_leave_one_group(self) -> None:
        self.install()
        first = self.snapshot()
        self.install()
        self.install()
        self.assertEqual(self.snapshot(), first)
        for path in (self.codex_hooks, self.claude_settings):
            groups = self.read(path)["hooks"]["UserPromptSubmit"]
            self.assertEqual(len(groups), 2, path)

    def test_reinstall_after_moving_the_clone_replaces_the_stale_command(self) -> None:
        self.install()
        moved = self.tmp / "moved-wiki"
        shutil.move(str(self.clone), str(moved))
        self.clone = moved
        self.install()
        groups = self.read(self.codex_hooks)["hooks"]["UserPromptSubmit"]
        self.assertEqual(len(groups), 2)
        self.assertIn(str(moved), groups[1]["hooks"][0]["command"])


# --------------------------------------------------------------------------- 롤백
class UninstallTest(InstallerCase):
    def uninstall(self, *extra: str) -> None:
        self.run_installer("uninstall", "--codex", "--claude", "--no-mcp", "-q", *extra)

    def test_uninstall_restores_the_foreign_config(self) -> None:
        self.install()
        self.uninstall()
        self.assertEqual(self.read(self.codex_hooks), FOREIGN_CODEX)
        self.assertEqual(self.read(self.claude_settings), FOREIGN_CLAUDE)

    def test_uninstall_restores_a_guide_it_did_not_create(self) -> None:
        self.install()
        self.uninstall()
        self.assertEqual(self.codex_guide.read_text(encoding="utf-8"), FOREIGN_AGENTS)

    def test_uninstall_removes_a_guide_it_created(self) -> None:
        self.install()
        self.assertTrue(self.claude_guide.exists())
        self.uninstall()
        self.assertFalse(self.claude_guide.exists())

    def test_uninstall_leaves_no_empty_hooks_shell(self) -> None:
        self.claude_settings.write_text(json.dumps({"theme": "dark"}), encoding="utf-8")
        self.install()
        self.uninstall()
        self.assertEqual(self.read(self.claude_settings), {"theme": "dark"})

    def test_uninstall_without_install_is_harmless(self) -> None:
        before = self.snapshot()
        self.uninstall()
        self.assertEqual(self.read(self.codex_hooks), FOREIGN_CODEX)
        self.assertEqual(set(before) - set(self.snapshot()), set())

    def test_uninstall_reports_backups(self) -> None:
        self.install()
        proc = self.run_installer("uninstall", "--codex", "--claude", "--no-mcp")
        self.assertIn("남은 백업", proc.stdout)


# --------------------------------------------------------------------------- 선택
class ClientSelectionTest(InstallerCase):
    def test_claude_only_leaves_codex_untouched(self) -> None:
        original = self.codex_hooks.read_text(encoding="utf-8")
        self.run_installer("install", "--claude", "--no-mcp", "--no-bun", "-q")
        self.assertEqual(self.codex_hooks.read_text(encoding="utf-8"), original)
        self.assertTrue(self.claude_guide.exists())
        self.assertEqual(len(self.read(self.claude_settings)["hooks"]["UserPromptSubmit"]), 2)

    def test_codex_only_leaves_claude_untouched(self) -> None:
        original = self.claude_settings.read_text(encoding="utf-8")
        self.run_installer("install", "--codex", "--no-mcp", "--no-bun", "-q")
        self.assertEqual(self.claude_settings.read_text(encoding="utf-8"), original)
        self.assertFalse(self.claude_guide.exists())

    def test_no_guides_skips_the_guide_files(self) -> None:
        self.install("--no-guides")
        self.assertEqual(self.codex_guide.read_text(encoding="utf-8"), FOREIGN_AGENTS)
        self.assertFalse(self.claude_guide.exists())
        self.assertEqual(len(self.read(self.codex_hooks)["hooks"]["UserPromptSubmit"]), 2)

    def test_no_detected_client_is_a_clear_error(self) -> None:
        proc = self.run_installer("install", "-q", expect=1)
        self.assertIn("설치할 클라이언트가 없다", proc.stderr)


# --------------------------------------------------------------------------- 환경
class EnvironmentTest(InstallerCase):
    def test_unsupported_os_refuses_by_default(self) -> None:
        self.stub("uname", FAKE_UNAME)
        proc = self.run_installer("doctor", "-q", expect=1)
        self.assertIn("지원 범위 밖", proc.stderr)

    def test_unsupported_os_proceeds_with_force(self) -> None:
        self.stub("uname", FAKE_UNAME)
        proc = self.run_installer("doctor", "-q", "--force")
        self.assertIn("검증되지 않았다", proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["root"], str(self.clone))

    def test_unknown_argument_exits_two(self) -> None:
        self.run_installer("--nope", expect=2)

    def test_help_exits_zero(self) -> None:
        self.assertIn("scripts/install.sh", self.run_installer("--help").stdout)

    def test_python_override_is_pinned_into_the_hook(self) -> None:
        self.run_installer("install", "--codex", "--no-mcp", "--no-bun", "-q",
                           "--python", "/usr/bin/python3")
        command = self.read(self.codex_hooks)["hooks"]["UserPromptSubmit"][1]["hooks"][0]["command"]
        self.assertIn("/usr/bin/python3", command)


# --------------------------------------------------------------------------- MCP
class McpOwnershipTest(InstallerCase):
    """설치기가 실제로 만든 MCP 서버만 제거한다."""

    def setUp(self) -> None:
        super().setUp()
        self.fake_clients()

    def with_mcp(self, command: str, *extra: str) -> subprocess.CompletedProcess[str]:
        return self.run_installer(command, "--codex", "--claude", "--no-bun", "-q", *extra)

    def output(self, proc: subprocess.CompletedProcess[str]) -> str:
        return proc.stdout + proc.stderr

    def test_install_registers_and_records_ownership(self) -> None:
        self.with_mcp("install")
        for client in ("claude", "codex"):
            self.assertIn("llmwiki", self.registered(client), client)
            self.assertIn(str(self.clone / "scripts" / "llmwiki_context.py"),
                          self.registered(client)["llmwiki"])
        rows = self.state_rows()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(str(self.clone) in row for row in rows))

    def test_uninstall_removes_only_what_it_registered(self) -> None:
        self.with_mcp("install")
        self.with_mcp("uninstall")
        self.assertEqual(self.registered("claude"), {})
        self.assertEqual(self.registered("codex"), {})
        self.assertEqual(self.state_rows(), [])
        self.assertFalse(self.mcp_state.exists())

    def test_a_pre_existing_server_is_never_registered_over(self) -> None:
        self.register_foreign("claude")
        out = self.output(self.with_mcp("install"))
        self.assertEqual(self.registered("claude")["llmwiki"], "/bin/other-server --run")
        self.assertIn("우리가 만든 것이 아니다", out)
        # 우리 것이 아니므로 소유 기록에도 남지 않는다
        self.assertEqual([r for r in self.state_rows() if r.startswith("claude")], [])

    def test_uninstall_keeps_a_pre_existing_server(self) -> None:
        self.register_foreign("claude")
        self.with_mcp("install")
        out = self.output(self.with_mcp("uninstall"))
        self.assertEqual(self.registered("claude")["llmwiki"], "/bin/other-server --run")
        self.assertIn("우리가 등록한 기록이 없다", out)
        self.assertIn("claude mcp remove", out)  # 직접 지우는 법을 알려준다
        self.assertEqual(self.registered("codex"), {})  # codex 쪽은 우리 것이라 제거됨

    def test_uninstall_keeps_a_server_replaced_after_install(self) -> None:
        self.with_mcp("install")
        db = self.mcp_db_for("claude")
        db.write_text("llmwiki\t/bin/someone-elses-server\n", encoding="utf-8")
        out = self.output(self.with_mcp("uninstall"))
        self.assertEqual(self.registered("claude")["llmwiki"], "/bin/someone-elses-server")
        self.assertIn("설치 후 다른 서버로 바뀌었다", out)
        self.assertEqual(self.state_rows(), [])  # 낡은 기록은 지운다

    def test_install_refreshes_its_own_stale_registration_after_a_move(self) -> None:
        self.with_mcp("install")
        moved = self.tmp / "moved-clone"
        shutil.move(str(self.clone), str(moved))
        self.clone = moved
        self.with_mcp("install")
        for client in ("claude", "codex"):
            self.assertIn(str(moved / "scripts" / "llmwiki_context.py"),
                          self.registered(client)["llmwiki"], client)
        self.assertTrue(all(str(moved) in row for row in self.state_rows()))

    def test_reinstall_does_not_duplicate_the_registration(self) -> None:
        self.with_mcp("install")
        self.with_mcp("install")
        self.with_mcp("install")
        self.assertEqual(len(self.registered("claude")), 1)
        self.assertEqual(len(self.state_rows()), 2)

    def test_no_mcp_flag_registers_nothing(self) -> None:
        self.with_mcp("install", "--no-mcp")
        self.assertEqual(self.registered("claude"), {})
        self.assertFalse(self.mcp_state.exists())

    def test_dry_run_registers_nothing_and_records_nothing(self) -> None:
        self.with_mcp("install", "--dry-run")
        self.assertEqual(self.registered("claude"), {})
        self.assertFalse(self.mcp_state.exists())

    def test_dry_run_uninstall_keeps_the_registration_and_the_record(self) -> None:
        self.with_mcp("install")
        before = self.state_rows()
        self.with_mcp("uninstall", "--dry-run")
        self.assertIn("llmwiki", self.registered("claude"))
        self.assertEqual(self.state_rows(), before)

    def test_custom_mcp_name_is_tracked_separately(self) -> None:
        self.with_mcp("install", "--mcp-name", "wiki-a")
        self.register_foreign("claude", name="wiki-b")
        self.with_mcp("uninstall", "--mcp-name", "wiki-b")
        self.assertEqual(self.registered("claude")["wiki-b"], "/bin/other-server --run")
        self.assertIn("wiki-a", self.registered("claude"))


# --------------------------------------------------------------------------- 공백 경로
class SpacedPythonPathTest(InstallerCase):
    """--python 에 공백이 있어도 모든 하위 명령이 정확히 동작한다."""

    def setUp(self) -> None:
        super().setUp()
        self.spaced = self.tmp / "my python dir"
        self.spaced.mkdir()
        self.interpreter = self.spaced / "python3"
        self.interpreter.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n',
                                    encoding="utf-8")
        self.interpreter.chmod(0o755)

    def spaced_run(self, command: str, *extra: str,
                   expect: int = 0) -> subprocess.CompletedProcess[str]:
        return self.run_installer(command, "--claude", "--no-mcp", "--no-bun", "-q",
                                  "--python", str(self.interpreter), *extra, expect=expect)

    def installed_command(self) -> str:
        groups = self.read(self.claude_settings)["hooks"]["UserPromptSubmit"]
        return groups[-1]["hooks"][0]["command"]

    def test_install_pins_the_spaced_interpreter_as_one_argument(self) -> None:
        self.spaced_run("install")
        command = self.installed_command()
        self.assertIn(f"'{self.interpreter}'", command)
        self.assertNotIn(f"[ -x {self.interpreter} ]", command)  # 인용 없이 새면 안 된다

    def test_the_generated_hook_actually_runs(self) -> None:
        self.spaced_run("install")
        payload = json.dumps({"prompt": "폐기 ICD 코드는 전체 몇 건인가?", "cwd": "/tmp",
                              "hook_event_name": "UserPromptSubmit"}, ensure_ascii=False)
        proc = subprocess.run(["/bin/sh", "-c", self.installed_command()], input=payload,
                              capture_output=True, text=True, env=self.env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        context = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("폐기-icd-코드", context)

    def test_verify_accepts_the_spaced_interpreter(self) -> None:
        self.spaced_run("install")
        out = self.spaced_run("verify", expect=0).stdout
        self.assertIn("ok   python:", out)
        self.assertIn("ok   claude.hook-current:", out)
        self.assertIn("모든 점검 통과", out)

    def test_dry_run_plan_quotes_the_spaced_interpreter(self) -> None:
        out = self.spaced_run("install", "--dry-run").stdout
        plan = json.loads(out[out.index("{"):out.rindex("}") + 1])
        self.assertIn(f"'{self.interpreter}'", plan["command"])

    def test_doctor_survives_the_spaced_interpreter(self) -> None:
        payload = json.loads(self.spaced_run("doctor").stdout)
        self.assertEqual(payload["root"], str(self.clone))

    def test_uninstall_restores_the_config(self) -> None:
        self.spaced_run("install")
        self.spaced_run("uninstall")
        self.assertEqual(self.read(self.claude_settings), FOREIGN_CLAUDE)

    def test_guide_quotes_the_spaced_interpreter(self) -> None:
        self.spaced_run("install")
        guide = self.claude_guide.read_text(encoding="utf-8")
        self.assertIn(f"'{self.interpreter}'", guide)


# --------------------------------------------------------------------------- 부트스트랩
class BootstrapCase(InstallerCase):
    """없는 도구를 받아 오는 경로. 가짜 curl/wget/uv/bun 으로 네트워크 없이 돈다."""

    def setUp(self) -> None:
        super().setUp()
        self.payload_file("uv", FAKE_UV)
        self.payload_file("bun", FAKE_BUN)
        self.payload_file("uv-install.sh", FAKE_UV_INSTALLER)
        self.payload_file("bun-install.sh", FAKE_BUN_INSTALLER)

    def boot(self, *extra: str, expect: int = 0,
             **env: str) -> subprocess.CompletedProcess[str]:
        return self.run_installer("install", "--codex", "--claude", "--no-mcp", "-q",
                                  *extra, expect=expect, **env)

    def hook_command(self) -> str:
        groups = self.read(self.codex_hooks)["hooks"]["UserPromptSubmit"]
        return groups[-1]["hooks"][0]["command"]

    def no_system_python(self) -> dict[str, str]:
        """시스템 Python 이 하나도 없는 기계를 흉내 낸다."""
        return {"LLMWIKI_PYTHON_CANDIDATES": ""}


class PythonBootstrapTest(BootstrapCase):
    def test_bootstraps_uv_and_python_when_nothing_is_available(self) -> None:
        self.boot(FAKE_UV_INSTALLS_PYTHON="1", **self.no_system_python())
        calls = self.net_calls()
        self.assertIn("astral.sh/uv", calls)
        self.assertIn("uv-installer", calls)
        self.assertIn("uv python install", calls)
        self.assertTrue(self.uv_bin.exists(), "uv 가 사용자 영역에 설치돼야 한다")
        self.assertTrue(self.uv_python.exists())
        self.assertIn(str(self.uv_python), self.hook_command())

    def test_uv_installer_is_told_not_to_touch_the_shell_profile(self) -> None:
        self.boot(FAKE_UV_INSTALLS_PYTHON="1", **self.no_system_python())
        line = [l for l in self.net_calls().splitlines() if l.startswith("uv-installer")][0]
        self.assertIn("UV_NO_MODIFY_PATH=1", line)
        self.assertIn("INSTALLER_NO_MODIFY_PATH=1", line)
        self.assertIn(f"UV_INSTALL_DIR={self.home}/.local/bin", line)
        self.assertEqual(sorted(p.name for p in self.home.iterdir() if p.is_file()), [])

    def test_an_existing_uv_managed_python_beats_the_system_one(self) -> None:
        self.stub("uv", FAKE_UV)
        self.uv_python.parent.mkdir(parents=True)
        self.uv_python.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n',
                                  encoding="utf-8")
        self.uv_python.chmod(0o755)
        self.boot("--no-bun")
        self.assertIn(str(self.uv_python), self.hook_command())
        # uv 를 부르기만 했지 아무것도 내려받지 않았다
        self.assertNotIn("astral.sh", self.net_calls())
        self.assertNotIn("uv-installer", self.net_calls())
        self.assertIn("uv-관리", self.run_installer("doctor").stdout)

    def test_system_python_is_used_when_there_is_no_uv(self) -> None:
        self.boot("--no-bun")
        self.assertIn("시스템", self.run_installer("doctor").stdout)
        self.assertEqual(self.net_calls(), "")

    def test_a_stable_absolute_python_beats_a_path_shim(self) -> None:
        """PATH 의 python3 는 pyenv shim 일 수 있다 — hook 에는 붙박이를 박는다."""
        shim = self.bin / "python3"
        shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', encoding="utf-8")
        shim.chmod(0o755)
        self.boot("--no-bun")
        command = self.hook_command()
        self.assertIn("/usr/bin/python3", command)
        self.assertNotIn(str(shim), command)

    def test_user_python_beats_everything_including_uv(self) -> None:
        self.stub("uv", FAKE_UV)
        self.uv_python.parent.mkdir(parents=True)
        self.uv_python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.uv_python.chmod(0o755)
        self.boot("--no-bun", "--python", sys.executable)
        self.assertIn(sys.executable, self.hook_command())
        self.assertEqual(self.net_calls(), "")

    def test_a_bad_user_python_is_rejected_before_anything_is_written(self) -> None:
        bad = self.tmp / "not-python"
        bad.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        bad.chmod(0o755)
        before = self.config_snapshot()
        proc = self.boot("--python", str(bad), expect=1)
        self.assertIn("3.9 이상", proc.stderr)
        self.assertEqual(self.config_snapshot(), before)

    def test_no_bootstrap_without_any_python_fails_and_writes_nothing(self) -> None:
        before = self.config_snapshot()
        proc = self.boot("--no-bootstrap", expect=1, **self.no_system_python())
        self.assertIn("--no-bootstrap", proc.stderr)
        self.assertEqual(self.config_snapshot(), before)
        self.assertEqual(self.net_calls(), "")

    def test_a_download_failure_aborts_before_touching_config(self) -> None:
        before = self.config_snapshot()
        proc = self.boot(expect=1, FAKE_NET_FAIL="1", **self.no_system_python())
        self.assertIn("받지 못했다", proc.stderr)
        self.assertEqual(self.config_snapshot(), before)

    def test_an_installer_failure_aborts_before_touching_config(self) -> None:
        before = self.config_snapshot()
        proc = self.boot(expect=1, FAKE_UV_INSTALL_FAIL="1", **self.no_system_python())
        self.assertIn("uv 설치가 실패했다", proc.stderr)
        self.assertEqual(self.config_snapshot(), before)

    def test_uv_python_install_failure_aborts(self) -> None:
        before = self.config_snapshot()
        proc = self.boot(expect=1, **self.no_system_python())  # FAKE_UV_INSTALLS_PYTHON 미설정
        self.assertIn("uv python install", proc.stderr)
        self.assertEqual(self.config_snapshot(), before)

    def test_wget_is_used_when_curl_is_missing(self) -> None:
        # curl 을 실행 불가로 만들어 `command -v curl` 이 못 찾게 한다.
        (self.bin / "curl").chmod(0o644)
        path_without_curl = self.tmp / "nocurl"
        path_without_curl.mkdir()
        for tool in ("sh", "awk", "sed", "mktemp", "mv", "rm", "cp", "mkdir", "chmod",
                     "cat", "grep", "head", "basename", "dirname", "uname", "readlink",
                     "ls", "touch", "env", "python3"):
            for root in ("/usr/bin", "/bin"):
                candidate = Path(root) / tool
                if candidate.exists():
                    (path_without_curl / tool).symlink_to(candidate)
                    break
        proc = subprocess.run(
            [str(self.clone / "scripts" / "install.sh"), "install", "--codex", "--claude",
             "--no-mcp", "--no-bun", "-q"],
            capture_output=True, text=True, cwd=str(self.tmp),
            env=self.env(PATH=os.pathsep.join([str(self.bin), str(path_without_curl)]),
                         FAKE_UV_INSTALLS_PYTHON="1", **self.no_system_python()))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("wget https://astral.sh", self.net_calls())
        self.assertNotIn("curl ", self.net_calls())


class BunBootstrapTest(BootstrapCase):
    """viewer 가 쓰는 Bun. 저장소 규칙(1.3.14)대로 사용자 영역에 받아 온다."""

    def test_bun_is_installed_by_default(self) -> None:
        self.boot()
        calls = self.net_calls()
        self.assertIn("bun.sh/install", calls)
        self.assertIn("bun-installer", calls)
        self.assertTrue(self.bun_bin.exists())

    def test_bun_is_pinned_to_the_repository_version_and_stays_user_local(self) -> None:
        self.boot()
        line = [l for l in self.net_calls().splitlines() if l.startswith("bun-installer")][0]
        self.assertIn("tag=bun-v1.3.14", line)
        self.assertIn(f"BUN_INSTALL={self.home}/.bun", line)
        # SHELL=/bin/sh 면 공식 설치기의 profile 수정 분기를 타지 않는다.
        self.assertIn("SHELL=/bin/sh", line)
        for profile in (".zshrc", ".bashrc", ".bash_profile", ".profile"):
            self.assertFalse((self.home / profile).exists(), profile)

    def test_an_existing_bun_is_used_and_never_replaced(self) -> None:
        self.stub("bun", FAKE_BUN)
        self.boot(FAKE_BUN_VERSION="1.2.0")
        self.assertNotIn("bun.sh/install", self.net_calls())
        self.assertFalse(self.bun_bin.exists())

    def test_an_existing_bun_of_another_version_only_warns(self) -> None:
        self.stub("bun", FAKE_BUN)
        proc = self.boot(FAKE_BUN_VERSION="1.2.0")
        self.assertIn("1.2.0", proc.stderr)
        self.assertIn("그대로 쓴다", proc.stderr)

    def test_no_bun_skips_the_bun_bootstrap_entirely(self) -> None:
        proc = self.boot("--no-bun")
        self.assertEqual(self.net_calls(), "")
        self.assertFalse(self.bun_bin.exists())
        self.assertNotIn("도구 준비", proc.stdout)

    def test_missing_bun_with_no_bootstrap_is_only_a_warning(self) -> None:
        proc = self.boot("--no-bootstrap")
        self.assertIn("--no-bootstrap", proc.stderr)
        self.assertIn("Bun", proc.stderr)
        self.assertEqual(self.net_calls(), "")  # 네트워크를 타지 않는다
        self.assertFalse(self.bun_bin.exists())
        # hook 은 Bun 과 무관하므로 그대로 설치된다.
        self.assertEqual(len(self.read(self.codex_hooks)["hooks"]["UserPromptSubmit"]), 2)

    def test_a_bun_install_failure_aborts_before_touching_config(self) -> None:
        before = self.config_snapshot()
        proc = self.boot(expect=1, FAKE_BUN_INSTALL_FAIL="1")
        self.assertIn("Bun 1.3.14 설치가 실패했다", proc.stderr)
        self.assertEqual(self.config_snapshot(), before)

    def test_bootstrap_is_idempotent(self) -> None:
        self.boot()
        first = self.snapshot()
        self.net_log.unlink()
        self.boot()
        self.assertEqual(self.net_calls(), "")  # 두 번째는 받아 오지 않는다
        self.assertEqual(self.snapshot(), first)

    def test_doctor_and_verify_never_install_bun(self) -> None:
        for command, exit_code in (("doctor", 0), ("verify", 1)):  # 미설치라 verify 는 1
            self.net_log.unlink(missing_ok=True)
            self.run_installer(command, "--codex", "--claude", expect=exit_code)
            self.assertEqual(self.net_calls(), "", command)
            self.assertFalse(self.bun_bin.exists(), command)


class BootstrapDryRunTest(BootstrapCase):
    def test_dry_run_never_touches_the_network_or_the_disk(self) -> None:
        before = self.config_snapshot()
        out = self.run_installer("install", "--codex", "--claude", "--no-mcp", "--dry-run",
                                 **self.no_system_python()).stdout
        self.assertEqual(self.net_calls(), "")
        self.assertEqual(self.config_snapshot(), before)
        self.assertFalse(self.uv_bin.exists())
        self.assertFalse(self.bun_bin.exists())
        self.assertFalse((self.clone / "index" / "search.sqlite").exists())
        self.assertIn("uv 를 받아", out)
        self.assertIn("Bun 1.3.14 설치", out)

    def test_dry_run_reports_the_downloader_it_would_use(self) -> None:
        out = self.run_installer("doctor", **self.no_system_python()).stdout
        self.assertIn("다운로더        curl", out)


class BootstrapReportTest(BootstrapCase):
    def test_doctor_shows_source_and_version_for_every_tool(self) -> None:
        self.boot()
        out = self.run_installer("doctor").stdout
        self.assertRegex(out, r"python .*\[3\.\d+\.\d+, 시스템\]")
        self.assertIn("bun             " + str(self.bun_bin), out)
        self.assertIn("1.3.14, 기존", out)
        self.assertIn("검색 색인       index/search.sqlite", out)
        self.assertIn("부트스트랩      허용", out)

    def test_doctor_marks_bootstrap_as_forbidden(self) -> None:
        out = self.run_installer("doctor", "--no-bootstrap").stdout
        self.assertIn("금지 (--no-bootstrap)", out)

    def test_verify_reports_the_bootstrapped_interpreter(self) -> None:
        self.boot(FAKE_UV_INSTALLS_PYTHON="1", **self.no_system_python())
        out = self.run_installer("verify", "--codex", "--claude", expect=1).stdout
        self.assertIn(f"ok   python: {self.uv_python}", out)
        self.assertIn("ok   codex.hook-current:", out)


class SpacedHomeBootstrapTest(BootstrapCase):
    """공백이 든 $HOME 에서도 받아 온 도구 경로가 깨지지 않는다."""

    def setUp(self) -> None:
        super().setUp()
        spaced = self.tmp / "my home dir"
        shutil.move(str(self.home), str(spaced))
        self.home = spaced
        self.codex_hooks = self.home / ".codex" / "hooks.json"
        self.claude_settings = self.home / ".claude" / "settings.json"
        self.codex_guide = self.home / ".codex" / "AGENTS.md"
        self.claude_guide = self.home / ".claude" / "CLAUDE.md"
        self.mcp_state = self.home / ".llmwiki" / "installed-mcp"
        self.uv_bin = self.home / ".local" / "bin" / "uv"
        self.uv_python = self.home / "uv-python" / "python3"
        self.bun_bin = self.home / ".bun" / "bin" / "bun"

    def test_uv_and_bun_land_under_the_spaced_home(self) -> None:
        self.boot(FAKE_UV_INSTALLS_PYTHON="1", **self.no_system_python())
        self.assertTrue(self.uv_bin.exists())
        self.assertTrue(self.bun_bin.exists())
        command = self.hook_command()
        self.assertIn(f"'{self.uv_python}'", command)

    def test_the_generated_hook_runs_from_a_spaced_home(self) -> None:
        self.boot(FAKE_UV_INSTALLS_PYTHON="1", **self.no_system_python())
        payload = json.dumps({"prompt": "폐기 ICD 코드는 전체 몇 건인가?", "cwd": "/tmp",
                              "hook_event_name": "UserPromptSubmit"}, ensure_ascii=False)
        proc = subprocess.run(["/bin/sh", "-c", self.hook_command()], input=payload,
                              capture_output=True, text=True, env=self.env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        context = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("폐기-icd-코드", context)


# --------------------------------------------------------------------------- verify
class VerifyTest(InstallerCase):
    def verify(self, expect: int) -> str:
        return self.run_installer("verify", "--codex", "--claude", "-q", expect=expect).stdout

    def test_verify_fails_before_install(self) -> None:
        out = self.verify(1)
        self.assertIn("FAIL codex.hook", out)
        self.assertIn("FAIL claude.hook", out)

    def test_verify_sees_the_installed_hook_and_probes_end_to_end(self) -> None:
        self.install()
        out = self.verify(1)  # 가짜 HOME 에는 Codex 신뢰 기록이 없으므로 1
        self.assertIn("ok   codex.hook:", out)
        self.assertIn("ok   claude.hook:", out)
        self.assertIn("ok   probe-injects:", out)
        self.assertIn("ok   probe-silent-on-noise:", out)
        self.assertIn("ok   fail-open:", out)
        self.assertIn("FAIL codex.trust", out)

    def test_verify_flags_a_stale_command_after_the_clone_moves(self) -> None:
        self.install()
        moved = self.tmp / "moved-again"
        shutil.move(str(self.clone), str(moved))
        self.clone = moved
        self.assertIn("stale command", self.verify(1))

    def test_verify_changes_nothing(self) -> None:
        self.install()
        before = self.snapshot()
        self.verify(1)
        self.assertEqual(self.snapshot(), before)


# --------------------------------------------------------------------------- 파이썬 API
class TrustStateTest(InstallerCase):
    def test_trust_is_unknown_without_a_codex_config(self) -> None:
        self.install()
        env = dict(os.environ, HOME=str(self.home))
        state = subprocess.run(
            [sys.executable, "-c",
             "import importlib.util,sys;"
             "spec=importlib.util.spec_from_file_location('m',sys.argv[1]);"
             "m=importlib.util.module_from_spec(spec);sys.modules['m']=m;"
             "spec.loader.exec_module(m);"
             "h,_=m.client_paths('codex');"
             "print(m.codex_trust(h, m.installed_group_index(h)))",
             str(self.clone / "scripts" / "llmwiki_context.py")],
            capture_output=True, text=True, env=env).stdout.strip()
        self.assertEqual(state, "unknown")

    def test_a_changed_command_invalidates_trust_and_verify_says_so(self) -> None:
        """명령이 바뀌면 Codex 는 다시 묻는다 — verify 가 trusted 라고 하면 거짓말이다."""
        hooks = self.codex_hooks
        config = self.home / ".codex" / "config.toml"
        self.run_installer("install", "--codex", "--no-mcp", "--no-bun", "-q",
                           "--python", "/usr/bin/python3")
        # Codex 가 그 명령을 신뢰한 상태를 만든다.
        config.write_text(
            f'[hooks.state."{hooks}:user_prompt_submit:1:0"]\n'
            'trusted_hash = "sha256:old-command-hash"\n', encoding="utf-8")
        out = self.run_installer("verify", "--codex", "-q", "--python",
                                 "/usr/bin/python3").stdout
        self.assertIn("ok   codex.trust: trusted", out)

        # 인터프리터를 바꿔 다시 설치하면 그 신뢰는 무효가 된다.
        self.run_installer("install", "--codex", "--no-mcp", "--no-bun", "-q",
                           "--python", sys.executable)
        out = self.run_installer("verify", "--codex", "-q", "--python", sys.executable,
                                 expect=1).stdout
        self.assertIn("FAIL codex.trust: review-required", out)

        # 사용자가 다시 신뢰하면 Codex 가 새 지문을 쓴다 → 다시 trusted.
        config.write_text(
            f'[hooks.state."{hooks}:user_prompt_submit:1:0"]\n'
            'trusted_hash = "sha256:new-command-hash"\n', encoding="utf-8")
        out = self.run_installer("verify", "--codex", "-q", "--python",
                                 sys.executable).stdout
        self.assertIn("ok   codex.trust: trusted", out)

    def test_uninstall_clears_the_stale_trust_marker(self) -> None:
        marker = self.home / ".llmwiki" / "codex-trust-stale"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("sha256:whatever\n", encoding="utf-8")
        self.run_installer("uninstall", "--codex", "--no-mcp", "-q")
        self.assertFalse(marker.exists())

    def test_trust_reads_the_hooks_state_table(self) -> None:
        hooks = self.home / ".codex" / "hooks.json"
        config = self.home / ".codex" / "config.toml"
        config.write_text(
            f'[hooks.state."{hooks}:user_prompt_submit:1:0"]\n'
            'trusted_hash = "sha256:deadbeef"\n', encoding="utf-8")
        old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home)
        self.addCleanup(lambda: os.environ.__setitem__("HOME", old_home or ""))
        self.assertEqual(ctx.codex_trust(hooks, 1), "trusted")
        self.assertEqual(ctx.codex_trust(hooks, 2), "review-required")
        self.assertEqual(ctx.codex_trust(hooks, -1), "not-installed")


if __name__ == "__main__":
    unittest.main()

"""scripts/install.ps1: Windows 설치기와 POSIX 설치기의 등가성.

Windows 설치기는 개발 머신에서 실제 Windows 클라이언트를 상대로 돌려 볼 수
없다. 그래서 이 파일이 확인하는 것은 두 가지다.

  1. 플랫폼과 무관하게 참이어야 하는 것 — hook 에 박히는 명령의 형태, 두
     설치기의 옵션 목록이 같다는 것, 저장소 경로가 어디에도 박혀 있지 않다는 것.
  2. pwsh 가 있으면 실제로 파싱하고 돌려 보는 것 — 구문 오류와 인자 처리는
     Windows 가 아니어도 그대로 드러난다.

pwsh 가 없는 기계에서는 2번만 건너뛴다. 1번은 언제나 돈다.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.support import REPO, llmwiki_context as ctx

INSTALLER_PS1 = REPO / "scripts" / "install.ps1"
INSTALLER_SH = REPO / "scripts" / "install.sh"
PWSH = shutil.which("pwsh") or shutil.which("powershell")


def run_pwsh(args: list[str], env: dict[str, str] | None = None,
             cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """설치기를 pwsh 로 실행한다. 프로필은 읽지 않는다 — 남의 설정에 흔들리면
    무엇을 시험하는지 알 수 없다."""
    return subprocess.run(
        [PWSH, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", str(INSTALLER_PS1), *args],
        capture_output=True, text=True, env=env, cwd=str(cwd or REPO), timeout=180)


# --------------------------------------------------------------- hook 명령
class WindowsHookCommandTest(unittest.TestCase):
    """hook 에 박히는 한 줄. 이것이 틀리면 설치기가 아무리 옳아도 소용없다."""

    PY = r"C:\Program Files\Python312\python.exe"
    SCRIPT = r"C:\wiki\scripts\llmwiki_context.py"

    def command(self) -> str:
        return ctx.hook_command(self.PY, Path(self.SCRIPT), windows=True)

    def test_windows_form_is_cmd_not_sh(self) -> None:
        command = self.command()
        self.assertNotIn("[ -r", command)
        self.assertNotIn("/dev/null", command)
        self.assertIn("2>nul", command)

    def test_paths_with_spaces_are_double_quoted(self) -> None:
        # shlex.quote 는 홑따옴표를 쓴다. cmd 는 그것을 경로의 일부로 읽으므로
        # 'C:\Program Files\...' 는 통째로 없는 파일이 된다.
        command = self.command()
        self.assertIn(f'"{self.PY}"', command)
        self.assertIn(f'"{self.SCRIPT}"', command)
        self.assertNotIn(f"'{self.PY}'", command)

    def test_it_falls_back_instead_of_failing(self) -> None:
        # 인터프리터나 스크립트가 사라져도 프롬프트를 막으면 안 된다.
        command = self.command()
        self.assertIn("||", command)
        self.assertIn("more>nul", command)

    def test_the_marker_survives_so_verify_can_find_it(self) -> None:
        self.assertIn(ctx.HOOK_MARKER, self.command())

    def test_posix_form_is_untouched(self) -> None:
        posix = ctx.hook_command(self.PY, Path(self.SCRIPT), windows=False)
        self.assertIn("[ -r", posix)
        self.assertNotIn("2>nul", posix)

    def test_platform_default_follows_the_host(self) -> None:
        default = ctx.hook_command(self.PY, Path(self.SCRIPT))
        expected = ctx.hook_command(self.PY, Path(self.SCRIPT),
                                    windows=os.name == "nt")
        self.assertEqual(default, expected)


class WindowsHookRoundTripTest(unittest.TestCase):
    """Windows 명령이 설정 파일을 왕복해도 그대로인가.

    경로의 역슬래시는 JSON 에서 `\\` 로 이스케이프된다. 쓸 때와 읽을 때가
    어긋나면 verify 가 매번 "stale command" 라고 말하면서 재설치를 시키고,
    재설치는 또 같은 값을 써서 낫지 않는다.
    """

    PY = r"C:\Program Files\Python312\python.exe"
    SCRIPT = r"C:\Users\me\my wiki\scripts\llmwiki_context.py"

    def test_the_command_survives_the_json_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Path(tmp) / "settings.json"
            command = ctx.hook_command(self.PY, Path(self.SCRIPT), windows=True)
            ctx.install_hook(settings, command, remove=False)
            self.assertEqual(ctx.installed_command(settings), command)
            self.assertGreaterEqual(ctx.installed_group_index(settings), 0)

    def test_uninstall_finds_the_windows_group_again(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Path(tmp) / "settings.json"
            command = ctx.hook_command(self.PY, Path(self.SCRIPT), windows=True)
            ctx.install_hook(settings, command, remove=False)
            ctx.install_hook(settings, command, remove=True)
            self.assertEqual(ctx.installed_group_index(settings), -1)


# --------------------------------------------------------------- 등가성
def long_flags(text: str) -> set[str]:
    return set(re.findall(r"--[a-z][a-z0-9-]+", text))


def accepted_flags(sh: str) -> set[str]:
    """install.sh 가 *자기 CLI 옵션으로* 받는 것만. 인자 파싱 case 문이 정본이다.

    본문에는 git/python 을 부르는 줄도 있어서(`git merge --ff-only` 등) 파일
    전체를 긁으면 남의 도구 플래그까지 옵션으로 오인한다.
    """
    start = sh.index("while [ $# -gt 0 ]")
    end = sh.index("\ndone", start)
    return long_flags(sh[start:end])


class ParityTest(unittest.TestCase):
    """두 설치기가 따로 자라나지 않게 묶어 둔다."""

    def setUp(self) -> None:
        self.sh = INSTALLER_SH.read_text(encoding="utf-8")
        self.ps1 = INSTALLER_PS1.read_text(encoding="utf-8")

    def test_every_posix_option_is_accepted_on_windows(self) -> None:
        # `--python=` 같은 형태까지 같은 이름으로 받아야, 문서 한 벌로 양쪽을
        # 설명할 수 있다.
        missing = sorted(accepted_flags(self.sh) - long_flags(self.ps1))
        self.assertEqual(missing, [], f"install.ps1 이 받지 않는 옵션: {missing}")

    def test_every_subcommand_exists_on_both(self) -> None:
        for command in ("install", "verify", "uninstall", "doctor", "update"):
            self.assertIn(command, self.ps1)
            self.assertIn(command, self.sh)

    def test_the_pinned_versions_agree(self) -> None:
        for pattern in (r"BUN_VERSION=([0-9.]+)", r"\$BunVersion\s*=\s*'([0-9.]+)'"):
            found = re.search(pattern, self.sh + self.ps1)
            self.assertIsNotNone(found, pattern)
        self.assertEqual(re.search(r"BUN_VERSION=([0-9.]+)", self.sh).group(1),
                         re.search(r"\$BunVersion\s*=\s*'([0-9.]+)'", self.ps1).group(1))

    def test_the_same_qmd_package_and_defaults(self) -> None:
        for token in ("@tobilu/qmd", "llmwiki_json", "llmwiki"):
            self.assertIn(token, self.ps1)

    def test_no_repository_path_is_baked_in(self) -> None:
        # 설치기는 어느 clone 에서도 그대로 돌아야 한다.
        self.assertNotIn("/Users/", self.ps1)
        self.assertNotIn("llmwiki_json/scripts", self.ps1)

    def test_it_reads_the_same_state_file_as_the_posix_installer(self) -> None:
        self.assertIn(".llmwiki", self.ps1)
        self.assertIn("installed-mcp", self.ps1)
        self.assertIn("LLMWIKI_STATE_DIR", self.ps1)

    def test_it_never_writes_to_the_canonical_wiki(self) -> None:
        # export-md 만이 index/markdown 을 만든다. wiki/ 쓰기는 없어야 한다.
        self.assertIn("export-md", self.ps1)
        for forbidden in ("Set-Content", "Out-File", "WriteAllText"):
            for line in self.ps1.splitlines():
                if forbidden in line:
                    self.assertNotIn("wiki", line.lower().replace("llmwiki", ""))


# --------------------------------------------------------------- 실행
@unittest.skipIf(PWSH is None, "pwsh 가 없다 — 구문·인자 검사는 건너뛴다")
class PowerShellRunTest(unittest.TestCase):
    def assert_untouched(self, home: Path) -> None:
        """설치기가 손대는 자리만 본다.

        "가짜 HOME 이 비어 있다" 로는 시험할 수 없다 — pwsh 는 제 시작 프로필
        캐시를 남기고, 감지 단계에서 부르는 `claude --version` 역시 제 설정을
        만든다. 우리 것이 아닌 흔적까지 금지하면 시험이 남의 도구 사정에 따라
        깨진다. 우리가 쓰는 파일이 하나도 생기지 않았는지만 확인한다.
        """
        for rel in (".codex/hooks.json", ".codex/AGENTS.md",
                    ".claude/settings.json", ".claude/CLAUDE.md",
                    ".llmwiki/installed-mcp"):
            self.assertFalse((home / rel).exists(), f"{rel} 이 생겼다")

    def test_the_script_parses(self) -> None:
        # 구문 오류는 Windows 를 기다릴 이유가 없다. 파서에 그대로 물린다.
        probe = (
            "$errors = $null; "
            f"$null = [System.Management.Automation.Language.Parser]::ParseFile("
            f"'{INSTALLER_PS1}', [ref]$null, [ref]$errors); "
            "if ($errors) { $errors | ForEach-Object { $_.ToString() }; exit 1 }")
        result = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-Command", probe],
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_help_exits_zero_and_lists_the_options(self) -> None:
        result = run_pwsh(["-Help"])
        self.assertEqual(result.returncode, 0, result.stderr)
        for flag in ("-DryRun", "-NoQmd", "-NoBootstrap", "-NoMcp", "-NoGuides",
                     "-Python", "-McpName", "-QmdName", "-Force", "-Quiet"):
            self.assertIn(flag, result.stdout)

    def test_unknown_argument_exits_two(self) -> None:
        result = run_pwsh(["--nope"])
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)

    def test_an_option_that_needs_a_value_says_so(self) -> None:
        result = run_pwsh(["--python"])
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)

    def test_a_bad_interpreter_is_rejected_before_anything_is_written(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            env = dict(os.environ, HOME=str(home))
            result = run_pwsh(["install", "--python", "/definitely/not/python", "--force"],
                              env=env)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn("3.9", result.stderr)
            self.assert_untouched(home)

    def test_the_value_of_an_option_is_not_reparsed_as_a_command(self) -> None:
        # `& $scriptblock` 로 다음 인자를 집으면 $i 증가가 부모에 반영되지 않아
        # 값이 다음 회차에 '알 수 없는 인자' 로 다시 읽힌다. 그 회귀를 막는다.
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, HOME=tmp)
            result = run_pwsh(["doctor", "--mcp-name", "custom-name", "--force"], env=env)
            self.assertNotIn("알 수 없는 인자", result.stderr)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_dry_run_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            env = dict(os.environ, HOME=str(home), LLMWIKI_STATE_DIR=str(home / ".llmwiki"))
            result = run_pwsh(["install", "--dry-run", "--force", "--claude",
                               "--python", sys.executable], env=env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assert_untouched(home)
            # 계획은 다 보여 줘야 한다 — 아무것도 안 했다는 말과 다르다.
            self.assertIn("dry-run", result.stdout)


if __name__ == "__main__":
    unittest.main()

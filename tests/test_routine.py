"""주기 ingest 루틴: 미처리 판정, 실행 순서, git 규율, 스케줄러 소유권."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from tests.support import REPO, WorkspaceCase, make_page

ROUTINE_SCRIPT = REPO / "scripts" / "llmwiki_routine.py"


def load_routine():
    import importlib.util

    spec = importlib.util.spec_from_file_location("llmwiki_routine", ROUTINE_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["llmwiki_routine"] = module
    spec.loader.exec_module(module)
    return module


routine = load_routine()


class RoutineCase(WorkspaceCase):
    def setUp(self) -> None:
        super().setUp()
        self.root_path = Path(self.root).resolve()
        self.state = self.root_path / "state"
        os.environ[routine.ENV_STATE_DIR] = str(self.state)

    def cli(self, *argv: str, expect: int = 0) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, routine.ENV_STATE_DIR: str(self.state)}
        proc = subprocess.run([sys.executable, str(ROUTINE_SCRIPT), "--root",
                               str(self.root_path), *argv],
                              capture_output=True, text=True, env=env)
        self.assertEqual(proc.returncode, expect, proc.stdout + proc.stderr)
        return proc

    def block_the_agent(self) -> None:
        """테스트가 진짜 에이전트를 깨우지 않게 막는다."""
        original = routine.agent_available
        routine.agent_available = lambda _agent: False  # type: ignore[assignment]
        self.addCleanup(setattr, routine, "agent_available", original)

    def write_raw_file(self, rel: str, text: str = "내용") -> Path:
        path = self.root_path / "raw" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def git(self, *args: str) -> tuple[int, str]:
        proc = subprocess.run(["git", *args], cwd=str(self.root_path),
                              capture_output=True, text=True)
        return proc.returncode, (proc.stdout + proc.stderr).strip()

    def init_repo(self) -> None:
        # 테스트 상태 디렉터리는 저장소 밖의 것이다 — 더러운 트리로 세지 않는다.
        (self.root_path / ".gitignore").write_text("state/\nremote.git/\n", encoding="utf-8")
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "user.name", "test")
        self.git("add", "-A")
        self.git("commit", "-qm", "init")


# --------------------------------------------------------------------------- 미처리 판정
class PendingTest(RoutineCase):
    def test_a_source_without_a_page_is_pending(self) -> None:
        self.write_raw_file("새-소스.md")
        self.assertEqual(routine.pending_sources(self.root_path), ["raw/새-소스.md"])

    def test_a_source_already_ingested_is_not_pending(self) -> None:
        self.write_raw_file("이미-넣음.md")
        page = make_page("이미-넣음", "# 이미 넣음\n\n본문.\n")
        page["raw_ref"] = "raw/이미-넣음.md"
        self.write_pages([page])
        self.assertEqual(routine.pending_sources(self.root_path), [])

    def test_readme_and_dotfiles_are_not_sources(self) -> None:
        self.write_raw_file("README.md")
        self.write_raw_file(".DS_Store")
        self.assertEqual(routine.pending_sources(self.root_path), [])

    def test_llmwikiignore_adds_patterns(self) -> None:
        self.write_raw_file("초안.tmp.md")
        self.write_raw_file("진짜.md")
        (self.root_path / "raw" / ".llmwikiignore").write_text("*.tmp.md\n# 주석\n",
                                                              encoding="utf-8")
        self.assertEqual(routine.pending_sources(self.root_path), ["raw/진짜.md"])

    def test_a_broken_shard_does_not_hide_everything(self) -> None:
        self.write_raw_file("소스.md")
        (self.root_path / "wiki" / "concepts" / "broken.json").write_text("{ nope",
                                                                          encoding="utf-8")
        self.assertEqual(routine.pending_sources(self.root_path), ["raw/소스.md"])

    def test_pending_command_reports_json(self) -> None:
        self.write_raw_file("소스.md")
        payload = json.loads(self.cli("pending").stdout)
        self.assertEqual(payload["pending"], ["raw/소스.md"])


# --------------------------------------------------------------------------- 실행 순서
class RunOrderTest(RoutineCase):
    def test_git_pull_is_the_first_step(self) -> None:
        self.write_raw_file("소스.md")
        report = json.loads(self.cli("run", "--dry-run", "--json").stdout)
        self.assertEqual(report["steps"][0]["step"], "pull")

    def test_no_pending_source_never_wakes_the_agent(self) -> None:
        report = json.loads(self.cli("run", "--dry-run", "--json").stdout)
        steps = [s["step"] for s in report["steps"]]
        self.assertIn("pending", steps)
        self.assertNotIn("agent", steps)

    def test_a_pending_source_reaches_the_agent(self) -> None:
        self.write_raw_file("소스.md")
        report = json.loads(self.cli("run", "--dry-run", "--json").stdout)
        self.assertIn("agent", [s["step"] for s in report["steps"]])

    def test_the_chosen_agent_shows_up_in_the_plan(self) -> None:
        self.write_raw_file("소스.md")
        report = json.loads(self.cli("run", "--dry-run", "--json", "--agent", "codex").stdout)
        ingest = [s for s in report["steps"] if s["step"] == "agent"][0]
        self.assertIn("codex", ingest["detail"])

    def test_a_repeat_of_the_same_backlog_is_skipped(self) -> None:
        # 에이전트가 이미 보고 판단한 목록으로 매시간 다시 깨우지 않는다.
        self.write_raw_file("소스.md")
        routine.write_seen(["raw/소스.md"])
        report = routine.do_run(self.root_path, agent="claude", remote="private",
                                push=False, dry_run=False, timeout=5.0)
        self.assertEqual(report.get("skipped"), "nothing-to-do")
        self.assertNotIn("agent", [s["step"] for s in report["steps"]])

    def test_a_new_source_breaks_the_skip(self) -> None:
        self.block_the_agent()
        self.write_raw_file("소스.md")
        self.write_raw_file("새것.md")
        routine.write_seen(["raw/소스.md"])
        report = routine.do_run(self.root_path, agent="claude", remote="private",
                                push=False, dry_run=False, timeout=5.0)
        self.assertNotEqual(report.get("skipped"), "nothing-to-do")

    def test_a_failed_agent_does_not_retry_the_same_backlog_forever(self) -> None:
        # 타임아웃·인증 오류로 실패해도 같은 목록으로 매시간 다시 깨우지 않는다.
        self.write_raw_file("소스.md")
        calls = []
        original = routine.run

        def fake_run(argv, **kwargs):
            if argv and "claude" in argv[0]:
                calls.append(argv)
                return 1, "실패했다"
            return original(argv, **kwargs)

        routine.run = fake_run  # type: ignore[assignment]
        routine.agent_available = lambda _a: True  # type: ignore[assignment]
        self.addCleanup(setattr, routine, "run", original)
        self.addCleanup(setattr, routine, "agent_available", routine.agent_available)

        first = routine.do_run(self.root_path, agent="claude", remote="private",
                               push=False, dry_run=False, timeout=5.0)
        self.assertEqual(first.get("stopped"), "agent-failed")
        second = routine.do_run(self.root_path, agent="claude", remote="private",
                                push=False, dry_run=False, timeout=5.0)
        self.assertEqual(second.get("skipped"), "nothing-to-do")
        self.assertEqual(len(calls), 1, "실패한 backlog 로 에이전트를 다시 불렀다")

    def test_a_commit_left_unpushed_is_pushed_next_time(self) -> None:
        # push 가 실패해도 워킹트리는 깨끗하고 raw 는 처리된 상태다. 다음 주기에
        # 다시 밀지 않으면 그 커밋은 원격에 영영 올라가지 않는다.
        bare = self.root_path / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
        self.init_repo()
        self.git("branch", "-M", "main")
        self.git("remote", "add", "private", str(bare))
        self.git("push", "-q", "private", "main")
        (self.root_path / "wiki" / "concepts" / "new.json").write_text("{}", encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-qm", "밀지 못한 커밋")
        self.assertEqual(routine.unpushed_commits(self.root_path, "private", "main"), 1)

        report = routine.do_run(self.root_path, agent="claude", remote="private",
                                push=True, dry_run=False, timeout=5.0)
        steps = {s["step"]: s for s in report["steps"]}
        self.assertIn("push-pending", steps)
        self.assertTrue(steps["push-pending"]["ok"], steps["push-pending"])
        self.assertEqual(routine.unpushed_commits(self.root_path, "private", "main"), 0)

    def test_two_routines_do_not_overlap(self) -> None:
        held = routine.acquire_lock()
        self.addCleanup(lambda: held and held.exists() and held.unlink())
        self.write_raw_file("소스.md")
        report = routine.do_run(self.root_path, agent="claude", remote="private",
                                push=False, dry_run=False, timeout=5.0)
        self.assertEqual(report.get("skipped"), "locked")


# --------------------------------------------------------------------------- git 규율
class GitDisciplineTest(RoutineCase):
    def test_a_dirty_tree_stops_before_pulling(self) -> None:
        self.init_repo()
        self.git("remote", "add", "private", str(self.root_path))
        (self.root_path / "wiki" / "dirty.json").write_text("{}", encoding="utf-8")
        ok, detail = routine.pull_first(self.root_path, "private", "main")
        self.assertFalse(ok)
        self.assertIn("커밋되지 않은 변경", detail)

    def test_a_missing_remote_is_not_an_error(self) -> None:
        self.init_repo()
        ok, detail = routine.pull_first(self.root_path, "private", "main")
        self.assertTrue(ok)
        self.assertIn("없음", detail)

    def test_a_dirty_tree_stops_even_without_a_remote(self) -> None:
        # remote 가 없다고 해서 사람이 편집 중인 트리 위에 에이전트를 풀지 않는다.
        self.init_repo()
        (self.root_path / "wiki" / "dirty.json").write_text("{}", encoding="utf-8")
        ok, detail = routine.pull_first(self.root_path, "private", "main")
        self.assertFalse(ok)
        self.assertIn("커밋되지 않은 변경", detail)

    def test_a_remote_without_the_branch_yet_still_proceeds(self) -> None:
        # 아직 한 번도 push 하지 않은 저장소에서 여기서 막으면 최초 push 에
        # 영영 도달하지 못한다.
        bare = self.root_path / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
        self.init_repo()
        self.git("remote", "add", "private", str(bare))
        ok, detail = routine.pull_first(self.root_path, "private", "main")
        self.assertTrue(ok, detail)
        self.assertIn("아직 없다", detail)

    def test_a_non_repo_is_not_mistaken_for_a_dirty_tree(self) -> None:
        ok, _ = routine.pull_first(self.root_path, "private", "main")
        self.assertTrue(ok)

    def test_nothing_is_committed_when_nothing_changed(self) -> None:
        self.init_repo()
        ok, detail = routine.commit_and_push(self.root_path, "private", "main", push=False)
        self.assertTrue(ok)
        self.assertIn("바뀐 정본 없음", detail)

    def test_a_failed_push_is_reported_as_failure(self) -> None:
        # 실패를 성공으로 보고하면 루틴이 조용히 아무것도 밀지 않는다.
        self.init_repo()
        self.git("remote", "add", "private", str(self.root_path / "nowhere.git"))
        (self.root_path / "wiki" / "concepts" / "new.json").write_text("{}", encoding="utf-8")
        ok, detail = routine.commit_and_push(self.root_path, "private", "main", push=True)
        self.assertFalse(ok)
        self.assertIn("push 실패", detail)

    def test_an_unreachable_remote_is_not_read_as_a_missing_branch(self) -> None:
        # 인증·네트워크 실패를 '아직 push 안 했다' 로 오판하면 낡은 트리에서 돈다.
        self.init_repo()
        self.git("remote", "add", "private", str(self.root_path / "nowhere.git"))
        self.assertEqual(routine.remote_branch_state(self.root_path, "private", "main"),
                         "unknown")
        ok, detail = routine.pull_first(self.root_path, "private", "main")
        self.assertFalse(ok)
        self.assertIn("조회할 수 없다", detail)

    def test_cron_spec_keeps_the_interval_even_across_midnight(self) -> None:
        # `*/N` 은 한 자리 안에서만 도므로, 그 자리의 약수가 아니면 하루
        # 경계에서 간격이 어긋난다. 약수로만 내려 적는다.
        self.assertEqual(routine.cron_spec(1800), "*/30 * * * *")
        self.assertEqual(routine.cron_spec(3600), "@hourly")
        self.assertEqual(routine.cron_spec(7200), "0 */2 * * *")
        self.assertEqual(routine.cron_spec(5 * 3600), "0 */4 * * *")
        self.assertEqual(routine.cron_spec(7 * 60), "*/6 * * * *")
        self.assertEqual(routine.cron_spec(86400), "0 0 * * *")
        self.assertEqual(routine.cron_spec(3 * 86400), "0 0 * * *")

    def test_an_unreadable_crontab_is_never_overwritten(self) -> None:
        original = routine.run
        wrote = []

        def fake_run(argv, **kwargs):
            if argv[:2] == ["crontab", "-l"]:
                return 1, "crontab: permission denied"
            if argv[:1] == ["crontab"]:
                wrote.append(argv)
            return original(argv, **kwargs)

        routine.run = fake_run  # type: ignore[assignment]
        self.addCleanup(setattr, routine, "run", original)
        readable, _ = routine.crontab_read()
        self.assertFalse(readable)
        self.assertEqual(wrote, [], "읽지도 못한 crontab 을 덮어썼다")

    def test_a_missing_crontab_is_not_an_error(self) -> None:
        original = routine.run
        routine.run = lambda argv, **kw: (1, "no crontab for user")  # type: ignore[assignment]
        self.addCleanup(setattr, routine, "run", original)
        readable, text = routine.crontab_read()
        self.assertTrue(readable)
        self.assertEqual(text, "")

    def test_ownership_survives_an_escaped_path(self) -> None:
        # plist 는 escape 해 쓰는데 검사만 원본으로 하면 자기 것을 못 알아본다.
        path = self.root_path / "escaped.plist"
        argv = [sys.executable, str(Path(routine.__file__).resolve()), "run"]
        path.write_text(routine.plist_body(argv, 3600), encoding="utf-8")
        original = routine.plist_path
        routine.plist_path = lambda: path  # type: ignore[assignment]
        self.addCleanup(setattr, routine, "plist_path", original)
        self.assertTrue(routine.ours_launchd(path))

    def test_a_failed_unload_keeps_the_plist(self) -> None:
        if routine.scheduler_kind() != "launchd":
            self.skipTest("launchd 가 아닌 호스트")
        path = self.root_path / "running.plist"
        argv = [sys.executable, str(Path(routine.__file__).resolve()), "run"]
        path.write_text(routine.plist_body(argv, 3600), encoding="utf-8")
        original_path, original_run = routine.plist_path, routine.run
        routine.plist_path = lambda: path  # type: ignore[assignment]
        routine.run = lambda a, **k: (1, "unload 실패")  # type: ignore[assignment]
        self.addCleanup(setattr, routine, "plist_path", original_path)
        self.addCleanup(setattr, routine, "run", original_run)
        routine.record_state(self.root_path, "claude", interval=3600, remote="private",
                             push=True, kind="launchd")
        plan = routine.uninstall_schedule(dry_run=False)
        self.assertFalse(plan["ok"])
        self.assertTrue(path.exists(), "내리지도 못했는데 plist 를 지웠다")
        self.assertTrue(routine.state_file().exists())

    def test_plist_escapes_paths(self) -> None:
        body = routine.plist_body(["/usr/bin/python3", "/tmp/a&b/<x>/run.py"], 3600)
        self.assertIn("&amp;", body)
        self.assertIn("&lt;x&gt;", body)
        self.assertNotIn("a&b", body)

    def test_the_project_group_file_is_committed_too(self) -> None:
        # ingest 가 모르는 프로젝트를 만나면 groups.json 을 직접 고친다. 이걸
        # 빼 두면 커밋되지 않은 채 남아 다음 차례가 dirty tree 에서 멈춘다.
        self.init_repo()
        groups = self.root_path / "tools" / "config" / "groups.json"
        groups.write_text('{"groups": []}', encoding="utf-8")
        ok, _ = routine.commit_and_push(self.root_path, "private", "main", push=False)
        self.assertTrue(ok)
        _, out = self.git("show", "--name-only", "--format=", "HEAD")
        self.assertIn("tools/config/groups.json", out)

    def test_a_detached_head_stops_the_routine(self) -> None:
        self.init_repo()
        head = self.git("rev-parse", "HEAD")[1]
        self.git("checkout", "-q", "--detach", head)
        self.assertEqual(routine.current_branch(self.root_path), "")
        report = routine.do_run(self.root_path, agent="claude", remote="private",
                                push=False, dry_run=False, timeout=5.0)
        self.assertEqual(report.get("stopped"), "detached-head")

    def test_only_wiki_and_derived_paths_are_committed(self) -> None:
        self.init_repo()
        (self.root_path / "wiki" / "concepts" / "new.json").write_text("{}", encoding="utf-8")
        (self.root_path / "unrelated.txt").write_text("건드리지 마라", encoding="utf-8")
        routine.commit_and_push(self.root_path, "private", "main", push=False)
        _, out = self.git("show", "--name-only", "--format=", "HEAD")
        self.assertIn("wiki/concepts/new.json", out)
        self.assertNotIn("unrelated.txt", out)


# --------------------------------------------------------------------------- 개인 저장소
class PrivateRemoteTest(RoutineCase):
    def test_it_adds_a_second_remote_and_leaves_origin_alone(self) -> None:
        self.init_repo()
        self.git("remote", "add", "origin", "https://example.com/upstream.git")
        report = routine.git_setup(self.root_path, url="https://example.com/mine.git",
                                   create="", remote="private", dry_run=False)
        self.assertTrue(report["ok"], report)
        self.assertEqual(routine.has_remote(self.root_path, "private"),
                         "https://example.com/mine.git")
        self.assertEqual(routine.has_remote(self.root_path, "origin"),
                         "https://example.com/upstream.git")

    def test_a_foreign_remote_with_the_same_name_is_left_alone(self) -> None:
        self.init_repo()
        self.git("remote", "add", "private", "https://example.com/someone-else.git")
        report = routine.git_setup(self.root_path, url="https://example.com/mine.git",
                                   create="", remote="private", dry_run=False)
        self.assertFalse(report["ok"])
        self.assertEqual(routine.has_remote(self.root_path, "private"),
                         "https://example.com/someone-else.git")

    def test_repeating_the_same_url_is_harmless(self) -> None:
        self.init_repo()
        for _ in range(2):
            report = routine.git_setup(self.root_path, url="https://example.com/mine.git",
                                       create="", remote="private", dry_run=False)
            self.assertTrue(report["ok"], report)

    def test_dry_run_writes_no_remote(self) -> None:
        self.init_repo()
        routine.git_setup(self.root_path, url="https://example.com/mine.git", create="",
                          remote="private", dry_run=True)
        self.assertEqual(routine.has_remote(self.root_path, "private"), "")

    def test_a_url_is_required(self) -> None:
        self.init_repo()
        report = routine.git_setup(self.root_path, url="", create="", remote="private",
                                   dry_run=False)
        self.assertFalse(report["ok"])

    def test_a_silly_repo_name_is_refused_before_calling_gh(self) -> None:
        ok, detail = routine.gh_create(self.root_path, "not a name; rm -rf /")
        self.assertFalse(ok)
        self.assertIn("이름", detail)


# --------------------------------------------------------------------------- 스케줄러
class ScheduleOwnershipTest(RoutineCase):
    def test_install_plan_pins_the_repo_and_agent(self) -> None:
        plan = routine.install_schedule(self.root_path, "codex", interval=1800,
                                        python="/usr/bin/python3", remote="private",
                                        push=True, dry_run=True)
        self.assertIn("--agent", plan["command"])
        self.assertIn("codex", plan["command"])
        self.assertIn(str(self.root_path), plan["command"])
        self.assertEqual(plan["interval"], 1800)

    def test_dry_run_records_no_state(self) -> None:
        routine.install_schedule(self.root_path, "claude", interval=3600, python=None,
                                 remote="private", push=True, dry_run=True)
        self.assertFalse(routine.state_file().exists())

    def test_uninstall_without_install_touches_nothing(self) -> None:
        plan = routine.uninstall_schedule(dry_run=False)
        self.assertTrue(plan["ok"])
        self.assertIn("기록이 없다", plan["detail"])

    def test_no_push_survives_into_the_command(self) -> None:
        plan = routine.install_schedule(self.root_path, "claude", interval=3600,
                                        python=None, remote="private", push=False,
                                        dry_run=True)
        self.assertIn("--no-push", plan["command"])

    def test_a_foreign_launchd_entry_is_left_alone(self) -> None:
        if routine.scheduler_kind() != "launchd":
            self.skipTest("launchd 가 아닌 호스트")
        path = self.root_path / "foreign.plist"
        path.write_text("<plist>남의 것</plist>", encoding="utf-8")
        original = routine.plist_path
        routine.plist_path = lambda: path  # type: ignore[assignment]
        self.addCleanup(setattr, routine, "plist_path", original)
        plan = routine.install_schedule(self.root_path, "claude", interval=3600,
                                        python=None, remote="private", push=True,
                                        dry_run=False)
        self.assertFalse(plan["ok"])
        self.assertEqual(path.read_text(encoding="utf-8"), "<plist>남의 것</plist>")
        self.assertFalse(routine.state_file().exists())

    def test_uninstall_leaves_an_entry_that_stopped_being_ours(self) -> None:
        if routine.scheduler_kind() != "launchd":
            self.skipTest("launchd 가 아닌 호스트")
        path = self.root_path / "swapped.plist"
        path.write_text("<plist>다른 사람이 갈아치웠다</plist>", encoding="utf-8")
        original = routine.plist_path
        routine.plist_path = lambda: path  # type: ignore[assignment]
        self.addCleanup(setattr, routine, "plist_path", original)
        routine.record_state(self.root_path, "claude", interval=3600, remote="private",
                             push=True, kind="launchd")
        plan = routine.uninstall_schedule(dry_run=False)
        self.assertTrue(plan["ok"])
        self.assertTrue(path.exists(), "우리 것이 아닌 항목을 지웠다")

    def test_failed_install_records_no_state(self) -> None:
        if routine.scheduler_kind() != "launchd":
            self.skipTest("launchd 가 아닌 호스트")
        path = self.root_path / "foreign2.plist"
        path.write_text("남의 것", encoding="utf-8")
        original = routine.plist_path
        routine.plist_path = lambda: path  # type: ignore[assignment]
        self.addCleanup(setattr, routine, "plist_path", original)
        routine.install_schedule(self.root_path, "claude", interval=3600, python=None,
                                 remote="private", push=True, dry_run=False)
        self.assertFalse(routine.state_file().exists())

    def test_a_failed_removal_keeps_the_ownership_record(self) -> None:
        # 지우지 못했는데 기록만 지우면, 다음 uninstall 이 그 항목에 손대지 못한다.
        if routine.scheduler_kind() != "cron":
            original_kind = routine.scheduler_kind
            routine.scheduler_kind = lambda: "cron"  # type: ignore[assignment]
            self.addCleanup(setattr, routine, "scheduler_kind", original_kind)
        original_run = routine.run
        routine.run = lambda argv, **kw: (1, "crontab: permission denied")  # type: ignore[assignment]
        self.addCleanup(setattr, routine, "run", original_run)
        routine.record_state(self.root_path, "claude", interval=3600, remote="private",
                             push=True, kind="cron")
        plan = routine.uninstall_schedule(dry_run=False)
        self.assertFalse(plan["ok"])
        self.assertTrue(routine.state_file().exists(), "지우지 못했는데 기록을 버렸다")

    def test_status_is_json_even_before_install(self) -> None:
        payload = json.loads(self.cli("status").stdout)
        self.assertFalse(payload["installed"])
        self.assertIn("scheduler", payload)


# --------------------------------------------------------------------------- 안전
class SafetyTest(RoutineCase):
    def test_the_routine_never_writes_to_raw(self) -> None:
        source = self.write_raw_file("소스.md", "원본 그대로")
        before = source.read_bytes()
        self.cli("run", "--dry-run")
        self.assertEqual(source.read_bytes(), before)

    def test_no_repository_path_is_baked_into_the_script(self) -> None:
        text = ROUTINE_SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("/Users/", text)

    def test_an_unknown_argument_exits_two(self) -> None:
        self.cli("run", "--nope", expect=2)


if __name__ == "__main__":
    unittest.main()

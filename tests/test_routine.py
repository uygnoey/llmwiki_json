"""주기 ingest 루틴: 미처리 판정, 실행 순서, git 규율, 스케줄러 소유권."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
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
        # 시험이 진짜 ~/.claude 나 ~/.codex 를 건드리지 않게 홈을 갈아 끼운다.
        self.task_home = self.root_path / "agent-home"
        self.task_home.mkdir(exist_ok=True)
        os.environ[routine.ENV_TASK_HOME] = str(self.task_home)
        self.install_agent_guard()

    def enable_agent_tasks(self, agent: str = "claude") -> Path:
        """그 에이전트가 주기 작업 자리를 가진 것처럼 만든다."""
        base = self.task_home / routine.AGENT_TASK_DIRS[agent]
        base.mkdir(parents=True, exist_ok=True)
        return base

    def cli(self, *argv: str, expect: int = 0) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, routine.ENV_STATE_DIR: str(self.state)}
        proc = subprocess.run([sys.executable, str(ROUTINE_SCRIPT), "--root",
                               str(self.root_path), *argv],
                              capture_output=True, text=True, env=env)
        self.assertEqual(proc.returncode, expect, proc.stdout + proc.stderr)
        return proc

    def install_agent_guard(self) -> None:
        """시험이 진짜 claude·codex 를 부르는 일은 없어야 한다.

        기본값은 '쓸 수 있는 에이전트 없음' 이고, 부르고 싶은 시험만
        `offer_agent` 로 가짜를 하나 등록한다. 등록되지 않은 이름으로 나가는
        호출은 여기서 잡아 시험을 깨뜨린다.
        """
        self.agents: dict[str, tuple[int, str]] = {}
        self.agent_calls: list[list[str]] = []
        original_run = routine.run
        original_available = routine.agent_available

        def fake_run(argv, **kwargs):
            name = Path(argv[0]).name if argv else ""
            if name in routine.AGENTS:
                self.agent_calls.append(list(argv))
                if name not in self.agents:
                    raise AssertionError(f"등록하지 않은 에이전트를 불렀다: {name}")
                return self.agents[name]
            return original_run(argv, **kwargs)

        routine.run = fake_run  # type: ignore[assignment]
        routine.agent_available = lambda name: name in self.agents  # type: ignore[assignment]
        self.addCleanup(setattr, routine, "run", original_run)
        self.addCleanup(setattr, routine, "agent_available", original_available)

    def offer_agent(self, agent: str = "claude", *, code: int = 0,
                    out: str = "끝났다") -> None:
        self.agents[agent] = (code, out)

    def calls_to(self, agent: str) -> list[list[str]]:
        return [c for c in self.agent_calls if Path(c[0]).name == agent]

    def block_the_agent(self) -> None:
        """이제는 기본값이다. 읽는 사람을 위해 이름만 남겨 둔다."""

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

    def test_a_page_that_only_logs_the_source_still_counts(self) -> None:
        # 에이전트가 page 를 손으로 쓰면 raw_ref 를 빠뜨리는 일이 잦다. 그걸
        # 미처리로 세면 이미 넣은 소스를 영원히 다시 넣으려 든다.
        self.write_raw_file("손으로-넣음.md")
        page = make_page("손으로-넣음", "# 손으로 넣음\n\n본문.\n")
        page["history"] = [{"at": "2026-08-19", "action": "ingested", "actor": "claude",
                            "note": "raw/손으로-넣음.md"}]
        self.write_pages([page])
        self.assertEqual(routine.pending_sources(self.root_path), [])

    def test_an_absolute_raw_ref_is_the_same_source(self) -> None:
        self.write_raw_file("절대경로.md")
        page = make_page("절대경로", "# 절대경로\n\n본문.\n")
        page["raw_ref"] = str(self.root_path / "raw" / "절대경로.md")
        self.write_pages([page])
        self.assertEqual(routine.pending_sources(self.root_path), [])

    def test_hidden_folders_inside_raw_are_not_sources(self) -> None:
        self.write_raw_file(".obsidian/workspace.json")
        self.write_raw_file("진짜.md")
        self.assertEqual(routine.pending_sources(self.root_path), ["raw/진짜.md"])

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
        routine.write_seen(self.root_path, ["raw/소스.md"])
        report = routine.do_run(self.root_path, agent="claude", remote="private",
                                push=False, dry_run=False, timeout=5.0)
        self.assertEqual(report.get("skipped"), "nothing-to-do")
        self.assertNotIn("agent", [s["step"] for s in report["steps"]])

    def test_a_new_source_breaks_the_skip(self) -> None:
        self.block_the_agent()
        self.write_raw_file("소스.md")
        self.write_raw_file("새것.md")
        routine.write_seen(self.root_path, ["raw/소스.md"])
        report = routine.do_run(self.root_path, agent="claude", remote="private",
                                push=False, dry_run=False, timeout=5.0)
        self.assertNotEqual(report.get("skipped"), "nothing-to-do")

    def fail_the_agent(self) -> list[list[str]]:
        """claude 를 부르면 실패하게 만든다. 호출 기록을 돌려준다."""
        self.offer_agent("claude", code=1, out="실패했다")
        return self.agent_calls

    def freeze(self, moment: float) -> None:
        original = routine.now
        routine.now = lambda: moment  # type: ignore[assignment]
        self.addCleanup(setattr, routine, "now", original)

    def once(self) -> dict:
        return routine.do_run(self.root_path, agent="claude", remote="private",
                              push=False, dry_run=False, timeout=5.0)

    def test_a_failed_agent_does_not_retry_the_same_backlog_every_hour(self) -> None:
        # 타임아웃·인증 오류로 실패해도 같은 목록으로 매시간 다시 깨우지 않는다.
        self.write_raw_file("소스.md")
        calls = self.fail_the_agent()
        self.assertEqual(self.once().get("stopped"), "agent-failed")
        self.assertEqual(self.once().get("skipped"), "nothing-to-do")
        self.assertEqual(len(self.calls_to("claude")), 1, "실패한 backlog 로 곧바로 다시 불렀다")

    def test_a_failed_backlog_is_retried_once_the_wait_is_over(self) -> None:
        # 한 번의 실패가 그 소스를 영영 미처리로 묻으면 안 된다. 간격을 두고
        # 다시 시도한다 — 이걸 안 하면 못 들어간 소스만 계속 쌓인다.
        self.write_raw_file("소스.md")
        calls = self.fail_the_agent()
        start = 1_000_000.0
        self.freeze(start)
        self.assertEqual(self.once().get("stopped"), "agent-failed")
        self.freeze(start + routine.RETRY_BASE_SECONDS + 60)
        self.assertEqual(self.once().get("stopped"), "agent-failed")
        self.assertEqual(len(self.calls_to("claude")), 2, "기다린 뒤에도 다시 부르지 않았다")

    def test_no_backlog_is_ever_given_up_on(self) -> None:
        # 몇 번을 실패했든 접지 않는다. 하루가 지나면 반드시 다시 넣어 본다.
        record = {"pending": ["raw/소스.md"], "outcome": "failed",
                  "attempts": 99, "at": 0.0}
        verdict, _ = routine.seen_verdict(record, ["raw/소스.md"], 10 ** 9)
        self.assertEqual(verdict, "go")

    def test_a_source_the_agent_skipped_is_tried_again_later(self) -> None:
        # 에이전트가 "이건 못 넣겠다" 고 두고 간 것도 영영 두지 않는다.
        record = {"pending": ["raw/소스.md"], "outcome": "done", "attempts": 1, "at": 0.0}
        early, _ = routine.seen_verdict(record, ["raw/소스.md"], routine.RETRY_BASE_SECONDS - 60)
        self.assertEqual(early, "waiting")
        later, _ = routine.seen_verdict(record, ["raw/소스.md"], routine.RETRY_BASE_SECONDS + 60)
        self.assertEqual(later, "go")

    def test_the_other_agent_gets_a_turn_when_the_first_one_fails(self) -> None:
        # 넣는 것이 목적이지 특정 에이전트를 부르는 것이 목적이 아니다.
        self.write_raw_file("소스.md")
        self.offer_agent("claude", code=1, out="죽었다")
        self.offer_agent("codex", code=0, out="넣었다")
        report = self.once()
        self.assertEqual(self.calls_to("claude")[0][0], "claude", "고른 쪽을 먼저 부르지 않았다")
        self.assertEqual(report["agent_used"], "codex", "다른 에이전트로 다시 해 보지 않았다")

    def test_a_broken_build_goes_back_to_the_agent(self) -> None:
        # 깨진 채로 멈추면 그 소스는 영영 위키에 못 들어간다. 고쳐서 넣는다.
        self.write_raw_file("소스.md")
        self.offer_agent("claude")
        stages = ["validate", ""]
        routine_rebuild = routine.rebuild
        routine.rebuild = lambda _root: (stages.pop(0), "결과")  # type: ignore[assignment]
        self.addCleanup(setattr, routine, "rebuild", routine_rebuild)
        report = self.once()
        self.assertNotIn("stopped", report, report)
        self.assertEqual(len(self.calls_to("claude")), 2, "고치라고 되돌리지 않았다")
        self.assertIn("repair", [s["step"] for s in report["steps"]])

    def test_the_backoff_grows_but_has_a_ceiling(self) -> None:
        self.assertEqual(routine.retry_delay(1), routine.RETRY_BASE_SECONDS)
        self.assertEqual(routine.retry_delay(2), routine.RETRY_BASE_SECONDS * 2)
        self.assertEqual(routine.retry_delay(99), routine.RETRY_MAX_SECONDS)

    def test_two_repositories_do_not_share_one_backlog(self) -> None:
        # 상태 파일 하나를 두 clone 이 나눠 쓰면, 한쪽의 판단이 다른 쪽의
        # 새 소스를 가린다.
        other = self.root_path / "다른-저장소"
        other.mkdir()
        routine.write_seen(self.root_path, ["raw/소스.md"])
        self.assertEqual(routine.read_seen(other), {})

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

    def test_a_lock_left_by_a_dead_process_is_reclaimed(self) -> None:
        # 재부팅·강제 종료로 남은 락 하나가 여섯 시간을 잡아먹으면, 그동안
        # 들어온 소스는 전부 미처리로 쌓인다.
        path = routine.lock_file(self.root_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("999999999", encoding="utf-8")  # 살아 있을 리 없는 pid
        lock = routine.acquire_lock(self.root_path)
        self.assertIsNotNone(lock)
        self.addCleanup(lambda: lock and lock.exists() and lock.unlink())

    def test_two_routines_do_not_overlap(self) -> None:
        held = routine.acquire_lock(self.root_path)
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

    def test_leftover_from_a_broken_run_is_finished_next_time(self) -> None:
        # build 가 깨지거나 에이전트가 죽어 커밋되지 못한 변경이 남으면, 그
        # 다음 차례부터 전부 더러운 트리에서 멈춘다. 우리가 남긴 것은 우리가
        # 이어서 끝낸다.
        self.init_repo()
        self.stub_rebuild("")
        routine.mark_inflight(self.root_path, "main")
        (self.root_path / "wiki" / "concepts" / "이월.json").write_text("[]", encoding="utf-8")
        ok, detail = routine.finish_leftover(self.root_path, "private", "main")
        self.assertTrue(ok, detail)
        self.assertEqual(routine.changed_paths(self.root_path), [])
        self.assertEqual(routine.read_inflight(self.root_path), {})

    def test_leftover_that_still_does_not_build_is_not_committed(self) -> None:
        # 깨진 정본을 커밋해 원격까지 밀지 않는다. 멈추되 이유를 남긴다.
        self.init_repo()
        self.stub_rebuild("validate")
        routine.mark_inflight(self.root_path, "main")
        (self.root_path / "wiki" / "concepts" / "이월.json").write_text("[]", encoding="utf-8")
        ok, detail = routine.finish_leftover(self.root_path, "private", "main")
        self.assertFalse(ok)
        self.assertIn("validate", detail)
        self.assertTrue(routine.read_inflight(self.root_path), "표시를 지우면 다음 차례가 막힌다")

    def stub_rebuild(self, stage: str) -> None:
        original = routine.rebuild
        routine.rebuild = lambda _root: (stage, f"{stage or 'build · validate'} 결과")  # type: ignore[assignment]
        self.addCleanup(setattr, routine, "rebuild", original)

    def test_a_dirty_tree_that_is_not_ours_is_left_alone(self) -> None:
        # 사람이 편집 중인 것은 우리가 커밋하지 않는다.
        self.init_repo()
        (self.root_path / "wiki" / "concepts" / "사람.json").write_text("[]", encoding="utf-8")
        ok, detail = routine.finish_leftover(self.root_path, "private", "main")
        self.assertTrue(ok)
        self.assertEqual(detail, "")
        self.assertTrue(routine.changed_paths(self.root_path))

    def test_edits_outside_the_wiki_do_not_stop_the_routine(self) -> None:
        # viewer/ 를 고치는 중이라고 해서 위키 ingest 까지 세우지 않는다.
        self.init_repo()
        (self.root_path / "viewer").mkdir(exist_ok=True)
        (self.root_path / "viewer" / "App.tsx").write_text("건드리는 중", encoding="utf-8")
        ok, detail = routine.pull_first(self.root_path, "private", "main")
        self.assertTrue(ok, detail)

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
class SchedulerPriorityTest(RoutineCase):
    """정석은 에이전트 자신의 루틴, OS 스케줄러는 폴백."""

    def install(self, **kwargs) -> dict:
        opts = {"interval": 3600, "python": None, "remote": "private", "push": True,
                "dry_run": False, **kwargs}
        agent = opts.pop("agent", "claude")
        return routine.install_schedule(self.root_path, agent, **opts)

    def test_the_agents_own_routine_comes_first(self) -> None:
        self.enable_agent_tasks("claude")
        self.assertEqual(routine.scheduler_kind("claude"), "claude-task")

    def test_an_agent_without_a_routine_slot_has_nowhere_to_hang_it(self) -> None:
        self.assertEqual(routine.scheduler_kind("codex"), "")

    def test_installing_writes_the_task_the_agent_itself_reads(self) -> None:
        self.enable_agent_tasks("claude")
        plan = self.install()
        self.assertTrue(plan["ok"], plan)
        self.assertEqual(plan["scheduler"], "claude-task")
        body = Path(plan["file"]).read_text(encoding="utf-8")
        self.assertIn(routine.TASK_MARKER, body)
        self.assertIn("llmwiki_routine.py", body)
        self.assertIn(str(self.root_path), body)
        self.assertEqual(routine.read_state()["scheduler"], "claude-task")

    def test_a_task_written_by_hand_is_left_alone(self) -> None:
        # 사람이 직접 쓴 같은 이름의 작업을 덮어쓰지 않는다.
        base = self.enable_agent_tasks("claude")
        mine = base / routine.TASK_NAME / "SKILL.md"
        mine.parent.mkdir(parents=True)
        mine.write_text("---\nname: llmwiki-ingest\n---\n내가 쓴 것\n", encoding="utf-8")
        plan = self.install()
        self.assertFalse(plan["ok"])
        self.assertIn("우리가 만든 것이 아니다", plan["detail"])
        self.assertIn("내가 쓴 것", mine.read_text(encoding="utf-8"))
        self.assertFalse(routine.state_file().exists())

    def test_uninstall_removes_only_our_own_task(self) -> None:
        self.enable_agent_tasks("claude")
        plan = self.install()
        path = Path(plan["file"])
        self.assertTrue(path.exists())
        removed = routine.uninstall_schedule(dry_run=False)
        self.assertTrue(removed["ok"], removed)
        self.assertFalse(path.exists())
        self.assertFalse(routine.state_file().exists())

    def test_uninstall_follows_what_install_recorded(self) -> None:
        # 지금 OS 를 다시 물으면 자체 루틴으로 걸어 둔 것을 놓친다.
        self.enable_agent_tasks("claude")
        self.install()
        self.assertEqual(routine.uninstall_schedule(dry_run=True)["scheduler"], "claude-task")

    def test_an_agent_without_a_slot_fails_loudly(self) -> None:
        plan = self.install(agent="codex")
        self.assertFalse(plan["ok"])
        self.assertIn("자리가 없다", plan["detail"])
        self.assertFalse(routine.state_file().exists())

    def test_the_cli_install_runs_end_to_end(self) -> None:
        # 시험이 함수만 부르면 CLI 배선이 끊긴 것을 못 잡는다.
        self.enable_agent_tasks("claude")
        payload = json.loads(self.cli("install", "--agent", "claude", "--dry-run").stdout)
        self.assertEqual(payload["scheduler"], "claude-task")
        self.assertTrue(payload["dry_run"])

    def test_the_plan_names_where_it_will_hang(self) -> None:
        self.enable_agent_tasks("claude")
        plan = self.install(dry_run=True)
        self.assertEqual(plan["scheduler"], "claude-task")

    def test_a_routine_that_stopped_firing_is_reported(self) -> None:
        # 걸어 두기만 하고 실제로 돌지 않는 것이 제일 위험하다 — 미처리만 쌓인다.
        ok, detail = routine.firing_check(3600)
        self.assertFalse(ok)
        self.assertIn("실행 기록이 없다", detail)
        routine.log("돌았다")
        ok, detail = routine.firing_check(3600)
        self.assertTrue(ok, detail)

    def test_a_stale_log_points_at_the_agent_routine(self) -> None:
        routine.log("옛날에 돌았다")
        original = routine.now
        routine.now = lambda: time.time() + 5 * 3600  # type: ignore[assignment]
        self.addCleanup(setattr, routine, "now", original)
        ok, detail = routine.firing_check(3600)
        self.assertFalse(ok)
        self.assertIn("에이전트 주기 작업", detail)


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

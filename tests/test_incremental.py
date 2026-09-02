"""증분 색인 갱신(FINAL_PROPOSAL §6-4) — `build --changed` 가 바뀐 page 만 반영해도 cold build 와 같은가.

bench/review_inc/identity.py 의 39가지 변경 시나리오를 fixture 크기로 줄였다: 본문 수정·추가·삭제·
supersedes 추가·체인 head 교체·체인 중간 삭제·df 이동·slug 개명·block 제거·파일 이동·빈 block·제목 충돌·
dangling link 해석. 각 시나리오에서 (a) fixture 질문의 page 순서·점수·block_ids 가 cold build 와 같고
(b) 발행본 `index/search.sqlite` 와 다른 파생물의 바이트가 cold build 와 같은지 본다. 그 밖에 힌트 밖의
변경이 전량으로 떨어지는지, compact 문턱, 갱신 중 훅 조회(WAL/immutable), 감시자 인자 형식을 고정한다.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import threading
import time
from pathlib import Path
from typing import Any
from unittest import mock

from tests.support import REPO, WorkspaceCase, llmwiki, llmwiki_context as ctx, make_page

IDX = ctx.IDX

TABLE = "| 항목 | 값 |\n| --- | --- |\n" + "\n".join(f"| 행{i} 잡음 {'x' * 12} | {i * 3} |" for i in range(1, 25)) \
    + "\n| 스테이징 QA 기간 | 2026-05-08 ~ 2026-05-15 |\n"

QUERIES = [
    "허브 중심 문서", "스테이징 QA 기간", "고유어휘3 토큰", "주제 7 세부 사항", "상충 수치 다르다",
    "정책 현행 주장", "표 행 값 잡음", "topic-5 참고", "릴리스 일정 구판", "유령 문서 참조",
    "주제 12 목록 항목", "login_rate_limited 필드", "허브 참고 주제", "체인 최신판",
]


def corpus() -> list[dict[str, Any]]:
    pages: list[dict[str, Any]] = []
    for i in range(20):
        body = (f"# 주제 {i}\n\n{i}번 주제는 [[hub]] 와 [[topic-{(i + 1) % 20}]] 를 참고한다. 고유어휘{i} 토큰.\n\n"
                f"## 세부 사항\n\n- 목록 항목 {i} 하나\n- 목록 항목 {i} 둘 참고\n")
        pages.append(make_page(f"topic-{i}", body, projects=["alpha" if i % 2 else "beta"], tags=[f"t{i % 3}"],
                               summary=f"주제 {i} 요약"))
    pages.append(make_page("hub", "# 허브\n\n중심 문서다. 모든 주제가 여기를 참고한다.\n", projects=["beta"],
                           tags=["hub"], summary="허브"))
    pages.append(make_page("release-v1", "# 릴리스 일정 구판\n\n스테이징 QA 기간은 2026-04-01 ~ 2026-04-07 이었다.\n",
                           projects=["beta"], tags=["릴리스"], summary="구판"))
    pages.append(make_page("release-v2", "# 릴리스 일정 2판\n\n스테이징 QA 기간은 2026-04-20 ~ 2026-04-27 로 바뀌었다.\n",
                           projects=["beta"], tags=["릴리스"], summary="2판"))
    pages.append(make_page("release-v3", "# 릴리스 일정 3판\n\n## 공장 관리자 릴리스\n\n" + TABLE,
                           projects=["beta"], tags=["릴리스"], summary="3판 체인 최신판"))
    pages[-2]["links"].append({"target": "release-v1", "label": "release-v1", "kind": "supersedes"})
    pages[-1]["links"].append({"target": "release-v2", "label": "release-v2", "kind": "supersedes"})
    pages.append(make_page("conflict", "# 상충 문서\n\n> ⚠️ 상충: 두 문서의 수치가 다르다.\n\n[[hub]] 참고.\n",
                           projects=["alpha"], tags=["c"], summary="상충"))
    policy = make_page("policy", "# 정책\n\n✅ 현행: 정책의 현행 주장은 이것이다.\n\n[[유령]] 문서를 참조한다.\n",
                       projects=["alpha"], tags=["p"], summary="정책")
    # 큐레이션 간선을 든 block(anchor) — 대상이 아직 없어 dangling 이다
    policy["links"].append({"target": "유령", "label": "유령", "kind": "related",
                            "block_id": policy["block_order"][1]})
    pages.append(policy)
    pages.append(make_page("golden", "# Golden Set\n\nField name login_rate_limited marks throttled rows.\n",
                           projects=["alpha"], tags=["g"], summary="golden"))
    return pages


def big_page(slug: str, n: int) -> dict[str, Any]:
    """색인 page 여러 장을 차지하는 page — 지우면 free page 가 생겨 compact 문턱을 넘긴다."""
    body = f"# 큰 {n}\n\n" + "\n\n".join(
        f"큰 본문 {n} 단락 {j} " + " ".join(f"어휘{n}_{j}_{k}" for k in range(40)) for j in range(20)) + "\n"
    return make_page(slug, body, projects=["beta"], tags=["big"], summary=f"큰 {n}")


def signatures(root: Path) -> list[list[tuple]]:
    idx = IDX.open_ro(root / "index" / "search.sqlite")
    try:
        return [[(h.page_id, h.score, tuple(h.block_ids), h.head, h.sup_state) for h in idx.search(q).hits]
                for q in QUERIES]
    finally:
        idx.close()


def artifact_bytes(root: Path) -> dict[str, bytes]:
    out = {}
    for base in (root / "index", root / "viewer" / "public" / "data"):
        for p in sorted(base.rglob("*")):
            if p.is_file() and not p.name.startswith("search.work."):
                out[p.relative_to(root).as_posix()] = p.read_bytes()
    return out


class IncrementalCase(WorkspaceCase):
    def setUp(self) -> None:
        super().setUp()
        self.root_path = Path(self.root).resolve()
        self.pages = {p["slug"]: p for p in corpus()}
        for p in self.pages.values():
            self.write_pages([p], name=f"{p['slug']}.json")
        self.stats = llmwiki.build(self.ws)
        self.assertEqual(self.stats["mode"], "full")

    def path_of(self, slug: str) -> str:
        return f"wiki/concepts/{slug}.json"

    def save(self, page: dict[str, Any], *, name: str | None = None) -> str:
        self.pages[page["slug"]] = page
        self.write_pages([page], name=name or f"{page['slug']}.json")
        return f"wiki/concepts/{name or page['slug'] + '.json'}"

    def remove(self, slug: str, name: str | None = None) -> str:
        rel = f"wiki/concepts/{name or slug + '.json'}"
        (self.root_path / rel).unlink()
        self.pages.pop(slug, None)
        return rel

    def check(self, hint: list[str], *, expect_delta: dict[str, int] | None = None) -> dict[str, Any]:
        """힌트로 증분 build → 결과·바이트를 잡고, cold build 와 대조한다."""
        stats = llmwiki.build(self.ws, changed=hint)
        self.assertEqual(stats["mode"], "incremental", stats)
        if expect_delta:
            self.assertEqual(stats["delta"], expect_delta)
        inc_sig, inc_bytes = signatures(self.root_path), artifact_bytes(self.root_path)
        self.assertTrue(any(inc_sig), "fixture queries must hit something")
        cold = llmwiki.build(self.ws, full=True)
        self.assertEqual(cold["mode"], "full")
        cold_sig, cold_bytes = signatures(self.root_path), artifact_bytes(self.root_path)
        for q, a, b in zip(QUERIES, inc_sig, cold_sig):
            self.assertEqual(a, b, q)
        self.assertEqual(sorted(inc_bytes), sorted(cold_bytes))
        for name in cold_bytes:
            self.assertEqual(inc_bytes[name], cold_bytes[name], name)
        return stats


# --------------------------------------------------------------------------- (a)+(b) 시나리오
class ScenarioTest(IncrementalCase):
    def test_body_edit(self) -> None:
        p = self.pages["topic-3"]
        bid = p["block_order"][1]
        p["blocks"][bid]["source_text"] += " 스테이징 QA 기간 언급 추가."
        p["blocks"][bid]["data"]["text"] = p["blocks"][bid]["source_text"]
        self.check([self.save(p)], expect_delta={"added": 0, "modified": 1, "deleted": 0})

    def test_body_edit_of_a_chain_member_keeps_its_head(self) -> None:
        # link 가 그대로인 본문 수정은 그래프를 다시 계산하지 않는다 — 체인 자리(head 등) 가 남아야 한다
        for slug in ("release-v1", "release-v2", "release-v3"):
            p = self.pages[slug]
            bid = p["block_order"][-1]
            p["blocks"][bid]["source_text"] += " 본문만 고침."
            p["blocks"][bid]["data"]["text"] = p["blocks"][bid]["source_text"]
            self.check([self.save(p)], expect_delta={"added": 0, "modified": 1, "deleted": 0})
        rows = self.rows(["page:release-v1", "page:release-v2", "page:release-v3"])
        self.assertEqual(rows["page:release-v1"]["head"], rows["page:release-v3"]["rid"])
        self.assertEqual(rows["page:release-v2"]["sup_state"], "stale")

    def test_add_page(self) -> None:
        new = make_page("topic-new", "# 새 주제\n\n허브 참고 [[hub]] 와 고유어휘3 토큰.\n", projects=["beta"], tags=["n"])
        self.check([self.save(new)], expect_delta={"added": 1, "modified": 0, "deleted": 0})

    def test_delete_page(self) -> None:
        self.check([self.remove("topic-7")], expect_delta={"added": 0, "modified": 0, "deleted": 1})

    def test_add_supersedes_edge(self) -> None:
        p = self.pages["topic-4"]
        p["links"].append({"target": "topic-5", "label": "topic-5", "kind": "supersedes"})
        stats = self.check([self.save(p)])
        rows = self.rows(["page:topic-5", "page:topic-4"])
        self.assertEqual(rows["page:topic-5"]["head"], rows["page:topic-4"]["rid"])
        self.assertEqual(stats["delta"]["modified"], 1)

    def test_chain_head_replaced(self) -> None:
        v4 = make_page("release-v4", "# 릴리스 일정 4판\n\n스테이징 QA 기간은 2026-06-01 ~ 2026-06-07 이다. 체인 최신판.\n",
                       projects=["beta"], tags=["릴리스"], summary="4판")
        v4["links"].append({"target": "release-v3", "label": "release-v3", "kind": "supersedes"})
        self.check([self.save(v4)])
        rows = self.rows(["page:release-v1", "page:release-v4"])
        self.assertEqual(rows["page:release-v1"]["head"], rows["page:release-v4"]["rid"])

    def test_chain_head_deleted(self) -> None:
        self.check([self.remove("release-v3")])
        rows = self.rows(["page:release-v1", "page:release-v2"])
        self.assertEqual(rows["page:release-v1"]["head"], rows["page:release-v2"]["rid"])
        self.assertEqual(rows["page:release-v2"]["head"], rows["page:release-v2"]["rid"])
        self.assertEqual(rows["page:release-v2"]["sup_state"], "")

    def test_chain_middle_deleted(self) -> None:
        self.check([self.remove("release-v2")])
        rows = self.rows(["page:release-v1", "page:release-v3"])
        self.assertEqual(rows["page:release-v1"]["head"], rows["page:release-v1"]["rid"])
        self.assertEqual(rows["page:release-v3"]["sup_state"], "")

    def test_df_shift(self) -> None:
        p = self.pages["topic-9"]
        bid = p["block_order"][1]
        blob = " ".join(QUERIES * 3)
        p["blocks"][bid]["source_text"] += " " + blob
        p["blocks"][bid]["data"]["text"] = p["blocks"][bid]["source_text"]
        self.check([self.save(p)])

    def test_slug_rename_of_the_hub(self) -> None:
        hub = self.pages.pop("hub")
        hub["id"], hub["slug"], hub["title"] = "page:hub-renamed", "hub-renamed", "허브 개명"
        rel_old = self.remove("hub")
        rel_new = self.save(hub)
        stats = self.check([rel_old, rel_new], expect_delta={"added": 1, "modified": 0, "deleted": 1})
        self.assertEqual(stats["index"]["reindexed"], 0)   # [[hub]] 는 wiki 간선이라 anchor 가 아니다
        _errors, _ = llmwiki.lint(self.ws)

    def test_block_removed_and_reordered(self) -> None:
        p = self.pages["topic-2"]
        gone = p["block_order"].pop(0)
        del p["blocks"][gone]
        p["block_order"].reverse()
        p["links"] = [l for l in p["links"] if l.get("block_id") != gone]
        self.check([self.save(p)])

    def test_file_moved_without_content_change(self) -> None:
        p = self.pages["topic-11"]
        rel_old = self.remove("topic-11")
        rel_new = self.save(p, name="moved-topic-11.json")
        self.check([rel_old, rel_new], expect_delta={"added": 0, "modified": 1, "deleted": 0})

    def test_empty_blocks(self) -> None:
        p = self.pages["topic-6"]
        for bid in p["block_order"]:
            p["blocks"][bid]["source_text"] = ""
            p["blocks"][bid]["data"]["text"] = ""
        self.check([self.save(p)])
        idx = IDX.open_ro(self.root_path / "index" / "search.sqlite")
        try:
            rows = idx.db.execute("SELECT indexed FROM blk WHERE prid=? AND pos>=0",
                                  (IDX.page_rid("page:topic-6"),)).fetchall()
        finally:
            idx.close()
        self.assertTrue(rows and not any(r[0] for r in rows))

    def test_title_collision_resolves_a_dangling_link(self) -> None:
        # policy 의 [[유령]] 은 dangling 이었다. 제목이 '유령' 인 page 가 생기면 제목으로 해석돼
        # policy 의 anchor block 이 바뀐다 → policy 를 다시 색인해야 cold build 와 같다.
        p = self.pages["topic-0"]
        p["title"] = "유령"
        stats = self.check([self.save(p)])
        self.assertEqual(stats["index"]["reindexed"], 1)

    def test_new_page_resolves_a_dangling_related_link(self) -> None:
        ghost = make_page("유령", "# 유령 문서\n\n유령 문서 참조 대상.\n", projects=["alpha"], tags=["g"])
        stats = self.check([self.save(ghost)])
        self.assertEqual(stats["index"]["reindexed"], 1)
        idx = IDX.open_ro(self.root_path / "index" / "search.sqlite")
        try:
            edges = idx.edges([self.pages["policy"]["block_order"][1]])
        finally:
            idx.close()
        self.assertIn(("page:유령", "related"), {(e[2], e[1]) for e in edges})

    def test_several_changes_in_one_batch(self) -> None:
        a = self.pages["topic-15"]
        a["blocks"][a["block_order"][1]]["source_text"] += " 허브 중심."
        a["blocks"][a["block_order"][1]]["data"]["text"] = a["blocks"][a["block_order"][1]]["source_text"]
        rels = [self.save(a), self.remove("topic-16"),
                self.save(make_page("topic-added", "# 추가\n\n[[topic-15]] 와 [[hub]].\n", projects=["beta"], tags=["x"]))]
        self.check(rels, expect_delta={"added": 1, "modified": 1, "deleted": 1})

    def test_heading_paths_option_is_honoured(self) -> None:
        llmwiki.build(self.ws, heading_paths=True)
        p = self.pages["topic-8"]
        p["blocks"][p["block_order"][1]]["source_text"] += " 세부 항목 추가."
        p["blocks"][p["block_order"][1]]["data"]["text"] = p["blocks"][p["block_order"][1]]["source_text"]
        rel = self.save(p)
        stats = llmwiki.build(self.ws, changed=[rel], heading_paths=True)
        self.assertEqual(stats["mode"], "incremental")
        inc = artifact_bytes(self.root_path)["index/search.sqlite"]
        llmwiki.build(self.ws, full=True, heading_paths=True)
        self.assertEqual(inc, artifact_bytes(self.root_path)["index/search.sqlite"])
        # 옵션이 달라지면 증분이 아니다
        stats = llmwiki.build(self.ws, changed=[rel])
        self.assertEqual((stats["mode"], stats["reason"]), ("full", "index-options-changed"))

    def test_repeated_rounds_do_not_drift(self) -> None:
        for r in range(6):
            p = self.pages[f"topic-{r}"]
            bid = p["block_order"][1]
            p["blocks"][bid]["source_text"] += f" 라운드 {r} 수정."
            p["blocks"][bid]["data"]["text"] = p["blocks"][bid]["source_text"]
            stats = llmwiki.build(self.ws, changed=[self.save(p)])
            self.assertEqual(stats["mode"], "incremental", stats)
        inc_sig, inc_bytes = signatures(self.root_path), artifact_bytes(self.root_path)
        llmwiki.build(self.ws, full=True)
        self.assertEqual(inc_sig, signatures(self.root_path))
        self.assertEqual(inc_bytes, artifact_bytes(self.root_path))

    def rows(self, ids: list[str]) -> dict[str, dict[str, Any]]:
        idx = IDX.open_ro(self.root_path / "index" / "search.sqlite")
        try:
            return idx.pages(ids)
        finally:
            idx.close()


# --------------------------------------------------------------------------- (c) 힌트는 힌트다
class HintTest(IncrementalCase):
    def test_a_change_outside_the_hint_falls_back_to_full(self) -> None:
        p = self.pages["topic-1"]
        p["blocks"][p["block_order"][1]]["source_text"] += " 몰래."
        rel_hidden = self.save(p)
        q = self.pages["topic-2"]
        q["summary"] = "다른 요약"
        rel_hint = self.save(q)
        stats = llmwiki.build(self.ws, changed=[rel_hint])
        self.assertEqual(stats["mode"], "full")
        self.assertTrue(stats["reason"].startswith("unhinted-change:"), stats["reason"])
        self.assertIn(rel_hidden, stats["reason"])
        # 이제 둘 다 반영됐으니 같은 힌트로 다시 돌리면 델타가 없다
        stats = llmwiki.build(self.ws, changed=[rel_hint])
        self.assertEqual((stats["mode"], stats["reason"]), ("incremental", "no-change"))

    def test_an_unhinted_new_or_removed_file_falls_back_to_full(self) -> None:
        self.save(make_page("stranger", "# 낯선 문서\n\n본문.\n", projects=["beta"], tags=["s"]))
        stats = llmwiki.build(self.ws, changed=[self.path_of("topic-1")])
        self.assertEqual(stats["mode"], "full")
        self.assertIn("wiki/concepts/stranger.json", stats["reason"])
        self.remove("topic-3")
        stats = llmwiki.build(self.ws, changed=[self.path_of("topic-1")])
        self.assertEqual(stats["mode"], "full")
        self.assertIn("wiki/concepts/topic-3.json", stats["reason"])

    def test_a_backdated_edit_is_still_caught_by_the_sha_check(self) -> None:
        p = self.pages["topic-1"]
        p["summary"] = "mtime 조작"
        rel = self.save(p)
        started = json.loads((self.root_path / "index" / "search.work.json").read_text())["started_ns"]
        os.utime(self.root_path / rel, ns=(started - 10 ** 9, started - 10 ** 9))   # 1초 과거
        stats = llmwiki.build(self.ws, changed=[self.path_of("topic-2")])
        self.assertEqual(stats["mode"], "full")

    def test_a_backdated_edit_far_in_the_past_is_still_caught(self) -> None:
        # codex REVIEW #4: 지난 build 시각보다 10초 이상 과거로 mtime 을 돌린 편집(timestamp 보존 복사·utime) 도
        # 지난 build 가 기록한 (mtime_ns, size) 와 다르므로 sha 확인에 들어가 전량으로 떨어진다
        state = json.loads((self.root_path / "index" / "search.work.json").read_text())
        rel = self.path_of("topic-1")
        recorded = state["files"][rel]
        p = self.pages["topic-1"]
        p["blocks"][p["block_order"][1]]["source_text"] += " 몰래 과거로."
        self.save(p)
        old_ns = state["started_ns"] - 10 ** 10
        os.utime(self.root_path / rel, ns=(old_ns, old_ns))
        self.assertNotEqual(list(recorded), llmwiki.wiki_files(self.ws)[rel])
        q = self.pages["topic-2"]
        q["summary"] = "힌트"
        stats = llmwiki.build(self.ws, changed=[self.save(q)])
        self.assertEqual(stats["mode"], "full")
        self.assertEqual(stats["reason"], f"unhinted-change:{rel}")
        entry = self.read_json("index/map.json")["pages"]["page:topic-1"]
        self.assertEqual(entry["sha256"], llmwiki.sha(llmwiki.canonical(p)))
        # 기록도 새 (mtime, size) 로 갱신됐다
        state = json.loads((self.root_path / "index" / "search.work.json").read_text())
        self.assertEqual(state["files"][rel], [old_ns, (self.root_path / rel).stat().st_size])

    def test_a_touch_without_a_content_change_keeps_the_incremental_path(self) -> None:
        rel = self.path_of("topic-1")
        now = time.time_ns() + 5 * 10 ** 9
        os.utime(self.root_path / rel, ns=(now, now))          # 내용은 같고 mtime 만 다르다 → sha 같음
        q = self.pages["topic-2"]
        q["summary"] = "힌트만 바뀜"
        hint = self.save(q)
        stats = llmwiki.build(self.ws, changed=[hint])
        self.assertEqual((stats["mode"], stats["reason"]), ("incremental", ""))
        self.assertEqual(stats["delta"], {"added": 0, "modified": 1, "deleted": 0})
        state = json.loads((self.root_path / "index" / "search.work.json").read_text())
        self.assertEqual(state["files"][rel][0], now)
        # 기록이 갱신됐으니 다음 build 는 그 파일을 열지 않고도 증분이다
        stats = llmwiki.build(self.ws, changed=[hint])
        self.assertEqual((stats["mode"], stats["reason"]), ("incremental", "no-change"))

    def test_a_no_change_build_still_refreshes_the_file_records(self) -> None:
        # 델타가 없는 증분 build 도 (mtime, size) 기록을 다시 쓴다 — 아니면 touch 된 파일을 build 마다 sha 로 다시 읽는다
        rel = self.path_of("topic-3")
        now = time.time_ns() + 7 * 10 ** 9
        os.utime(self.root_path / rel, ns=(now, now))
        stats = llmwiki.build(self.ws, changed=[self.path_of("topic-2")])
        self.assertEqual((stats["mode"], stats["reason"]), ("incremental", "no-change"))
        state = json.loads((self.root_path / "index" / "search.work.json").read_text())
        self.assertEqual(state["files"][rel][0], now)
        with mock.patch.object(llmwiki, "file_shas_match", side_effect=AssertionError("must not reread")):
            stats = llmwiki.build(self.ws, changed=[self.path_of("topic-2")])
        self.assertEqual((stats["mode"], stats["reason"]), ("incremental", "no-change"))

    def test_a_build_without_file_records_checks_every_known_file_by_sha(self) -> None:
        # 옛 형식 상태 파일(files 없음): 알려진 파일을 전부 sha 로 확인한다 — 같으면 증분, 다르면 전량
        state_path = self.root_path / "index" / "search.work.json"
        state = json.loads(state_path.read_text())
        state.pop("files")
        state_path.write_text(json.dumps(state), encoding="utf-8")
        q = self.pages["topic-2"]
        q["summary"] = "기록 없음"
        stats = llmwiki.build(self.ws, changed=[self.save(q)])
        self.assertEqual((stats["mode"], stats["reason"]), ("incremental", ""))
        state = json.loads(state_path.read_text())
        state.pop("files")
        state_path.write_text(json.dumps(state), encoding="utf-8")
        p = self.pages["topic-1"]
        p["summary"] = "기록 없이 몰래"
        rel = self.save(p)
        os.utime(self.root_path / rel, ns=(10 ** 9, 10 ** 9))
        stats = llmwiki.build(self.ws, changed=[self.path_of("topic-2")])
        self.assertEqual((stats["mode"], stats["reason"]), ("full", f"unhinted-change:{rel}"))

    def test_the_hint_only_rereads_the_hinted_files(self) -> None:
        # 힌트 밖 파일이 깨져 있어도(mtime 은 옛것) 힌트 경로는 그 파일을 열지 않는다
        victim = self.root_path / self.path_of("topic-5")
        old_ns = json.loads((self.root_path / "index" / "search.work.json").read_text())["started_ns"] - 10 ** 10
        victim.write_text("{ not json", encoding="utf-8")
        os.utime(victim, ns=(old_ns, old_ns))
        p = self.pages["topic-1"]
        p["summary"] = "힌트만"
        rel = self.save(p)
        pages, docs = llmwiki.page_hash_map(self.ws, [rel], old_map=llmwiki.read_map(self.ws))
        self.assertEqual(set(docs), {"page:topic-1"})
        self.assertIn("page:topic-5", pages)
        with self.assertRaises(llmwiki.WikiError):
            llmwiki.page_hash_map(self.ws)

    def test_large_deltas_use_a_full_build(self) -> None:
        rels = []
        for i in range(10):
            p = self.pages[f"topic-{i}"]
            p["summary"] = f"대량 {i}"
            rels.append(self.save(p))
        stats = llmwiki.build(self.ws, changed=rels)
        self.assertEqual((stats["mode"], stats["reason"]), ("full", "large-delta"))

    def test_map_delta_is_pure(self) -> None:
        old = {"a": {"sha256": "1", "source": "x"}, "b": {"sha256": "2", "source": "y"}, "c": {"sha256": "3", "source": "z"}}
        new = {"a": {"sha256": "1", "source": "x"}, "b": {"sha256": "9", "source": "y"},
               "c": {"sha256": "3", "source": "moved"}, "d": {"sha256": "4", "source": "w"}}
        delta = llmwiki.map_delta(old, new)
        self.assertEqual((delta.added, delta.modified, delta.deleted), (["d"], ["b", "c"], []))
        self.assertEqual(llmwiki.map_delta(new, old).deleted, ["d"])
        self.assertFalse(llmwiki.map_delta(old, old))

    def test_stale_index_uses_the_mtime_scan(self) -> None:
        self.assertEqual(llmwiki.stale_index(self.ws), [])
        p = self.pages["topic-1"]
        p["summary"] = "낡음"
        self.save(p)
        self.assertEqual(llmwiki.stale_index(self.ws), ["index/map.json stale — run build"])
        llmwiki.build(self.ws, changed=[self.path_of("topic-1")])
        self.assertEqual(llmwiki.stale_index(self.ws), [])
        self.remove("topic-2")
        self.assertEqual(llmwiki.stale_index(self.ws), ["index/map.json stale — run build"])


# --------------------------------------------------------------------------- (d) compact
class CompactTest(IncrementalCase):
    def test_compact_runs_only_past_the_freelist_threshold(self) -> None:
        work = self.root_path / "index" / "search.work.sqlite"
        db = IDX.open_work(work)
        try:
            self.assertFalse(IDX.compact(db))
            # blk.text 를 부풀렸다 지워 free page 를 만든다
            db.execute("BEGIN IMMEDIATE")
            db.execute("CREATE TABLE junk(x)")
            db.executemany("INSERT INTO junk VALUES(?)", [("x" * 4000,) for _ in range(400)])
            db.execute("DROP TABLE junk")
            db.commit()
            free = db.execute("PRAGMA freelist_count").fetchone()[0]
            total = db.execute("PRAGMA page_count").fetchone()[0]
            self.assertGreater(free / total, IDX.COMPACT_FREELIST)
            self.assertTrue(IDX.compact(db))
            self.assertEqual(db.execute("PRAGMA freelist_count").fetchone()[0], 0)
            self.assertFalse(IDX.compact(db))
        finally:
            db.close()

    def test_body_edits_make_no_free_pages_and_do_not_compact(self) -> None:
        # compact 문턱은 free page 비율 10% 다(codex REVIEW #5). posting 은 제자리 교체라 본문 수정은 delta/tomb 층을
        # 만들지 않고 free page 도 거의 만들지 않는다 — 20% 의 page 본문을 고쳐도 compact 대상이 아니다.
        rels = []
        for i in range(5):
            p = self.pages[f"topic-{i}"]
            p["blocks"][p["block_order"][1]]["source_text"] += f" 본문 수정 {i} 고유어휘 다시."
            rels.append(self.save(p))
        stats = llmwiki.build(self.ws, changed=rels)
        self.assertEqual(stats["mode"], "incremental")
        self.assertEqual(stats["delta"]["modified"], 5)
        self.assertIs(stats["index"]["compacted"], False)
        work = self.root_path / "index" / "search.work.sqlite"
        db = sqlite3.connect(work)
        try:
            self.assertEqual(db.execute("PRAGMA freelist_count").fetchone()[0], 0)
        finally:
            db.close()
        # 삭제만 free page 를 만들고, 문턱(10%) 을 넘으면 그 build 가 compact 한다
        for i in range(3):
            stats = llmwiki.build(self.ws, changed=[self.save(big_page(f"big-{i}", i))])
            self.assertEqual(stats["mode"], "incremental", stats)
        stats = llmwiki.build(self.ws, changed=[self.remove("big-0")])
        self.assertEqual(stats["mode"], "incremental")
        self.assertIs(stats["index"]["compacted"], True)
        db = sqlite3.connect(work)
        try:
            self.assertEqual(db.execute("PRAGMA freelist_count").fetchone()[0], 0)
        finally:
            db.close()

    def test_update_reports_compaction_and_deleting_many_pages_compacts(self) -> None:
        # 5 page 삭제 (25% 미만) → 작업 DB 에 free page 가 남고, 문턱을 넘으면 build 가 접는다
        rels = [self.remove(f"topic-{i}") for i in range(5)]
        stats = llmwiki.build(self.ws, changed=rels)
        self.assertEqual(stats["mode"], "incremental")
        self.assertIn("compacted", stats["index"])
        work = self.root_path / "index" / "search.work.sqlite"
        db = sqlite3.connect(work)
        try:
            free = db.execute("PRAGMA freelist_count").fetchone()[0]
            total = db.execute("PRAGMA page_count").fetchone()[0]
        finally:
            db.close()
        self.assertLessEqual(free / total, IDX.COMPACT_FREELIST)


# --------------------------------------------------------------------------- (e) 갱신 중 조회
class ConcurrencyTest(IncrementalCase):
    def test_hook_queries_keep_working_while_the_index_is_updated(self) -> None:
        errors: list[BaseException] = []
        stop = threading.Event()
        counts = {"queries": 0, "modes": set()}

        def reader() -> None:
            while not stop.is_set():
                try:
                    idx, why = ctx.open_index(self.root_path)
                    if idx is None:
                        counts["modes"].add(why)
                        continue
                    try:
                        idx.search("스테이징 QA 기간")
                        counts["queries"] += 1
                    finally:
                        idx.close()
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)
                    return

        t = threading.Thread(target=reader)
        t.start()
        try:
            for r in range(8):
                p = self.pages[f"topic-{r}"]
                bid = p["block_order"][1]
                p["blocks"][bid]["source_text"] += f" 동시 {r}."
                p["blocks"][bid]["data"]["text"] = p["blocks"][bid]["source_text"]
                stats = llmwiki.build(self.ws, changed=[self.save(p)])
                self.assertEqual(stats["mode"], "incremental")
        finally:
            stop.set()
            t.join(10)
        self.assertEqual(errors, [])
        self.assertGreater(counts["queries"], 0)
        # 색인이 낡은 순간에는 폴백 사유만 남고 오류는 없다
        self.assertTrue(counts["modes"] <= {"stale-mtime", "revision-mismatch"}, counts["modes"])

    def test_the_hook_sees_the_incremental_revision(self) -> None:
        p = self.pages["topic-1"]
        p["summary"] = "훅 신선도"
        stats = llmwiki.build(self.ws, changed=[self.save(p)])
        self.assertEqual(stats["mode"], "incremental")
        idx, why = ctx.open_index(self.root_path)
        self.assertIsNotNone(idx, why)
        try:
            self.assertEqual(idx.revision, self.read_json("index/revision.json")["revision"])
            self.assertEqual(idx.map_root, json.loads((self.root_path / "index" / "search.work.json").read_text())["map_root"])
        finally:
            idx.close()
        text, result, _ = ctx.build_context(self.root_path, "스테이징 QA 기간")
        self.assertEqual(result.mode, "index")


# --------------------------------------------------------------------------- CLI · ingest · 감시자
class CliAndWatcherTest(IncrementalCase):
    def test_cli_accepts_changed_paths_relative_or_absolute_and_full(self) -> None:
        p = self.pages["topic-1"]
        p["summary"] = "CLI"
        rel = self.save(p)
        stats = json.loads(self.cli("build", "--changed", str(self.root_path / rel)).stdout)
        self.assertEqual(stats["mode"], "incremental")
        self.assertEqual(stats["delta"], {"added": 0, "modified": 1, "deleted": 0})
        stats = json.loads(self.cli("build", "--changed", rel).stdout)
        self.assertEqual((stats["mode"], stats["reason"]), ("incremental", "no-change"))
        stats = json.loads(self.cli("build", "--changed", rel, "--full").stdout)
        self.assertEqual((stats["mode"], stats["reason"]), ("full", "full-requested"))
        stats = json.loads(self.cli("build").stdout)
        self.assertEqual((stats["mode"], stats["reason"]), ("full", "no-hint"))

    def test_ingest_builds_incrementally_with_the_written_file_as_the_hint(self) -> None:
        source = self.write_raw("note.md", "# Note\n\n[[hub]] 를 참고하는 노트.\n")
        result = llmwiki.ingest(self.ws, source, "source", ["beta"], "노트")
        self.assertEqual(result["build"]["mode"], "incremental")
        self.assertEqual(result["build"]["delta"], {"added": 1, "modified": 0, "deleted": 0})
        self.assertIn("page:note", llmwiki.read_map(self.ws)["pages"])
        # type 이 바뀌어 파일이 옮겨지면 옛 경로도 힌트에 들어간다
        (self.root_path / "raw" / "note.md").write_text("# Note\n\n[[hub]] 를 참고하는 개념 노트.\n", encoding="utf-8")
        result = llmwiki.ingest(self.ws, source, "concept", ["beta"], "노트", update=True)
        self.assertEqual(result["moved_from"], "wiki/sources/note.json")
        self.assertEqual(result["build"]["mode"], "incremental")
        self.assertEqual(result["build"]["delta"], {"added": 0, "modified": 1, "deleted": 0})
        self.assertEqual(llmwiki.read_map(self.ws)["pages"]["page:note"]["source"], "wiki/concepts/note.json")

    def test_watcher_passes_changed_paths_and_retries_without_them(self) -> None:
        source = (REPO / "viewer" / "scripts" / "wiki-data.ts").read_text(encoding="utf-8")
        self.assertIn('args.push("--changed", ...changed)', source)
        self.assertIn("run(undefined, finish)", source)          # 증분 실패 → 인자 없이 재시도
        self.assertIn("debounceMs = 250", source)
        vite = (REPO / "viewer" / "vite.config.ts").read_text(encoding="utf-8")
        self.assertIn("schedule(path)", vite)
        bun = shutil.which("bun")
        if not bun:
            self.skipTest("bun 없음")
        script = ("const m = await import(%r); const a = m.buildArgs(['/w/a.json', '/w/b.json']); "
                  "const b = m.buildArgs(); const c = m.changedPaths(new Map([['x','1:1'],['y','1:1']]), "
                  "new Map([['x','2:1'],['z','1:1']])); console.log(JSON.stringify({a, b, c}));"
                  % str(REPO / "viewer" / "scripts" / "wiki-data.ts"))
        proc = subprocess.run([bun, "-e", script], capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(out["a"], ["scripts/llmwiki.py", "build", "--changed", "/w/a.json", "/w/b.json"])
        self.assertEqual(out["b"], ["scripts/llmwiki.py", "build"])
        self.assertEqual(out["c"], ["x", "y", "z"])


# --------------------------------------------------------------------------- 공용 순수 함수
class PureFunctionTest(IncrementalCase):
    def test_rids_are_content_derived(self) -> None:
        self.assertEqual(IDX.page_rid("page:hub"), IDX.page_rid("page:hub"))
        self.assertLess(IDX.page_rid("page:hub"), 1 << IDX.PRID_BITS)
        brid = IDX.block_rid(IDX.page_rid("page:hub"), 3)
        self.assertEqual(IDX.page_of(brid), IDX.page_rid("page:hub"))
        self.assertLess(brid, 1 << 63)

    def test_publish_bytes_do_not_depend_on_the_work_db_history(self) -> None:
        # grok s7: 수정 10 + 추가 10 + 삭제 10 을 매번 `--changed` 로 반영한 뒤 발행본이 cold 와 헤더까지 같은가.
        # 삭제가 free page 를 만들어 compact(VACUUM) 가 돌면 작업 DB 의 schema cookie 가 오르는데, 발행본은
        # 매번 빈 파일에 같은 DDL·PK 순으로 다시 쓰므로 그 이력이 바이트에 남지 않는다.
        compacted = False
        for i in range(10):
            p = self.pages[f"topic-{i}"]
            p["blocks"][p["block_order"][1]]["source_text"] += f" 라운드 {i} 수정."
            stats = llmwiki.build(self.ws, changed=[self.save(p)])
            self.assertEqual(stats["mode"], "incremental", stats)
        for i in range(10):
            stats = llmwiki.build(self.ws, changed=[self.save(big_page(f"extra-{i}", i))])
            self.assertEqual(stats["mode"], "incremental", stats)
        for i in range(10):
            stats = llmwiki.build(self.ws, changed=[self.remove(f"extra-{i}")])
            self.assertEqual(stats["mode"], "incremental", stats)
            compacted = compacted or bool(stats["index"].get("compacted"))
        self.assertTrue(compacted, "the rounds must exercise a compact of the work db")
        published = self.root_path / "index" / "search.sqlite"
        inc_bytes = published.read_bytes()
        inc_root = self.read_json("index/revision.json")["search_root"]
        work = sqlite3.connect(self.root_path / "index" / "search.work.sqlite")
        try:
            work_cookie = work.execute("PRAGMA schema_version").fetchone()[0]
        finally:
            work.close()
        cold = llmwiki.build(self.ws, full=True)
        self.assertEqual(cold["mode"], "full")
        self.assertEqual(published.read_bytes(), inc_bytes)
        self.assertEqual(self.read_json("index/revision.json")["search_root"], inc_root)
        pub = sqlite3.connect(published)
        try:
            pub_cookie = pub.execute("PRAGMA schema_version").fetchone()[0]
        finally:
            pub.close()
        self.assertNotEqual(work_cookie, pub_cookie)      # 작업 DB 의 이력은 발행본 헤더에 없다

    def test_publish_is_a_normalised_copy(self) -> None:
        # 작업 DB 를 다른 순서로 채워도 publish 바이트는 같다
        published = self.root_path / "index" / "search.sqlite"
        first = published.read_bytes()
        docs = [(f"wiki/concepts/{p['slug']}.json", p) for p in self.pages.values()]
        db = IDX._new_db(":memory:", revision=self.read_json("index/revision.json")["revision"], heading_paths=False,
                         map_root=llmwiki.map_root(llmwiki.read_map(self.ws)))
        try:
            half = len(docs) // 2
            IDX.apply_delta(db, {str(p["id"]): (r, p) for r, p in docs[half:]},
                            IDX.Delta(added=sorted(str(p["id"]) for _r, p in docs[half:])))
            db.commit()
            touched = IDX.apply_delta(db, {str(p["id"]): (r, p) for r, p in docs[:half]},
                                      IDX.Delta(added=sorted(str(p["id"]) for _r, p in docs[:half])))["touched"]
            IDX.refresh_graph(db, touched | {IDX.page_rid(str(p["id"])) for _r, p in docs})
            db.commit()
            out = self.root_path / "other.sqlite"
            digest = IDX.publish(db, out)
        finally:
            db.close()
        self.assertEqual(digest, self.read_json("index/revision.json")["search_root"])
        self.assertEqual(out.read_bytes(), first)
        self.assertEqual(IDX.file_digest(out), digest)

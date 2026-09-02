"""검색 색인(`scripts/llmwiki_index.py`)과 색인 경로의 컨텍스트 주입.

정본 → `index/search.sqlite` build 의 결정성, 훅의 fail-open(색인 없음·낡음), 행 단위 선택의
320자 상한, heading block 제외, 낡은 page 본문 생략, 바이트·토큰 상한, 무주입 옵션, redact,
워치독까지 — FINAL_PROPOSAL §2 의 항목을 하나씩 고정한다.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from unittest import mock

from tests.support import (CONTEXT_SCRIPT, WorkspaceCase, llmwiki, llmwiki_context as ctx,
                           make_page)

IDX = ctx.IDX

LONG_TABLE = "| 항목 | 값 |\n| --- | --- |\n" + "\n".join(
    f"| 행{i} 잡음 {'x' * 20} | {i * 7} |" for i in range(1, 30)) + "\n| 스테이징 QA 기간 | 2026-05-08 ~ 2026-05-15 |\n"

CORPUS = [
    make_page("폐기-icd-코드",
              "# 폐기 ICD 코드\n\n"
              "RefModel 기준으로 무효 처리된 RefCode-CM 코드 324건을 어떻게 노출할지의 정책이다.\n\n"
              "## 핵심 사실\n\n"
              "- 전체 모집단은 324건이며 Condition 도달 149건과 Observation 전용 175건으로 나뉜다.\n"
              "- 자체 개념 유지와 Maps to 참고 후보를 섞은 혼합 정책이 권고됐다.\n\n"
              "> ⚠️ 상충: OSE_DATA_03 문서는 149건이라고 적었고 전수 대조는 324건을 보고했다.\n",
              type="concept", projects=["beta"], tags=["폐기-코드"],
              sources=["source:beta-폐기-icd-코드-정책-비교", "user:2026-08-12"],
              summary="RefModel 무효 ICD 코드의 검색 노출 정책."),
    make_page("golden-set",
              "# Golden Set\n\n"
              "The golden set is the manually curated answer key for AlphaStd SDTM mapping "
              "regression checks. Field name login_rate_limited marks throttled rows.\n",
              type="concept", projects=["alpha"], tags=["검증"],
              sources=["source:샘플노트-2026-07-16"],
              summary="AlphaStd SDTM 매핑 회귀 검증용 정답지."),
    make_page("배포-메모",
              "# 배포 메모\n\n"
              "api_key: sk-live-should-never-leak 를 설정에 넣지 말 것. 배포 절차는 수요일이다.\n",
              type="concept", projects=["beta"], summary="배포 시 주의사항."),
    make_page("릴리스-일정",
              "# 릴리스 일정\n\n## 공장 관리자 릴리스\n\n" + LONG_TABLE,
              type="concept", projects=["beta"], tags=["릴리스"], summary="릴리스별 일정표."),
    make_page("릴리스-일정-v1",
              "# 릴리스 일정 (구판)\n\n스테이징 QA 기간은 2026-04-01 ~ 2026-04-07 이었다. 낡은 값이다.\n",
              type="concept", projects=["beta"], tags=["릴리스"], summary="구판 일정."),
]
# 릴리스-일정 이 구판을 대체한다 (supersedes 간선은 새 page 위에 있다).
CORPUS[3]["links"].append({"target": "릴리스-일정-v1", "label": "릴리스-일정-v1", "kind": "supersedes"})


class IndexCase(WorkspaceCase):
    def setUp(self) -> None:
        super().setUp()
        self.write_pages(CORPUS, name="corpus.json")
        self.root_path = Path(self.root).resolve()

    def build(self, **kw: object) -> dict:
        return llmwiki.build(self.ws, **kw)

    def db(self) -> Path:
        return self.root_path / "index" / "search.sqlite"

    def context(self, query: str, **options: object) -> tuple[str, object, list[dict]]:
        return ctx.build_context(self.root_path, query, **options)


# --------------------------------------------------------------------------- build
class BuildTest(IndexCase):
    def test_build_is_deterministic_and_records_search_root(self) -> None:
        self.build()
        first = self.db().read_bytes()
        rev1 = self.read_json("index/revision.json")
        self.build()
        self.assertEqual(self.db().read_bytes(), first)
        rev2 = self.read_json("index/revision.json")
        self.assertEqual(rev1, rev2)
        self.assertEqual(len(rev1["search_root"]), 64)
        idx = IDX.open_ro(self.db())
        try:
            self.assertEqual(IDX.file_digest(self.db()), rev1["search_root"])
            self.assertEqual(len(IDX.logical_digest(idx.db)), 64)
            self.assertEqual(idx.revision, rev1["revision"])
            self.assertEqual(idx.npages, len(CORPUS))
        finally:
            idx.close()

    def test_build_leaves_no_partial_file_and_uses_wal(self) -> None:
        self.build()
        names = sorted(p.name for p in (self.root_path / "index").iterdir())
        self.assertNotIn("search.sqlite.tmp", names)
        self.assertNotIn("search.sqlite.build", names)
        db = sqlite3.connect(self.db())
        try:
            self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        finally:
            db.close()

    def test_heading_and_title_blocks_are_indexed_but_not_evidence(self) -> None:
        self.build()
        idx = IDX.open_ro(self.db())
        try:
            kinds = dict(idx.db.execute("SELECT rid, kind FROM blk"))
            for rid in idx.noev:
                self.assertIn(kinds[rid], {"heading", "title", "thematic_break"})
            found = idx.search("공장 관리자 릴리스 스테이징 QA 기간")
        finally:
            idx.close()
        self.assertEqual(found.hits[0].page_id, "page:릴리스-일정")
        blocks = {b for h in found.hits for b in h.block_ids}
        page = CORPUS[3]
        for bid in blocks:
            if bid in page["blocks"]:
                self.assertNotEqual(page["blocks"][bid]["kind"], "heading")

    def test_identifier_fragments_are_indexed(self) -> None:
        self.assertIn("rate", IDX.tokenize("login_rate_limited"))
        self.assertIn("login_rate_limited", IDX.tokenize("login_rate_limited"))
        self.build()
        idx = IDX.open_ro(self.db())
        try:
            found = idx.search("rate limited rows")
        finally:
            idx.close()
        self.assertEqual(found.hits[0].page_id, "page:golden-set")

    def test_supersedes_chain_folds_to_the_head(self) -> None:
        self.build()
        idx = IDX.open_ro(self.db())
        try:
            found = idx.search("스테이징 QA 기간")
            rows = idx.pages(["page:릴리스-일정-v1", "page:릴리스-일정"])
        finally:
            idx.close()
        self.assertEqual(rows["page:릴리스-일정-v1"]["head"], rows["page:릴리스-일정"]["rid"])
        self.assertEqual(rows["page:릴리스-일정-v1"]["sup_state"], "stale")
        by_id = {h.page_id: h for h in found.hits}
        self.assertIn("page:릴리스-일정", by_id)
        self.assertEqual(by_id["page:릴리스-일정-v1"].head, "page:릴리스-일정")
        self.assertLess(by_id["page:릴리스-일정-v1"].score, by_id["page:릴리스-일정"].score)

    def test_a_fork_is_not_silently_collapsed(self) -> None:
        pages = [make_page("old", "# Old\n\n옛 주장이다.\n"),
                 make_page("new-a", "# New A\n\n새 주장 A.\n"),
                 make_page("new-b", "# New B\n\n새 주장 B.\n")]
        for p in pages[1:]:
            p["links"].append({"target": "old", "label": "old", "kind": "supersedes"})
        idx = IDX.build_memory([("wiki/x.json", p) for p in pages])
        rows = idx.pages(["page:old"])
        idx.close()
        self.assertEqual(rows["page:old"]["sup_state"], "fork")
        pages = [make_page("a", "# A\n\n에이.\n"), make_page("b", "# B\n\n비.\n")]
        pages[0]["links"].append({"target": "b", "label": "b", "kind": "supersedes"})
        pages[1]["links"].append({"target": "a", "label": "a", "kind": "supersedes"})
        idx = IDX.build_memory([("wiki/y.json", p) for p in pages])
        rows = idx.pages(["page:a", "page:b"])
        idx.close()
        self.assertEqual({r["sup_state"] for r in rows.values()}, {"cycle"})
        self.assertEqual(rows["page:a"]["head"], rows["page:a"]["rid"])

    def test_credentials_never_reach_the_index(self) -> None:
        self.build()
        raw = self.db().read_bytes()
        self.assertNotIn(b"sk-live-should-never-leak", raw)
        self.assertIn("(접속 정보 생략)".encode("utf-8"), raw)


# --------------------------------------------------------------------------- rows
class SelectRowsTest(IndexCase):
    def test_short_blocks_are_whole(self) -> None:
        self.assertEqual(IDX.select_rows("짧은 본문", {}, 320), ("짧은 본문", False))

    def test_row_selection_never_exceeds_the_limit(self) -> None:
        wt = {"스테": 9.0, "테이": 9.0, "이징": 9.0, "기간": 9.0}
        for limit in (60, 100, 200, 320, 321, 400):
            for raw in (LONG_TABLE, "\n".join(f"- 항목 {i} {'가' * 50}" for i in range(20)),
                        " ".join(f"문장 {i} 입니다." for i in range(80)),
                        "x" * 2000):
                body, cut = IDX.select_rows(raw, wt, limit)
                self.assertLessEqual(len(body), limit, (limit, raw[:30]))
                self.assertTrue(cut)

    def test_the_matching_row_is_chosen_with_the_table_header(self) -> None:
        wt = {"스테": 9.0, "테이": 9.0, "이징": 9.0, "기간": 9.0}
        body, _ = IDX.select_rows(LONG_TABLE, wt, 320)
        self.assertIn("2026-05-08 ~ 2026-05-15", body)
        self.assertTrue(body.startswith("| 항목 | 값 |"))
        self.assertNotIn("행1 ", body)


# --------------------------------------------------------------------------- context
class IndexedContextTest(IndexCase):
    def setUp(self) -> None:
        super().setUp()
        self.build()

    def test_index_path_is_used_and_renders_pbe(self) -> None:
        text, result, pages = self.context("폐기 ICD 코드는 몇 건인가요?")
        self.assertEqual(result.mode, "index")
        self.assertEqual(result.reason, "index")
        self.assertTrue(text.startswith("<llmwiki-context v=3>"))
        self.assertIn("P 폐기-icd-코드 concept", text)
        self.assertIn("B 폐기-icd-코드#", text)
        self.assertIn("324건", text)
        self.assertEqual(pages[0]["id"], "page:폐기-icd-코드")
        self.assertEqual(pages[0]["via"], "index")

    def test_stale_page_body_is_omitted(self) -> None:
        text, _, pages = self.context("공장 관리자 릴리스의 스테이징 QA 기간은 언제부터 언제까지인가요?")
        self.assertIn("2026-05-08 ~ 2026-05-15", text)
        self.assertNotIn("2026-04-01", text)
        # 낡은 page 는 head 점수 × 0.3 이라 cut(0.5) 아래지만, head 가 실리면 `sup→` P 한 줄로 함께 실린다.
        self.assertIn("P 릴리스-일정-v1 concept", text)
        self.assertIn("sup→릴리스-일정", text)
        stale = next(p for p in pages if p["id"] == "page:릴리스-일정-v1")
        self.assertEqual(stale["blocks"], [])
        self.assertEqual(stale["superseded_by"], "릴리스-일정")
        lines = text.splitlines()
        head_at = next(i for i, l in enumerate(lines) if l.startswith("P 릴리스-일정 concept"))
        self.assertTrue(lines[head_at + 1].startswith("B 릴리스-일정#"))
        self.assertTrue(any(l.startswith("P 릴리스-일정-v1 concept") for l in lines[head_at + 1:]))
        # sup→ 줄은 head 묶음 바로 뒤에 온다(예산이 뒤에서 끊겨도 head 와 함께 남는다)
        sup_at = next(i for i, l in enumerate(lines) if l.startswith("P 릴리스-일정-v1 concept"))
        self.assertTrue(all(not l.startswith("P ") for l in lines[head_at + 1:sup_at]))

    def test_a_question_about_the_old_body_yields_the_head_body_and_a_sup_line(self) -> None:
        # (b) head 에 anchor block 이 없고(links 만, block_id 없음) 질문 토큰도 head 본문에 없다 → head 의 첫 본문 block
        text, result, pages = self.context("낡은 값이다 이었다")
        self.assertEqual(result.grade, "strong")
        self.assertGreater(len(text.encode("utf-8")), 0)
        self.assertIn("P 릴리스-일정 concept", text)
        self.assertIn("B 릴리스-일정#", text)
        self.assertIn("P 릴리스-일정-v1 concept", text)
        self.assertIn("sup→릴리스-일정", text)
        # (c) 옛 본문 문장은 절대 안 나온다
        self.assertNotIn("2026-04-01", text)
        self.assertNotIn("낡은 값이다", text)
        head = next(p for p in pages if p["id"] == "page:릴리스-일정")
        self.assertEqual(len(head["blocks"]), 1)
        self.assertNotEqual(head["blocks"][0]["kind"], "heading")
        self.assertEqual(pages[0]["id"], "page:릴리스-일정")
        self.assertEqual(pages[1]["id"], "page:릴리스-일정-v1")

    def test_the_supersedes_anchor_block_is_the_head_evidence(self) -> None:
        # (a) head 의 supersedes 링크가 block 에 걸려 있으면(links[].block_id) 그 block 이 근거다
        old = make_page("old-policy", "# 옛 정책\n\n옛 정책은 수요일 배포였다. 고유토큰 zebra 만 여기 있다.\n",
                        projects=["beta"], tags=["정책"])
        new = make_page("new-policy", "# 새 정책\n\n새 정책은 금요일 배포다.\n\n대체 근거: 수요일 배포는 [[old-policy]] 로 끝났다.\n",
                        projects=["beta"], tags=["정책"])
        anchor = new["block_order"][2]
        new["links"].append({"target": "old-policy", "label": "old-policy", "kind": "supersedes", "block_id": anchor})
        self.write_pages([old, new], name="policy.json")
        self.build()
        text, _, pages = self.context("고유토큰 zebra")
        self.assertIn("P new-policy concept", text)
        self.assertIn(f"B new-policy#{IDX.tail_of(anchor, 'new-policy')} cur | 대체 근거", text)
        self.assertIn("P old-policy concept", text)
        self.assertIn("sup→new-policy", text)
        self.assertNotIn("옛 정책은 수요일 배포였다", text)
        self.assertNotIn("zebra", text)
        head = next(p for p in pages if p["id"] == "page:new-policy")
        self.assertEqual([b["id"] for b in head["blocks"]], [anchor])
        # search JSON 도 같은 근거를 보인다 (blocks: [] 가 아니다)
        idx = IDX.open_ro(self.db())
        try:
            hit = next(h for h in idx.search("고유토큰 zebra").hits if h.page_id == "page:new-policy")
        finally:
            idx.close()
        self.assertEqual(hit.block_ids, [anchor])

    def test_a_stale_page_outside_the_top_k_still_follows_its_head(self) -> None:
        # 낡은 page 는 head × 0.3 이라 k 밖으로 밀릴 수 있다 — head 가 실리면 `sup→` 줄로 따라온다
        text, _, pages = self.context("낡은 값이다 이었다", limit=1)
        self.assertIn("P 릴리스-일정 concept", text)
        self.assertIn("P 릴리스-일정-v1 concept 2026-08-01 sup→릴리스-일정", text.replace(pages[1]["updated"], "2026-08-01"))

    def test_heading_blocks_are_not_evidence(self) -> None:
        text, _, pages = self.context("공장 관리자 릴리스의 스테이징 QA 기간")
        for page in pages:
            for block in page["blocks"]:
                self.assertNotEqual(block["kind"], "heading")
        self.assertNotIn("| ## ", text)

    def test_long_block_rows_stay_within_320_chars(self) -> None:
        text, _, _ = self.context("공장 관리자 릴리스의 스테이징 QA 기간")
        for line in text.splitlines():
            if line.startswith("B "):
                body = line.split(" | ", 1)[1]
                self.assertLessEqual(len(body), 320, line)

    def test_byte_and_token_caps_hold(self) -> None:
        query = "폐기 ICD 코드 정책 golden set mapping 릴리스 일정 배포"
        for max_bytes in (300, 600, 900, 1500, 4000, 20000):
            text, _, _ = self.context(query, max_bytes=max_bytes)
            self.assertLessEqual(len(text.encode("utf-8")), max_bytes, max_bytes)
        for max_tokens in (60, 120, 400, 2000):
            text, _, _ = self.context(query, max_tokens=max_tokens)
            self.assertLessEqual(ctx.est_tokens(text), max_tokens, max_tokens)

    def test_credentials_are_masked(self) -> None:
        text, _, pages = self.context("배포 절차 메모 수요일")
        self.assertTrue(pages)
        self.assertNotIn("sk-live-should-never-leak", text)
        self.assertIn("(접속 정보 생략)", text)

    def test_silence_threshold_is_off_by_default_and_optional(self) -> None:
        text, result, _ = self.context("파스타 삶는 법 알려줘")
        self.assertEqual(result.reason, "no-match")   # 한 토큰도 겹치지 않으면 hit 자체가 없다
        text, result, _ = self.context("파스타 삶는 법과 정책 이야기")
        self.assertEqual(result.grade, "strong")      # 문턱이 없으면 겹치는 게 있는 한 넣는다
        self.assertTrue(text)
        text, result, _ = self.context("파스타 삶는 법과 정책 이야기", silence_t=1e9)
        self.assertEqual(text, "")
        self.assertEqual(result.grade, "none")
        self.assertEqual(result.reason, "below-threshold:index")
        text, result, _ = self.context("폐기 ICD 코드는 몇 건인가요?", silence_t=1e9, hint_t=0.001)
        self.assertEqual(result.grade, "weak")
        self.assertIn("<llmwiki-context v=3 weak>", text)
        self.assertNotIn("324건", text)
        self.assertIn("폐기-icd-코드#", text)

    def test_unrelated_words_with_no_overlap_inject_nothing(self) -> None:
        text, result, _ = self.context("zzqq xxyy vvww 1234567")
        self.assertEqual(text, "")
        self.assertEqual(result.reason, "no-match")

    def test_pinned_page_rides_along_and_is_not_repeated(self) -> None:
        path = self.root_path / ctx.ALWAYS_CONFIG
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"always": ["배포-메모"]}, ensure_ascii=False), encoding="utf-8")
        text, result, pages = self.context("배포 절차 메모 수요일")
        self.assertEqual(text.count("page:배포-메모"), 1)
        self.assertNotIn("P 배포-메모", text)
        self.assertNotIn("page:배포-메모", [p["id"] for p in pages])
        text, _, _ = self.context("zzqq xxyy")
        self.assertIn("page:배포-메모", text)
        self.assertTrue(text.startswith("<llmwiki-context v=3>"))

    def test_search_rows_come_from_the_index(self) -> None:
        payload = ctx.search_rows(self.root_path, "폐기 ICD 코드", 3)
        self.assertEqual(payload["mode"], "index")
        self.assertEqual(payload["results"][0]["id"], "page:폐기-icd-코드")
        self.assertEqual(payload["results"][0]["file"], "wiki/concepts/corpus.json")

    def test_get_reads_one_canonical_file_via_the_index_hint(self) -> None:
        doc = ctx.find_doc(self.root_path, "폐기-icd-코드")
        self.assertIsNotNone(doc)
        self.assertEqual(doc.page_id, "page:폐기-icd-코드")
        self.assertEqual(doc.rel, "wiki/concepts/corpus.json")
        self.assertIsNone(ctx.read_doc(self.root_path, "../outside.json", "page:x"))
        # 힌트가 빗나가면(색인에 없는 새 page) 정본 스캔이 받는다.
        self.write_pages([make_page("신규", "# 신규\n\n새 문서.\n")], name="new.json")
        self.assertEqual(ctx.find_doc(self.root_path, "신규").page_id, "page:신규")


class FreshnessTest(IndexCase):
    """디스크 색인을 못 쓰면 정본에서 메모리 색인을 만든다 — 출력 문법은 색인 경로와 같다."""

    def test_without_an_index_the_memory_index_is_used(self) -> None:
        text, result, pages = self.context("폐기 ICD 코드는 몇 건인가요?")
        self.assertEqual(result.mode, "memory")
        self.assertEqual(result.reason, "memory")
        self.assertEqual(result.fallback, "no-index")
        self.assertTrue(text.startswith("<llmwiki-context v=3>"))
        self.assertIn("B 폐기-icd-코드#", text)
        self.assertEqual(pages[0]["id"], "page:폐기-icd-코드")
        self.assertEqual(pages[0]["via"], "memory")
        self.assertIn("memory_build_ms", result.stats)

    def test_memory_fallback_renders_the_same_bytes_as_the_index(self) -> None:
        self.build()
        query = "공장 관리자 릴리스의 스테이징 QA 기간은 언제부터 언제까지인가요?"
        indexed, result_i, _ = self.context(query)
        self.db().unlink()
        memory, result_m, _ = self.context(query)
        self.assertEqual((result_i.mode, result_m.mode), ("index", "memory"))
        self.assertEqual(indexed, memory)

    def test_a_revision_mismatch_falls_back_to_memory(self) -> None:
        self.build()
        rev = self.root_path / "index" / "revision.json"
        rev.write_text(json.dumps({"schema_version": "1.0", "revision": "0" * 64}), encoding="utf-8")
        text, result, _ = self.context("폐기 ICD 코드는 몇 건인가요?")
        self.assertEqual(result.mode, "memory")
        self.assertEqual(result.fallback, "revision-mismatch")
        self.assertTrue(text.startswith("<llmwiki-context v=3>"))

    def test_a_newer_canonical_file_falls_back_to_memory(self) -> None:
        self.build()
        time.sleep(0.02)
        self.write_pages([make_page("신규", "# 신규 문서\n\n색인에 없는 새 문서다.\n")], name="new.json")
        future = time.time() + 5
        os.utime(self.root_path / "wiki" / "concepts" / "new.json", (future, future))
        text, result, pages = self.context("신규 문서")
        self.assertEqual(result.mode, "memory")
        self.assertEqual(result.fallback, "stale-mtime")
        self.assertEqual(pages[0]["id"], "page:신규")
        self.assertIn("B 신규#", text)

    def test_memory_fallback_stays_far_inside_the_watchdog(self) -> None:
        started = time.perf_counter()
        _, result, _ = self.context("폐기 ICD 코드는 몇 건인가요?")
        elapsed = time.perf_counter() - started
        self.assertEqual(result.mode, "memory")
        self.assertLess(elapsed, ctx.WATCHDOG_SECONDS / 3)
        self.assertLess(result.stats["memory_build_ms"], 1000)


class ScanLastResortTest(IndexCase):
    """메모리 색인 build 마저 실패하면 옛 스캔 경로가 exit 0 을 지킨다."""

    def test_a_memory_build_failure_falls_to_scan(self) -> None:
        with mock.patch.object(IDX, "build_memory", side_effect=RuntimeError("boom")):
            text, result, pages = self.context("폐기 ICD 코드는 몇 건인가요?")
        self.assertEqual(result.mode, "scan")
        self.assertEqual(result.fallback, "no-index;memory-error:RuntimeError")
        self.assertTrue(text.startswith("<llmwiki-context>\n"))
        self.assertEqual(pages[0]["id"], "page:폐기-icd-코드")

    def test_the_memory_path_can_be_turned_off_per_call(self) -> None:
        self.build()
        _, result, _ = self.context("폐기 ICD 코드는 몇 건인가요?", use_index=False, use_memory=False)
        self.assertEqual(result.mode, "scan")
        self.assertEqual(result.fallback, "disabled;memory-disabled")

    def test_search_rows_follow_the_same_three_paths(self) -> None:
        self.assertEqual(ctx.search_rows(self.root_path, "폐기 ICD 코드")["mode"], "memory")
        self.build()
        self.assertEqual(ctx.search_rows(self.root_path, "폐기 ICD 코드")["mode"], "index")
        with mock.patch.dict(os.environ, {ctx.ENV_INDEX: "0", ctx.ENV_MEMORY: "0"}):
            rows = ctx.search_rows(self.root_path, "폐기 ICD 코드")
        self.assertEqual(rows["mode"], "scan")
        self.assertEqual(rows["fallback"], "disabled;memory-disabled")

    def test_a_changed_hit_page_is_reread_from_the_canonical_file(self) -> None:
        self.build()
        target = self.root_path / "wiki" / "concepts" / "corpus.json"
        stat = target.stat()
        pages = json.loads(target.read_text(encoding="utf-8"))
        page = next(p for p in pages if p["id"] == "page:폐기-icd-코드")
        bid = page["block_order"][1]
        page["blocks"][bid]["data"]["text"] = page["blocks"][bid]["data"]["text"].replace("324건", "999건")
        page["blocks"][bid]["source_text"] = page["blocks"][bid]["source_text"].replace("324건", "999건")
        target.write_text(json.dumps(pages, ensure_ascii=False, indent=2), encoding="utf-8")
        os.utime(target, (stat.st_atime, stat.st_mtime))      # mtime 이 낡음을 숨기는 경우다
        text, result, proj = self.context("폐기 ICD 코드는 몇 건인가요?")
        self.assertEqual(result.mode, "index")
        self.assertEqual(result.stats["reread"], ["page:폐기-icd-코드"])
        self.assertIn("999건", text)
        self.assertNotIn("324건을 어떻게", text)
        self.assertTrue(proj[0].get("reread"))

    def test_a_deleted_hit_page_is_dropped(self) -> None:
        self.build()
        target = self.root_path / "wiki" / "concepts" / "corpus.json"
        stat = target.stat()
        pages = [p for p in json.loads(target.read_text(encoding="utf-8")) if p["id"] != "page:배포-메모"]
        target.write_text(json.dumps(pages, ensure_ascii=False, indent=2), encoding="utf-8")
        os.utime(target, (stat.st_atime, stat.st_mtime))
        text, result, proj = self.context("배포 절차 메모 수요일")
        self.assertEqual(result.mode, "index")
        self.assertIn("page:배포-메모", result.stats["missing"])
        self.assertNotIn("배포-메모", text)


# --------------------------------------------------------------------------- hook
class HookIndexTest(IndexCase):
    def hook(self, prompt: str, *, env: dict[str, str] | None = None,
             timeout: float = 30) -> subprocess.CompletedProcess[str]:
        payload = json.dumps({"prompt": prompt, "cwd": "/tmp", "hook_event_name": "UserPromptSubmit"},
                             ensure_ascii=False)
        environ = {**os.environ, llmwiki.ENV_ROOT: str(self.root_path), **(env or {})}
        return subprocess.run([sys.executable, str(CONTEXT_SCRIPT), "hook"], input=payload,
                              capture_output=True, text=True, env=environ, timeout=timeout)

    def test_hook_uses_the_index_and_logs_the_mode(self) -> None:
        self.build()
        log = self.root_path / "hook.log"
        proc = self.hook("폐기 ICD 코드는 몇 건인가요?", env={ctx.ENV_LOG: str(log)})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        text = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("<llmwiki-context v=3>", text)
        row = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(row["mode"], "index")
        self.assertTrue(row["injected"])
        self.assertNotIn("몇 건인가요", json.dumps(row, ensure_ascii=False))

    def test_hook_finishes_inside_the_watchdog_including_index_open(self) -> None:
        self.build()
        started = time.perf_counter()
        proc = self.hook("폐기 ICD 코드는 몇 건인가요?", env={ctx.ENV_TIMEOUT: "6"})
        elapsed = time.perf_counter() - started
        self.assertEqual(proc.returncode, 0)
        self.assertIn("폐기-icd-코드", proc.stdout)
        self.assertLess(elapsed, 6.0)

    def test_an_expired_watchdog_exits_quietly(self) -> None:
        self.build()
        proc = self.hook("폐기 ICD 코드는 몇 건인가요?", env={ctx.ENV_TIMEOUT: "0.0001"})
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")

    def test_a_corrupt_index_file_fails_open_to_memory(self) -> None:
        self.build()
        (self.root_path / "index" / "search.sqlite").write_bytes(b"not a database at all")
        log = self.root_path / "hook.log"
        proc = self.hook("폐기 ICD 코드는 몇 건인가요?", env={ctx.ENV_LOG: str(log)})
        self.assertEqual(proc.returncode, 0)
        text = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertTrue(text.startswith("<llmwiki-context v=3>"))
        self.assertIn("B 폐기-icd-코드#", text)
        row = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(row["mode"], "memory")
        self.assertTrue(row["fallback"].startswith("open-error"))
        self.assertIn("memory_build_ms", row)

    def test_index_can_be_disabled_by_environment(self) -> None:
        self.build()
        log = self.root_path / "hook.log"
        self.hook("폐기 ICD 코드는 몇 건인가요?", env={ctx.ENV_LOG: str(log), ctx.ENV_INDEX: "0"})
        row = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(row["mode"], "memory")
        self.assertEqual(row["fallback"], "disabled")

    def test_memory_path_can_be_disabled_by_environment_leaving_scan(self) -> None:
        log = self.root_path / "hook.log"
        proc = self.hook("폐기 ICD 코드는 몇 건인가요?", env={ctx.ENV_LOG: str(log), ctx.ENV_MEMORY: "0"})
        self.assertEqual(proc.returncode, 0)
        text = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertTrue(text.startswith("<llmwiki-context>\n"))
        row = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(row["mode"], "scan")
        # 최후 스캔을 고르면 디스크 색인도 보지 않는다 — 있어도 없어도 같은 경로다.
        self.assertEqual(row["fallback"], "disabled;memory-disabled")

    def test_hook_without_an_index_finishes_inside_the_watchdog(self) -> None:
        started = time.perf_counter()
        proc = self.hook("폐기 ICD 코드는 몇 건인가요?", env={ctx.ENV_TIMEOUT: "6"})
        elapsed = time.perf_counter() - started
        self.assertEqual(proc.returncode, 0)
        self.assertIn("<llmwiki-context v=3>", proc.stdout)
        self.assertLess(elapsed, 6.0)

    def test_v3_output_is_not_reinjected(self) -> None:
        self.build()
        proc = self.hook("<llmwiki-context v=3>\nP x\n</llmwiki-context> 폐기 ICD 코드는?")
        self.assertEqual(proc.stdout, "")


class VerifyProbeTest(IndexCase):
    """installer verify 의 probe 는 본문 block 에서 질문을 뽑고 검색·주입을 따로 검사한다."""

    def probes(self) -> dict[str, dict]:
        report = ctx.verify(self.root_path, clients=(), python=sys.executable)
        return {c["check"]: c for c in report["checks"]}

    def test_probe_query_comes_from_a_body_block_not_a_title(self) -> None:
        # 가장 긴 제목의 page 에는 본문이 거의 없다 — 옛 probe 는 여기서 헛되이 실패했다.
        self.write_pages([make_page("긴-제목", "# 아주 길고 화려한 제목의 프로젝트 운영 규정 안내 문서 모음집\n\n짧다.\n")],
                         name="title.json")
        query, expected = ctx.probe_query(ctx.load_corpus(self.root_path))
        self.assertTrue(query)
        self.assertNotEqual(expected, "page:긴-제목")
        self.assertNotIn("화려한", query)
        self.assertLessEqual(len(query.split()), ctx.PROBE_WORDS)

    def test_probe_query_skips_superseded_pages(self) -> None:
        old = make_page("구판", "# 구판\n\n" + "구판에만 있는 아주 긴 본문 문단이다. " * 30 + "\n")
        new = make_page("신판", "# 신판\n\n신판은 짧다. 그러나 구판을 대체한다는 사실은 길게 적어 둔다.\n")
        new["links"].append({"target": "구판", "label": "구판", "kind": "supersedes"})
        self.write_pages([old, new], name="sup.json")
        _, expected = ctx.probe_query(ctx.load_corpus(self.root_path))
        self.assertNotEqual(expected, "page:구판")

    def test_both_probe_checks_pass_with_and_without_an_index(self) -> None:
        for built in (False, True):
            if built:
                self.build()
            checks = self.probes()
            self.assertTrue(checks["probe-query"]["ok"], checks["probe-query"])
            self.assertTrue(checks["probe-search"]["ok"], checks["probe-search"])
            self.assertTrue(checks["probe-injects"]["ok"], checks["probe-injects"])
            self.assertIn("mode=index" if built else "mode=memory", checks["probe-injects"]["detail"])
            self.assertIn("expected_hit=True", checks["probe-search"]["detail"])

    def test_probe_checks_tell_search_and_injection_apart(self) -> None:
        # 검색은 되지만 렌더가 비면 probe-search 만 통과하고 probe-injects 가 실패해야 한다.
        with mock.patch.object(ctx, "run_hook", return_value=("", {"mode": "index", "reason": "index"})):
            checks = self.probes()
        self.assertTrue(checks["probe-search"]["ok"])
        self.assertFalse(checks["probe-injects"]["ok"])
        with mock.patch.object(ctx, "search_rows", return_value={"mode": "index", "results": []}):
            checks = self.probes()
        self.assertFalse(checks["probe-search"]["ok"])


class CliIndexTest(IndexCase):
    def test_query_answers_from_the_fresh_index(self) -> None:
        self.build()
        rows = [json.loads(line) for line in self.cli("query", "폐기 ICD 코드").stdout.splitlines()]
        self.assertEqual(rows[0]["id"], "page:폐기-icd-코드")
        self.assertTrue(rows[0]["blocks"])

    def test_context_json_reports_mode_and_signals(self) -> None:
        self.build()
        payload = json.loads(self.context_cli("context", "폐기 ICD 코드", "--json").stdout)
        self.assertEqual(payload["mode"], "index")
        self.assertIn("raw_x_cov", payload["signals"])
        self.assertEqual(payload["bytes"], len(payload["text"].encode("utf-8")))

    def test_doctor_reports_the_index(self) -> None:
        self.build()
        payload = json.loads(self.context_cli("doctor").stdout)
        self.assertTrue(payload["search_index"]["fresh"])
        self.assertEqual(payload["search_index"]["pages"], len(CORPUS))

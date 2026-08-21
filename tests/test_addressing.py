"""Direct page and block addressing: get, pointer, outline, section."""
from __future__ import annotations

from tests.support import WorkspaceCase, llmwiki, make_page

BODY = """# Sample Topic

개요 문단이다.

## 폐기(비활성)

폐기된 tier 설명.

### 세부

세부 항목.

## 현행

현행 tier 설명. [[beta]]
"""


class ResolveTest(WorkspaceCase):
    def setUp(self) -> None:
        super().setUp()
        self.page = make_page("sample-topic", BODY, projects=["beta"])
        self.write_pages([self.page, make_page("beta", "# Beta\n\n[[sample-topic]]\n", projects=["beta"])])
        self.block_id = self.page["block_order"][1]

    def test_find_page_by_slug_id_or_bare_name(self) -> None:
        for selector in ("sample-topic", "page:sample-topic"):
            self.assertEqual(llmwiki.find_page(self.ws, selector)["id"], "page:sample-topic")

    def test_unknown_page_raises(self) -> None:
        with self.assertRaisesRegex(llmwiki.WikiError, "page not found"):
            llmwiki.find_page(self.ws, "does-not-exist")

    def test_resolve_page_only(self) -> None:
        page, block = llmwiki.resolve(self.ws, "sample-topic")
        self.assertEqual(page["id"], "page:sample-topic")
        self.assertIsNone(block)

    def test_resolve_block_scoped_to_page(self) -> None:
        page, block = llmwiki.resolve(self.ws, f"sample-topic#{self.block_id}")
        self.assertEqual(page["id"], "page:sample-topic")
        self.assertEqual(block["id"], self.block_id)

    def test_resolve_block_without_page_context(self) -> None:
        page, block = llmwiki.resolve(self.ws, self.block_id)
        self.assertEqual(page["id"], "page:sample-topic")
        self.assertEqual(block["id"], self.block_id)

    def test_resolve_block_by_fingerprint(self) -> None:
        fingerprint = self.page["blocks"][self.block_id]["fingerprint"]
        _, block = llmwiki.find_block(self.ws, fingerprint)
        self.assertEqual(block["id"], self.block_id)

    def test_unknown_block_raises(self) -> None:
        with self.assertRaisesRegex(llmwiki.WikiError, "block not found"):
            llmwiki.resolve(self.ws, "sample-topic#block:nope")


class ProjectionOfFieldsTest(WorkspaceCase):
    def setUp(self) -> None:
        super().setUp()
        self.page = make_page("sample-topic", BODY, projects=["beta"], tags=["검색"])
        self.write_pages([self.page, make_page("beta", "# Beta\n\n[[sample-topic]]\n", projects=["beta"])])

    def test_pick_fields_walks_dicts_and_arrays(self) -> None:
        self.assertEqual(llmwiki.pick_fields(self.page, ["title"]), "Sample Topic")
        self.assertEqual(llmwiki.pick_fields(self.page, ["tags", "0"]), "검색")
        self.assertEqual(llmwiki.pick_fields(self.page, ["history", "0", "action"]), "created")

    def test_pick_fields_reports_missing_key(self) -> None:
        with self.assertRaisesRegex(llmwiki.WikiError, "field not found: nope"):
            llmwiki.pick_fields(self.page, ["nope"])

    def test_json_pointer_reads_nested_values(self) -> None:
        bid = self.page["block_order"][0]
        self.assertEqual(llmwiki.json_pointer(self.page, f"/blocks/{bid}/data/text"), "Sample Topic")
        self.assertEqual(llmwiki.json_pointer(self.page, "/block_order/0"), bid)
        self.assertIs(llmwiki.json_pointer(self.page, ""), self.page)

    def test_json_pointer_escapes(self) -> None:
        self.assertEqual(llmwiki.json_pointer({"a/b": {"c~d": 1}}, "/a~1b/c~0d"), 1)

    def test_json_pointer_errors_are_actionable(self) -> None:
        with self.assertRaisesRegex(llmwiki.WikiError, "missing key"):
            llmwiki.json_pointer(self.page, "/nope")
        with self.assertRaisesRegex(llmwiki.WikiError, "bad array index"):
            llmwiki.json_pointer(self.page, "/block_order/99")


class OutlineAndSectionTest(WorkspaceCase):
    def setUp(self) -> None:
        super().setUp()
        self.page = make_page("sample-topic", BODY, projects=["beta"])

    def test_outline_lists_headings_only(self) -> None:
        rows = llmwiki.outline(self.page)
        self.assertEqual([r["text"] for r in rows],
                         ["Sample Topic", "폐기(비활성)", "세부", "현행"])
        self.assertEqual([r["level"] for r in rows], [1, 2, 3, 2])
        self.assertTrue(all(r["block_id"] in self.page["blocks"] for r in rows))

    def test_section_stops_at_the_next_same_level_heading(self) -> None:
        target = next(r["block_id"] for r in llmwiki.outline(self.page) if r["text"] == "폐기(비활성)")
        texts = [b["source_text"] for b in llmwiki.section(self.page, target)]
        self.assertEqual(texts, ["## 폐기(비활성)", "폐기된 tier 설명.", "### 세부", "세부 항목."])

    def test_section_of_a_leaf_heading(self) -> None:
        target = next(r["block_id"] for r in llmwiki.outline(self.page) if r["text"] == "현행")
        texts = [b["source_text"] for b in llmwiki.section(self.page, target)]
        self.assertEqual(texts, ["## 현행", "현행 tier 설명. [[beta]]"])

    def test_section_of_a_non_heading_block_is_just_that_block(self) -> None:
        bid = self.page["block_order"][1]
        self.assertEqual([b["id"] for b in llmwiki.section(self.page, bid)], [bid])

    def test_section_rejects_unknown_block(self) -> None:
        with self.assertRaisesRegex(llmwiki.WikiError, "no block"):
            llmwiki.section(self.page, "block:missing")

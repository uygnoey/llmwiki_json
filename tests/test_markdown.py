"""Markdown -> block parsing and page construction."""
from __future__ import annotations

from tests.support import FROZEN_DATE, WorkspaceCase, llmwiki

SAMPLE = """# 제목

첫 문단이다. [[sample-topic]]를 참조한다.

## 표

| 컬럼 | 값 |
| --- | --- |
| tier | 1 |

- 첫 항목
- 둘째 항목
  이어지는 줄

1. 하나
2. 둘

```python
print("hi")
```

> ⚠️ 상충: 문서마다 수치가 다르다.

> ✅ 현행: 최신 소스를 따른다.

> 그냥 인용이다.

---
"""


class ParseBlocksTest(WorkspaceCase):
    def setUp(self) -> None:
        super().setUp()
        self.blocks, self.order = llmwiki.parse_blocks("page:sample", SAMPLE)
        self.kinds = [self.blocks[b]["kind"] for b in self.order]

    def test_block_sequence(self) -> None:
        self.assertEqual(self.kinds, ["heading", "paragraph", "heading", "table", "list", "list",
                                      "code", "conflict", "current", "quote", "thematic_break"])

    def test_every_block_has_a_persistent_id_and_fingerprint(self) -> None:
        for bid in self.order:
            block = self.blocks[bid]
            self.assertEqual(block["id"], bid)
            self.assertTrue(bid.startswith("block:sample:"))
            self.assertEqual(block["fingerprint"],
                             llmwiki.block_fingerprint(block["kind"], block["source_text"]))

    def test_ids_are_not_positional(self) -> None:
        shifted, _ = llmwiki.parse_blocks("page:sample", "새 문단이 앞에 붙었다.\n\n" + SAMPLE)
        original = {self.blocks[b]["source_text"]: b for b in self.order}
        for bid, block in shifted.items():
            if block["source_text"] in original:
                self.assertEqual(bid, original[block["source_text"]])

    def test_repeated_text_gets_distinct_ids(self) -> None:
        blocks, order = llmwiki.parse_blocks("page:dup", "같은 문단\n\n같은 문단\n")
        self.assertEqual(len(order), 2)
        self.assertNotEqual(order[0], order[1])
        self.assertEqual(blocks[order[0]]["fingerprint"], blocks[order[1]]["fingerprint"])

    def test_table_rows_are_split(self) -> None:
        table = next(self.blocks[b] for b in self.order if self.blocks[b]["kind"] == "table")
        self.assertEqual(table["data"]["rows"][0], ["컬럼", "값"])
        self.assertEqual(table["data"]["rows"][2], ["tier", "1"])

    def test_list_items_and_ordering(self) -> None:
        lists = [self.blocks[b] for b in self.order if self.blocks[b]["kind"] == "list"]
        self.assertFalse(lists[0]["data"]["ordered"])
        self.assertEqual(lists[0]["data"]["items"], ["첫 항목", "둘째 항목 이어지는 줄"])
        self.assertTrue(lists[1]["data"]["ordered"])
        self.assertEqual(lists[1]["data"]["items"], ["하나", "둘"])

    def test_code_keeps_language_and_body(self) -> None:
        code = next(self.blocks[b] for b in self.order if self.blocks[b]["kind"] == "code")
        self.assertEqual(code["data"]["language"], "python")
        self.assertEqual(code["data"]["text"], 'print("hi")')

    def test_conflict_and_current_carry_resolution_status(self) -> None:
        by_kind = {self.blocks[b]["kind"]: self.blocks[b] for b in self.order}
        self.assertEqual(by_kind["conflict"]["resolution"]["status"], "unresolved")
        self.assertEqual(by_kind["current"]["resolution"]["status"], "resolved")
        self.assertEqual(by_kind["current"]["resolution"]["decided_at"], FROZEN_DATE)
        self.assertNotIn("resolution", by_kind["quote"])

    def test_wikilink_refs_are_collected(self) -> None:
        paragraph = next(self.blocks[b] for b in self.order if self.blocks[b]["kind"] == "paragraph")
        self.assertEqual(paragraph["refs"], ["sample-topic"])


class FrontmatterTest(WorkspaceCase):
    def test_parses_scalars_and_lists(self) -> None:
        meta, body = llmwiki.split_frontmatter(
            "---\ntype: concept\ntags: [a, b]\nprojects: []\ndraft: true\nnote: ~\n---\n# 본문\n")
        self.assertEqual(meta["type"], "concept")
        self.assertEqual(meta["tags"], ["a", "b"])
        self.assertEqual(meta["projects"], [])
        self.assertIs(meta["draft"], True)
        self.assertIsNone(meta["note"])
        self.assertEqual(body, "# 본문\n")

    def test_body_without_frontmatter_is_untouched(self) -> None:
        meta, body = llmwiki.split_frontmatter("# 본문\n")
        self.assertEqual(meta, {})
        self.assertEqual(body, "# 본문\n")


class PageFromMarkdownTest(WorkspaceCase):
    def test_builds_a_valid_page_with_snapshot_and_links(self) -> None:
        path = self.write_raw("note.md",
                              "---\ntype: concept\ntags: [tier]\n---\n# Sample Topic\n\n[[beta]] 참조.\n")
        page = llmwiki.page_from_markdown(self.ws, path)
        self.assertEqual(page["id"], "page:note")
        self.assertEqual(page["slug"], "note")
        self.assertEqual(page["title"], "Sample Topic")
        self.assertEqual(page["type"], "concept")
        self.assertEqual(page["tags"], ["tier"])
        self.assertEqual(page["created"], FROZEN_DATE)
        self.assertEqual(page["raw_ref"], "raw/note.md")
        self.assertEqual(page["source_snapshot"]["format"], "markdown")
        self.assertEqual(page["links"][0]["target"], "beta")
        self.assertEqual(llmwiki.validate_page(page, llmwiki.SchemaValidator.load(self.ws)), [])

    def test_cli_flags_override_frontmatter(self) -> None:
        path = self.write_raw("note.md", "---\ntype: source\n---\n# 제목\n")
        page = llmwiki.page_from_markdown(self.ws, path, "entity", ["beta"], summary="요약")
        self.assertEqual(page["type"], "entity")
        self.assertEqual(page["projects"], ["beta"])
        self.assertEqual(page["summary"], "요약")

    def test_title_falls_back_to_slug(self) -> None:
        path = self.write_raw("no-heading.md", "제목 없는 본문.\n")
        self.assertEqual(llmwiki.page_from_markdown(self.ws, path)["title"], "no-heading")

    def test_legacy_wiki_metadata_keeps_raw_evidence_and_normalizes_sources(self) -> None:
        path = self.write_raw(
            "legacy.md",
            "---\ntype: concept\nsources: [alpha, user:2026-08-19]\n"
            "raw: \"raw/beta/source.md\"\n---\n# Legacy\n\n[[alpha|표시명]] 첫 문단.\n",
        )
        page = llmwiki.page_from_markdown(self.ws, path)
        self.assertEqual(page["raw_ref"], "raw/beta/source.md")
        self.assertEqual(page["sources"], ["source:alpha", "user:2026-08-19"])
        self.assertEqual(page["summary"], "표시명 첫 문단.")
        self.assertEqual(llmwiki.validate_page(page, llmwiki.SchemaValidator.load(self.ws)), [])

    def test_summary_falls_back_to_first_list(self) -> None:
        path = self.write_raw("list-first.md", "# 목록\n\n- 첫 번째 핵심 사실\n- 다음 사실\n")
        self.assertEqual(llmwiki.page_from_markdown(self.ws, path)["summary"],
                         "첫 번째 핵심 사실 다음 사실")

"""Markdown and HTML rendering, including exact round-trip."""
from __future__ import annotations

from tests.support import WorkspaceCase, llmwiki, make_page

BODY = """# 제목

문단 [[sample-topic|티어]] 참조.

| a | b |
| --- | --- |
| 1 | 2 |

- 하나
- 둘

```sql
SELECT 1;
```

> ⚠️ 상충: 값이 다르다.

---
"""


class MarkdownRenderTest(WorkspaceCase):
    def test_renders_frontmatter_and_blocks(self) -> None:
        page = make_page("t", BODY, type="concept", tags=["a", "b"], projects=["beta"],
                         sources=["user:2026-08-19"])
        text = llmwiki.render_markdown(page)
        self.assertTrue(text.startswith("---\ntype: concept\n"))
        self.assertIn("tags: [a, b]", text)
        self.assertIn("projects: [beta]", text)
        self.assertIn("sources: [user:2026-08-19]", text)
        self.assertIn("# 제목", text)
        self.assertTrue(text.endswith("\n"))

    def test_exact_replays_the_ingested_snapshot(self) -> None:
        raw = "---\ntype: source\n---\n# 제목\n\n\n들쭉날쭉한   원문\n"
        page = make_page("t", raw, snapshot=raw)
        self.assertEqual(llmwiki.render_markdown(page, exact=True), raw)

    def test_exact_without_snapshot_is_an_error(self) -> None:
        with self.assertRaisesRegex(llmwiki.WikiError, "no markdown snapshot"):
            llmwiki.render_markdown(make_page("t", BODY), exact=True)

    def test_rendered_markdown_reparses_to_the_same_blocks(self) -> None:
        page = make_page("t", BODY, projects=["beta"])
        rendered = llmwiki.render_markdown(page)
        _, body = llmwiki.split_frontmatter(rendered)
        blocks, order = llmwiki.parse_blocks("page:t", body)
        self.assertEqual([blocks[b]["kind"] for b in order],
                         [page["blocks"][b]["kind"] for b in page["block_order"]])
        self.assertEqual([blocks[b]["source_text"] for b in order],
                         [page["blocks"][b]["source_text"] for b in page["block_order"]])


class HtmlRenderTest(WorkspaceCase):
    def setUp(self) -> None:
        super().setUp()
        self.page = make_page("t", BODY, projects=["beta"])
        self.html = llmwiki.render_html(self.page)

    def test_wraps_the_page_with_its_id(self) -> None:
        self.assertTrue(self.html.startswith('<article data-page-id="page:t">'))
        self.assertTrue(self.html.endswith("</article>"))

    def test_headings_tables_lists_and_code(self) -> None:
        self.assertIn("<h1>제목</h1>", self.html)
        self.assertIn("<table><tr><th>a</th><th>b</th></tr><tr><td>1</td><td>2</td></tr></table>",
                      self.html)
        self.assertIn("<ul><li>하나</li><li>둘</li></ul>", self.html)
        self.assertIn('<pre><code data-language="sql">SELECT 1;</code></pre>', self.html)
        self.assertIn("<hr>", self.html)

    def test_table_separator_row_is_dropped(self) -> None:
        self.assertNotIn("---", self.html)

    def test_conflict_blockquote_keeps_its_class(self) -> None:
        self.assertIn('<blockquote class="conflict">', self.html)

    def test_wikilinks_become_navigable_anchors(self) -> None:
        self.assertIn('<a href="#" data-wiki-target="sample-topic">티어</a>', self.html)

    def test_escapes_untrusted_text(self) -> None:
        page = make_page("x", '<script>alert("x")</script> & more\n')
        rendered = llmwiki.render_html(page)
        self.assertNotIn("<script>", rendered)
        self.assertIn("&lt;script&gt;", rendered)
        self.assertIn("&amp;", rendered)

    def test_paragraph_newlines_become_breaks(self) -> None:
        page = make_page("x", "첫 줄\n둘째 줄\n")
        self.assertIn("첫 줄<br>둘째 줄", llmwiki.render_html(page))

    def test_unknown_block_kind_degrades_to_preformatted(self) -> None:
        block = {"id": "block:x:1", "kind": "raw", "data": {}, "refs": [],
                 "source_text": "원문 <b>", "fingerprint": "f"}
        self.assertIn("&lt;b&gt;", llmwiki.render_block_html(block))

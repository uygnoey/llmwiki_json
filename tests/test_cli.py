"""End-to-end CLI behaviour: exit codes, JSON output, workspace isolation."""
from __future__ import annotations

import json

from tests.support import REPO, WorkspaceCase, llmwiki, make_page


class CliTest(WorkspaceCase):
    def setUp(self) -> None:
        super().setUp()
        self.write_pages([
            make_page("alpha", "# Alpha\n\n첫 문단 [[beta]].\n\n## 상세\n\n상세 내용.\n",
                      projects=["beta"], tags=["검색"]),
            make_page("beta", "# Beta\n\n[[alpha]]\n\n> ⚠️ 상충: 값이 다르다.\n", projects=["beta"]),
        ])

    def test_build_prints_stats(self) -> None:
        stats = json.loads(self.cli("build").stdout)
        self.assertEqual(stats["pages"], 2)
        self.assertEqual(stats["edges"], 2)
        self.assertTrue((self.root / "index" / "graph.json").exists())

    def test_validate_exits_zero_on_a_clean_wiki(self) -> None:
        self.assertIn("0 errors", self.cli("validate").stdout)

    def test_lint_exits_one_when_errors_exist(self) -> None:
        self.write_pages([make_page("gamma", "# Gamma\n\n[[nowhere]]\n")], name="gamma.json")
        proc = self.cli("lint", expect=1)
        self.assertIn("ERROR page:gamma: missing link target [[nowhere]]", proc.stdout)

    def test_lint_json_output(self) -> None:
        self.cli("build")
        payload = json.loads(self.cli("lint", "--json").stdout)
        self.assertEqual(payload["errors"], [])
        self.assertTrue(any("unresolved conflict" in w for w in payload["warnings"]))

    def test_get_page_block_and_pointer(self) -> None:
        page = json.loads(self.cli("get", "alpha").stdout)
        self.assertEqual(page["id"], "page:alpha")

        bid = page["block_order"][1]
        block = json.loads(self.cli("get", f"alpha#{bid}").stdout)
        self.assertEqual(block["id"], bid)
        self.assertEqual(json.loads(self.cli("get", "alpha", "--block", bid).stdout)["id"], bid)
        self.assertEqual(json.loads(self.cli("get", bid).stdout)["id"], bid)

        self.assertEqual(json.loads(self.cli("get", "alpha", "--field", "title").stdout), "Alpha")
        self.assertEqual(
            json.loads(self.cli("get", "alpha", "--pointer", f"/blocks/{bid}/kind").stdout),
            "paragraph")

    def test_get_unknown_page_exits_two(self) -> None:
        proc = self.cli("get", "nope", expect=2)
        self.assertIn("page not found", proc.stderr)

    def test_outline_lists_sections(self) -> None:
        rows = json.loads(self.cli("outline", "alpha").stdout)
        self.assertEqual([r["text"] for r in rows], ["Alpha", "상세"])

    def test_render_markdown_html_json_and_section(self) -> None:
        md = self.cli("render", "alpha").stdout
        self.assertIn("# Alpha", md)
        self.assertIn("type: concept", md)

        html = self.cli("render", "alpha", "--format", "html").stdout
        self.assertIn('<article data-page-id="page:alpha">', html)
        self.assertIn('data-wiki-target="beta"', html)

        self.assertEqual(json.loads(self.cli("render", "alpha", "--format", "json").stdout)["id"],
                         "page:alpha")

        heading = next(r for r in json.loads(self.cli("outline", "alpha").stdout)
                       if r["text"] == "상세")
        section = self.cli("render", "alpha", "--section", heading["block_id"]).stdout
        self.assertEqual(section.strip(), "## 상세\n\n상세 내용.")

    def test_query_ranks_by_title_then_body(self) -> None:
        rows = [json.loads(line) for line in self.cli("query", "Alpha").stdout.splitlines()]
        self.assertEqual(rows[0]["id"], "page:alpha")
        self.assertGreater(rows[0]["score"], rows[-1]["score"])

    def test_query_works_without_a_built_index_and_with_a_stale_one(self) -> None:
        # 색인이 없으면 정본에서 메모리 색인을 만든다.
        rows = [json.loads(line) for line in self.cli("query", "상세 내용").stdout.splitlines()]
        self.assertEqual(rows[0]["id"], "page:alpha")
        # 색인을 만든 뒤 정본이 바뀌면 낡은 색인이 아니라 정본으로 답한다.
        self.cli("build")
        self.write_pages([make_page("gamma", "# Gamma\n\n새로 들어온 감마 문서 [[alpha]].\n",
                                    projects=["beta"])], name="gamma.json")
        rows = [json.loads(line) for line in self.cli("query", "감마 문서").stdout.splitlines()]
        self.assertEqual(rows[0]["id"], "page:gamma")

    def test_query_respects_limit(self) -> None:
        self.assertEqual(len(self.cli("query", "문단 값", "--limit", "1").stdout.splitlines()), 1)

    def test_ingest_dry_run_then_real(self) -> None:
        source = self.write_raw("note.md", "# Note\n\n[[alpha]]\n")
        dry = json.loads(self.cli("ingest", str(source), "--dry-run").stdout)
        self.assertTrue(dry["dry_run"])
        real = json.loads(self.cli("ingest", str(source), "--type", "source",
                                   "--project", "beta", "--summary", "요약").stdout)
        self.assertEqual(real["page_id"], "page:note")
        page = json.loads((self.root / real["dest"]).read_text(encoding="utf-8"))
        self.assertEqual(page["type"], "source")
        self.assertEqual(page["projects"], ["beta"])

    def test_ingest_refuses_credentials_with_exit_two(self) -> None:
        source = self.write_raw("leak.md", "# Leak\n\npassword: hunter2\n")
        self.assertIn("보안정보", self.cli("ingest", str(source), expect=2).stderr)

    def test_build_writes_the_search_index_and_records_its_digest(self) -> None:
        self.cli("build")
        self.assertTrue((self.root / "index" / "search.sqlite").exists())
        revision = self.read_json("index/revision.json")
        self.assertEqual(len(revision["search_root"]), 64)
        self.assertEqual(len(revision["revision"]), 64)

    def test_export_md_is_gone(self) -> None:
        self.cli("export-md", expect=2)

    def test_log_append_and_show(self) -> None:
        self.cli("log", "--action", "query", "--note", "tier 정의")
        rows = [json.loads(line) for line in self.cli("log", "--show", "5").stdout.splitlines()]
        self.assertEqual(rows[-1]["action"], "query")
        self.assertEqual(rows[-1]["note"], "tier 정의")

    def test_root_flag_overrides_the_environment(self) -> None:
        proc = self.cli("--root", str(self.root), "build")
        self.assertEqual(json.loads(proc.stdout)["pages"], 2)

    def test_unknown_command_is_a_usage_error(self) -> None:
        self.cli("nope", expect=2)


class FixtureCliTest(WorkspaceCase):
    """`--fixtures` must read the demo tree, never the canonical store."""

    def setUp(self) -> None:
        super().setUp()
        self.write_pages([make_page("demo", "# Demo\n")], fixtures=True)
        self.write_pages([make_page("real", "# Real\n")])

    def test_fixtures_flag_selects_the_demo_tree(self) -> None:
        self.assertEqual(json.loads(self.cli("build", "--fixtures").stdout)["pages"], 1)
        catalog = self.read_json("index/catalog.json")
        self.assertEqual([row["id"] for row in catalog], ["page:demo"])
        self.assertEqual(json.loads(self.cli("get", "demo", "--fixtures", "--field", "title").stdout),
                         "Demo")
        self.cli("get", "real", "--fixtures", expect=2)

    def test_default_reads_the_canonical_store(self) -> None:
        catalog = json.loads(self.cli("build").stdout)
        self.assertEqual(catalog["pages"], 1)
        self.assertEqual([row["id"] for row in self.read_json("index/catalog.json")], ["page:real"])


class RepoFixtureTest(WorkspaceCase):
    """The checked-in demo fixtures must stay buildable and lint-clean."""

    def test_repo_fixtures_validate(self) -> None:
        ws = llmwiki.Workspace(REPO, fixtures=True)
        payloads = llmwiki.project(ws)
        self.assertGreaterEqual(payloads["stats.json"]["pages"], 1)
        validator = llmwiki.SchemaValidator.load(ws)
        for page in ws.load_pages():
            self.assertEqual(llmwiki.validate_page(page, validator), [], page["id"])

    def test_repo_fixtures_have_no_missing_links(self) -> None:
        ws = llmwiki.Workspace(REPO, fixtures=True)
        slugs = {p["slug"] for p in ws.load_pages()}
        for page in ws.load_pages():
            for link in page["links"]:
                self.assertIn(link["target"], slugs, f"{page['id']} -> {link['target']}")

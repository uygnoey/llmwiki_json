"""Lint: schema validation, missing links, conflicts, orphans, stale index."""
from __future__ import annotations

import json

from tests.support import WorkspaceCase, llmwiki, make_page


def only(items: list[str], needle: str) -> list[str]:
    return [item for item in items if needle in item]


class ValidatePageTest(WorkspaceCase):
    def setUp(self) -> None:
        super().setUp()
        self.validator = llmwiki.SchemaValidator.load(self.ws)

    def check(self, page: dict) -> list[str]:
        return llmwiki.validate_page(page, self.validator)

    def test_a_well_formed_page_has_no_errors(self) -> None:
        self.assertEqual(self.check(make_page("alpha", "# Alpha\n\n[[beta]]\n")), [])

    def test_missing_required_keys(self) -> None:
        page = make_page("alpha", "# Alpha\n")
        del page["tags"]
        self.assertEqual(only(self.check(page), "missing tags"), ["page:alpha: missing tags"])

    def test_schema_version_type_and_dates(self) -> None:
        page = make_page("alpha", "# Alpha\n")
        page["schema_version"] = "2.0"
        page["type"] = "nope"
        page["created"] = "2026/08/19"
        errors = " ".join(self.check(page))
        self.assertIn("schema_version must be 1.0", errors)
        self.assertIn("invalid type nope", errors)
        self.assertIn("created must be YYYY-MM-DD", errors)

    def test_id_must_track_slug(self) -> None:
        page = make_page("alpha", "# Alpha\n")
        page["slug"] = "beta"
        self.assertTrue(only(self.check(page), "id must be"))

    def test_block_order_must_match_blocks(self) -> None:
        page = make_page("alpha", "# Alpha\n\n문단.\n")
        page["block_order"].append("block:alpha:ghost")
        self.assertTrue(only(self.check(page), "block_order mismatch"))

    def test_block_key_and_kind_are_checked(self) -> None:
        page = make_page("alpha", "# Alpha\n")
        bid = page["block_order"][0]
        page["blocks"][bid]["kind"] = "nope"
        page["blocks"][bid]["id"] = "block:other"
        errors = " ".join(self.check(page))
        self.assertIn("invalid block kind", errors)
        self.assertIn("block key/id mismatch", errors)

    def test_source_ref_formats(self) -> None:
        good = make_page("alpha", "# Alpha\n", sources=["user:2026-08-19", "source:x", "raw:y.md"])
        self.assertEqual(self.check(good), [])
        bad = make_page("alpha", "# Alpha\n", sources=["대화에서 들음"])
        self.assertTrue(only(self.check(bad), "invalid source ref"))

    def test_link_kind_and_block_id_are_checked(self) -> None:
        page = make_page("alpha", "# Alpha\n\n[[beta]]\n")
        page["links"][0]["kind"] = "nope"
        page["links"][0]["block_id"] = "block:ghost"
        errors = " ".join(self.check(page))
        self.assertIn("invalid kind", errors)
        self.assertIn("unknown block_id", errors)

    def test_unknown_top_level_property_is_rejected_by_the_schema(self) -> None:
        page = make_page("alpha", "# Alpha\n")
        page["extra"] = 1
        self.assertTrue(only(self.check(page), "unexpected property 'extra'"))

    def test_resolution_status_enum(self) -> None:
        page = make_page("alpha", "# Alpha\n\n> ⚠️ 상충: 다르다.\n")
        bid = page["block_order"][-1]
        page["blocks"][bid]["resolution"]["status"] = "maybe"
        self.assertTrue(only(self.check(page), "invalid resolution status"))


class SchemaValidatorTest(WorkspaceCase):
    def test_mirrors_the_schema_file_enums(self) -> None:
        schema = json.loads((self.root / "tools" / "schema" / "page.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(set(schema["properties"]["type"]["enum"]), llmwiki.ALLOWED_TYPES)
        self.assertEqual(set(schema["$defs"]["block"]["properties"]["kind"]["enum"]),
                         llmwiki.ALLOWED_BLOCKS)
        self.assertEqual(set(schema["$defs"]["link"]["properties"]["kind"]["enum"]),
                         llmwiki.ALLOWED_LINK_KINDS)

    def test_reports_type_pattern_and_uniqueness(self) -> None:
        validator = llmwiki.SchemaValidator({"type": "object", "properties": {
            "a": {"type": "string", "pattern": "^x"},
            "b": {"type": "array", "items": {"type": "integer"}, "uniqueItems": True}}})
        errors = validator.validate({"a": "yy", "b": [1, 1, "z"]})
        self.assertTrue(any("does not match" in e for e in errors))
        self.assertTrue(any("expected type integer" in e for e in errors))
        self.assertTrue(any("unique" in e for e in errors))

    def test_resolves_refs(self) -> None:
        validator = llmwiki.SchemaValidator({"$ref": "#/$defs/x",
                                             "$defs": {"x": {"type": "string"}}})
        self.assertEqual(validator.validate("ok"), [])
        self.assertTrue(validator.validate(3))


class LintTest(WorkspaceCase):
    def build_and_lint(self) -> tuple[list[str], list[str]]:
        llmwiki.build(self.ws)
        return llmwiki.lint(self.ws)

    def test_clean_wiki_reports_nothing(self) -> None:
        self.write_pages([make_page("alpha", "# Alpha\n\n[[beta]]\n", projects=["beta"]),
                          make_page("beta", "# Beta\n\n[[alpha]]\n", projects=["beta"])])
        errors, warnings = self.build_and_lint()
        self.assertEqual(errors, [])
        self.assertEqual(warnings, [])

    def test_missing_link_target_is_an_error(self) -> None:
        self.write_pages([make_page("alpha", "# Alpha\n\n[[nowhere]]\n", projects=["beta"])])
        errors, _ = self.build_and_lint()
        self.assertEqual(only(errors, "missing link target"),
                         ["page:alpha: missing link target [[nowhere]]"])

    def test_unresolved_conflict_is_a_warning(self) -> None:
        self.write_pages([make_page("alpha", "# Alpha\n\n[[beta]]\n\n> ⚠️ 상충: 값이 다르다.\n"),
                          make_page("beta", "# Beta\n\n[[alpha]]\n")])
        errors, warnings = self.build_and_lint()
        self.assertEqual(errors, [])
        self.assertEqual(len(only(warnings, "unresolved conflict")), 1)

    def test_resolved_conflict_is_not_warned_about(self) -> None:
        self.write_pages([make_page("alpha", "# Alpha\n\n[[beta]]\n\n> ✅ 현행: 최신을 따른다.\n"),
                          make_page("beta", "# Beta\n\n[[alpha]]\n")])
        _, warnings = self.build_and_lint()
        self.assertEqual(only(warnings, "conflict"), [])

    def test_orphan_pages_are_warned_about(self) -> None:
        self.write_pages([make_page("alpha", "# Alpha\n\n[[beta]]\n"),
                          make_page("beta", "# Beta\n"),
                          make_page("lonely", "# Lonely\n")])
        _, warnings = self.build_and_lint()
        self.assertEqual(only(warnings, "orphan"),
                         ["page:lonely: orphan (no inbound or outbound links)"])

    def test_home_index_and_log_pages_may_be_unlinked(self) -> None:
        self.write_pages([make_page("home", "# Home\n", type="home"),
                          make_page("catalog", "# Catalog\n", type="index"),
                          make_page("history", "# History\n", type="log")])
        _, warnings = self.build_and_lint()
        self.assertEqual(only(warnings, "orphan"), [])

    def test_empty_summary_is_a_warning(self) -> None:
        self.write_pages([make_page("alpha", "# Alpha\n\n[[beta]]\n", summary=""),
                          make_page("beta", "# Beta\n\n[[alpha]]\n")])
        _, warnings = self.build_and_lint()
        self.assertEqual(only(warnings, "empty summary"), ["page:alpha: empty summary"])

    def test_duplicate_slug_is_an_error(self) -> None:
        page = make_page("alpha", "# Alpha\n")
        self.write_pages([page], name="a.json")
        self.write_pages([page], name="b.json")
        errors, _ = llmwiki.lint(self.ws)
        self.assertTrue(only(errors, "duplicate slug"))

    def test_duplicate_block_id_is_an_error(self) -> None:
        alpha = make_page("alpha", "# Alpha\n")
        beta = make_page("beta", "# Beta\n")
        bid = alpha["block_order"][0]
        beta["blocks"][bid] = dict(alpha["blocks"][bid])
        beta["block_order"].append(bid)
        self.write_pages([alpha, beta])
        errors, _ = llmwiki.lint(self.ws)
        self.assertTrue(only(errors, "duplicate block id"))

    def test_dangling_page_source_ref_is_an_error(self) -> None:
        self.write_pages([make_page("alpha", "# Alpha\n\n[[beta]]\n", sources=["page:ghost"]),
                          make_page("beta", "# Beta\n\n[[alpha]]\n")])
        errors, _ = llmwiki.lint(self.ws)
        self.assertEqual(only(errors, "source ref"), ["page:alpha: source ref page:ghost has no page"])

    def test_missing_index_is_reported(self) -> None:
        self.write_pages([make_page("alpha", "# Alpha\n\n[[beta]]\n"),
                          make_page("beta", "# Beta\n\n[[alpha]]\n")])
        _, warnings = llmwiki.lint(self.ws)
        self.assertIn("index/map.json missing — run build", warnings)

    def test_stale_index_is_reported_and_clears_after_rebuild(self) -> None:
        pages = [make_page("alpha", "# Alpha\n\n[[beta]]\n"), make_page("beta", "# Beta\n\n[[alpha]]\n")]
        self.write_pages(pages)
        llmwiki.build(self.ws)
        self.assertEqual(llmwiki.stale_index(self.ws), [])

        pages.append(make_page("gamma", "# Gamma\n\n[[alpha]]\n"))
        self.write_pages(pages)
        self.assertEqual(llmwiki.stale_index(self.ws), ["index/map.json stale — run build"])
        llmwiki.build(self.ws)
        self.assertEqual(llmwiki.stale_index(self.ws), [])

    def test_lint_does_not_write_anything(self) -> None:
        self.write_pages([make_page("alpha", "# Alpha\n\n[[beta]]\n"),
                          make_page("beta", "# Beta\n\n[[alpha]]\n")])
        llmwiki.build(self.ws)
        snapshot = {p: p.read_bytes() for p in sorted(self.root.rglob("*.json"))}
        llmwiki.lint(self.ws)
        self.assertEqual({p: p.read_bytes() for p in sorted(self.root.rglob("*.json"))}, snapshot)

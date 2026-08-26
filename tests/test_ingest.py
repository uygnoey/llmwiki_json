"""Ingest safety: raw immutability, secret refusal, overwrite policy, logging."""
from __future__ import annotations

import json

from tests.support import FROZEN_DATE, WorkspaceCase, llmwiki, make_page

NOTE = "---\ntype: concept\ntags: [tier]\n---\n# Sample Topic\n\n본문 [[beta]].\n"


class IngestTest(WorkspaceCase):
    def test_writes_a_sharded_canonical_page(self) -> None:
        path = self.write_raw("note.md", NOTE)
        result = llmwiki.ingest(self.ws, path)
        self.assertEqual(result["page_id"], "page:note")
        self.assertFalse(result["updated"])
        dest = self.root / result["dest"]
        self.assertTrue(dest.exists())
        self.assertEqual(dest.parent.name, "concepts")
        page = json.loads(dest.read_text(encoding="utf-8"))
        self.assertEqual(page["title"], "Sample Topic")
        self.assertEqual(page["raw_ref"], "raw/note.md")

    def test_raw_source_is_never_modified(self) -> None:
        path = self.write_raw("note.md", NOTE)
        before = path.read_bytes()
        llmwiki.ingest(self.ws, path)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in (self.root / "raw").iterdir()), ["note.md"])

    def test_appends_one_log_line(self) -> None:
        path = self.write_raw("note.md", NOTE)
        llmwiki.ingest(self.ws, path)
        rows = llmwiki.read_log(self.ws)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["action"], "ingest")
        self.assertEqual(rows[0]["page_id"], "page:note")
        self.assertEqual(rows[0]["source"], "raw/note.md")
        self.assertEqual(rows[0]["mode"], "create")
        self.assertTrue(rows[0]["at"].startswith(FROZEN_DATE))

    def test_log_is_append_only(self) -> None:
        llmwiki.ingest(self.ws, self.write_raw("a.md", "# A\n"))
        llmwiki.ingest(self.ws, self.write_raw("b.md", "# B\n"))
        rows = llmwiki.read_log(self.ws, limit=0)
        self.assertEqual([r["page_id"] for r in rows], ["page:a", "page:b"])

    def test_dry_run_writes_nothing(self) -> None:
        path = self.write_raw("note.md", NOTE)
        result = llmwiki.ingest(self.ws, path, dry_run=True)
        self.assertTrue(result["dry_run"])
        self.assertEqual(list((self.root / "wiki").rglob("*.json")), [])
        self.assertEqual(llmwiki.read_log(self.ws), [])

    def test_refuses_to_overwrite_without_update(self) -> None:
        path = self.write_raw("note.md", NOTE)
        llmwiki.ingest(self.ws, path)
        with self.assertRaisesRegex(llmwiki.WikiError, "already exists"):
            llmwiki.ingest(self.ws, path)

    def test_update_preserves_created_and_appends_history(self) -> None:
        path = self.write_raw("note.md", NOTE)
        first = llmwiki.ingest(self.ws, path)
        original = json.loads((self.root / first["dest"]).read_text(encoding="utf-8"))
        original["created"] = "2020-01-01"
        (self.root / first["dest"]).write_text(json.dumps(original, ensure_ascii=False),
                                               encoding="utf-8")

        path.write_text(NOTE + "\n추가된 문단.\n", encoding="utf-8")
        result = llmwiki.ingest(self.ws, path, update=True)
        self.assertTrue(result["updated"])
        page = json.loads((self.root / result["dest"]).read_text(encoding="utf-8"))
        self.assertEqual(page["created"], "2020-01-01")
        self.assertEqual(len(page["history"]), 2)
        self.assertIn("추가된 문단.", page["source_snapshot"]["text"])

    def test_rejects_credentials(self) -> None:
        for secret in ("password: hunter2", "API_KEY=sk-abc123", "access_token: ghp_x",
                       "connection_string=postgres://u:p@h/db"):
            path = self.write_raw("leak.md", f"# 제목\n\n{secret}\n")
            with self.assertRaisesRegex(llmwiki.WikiError, "보안정보"):
                llmwiki.ingest(self.ws, path)
        self.assertEqual(list((self.root / "wiki").rglob("*.json")), [])

    def test_allows_schema_field_names_without_values(self) -> None:
        path = self.write_raw("schema.md", "# 스키마\n\npassword 컬럼이 존재한다. (접속 정보 생략)\n")
        self.assertEqual(llmwiki.ingest(self.ws, path)["page_id"], "page:schema")

    def test_missing_source_is_reported(self) -> None:
        with self.assertRaisesRegex(llmwiki.WikiError, "source not found"):
            llmwiki.ingest(self.ws, self.root / "raw" / "nope.md")

    def test_refuses_to_reingest_from_the_canonical_store(self) -> None:
        self.write_pages([make_page("alpha", "# Alpha\n")], name="alpha.json")
        canonical = next((self.root / "wiki").rglob("*.json"))
        with self.assertRaisesRegex(llmwiki.WikiError, "canonical store"):
            llmwiki.ingest(self.ws, canonical)

    def test_json_source_is_validated_before_it_lands(self) -> None:
        bad = make_page("alpha", "# Alpha\n")
        bad["type"] = "nope"
        path = self.root / "raw" / "alpha.json"
        path.write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8")
        with self.assertRaisesRegex(llmwiki.WikiError, "invalid type"):
            llmwiki.ingest(self.ws, path)
        self.assertEqual(list((self.root / "wiki").rglob("*.json")), [])

    def test_json_source_is_accepted_and_stamped(self) -> None:
        path = self.root / "raw" / "alpha.json"
        path.write_text(json.dumps(make_page("alpha", "# Alpha\n"), ensure_ascii=False),
                        encoding="utf-8")
        result = llmwiki.ingest(self.ws, path, projects=["beta"], summary="요약")
        page = json.loads((self.root / result["dest"]).read_text(encoding="utf-8"))
        self.assertEqual(page["projects"], ["beta"])
        self.assertEqual(page["summary"], "요약")
        self.assertEqual(page["history"][-1]["action"], "ingested")

    def test_json_arrays_are_rejected(self) -> None:
        path = self.root / "raw" / "many.json"
        path.write_text(json.dumps([make_page("alpha", "# Alpha\n")]), encoding="utf-8")
        with self.assertRaisesRegex(llmwiki.WikiError, "single page object"):
            llmwiki.ingest(self.ws, path)

    def test_shard_names_cannot_escape_the_store(self) -> None:
        self.assertEqual(llmwiki.safe_name("page:../../etc/passwd"), "etc-passwd.json")
        self.assertNotIn("/", llmwiki.safe_name("page:a/b"))
        self.assertTrue(llmwiki.safe_name("page:...").endswith(".json"))

    def test_ingested_page_survives_a_full_build(self) -> None:
        llmwiki.ingest(self.ws, self.write_raw("note.md", NOTE))
        llmwiki.ingest(self.ws, self.write_raw("beta.md", "# Beta\n\n[[note]]\n"))
        stats = llmwiki.build(self.ws)
        self.assertEqual(stats["pages"], 2)
        self.assertEqual(stats["edges"], 2)  # note -> beta and beta -> note


class LogCommandTest(WorkspaceCase):
    def test_append_and_read_back(self) -> None:
        llmwiki.append_log(self.ws, {"action": "query", "note": "tier 정의"})
        llmwiki.append_log(self.ws, {"action": "lint", "note": "0 errors"})
        rows = llmwiki.read_log(self.ws, limit=1)
        self.assertEqual(rows, [{"at": llmwiki.timestamp(), "action": "lint", "note": "0 errors"}])

    def test_empty_log_reads_as_empty_list(self) -> None:
        self.assertEqual(llmwiki.read_log(self.ws), [])

    def test_registers_a_new_project_group_during_ingest(self) -> None:
        note = "---\ntype: source\ntags: [신규, 분류]\nprojects: [newlab]\n---\n# 신규 연구\n"
        result = llmwiki.ingest(self.ws, self.write_raw("new.md", note))
        self.assertEqual(result["registered_groups"], ["newlab"])
        page = json.loads((self.root / result["dest"]).read_text(encoding="utf-8"))
        self.assertEqual(page["type"], "source")
        self.assertEqual(page["tags"], ["신규", "분류"])
        self.assertEqual(page["projects"], ["newlab"])
        groups = self.ws.load_groups()
        self.assertEqual(groups["project"]["newlab"]["match"], ["newlab"])
        self.assertEqual(llmwiki.project_group(page["projects"], groups), "newlab")


class ClassificationTest(WorkspaceCase):
    def test_changing_the_type_moves_the_page_to_its_new_directory(self) -> None:
        note = "---\ntype: source\n---\n# 노트\n\n본문.\n"
        first = llmwiki.ingest(self.ws, self.write_raw("note.md", note))
        self.assertEqual(first["dest"], "wiki/sources/note.json")

        second = llmwiki.ingest(self.ws, self.write_raw("note.md", note),
                                page_type="entity", update=True)
        self.assertEqual(second["dest"], "wiki/entities/note.json")
        self.assertEqual(second["moved_from"], "wiki/sources/note.json")
        self.assertFalse((self.root / "wiki" / "sources" / "note.json").exists())
        page = json.loads((self.root / second["dest"]).read_text(encoding="utf-8"))
        self.assertEqual(page["type"], "entity")
        self.assertEqual(len(list((self.root / "wiki").rglob("note.json"))), 1)

    def test_refuses_to_overwrite_a_different_page_with_the_same_filename(self) -> None:
        other = make_page("other", "# 다른 페이지\n")
        target = self.root / "wiki" / "concepts" / "note.json"
        target.write_text(json.dumps(other, ensure_ascii=False), encoding="utf-8")
        with self.assertRaisesRegex(llmwiki.WikiError, "would overwrite a different page"):
            llmwiki.ingest(self.ws, self.write_raw("note.md", "---\ntype: concept\n---\n# 노트\n"),
                           update=True)
        self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["id"], "page:other")

    def test_refuses_to_ingest_over_a_multi_page_file(self) -> None:
        self.write_pages([make_page("note", "# 노트\n"), make_page("other", "# 다른 노트\n")],
                         name="note.json")
        with self.assertRaisesRegex(llmwiki.WikiError, "several pages in one file"):
            llmwiki.ingest(self.ws, self.write_raw("note.md", "---\ntype: concept\n---\n# 노트\n"),
                           update=True)

    def test_json_page_metadata_is_normalised(self) -> None:
        page = make_page("json-note", "# JSON 노트\n\n본문.\n", type="entity")
        page["tags"] = ["a", " a ", "b"]
        page["projects"] = ["OSE", "OSE"]
        path = self.root / "raw" / "page.json"
        path.write_text(json.dumps(page, ensure_ascii=False), encoding="utf-8")
        result = llmwiki.ingest(self.ws, path)
        stored = json.loads((self.root / result["dest"]).read_text(encoding="utf-8"))
        self.assertEqual(stored["tags"], ["a", "b"])
        self.assertEqual(stored["projects"], ["OSE"])
        self.assertEqual(result["registered_groups"], ["ose"])

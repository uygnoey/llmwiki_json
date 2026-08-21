"""Shared fixtures for the llmwiki_json backend tests.

Every test runs against a throwaway workspace so `wiki/` and
`wiki/log.jsonl` in the real repo are never written to.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Iterable

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "llmwiki.py"
CONTEXT_SCRIPT = REPO / "scripts" / "llmwiki_context.py"


def _load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules.setdefault(name, module)
    spec.loader.exec_module(module)
    return module


llmwiki = _load("llmwiki", SCRIPT)
llmwiki_context = _load("llmwiki_context", CONTEXT_SCRIPT)

FROZEN_NOW = "2026-08-19T00:00:00+00:00"
FROZEN_DATE = "2026-08-19"


def make_page(slug: str, body: str, *, type: str = "concept", projects: Iterable[str] = (),
              tags: Iterable[str] = (), sources: Iterable[str] = (), summary: str = "요약",
              created: str = FROZEN_DATE, snapshot: str | None = None) -> dict[str, Any]:
    """Build a schema-valid page from Markdown body text."""
    page_id = f"page:{slug}"
    blocks, order = llmwiki.parse_blocks(page_id, body)
    title = next((b["data"]["text"] for b in blocks.values()
                  if b["kind"] == "heading" and b["data"].get("level") == 1), slug)
    page = {
        "schema_version": "1.0", "id": page_id, "slug": slug, "title": title, "type": type,
        "created": created, "updated": created, "tags": list(tags), "projects": list(projects),
        "sources": list(sources), "raw_ref": None, "summary": summary,
        "blocks": blocks, "block_order": order,
        "links": llmwiki.page_links(page_id, blocks, order),
        "history": [{"at": created, "action": "created", "actor": "test"}],
    }
    if snapshot is not None:
        page["source_snapshot"] = {"format": "markdown", "text": snapshot,
                                   "sha256": llmwiki.sha(snapshot)}
    return page


DEMO_BODY = {
    "alpha": "# Alpha\n\n[[beta]]를 참조한다.\n",
    "beta": "# Beta\n\n[[alpha]]로 되돌아온다.\n\n> ⚠️ 상충: 두 문서의 수치가 다르다.\n",
    "gamma": "# Gamma\n\n아무도 링크하지 않는 고아 페이지.\n",
}


class WorkspaceCase(unittest.TestCase):
    """Base case providing an isolated workspace rooted in a temp dir."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="llmwiki-test-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        for rel in ("tools/config/groups.json", "tools/schema/page.schema.json"):
            dest = self.root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO / rel, dest)
        for rel in ("raw", "wiki/sources", "wiki/entities", "wiki/concepts",
                    "wiki/syntheses", "wiki/projects", "tests/fixtures/pages", "index",
                    "viewer/public/data"):
            (self.root / rel).mkdir(parents=True, exist_ok=True)
        self._env = dict(os.environ)
        os.environ[llmwiki.ENV_NOW] = FROZEN_NOW
        self.addCleanup(self._restore_env)
        self.ws = llmwiki.Workspace(self.root)

    def _restore_env(self) -> None:
        os.environ.clear()
        os.environ.update(self._env)

    # ---------------------------------------------------------------- helpers
    def write_pages(self, pages: Iterable[dict[str, Any]], *, name: str = "pages.json",
                    fixtures: bool = False) -> Path:
        pages = list(pages)
        if fixtures:
            target = self.root / "tests" / "fixtures" / "pages" / name
        else:
            folder = llmwiki.PAGE_DIRS.get(pages[0]["type"], "concepts") if pages else "concepts"
            target = self.root / "wiki" / folder / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(pages, ensure_ascii=False, indent=2), encoding="utf-8")
        return target

    def write_demo(self, **overrides: str) -> list[dict[str, Any]]:
        bodies = {**DEMO_BODY, **overrides}
        pages = [make_page(slug, body, projects=["공통"] if slug == "gamma" else ["beta"])
                 for slug, body in bodies.items()]
        self.write_pages(pages)
        return pages

    def write_raw(self, name: str, text: str) -> Path:
        path = self.root / "raw" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def read_json(self, rel: str) -> Any:
        return json.loads((self.root / rel).read_text(encoding="utf-8"))

    def cli(self, *argv: str, expect: int = 0) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, llmwiki.ENV_ROOT: str(self.root), llmwiki.ENV_NOW: FROZEN_NOW}
        proc = subprocess.run([sys.executable, str(SCRIPT), *argv], capture_output=True,
                              text=True, env=env)
        self.assertEqual(proc.returncode, expect,
                         f"argv={argv}\nstdout={proc.stdout}\nstderr={proc.stderr}")
        return proc

    def context_cli(self, *argv: str, stdin: str = "", cwd: Path | None = None,
                    expect: int = 0, env: dict[str, str] | None = None,
                    ) -> subprocess.CompletedProcess[str]:
        """Run scripts/llmwiki_context.py against the throwaway workspace."""
        environ = {**os.environ, llmwiki.ENV_ROOT: str(self.root)}
        environ.update(env or {})
        proc = subprocess.run([sys.executable, str(CONTEXT_SCRIPT), *argv], input=stdin,
                              capture_output=True, text=True, env=environ,
                              cwd=str(cwd) if cwd else None)
        self.assertEqual(proc.returncode, expect,
                         f"argv={argv}\nstdout={proc.stdout}\nstderr={proc.stderr}")
        return proc

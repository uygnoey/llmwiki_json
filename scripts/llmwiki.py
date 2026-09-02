#!/usr/bin/env python3
"""llmwiki_json CLI.

JSON canonical knowledge wiki: ingest, deterministic projection (build),
direct page/block get, md/html render, lint and append-only log.

Determinism contract
--------------------
`build` is a pure function of (wiki pages, tools/config/groups.json).
Running it twice on the same input produces byte-identical artifacts:
every collection is emitted in a stable sort order, every path stored in
an artifact is repo-relative, and layout coordinates are rounded. The
search index (`index/search.sqlite`, built by `llmwiki_index`) is published
by rewriting a fresh file in fixed DDL/PK order, so its bytes are deterministic too; `revision.json`
additionally records `search_root`, the sha256 of the published index file.

`build --changed PATH...` is incremental: only the hinted files are re-hashed,
the search index gets those pages swapped in place (`index/search.work.sqlite`
is the mutable work copy, WAL) and the JSON artifacts are projected from the
published index. An mtime scan verifies the hint; any change outside it falls
back to a full build. Incremental and full builds produce the same bytes.

Test hooks
----------
LLMWIKI_ROOT  repository root override (tests point this at a temp dir)
LLMWIKI_NOW   frozen clock, `YYYY-MM-DD` or a full ISO-8601 timestamp
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import json
import math
import os
import re
import shutil
import sqlite3
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

try:  # package import (bench: `from scripts import llmwiki`)
    from . import llmwiki_index as search_index
except ImportError:  # run as a script or loaded from its file path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import llmwiki_index as search_index  # type: ignore[no-redef]

DEFAULT_ROOT = Path(__file__).resolve().parents[1]
ENV_ROOT = "LLMWIKI_ROOT"
ENV_NOW = "LLMWIKI_NOW"
SEARCH_INDEX = search_index.DB_NAME

ALLOWED_TYPES = {"source", "entity", "concept", "synthesis", "project", "home", "index", "log"}
ALLOWED_BLOCKS = {"heading", "paragraph", "list", "table", "quote", "conflict", "current",
                  "code", "thematic_break", "markdown", "raw"}
ALLOWED_LINK_KINDS = {"wiki", "source", "supersedes", "related"}
UNLINKED_TYPES = {"home", "index", "log"}
PAGE_DIRS = {
    "source": "sources", "entity": "entities", "concept": "concepts",
    "synthesis": "syntheses", "project": "projects",
    "home": "", "index": "", "log": "",
}

WIKILINK = re.compile(r"\[\[([^\]|#]+)(?:#([^\]|]+))?(?:\|([^\]]+))?\]\]")
# YAML subset used by frontmatter: `key: value`, `- item`, `[a, "b, c"]`.
META_KEY = re.compile(r"^([^\s:#][^:]*):[ \t]*(.*)$")
BLOCK_ITEM = re.compile(r"^[ \t]+-[ \t]*(.*)$")
FLOW_ITEM = re.compile(r"""[ \t]*(?:"([^"]*)"|'([^']*)'|([^,]*))[ \t]*(?:,|$)""")
# Loose link key: `[[Alpha Platform]]`, `[[alpha_platform]]` and `[[page:alpha-platform]]`
# all have to find the page whose slug is `alpha-platform`.
LINK_SEPARATOR = re.compile(r"[\s_]+")
SECRET = re.compile(r"(?i)(api[_-]?key|access[_-]?token|password|passwd|secret|connection[_-]?string)"
                    r"\s*[:=]\s*[^\s,;\"']+")
SOURCE_REF = re.compile(r"^(?:page|source|raw):\S+$|^user:\d{4}-\d{2}-\d{2}$")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
BLOCK_KEEP = re.compile(r"[^a-zA-Z0-9가-힣._-]+")

CONFLICT_MARK = "⚠️ 상충"
CURRENT_MARK = "✅ 현행"

# Deterministic radial layout. Project groups occupy adjacent angular sectors,
# while every sector fills the same disk so the complete graph stays circular.
RESERVED_PROJECT_GROUPS = {"multi", "ungrouped"}
AUTO_PROJECT_COLORS = (
    "#65e79c", "#ff8dca", "#ffa56d", "#56e1f2", "#91a7ff", "#c69cff",
)
GOLDEN_FRACTION = 0.6180339887498949
LAYOUT_RADIUS = 18.0


# --------------------------------------------------------------------------- utils
def norm(value: Any) -> str:
    return unicodedata.normalize("NFC", str(value or "")).strip()


def sha(text: str, length: int = 64) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def today() -> str:
    override = os.environ.get(ENV_NOW)
    return override[:10] if override else dt.date.today().isoformat()


def timestamp() -> str:
    override = os.environ.get(ENV_NOW)
    if not override:
        return dt.datetime.now(dt.timezone.utc).isoformat()
    return override if "T" in override else f"{override[:10]}T00:00:00+00:00"


def dump(path: Path, data: Any, *, pretty: bool = False) -> None:
    """Atomic, newline-terminated, deterministic JSON write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=2 if pretty else None,
                      separators=None if pretty else (",", ":")) + "\n"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def safe_name(page_id: str) -> str:
    """Filesystem-safe shard name; never escapes its directory."""
    stem = BLOCK_KEEP.sub("-", page_id.removeprefix("page:")).strip("-. ") or sha(page_id, 12)
    return stem + ".json"


class WikiError(Exception):
    """User-facing failure; the CLI turns this into exit code 2."""


# --------------------------------------------------------------------------- workspace
class Workspace:
    """Resolves every path the CLI touches, so tests can relocate the tree."""

    def __init__(self, root: str | os.PathLike[str] | None = None, fixtures: bool = False) -> None:
        self.root = Path(root or os.environ.get(ENV_ROOT) or DEFAULT_ROOT).resolve()
        self.fixtures = bool(fixtures)

    @property
    def knowledge(self) -> Path:
        return self.root / "wiki"

    @property
    def fixtures_dir(self) -> Path:
        return self.root / "tests" / "fixtures" / "pages"

    @property
    def pages_dir(self) -> Path:
        return self.fixtures_dir if self.fixtures else self.knowledge

    @property
    def raw(self) -> Path:
        return self.root / "raw"

    @property
    def index(self) -> Path:
        return self.root / "index"

    @property
    def search_db(self) -> Path:
        return self.index / SEARCH_INDEX

    @property
    def work_db(self) -> Path:
        return self.index / search_index.WORK_NAME

    @property
    def state_path(self) -> Path:
        return self.index / search_index.STATE_NAME

    @property
    def public(self) -> Path:
        return self.root / "viewer" / "public" / "data"

    @property
    def groups_path(self) -> Path:
        return self.root / "tools" / "config" / "groups.json"

    @property
    def schema_path(self) -> Path:
        return self.root / "tools" / "schema" / "page.schema.json"

    @property
    def log_path(self) -> Path:
        return self.root / "wiki" / "log.jsonl"

    def rel(self, path: Path) -> str:
        """Repo-relative POSIX path when possible — artifacts must not embed $HOME."""
        resolved = Path(path).resolve()
        try:
            return resolved.relative_to(self.root).as_posix()
        except ValueError:
            return resolved.as_posix()

    def load_documents(self) -> list[tuple[str, dict[str, Any]]]:
        """(relative shard path, page) pairs, sorted by page id."""
        docs: list[tuple[str, dict[str, Any]]] = []
        source = self.pages_dir
        if not source.exists():
            return docs
        for path in sorted(source.rglob("*.json")):
            if path.name.startswith("."):
                continue
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise WikiError(f"{self.rel(path)}: invalid JSON ({exc})") from exc
            for page in (value if isinstance(value, list) else [value]):
                docs.append((self.rel(path), page))
        docs.sort(key=lambda item: (str(item[1].get("id", "")), item[0]))
        return docs

    def load_pages(self) -> list[dict[str, Any]]:
        return [page for _, page in self.load_documents()]

    def load_groups(self) -> dict[str, Any]:
        if not self.groups_path.exists():
            raise WikiError(f"missing {self.rel(self.groups_path)}")
        return json.loads(self.groups_path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- markdown parsing
def refs_in(text: str) -> list[str]:
    return sorted({norm(m.group(1)) for m in WIKILINK.finditer(text)})


def unquote(value: str) -> str:
    """Strip one matching pair of surrounding quotes — `"a, b"` stays one value."""
    value = value.strip()
    for quote in ('"', "'"):
        if len(value) >= 2 and value.startswith(quote) and value.endswith(quote):
            return value[1:-1].strip()
    return value


def split_flow(value: str) -> list[str]:
    """`a, "b, c", 'd'` -> ['a', 'b, c', 'd'] — commas inside quotes do not split."""
    items: list[str] = []
    position = 0
    while position < len(value):
        match = FLOW_ITEM.match(value, position)
        if not match or match.end() == position:
            break
        items.append(norm(next(g for g in match.groups() if g is not None)))
        position = match.end()
    return [item for item in items if item]


def parse_scalar(value: str) -> Any:
    value = value.strip()
    if value.startswith("[") and value.endswith("]"):
        return split_flow(value[1:-1])
    if value in {"null", "~", ""}:
        return None
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    return unquote(value)


def string_list(value: Any) -> list[str]:
    """Coerce a frontmatter value to a clean, de-duplicated list of strings.

    `tags: [a, b]`, a block sequence, a bare `tags: a` and `tags: a, b` all
    have to land on the same shape: the schema demands an array of unique
    strings, and a page whose tags silently arrived as `None` is a page the
    graph cannot colour or filter.
    """
    if value is None or value is False or value == "":
        return []
    if isinstance(value, str):
        items: list[Any] = split_flow(value) if "," in value else [value]
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        items = [value]
    return list(dict.fromkeys(text for text in (norm(item) for item in items) if text))


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Parse the YAML subset real notes use: scalars, flow lists, block sequences.

    A block sequence (`tags:` then indented `- value` lines) is what most
    editors and humans write. Dropping it silently used to leave `tags`,
    `projects` and `sources` empty, so pages arrived unclassified.
    """
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}, text
    meta: dict[str, Any] = {}
    key: str | None = None
    for line in text[4:end].splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        item = BLOCK_ITEM.match(line)
        if item and key is not None:
            if not isinstance(meta.get(key), list):
                meta[key] = []
            value = unquote(norm(item.group(1)))
            if value:
                meta[key].append(value)
            continue
        entry = META_KEY.match(line)
        if not entry:
            continue
        key = norm(entry.group(1))
        meta[key] = parse_scalar(entry.group(2))
    return meta, text[end + 5:]


def block_fingerprint(kind: str, raw: str) -> str:
    return sha(kind + "\0" + re.sub(r"\s+", " ", raw).strip(), 16)


def make_block(page_id: str, kind: str, raw: str, data: dict[str, Any], occurrence: int,
               resolution: dict[str, Any] | None = None) -> dict[str, Any]:
    """Content-addressed block id: stable across edits elsewhere on the page."""
    fingerprint = block_fingerprint(kind, raw)
    block: dict[str, Any] = {
        "id": f"block:{page_id.removeprefix('page:')}:{fingerprint}:{occurrence}",
        "kind": kind, "data": data, "refs": refs_in(raw),
        "source_text": raw, "fingerprint": fingerprint,
    }
    if resolution:
        block["resolution"] = resolution
    return block


def list_items(raw_lines: list[str]) -> list[str]:
    items: list[str] = []
    for line in raw_lines:
        marker = re.match(r"^\s*(?:[-*+]|\d+\.)\s+(.*)$", line)
        if marker:
            items.append(marker.group(1).strip())
        elif items and line.strip():
            items[-1] = f"{items[-1]} {line.strip()}"
    return items


def parse_blocks(page_id: str, body: str) -> tuple[dict[str, Any], list[str]]:
    lines = body.splitlines()
    blocks: dict[str, Any] = {}
    order: list[str] = []
    occurrences: Counter[tuple[str, str]] = Counter()

    def add(kind: str, raw_lines: list[str], data: dict[str, Any],
            resolution: dict[str, Any] | None = None) -> None:
        raw = "\n".join(raw_lines)
        key = (kind, block_fingerprint(kind, raw))
        occurrences[key] += 1
        block = make_block(page_id, kind, raw, data, occurrences[key], resolution)
        blocks[block["id"]] = block
        order.append(block["id"])

    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue

        if line.startswith("```"):
            raw = [line]
            language = line[3:].strip()
            i += 1
            while i < len(lines):
                raw.append(lines[i])
                closed = lines[i].startswith("```")
                i += 1
                if closed:
                    break
            inner = raw[1:-1] if len(raw) > 1 and raw[-1].startswith("```") else raw[1:]
            add("code", raw, {"language": language, "text": "\n".join(inner)})
            continue

        heading = re.match(r"^(#{1,6})\s+(.+)$", line)
        if heading:
            add("heading", [line], {"level": len(heading.group(1)), "text": heading.group(2).strip()})
            i += 1
            continue

        if re.fullmatch(r"\s*(?:-{3,}|\*{3,}|_{3,})\s*", line):
            add("thematic_break", [line], {})
            i += 1
            continue

        if line.lstrip().startswith(">"):
            raw = []
            while i < len(lines) and lines[i].lstrip().startswith(">"):
                raw.append(lines[i])
                i += 1
            text = "\n".join(re.sub(r"^\s*>\s?", "", x) for x in raw)
            kind = "conflict" if CONFLICT_MARK in text else "current" if CURRENT_MARK in text else "quote"
            resolution = None
            if kind == "conflict":
                resolution = {"status": "unresolved"}
            elif kind == "current":
                resolution = {"status": "resolved", "decided_at": today()}
            add(kind, raw, {"text": text}, resolution)
            continue

        if line.startswith("|"):
            raw = []
            while i < len(lines) and lines[i].startswith("|"):
                raw.append(lines[i])
                i += 1
            rows = [[c.strip().replace("\\|", "|") for c in r.strip().strip("|").split("|")] for r in raw]
            add("table", raw, {"rows": rows})
            continue

        if re.match(r"^\s*(?:[-*+] |\d+\. )", line):
            raw = []
            while i < len(lines) and (re.match(r"^\s*(?:[-*+] |\d+\. )", lines[i])
                                      or (lines[i].startswith("  ") and lines[i].strip())):
                raw.append(lines[i])
                i += 1
            add("list", raw, {"ordered": bool(re.match(r"^\s*\d+\. ", raw[0])),
                              "items": list_items(raw), "text": "\n".join(raw)})
            continue

        raw = [line]
        i += 1
        while i < len(lines) and lines[i].strip() and not re.match(
                r"^(#{1,6})\s|^```|^\||^\s*>|^\s*(?:[-*+] |\d+\. )", lines[i]):
            raw.append(lines[i])
            i += 1
        add("paragraph", raw, {"text": "\n".join(raw)})
    return blocks, order


def page_links(page_id: str, blocks: dict[str, Any], order: list[str]) -> list[dict[str, Any]]:
    links: list[dict[str, Any]] = []
    for bid in order:
        for match in WIKILINK.finditer(blocks[bid]["source_text"]):
            links.append({"target": norm(match.group(1)),
                          "label": norm(match.group(3)) or norm(match.group(1)),
                          "anchor": norm(match.group(2)), "kind": "wiki", "block_id": bid})
    return links


def strip_ref_prefix(ref: str) -> tuple[str, str]:
    """`source:handbook` -> ('source', 'handbook'); a bare value keeps an empty prefix."""
    prefix, separator, rest = norm(ref).partition(":")
    if not separator or not norm(rest):
        return "", norm(ref)
    return prefix.lower(), norm(rest)


def reference_links(sources: Iterable[str] = (), supersedes: Iterable[str] = (),
                    related: Iterable[str] = ()) -> list[dict[str, Any]]:
    """Frontmatter relations become links so evidence is an edge, not just a string.

    `sources: [page:x]` is how a claim points at what backs it. Until these
    became links the graph only knew about `[[wikilinks]]`, so an ingested
    page could sit there with no line to the source it was built from.
    """
    links: list[dict[str, Any]] = []
    for kind, refs in (("source", sources), ("supersedes", supersedes), ("related", related)):
        for ref in refs:
            prefix, target = strip_ref_prefix(ref)
            # user:2026-08-19 and raw:notes.md name evidence outside the wiki.
            if prefix in {"user", "raw"} or not target:
                continue
            links.append({"target": target, "label": target, "kind": kind})
    return links


def link_key(value: str) -> str:
    """Comparison key that ignores case, spacing and the `page:` prefix."""
    text = norm(value).removeprefix("page:").casefold()
    return LINK_SEPARATOR.sub("-", text).strip("-")


def page_lookup(pages: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Resolve a link target by slug, id or title — slugs always win a tie."""
    pages = list(pages)
    lookup: dict[str, dict[str, Any]] = {}
    for page in pages:
        for value in (page.get("slug", ""), page.get("id", "")):
            key = link_key(value)
            if key:
                lookup.setdefault(key, page)
    for page in pages:
        key = link_key(page.get("title", ""))
        if key:
            lookup.setdefault(key, page)
    return lookup


def implied_links(page: dict[str, Any]) -> list[dict[str, Any]]:
    """Declared links plus everything the page's own content already implies.

    Pages written straight to JSON often carry `[[wikilinks]]` in block text
    without repeating them in `links`. The graph must not lose those edges
    just because the array was hand-maintained.
    """
    links = [dict(link) for link in page.get("links", []) if norm(link.get("target"))]
    seen = {(link_key(link["target"]), link.get("kind", "wiki")) for link in links}

    def add(target: str, kind: str, block_id: str | None = None) -> None:
        key = (link_key(target), kind)
        if not key[0] or key in seen:
            return
        seen.add(key)
        link = {"target": norm(target), "label": norm(target), "kind": kind}
        if block_id:
            link["block_id"] = block_id
        links.append(link)

    blocks = page.get("blocks") or {}
    for bid in page.get("block_order", []):
        block = blocks.get(bid) or {}
        refs = block.get("refs")
        if refs is None:
            refs = refs_in(str(block.get("source_text", "")))
        for ref in refs:
            add(ref, "wiki", bid)
    for link in reference_links(page.get("sources", [])):
        add(link["target"], link["kind"])
    return links


def summary_from_blocks(blocks: dict[str, Any], order: list[str]) -> str:
    """Return a compact searchable lead even when a page starts with a list/quote."""
    for bid in order:
        block = blocks[bid]
        if block["kind"] in {"heading", "thematic_break", "code"}:
            continue
        text = norm(block["data"].get("text") or block["source_text"])
        text = WIKILINK.sub(lambda match: norm(match.group(3)) or norm(match.group(1)), text)
        text = re.sub(r"(?m)^\s*(?:[-*+]|\d+\.)\s+", "", text)
        text = re.sub(r"[*_`>#|]", "", text)
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            return text[:280]
    return ""


def page_from_markdown(ws: Workspace, path: Path, page_type: str | None = None,
                       projects: list[str] | None = None, summary: str = "") -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    assert_no_secrets(text, ws.rel(path))
    meta, body = split_frontmatter(text)
    slug = norm(path.stem).replace(" ", "-").lower()
    page_id = f"page:{slug}"
    blocks, order = parse_blocks(page_id, body)
    title = next((b["data"]["text"] for b in blocks.values()
                  if b["kind"] == "heading" and b["data"].get("level") == 1), slug)
    source_refs = [value if SOURCE_REF.match(value) else f"source:{value}"
                   for value in string_list(meta.get("sources"))]
    raw_ref = norm(meta.get("raw")) or ws.rel(path)
    inferred_summary = summary_from_blocks(blocks, order)
    stamp = today()
    links = page_links(page_id, blocks, order)
    links.extend(reference_links(source_refs, string_list(meta.get("supersedes")),
                                 string_list(meta.get("related"))))
    return {
        "schema_version": "1.0", "id": page_id, "slug": slug, "title": title,
        "type": page_type or meta.get("type") or "source",
        "created": meta.get("created") or stamp, "updated": meta.get("updated") or stamp,
        "tags": string_list(meta.get("tags")),
        "projects": string_list(projects) or string_list(meta.get("projects")),
        "sources": source_refs, "raw_ref": raw_ref,
        "summary": summary or inferred_summary[:280], "blocks": blocks, "block_order": order,
        "links": links,
        "history": [{"at": stamp, "action": "ingested", "actor": "llmwiki-cli", "note": ws.rel(path)}],
        "source_snapshot": {"format": "markdown", "text": text, "sha256": sha(text)},
    }


# --------------------------------------------------------------------------- schema validation
class SchemaValidator:
    """Small JSON Schema subset covering tools/schema/page.schema.json.

    Supported: $ref/$defs, type, const, enum, pattern, minLength, format:date,
    required, properties, additionalProperties, items, uniqueItems.
    Keeps schema and code in sync without adding a runtime dependency.
    """

    TYPES = {"object": dict, "array": list, "string": str, "boolean": bool,
             "number": (int, float), "integer": int, "null": type(None)}

    def __init__(self, schema: dict[str, Any]) -> None:
        self.schema = schema

    @classmethod
    def load(cls, ws: Workspace) -> "SchemaValidator | None":
        if not ws.schema_path.exists():
            return None
        return cls(json.loads(ws.schema_path.read_text(encoding="utf-8")))

    def resolve(self, schema: dict[str, Any]) -> dict[str, Any]:
        ref = schema.get("$ref")
        if not ref:
            return schema
        node: Any = self.schema
        for part in ref.lstrip("#/").split("/"):
            node = node[part]
        return self.resolve(node)

    def validate(self, value: Any, schema: dict[str, Any] | None = None,
                 path: str = "") -> list[str]:
        schema = self.resolve(schema if schema is not None else self.schema)
        errors: list[str] = []
        where = path or "/"

        if "const" in schema and value != schema["const"]:
            errors.append(f"{where}: expected {schema['const']!r}")
        if "enum" in schema and value not in schema["enum"]:
            errors.append(f"{where}: {value!r} not in {schema['enum']}")

        expected = schema.get("type")
        if expected:
            wanted = expected if isinstance(expected, list) else [expected]
            types = tuple(self.TYPES[name] for name in wanted if name in self.TYPES)
            ok = isinstance(value, types) and not (isinstance(value, bool) and "boolean" not in wanted)
            if not ok:
                errors.append(f"{where}: expected type {'|'.join(wanted)}, got {type(value).__name__}")
                return errors

        if isinstance(value, str):
            if "pattern" in schema and not re.search(schema["pattern"], value):
                errors.append(f"{where}: {value!r} does not match {schema['pattern']}")
            if "minLength" in schema and len(value) < schema["minLength"]:
                errors.append(f"{where}: shorter than {schema['minLength']}")
            if schema.get("format") == "date" and not DATE.fullmatch(value):
                errors.append(f"{where}: {value!r} is not a YYYY-MM-DD date")

        if isinstance(value, dict):
            for key in schema.get("required", []):
                if key not in value:
                    errors.append(f"{where}: missing required '{key}'")
            properties = schema.get("properties", {})
            extra = schema.get("additionalProperties", True)
            for key, item in value.items():
                if key in properties:
                    errors.extend(self.validate(item, properties[key], f"{path}/{key}"))
                elif extra is False:
                    errors.append(f"{where}: unexpected property '{key}'")
                elif isinstance(extra, dict):
                    errors.extend(self.validate(item, extra, f"{path}/{key}"))

        if isinstance(value, list):
            if "items" in schema:
                for idx, item in enumerate(value):
                    errors.extend(self.validate(item, schema["items"], f"{path}/{idx}"))
            if schema.get("uniqueItems") and len({canonical(v) for v in value}) != len(value):
                errors.append(f"{where}: items must be unique")
        return errors


def assert_no_secrets(text: str, label: str) -> None:
    match = SECRET.search(text)
    if match:
        raise WikiError(
            f"{label}: 보안정보로 보이는 '{match.group(1)}' 값이 있습니다. "
            "원본을 정리하거나 '(접속 정보 생략)'으로 치환한 뒤 ingest 하세요.")


def validate_page(page: dict[str, Any], validator: SchemaValidator | None = None) -> list[str]:
    """Structural checks; `validator` adds full schema conformance when available."""
    pid = page.get("id", "?")
    errors: list[str] = []
    if not isinstance(page, dict):
        return [f"{pid}: page must be an object"]

    required = ["schema_version", "id", "slug", "title", "type", "created", "updated",
                "tags", "projects", "sources", "blocks", "block_order", "links", "history"]
    for key in required:
        if key not in page:
            errors.append(f"{pid}: missing {key}")
    if errors:
        return errors

    if page["schema_version"] != "1.0":
        errors.append(f"{pid}: schema_version must be 1.0")
    if not str(pid).startswith("page:"):
        errors.append(f"{pid}: invalid page id")
    if pid != f"page:{page['slug']}":
        errors.append(f"{pid}: id must be 'page:' + slug ({page['slug']!r})")
    if page["type"] not in ALLOWED_TYPES:
        errors.append(f"{pid}: invalid type {page['type']}")
    for key in ("created", "updated"):
        if not DATE.fullmatch(str(page[key])):
            errors.append(f"{pid}: {key} must be YYYY-MM-DD")
    for ref in page["sources"]:
        if not SOURCE_REF.match(str(ref)):
            errors.append(f"{pid}: invalid source ref {ref!r} (page:/source:/raw:/user:YYYY-MM-DD)")

    blocks, order = page["blocks"], page["block_order"]
    if not isinstance(blocks, dict) or not isinstance(order, list):
        return errors + [f"{pid}: blocks must be an object and block_order an array"]
    if len(order) != len(set(order)):
        errors.append(f"{pid}: duplicate ids in block_order")
    if set(order) != set(blocks):
        missing = sorted(set(blocks) - set(order))
        unknown = sorted(set(order) - set(blocks))
        errors.append(f"{pid}: block_order mismatch (unordered={missing}, unknown={unknown})")
    for bid, block in blocks.items():
        if block.get("id") != bid:
            errors.append(f"{pid}: block key/id mismatch {bid}")
        if block.get("kind") not in ALLOWED_BLOCKS:
            errors.append(f"{pid}: invalid block kind {block.get('kind')} on {bid}")
        for key in ("data", "refs", "source_text", "fingerprint"):
            if key not in block:
                errors.append(f"{pid}:{bid}: missing {key}")
        status = block.get("resolution", {}).get("status")
        if status is not None and status not in {"resolved", "unresolved"}:
            errors.append(f"{pid}:{bid}: invalid resolution status {status!r}")
    for idx, link in enumerate(page["links"]):
        if link.get("kind") not in ALLOWED_LINK_KINDS:
            errors.append(f"{pid}: link[{idx}] invalid kind {link.get('kind')}")
        if not norm(link.get("target")):
            errors.append(f"{pid}: link[{idx}] empty target")
        if link.get("block_id") and link["block_id"] not in blocks:
            errors.append(f"{pid}: link[{idx}] unknown block_id {link['block_id']}")

    if validator:
        errors.extend(f"{pid}{err}" for err in validator.validate(page))
    return sorted(dict.fromkeys(errors))


# --------------------------------------------------------------------------- projection / build
def project_group(projects: Iterable[str], groups: dict[str, Any]) -> str:
    projects = [norm(project).lower() for project in projects if norm(project)]
    if len(projects) > 1:
        return "multi"
    for key, config in groups.get("project", {}).items():
        if key in RESERVED_PROJECT_GROUPS:
            continue
        matches = {norm(value).lower() for value in config.get("match", []) if norm(value)}
        if any(project in matches for project in projects):
            return key
    return "ungrouped"


def project_group_key(project: str) -> str:
    """Return a stable config key for a newly observed canonical project value."""
    key = BLOCK_KEEP.sub("-", norm(project).lower()).strip("-._")
    return key or f"project-{sha(norm(project), 8)}"


def auto_project_groups(projects: Iterable[str], groups: dict[str, Any]) -> dict[str, Any]:
    """Groups for project values `groups.json` has never been told about.

    A project the config has not heard of used to fall into `ungrouped`, which
    made every new workstream look unclassified. Deriving the group instead
    keeps the answer a pure function of (pages, config) — same input, same
    key, same colour — while `register_project_groups` writes it down so the
    label and colour can then be edited by hand.
    """
    configured = groups.get("project", {})
    known = {norm(value).lower()
             for key, config in configured.items() if key not in RESERVED_PROJECT_GROUPS
             for value in config.get("match", []) if norm(value)}
    used_colors = {config.get("color") for config in configured.values()}
    additions: dict[str, Any] = {}
    for project in sorted({norm(value) for value in projects if norm(value)}):
        if project.lower() in known:
            continue
        known.add(project.lower())
        key = project_group_key(project)
        if key in configured or key in additions:
            key = f"{key}-{sha(project, 8)}"
        color = next((value for value in AUTO_PROJECT_COLORS if value not in used_colors),
                     AUTO_PROJECT_COLORS[len(additions) % len(AUTO_PROJECT_COLORS)])
        used_colors.add(color)
        label = project.upper() if re.fullmatch(r"[A-Za-z0-9._-]{1,12}", project) else project
        additions[key] = {"label": label, "color": color, "match": [project]}
    return additions


def merge_project_groups(groups: dict[str, Any], additions: dict[str, Any]) -> dict[str, Any]:
    """Ordinary groups, then the derived ones, with `multi`/`ungrouped` last."""
    if not additions:
        return groups
    configured = groups.get("project", {})
    ordinary = {k: v for k, v in configured.items() if k not in RESERVED_PROJECT_GROUPS}
    reserved = {k: v for k, v in configured.items() if k in RESERVED_PROJECT_GROUPS}
    return {**groups, "project": {**ordinary, **additions, **reserved}}


def register_project_groups(ws: Workspace, projects: Iterable[str], *, write: bool) -> list[str]:
    """Persist previously unseen project values so ingest cannot silently ungroup them."""
    groups = ws.load_groups()
    additions = auto_project_groups(projects, groups)
    if additions and write:
        dump(ws.groups_path, merge_project_groups(groups, additions), pretty=True)
    return list(additions)


def unresolved_count(page: dict[str, Any]) -> int:
    return sum(1 for b in page["blocks"].values()
               if b["kind"] == "conflict" and b.get("resolution", {}).get("status") != "resolved")


# 투영에 필요한 page 의 요약. 정본 파일에서도, 검색 색인(page/blk/link 표)에서도 같은 모양으로 만든다 —
# 그래서 증분 build 가 정본을 전부 다시 읽지 않고도 cold build 와 같은 파생물을 낸다.
def _record_from_page(rel: str, page: dict[str, Any]) -> dict[str, Any]:
    rec = {"id": page["id"], "slug": page["slug"], "title": page.get("title"), "type": page.get("type"),
           "created": page.get("created"), "updated": page.get("updated"),
           "projects": page.get("projects"), "tags": page.get("tags"), "sources": page.get("sources"),
           "source": rel, "sha256": sha(canonical(page)),
           "blocks": [(bid, page["blocks"][bid]["kind"]) for bid in page["block_order"]],
           "links": [], "unresolved": unresolved_count(page)}
    if "summary" in page:
        rec["summary"] = page["summary"]
    return rec


def _records_from_docs(docs: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    pages = [page for _, page in docs]
    lookup = page_lookup(pages)
    records = [_record_from_page(rel, page) for rel, page in docs]
    for rec, page in zip(records, pages):
        seen: set[str] = set()
        for link in implied_links(page):
            target = lookup.get(link_key(link["target"]))
            if not target or target["id"] == page["id"] or target["id"] in seen:
                continue
            # One line per ordered pair: a page that both links to and cites
            # another gets one edge, not two stacked on top of each other.
            seen.add(target["id"])
            rec["links"].append((target["id"], link["kind"]))
    return records


def _records_from_index(idx: "search_index.Index") -> list[dict[str, Any]]:
    """publish 된 색인의 page/blk/link 표에서 같은 요약을 읽는다 (정본 파일을 열지 않는다)."""
    db = idx.db
    records: dict[int, dict[str, Any]] = {}
    for rid, pid, slug, rel, sha256, meta_json, unresolved in db.execute(
            "SELECT rid, page_id, slug, source, sha256, meta, unresolved FROM page ORDER BY page_id"):
        meta = json.loads(meta_json or "{}")
        rec = {"id": pid, "slug": slug, "title": meta.get("title"), "type": meta.get("type"),
               "created": meta.get("created"), "updated": meta.get("updated"),
               "projects": meta.get("projects"), "tags": meta.get("tags"), "sources": meta.get("sources"),
               "source": rel, "sha256": sha256, "blocks": [], "links": [], "unresolved": int(unresolved or 0)}
        if "summary" in meta:
            rec["summary"] = meta["summary"]
        records[rid] = rec
    for prid, bid, kind in db.execute("SELECT prid, block_id, kind FROM blk WHERE pos >= 0 ORDER BY prid, pos"):
        records[prid]["blocks"].append((bid, kind))
    for src, dst_pid, kind in db.execute(
            "SELECT l.src, p.page_id, l.kind FROM link l JOIN page p ON p.rid=l.dst "
            "WHERE l.dst IS NOT NULL ORDER BY l.src, l.ord"):
        rec = records[src]
        if any(d == dst_pid for d, _k in rec["links"]):
            continue
        rec["links"].append((dst_pid, kind))
    return sorted(records.values(), key=lambda r: r["id"])


def _project_records(records: list[dict[str, Any]], groups: dict[str, Any]) -> dict[str, Any]:
    """Pure projection: page records -> derived artifacts (no disk writes)."""
    groups = merge_project_groups(
        groups, auto_project_groups((p for rec in records for p in rec["projects"]), groups))
    incoming: Counter[str] = Counter()
    outgoing: Counter[str] = Counter()
    edges: list[dict[str, Any]] = []
    for rec in records:
        for target_id, kind in rec["links"]:
            outgoing[rec["id"]] += 1
            incoming[target_id] += 1
            edges.append({"id": f"edge:{sha('|'.join((rec['id'], target_id)), 16)}", "source": rec["id"],
                          "target": target_id, "kind": kind})
    edges.sort(key=lambda e: (e["source"], e["target"], e["kind"]))

    buckets: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    nodes: list[dict[str, Any]] = []
    for rec in records:
        group = project_group(rec["projects"], groups)
        degree = incoming[rec["id"]] + outgoing[rec["id"]]
        node = {"id": rec["id"], "slug": rec["slug"], "label": rec["title"], "type": rec["type"],
                "created": rec["created"], "updated": rec["updated"],
                "projects": rec["projects"], "tags": rec["tags"], "group": group,
                "summary": rec.get("summary", ""), "incoming": incoming[rec["id"]],
                "outgoing": outgoing[rec["id"]], "degree": degree,
                "unresolved_conflicts": rec["unresolved"], "orphan": degree == 0,
                "data_url": f"pages/{safe_name(rec['id'])}"}
        buckets[group].append(node)
        nodes.append(node)
    # Allocate an angular sector proportional to each group's size. Within a
    # sector, a golden-ratio sequence distributes nodes from the centre to the
    # rim without rings or clumps. The resulting silhouette is a true disk,
    # while adjacent colours still make project groups easy to distinguish.
    configured_order = list(groups.get("project", {}))
    ordered_groups = [group for group in configured_order if buckets.get(group)]
    ordered_groups.extend(sorted(group for group in buckets if group not in configured_order))
    total_nodes = max(1, len(nodes))
    sector_start = -math.pi / 2
    for group in ordered_groups:
        group_nodes = sorted(buckets[group], key=lambda item: item["id"])
        sector_span = math.tau * len(group_nodes) / total_nodes
        angular_padding = min(0.035, sector_span * 0.04)
        usable_span = max(0.0, sector_span - angular_padding * 2)
        for i, node in enumerate(group_nodes):
            radial_fraction = math.sqrt((i + 0.72) / (len(group_nodes) + 0.72))
            angular_fraction = (i * GOLDEN_FRACTION) % 1.0
            angle = sector_start + angular_padding + usable_span * angular_fraction
            radius = LAYOUT_RADIUS * radial_fraction
            node["x"] = round(radius * math.cos(angle), 6)
            node["y"] = round(radius * math.sin(angle), 6)
        sector_start += sector_span
    nodes.sort(key=lambda n: n["id"])

    catalog = [{k: rec.get(k) for k in ("id", "slug", "title", "type", "updated",
                                        "projects", "tags", "sources", "summary")} for rec in records]
    map_pages: dict[str, Any] = {}
    map_blocks: dict[str, Any] = {}
    for rec in sorted(records, key=lambda r: r["id"]):
        # data_url serves the page as a standalone object; the empty RFC 6901
        # pointer selects that object root directly.
        map_pages[rec["id"]] = {"source": rec["source"], "pointer": "",
                                "data_url": f"pages/{safe_name(rec['id'])}", "sha256": rec["sha256"]}
        for bid, kind in rec["blocks"]:
            map_blocks[bid] = {"page_id": rec["id"], "pointer": f"/blocks/{bid}", "kind": kind,
                               "data_url": f"pages/{safe_name(rec['id'])}"}
    routes = {key: sorted(n["id"] for n in nodes if n["group"] == key) for key in groups["project"]}

    return {
        "catalog.json": catalog,
        "map.json": {"schema_version": "1.0", "pages": map_pages, "blocks": map_blocks},
        "graph.json": {"schema_version": "1.0", "nodes": nodes, "edges": edges, "groups": groups},
        "routes.json": routes,
        "stats.json": {"pages": len(records), "blocks": sum(len(r["blocks"]) for r in records),
                       "edges": len(edges),
                       "unresolved_conflicts": sum(n["unresolved_conflicts"] for n in nodes)},
    }


def check_documents(docs: list[tuple[str, dict[str, Any]]], validator: "SchemaValidator | None") -> None:
    """Schema + cross-page uniqueness; raises WikiError with every problem listed."""
    pages = [page for _, page in docs]
    errors = [e for page in pages for e in validate_page(page, validator)]
    if errors:
        raise WikiError("\n".join(errors))
    ids = [p["id"] for p in pages]
    slugs = [p["slug"] for p in pages]
    if len(ids) != len(set(ids)):
        raise WikiError(f"duplicate page id: {sorted(k for k, v in Counter(ids).items() if v > 1)}")
    if len(slugs) != len(set(slugs)):
        raise WikiError(f"duplicate page slug: {sorted(k for k, v in Counter(slugs).items() if v > 1)}")
    block_owner: dict[str, str] = {}
    for page in pages:
        for bid in page["block_order"]:
            if bid in block_owner:
                raise WikiError(f"duplicate block id {bid} in {block_owner[bid]} and {page['id']}")
            block_owner[bid] = page["id"]


def project(ws: Workspace) -> dict[str, Any]:
    """Pure projection: canonical pages -> derived artifacts (no disk writes)."""
    return _project_docs(ws, ws.load_documents())


def _project_docs(ws: Workspace, docs: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    check_documents(docs, SchemaValidator.load(ws))
    return _project_records(_records_from_docs(docs), ws.load_groups())


def project_from_index(ws: Workspace) -> dict[str, Any]:
    """The same artifacts, read from the published search index instead of the canonical files."""
    idx = search_index.open_ro(ws.search_db)
    try:
        return _project_records(_records_from_index(idx), ws.load_groups())
    finally:
        idx.close()


# --------------------------------------------------------------------------- 델타 · 신선도
# 힌트 밖 파일의 변경 감지는 지난 build 가 `index/search.work.json` 에 파일마다 남긴 [mtime_ns, size] 와의
# 비교다 — 다르기만 하면(과거로 돌린 mtime 포함) sha 로 확인한다. 시각 문턱은 쓰지 않는다.
# 델타가 page 의 이 비율을 넘으면 cold build 가 더 싸다.
FULL_FRACTION = 0.25
MAP_PAGE_KEYS = ("source", "pointer", "data_url", "sha256")


def map_root(payload: dict[str, Any] | None) -> str:
    """`index/map.json` 의 page 부분 지문. revision 과 색인 meta.map_root 가 이 값으로 맞물린다."""
    if not payload:
        return ""
    pages = payload.get("pages") or {}
    return sha(canonical({pid: {k: entry.get(k) for k in MAP_PAGE_KEYS} for pid, entry in pages.items()}))


def revision_of(root: str, groups: dict[str, Any], *, heading_paths: bool) -> str:
    """산출물이 실제로 달라질 때만 바뀌는 값 — page 집합(map root)·그룹 설정·색인 옵션의 함수."""
    return sha(canonical({"map_root": root, "groups": groups, "heading_paths": bool(heading_paths),
                          "schema": search_index.SCHEMA_VERSION}))


def read_map(ws: Workspace) -> dict[str, Any] | None:
    path = ws.index / "map.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) and isinstance(value.get("pages"), dict) else None


def read_state(ws: Workspace) -> dict[str, Any] | None:
    try:
        value = json.loads(ws.state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def wiki_files(ws: Workspace) -> dict[str, list[int]]:
    """wiki/**/*.json → [mtime_ns, size]. 수십 ms 짜리 스캔 — 정본을 열지 않는다."""
    out: dict[str, list[int]] = {}
    base = ws.pages_dir
    # ws.rel 은 symlink 를 풀어 저장소 상대 경로를 만든다. wiki/ 가 symlink 가 아니면 문자열로 자른다(10,000
    # 파일에서 0.3 초 → 30 ms); symlink 면 파일마다 ws.rel 로 간다.
    prefix = str(ws.root) + os.sep
    fast = os.path.realpath(str(base)) == str(base)
    stack = [str(base)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    if entry.name.startswith("."):
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(entry.path)
                    elif entry.name.endswith(".json"):
                        try:
                            st = entry.stat()
                        except OSError:
                            continue
                        if fast and entry.path.startswith(prefix):
                            rel = entry.path[len(prefix):].replace(os.sep, "/")
                        else:
                            rel = ws.rel(Path(entry.path))
                        out[rel] = [st.st_mtime_ns, st.st_size]
        except OSError:
            continue
    return out


def load_file_pages(ws: Workspace, rel: str) -> list[dict[str, Any]]:
    """정본 파일 하나의 page 목록 (없으면 [])."""
    path = ws.root / rel
    if not path.is_file():
        return []
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise WikiError(f"{rel}: invalid JSON ({exc})") from exc
    return [p for p in (value if isinstance(value, list) else [value]) if isinstance(p, dict)]


def file_shas_match(ws: Workspace, rel: str, old_pages: dict[str, Any]) -> bool:
    """파일의 page sha 들이 옛 map 의 항목과 정확히 같은가 (같은 source 의 항목만 본다)."""
    expected = {pid: e.get("sha256") for pid, e in old_pages.items() if e.get("source") == rel}
    try:
        pages = load_file_pages(ws, rel)
    except WikiError:
        return False
    found = {str(p.get("id")): sha(canonical(p)) for p in pages}
    return found == expected


def scan_changes(ws: Workspace, old_map: dict[str, Any], files: dict[str, Any], hint: set[str],
                 on_disk: dict[str, list[int]] | None = None) -> list[str]:
    """힌트 밖에서 바뀐 파일 목록. 지난 build 가 파일마다 기록한 `[mtime_ns, size]`(`files`, search.work.json) 와
    **다르기만 하면** — 더 오래된 mtime 이라도 — sha 로 확인하고, 같으면 파일을 열지 않는다.

    "지난 build 시각보다 새 mtime" 기준은 timestamp 를 보존한 복사나 utime 으로 과거로 돌린 편집을 놓쳤다
    (codex REVIEW #4). 기록이 없는 파일(새 파일)과 기록·map 에 있는데 없어진 파일은 그대로 변경으로 본다.
    기록이 없는 상태(옛 build) 에서는 알려진 파일을 전부 sha 로 확인한다 — 느리지만 안전하다."""
    on_disk = wiki_files(ws) if on_disk is None else on_disk
    old_pages = old_map.get("pages") or {}
    known = {str(e.get("source")) for e in old_pages.values()}
    outside: list[str] = []
    for rel in sorted((known | set(files)) - set(on_disk)):
        if rel not in hint:
            outside.append(rel)
    for rel, stat in sorted(on_disk.items()):
        if rel in hint:
            continue
        if rel not in known and rel not in files:
            outside.append(rel)
        elif list(files.get(rel) or ()) != list(stat) and not file_shas_match(ws, rel, old_pages):
            outside.append(rel)
    return outside


def page_hash_map(ws: Workspace, changed: Iterable[str] | None = None, *, old_map: dict[str, Any] | None = None
                  ) -> tuple[dict[str, Any], dict[str, tuple[str, dict[str, Any]]]]:
    """(map.json 의 pages, 다시 읽은 문서 {page_id: (source, page)}).

    `changed` 가 None 이면 전량(정본을 모두 읽고 검증), 아니면 그 파일만 다시 읽어 sha 를 내고 나머지는
    `old_map` 의 항목을 재사용한다. 힌트 밖의 변경은 `scan_changes` 가 따로 잡는다 — 여기서는 믿는다.
    """
    validator = SchemaValidator.load(ws)
    if changed is None:
        docs = ws.load_documents()
        check_documents(docs, validator)
        payload = _project_records(_records_from_docs(docs), {"project": {}})["map.json"]
        return payload["pages"], {str(p["id"]): (rel, p) for rel, p in docs}
    hint = {ws.rel(ws.root / rel) if not os.path.isabs(rel) else ws.rel(Path(rel)) for rel in changed}
    old_pages = dict((old_map or {}).get("pages") or {})
    pages = {pid: e for pid, e in old_pages.items() if str(e.get("source")) not in hint}
    docs: dict[str, tuple[str, dict[str, Any]]] = {}
    for rel in sorted(hint):
        for page in load_file_pages(ws, rel):
            errors = validate_page(page, validator)
            if errors:
                raise WikiError("\n".join(errors))
            pid = str(page["id"])
            if pid in docs:
                raise WikiError(f"duplicate page id: {pid} ({docs[pid][0]}, {rel})")
            if pid in pages and (ws.root / str(pages[pid].get("source"))).is_file():
                raise WikiError(f"duplicate page id: {pid} ({pages[pid].get('source')}, {rel})")
            docs[pid] = (rel, page)
            pages[pid] = {"source": rel, "pointer": "", "data_url": f"pages/{safe_name(pid)}",
                          "sha256": sha(canonical(page))}
    return dict(sorted(pages.items())), docs


map_delta = search_index.map_delta


def build(ws: Workspace, *, changed: Iterable[str] | None = None, full: bool = False,
          heading_paths: bool = False) -> dict[str, Any]:
    """정본 → index/*.json + search.sqlite + viewer/public/data. 결과는 cold 든 증분이든 같은 바이트다.

    `changed` 는 힌트다: 그 파일만 다시 sha 를 내고, `wiki/**/*.json` mtime 스캔으로 힌트 밖의 변경이
    보이면 전량으로 떨어진다. 증분은 검색 색인의 바뀐 page 행만 갈아 끼우고(`llmwiki_index.update`),
    파생물은 publish 된 색인의 표에서 투영한다. `index/map.json`·`revision.json` 은 publish 가 성공한
    뒤에만 쓴다.
    """
    started_ns = time.time_ns()
    phases: dict[str, float] = {}
    clock = time.perf_counter()

    def lap(name: str) -> None:
        nonlocal clock
        now = time.perf_counter()
        phases[name] = round(phases.get(name, 0.0) + (now - clock) * 1000, 1)
        clock = now

    groups = ws.load_groups()
    mode, reason = "full", ""
    old_map = read_map(ws)
    state = read_state(ws)
    old_root = map_root(old_map)
    on_disk = wiki_files(ws)          # 정본을 읽기 전의 스냅샷 — 그 뒤 바뀐 파일은 다음 build 가 다른 (mtime,size) 로 잡는다
    hint: set[str] | None = None
    if changed is not None and not full:
        hint = {ws.rel(ws.root / rel) if not os.path.isabs(rel) else ws.rel(Path(rel)) for rel in changed}
        if not old_map or not state:
            reason = "no-previous-build"
        elif str(state.get("schema")) != search_index.SCHEMA_VERSION or bool(state.get("heading_paths")) != bool(heading_paths):
            reason = "index-options-changed"
        elif str(state.get("map_root")) != old_root:
            reason = "map-root-mismatch"
        else:
            outside = scan_changes(ws, old_map, dict(state.get("files") or {}), hint, on_disk)
            if outside:
                reason = "unhinted-change:" + ",".join(outside[:3])
            else:
                mode = "incremental"
    elif full:
        reason = "full-requested"
    else:
        reason = "no-hint"
    lap("scan")

    built: dict[str, Any] = {}
    delta = search_index.Delta()
    docs_by_id: dict[str, tuple[str, dict[str, Any]]] = {}
    if mode == "incremental":
        assert old_map is not None and hint is not None
        new_pages, docs_by_id = page_hash_map(ws, hint, old_map=old_map)
        delta = map_delta(old_map["pages"], new_pages)
        new_root = map_root({"pages": new_pages})
        lap("hash")
        if len(delta) > FULL_FRACTION * max(len(new_pages), 1):
            mode, reason = "full", "large-delta"
        elif not delta:
            reason = "no-change"
        else:
            revision = revision_of(new_root, groups, heading_paths=heading_paths)

            def loader(page_id: str, rel: str) -> dict[str, Any] | None:
                return next((p for p in load_file_pages(ws, rel) if str(p.get("id")) == page_id), None)

            try:
                built = search_index.update(ws.search_db, docs_by_id, delta, revision=revision, map_root=new_root,
                                            expect_map_root=old_root, loader=loader, heading_paths=heading_paths)
            except search_index.IndexError_ as exc:
                mode, reason = "full", f"index:{exc}"
            lap("index")
    if mode == "incremental":
        if delta:
            payloads = project_from_index(ws)
            lap("project")
            if map_root(payloads["map.json"]) != new_root:      # 색인과 map 이 어긋나면 믿지 않는다
                mode, reason = "full", "projection-mismatch"
        else:
            payloads = None
    if mode == "full":
        docs = ws.load_documents()
        payloads = _project_docs(ws, docs)
        new_root = map_root(payloads["map.json"])
        revision = revision_of(new_root, groups, heading_paths=heading_paths)
        built = search_index.build(docs, ws.search_db, revision=revision, heading_paths=heading_paths,
                                   map_root=new_root)
        docs_by_id = {str(p["id"]): (rel, p) for rel, p in docs}
        delta = search_index.Delta(added=sorted(docs_by_id))
        lap("full")

    stats: dict[str, Any]
    if payloads is None:                                    # 증분인데 델타가 비었다: 산출물은 그대로다
        stats = dict(json.loads((ws.index / "stats.json").read_text(encoding="utf-8")))
        # 파일 기록은 갱신한다 — touch 만 된 파일을 sha 로 확인했으니 다음 build 는 열지 않아도 된다
        dump(ws.state_path, {"schema": search_index.SCHEMA_VERSION, "started_ns": started_ns,
                             "map_root": new_root, "heading_paths": bool(heading_paths),
                             "files": dict(sorted(on_disk.items()))})
    else:
        payloads["revision.json"] = {"schema_version": "1.0", "revision": revision, "search_root": built["digest"]}
        for name, data in payloads.items():
            dump(ws.index / name, data, pretty=True)
            dump(ws.public / name, data)
        shard_dir = ws.public / "pages"
        if mode == "full":
            shutil.rmtree(shard_dir, ignore_errors=True)
            shard_dir.mkdir(parents=True, exist_ok=True)
            for _pid, (_rel, page) in docs_by_id.items():
                dump(shard_dir / safe_name(page["id"]), page)
        else:
            for pid in delta.deleted:
                (shard_dir / safe_name(pid)).unlink(missing_ok=True)
            for pid in [*delta.added, *delta.modified]:
                dump(shard_dir / safe_name(pid), docs_by_id[pid][1])
        stats = dict(payloads["stats.json"])
        dump(ws.state_path, {"schema": search_index.SCHEMA_VERSION, "started_ns": started_ns,
                             "map_root": new_root, "heading_paths": bool(heading_paths),
                             "files": dict(sorted(on_disk.items()))})
        lap("write")
    stats["mode"] = mode
    stats["phases"] = phases
    stats["reason"] = reason
    stats["delta"] = {"added": len(delta.added), "modified": len(delta.modified), "deleted": len(delta.deleted)}
    if built:
        stats["index"] = {k: built[k] for k in ("bytes", "digest", "mode") if k in built}
        if "compacted" in built:
            stats["index"]["compacted"] = built["compacted"]
        if "reindexed" in built:
            stats["index"]["reindexed"] = built["reindexed"]
    stats["ms"] = round((time.time_ns() - started_ns) / 1e6, 1)
    return stats


# --------------------------------------------------------------------------- render
def frontmatter(page: dict[str, Any]) -> list[str]:
    return ["---", f"type: {page['type']}", f"created: {page['created']}",
            f"updated: {page['updated']}", f"tags: [{', '.join(page['tags'])}]",
            f"projects: [{', '.join(page['projects'])}]",
            f"sources: [{', '.join(page['sources'])}]", "---", ""]


def render_markdown(page: dict[str, Any], exact: bool = False) -> str:
    """`exact` replays the ingested snapshot byte-for-byte; otherwise blocks are joined."""
    snapshot = page.get("source_snapshot")
    if exact:
        if not snapshot or snapshot.get("format") != "markdown":
            raise WikiError(f"{page['id']}: no markdown snapshot for --exact")
        return snapshot["text"]
    body = "\n\n".join(page["blocks"][bid]["source_text"] for bid in page["block_order"])
    return "\n".join(frontmatter(page)) + body.rstrip() + "\n"


def wiki_to_html(text: str) -> str:
    escaped = html.escape(str(text or ""))
    return WIKILINK.sub(
        lambda m: '<a href="#" data-wiki-target="{}">{}</a>'.format(
            html.escape(norm(m.group(1))), html.escape(norm(m.group(3)) or norm(m.group(1)))),
        escaped)


def render_block_html(block: dict[str, Any]) -> str:
    data = block.get("data", {})
    kind = block["kind"]
    if kind == "heading":
        level = min(6, max(1, int(data.get("level", 2))))
        return f"<h{level}>{wiki_to_html(data.get('text', ''))}</h{level}>"
    if kind in {"paragraph", "markdown", "raw"}:
        return "<p>{}</p>".format(
            wiki_to_html(data.get("text", block["source_text"])).replace("\n", "<br>"))
    if kind == "code":
        return '<pre><code data-language="{}">{}</code></pre>'.format(
            html.escape(str(data.get("language", ""))), html.escape(str(data.get("text", ""))))
    if kind == "table":
        rows = data.get("rows", [])
        out = ["<table>"]
        for idx, row in enumerate(rows):
            if idx == 1 and row and all(re.fullmatch(r":?-+:?", c) for c in row):
                continue
            tag = "th" if idx == 0 else "td"
            out.append("<tr>" + "".join(f"<{tag}>{wiki_to_html(c)}</{tag}>" for c in row) + "</tr>")
        out.append("</table>")
        return "".join(out)
    if kind == "list":
        tag = "ol" if data.get("ordered") else "ul"
        items = data.get("items") or [
            line.strip() for line in str(data.get("text", "")).splitlines() if line.strip()]
        return f"<{tag}>" + "".join(f"<li>{wiki_to_html(i)}</li>" for i in items) + f"</{tag}>"
    if kind in {"quote", "conflict", "current"}:
        return f'<blockquote class="{kind}">{wiki_to_html(data.get("text", ""))}</blockquote>'
    if kind == "thematic_break":
        return "<hr>"
    return f"<pre>{html.escape(block['source_text'])}</pre>"


def render_html(page: dict[str, Any]) -> str:
    body = "".join(render_block_html(page["blocks"][bid]) for bid in page["block_order"])
    return f'<article data-page-id="{html.escape(page["id"])}">{body}</article>'


# --------------------------------------------------------------------------- addressing
def split_address(selector: str) -> tuple[str, str | None]:
    selector = norm(selector)
    if selector.startswith("block:"):
        return "", selector
    if "#" in selector:
        page_sel, block_sel = selector.split("#", 1)
        return page_sel.strip(), block_sel.strip()
    return selector, None


def find_page(ws: Workspace, selector: str) -> dict[str, Any]:
    selector = norm(selector)
    pages = ws.load_pages()
    for page in pages:
        if selector in {page["id"], page["slug"], page["id"].removeprefix("page:")}:
            return page
    # Same tolerance the graph uses: a title or a differently-cased slug still lands.
    match = page_lookup(pages).get(link_key(selector))
    if match:
        return match
    raise WikiError(f"page not found: {selector}")


def find_block(ws: Workspace, block_id: str, page: dict[str, Any] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    candidates = [page] if page else ws.load_pages()
    for candidate in candidates:
        block = candidate["blocks"].get(block_id)
        if block:
            return candidate, block
    # Allow addressing a block by its bare fingerprint too.
    for candidate in candidates:
        for bid, block in candidate["blocks"].items():
            if bid.endswith(f":{block_id}") or block.get("fingerprint") == block_id:
                return candidate, block
    raise WikiError(f"block not found: {block_id}")


def resolve(ws: Workspace, selector: str) -> tuple[dict[str, Any], dict[str, Any] | None]:
    page_sel, block_sel = split_address(selector)
    page = find_page(ws, page_sel) if page_sel else None
    if not block_sel:
        if page is None:
            raise WikiError(f"page not found: {selector}")
        return page, None
    page, block = find_block(ws, block_sel, page)
    return page, block


def json_pointer(value: Any, pointer: str) -> Any:
    """RFC 6901 pointer; '' selects the whole document."""
    if not pointer or pointer == "/":
        return value
    for token in pointer.lstrip("/").split("/"):
        key = token.replace("~1", "/").replace("~0", "~")
        if isinstance(value, list):
            if not re.fullmatch(r"\d+", key) or int(key) >= len(value):
                raise WikiError(f"pointer {pointer}: bad array index {key!r}")
            value = value[int(key)]
        elif isinstance(value, dict):
            if key not in value:
                raise WikiError(f"pointer {pointer}: missing key {key!r}")
            value = value[key]
        else:
            raise WikiError(f"pointer {pointer}: cannot descend into {type(value).__name__}")
    return value


def pick_fields(value: Any, fields: Iterable[str]) -> Any:
    for field in fields:
        if isinstance(value, list) and re.fullmatch(r"\d+", field):
            value = value[int(field)]
        elif isinstance(value, dict) and field in value:
            value = value[field]
        else:
            raise WikiError(f"field not found: {field}")
    return value


def outline(page: dict[str, Any]) -> list[dict[str, Any]]:
    """Cheap section index — read a section instead of the whole page."""
    rows = []
    for position, bid in enumerate(page["block_order"]):
        block = page["blocks"][bid]
        if block["kind"] == "heading":
            rows.append({"block_id": bid, "level": block["data"].get("level", 2),
                         "text": block["data"].get("text", ""), "position": position})
    return rows


def section(page: dict[str, Any], block_id: str) -> list[dict[str, Any]]:
    """The heading block plus every block up to the next heading of the same/higher level."""
    order = page["block_order"]
    if block_id not in order:
        raise WikiError(f"{page['id']}: no block {block_id}")
    start = order.index(block_id)
    head = page["blocks"][block_id]
    if head["kind"] != "heading":
        return [head]
    level = head["data"].get("level", 2)
    picked = [head]
    for bid in order[start + 1:]:
        block = page["blocks"][bid]
        if block["kind"] == "heading" and block["data"].get("level", 2) <= level:
            break
        picked.append(block)
    return picked


# --------------------------------------------------------------------------- lint
def lint(ws: Workspace) -> tuple[list[str], list[str]]:
    docs = ws.load_documents()
    pages = [p for _, p in docs]
    validator = SchemaValidator.load(ws)
    by_slug: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    warnings: list[str] = []

    for page in pages:
        errors.extend(validate_page(page, validator))
        slug = page.get("slug")
        if slug in by_slug:
            errors.append(f"{page.get('id')}: duplicate slug {slug}")
        by_slug[slug] = page

    seen_blocks: dict[str, str] = {}
    for page in pages:
        for bid in page.get("block_order", []):
            if bid in seen_blocks:
                errors.append(f"{page['id']}: duplicate block id {bid} (also in {seen_blocks[bid]})")
            seen_blocks[bid] = page["id"]

    lookup = page_lookup(pages)
    for page in pages:
        declared = {link_key(link.get("target", "")) for link in page.get("links", [])}
        for link in implied_links(page):
            target = lookup.get(link_key(link["target"]))
            if target is None:
                # A `source:` ref may name evidence that has no page of its own;
                # a [[wikilink]] that resolves to nothing is always a mistake.
                if link["kind"] == "wiki":
                    errors.append(f"{page['id']}: missing link target [[{link['target']}]]")
                continue
            if link["kind"] == "wiki" and link_key(link["target"]) not in declared:
                warnings.append(f"{page['id']}: block link [[{link['target']}]] missing from links")
        for ref in page.get("sources", []):
            if str(ref).startswith("page:") and link_key(str(ref)) not in lookup:
                errors.append(f"{page['id']}: source ref {ref} has no page")
        for bid in page.get("block_order", []):
            block = page["blocks"][bid]
            if block["kind"] == "conflict" and block.get("resolution", {}).get("status") != "resolved":
                warnings.append(f"{page['id']}:{bid}: unresolved conflict")
        if not page.get("summary"):
            warnings.append(f"{page['id']}: empty summary")

    degree: Counter[str] = Counter()
    for page in pages:
        for link in implied_links(page):
            target = lookup.get(link_key(link["target"]))
            if target is not None and target["id"] != page["id"]:
                degree[page["slug"]] += 1
                degree[target["slug"]] += 1
    for page in pages:
        if page.get("type") not in UNLINKED_TYPES and degree[page.get("slug")] == 0:
            warnings.append(f"{page['id']}: orphan (no inbound or outbound links)")

    warnings.extend(stale_index(ws))
    return sorted(dict.fromkeys(errors)), sorted(dict.fromkeys(warnings))


def stale_index(ws: Workspace) -> list[str]:
    """index/map.json 이 정본과 맞는가 — 전량 재투영 대신 mtime 스캔 + 후보 파일의 sha 대조. 아무것도 쓰지 않는다."""
    path = ws.index / "map.json"
    if not path.exists():
        return ["index/map.json missing — run build"]
    current = read_map(ws)
    if current is None:
        return ["index/map.json unreadable — run build"]
    state = read_state(ws)
    if scan_changes(ws, current, dict((state or {}).get("files") or {}), set()):
        return ["index/map.json stale — run build"]
    return []


# --------------------------------------------------------------------------- log
def append_log(ws: Workspace, entry: dict[str, Any]) -> dict[str, Any]:
    record = {"at": timestamp(), **entry}
    ws.log_path.parent.mkdir(parents=True, exist_ok=True)
    with ws.log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def read_log(ws: Workspace, limit: int = 10) -> list[dict[str, Any]]:
    if not ws.log_path.exists():
        return []
    rows = [json.loads(line) for line in ws.log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return rows[-limit:] if limit > 0 else rows


# --------------------------------------------------------------------------- ingest
def ingest(ws: Workspace, source: Path, page_type: str | None = None,
           projects: list[str] | None = None, summary: str = "",
           update: bool = False, dry_run: bool = False) -> dict[str, Any]:
    """Write one canonical page. raw/ is read-only and is verified untouched."""
    path = Path(source).resolve()
    if not path.exists() or not path.is_file():
        raise WikiError(f"source not found: {source}")
    try:
        path.relative_to(ws.knowledge)
        raise WikiError("wiki/ is the canonical store; ingest reads from raw/, not from it")
    except ValueError:
        pass

    before = sha(path.read_bytes().hex())
    text = path.read_text(encoding="utf-8")
    assert_no_secrets(text, ws.rel(path))

    if path.suffix.lower() == ".json":
        page = json.loads(text)
        if isinstance(page, list):
            raise WikiError("ingest takes a single page object, not an array")
        page.setdefault("raw_ref", ws.rel(path))
        if page_type:
            page["type"] = page_type
        if projects:
            page["projects"] = projects
        if summary:
            page["summary"] = summary
        for key in ("tags", "projects", "sources"):
            if key in page:
                page[key] = string_list(page[key])
        page.setdefault("history", []).append(
            {"at": today(), "action": "ingested", "actor": "llmwiki-cli", "note": ws.rel(path)})
    else:
        page = page_from_markdown(ws, path, page_type, projects, summary)

    errors = validate_page(page, SchemaValidator.load(ws))
    if errors:
        raise WikiError("\n".join(errors))

    dest = ws.knowledge / PAGE_DIRS[page["type"]] / safe_name(page["id"])
    existing = next((p for p in ws.knowledge.rglob("*.json")
                     if p.name == dest.name and p.is_file()), None) if ws.knowledge.exists() else None
    if existing and not update:
        raise WikiError(f"{page['id']} already exists at {ws.rel(existing)} — pass --update to replace")
    if existing:
        prior = json.loads(existing.read_text(encoding="utf-8"))
        if not isinstance(prior, dict):
            raise WikiError(f"{ws.rel(existing)} holds several pages in one file — "
                            "edit it directly instead of ingesting over it")
        if norm(prior.get("id")) != page["id"]:
            raise WikiError(f"{ws.rel(existing)} already holds {prior.get('id')} — "
                            f"{page['id']} would overwrite a different page")
        page["created"] = prior.get("created", page["created"])
        page["history"] = [*prior.get("history", []), *page["history"]]

    registered_groups = register_project_groups(ws, page["projects"], write=not dry_run)
    # A page whose type changed belongs in the directory of its new type —
    # leaving it behind would file an entity under wiki/sources/.
    moved_from = existing if existing and existing.resolve() != dest.resolve() else None
    result = {"page_id": page["id"], "dest": ws.rel(dest), "blocks": len(page["blocks"]),
              "updated": bool(existing), "dry_run": dry_run,
              "registered_groups": registered_groups}
    if moved_from:
        result["moved_from"] = ws.rel(moved_from)
    if dry_run:
        return result

    dump(dest, page, pretty=True)
    if moved_from:
        moved_from.unlink(missing_ok=True)
    if sha(path.read_bytes().hex()) != before:
        raise WikiError(f"source {ws.rel(path)} changed during ingest — raw/ must stay immutable")
    append_log(ws, {"action": "ingest", "page_id": page["id"],
                    "source": ws.rel(path), "dest": ws.rel(dest),
                    "mode": "update" if existing else "create",
                    "registered_groups": registered_groups})
    # 파생물은 build 로만 갱신한다 — 바뀐 파일만 힌트로 넘겨 색인의 그 page 행만 갈아 끼운다.
    changed = [ws.rel(dest)] + ([ws.rel(moved_from)] if moved_from else [])
    result["build"] = build(ws, changed=changed)
    return result


# --------------------------------------------------------------------------- search
def open_search_index(ws: Workspace) -> "search_index.Index":
    """The built index when it is fresh, otherwise one built in memory from the canonical pages.

    `query` therefore never answers from a stale index: freshness means the index's
    `meta.revision` matches `index/revision.json` and no canonical file is newer than it.
    """
    if not ws.fixtures and ws.search_db.is_file():
        try:
            idx = search_index.open_ro(ws.search_db)
            fresh = (idx.revision and idx.revision == search_index.read_revision(ws.root)
                     and search_index.newest_mtime(ws.pages_dir) <= ws.search_db.stat().st_mtime)
            if fresh:
                return idx
            idx.close()
        except (OSError, ValueError, sqlite3.Error):
            pass
    return search_index.build_memory(ws.load_documents())


def query(ws: Workspace, text: str, limit: int = 10) -> list[dict[str, Any]]:
    """Rank pages with the search index (fresh on disk, or built in memory from the pages)."""
    idx = open_search_index(ws)
    try:
        result = idx.search(text, k=max(1, limit))
        pages = idx.pages([h.page_id for h in result.hits])
    finally:
        idx.close()
    rows = []
    for hit in result.hits:
        page = pages.get(hit.page_id, {})
        rows.append({"score": round(hit.score, 4), "id": hit.page_id, "slug": page.get("slug", hit.slug),
                     "title": page.get("title", ""), "summary": page.get("summary", ""),
                     "blocks": list(hit.block_ids), "superseded_by": hit.head if hit.head != hit.page_id else None})
    return rows


# --------------------------------------------------------------------------- CLI
def emit(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="llmwiki", description=__doc__.splitlines()[0])
    parser.add_argument("--root", help="repository root (default: $LLMWIKI_ROOT or the repo)")
    sub = parser.add_subparsers(dest="command", required=True)

    ing = sub.add_parser("ingest", help="add one raw source as a canonical page")
    ing.add_argument("input")
    ing.add_argument("--type", choices=sorted(ALLOWED_TYPES))
    ing.add_argument("--project", action="append", default=[])
    ing.add_argument("--summary", default="")
    ing.add_argument("--update", action="store_true", help="replace an existing page, keeping created/history")
    ing.add_argument("--dry-run", action="store_true")

    for name, help_text in (("build", "regenerate index/ (incl. search.sqlite) and viewer public data"),
                            ("validate", "schema + structural checks"),
                            ("lint", "links, conflicts, orphans, stale index")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--fixtures", action="store_true")
        if name != "build":
            p.add_argument("--json", action="store_true", dest="as_json")
        else:
            p.add_argument("--heading-paths", action="store_true",
                           help="index blocks with their heading path prefixed (H6; default off)")
            p.add_argument("--changed", nargs="+", metavar="PATH", default=None,
                           help="hint: only these wiki files changed (verified by an mtime scan; "
                                "anything else changed falls back to a full build)")
            p.add_argument("--full", action="store_true", help="ignore the work index and rebuild everything")

    q = sub.add_parser("query", help="rank pages with the search index")
    q.add_argument("query")
    q.add_argument("--limit", type=int, default=10)
    q.add_argument("--fixtures", action="store_true")

    get = sub.add_parser("get", help="page or block projection (slug, slug#block:…, block:…)")
    get.add_argument("selector")
    get.add_argument("--block", help="block id within the selected page")
    get.add_argument("--field", action="append", help="descend one key or array index (repeatable)")
    get.add_argument("--pointer", help="RFC 6901 JSON pointer")
    get.add_argument("--fixtures", action="store_true")

    ren = sub.add_parser("render", help="render a page, block or section")
    ren.add_argument("selector")
    ren.add_argument("--format", choices=["md", "html", "json"], default="md")
    ren.add_argument("--exact", action="store_true", help="replay the ingested markdown snapshot")
    ren.add_argument("--section", help="heading block id: render that section only")
    ren.add_argument("--fixtures", action="store_true")

    out = sub.add_parser("outline", help="list heading blocks (cheap section index)")
    out.add_argument("selector")
    out.add_argument("--fixtures", action="store_true")

    log = sub.add_parser("log", help="append to or read wiki/log.jsonl")
    log.add_argument("--action")
    log.add_argument("--page")
    log.add_argument("--note")
    log.add_argument("--show", type=int, default=10)
    return parser


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    ws = Workspace(args.root, getattr(args, "fixtures", False))
    command = args.command

    if command == "ingest":
        emit(ingest(ws, Path(args.input), args.type, args.project, args.summary,
                    args.update, args.dry_run))
    elif command == "build":
        emit(build(ws, changed=args.changed, full=args.full, heading_paths=args.heading_paths))
    elif command in {"validate", "lint"}:
        errors, warnings = lint(ws)
        if command == "validate":
            warnings = []
        if getattr(args, "as_json", False):
            emit({"errors": errors, "warnings": warnings})
        else:
            for item in errors:
                print("ERROR", item)
            for item in warnings:
                print("WARN", item)
            print(f"{len(errors)} errors, {len(warnings)} warnings")
        return 1 if errors else 0
    elif command == "query":
        for row in query(ws, args.query, args.limit):
            print(json.dumps(row, ensure_ascii=False))
    elif command == "get":
        selector = f"{args.selector}#{args.block}" if args.block else args.selector
        page, block = resolve(ws, selector)
        value: Any = block if block else page
        if args.pointer:
            value = json_pointer(value, args.pointer)
        value = pick_fields(value, args.field or [])
        emit(value)
    elif command == "render":
        page, block = resolve(ws, args.selector)
        blocks = [block] if block else None
        if args.section:
            blocks = section(page, args.section)
        if args.format == "json":
            emit(blocks if blocks else page)
        elif args.format == "html":
            print("".join(render_block_html(b) for b in blocks) if blocks else render_html(page))
        elif blocks:
            print("\n\n".join(b["source_text"] for b in blocks))
        else:
            print(render_markdown(page, args.exact), end="")
    elif command == "outline":
        page, _ = resolve(ws, args.selector)
        emit(outline(page))
    elif command == "log":
        if args.action:
            emit(append_log(ws, {k: v for k, v in
                                 (("action", args.action), ("page_id", args.page), ("note", args.note))
                                 if v}))
        else:
            for row in read_log(ws, args.show):
                print(json.dumps(row, ensure_ascii=False))
    return 0


def main() -> None:
    try:
        raise SystemExit(run())
    except (WikiError, FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()

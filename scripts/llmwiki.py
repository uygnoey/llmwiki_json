#!/usr/bin/env python3
"""llmwiki_json CLI.

JSON canonical knowledge wiki: ingest, deterministic projection (build),
direct page/block get, md/html render, lint and append-only log.

Determinism contract
--------------------
`build` is a pure function of (wiki pages, tools/config/groups.json).
Running it twice on the same input produces byte-identical artifacts:
every collection is emitted in a stable sort order, every path stored in
an artifact is repo-relative, and layout coordinates are rounded.

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
import sys
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

DEFAULT_ROOT = Path(__file__).resolve().parents[1]
ENV_ROOT = "LLMWIKI_ROOT"
ENV_NOW = "LLMWIKI_NOW"

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
SECRET = re.compile(r"(?i)(api[_-]?key|access[_-]?token|password|passwd|secret|connection[_-]?string)"
                    r"\s*[:=]\s*[^\s,;\"']+")
SOURCE_REF = re.compile(r"^(?:page|source|raw):\S+$|^user:\d{4}-\d{2}-\d{2}$")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
BLOCK_KEEP = re.compile(r"[^a-zA-Z0-9가-힣._-]+")

CONFLICT_MARK = "⚠️ 상충"
CURRENT_MARK = "✅ 현행"

# Deterministic radial layout. Project groups occupy adjacent angular sectors,
# while every sector fills the same disk so the complete graph stays circular.
GROUP_ORDER = ("alpha", "beta", "common", "multi", "ungrouped")
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
    def markdown(self) -> Path:
        return self.index / "markdown"

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


def parse_scalar(value: str) -> Any:
    value = value.strip()
    if value.startswith("[") and value.endswith("]"):
        return [norm(x) for x in value[1:-1].split(",") if norm(x)]
    if value in {"null", "~", ""}:
        return None
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    return value.strip("\"'")


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}, text
    meta: dict[str, Any] = {}
    for line in text[4:end].splitlines():
        if ":" in line and not line.startswith((" ", "\t", "-")):
            key, value = line.split(":", 1)
            meta[norm(key)] = parse_scalar(value)
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
    source_refs = [norm(value) for value in (meta.get("sources") or []) if norm(value)]
    source_refs = [value if SOURCE_REF.match(value) else f"source:{value}" for value in source_refs]
    raw_ref = norm(meta.get("raw")) or ws.rel(path)
    inferred_summary = summary_from_blocks(blocks, order)
    stamp = today()
    return {
        "schema_version": "1.0", "id": page_id, "slug": slug, "title": title,
        "type": page_type or meta.get("type") or "source",
        "created": meta.get("created") or stamp, "updated": meta.get("updated") or stamp,
        "tags": meta.get("tags") or [], "projects": projects or meta.get("projects") or [],
        "sources": source_refs, "raw_ref": raw_ref,
        "summary": summary or inferred_summary[:280], "blocks": blocks, "block_order": order,
        "links": page_links(page_id, blocks, order),
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
    projects = list(projects)
    if len(projects) > 1:
        return "multi"
    for key in ("alpha", "beta", "common"):
        if any(p in groups["project"].get(key, {}).get("match", []) for p in projects):
            return key
    return "ungrouped"


def unresolved_count(page: dict[str, Any]) -> int:
    return sum(1 for b in page["blocks"].values()
               if b["kind"] == "conflict" and b.get("resolution", {}).get("status") != "resolved")


def project(ws: Workspace) -> dict[str, Any]:
    """Pure projection: canonical pages -> derived artifacts (no disk writes)."""
    docs = ws.load_documents()
    pages = [page for _, page in docs]
    validator = SchemaValidator.load(ws)
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

    groups = ws.load_groups()
    by_slug = {p["slug"]: p for p in pages}
    incoming: Counter[str] = Counter()
    outgoing: Counter[str] = Counter()
    edges: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for page in pages:
        for link in page["links"]:
            target = by_slug.get(link["target"])
            if not target:
                continue
            key = (page["id"], target["id"], link["kind"])
            if key in seen:
                continue
            seen.add(key)
            outgoing[page["id"]] += 1
            incoming[target["id"]] += 1
            edges.append({"id": f"edge:{sha('|'.join(key), 16)}", "source": page["id"],
                          "target": target["id"], "kind": link["kind"]})
    edges.sort(key=lambda e: (e["source"], e["target"], e["kind"]))

    buckets: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    nodes: list[dict[str, Any]] = []
    for page in pages:
        group = project_group(page["projects"], groups)
        degree = incoming[page["id"]] + outgoing[page["id"]]
        node = {"id": page["id"], "slug": page["slug"], "label": page["title"], "type": page["type"],
                "created": page["created"], "updated": page["updated"],
                "projects": page["projects"], "tags": page["tags"], "group": group,
                "summary": page.get("summary", ""), "incoming": incoming[page["id"]],
                "outgoing": outgoing[page["id"]], "degree": degree,
                "unresolved_conflicts": unresolved_count(page), "orphan": degree == 0,
                "data_url": f"pages/{safe_name(page['id'])}"}
        buckets[group].append(node)
        nodes.append(node)
    # Allocate an angular sector proportional to each group's size. Within a
    # sector, a golden-ratio sequence distributes nodes from the centre to the
    # rim without rings or clumps. The resulting silhouette is a true disk,
    # while adjacent colours still make project groups easy to distinguish.
    ordered_groups = [g for g in GROUP_ORDER if buckets.get(g)]
    ordered_groups.extend(sorted(g for g in buckets if g not in GROUP_ORDER))
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

    catalog = [{k: p.get(k) for k in ("id", "slug", "title", "type", "updated",
                                      "projects", "tags", "sources", "summary")} for p in pages]
    map_pages: dict[str, Any] = {}
    map_blocks: dict[str, Any] = {}
    for rel, page in sorted(docs, key=lambda item: item[1]["id"]):
        # data_url serves the page as a standalone object; the empty RFC 6901
        # pointer selects that object root directly.
        map_pages[page["id"]] = {"source": rel, "pointer": "",
                                 "data_url": f"pages/{safe_name(page['id'])}",
                                 "sha256": sha(canonical(page))}
        for bid in page["block_order"]:
            map_blocks[bid] = {"page_id": page["id"], "pointer": f"/blocks/{bid}",
                               "kind": page["blocks"][bid]["kind"],
                               "data_url": f"pages/{safe_name(page['id'])}"}
    search = [{"id": p["id"], "slug": p["slug"], "title": p["title"], "type": p["type"],
               "summary": p.get("summary", ""),
               "text": " ".join([p["title"], p.get("summary", ""), *p["tags"], *p["projects"],
                                 *(p["blocks"][b]["source_text"] for b in p["block_order"])])}
              for p in pages]
    routes = {key: sorted(n["id"] for n in nodes if n["group"] == key) for key in groups["project"]}

    return {
        "catalog.json": catalog,
        "map.json": {"schema_version": "1.0", "pages": map_pages, "blocks": map_blocks},
        "search.json": search,
        "graph.json": {"schema_version": "1.0", "nodes": nodes, "edges": edges, "groups": groups},
        "routes.json": routes,
        "stats.json": {"pages": len(pages), "blocks": sum(len(p["blocks"]) for p in pages),
                       "edges": len(edges),
                       "unresolved_conflicts": sum(n["unresolved_conflicts"] for n in nodes)},
    }


def build(ws: Workspace) -> dict[str, int]:
    payloads = project(ws)
    pages = ws.load_pages()
    # 뷰어가 폴링으로 갱신을 감지하는 값. 산출물이 실제로 달라질 때만 바뀐다.
    payloads["revision.json"] = {"schema_version": "1.0", "revision": sha(
        "".join(canonical(payloads[name]) for name in sorted(payloads))
        + "".join(canonical(page) for page in pages))}
    for name, data in payloads.items():
        dump(ws.index / name, data, pretty=True)
        dump(ws.public / name, data)
    shard_dir = ws.public / "pages"
    shutil.rmtree(shard_dir, ignore_errors=True)
    shard_dir.mkdir(parents=True, exist_ok=True)
    for page in pages:
        dump(shard_dir / safe_name(page["id"]), page)
    return payloads["stats.json"]


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


def export_markdown(ws: Workspace, out: Path | None = None) -> dict[str, Any]:
    """Rendered Markdown mirror — optional input for a qmd collection.

    Derived output: safe to delete, regenerated wholesale on every run.
    """
    target = Path(out) if out else ws.markdown
    pages = ws.load_pages()
    shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True, exist_ok=True)
    manifest = []
    for page in pages:
        name = safe_name(page["id"]).removesuffix(".json") + ".md"
        text = render_markdown(page)
        (target / name).write_text(text, encoding="utf-8")
        manifest.append({"id": page["id"], "slug": page["slug"], "title": page["title"],
                         "file": name, "sha256": sha(text)})
    dump(target / "manifest.json", {"schema_version": "1.0", "generated_from": ws.rel(ws.pages_dir),
                                    "pages": manifest}, pretty=True)
    return {"pages": len(manifest), "out": ws.rel(target)}


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
    for page in ws.load_pages():
        if selector in {page["id"], page["slug"], page["id"].removeprefix("page:")}:
            return page
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

    for page in pages:
        for link in page.get("links", []):
            if link.get("target") not in by_slug:
                errors.append(f"{page['id']}: missing link target [[{link.get('target')}]]")
        for ref in page.get("sources", []):
            if str(ref).startswith("page:") and str(ref).removeprefix("page:") not in by_slug:
                errors.append(f"{page['id']}: source ref {ref} has no page")
        for bid in page.get("block_order", []):
            block = page["blocks"][bid]
            if block["kind"] == "conflict" and block.get("resolution", {}).get("status") != "resolved":
                warnings.append(f"{page['id']}:{bid}: unresolved conflict")
        if not page.get("summary"):
            warnings.append(f"{page['id']}: empty summary")

    degree: Counter[str] = Counter()
    for page in pages:
        for link in page.get("links", []):
            if link.get("target") in by_slug:
                degree[page["slug"]] += 1
                degree[link["target"]] += 1
    for page in pages:
        if page.get("type") not in UNLINKED_TYPES and degree[page.get("slug")] == 0:
            warnings.append(f"{page['id']}: orphan (no inbound or outbound links)")

    warnings.extend(stale_index(ws))
    return sorted(dict.fromkeys(errors)), sorted(dict.fromkeys(warnings))


def stale_index(ws: Workspace) -> list[str]:
    """Compare index/map.json against a fresh projection without writing anything."""
    path = ws.index / "map.json"
    if not path.exists():
        return ["index/map.json missing — run build"]
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
        fresh = project(ws)["map.json"]
    except (WikiError, json.JSONDecodeError):
        return ["index/map.json unreadable — run build"]
    if canonical(current) != canonical(fresh):
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
        page["created"] = prior.get("created", page["created"])
        page["history"] = [*prior.get("history", []), *page["history"]]
        dest = existing

    result = {"page_id": page["id"], "dest": ws.rel(dest), "blocks": len(page["blocks"]),
              "updated": bool(existing), "dry_run": dry_run}
    if dry_run:
        return result

    dump(dest, page, pretty=True)
    if sha(path.read_bytes().hex()) != before:
        raise WikiError(f"source {ws.rel(path)} changed during ingest — raw/ must stay immutable")
    append_log(ws, {"action": "ingest", "page_id": page["id"],
                    "source": ws.rel(path), "dest": ws.rel(dest),
                    "mode": "update" if existing else "create"})
    return result


# --------------------------------------------------------------------------- search
def query(ws: Workspace, text: str, limit: int = 10) -> list[dict[str, Any]]:
    """Scores straight off the canonical pages — never depends on a stale index."""
    terms = [norm(t).lower() for t in text.split() if norm(t)]
    rows = project(ws)["search.json"]
    scored: list[tuple[int, dict[str, Any]]] = []
    for row in rows:
        title, summary, body = row["title"].lower(), row["summary"].lower(), row["text"].lower()
        score = sum(8 if t in title else 2 if t in summary else 1 if t in body else 0 for t in terms)
        if score:
            scored.append((score, row))
    scored.sort(key=lambda item: (-item[0], item[1]["title"], item[1]["id"]))
    return [{"score": score, "id": row["id"], "slug": row["slug"], "title": row["title"],
             "summary": row["summary"]} for score, row in scored[:limit]]


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

    for name, help_text in (("build", "regenerate index/ and viewer public data"),
                            ("validate", "schema + structural checks"),
                            ("lint", "links, conflicts, orphans, stale index")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--fixtures", action="store_true")
        if name != "build":
            p.add_argument("--json", action="store_true", dest="as_json")

    q = sub.add_parser("query", help="rank pages by term overlap")
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

    exp = sub.add_parser("export-md", help="write rendered Markdown for optional qmd indexing")
    exp.add_argument("--out")
    exp.add_argument("--fixtures", action="store_true")

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
        emit(build(ws))
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
    elif command == "export-md":
        emit(export_markdown(ws, Path(args.out) if args.out else None))
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

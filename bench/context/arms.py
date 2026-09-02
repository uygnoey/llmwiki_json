#!/usr/bin/env python3
"""컨텍스트 페이로드 arm — 질문 하나에 대해 "LLM 에 넣을 문자열" 을 만든다.

arm 마다 `Payload(text, manifest)` 를 돌려준다. manifest 는 페이로드 안에 무엇이
어떤 상태로 들어갔는지를 arm 스스로 적은 목록이고, harness 는 이 목록과 본문
문자열 검사를 **둘 다** 써서 지표를 낸다(manifest 가 거짓말을 못 하게).

arm
  production   scripts.llmwiki_context.build_context 그대로 .
  v2-text      structural2 상위 5 page 의 근거 block 을 production render 형식으로.
  v2-graph     JSON 네이티브 부분 그래프를 축약 텍스트로 (block 단위 예산 채움).
  v2-graph-json  v2-graph 와 같은 내용을 compact JSON 으로 (형식 비교용).
  v2-address   주소만 (page/block id + 한 줄) — 2단계 전략의 1단계.

정본·scripts 는 읽기만 한다. v2 arm 은 bench/index_ctx/ 의 색인만 읽는다:
  structural2/structural2.db  검색 (bench/rankers/structural2.py 가 build)
  ctx.sqlite                  page/block 투영 (이 파일의 CtxIndex 가 build)
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rankers.base import load_pages
from rankers.structural2 import Structural2Ranker

from scripts import llmwiki_context as C

REPO_ROOT = Path(__file__).resolve().parents[2]

# production 과 같은 규모의 상수
PAGES_TEXT = C.MAX_PAGES            # 5
BLOCK_CHARS = C.MAX_BLOCK_CHARS     # 320
PAGES_GRAPH = 10
BLOCK_CHARS_GRAPH = 320
ADDR_LINE_CHARS = 60
CURATED = ("related", "supersedes")


@dataclass
class Entry:
    page_id: str
    block_id: str = ""          # "" 이면 page 머리만
    body: bool = False          # block 본문이 실렸는가
    status: str = "none"        # none | cur | conflict | superseded | address


@dataclass
class Payload:
    text: str
    manifest: list[Entry] = field(default_factory=list)
    reason: str = ""


def _slug(page_id: str) -> str:
    return page_id[5:] if page_id.startswith("page:") else page_id


def _tail(block_id: str, slug: str) -> str:
    """block:<slug>:<tail> → <tail>. llmwiki_get 의 resolve_blocks 가 꼬리만 줘도 푼다."""
    prefix = f"block:{slug}:"
    return block_id[len(prefix):] if block_id.startswith(prefix) else block_id


# --------------------------------------------------------------------------- ctx index
class CtxIndex:
    """search.sqlite 가 가져야 할 투영용 표. 정본을 한 번 읽어 sqlite 로 굽는다."""

    SCHEMA = """
    PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;
    CREATE TABLE page(page_id TEXT PRIMARY KEY, slug TEXT, title TEXT, type TEXT, updated TEXT,
                      projects TEXT, tags TEXT, sources TEXT, summary TEXT, head TEXT, file TEXT,
                      unresolved INTEGER) WITHOUT ROWID;
    CREATE TABLE blk(block_id TEXT PRIMARY KEY, page_id TEXT, kind TEXT, pos INTEGER, text TEXT,
                     unresolved INTEGER, refs TEXT) WITHOUT ROWID;
    CREATE TABLE edge(src_block TEXT, kind TEXT, dst_page TEXT, dst_block TEXT);
    CREATE INDEX edge_src ON edge(src_block);
    """

    def __init__(self, db: sqlite3.Connection):
        self.db = db

    @classmethod
    def build(cls, corpus_dir: Path, index_dir: Path) -> dict[str, Any]:
        t0 = time.perf_counter()
        index_dir = Path(index_dir)
        index_dir.mkdir(parents=True, exist_ok=True)
        dbp = index_dir / "ctx.sqlite"
        if dbp.exists():
            dbp.unlink()
        corpus_dir = Path(corpus_dir).resolve()
        pages = sorted(load_pages(corpus_dir), key=lambda p: str(p["id"]))
        by_key: dict[str, dict[str, Any]] = {}
        for p in pages:
            by_key[str(p["id"])] = p
            by_key.setdefault(str(p.get("slug") or ""), p)
        # 파일 경로: 정본이면 wiki/<sub>/<slug>.json 이다.
        files: dict[str, str] = {}
        for path in sorted(corpus_dir.rglob("*.json")):
            try:
                pid = json.loads(path.read_text(encoding="utf-8")).get("id")
            except (OSError, json.JSONDecodeError, AttributeError):
                continue
            if pid:
                files[str(pid)] = "wiki/" + path.relative_to(corpus_dir).as_posix()

        def resolve(target: str) -> dict[str, Any] | None:
            return by_key.get(target) or by_key.get(_slug(target)) or by_key.get("page:" + target)

        succ: dict[str, str] = {}
        edges: list[tuple[str, str, str, str]] = []
        owner: dict[tuple[str, str], str] = {}     # (src page, dst page) → src 위의 block
        for p in pages:
            for link in p.get("links") or []:
                if not isinstance(link, dict):
                    continue
                dst = resolve(str(link.get("target") or ""))
                if not dst or dst["id"] == p["id"]:
                    continue
                kind = str(link.get("kind") or "wiki")
                bid = str(link.get("block_id") or "")
                owner.setdefault((p["id"], dst["id"]), bid)
                edges.append((bid, kind, dst["id"], p["id"]))
                if kind == "supersedes":
                    succ.setdefault(dst["id"], p["id"])
        head: dict[str, str] = {}
        for start in succ:
            cur, guard = start, 0
            while cur in succ and guard < 32:
                cur, guard = succ[cur], guard + 1
            head[start] = cur

        page_rows, blk_rows, edge_rows = [], [], []
        for p in pages:
            blocks = p.get("blocks") or {}
            unresolved = 0
            for pos, bid in enumerate(p.get("block_order") or list(blocks)):
                b = blocks.get(bid)
                if not isinstance(b, dict):
                    continue
                bad = int(C.unresolved(b))
                unresolved += bad
                blk_rows.append((bid, p["id"], str(b.get("kind") or ""), pos,
                                 C.block_text(b), bad, json.dumps(list(b.get("refs") or []))))
            page_rows.append((p["id"], p.get("slug"), p.get("title"), p.get("type"), p.get("updated"),
                              ",".join(p.get("projects") or []), ",".join(p.get("tags") or []),
                              ",".join(p.get("sources") or []), p.get("summary") or "",
                              head.get(p["id"], p["id"]), files.get(p["id"], ""), unresolved))
        for bid, kind, dst, src in edges:
            edge_rows.append((bid, kind, dst, owner.get((dst, src), "")))

        db = sqlite3.connect(dbp)
        db.executescript(cls.SCHEMA)
        db.executemany("INSERT INTO page VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", page_rows)
        db.executemany("INSERT INTO blk VALUES(?,?,?,?,?,?,?)", blk_rows)
        db.executemany("INSERT INTO edge VALUES(?,?,?,?)", edge_rows)
        db.commit()
        db.execute("VACUUM")
        db.close()
        return {"elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
                "bytes": dbp.stat().st_size, "pages": len(page_rows), "blocks": len(blk_rows),
                "edges": len(edge_rows), "superseded_pages": len(head)}

    @classmethod
    def load(cls, index_dir: Path) -> "CtxIndex":
        dbp = Path(index_dir) / "ctx.sqlite"
        return cls(sqlite3.connect(f"file:{dbp}?mode=ro", uri=True, check_same_thread=False))

    def pages(self, ids: list[str]) -> dict[str, dict[str, Any]]:
        if not ids:
            return {}
        cols = ("page_id", "slug", "title", "type", "updated", "projects", "tags", "sources",
                "summary", "head", "file", "unresolved")
        rows = self.db.execute("SELECT * FROM page WHERE page_id IN (%s)" % ",".join("?" * len(ids)), ids)
        return {r[0]: dict(zip(cols, r)) for r in rows}

    def blocks(self, ids: list[str]) -> dict[str, dict[str, Any]]:
        if not ids:
            return {}
        cols = ("block_id", "page_id", "kind", "pos", "text", "unresolved", "refs")
        rows = self.db.execute("SELECT * FROM blk WHERE block_id IN (%s)" % ",".join("?" * len(ids)), ids)
        return {r[0]: dict(zip(cols, r)) for r in rows}

    def edges(self, block_ids: list[str]) -> list[tuple[str, str, str, str]]:
        if not block_ids:
            return []
        return list(self.db.execute(
            "SELECT src_block,kind,dst_page,dst_block FROM edge WHERE src_block IN (%s) ORDER BY src_block,kind,dst_page"
            % ",".join("?" * len(block_ids)), block_ids))


# --------------------------------------------------------------------------- production
class ProductionArm:
    """scripts/llmwiki_context.py 의 build_context 를 그대로 부른다.

    root 는 `<root>/wiki/**/*.json` 모양이어야 한다(harness 가 하드링크 뷰를 만든다).
    build_context 는 예산과 무관하게 정본을 한 번 스캔하므로, 한 번 부르고 그 결과
    (Result, pages projection) 를 여러 예산에 재사용한다 — build_context 내부가
    하는 일과 같다(render 만 예산을 본다).
    """
    name = "production"

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.prod_root = str(REPO_ROOT)

    def prepare(self, query: str) -> tuple[Any, list[dict[str, Any]], str, float]:
        t0 = time.perf_counter()
        text, result, pages = C.build_context(self.root, query, max_bytes=C.MAX_BYTES, max_tokens=C.MAX_TOKENS)
        ms = (time.perf_counter() - t0) * 1000
        return result, pages, text, ms

    def render(self, result: Any, pages: list[dict[str, Any]], budget: int,
               text6000: str = "") -> Payload:
        if budget == C.MAX_BYTES and text6000:
            text = text6000
        elif result.reason.startswith("hint"):
            text = C.render_hint(result, pages, max_bytes=budget, max_tokens=C.MAX_TOKENS)
        else:
            text = C.render(result, pages, max_bytes=budget, max_tokens=C.MAX_TOKENS)
        # 벤치 뷰 경로를 실제 저장소 경로로 바꿔 바이트를 프로덕션과 같게 센다.
        text = text.replace(str(self.root), self.prod_root)
        manifest: list[Entry] = []
        for page in pages:
            if f"### {page['id']} — " in text:
                manifest.append(Entry(page["id"], "", False, "none"))
                for b in page["blocks"]:
                    if f"[{b['id']}]" in text:
                        manifest.append(Entry(page["id"], b["id"], True, "none"))
            elif f"- {page['id']} (" in text:   # hint 경로
                manifest.append(Entry(page["id"], "", False, "address"))
        return Payload(text, manifest, result.reason)


# --------------------------------------------------------------------------- v2 공통
class V2Base:
    def __init__(self, ranker: Structural2Ranker, ctx: CtxIndex, **opts: Any):
        self.ranker = ranker
        self.ctx = ctx
        # cut: 1위 점수 대비 이 비율 아래인 page 는 싣지 않는다 (0 = 끔). structural2 점수는
        # 어휘 1위가 1.0 인 상대값이라 비율 컷이 코퍼스 크기와 무관하게 뜻이 같다.
        self.cut = float(opts.get("cut", 0.0))
        self.k = int(opts.get("k", 0))

    def fetch(self, query: str, k: int) -> tuple[list[Any], dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
        hits = self.ranker.search(query, k=self.k or k)
        if hits and self.cut > 0:
            top = hits[0].score
            hits = [h for h in hits if h.score >= self.cut * top]
        pages = self.ctx.pages([h.page_id for h in hits])
        blocks = self.ctx.blocks([b for h in hits for b in h.block_ids])
        return hits, pages, blocks

    @staticmethod
    def status(page: dict[str, Any], blk: dict[str, Any] | None) -> str:
        if page["head"] != page["page_id"]:
            return "superseded"
        if blk and blk["unresolved"]:
            return "conflict"
        return "cur"


class V2TextArm(V2Base):
    """검색만 structural2 로 바꾸고 형식은 production render 그대로."""
    name = "v2-text"

    def run(self, query: str, budget: int) -> Payload:
        hits, pages, blocks = self.fetch(query, PAGES_TEXT)
        if not hits:
            return Payload("", [], "no-match")
        result = C.Result(query, REPO_ROOT, [], {}, [], 0.0, "structural2")
        proj: list[dict[str, Any]] = []
        for h in hits:
            p = pages.get(h.page_id)
            if not p:
                continue
            bl = []
            for bid in h.block_ids:
                b = blocks.get(bid)
                if not b:
                    continue
                bl.append({"id": bid, "kind": b["kind"],
                           "text": C.clip(C.redact(b["text"]), BLOCK_CHARS),
                           "refs": json.loads(b["refs"]),
                           "resolution": ("unresolved" if b["unresolved"] else "resolved")
                           if b["kind"] == "conflict" else None,
                           "flagged": bool(b["unresolved"])})
            proj.append({"id": p["page_id"], "slug": p["slug"], "title": p["title"], "type": p["type"],
                         "updated": p["updated"], "projects": p["projects"].split(",") if p["projects"] else [],
                         "tags": p["tags"].split(",") if p["tags"] else [],
                         "summary": C.clip(C.redact(p["summary"]), 240),
                         "sources": p["sources"].split(",") if p["sources"] else [],
                         "raw_ref": None, "file": p["file"], "abs_file": "",
                         "score": round(h.score, 2), "via": "structural2",
                         "unresolved_conflicts": p["unresolved"], "blocks": bl})
        text = C.render(result, proj, max_bytes=budget, max_tokens=10**9)
        manifest: list[Entry] = []
        for page in proj:
            if f"### {page['id']} — " in text:
                manifest.append(Entry(page["id"], "", False, "none"))
                for b in page["blocks"]:
                    if f"[{b['id']}]" in text:
                        manifest.append(Entry(page["id"], b["id"], True, "none"))
        return Payload(text, manifest, "structural2")


GRAPH_HEAD = (
    "<llmwiki-context v=2>\n"
    "정본(wiki/**/*.json) 부분 그래프. P=page(slug type updated src=근거 sources) "
    "B=block(<slug>#<id> 상태 | 본문) E=간선(block kind→대상). "
    "상태 cur=현재 주장, conflict=미판정 상충(양쪽 병기), sup→X=X 로 대체된 낡은 page(본문 생략, 인용 금지). "
    "근거 밖 내용은 모른다고 답하라. 더 필요하면 llmwiki_get(selector=\"<slug>#<id>\").\n")
GRAPH_TAIL = "</llmwiki-context>"


class V2GraphArm(V2Base):
    """JSON 네이티브 부분 그래프. block 단위로 예산을 채운다.

    바이트를 줄이는 선택과 근거:
    - page 머리는 slug·type·updated·sources 만. title/summary/tags/projects/score/file 은
      질문에 답하는 데 쓰이지 않는다(제목은 slug 로 llmwiki_get 이 풀고, file 은
      map.json 이 안다). 상충 건수는 block 상태로 대신한다.
    - block 주소는 <slug>#<tail>. block id 의 `block:<slug>:` 접두는 page 줄에서 복원된다.
    - 간선은 related/supersedes 만 E 줄로. wiki 링크는 본문 [[…]] 에 이미 있다.
    - 낡은 page 는 P 줄 하나(sup→head)만. 본문을 싣지 않으므로 낡은 주장이 섞일 수 없다.
    """
    name = "v2-graph"
    fmt = "text"

    def lines(self, query: str) -> tuple[list[tuple[str, Entry, dict[str, Any]]], str]:
        hits, pages, blocks = self.fetch(query, PAGES_GRAPH)
        if not hits:
            return [], "no-match"
        edges = self.ctx.edges([b for h in hits for b in h.block_ids])
        by_src: dict[str, list[tuple[str, str, str]]] = {}
        for src, kind, dst, dstb in edges:
            if kind in CURATED:
                by_src.setdefault(src, []).append((kind, dst, dstb))
        dst_pages = self.ctx.pages(sorted({d for lst in by_src.values() for _k, d, _b in lst if d not in pages}))
        pages = {**dst_pages, **pages}
        out: list[tuple[str, Entry, dict[str, Any]]] = []
        for h in hits:
            p = pages.get(h.page_id)
            if not p:
                continue
            slug = p["slug"]
            if p["head"] != p["page_id"]:
                hp = pages.get(p["head"]) or self.ctx.pages([p["head"]]).get(p["head"])
                head_slug = hp["slug"] if hp else _slug(p["head"])
                out.append((f"P {slug} {p['type']} {p['updated']} sup→{head_slug}",
                            Entry(p["page_id"], "", False, "superseded"),
                            {"p": slug, "type": p["type"], "updated": p["updated"], "sup": head_slug}))
                continue
            src = f" src={p['sources']}" if p["sources"] else ""
            out.append((f"P {slug} {p['type']} {p['updated']}{src}",
                        Entry(p["page_id"], "", False, "cur"),
                        {"p": slug, "type": p["type"], "updated": p["updated"],
                         "src": p["sources"].split(",") if p["sources"] else []}))
            for bid in h.block_ids:
                b = blocks.get(bid)
                if not b:
                    continue
                st = self.status(p, b)
                body = C.clip(C.redact(b["text"]), BLOCK_CHARS_GRAPH)
                addr = f"{slug}#{_tail(bid, slug)}"
                out.append((f"B {addr} {st} | {body}", Entry(p["page_id"], bid, True, st),
                            {"b": addr, "st": st, "t": body}))
                for kind, dst, dstb in by_src.get(bid, []):
                    dp = pages.get(dst)
                    dslug = dp["slug"] if dp else _slug(dst)
                    target = f"{dslug}#{_tail(dstb, dslug)}" if dstb else dslug
                    out.append((f"E {addr} {kind}→{target}", Entry(p["page_id"], bid, False, "edge"),
                                {"e": [addr, kind, target]}))
        return out, "structural2"

    def run(self, query: str, budget: int) -> Payload:
        items, reason = self.lines(query)
        if not items:
            return Payload("", [], reason)
        if self.fmt == "json":
            return self._json(items, budget, reason)
        # page 단위로 묶되 채움은 block 단위. block 이 하나도 못 들어가는 page 의 머리(P) 는
        # 예산만 먹으므로 싣지 않는다. 낡은 page 의 P 줄은 그 자체가 정보라 남긴다.
        groups: list[list[tuple[str, Entry]]] = []
        for line, entry, _obj in items:
            if line.startswith("P "):
                groups.append([])
            groups[-1].append((line, entry))
        used = len(GRAPH_HEAD.encode("utf-8")) + len(GRAPH_TAIL.encode("utf-8"))
        kept: list[str] = []
        manifest: list[Entry] = []
        for group in groups:
            head_line, head_entry = group[0]
            head_size = len(head_line.encode("utf-8")) + 1
            if used + head_size > budget:
                continue
            chosen: list[tuple[str, Entry]] = []
            sub = head_size
            for line, entry in group[1:]:
                size = len(line.encode("utf-8")) + 1
                if used + sub + size > budget:
                    continue
                chosen.append((line, entry))
                sub += size
            if not chosen and head_entry.status != "superseded":
                continue
            kept.append(head_line)
            manifest.append(head_entry)
            used += sub
            for line, entry in chosen:
                kept.append(line)
                if entry.status != "edge":
                    manifest.append(entry)
        text = GRAPH_HEAD + "\n".join(kept) + "\n" + GRAPH_TAIL
        return Payload(text, manifest, reason)

    def _json(self, items: list[tuple[str, Entry, dict[str, Any]]], budget: int, reason: str) -> Payload:
        head = {"v": 2, "legend": "P page,B block(st: cur|conflict|superseded),E edge; sup=대체된 낡은 page(본문 없음)"}
        objs: list[dict[str, Any]] = []
        manifest: list[Entry] = []
        for _line, entry, obj in items:
            trial = json.dumps({**head, "items": objs + [obj]}, ensure_ascii=False, separators=(",", ":"))
            if len(trial.encode("utf-8")) > budget:
                continue
            objs.append(obj)
            if entry.status != "edge":
                manifest.append(entry)
        text = json.dumps({**head, "items": objs}, ensure_ascii=False, separators=(",", ":"))
        return Payload(text, manifest, reason)


class V2GraphJsonArm(V2GraphArm):
    name = "v2-graph-json"
    fmt = "json"


ADDR_HEAD = (
    "<llmwiki-context v=2 addr>\n"
    "정본에서 이 질문과 겹치는 block 주소만. 본문은 없다 — 답을 담고 있다고 가정하지 마라. "
    "sup→X 는 X 로 대체된 낡은 page. 필요한 것만 llmwiki_get(selector=\"<slug>#<id>\") 로 가져와라.\n")


class V2AddressArm(V2Base):
    """주소만. 2단계 전략의 1단계 비용."""
    name = "v2-address"

    def run(self, query: str, budget: int) -> Payload:
        hits, pages, blocks = self.fetch(query, PAGES_GRAPH)
        if not hits:
            return Payload("", [], "no-match")
        rows: list[tuple[str, Entry]] = []
        for h in hits:
            p = pages.get(h.page_id)
            if not p:
                continue
            slug = p["slug"]
            if p["head"] != p["page_id"]:
                hp = self.ctx.pages([p["head"]]).get(p["head"])
                rows.append((f"- {slug} sup→{hp['slug'] if hp else _slug(p['head'])}",
                             Entry(p["page_id"], "", False, "superseded")))
                continue
            summary = C.clip(f"{p['title']}: {p['summary']}", ADDR_LINE_CHARS)
            addrs = []
            for bid in h.block_ids:
                b = blocks.get(bid)
                if b:
                    addrs.append(f"{slug}#{_tail(bid, slug)}" + (" conflict" if b["unresolved"] else ""))
            rows.append((f"- {slug} ({p['type']} {p['updated']}) {summary} → " + ", ".join(addrs),
                         Entry(p["page_id"], "", False, "address")))
            for bid in h.block_ids:
                if bid in blocks:
                    rows.append(("", Entry(p["page_id"], bid, False, "address")))
        used = len(ADDR_HEAD.encode("utf-8")) + len(GRAPH_TAIL.encode("utf-8"))
        kept: list[str] = []
        manifest: list[Entry] = []
        skipping = False
        for line, entry in rows:
            if line:
                size = len(line.encode("utf-8")) + 1
                skipping = used + size > budget
                if skipping:
                    continue
                kept.append(line)
                used += size
                manifest.append(entry)
            elif not skipping:
                manifest.append(entry)
        return Payload(ADDR_HEAD + "\n".join(kept) + "\n" + GRAPH_TAIL, manifest, "structural2")


def stage2_bytes(page: dict[str, Any], block_ids: list[str]) -> int:
    """llmwiki_get(selector=slug, blocks=[…]) 이 돌려주는 JSON 의 바이트 (mcp_call 과 같은 indent=2)."""
    found, _missing = C.resolve_blocks(page, block_ids)
    out = {"id": page["id"], "slug": page["slug"], "title": C.norm(page.get("title")),
           "file": "", "blocks": [C.block_view(b) for b in found]}
    return len(json.dumps(out, ensure_ascii=False, indent=2).encode("utf-8"))

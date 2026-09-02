#!/usr/bin/env python3
"""CONTEXT_REPORT 적대적 검증용 교차 arm.

프로덕션/벤치 원본은 수정하지 않고 기존 읽기 전용 색인을 재사용한다. 이 파일의
arm 은 검색 결과와 렌더 형식을 교차하거나, supersedes head 주석이 없는 경우를
만들기 위한 리뷰 전용 구현이다.
"""
from __future__ import annotations

import json
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "bench"))
sys.path.insert(0, str(ROOT))

from context.arms import (  # noqa: E402
    BLOCK_CHARS_GRAPH,
    CURATED,
    GRAPH_HEAD,
    GRAPH_TAIL,
    PAGES_GRAPH,
    CtxIndex,
    Entry,
    Payload,
    ProductionArm,
    V2GraphArm,
    _slug,
    _tail,
)
from rankers.structural2 import Structural2Ranker  # noqa: E402
from scripts import llmwiki_context as C  # noqa: E402


def _render_graph_items(items: list[tuple[str, Entry]], budget: int, reason: str) -> Payload:
    """원본 V2GraphArm.run 과 같은 page 묶음/block 단위 byte 채움."""
    if not items:
        return Payload("", [], reason)
    groups: list[list[tuple[str, Entry]]] = []
    for line, entry in items:
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
    if not kept:
        return Payload("", [], reason)
    return Payload(GRAPH_HEAD + "\n".join(kept) + "\n" + GRAPH_TAIL, manifest, reason)


def _production_manifest(text: str, pages: list[dict[str, Any]], reason: str) -> Payload:
    manifest: list[Entry] = []
    for page in pages:
        if f"### {page['id']} — " in text:
            manifest.append(Entry(page["id"], "", False, "none"))
            for block in page["blocks"]:
                if f"[{block['id']}]" in text:
                    manifest.append(Entry(page["id"], block["id"], True, "none"))
        elif f"- {page['id']} (" in text:
            manifest.append(Entry(page["id"], "", False, "address"))
    return Payload(text, manifest, reason)


class ProductionSearchV2FormatArm:
    """정본 production 검색/근거 block을 그대로 두고 v2-graph로만 렌더한다."""

    def __init__(self, root: Path, ctx: CtxIndex):
        self.root = Path(root).resolve()
        self.ctx = ctx
        self.production = ProductionArm(self.root)

    def prepare(self, query: str) -> tuple[Any, list[dict[str, Any]], str, float]:
        """production build_context를 한 번만 실행해 예산별 렌더에 재사용한다."""
        t0 = time.perf_counter()
        text, result, pages = C.build_context(
            self.root,
            query,
            max_bytes=C.MAX_BYTES,
            max_tokens=C.MAX_TOKENS,
        )
        elapsed_ms = (time.perf_counter() - t0) * 1000
        return result, pages, text, elapsed_ms

    def render_three(self, prepared: tuple[Any, list[dict[str, Any]], str, float],
                     budget: int) -> tuple[Payload, Payload, Payload, float]:
        """native, 올바른 root 길이 native, v2 형식과 1회 scan 지연을 돌려준다."""
        result, pages, text, elapsed_ms = prepared
        native = self.production.render(result, pages, budget, text if budget == C.MAX_BYTES else "")

        # 원본 arm은 긴 벤치 root로 예산 결정을 내린 뒤 문자열만 짧은 실제 root로
        # 바꾼다. 이 arm은 예산 계산 전에 실제 production root를 넣어 차이를 잰다.
        corrected_result = replace(result, root=ROOT)
        if corrected_result.reason.startswith("hint"):
            corrected_text = C.render_hint(
                corrected_result, pages, max_bytes=budget, max_tokens=C.MAX_TOKENS
            )
        else:
            corrected_text = C.render(
                corrected_result, pages, max_bytes=budget, max_tokens=C.MAX_TOKENS
            )
        corrected = _production_manifest(corrected_text, pages, result.reason)
        cross = self._v2_payload(pages, budget, result.reason)
        return native, corrected, cross, elapsed_ms

    def run_three(self, query: str, budget: int) -> tuple[Payload, Payload, Payload, float]:
        return self.render_three(self.prepare(query), budget)

    def _v2_payload(self, pages: list[dict[str, Any]], budget: int, reason: str) -> Payload:
        if not pages or reason.startswith("hint"):
            return Payload("", [], reason)
        page_rows = self.ctx.pages([str(page["id"]) for page in pages])
        block_ids = [str(block["id"]) for page in pages for block in page["blocks"]]
        edges = self.ctx.edges(block_ids)
        by_src: dict[str, list[tuple[str, str, str]]] = {}
        for src, kind, dst, dst_block in edges:
            if kind in CURATED:
                by_src.setdefault(src, []).append((kind, dst, dst_block))
        extra = self.ctx.pages(sorted({dst for rows in by_src.values() for _k, dst, _b in rows}))
        all_pages = {**extra, **page_rows}
        items: list[tuple[str, Entry]] = []
        for page in pages:
            pid = str(page["id"])
            indexed = page_rows.get(pid)
            if not indexed:
                continue
            slug = str(page.get("slug") or _slug(pid))
            if indexed["head"] != pid:
                head = all_pages.get(indexed["head"]) or self.ctx.pages([indexed["head"]]).get(indexed["head"])
                head_slug = head["slug"] if head else _slug(indexed["head"])
                items.append((
                    f"P {slug} {page['type']} {page['updated']} sup→{head_slug}",
                    Entry(pid, "", False, "superseded"),
                ))
                continue
            sources = ",".join(str(s) for s in page.get("sources") or [])
            src = f" src={C.redact(sources)}" if sources else ""
            items.append((
                f"P {slug} {page['type']} {page['updated']}{src}",
                Entry(pid, "", False, "cur"),
            ))
            for block in page["blocks"]:
                bid = str(block["id"])
                status = "conflict" if block.get("flagged") else "cur"
                address = f"{slug}#{_tail(bid, slug)}"
                body = C.clip(C.redact(str(block.get("text") or "")), BLOCK_CHARS_GRAPH)
                items.append((
                    f"B {address} {status} | {body}",
                    Entry(pid, bid, True, status),
                ))
                for kind, dst, dst_block in by_src.get(bid, []):
                    dst_page = all_pages.get(dst)
                    dst_slug = dst_page["slug"] if dst_page else _slug(dst)
                    target = f"{dst_slug}#{_tail(dst_block, dst_slug)}" if dst_block else dst_slug
                    items.append((
                        f"E {address} {kind}→{target}",
                        Entry(pid, bid, False, "edge"),
                    ))
        return _render_graph_items(items, budget, reason)


class V2SearchProductionFormatArm:
    """structural2 top-10/cut 검색 결과를 production 마크다운으로 렌더한다."""

    def __init__(self, ranker: Structural2Ranker, ctx: CtxIndex, *, cut: float = 0.5):
        self.ranker = ranker
        self.ctx = ctx
        self.cut = cut

    def run(self, query: str, budget: int) -> Payload:
        hits = self.ranker.search(query, k=PAGES_GRAPH)
        if hits and self.cut > 0:
            top = hits[0].score
            hits = [hit for hit in hits if hit.score >= self.cut * top]
        if not hits:
            return Payload("", [], "no-match")
        pages = self.ctx.pages([hit.page_id for hit in hits])
        blocks = self.ctx.blocks([bid for hit in hits for bid in hit.block_ids])
        projected: list[dict[str, Any]] = []
        for hit in hits:
            page = pages.get(hit.page_id)
            if not page:
                continue
            projected_blocks = []
            for bid in hit.block_ids:
                block = blocks.get(bid)
                if not block:
                    continue
                projected_blocks.append({
                    "id": bid,
                    "kind": block["kind"],
                    "text": C.clip(C.redact(block["text"]), C.MAX_BLOCK_CHARS),
                    "refs": json.loads(block["refs"]),
                    "resolution": (
                        "unresolved" if block["unresolved"] else "resolved"
                    ) if block["kind"] == "conflict" else None,
                    "flagged": bool(block["unresolved"]),
                })
            projected.append({
                "id": page["page_id"],
                "slug": page["slug"],
                "title": page["title"],
                "type": page["type"],
                "updated": page["updated"],
                "projects": page["projects"].split(",") if page["projects"] else [],
                "tags": page["tags"].split(",") if page["tags"] else [],
                "summary": C.clip(C.redact(page["summary"]), 240),
                "sources": page["sources"].split(",") if page["sources"] else [],
                "raw_ref": None,
                "file": page["file"],
                "abs_file": "",
                "score": round(hit.score, 2),
                "via": "structural2",
                "unresolved_conflicts": page["unresolved"],
                "blocks": projected_blocks,
            })
        result = C.Result(query, ROOT, [], {}, [], 0.0, "structural2")
        text = C.render(result, projected, max_bytes=budget, max_tokens=C.MAX_TOKENS)
        return _production_manifest(text, projected, "structural2")


class NoHeadCtx:
    """head 접기를 제거하되 edge와 block은 그대로 노출하는 read-only 뷰."""

    def __init__(self, inner: CtxIndex):
        self.inner = inner

    def pages(self, ids: list[str]) -> dict[str, dict[str, Any]]:
        rows = self.inner.pages(ids)
        return {pid: {**page, "head": pid} for pid, page in rows.items()}

    def blocks(self, ids: list[str]) -> dict[str, dict[str, Any]]:
        return self.inner.blocks(ids)

    def edges(self, block_ids: list[str]) -> list[tuple[str, str, str, str]]:
        return self.inner.edges(block_ids)


class LongBlockV2GraphArm(V2GraphArm):
    """검색 색인은 그대로 두고, 렌더할 본문만 긴 변형 정본으로 교체한다."""

    def __init__(self, ranker: Structural2Ranker, ctx: CtxIndex,
                 long_texts: dict[str, str], **opts: Any):
        super().__init__(ranker, ctx, **opts)
        self.long_texts = long_texts

    def lines(self, query: str) -> tuple[list[tuple[str, Entry, dict[str, Any]]], str]:
        items, reason = super().lines(query)
        replaced: list[tuple[str, Entry, dict[str, Any]]] = []
        for line, entry, obj in items:
            text = self.long_texts.get(entry.block_id)
            if text is None or not line.startswith("B "):
                replaced.append((line, entry, obj))
                continue
            prefix = line.split(" | ", 1)[0]
            clipped = C.clip(C.redact(text), BLOCK_CHARS_GRAPH)
            replaced.append((prefix + " | " + clipped, entry, {**obj, "t": clipped}))
        return replaced, reason


def no_head_graph_arm(ranker: Structural2Ranker, ctx: CtxIndex, *, cut: float = 0.0) -> V2GraphArm:
    return V2GraphArm(ranker, NoHeadCtx(ctx), cut=cut)

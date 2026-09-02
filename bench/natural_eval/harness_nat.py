#!/usr/bin/env python3
"""자연 문서 컨텍스트 평가 하네스.

정답 선택, 앞부분 전달, 실제 answer span 전달을 분리하고, 정답이 없는 질문으로
무주입 신호를 보정한다. 검색/렌더 구현은 제품 함수와 기존 context arm을 그대로
호출하며 이 파일은 정답 oracle을 판정에만 사용한다.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "bench"))
sys.path.insert(0, str(ROOT))

from context import arms as A  # noqa: E402
from rankers import structural2 as S2  # noqa: E402
from rankers.base import load_pages  # noqa: E402
from scripts import llmwiki_context as C  # noqa: E402

BUDGETS = (2000, 4000, 6000)
CUTS = (0.3, 0.5, 0.7)
PREFIX_CHARS = 320
COMPLETENESS_CLIPS: tuple[int | None, ...] = (320, 640, 1000, None)
SIGNALS = (
    "production_top_score",
    "production_coverage",
    "production_matched",
    "structural_raw_top",
    "structural_coverage",
    "structural_top1_top2_ratio",
)


def norm_ws(value: Any) -> str:
    return " ".join(str(value or "").split())


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    vals = sorted(values)
    pos = min(len(vals) - 1, int(round((len(vals) - 1) * p)))
    return round(vals[pos], 6)


def rate(values: list[int]) -> float | None:
    return round(statistics.fmean(values), 4) if values else None


class Oracle:
    """정답 판정 전용 정본 뷰. arm에는 전달하지 않는다."""

    def __init__(self, wiki_dir: Path):
        self.pages: dict[str, dict[str, Any]] = {}
        self.blocks: dict[str, str] = {}
        self.source_blocks: dict[str, str] = {}
        self.page_blocks: dict[str, list[str]] = {}
        for page in load_pages(wiki_dir):
            pid = str(page["id"])
            self.pages[pid] = page
            blocks = page.get("blocks") or {}
            for bid in page.get("block_order") or list(blocks):
                block = blocks.get(bid)
                if not isinstance(block, dict):
                    continue
                text = norm_ws(C.block_text(block))
                self.blocks[str(bid)] = text
                source_text = block.get("source_text")
                self.source_blocks[str(bid)] = norm_ws(
                    source_text if isinstance(source_text, str) else C.block_text(block)
                )
                self.page_blocks.setdefault(pid, []).append(str(bid))


def validate_contract(queries: list[dict[str, Any]], oracle: Oracle) -> None:
    errors: list[str] = []
    for q in queries:
        qid = str(q.get("id") or "<missing-id>")
        expected = bool(q.get("expects_injection"))
        gold_pages = list(q.get("gold_pages") or [])
        gold_blocks = list(q.get("gold_blocks") or [])
        if not expected:
            if gold_pages or gold_blocks or norm_ws(q.get("answer_span")):
                errors.append(f"{qid}: unrelated인데 gold/answer_span이 비어 있지 않다")
            continue
        if not gold_pages or not gold_blocks:
            errors.append(f"{qid}: answerable인데 gold page/block이 없다")
            continue
        for pid in gold_pages:
            if pid not in oracle.pages:
                errors.append(f"{qid}: 없는 gold page {pid}")
        span = norm_ws(q.get("answer_span"))
        if not span:
            errors.append(f"{qid}: answer_span이 비었다")
        for bid in gold_blocks:
            body = oracle.source_blocks.get(bid)
            if body is None:
                errors.append(f"{qid}: 없는 gold block {bid}")
                continue
            offset = body.find(span)
            if offset < 0:
                errors.append(f"{qid}: answer_span이 {bid} 본문에 없다")
            elif offset != int(q.get("span_offset", -1)):
                errors.append(
                    f"{qid}: span_offset={q.get('span_offset')}이나 정규화 본문에서는 {offset}"
                )
        if q.get("type") == "long" and int(q.get("span_offset", -1)) < PREFIX_CHARS:
            errors.append(f"{qid}: long span_offset가 {PREFIX_CHARS} 미만")
    if errors:
        raise ValueError("자연 질문 계약 오류:\n- " + "\n- ".join(errors))


def production_manifest(text: str, pages: list[dict[str, Any]], reason: str) -> A.Payload:
    entries: list[A.Entry] = []
    for page in pages:
        pid = str(page["id"])
        if f"### {pid} — " in text:
            entries.append(A.Entry(pid, "", False, "none"))
            for block in page.get("blocks") or []:
                bid = str(block["id"])
                if f"[{bid}]" in text:
                    entries.append(A.Entry(pid, bid, True, "none"))
        elif f"- {pid} (" in text:
            entries.append(A.Entry(pid, "", False, "address"))
    return A.Payload(text, entries, reason)


class ExactProductionArm:
    """자연 root 경로까지 바꾸지 않는 제품 build_context arm."""

    def __init__(self, root: Path):
        self.root = root

    def prepare(self, query: str) -> tuple[C.Result, list[dict[str, Any]], str, float]:
        t0 = time.perf_counter()
        text, result, pages = C.build_context(
            self.root,
            query,
            max_bytes=C.MAX_BYTES,
            max_tokens=C.MAX_TOKENS,
        )
        return result, pages, text, (time.perf_counter() - t0) * 1000

    def render(self, result: C.Result, pages: list[dict[str, Any]], budget: int,
               text6000: str) -> A.Payload:
        if budget == C.MAX_BYTES:
            text = text6000
        elif result.reason.startswith("hint"):
            text = C.render_hint(result, pages, max_bytes=budget, max_tokens=C.MAX_TOKENS)
        else:
            text = C.render(result, pages, max_bytes=budget, max_tokens=C.MAX_TOKENS)
        return production_manifest(text, pages, result.reason)


def selected_from_pages(pages: list[dict[str, Any]]) -> set[str]:
    return {
        str(block["id"])
        for page in pages
        for block in page.get("blocks") or []
        if block.get("id")
    }


def selected_from_graph_items(items: list[tuple[str, A.Entry, dict[str, Any]]]) -> set[str]:
    return {entry.block_id for _line, entry, _obj in items if entry.body and entry.block_id}


def judge(payload: A.Payload, q: dict[str, Any], oracle: Oracle, selected_ids: set[str],
          latency_ms: float, *, clip_chars: int | None = PREFIX_CHARS) -> dict[str, Any]:
    expected = bool(q.get("expects_injection"))
    gold = list(q.get("gold_blocks") or [])
    flat = norm_ws(payload.text)
    manifested_body = {entry.block_id for entry in payload.manifest if entry.body and entry.block_id}
    selected = bool(gold) and all(bid in selected_ids for bid in gold)
    delivered_prefix = False
    if gold and clip_chars is not None:
        keys = [norm_ws(C.clip(oracle.blocks.get(bid, ""), clip_chars)) for bid in gold]
        delivered_prefix = all(
            bid in manifested_body and key and key in flat for bid, key in zip(gold, keys)
        )
    span = norm_ws(q.get("answer_span"))
    answer_delivered = bool(span) and span in flat

    stale_pages = list(q.get("stale_pages") or [])
    marked = {entry.page_id for entry in payload.manifest if entry.status == "superseded"}
    stale_body = False
    stale_leak = False
    for pid in stale_pages:
        for bid in oracle.page_blocks.get(pid, []):
            key = norm_ws(C.clip(oracle.blocks.get(bid, ""), PREFIX_CHARS))
            if bid in manifested_body and key and key in flat:
                stale_body = True
                stale_leak = stale_leak or pid not in marked

    truncated = bool(
        selected
        and clip_chars is not None
        and int(q.get("span_offset", -1)) >= clip_chars
    )
    return {
        "id": q["id"],
        "type": q["type"],
        "expects_injection": expected,
        "selected": int(selected) if expected else None,
        "delivered_prefix": int(delivered_prefix) if expected else None,
        "answer_delivered": int(answer_delivered) if expected else None,
        "truncated": int(truncated) if expected else None,
        "stale_body": int(stale_body) if stale_pages else None,
        "stale_leak": int(stale_leak) if stale_pages else None,
        "injected": int(bool(payload.text)),
        "payload_bytes": len(payload.text.encode("utf-8")),
        "est_tokens": C.est_tokens(payload.text) if payload.text else 0,
        "latency_ms": round(latency_ms, 4),
        "reason": payload.reason,
    }


def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    answerable = [row for row in rows if row["expects_injection"]]
    unrelated = [row for row in rows if not row["expects_injection"]]
    temporal = [row for row in rows if row["stale_leak"] is not None]
    selected_rows = [row for row in answerable if row["selected"]]
    latencies = [float(row["latency_ms"]) for row in rows]
    byte_values = [int(row["payload_bytes"]) for row in rows]
    return {
        "n": len(rows),
        "answerable_n": len(answerable),
        "unrelated_n": len(unrelated),
        "selected": rate([row["selected"] for row in answerable]),
        "delivered_prefix": rate([row["delivered_prefix"] for row in answerable]),
        "answer_delivered": rate([row["answer_delivered"] for row in answerable]),
        "truncated": rate([row["truncated"] for row in answerable]),
        "truncated_given_selected": rate([row["truncated"] for row in selected_rows]),
        "stale_leak": rate([row["stale_leak"] for row in temporal]),
        "injected_answerable": rate([row["injected"] for row in answerable]),
        "injected_unrelated": rate([row["injected"] for row in unrelated]),
        "payload_bytes_mean": round(statistics.fmean(byte_values), 2) if byte_values else None,
        "payload_bytes_p50": percentile([float(v) for v in byte_values], 0.5),
        "est_tokens_mean": round(statistics.fmean(row["est_tokens"] for row in rows), 2) if rows else None,
        "latency_ms": {
            "p50": percentile(latencies, 0.5),
            "p95": percentile(latencies, 0.95),
            "mean": round(statistics.fmean(latencies), 4) if latencies else None,
        },
    }


def production_signal(root: Path, query: str) -> dict[str, float]:
    # 문턱 적용 전 후보 Result가 필요하다. 제품 retrieve 자체를 문턱 0으로 호출한다.
    result = C.retrieve(
        root,
        query,
        min_score=0.0,
        min_coverage=0.0,
        min_matched=0,
        hint_score=0.0,
        limit=C.MAX_PAGES,
    )
    hit = result.hits[0] if result.hits else None
    raw_matched = len(hit.matched) if hit else 0
    matched = raw_matched if hit and any(not token.isdigit() for token in hit.matched) else 0
    return {
        "production_top_score": float(hit.score if hit else 0.0),
        "production_coverage": float(result.coverage),
        "production_matched": float(matched),
        "production_matched_raw": float(raw_matched),
    }


def structural_signal(ranker: S2.Structural2Ranker, query: str) -> dict[str, float]:
    page_lex, _blocks = ranker._lex(query)  # 후보의 정규화 전 절대 impact
    raw_top = max(page_lex.values()) if page_lex else 0.0
    terms = sorted(set(S2.tokenize(query)))
    matched_terms = 0
    if terms:
        sql = "SELECT count(*) FROM post WHERE term IN (%s)" % ",".join("?" * len(terms))
        matched_terms = int(ranker.db.execute(sql, terms).fetchone()[0])
    hits = ranker.search(query, k=2)
    first = float(hits[0].score) if hits else 0.0
    second = float(hits[1].score) if len(hits) > 1 else 0.0
    # 후보가 하나뿐이면 수학적으로 무한대다. JSON/ROC를 위해 큰 유한 sentinel을 쓴다.
    ratio = first / second if second > 0.0 else (1e9 if first > 0.0 else 0.0)
    return {
        "structural_raw_top": float(raw_top),
        "structural_coverage": matched_terms / len(terms) if terms else 0.0,
        "structural_top1_top2_ratio": float(ratio),
        "structural_top_score": first,
        "structural_second_score": second,
        "structural_query_terms": float(len(terms)),
        "structural_posted_terms": float(matched_terms),
    }


def distribution(values: list[float]) -> dict[str, Any]:
    return {
        "n": len(values),
        "min": round(min(values), 6) if values else None,
        "p25": percentile(values, 0.25),
        "p50": percentile(values, 0.5),
        "p75": percentile(values, 0.75),
        "max": round(max(values), 6) if values else None,
        "mean": round(statistics.fmean(values), 6) if values else None,
    }


def threshold_point(rows: list[dict[str, Any]], signal: str, threshold: float) -> dict[str, Any]:
    pos = [row for row in rows if row["expects_injection"]]
    neg = [row for row in rows if not row["expects_injection"]]
    return {
        "threshold": round(threshold, 9),
        "answerable_injection": rate([int(row[signal] >= threshold) for row in pos]),
        "unrelated_injection": rate([int(row[signal] >= threshold) for row in neg]),
        "answerable_n": len(pos),
        "unrelated_n": len(neg),
    }


def auc_pairwise(rows: list[dict[str, Any]], signal: str) -> float | None:
    pos = [float(row[signal]) for row in rows if row["expects_injection"]]
    neg = [float(row[signal]) for row in rows if not row["expects_injection"]]
    if not pos or not neg:
        return None
    wins = sum(1.0 if p > n else 0.5 if p == n else 0.0 for p in pos for n in neg)
    return round(wins / (len(pos) * len(neg)), 6)


def roc(signal_rows: list[dict[str, Any]], signal: str) -> dict[str, Any]:
    values = sorted({float(row[signal]) for row in signal_rows}, reverse=True)
    max_value = max(values) if values else 0.0
    # 출력 정밀도로 다시 읽어도 최댓값보다 큰 "아무것도 주입하지 않음" 점이어야 한다.
    above_max = max_value + max(1e-9, abs(max_value) * 1e-9)
    points = [threshold_point(signal_rows, signal, above_max)]
    points.extend(threshold_point(signal_rows, signal, value) for value in values)
    feasible = [p for p in points if (p["unrelated_injection"] or 0.0) <= 0.05]
    recommended = max(
        feasible,
        key=lambda p: (
            p["answerable_injection"] or 0.0,
            -(p["unrelated_injection"] or 0.0),
            -p["threshold"],
        ),
    )
    pos = [float(row[signal]) for row in signal_rows if row["expects_injection"]]
    neg = [float(row[signal]) for row in signal_rows if not row["expects_injection"]]
    return {
        "signal": signal,
        "auc": auc_pairwise(signal_rows, signal),
        "answerable": distribution(pos),
        "unrelated": distribution(neg),
        "recommended_at_fpr_lte_0_05": recommended,
        "roc_points": points,
    }


def current_production_gates(signal_rows: list[dict[str, Any]]) -> dict[str, Any]:
    def body(row: dict[str, Any]) -> bool:
        return (
            row["production_top_score"] >= C.MIN_SCORE
            and (
                row["production_coverage"] >= C.MIN_COVERAGE
                or row["production_matched"] >= C.MIN_MATCHED
            )
        )

    def hint(row: dict[str, Any]) -> bool:
        weak = not body(row)
        return (
            weak
            and row["production_matched"] > 0
            and row["production_top_score"] >= C.HINT_SCORE
            and (
                row["production_coverage"] >= C.HINT_COVERAGE
                or row["production_matched"] >= C.HINT_MATCHED
            )
        )

    def gate_rates(predicate: Callable[[dict[str, Any]], bool]) -> dict[str, Any]:
        pos = [row for row in signal_rows if row["expects_injection"]]
        neg = [row for row in signal_rows if not row["expects_injection"]]
        return {
            "answerable_injection": rate([int(predicate(row)) for row in pos]),
            "unrelated_injection": rate([int(predicate(row)) for row in neg]),
        }

    return {
        "constants": {
            "MIN_SCORE": C.MIN_SCORE,
            "MIN_COVERAGE": C.MIN_COVERAGE,
            "MIN_MATCHED": C.MIN_MATCHED,
            "HINT_SCORE": C.HINT_SCORE,
            "HINT_COVERAGE": C.HINT_COVERAGE,
            "HINT_MATCHED": C.HINT_MATCHED,
            "HINT_PAGES": C.HINT_PAGES,
            "HINT_SUMMARY_CHARS": C.HINT_SUMMARY_CHARS,
        },
        "body_gate": gate_rates(body),
        "hint_only_gate": gate_rates(hint),
        "any_payload_gate": gate_rates(lambda row: body(row) or hint(row)),
        "individual_threshold_positions": [
            {"signal": "production_top_score", "name": "MIN_SCORE", **threshold_point(signal_rows, "production_top_score", C.MIN_SCORE)},
            {"signal": "production_top_score", "name": "HINT_SCORE", **threshold_point(signal_rows, "production_top_score", C.HINT_SCORE)},
            {"signal": "production_coverage", "name": "MIN_COVERAGE", **threshold_point(signal_rows, "production_coverage", C.MIN_COVERAGE)},
            {"signal": "production_coverage", "name": "HINT_COVERAGE", **threshold_point(signal_rows, "production_coverage", C.HINT_COVERAGE)},
            {"signal": "production_matched", "name": "MIN_MATCHED", **threshold_point(signal_rows, "production_matched", float(C.MIN_MATCHED))},
            {"signal": "production_matched", "name": "HINT_MATCHED", **threshold_point(signal_rows, "production_matched", float(C.HINT_MATCHED))},
        ],
    }


def dense_window(text: str, query: str, limit: int) -> str:
    """정답을 보지 않고 질문 토큰이 가장 조밀한 본문 구간을 고른다."""
    text = norm_ws(text)
    if len(text) <= limit:
        return text
    search_text = text.lower()
    terms = sorted(set(S2.tokenize(query)))
    max_start = len(text) - limit
    candidates = {0, max_start}
    for term in terms:
        start = 0
        while True:
            pos = search_text.find(term, start)
            if pos < 0:
                break
            candidates.update({
                min(max_start, max(0, pos)),
                min(max_start, max(0, pos - limit // 2)),
                min(max_start, max(0, pos - limit + len(term))),
            })
            start = pos + 1

    def score(begin: int) -> tuple[int, int, int, int]:
        window = search_text[begin:begin + limit]
        unique = sum(1 for term in terms if term in window)
        occurrences = sum(window.count(term) for term in terms)
        # 같은 토큰 수라면 경계에서 잘릴 위험이 작은, 토큰이 안쪽에 있는 창을 택한다.
        margin = 0
        for term in terms:
            pos = window.find(term)
            if pos >= 0:
                margin += max(0, min(pos, limit - pos - len(term)))
        return unique, occurrences, margin, -begin

    begin = max(candidates, key=score)
    window = text[begin:begin + limit]
    if begin > 0:
        window = "…" + window[1:]
    if begin + limit < len(text):
        window = window[:-1] + "…"
    return window


class VariableGraphArm(A.V2GraphArm):
    """v2-graph 검색/형식은 유지하고 block 본문 선택 규칙만 바꾼다."""

    def __init__(self, *args: Any, block_chars: int | None, window: bool = False, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.block_chars = block_chars
        self.window = window

    def lines(self, query: str) -> tuple[list[tuple[str, A.Entry, dict[str, Any]]], str]:
        hits, pages, blocks = self.fetch(query, A.PAGES_GRAPH)
        if not hits:
            return [], "no-match"
        edges = self.ctx.edges([bid for hit in hits for bid in hit.block_ids])
        by_src: dict[str, list[tuple[str, str, str]]] = {}
        for src, kind, dst, dst_block in edges:
            if kind in A.CURATED:
                by_src.setdefault(src, []).append((kind, dst, dst_block))
        dst_pages = self.ctx.pages(
            sorted({dst for rows in by_src.values() for _kind, dst, _block in rows if dst not in pages})
        )
        pages = {**dst_pages, **pages}
        out: list[tuple[str, A.Entry, dict[str, Any]]] = []
        for hit in hits:
            page = pages.get(hit.page_id)
            if not page:
                continue
            slug = page["slug"]
            if page["head"] != page["page_id"]:
                head_page = pages.get(page["head"]) or self.ctx.pages([page["head"]]).get(page["head"])
                head_slug = head_page["slug"] if head_page else A._slug(page["head"])
                out.append((
                    f"P {slug} {page['type']} {page['updated']} sup→{head_slug}",
                    A.Entry(page["page_id"], "", False, "superseded"),
                    {"p": slug, "type": page["type"], "updated": page["updated"], "sup": head_slug},
                ))
                continue
            source = f" src={page['sources']}" if page["sources"] else ""
            out.append((
                f"P {slug} {page['type']} {page['updated']}{source}",
                A.Entry(page["page_id"], "", False, "cur"),
                {"p": slug, "type": page["type"], "updated": page["updated"]},
            ))
            for bid in hit.block_ids:
                block = blocks.get(bid)
                if not block:
                    continue
                status = self.status(page, block)
                full = C.redact(block["text"])
                if self.block_chars is None:
                    body = norm_ws(full)
                elif self.window:
                    body = dense_window(full, query, self.block_chars)
                else:
                    body = C.clip(full, self.block_chars)
                address = f"{slug}#{A._tail(bid, slug)}"
                out.append((
                    f"B {address} {status} | {body}",
                    A.Entry(page["page_id"], bid, True, status),
                    {"b": address, "st": status, "t": body},
                ))
                for kind, dst, dst_block in by_src.get(bid, []):
                    dst_page = pages.get(dst)
                    dst_slug = dst_page["slug"] if dst_page else A._slug(dst)
                    target = f"{dst_slug}#{A._tail(dst_block, dst_slug)}" if dst_block else dst_slug
                    out.append((
                        f"E {address} {kind}→{target}",
                        A.Entry(page["page_id"], bid, False, "edge"),
                        {"e": [address, kind, target]},
                    ))
        return out, "structural2"


def production_variant(root: Path, query: str, budget: int, block_chars: int | None,
                       *, window: bool = False) -> tuple[A.Payload, set[str], float]:
    t0 = time.perf_counter()
    result = C.retrieve(root, query)
    if result.reason.startswith("hint"):
        pages = [C.project_hit(hit, result.tokens, result.idf, max_blocks=0) for hit in result.hits]
        text = C.render_hint(result, pages, max_bytes=budget, max_tokens=C.MAX_TOKENS)
    else:
        limit = 10**9 if block_chars is None or window else block_chars
        pages = [
            C.project_hit(hit, result.tokens, result.idf, max_blocks=C.MAX_BLOCKS, max_block_chars=limit)
            for hit in result.hits
        ]
        if window and block_chars is not None:
            for page in pages:
                for block in page.get("blocks") or []:
                    block["text"] = dense_window(block["text"], query, block_chars)
        text = C.render(result, pages, max_bytes=budget, max_tokens=C.MAX_TOKENS)
    elapsed = (time.perf_counter() - t0) * 1000
    return production_manifest(text, pages, result.reason), selected_from_pages(pages), elapsed


def completeness(root: Path, queries: list[dict[str, Any]], oracle: Oracle,
                 ranker: S2.Structural2Ranker, ctx: A.CtxIndex, budget: int = 6000) -> dict[str, Any]:
    long_queries = [q for q in queries if q.get("type") == "long" and q.get("expects_injection")]
    variants: list[dict[str, Any]] = []

    def add(label: str, arm_name: str, method: str, clip_chars: int | None,
            runner: Callable[[dict[str, Any]], tuple[A.Payload, set[str], float]]) -> None:
        rows = []
        for q in long_queries:
            payload, selected, elapsed = runner(q)
            rows.append(judge(payload, q, oracle, selected, elapsed, clip_chars=clip_chars))
        variants.append({
            "label": label,
            "arm": arm_name,
            "method": method,
            "clip_chars": clip_chars,
            "budget": budget,
            "overall": aggregate(rows),
            "per_query": rows,
        })

    for clip_chars in COMPLETENESS_CLIPS:
        label = "unlimited" if clip_chars is None else str(clip_chars)
        add(
            f"production-prefix-{label}",
            "production",
            "prefix",
            clip_chars,
            lambda q, n=clip_chars: production_variant(root, q["text"], budget, n),
        )
        add(
            f"v2-graph-cut0.5-prefix-{label}",
            "v2-graph[cut=0.5]",
            "prefix",
            clip_chars,
            lambda q, n=clip_chars: _run_variable_graph(ranker, ctx, q["text"], budget, n, False),
        )
    add(
        "production-window-320",
        "production",
        "question-density-window",
        PREFIX_CHARS,
        lambda q: production_variant(root, q["text"], budget, PREFIX_CHARS, window=True),
    )
    add(
        "v2-graph-cut0.5-window-320",
        "v2-graph[cut=0.5]",
        "question-density-window",
        PREFIX_CHARS,
        lambda q: _run_variable_graph(ranker, ctx, q["text"], budget, PREFIX_CHARS, True),
    )
    return {
        "long_queries": len(long_queries),
        "budget": budget,
        "window_rule": (
            "정규화한 block에서 structural2 질문 토큰의 고유 일치 수, 전체 출현 수, "
            "앞쪽 위치 순으로 320자 창을 고른다. answer_span/gold 위치는 사용하지 않는다."
        ),
        "variants": variants,
    }


def _run_variable_graph(ranker: S2.Structural2Ranker, ctx: A.CtxIndex, query: str,
                        budget: int, block_chars: int | None,
                        window: bool) -> tuple[A.Payload, set[str], float]:
    arm = VariableGraphArm(ranker, ctx, cut=0.5, block_chars=block_chars, window=window)
    items, _reason = arm.lines(query)
    selected = selected_from_graph_items(items)
    t0 = time.perf_counter()
    payload = arm.run(query, budget)
    elapsed = (time.perf_counter() - t0) * 1000
    return payload, selected, elapsed


def evaluate_arms(root: Path, queries: list[dict[str, Any]], oracle: Oracle,
                  ranker: S2.Structural2Ranker, ctx: A.CtxIndex,
                  out_dir: Path) -> tuple[list[dict[str, Any]], dict[str, dict[int, list[dict[str, Any]]]]]:
    specs: list[tuple[str, Any]] = [("production", ExactProductionArm(root))]
    specs.extend((f"v2-graph[cut={cut}]", A.V2GraphArm(ranker, ctx, cut=cut)) for cut in CUTS)
    specs.append(("v2-address", A.V2AddressArm(ranker, ctx)))
    all_rows: dict[str, dict[int, list[dict[str, Any]]]] = {}
    summary: list[dict[str, Any]] = []
    for label, arm in specs:
        rows_by_budget = {budget: [] for budget in BUDGETS}
        for index, q in enumerate(queries, 1):
            query = q["text"]
            if label == "production":
                result, pages, text6000, prepare_ms = arm.prepare(query)
                selected = selected_from_pages(pages)
                for budget in BUDGETS:
                    t0 = time.perf_counter()
                    payload = arm.render(result, pages, budget, text6000)
                    elapsed = prepare_ms + (time.perf_counter() - t0) * 1000
                    rows_by_budget[budget].append(judge(payload, q, oracle, selected, elapsed))
            elif label.startswith("v2-graph"):
                items, _reason = arm.lines(query)
                selected = selected_from_graph_items(items)
                for budget in BUDGETS:
                    t0 = time.perf_counter()
                    payload = arm.run(query, budget)
                    elapsed = (time.perf_counter() - t0) * 1000
                    rows_by_budget[budget].append(judge(payload, q, oracle, selected, elapsed))
            else:
                hits, _pages, _blocks = arm.fetch(query, A.PAGES_GRAPH)
                selected = {bid for hit in hits for bid in hit.block_ids}
                for budget in BUDGETS:
                    t0 = time.perf_counter()
                    payload = arm.run(query, budget)
                    elapsed = (time.perf_counter() - t0) * 1000
                    rows_by_budget[budget].append(
                        judge(payload, q, oracle, selected, elapsed, clip_chars=None)
                    )
            if index % 10 == 0:
                print(f"[natural] {label}: {index}/{len(queries)}", file=sys.stderr)
        all_rows[label] = rows_by_budget
        file_label = label.replace("[", "-").replace("]", "").replace("=", "-").replace(".", "_")
        for budget, rows in rows_by_budget.items():
            entry = {"arm": label, "budget": budget, "overall": aggregate(rows)}
            summary.append(entry)
            (out_dir / f"nat-{file_label}-{budget}.json").write_text(
                json.dumps({**entry, "per_query": rows}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
    return summary, all_rows


def evaluate_signals(root: Path, queries: list[dict[str, Any]],
                     ranker: S2.Structural2Ranker) -> dict[str, Any]:
    rows = []
    for index, q in enumerate(queries, 1):
        row = {
            "id": q["id"],
            "type": q["type"],
            "expects_injection": bool(q["expects_injection"]),
            **production_signal(root, q["text"]),
            **structural_signal(ranker, q["text"]),
        }
        rows.append(row)
        if index % 10 == 0:
            print(f"[natural] signals: {index}/{len(queries)}", file=sys.stderr)
    rocs = {signal: roc(rows, signal) for signal in SIGNALS}
    # 상대 1위/2위 비율은 단독 우연 hit도 커지므로 권장 gate 후보에서 제외한다.
    candidates = [rocs["structural_raw_top"], rocs["structural_coverage"]]
    best = max(
        candidates,
        key=lambda item: (
            item["recommended_at_fpr_lte_0_05"]["answerable_injection"] or 0.0,
            -(item["recommended_at_fpr_lte_0_05"]["unrelated_injection"] or 0.0),
        ),
    )
    return {
        "per_query": rows,
        "roc": rocs,
        "current_production": current_production_gates(rows),
        "recommended_structural_gate": {
            "signal": best["signal"],
            **best["recommended_at_fpr_lte_0_05"],
            "note": "같은 자연 세트에서 보정한 단일 절대 신호 문턱이며 독립 holdout 전에는 잠정값이다.",
        },
    }


def fmt(value: Any, digits: int = 3) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def synthetic_rows() -> dict[str, dict[str, Any]]:
    path = ROOT / "bench/results_ctx/500q-summary.json"
    if not path.exists():
        return {}
    rows = json.loads(path.read_text(encoding="utf-8"))
    return {f"{row['arm']}@{row['budget']}": row["overall"] for row in rows}


def write_report(path: Path, payload: dict[str, Any]) -> None:
    summary = payload["summary"]
    signals = payload["signals"]
    complete = payload["completeness"]
    dataset = payload["dataset"]
    lines = [
        "# 자연 문서 컨텍스트 평가",
        "",
        f"질문 {dataset['queries']}개(answerable {dataset['answerable']}, unrelated {dataset['unrelated']})와 "
        f"page {dataset['pages']}개를 제품 함수(qmd=False)와 structural2로 측정했다. "
        "문턱은 같은 세트에서 보정한 잠정값이며 독립 holdout 검증 전에는 제품 기본값이 아니다.",
        "",
        "## 예산·arm별 결과",
        "",
        "selected는 검색/투영 선택, delivered_prefix는 arm manifest의 같은 block ID와 실제 앞 320자 문자열이 "
        "모두 있는 경우, answer_delivered는 실제 정답 span이다. "
        "unrelated 주입률은 페이로드가 한 바이트라도 나간 오탐률이다.",
        "",
        "| arm | B | selected | prefix | answer | truncated | stale leak | answerable 주입 | unrelated 주입 | bytes | tokens | p50 ms |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in summary:
        overall = row["overall"]
        lines.append(
            f"| {row['arm']} | {row['budget']} | {fmt(overall['selected'])} | "
            f"{fmt(overall['delivered_prefix'])} | {fmt(overall['answer_delivered'])} | "
            f"{fmt(overall['truncated'])} | {fmt(overall['stale_leak'])} | "
            f"{fmt(overall['injected_answerable'])} | {fmt(overall['injected_unrelated'])} | "
            f"{fmt(overall['payload_bytes_mean'], 1)} | "
            f"{fmt(overall['est_tokens_mean'], 1)} | {fmt(overall['latency_ms']['p50'])} |"
        )

    lines.extend([
        "",
        "## 무주입 문턱",
        "",
        "각 신호는 값이 문턱 이상이면 주입으로 판정했다. 권장점은 unrelated 오탐률 5% 이하에서 "
        "answerable 주입률이 가장 큰 ROC 운용점이다.",
        "",
        "| 신호 | answerable p50 [min,max] | unrelated p50 [min,max] | AUC | 권장 문턱 | answerable 주입 | unrelated 오탐 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ])
    for signal in SIGNALS:
        row = signals["roc"][signal]
        pos, neg = row["answerable"], row["unrelated"]
        rec = row["recommended_at_fpr_lte_0_05"]
        lines.append(
            f"| {signal} | {fmt(pos['p50'])} [{fmt(pos['min'])},{fmt(pos['max'])}] | "
            f"{fmt(neg['p50'])} [{fmt(neg['min'])},{fmt(neg['max'])}] | {fmt(row['auc'])} | "
            f"{fmt(rec['threshold'], 9)} | {fmt(rec['answerable_injection'])} | "
            f"{fmt(rec['unrelated_injection'])} |"
        )
    current = signals["current_production"]
    lines.extend([
        "",
        "현재 production 개별 문턱의 같은 분포상 위치:",
        "",
        "| 신호/상수 | 문턱 | answerable 주입 | unrelated 오탐 |",
        "|---|---:|---:|---:|",
    ])
    for point in current["individual_threshold_positions"]:
        lines.append(
            f"| {point['signal']} / {point['name']} | {fmt(point['threshold'], 6)} | "
            f"{fmt(point['answerable_injection'])} | {fmt(point['unrelated_injection'])} |"
        )
    lines.extend([
        "",
        "현재 production 결합 gate:",
        "",
        "| gate | answerable 주입 | unrelated 오탐 |",
        "|---|---:|---:|",
        f"| body: score≥{C.MIN_SCORE} AND (coverage≥{C.MIN_COVERAGE} OR matched≥{C.MIN_MATCHED}) | "
        f"{fmt(current['body_gate']['answerable_injection'])} | {fmt(current['body_gate']['unrelated_injection'])} |",
        f"| hint only: score≥{C.HINT_SCORE} AND (coverage≥{C.HINT_COVERAGE} OR matched≥{C.HINT_MATCHED}) | "
        f"{fmt(current['hint_only_gate']['answerable_injection'])} | {fmt(current['hint_only_gate']['unrelated_injection'])} |",
        f"| body 또는 hint | {fmt(current['any_payload_gate']['answerable_injection'])} | "
        f"{fmt(current['any_payload_gate']['unrelated_injection'])} |",
        "",
        f"`HINT_PAGES={C.HINT_PAGES}`와 `HINT_SUMMARY_CHARS={C.HINT_SUMMARY_CHARS}`는 hint가 "
        "결정된 뒤 payload 모양만 제한하므로 ROC 문턱은 아니다.",
        "",
    ])
    rec = signals["recommended_structural_gate"]
    matched_point = next(
        point for point in current["individual_threshold_positions"] if point["name"] == "MIN_MATCHED"
    )
    lines.append(
        f"잠정 권장 무주입 gate는 `{rec['signal']} >= {fmt(rec['threshold'], 6)}`이다"
        f"(answerable {fmt(rec['answerable_injection'])}, unrelated {fmt(rec['unrelated_injection'])}). "
        "이는 structural2만으로 계산 가능한 신호 중 최선이다. 전체 신호 중에는 "
        f"`production_matched >= {C.MIN_MATCHED}`가 answerable "
        f"{fmt(matched_point['answerable_injection'])}, unrelated "
        f"{fmt(matched_point['unrelated_injection'])}로 더 낫지만, "
        "정본 전량 스캔 점수를 별도로 계산해야 한다. 1위/2위 비율은 단독 우연 posting도 "
        "크게 보이므로 권장 후보에서 제외했다."
    )

    lines.extend([
        "",
        "## 긴 block 완전성",
        "",
        f"long {complete['long_queries']}문항, 예산 {complete['budget']} B. answer window는 "
        "gold/answer_span을 보지 않고 질문 토큰 밀도로만 위치를 정했다.",
        "",
        "| arm/방법 | clip | selected | answer_delivered | bytes |",
        "|---|---:|---:|---:|---:|",
    ])
    for row in complete["variants"]:
        overall = row["overall"]
        clip = "무제한" if row["clip_chars"] is None else row["clip_chars"]
        lines.append(
            f"| {row['arm']} / {row['method']} | {clip} | {fmt(overall.get('selected'))} | "
            f"{fmt(overall.get('answer_delivered'))} | {fmt(overall.get('payload_bytes_mean'), 1)} |"
        )
    for arm_name in ("production", "v2-graph[cut=0.5]"):
        prefix320 = next((row for row in complete["variants"]
                          if row["arm"] == arm_name and row["method"] == "prefix"
                          and row["clip_chars"] == 320), None)
        prefix640 = next((row for row in complete["variants"]
                          if row["arm"] == arm_name and row["method"] == "prefix"
                          and row["clip_chars"] == 640), None)
        window320 = next((row for row in complete["variants"]
                          if row["arm"] == arm_name and row["method"] == "question-density-window"), None)
        if prefix320 and prefix640 and window320 and complete["long_queries"]:
            p320 = prefix320["overall"]
            p640 = prefix640["overall"]
            win = window320["overall"]
            relation = "더 많이" if (win["answer_delivered"] or 0) > (p320["answer_delivered"] or 0) else "같게" if win["answer_delivered"] == p320["answer_delivered"] else "더 적게"
            lines.extend([
                "",
                f"- {arm_name}: prefix 320은 answer {fmt(p320['answer_delivered'])} / "
                f"{fmt(p320['payload_bytes_mean'], 1)} B, prefix 640은 "
                f"{fmt(p640['answer_delivered'])} / {fmt(p640['payload_bytes_mean'], 1)} B, "
                f"질문 밀도 window 320은 {fmt(win['answer_delivered'])} / "
                f"{fmt(win['payload_bytes_mean'], 1)} B로 같은 320자 분량에서 {relation} 전달했다."
            ])
    if complete["long_queries"]:
        lines.extend([
            "",
            "clip을 늘렸는데 평균 bytes가 줄어드는 행은 절감이 아니다. renderer가 긴 block/page "
            "chunk를 통째로 예산 밖으로 버려 더 빈 payload를 만든 결과다."
        ])

    synth = synthetic_rows()
    natural_prod = next((r for r in summary if r["arm"] == "production" and r["budget"] == 6000), None)
    natural_v2 = next((r for r in summary if r["arm"] == "v2-graph[cut=0.5]" and r["budget"] == 6000), None)
    syn_prod = synth.get("production@6000", {})
    syn_v2 = synth.get("v2-graph[cut=0.5]@6000", {})
    lines.extend([
        "",
        "## 합성 세트와의 차이",
        "",
        "합성 500문항의 `gold_block_in_payload`는 본문 앞 60자 전달 지표여서 자연 세트의 "
        "`answer_delivered`와 동일 지표가 아니다. 아래에는 이 차이를 숨기지 않고 나란히 적는다.",
        "",
        "| arm @6000 B | 합성 prefix 지표 | 자연 selected | 자연 prefix | 자연 answer span | 합성 unrelated | 자연 unrelated |",
        "|---|---:|---:|---:|---:|---:|---:|",
        f"| production | {fmt(syn_prod.get('gold_block_in_payload'))} | "
        f"{fmt(natural_prod['overall']['selected'] if natural_prod else None)} | "
        f"{fmt(natural_prod['overall']['delivered_prefix'] if natural_prod else None)} | "
        f"{fmt(natural_prod['overall']['answer_delivered'] if natural_prod else None)} | 미측정 | "
        f"{fmt(natural_prod['overall']['injected_unrelated'] if natural_prod else None)} |",
        f"| v2-graph[cut=0.5] | {fmt(syn_v2.get('gold_block_in_payload'))} | "
        f"{fmt(natural_v2['overall']['selected'] if natural_v2 else None)} | "
        f"{fmt(natural_v2['overall']['delivered_prefix'] if natural_v2 else None)} | "
        f"{fmt(natural_v2['overall']['answer_delivered'] if natural_v2 else None)} | 미측정 | "
        f"{fmt(natural_v2['overall']['injected_unrelated'] if natural_v2 else None)} |",
        "",
        "원시 문항별 행은 `bench/results_nat/`, ROC·분포는 `signals.json`, 완전성은 "
        "`completeness.json`에 있다.",
        "",
    ])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default="bench/natural/root")
    parser.add_argument("--queries", default="bench/natural/queries.json")
    parser.add_argument("--index-root", default="bench/index_nat")
    parser.add_argument("--out", default="bench/results_nat")
    parser.add_argument("--report", default="bench/NATURAL_REPORT.md")
    parser.add_argument("--no-rebuild", action="store_true")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    query_path = Path(args.queries).resolve()
    index_root = Path(args.index_root).resolve()
    out_dir = Path(args.out).resolve()
    report_path = Path(args.report).resolve()
    corpus = root / "wiki"
    if not corpus.is_dir():
        raise SystemExit(f"wiki가 없다: {corpus}")
    query_payload = json.loads(query_path.read_text(encoding="utf-8"))
    queries = query_payload["queries"] if isinstance(query_payload, dict) else query_payload
    oracle = Oracle(corpus)
    validate_contract(queries, oracle)

    out_dir.mkdir(parents=True, exist_ok=True)
    s2_dir = index_root / "structural2"
    build: dict[str, Any] = {}
    if not args.no_rebuild or not (s2_dir / "structural2.db").exists():
        stats = S2.Structural2Ranker.build(corpus, s2_dir)
        build["structural2"] = {
            "elapsed_ms": stats.elapsed_ms,
            "index_bytes": stats.index_bytes,
            "notes": stats.notes,
        }
    if not args.no_rebuild or not (index_root / "ctx.sqlite").exists():
        build["ctx"] = A.CtxIndex.build(corpus, index_root)
    ranker = S2.Structural2Ranker.load(corpus, s2_dir)
    ctx = A.CtxIndex.load(index_root)

    summary, _all_rows = evaluate_arms(root, queries, oracle, ranker, ctx, out_dir)
    signal_results = evaluate_signals(root, queries, ranker)
    complete = completeness(root, queries, oracle, ranker, ctx)
    manifest_path = root.parent / "MANIFEST.json"
    manifest = None
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    dataset = {
        "schema_version": query_payload.get("schema_version") if isinstance(query_payload, dict) else None,
        "source_commit": query_payload.get("source_commit") if isinstance(query_payload, dict) else None,
        "root": str(root),
        "queries_path": str(query_path),
        "manifest": manifest,
        "pages": len(oracle.pages),
        "queries": len(queries),
        "answerable": sum(bool(q.get("expects_injection")) for q in queries),
        "unrelated": sum(not bool(q.get("expects_injection")) for q in queries),
        "by_type": dict(sorted(Counter(str(q.get("type")) for q in queries).items())),
    }
    payload = {
        "dataset": dataset,
        "build": build,
        "summary": summary,
        "signals": signal_results,
        "completeness": complete,
    }
    (out_dir / "summary.json").write_text(
        json.dumps({"dataset": dataset, "build": build, "summary": summary}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (out_dir / "signals.json").write_text(
        json.dumps(signal_results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / "completeness.json").write_text(
        json.dumps(complete, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    write_report(report_path, payload)
    print(json.dumps({
        "dataset": dataset,
        "recommended_gate": signal_results["recommended_structural_gate"],
        "report": str(report_path),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""600/900/1200자 gold block에서 60자 판정과 전체 본문 판정을 비교한다."""
from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "bench"))
sys.path.insert(0, str(ROOT / "bench/review_ctx"))
sys.path.insert(0, str(ROOT))

from context.arms import CtxIndex, ProductionArm  # noqa: E402
from context.harness_ctx import squash  # noqa: E402
from rankers.structural2 import Structural2Ranker  # noqa: E402
from review_arms import LongBlockV2GraphArm  # noqa: E402
from scripts import llmwiki_context as C  # noqa: E402


CORPUS = ROOT / "bench/frozen/corpus"
INDEX = ROOT / "bench/index_ctx"
LONG_ROOT = ROOT / "bench/review_ctx/long_root"
QUERIES = ROOT / "bench/review_ctx/long_queries.json"
MANIFEST = ROOT / "bench/review_ctx/long_manifest.json"
OUT = ROOT / "bench/results_review_ctx/long_blocks.json"
BUDGET = 6000


def judge(payload: Any, query: dict[str, Any], long_texts: dict[str, str],
          lengths: dict[str, int]) -> dict[str, Any]:
    flat = squash(payload.text)
    manifest_body = {entry.block_id for entry in payload.manifest if entry.block_id and entry.body}
    bids = list(query.get("gold_blocks") or [])
    prefix = full = suffix = clipped = selected = 0
    for bid in bids:
        body = long_texts[bid]
        in_manifest = bid in manifest_body
        selected += int(in_manifest)
        prefix += int(in_manifest and squash(body)[:60] in flat)
        full += int(in_manifest and squash(body) in flat)
        suffix += int(in_manifest and squash(body)[-60:] in flat)
        clipped += int(in_manifest and squash(C.clip(C.redact(body), C.MAX_BLOCK_CHARS)) in flat)
    return {
        "id": query["id"],
        "type": query["type"],
        "chars": lengths[bids[0]],
        "selected": int(selected == len(bids)),
        "prefix60_hit": int(prefix == len(bids)),
        "full_body_hit": int(full == len(bids)),
        "suffix60_hit": int(suffix == len(bids)),
        "clipped320_hit": int(clipped == len(bids)),
        "payload_bytes": len(payload.text.encode("utf-8")),
        "est_tokens": C.est_tokens(payload.text) if payload.text else 0,
    }


def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "n": len(rows),
        **{key: round(statistics.fmean(row[key] for row in rows), 4)
           for key in ("selected", "prefix60_hit", "full_body_hit", "suffix60_hit", "clipped320_hit")},
        "payload_bytes_mean": round(statistics.fmean(row["payload_bytes"] for row in rows), 1),
        "est_tokens_mean": round(statistics.fmean(row["est_tokens"] for row in rows), 1),
    }


def main() -> int:
    query_payload = json.loads(QUERIES.read_text(encoding="utf-8"))
    queries = query_payload["queries"]
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    long_texts = {bid: row["text"] for bid, row in manifest.items()}
    lengths = {bid: int(row["chars"]) for bid, row in manifest.items()}
    ctx = CtxIndex.load(INDEX)
    ranker = Structural2Ranker.load(CORPUS, INDEX / "structural2")
    arms = {
        "production-long": ProductionArm(LONG_ROOT),
        "v2-graph-long[cut=0.5]": LongBlockV2GraphArm(ranker, ctx, long_texts, cut=0.5),
    }
    output = []
    for name, arm in arms.items():
        rows = []
        for i, query in enumerate(queries):
            t0 = time.perf_counter()
            if name == "production-long":
                result, pages, text, base_ms = arm.prepare(query["text"])
                render_t0 = time.perf_counter()
                payload = arm.render(result, pages, BUDGET, text)
                latency = base_ms + (time.perf_counter() - render_t0) * 1000
            else:
                payload = arm.run(query["text"], BUDGET)
                latency = (time.perf_counter() - t0) * 1000
            row = judge(payload, query, long_texts, lengths)
            row["latency_ms"] = round(latency, 3)
            rows.append(row)
            if (i + 1) % 20 == 0:
                print(f"[review-long] {name}: {i + 1}/{len(queries)}", file=sys.stderr)
        entry = {
            "arm": name,
            "budget": BUDGET,
            "queries": len(rows),
            "overall": aggregate(rows),
            "by_chars": {str(chars): aggregate([row for row in rows if row["chars"] == chars])
                         for chars in sorted({row["chars"] for row in rows})},
            "by_type": {kind: aggregate([row for row in rows if row["type"] == kind])
                        for kind in sorted({row["type"] for row in rows})},
            "per_query": rows,
        }
        output.append(entry)
        print(name, entry["overall"])
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

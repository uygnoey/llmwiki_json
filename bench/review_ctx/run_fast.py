#!/usr/bin/env python3
"""색인 읽기 전용으로 교차 형식 arm과 no-head stale arm을 측정한다."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "bench"))
sys.path.insert(0, str(ROOT / "bench/review_ctx"))
sys.path.insert(0, str(ROOT))

from context.arms import CtxIndex  # noqa: E402
from context.harness_ctx import Oracle, aggregate, judge  # noqa: E402
from rankers.structural2 import Structural2Ranker  # noqa: E402
from review_arms import V2SearchProductionFormatArm, no_head_graph_arm  # noqa: E402


CORPUS = ROOT / "bench/frozen/corpus"
INDEX = ROOT / "bench/index_ctx"
QUERIES = ROOT / "bench/frozen/queries.json"
OUT = ROOT / "bench/results_review_ctx/fast_arms.json"
BUDGET = 6000


def measure(name: str, arm: Any, queries: list[dict[str, Any]], oracle: Oracle) -> dict[str, Any]:
    rows = []
    for i, query in enumerate(queries):
        t0 = time.perf_counter()
        payload = arm.run(query["text"], BUDGET)
        row = judge(payload, query, oracle)
        row.update({
            "id": query["id"],
            "type": query["type"],
            "latency_ms": round((time.perf_counter() - t0) * 1000, 3),
        })
        rows.append(row)
        if (i + 1) % 100 == 0:
            print(f"[review-fast] {name}: {i + 1}/{len(queries)}", file=sys.stderr)
    return {
        "arm": name,
        "budget": BUDGET,
        "queries": len(rows),
        "overall": aggregate(rows),
        "by_type": {
            kind: aggregate([row for row in rows if row["type"] == kind])
            for kind in sorted({row["type"] for row in rows})
        },
        "per_query": rows,
    }


def main() -> int:
    payload = json.loads(QUERIES.read_text(encoding="utf-8"))
    queries = payload["queries"] if isinstance(payload, dict) else payload
    temporal = [query for query in queries if query["type"] == "temporal"]
    oracle = Oracle(CORPUS)
    ctx = CtxIndex.load(INDEX)
    folded = Structural2Ranker.load(CORPUS, INDEX / "structural2")
    unfolded = Structural2Ranker.load(CORPUS, INDEX / "structural2", fold=False)
    runs = [
        measure("v2-search+production-format[cut=0]",
                V2SearchProductionFormatArm(folded, ctx, cut=0.0), queries, oracle),
        measure("v2-search+production-format[cut=0.5]",
                V2SearchProductionFormatArm(folded, ctx, cut=0.5), queries, oracle),
        measure("v2-graph[fold=false,no-head]",
                no_head_graph_arm(unfolded, ctx, cut=0.0), temporal, oracle),
        measure("v2-graph[fold=false,no-head,cut=0.5]",
                no_head_graph_arm(unfolded, ctx, cut=0.5), temporal, oracle),
    ]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(runs, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for run in runs:
        overall = run["overall"]
        print(run["arm"], "gold", overall["gold_block_in_payload"],
              "stale_body", overall["stale_body_in_payload"],
              "stale_leak", overall["stale_leak"],
              "bytes", overall["payload_bytes_mean"],
              "B/gold", overall["bytes_per_gold_hit"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

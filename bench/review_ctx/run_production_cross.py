#!/usr/bin/env python3
"""production scan을 한 번씩만 실행해 native/root-corrected/v2 형식을 비교한다."""
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
from review_arms import ProductionSearchV2FormatArm  # noqa: E402


CORPUS = ROOT / "bench/frozen/corpus"
PROD_ROOT = ROOT / "bench/index_ctx/root"
INDEX = ROOT / "bench/index_ctx"
QUERIES = ROOT / "bench/frozen/queries.json"
OUT = ROOT / "bench/results_review_ctx/production_cross.json"
BUDGETS = (2000, 4000, 6000)
NAMES = ("production-native", "production-root-corrected", "production-search+v2-format")


def main() -> int:
    payload = json.loads(QUERIES.read_text(encoding="utf-8"))
    queries = payload["queries"] if isinstance(payload, dict) else payload
    oracle = Oracle(CORPUS)
    ctx = CtxIndex.load(INDEX)
    arm = ProductionSearchV2FormatArm(PROD_ROOT, ctx)
    rows: dict[tuple[str, int], list[dict[str, Any]]] = {
        (name, budget): [] for name in NAMES for budget in BUDGETS
    }
    differences = {str(budget): {"text_changed": 0, "judge_changed": 0,
                                 "bytes_delta_sum": 0, "bytes_delta_max": 0}
                   for budget in BUDGETS}
    for i, query in enumerate(queries):
        prepared = arm.prepare(query["text"])
        for budget in BUDGETS:
            t0 = time.perf_counter()
            native, corrected, cross, base_ms = arm.render_three(prepared, budget)
            render_ms = (time.perf_counter() - t0) * 1000
            judged = []
            for name, output in zip(NAMES, (native, corrected, cross)):
                row = judge(output, query, oracle)
                row.update({"id": query["id"], "type": query["type"],
                            "latency_ms": round(base_ms + render_ms, 3)})
                rows[(name, budget)].append(row)
                judged.append(row)
            delta = judged[1]["payload_bytes"] - judged[0]["payload_bytes"]
            diff = differences[str(budget)]
            diff["text_changed"] += int(native.text != corrected.text)
            keys = ("injected", "gold_page_in_payload", "gold_block_in_payload",
                    "gold_addr_in_payload", "stale_body_in_payload", "stale_leak")
            diff["judge_changed"] += int(any(judged[0][key] != judged[1][key] for key in keys))
            diff["bytes_delta_sum"] += delta
            diff["bytes_delta_max"] = max(diff["bytes_delta_max"], abs(delta))
        if (i + 1) % 25 == 0:
            print(f"[review-production] {i + 1}/{len(queries)}", file=sys.stderr, flush=True)
    output = []
    for name in NAMES:
        for budget in BUDGETS:
            selected = rows[(name, budget)]
            output.append({
                "arm": name,
                "budget": budget,
                "queries": len(selected),
                "overall": aggregate(selected),
                "by_type": {
                    kind: aggregate([row for row in selected if row["type"] == kind])
                    for kind in sorted({row["type"] for row in selected})
                },
                "per_query": selected,
            })
    result = {"runs": output, "root_path_effect": differences}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for run in output:
        overall = run["overall"]
        print(run["arm"], run["budget"], "gold", overall["gold_block_in_payload"],
              "stale", overall["stale_leak"], "bytes", overall["payload_bytes_mean"],
              "B/gold", overall["bytes_per_gold_hit"])
    print("root_path_effect", differences)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

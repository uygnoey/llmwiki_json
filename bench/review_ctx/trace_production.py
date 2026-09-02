#!/usr/bin/env python3
"""ProductionArm.prepare가 실제 production 호출 사슬을 타는지 동적 추적한다."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "bench"))
sys.path.insert(0, str(ROOT))

from context.arms import ProductionArm  # noqa: E402
from scripts import llmwiki_context as C  # noqa: E402


def main() -> int:
    query = json.loads((ROOT / "bench/frozen/queries.json").read_text(encoding="utf-8"))["queries"][0]
    calls: list[dict[str, Any]] = []
    originals = {name: getattr(C, name) for name in ("retrieve", "project_hit", "render")}

    def wrap(name: str):
        original = originals[name]

        def traced(*args: Any, **kwargs: Any) -> Any:
            calls.append({"function": name, "kwargs": sorted(kwargs)})
            return original(*args, **kwargs)

        return traced

    try:
        for name in originals:
            setattr(C, name, wrap(name))
        result, pages, text, elapsed_ms = ProductionArm(ROOT / "bench/index_ctx/root").prepare(query["text"])
    finally:
        for name, original in originals.items():
            setattr(C, name, original)
    report = {
        "query": query["id"],
        "calls": calls,
        "counts": {name: sum(call["function"] == name for call in calls) for name in originals},
        "result_reason": result.reason,
        "pages": len(pages),
        "payload_bytes": len(text.encode("utf-8")),
        "elapsed_ms": round(elapsed_ms, 3),
        "note": "prepare -> C.build_context; build_context globals were wrapped without changing behavior",
    }
    target = ROOT / "bench/results_review_ctx/production_trace.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

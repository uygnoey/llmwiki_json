#!/usr/bin/env python3
"""두 자연 vector 실행이 지연 외 문항 단위로 같은지 검사한다."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "bench/results_vec_nat/determinism.json"
VOLATILE = {
    "latency_ms",
    "score_probe_latency_ms",
    "block_latency_ms",
    "called_latency_ms",
    "expected_latency_ms_resident_p50",
    "expected_latency_ms_called_mean",
    "expected_latency_ms_at_conditional_call_rate",
}


def normalized(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: normalized(item)
            for key, item in sorted(value.items())
            if key not in VOLATILE
        }
    if isinstance(value, list):
        return [normalized(item) for item in value]
    return value


def canonical(value: Any) -> bytes:
    return json.dumps(
        normalized(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("first", nargs="?", default="bench/results_vec_nat/results.json")
    parser.add_argument("second", nargs="?", default="bench/results_vec_nat/results-rerun.json")
    args = parser.parse_args()
    first_path = (ROOT / args.first).resolve()
    second_path = (ROOT / args.second).resolve()
    first = json.loads(first_path.read_text(encoding="utf-8"))
    second = json.loads(second_path.read_text(encoding="utf-8"))
    first_bytes, second_bytes = canonical(first), canonical(second)
    result = {
        "schema_version": "1.0",
        "first": str(first_path.relative_to(ROOT)),
        "second": str(second_path.relative_to(ROOT)),
        "excluded_keys": sorted(VOLATILE),
        "first_sha256": hashlib.sha256(first_bytes).hexdigest(),
        "second_sha256": hashlib.sha256(second_bytes).hexdigest(),
        "identical_except_latency": first_bytes == second_bytes,
        "queries_first": len(first.get("per_query") or []),
        "queries_second": len(second.get("per_query") or []),
    }
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["identical_except_latency"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

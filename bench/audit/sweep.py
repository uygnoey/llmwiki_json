#!/usr/bin/env python3
"""구조 랭커 핵심 상수의 국소 민감도(0.5x..1.5x)를 측정한다."""

from __future__ import annotations

import argparse
from pathlib import Path

from common import (
    DEFAULT_CORPUS,
    DEFAULT_INDEX_ROOT,
    DEFAULT_QUERIES,
    DEFAULT_RESULTS,
    ensure_index,
    load_queries,
    run_arm,
    structural,
    verify_frozen,
    write_json,
)


CONSTANTS = (
    "W_CURATED_ANCHOR",
    "W_RELATED_HOP",
    "GATE_SUPERSEDED",
    "W_RARE_MATCH",
    "MIN_RELATED_SRC_LEX",
)
FACTORS = (0.50, 0.75, 1.25, 1.50)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--queries", type=Path, default=DEFAULT_QUERIES)
    parser.add_argument("--index-root", type=Path, default=DEFAULT_INDEX_ROOT)
    parser.add_argument("--out", type=Path, default=DEFAULT_RESULTS / "sensitivity.json")
    parser.add_argument("--rebuild-index", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    manifest = verify_frozen(args.corpus, args.queries)
    query_payload, queries = load_queries(args.queries)
    index_dir = ensure_index(args.corpus, args.index_root, args.rebuild_index)
    baseline = run_arm(
        "baseline", {}, corpus=args.corpus, index_dir=index_dir, queries=queries
    )

    sweeps = []
    for constant in CONSTANTS:
        base_value = float(getattr(structural, constant))
        variants = []
        for factor in FACTORS:
            value = round(base_value * factor, 6)
            arm = run_arm(
                f"{constant}={factor:.2f}x",
                {constant: value},
                corpus=args.corpus,
                index_dir=index_dir,
                queries=queries,
            )
            arm["factor"] = factor
            arm["value"] = value
            arm["delta_recall@5"] = round(
                float(arm["overall"]["recall@5"])
                - float(baseline["overall"]["recall@5"]),
                4,
            )
            arm["delta_by_type_recall@5"] = {
                kind: round(
                    float(metrics["recall@5"])
                    - float(baseline["by_type"][kind]["recall@5"]),
                    4,
                )
                for kind, metrics in arm["by_type"].items()
            }
            variants.append(arm)
        sweeps.append(
            {"constant": constant, "baseline_value": base_value, "variants": variants}
        )

    payload = {
        "schema_version": "1.0",
        "experiment": "local_constant_sensitivity",
        "seed": query_payload.get("seed"),
        "corpus_pages": query_payload.get("corpus_pages"),
        "k": 10,
        "factors": list(FACTORS),
        "manifest": manifest,
        "index_root": args.index_root.as_posix(),
        "monkeypatch_only": True,
        "baseline": baseline,
        "sweeps": sweeps,
    }
    write_json(args.out, payload)
    print(args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

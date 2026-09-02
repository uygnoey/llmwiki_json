#!/usr/bin/env python3
"""구조 랭커 핵심 상수의 단일 절제 행렬을 실행한다.

랭커 원본은 수정하지 않는다. 색인은 bench/index_audit만 사용하고, 검색 직전에
bench.rankers.structural 모듈 상수를 monkeypatch한다.
"""

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
    verify_frozen,
    write_json,
)


ARMS: tuple[tuple[str, dict[str, float]], ...] = (
    ("baseline", {}),
    ("W_RARE_MATCH=0", {"W_RARE_MATCH": 0.0}),
    ("W_CURATED_ANCHOR=0", {"W_CURATED_ANCHOR": 0.0}),
    ("W_SUPERSEDE_FORWARD=0", {"W_SUPERSEDE_FORWARD": 0.0}),
    ("GATE_SUPERSEDED=1", {"GATE_SUPERSEDED": 1.0}),
    ("W_RELATED_HOP=0", {"W_RELATED_HOP": 0.0}),
    ("W_CONCEPT_HOP=0", {"W_CONCEPT_HOP": 0.0}),
    ("W_RELATION=0", {"W_RELATION": 0.0}),
    ("LEX_TAIL_W=1", {"LEX_TAIL_W": 1.0}),
    (
        "W_RELATION=0,W_CURATED_ANCHOR=0",
        {"W_RELATION": 0.0, "W_CURATED_ANCHOR": 0.0},
    ),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--queries", type=Path, default=DEFAULT_QUERIES)
    parser.add_argument("--index-root", type=Path, default=DEFAULT_INDEX_ROOT)
    parser.add_argument("--out", type=Path, default=DEFAULT_RESULTS / "ablation.json")
    parser.add_argument("--rebuild-index", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    manifest = verify_frozen(args.corpus, args.queries)
    query_payload, queries = load_queries(args.queries)
    index_dir = ensure_index(args.corpus, args.index_root, args.rebuild_index)
    arms = [
        run_arm(
            name,
            overrides,
            corpus=args.corpus,
            index_dir=index_dir,
            queries=queries,
        )
        for name, overrides in ARMS
    ]
    payload = {
        "schema_version": "1.0",
        "experiment": "single_constant_ablation",
        "seed": query_payload.get("seed"),
        "corpus_pages": query_payload.get("corpus_pages"),
        "k": 10,
        "manifest": manifest,
        "index_root": args.index_root.as_posix(),
        "monkeypatch_only": True,
        "arms": arms,
    }
    write_json(args.out, payload)
    print(args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

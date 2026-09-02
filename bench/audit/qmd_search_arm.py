#!/usr/bin/env python3
"""기존 qmd BM25 색인에서 raw/조사 제거/결정적 backoff arm을 측정한다.

조회 전용이다. qmd init/pull/update/embed나 collection 변경은 하지 않는다.
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import math
import statistics
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from common import (
    DEFAULT_CORPUS,
    DEFAULT_QUERIES,
    DEFAULT_RESULTS,
    ROOT,
    load_queries,
    verify_frozen,
    write_json,
)
from harness import TYPES, aggregate, pct, query_metrics
from rankers.structural import query_terms, tokenize
from rankers.vector import COLLECTION, VectorRanker


METRICS = (
    "recall@1",
    "recall@5",
    "recall@10",
    "mrr@10",
    "ndcg@10",
    "stale_above",
)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def compact(metrics: dict[str, float]) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    for key in METRICS:
        value = metrics.get(key, float("nan"))
        out[key] = value if math.isfinite(value) else None
    return out


def estimate_df(markdown_dir: Path, terms: set[str]) -> dict[str, int]:
    """색인 입력 Markdown에서 qmd 단어 prefix의 문서 빈도를 결정적으로 센다."""
    token_docs: dict[str, set[int]] = defaultdict(set)
    paths = sorted(markdown_dir.glob("*.md"))
    if not paths:
        raise RuntimeError(f"기존 qmd Markdown 색인 입력이 없다: {markdown_dir}")
    for doc_number, path in enumerate(paths):
        for token in set(tokenize(path.read_text(encoding="utf-8"))):
            token_docs[token].add(doc_number)

    vocabulary = sorted(token_docs)
    estimated: dict[str, int] = {}
    for term in sorted(terms):
        start = bisect.bisect_left(vocabulary, term)
        end = bisect.bisect_right(vocabulary, term + chr(0x10FFFF))
        docs: set[int] = set()
        for token in vocabulary[start:end]:
            if not token.startswith(term):
                break
            docs.update(token_docs[token])
        estimated[term] = len(docs)
    return estimated


def summarize(
    rows: list[dict[str, float]],
    by_type: dict[str, list[dict[str, float]]],
    latencies: list[float],
    *,
    process_calls: int,
    empty_queries: int,
) -> dict[str, Any]:
    return {
        "queries": len(rows),
        "process_calls": process_calls,
        "empty_queries": empty_queries,
        "latency_ms": {
            "p50": pct(latencies, 50),
            "p95": pct(latencies, 95),
            "mean": round(statistics.fmean(latencies), 3),
        },
        "overall": compact(aggregate(rows)),
        "by_type": {
            kind: compact(aggregate(by_type[kind])) for kind in TYPES
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queries", type=Path, default=DEFAULT_QUERIES)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument(
        "--index-dir",
        type=Path,
        default=ROOT / "bench" / "index" / "vector-mode_search",
    )
    parser.add_argument("--out", type=Path, default=DEFAULT_RESULTS / "qmd_search_arm.json")
    parser.add_argument("--k", type=int, default=10)
    parser.add_argument("--max-backoff", type=int, default=3)
    args = parser.parse_args()

    manifest = verify_frozen(args.corpus, args.queries)
    query_payload, queries = load_queries(args.queries)
    index_dir = args.index_dir.resolve()
    db_path = index_dir / ".qmd" / "index.sqlite"
    page_map = index_dir / "page-map.json"
    if not db_path.is_file() or not page_map.is_file():
        raise RuntimeError(f"기존 project-local qmd 색인이 없다: {index_dir}")
    db_sha_before = file_sha256(db_path)

    transformed = {query["id"]: query_terms(query["text"]) for query in queries}
    all_terms = {term for terms in transformed.values() for term in terms}
    df = estimate_df(index_dir / "markdown", all_terms)
    ranker = VectorRanker.load(args.corpus, index_dir, mode="search")

    variant_rows: dict[str, list[dict[str, float]]] = {
        name: [] for name in ("raw", "stripped", "backoff")
    }
    variant_type_rows: dict[str, dict[str, list[dict[str, float]]]] = {
        name: {kind: [] for kind in TYPES}
        for name in ("raw", "stripped", "backoff")
    }
    variant_latencies: dict[str, list[float]] = {
        name: [] for name in ("raw", "stripped", "backoff")
    }
    process_calls = Counter()
    empty_queries = Counter()
    retry_counts = Counter()
    success_after_retry = 0
    per_query = []

    def record(name: str, hits: list[Any], query: dict[str, Any], elapsed_ms: float) -> None:
        metrics = query_metrics(hits, query, args.k)
        variant_rows[name].append(metrics)
        variant_type_rows[name][query["type"]].append(metrics)
        variant_latencies[name].append(elapsed_ms)
        if not hits:
            empty_queries[name] += 1

    for number, query in enumerate(queries, start=1):
        terms = transformed[query["id"]]
        stripped_query = " ".join(terms)

        started = time.perf_counter()
        raw_hits = ranker.search(query["text"], k=args.k)
        raw_ms = (time.perf_counter() - started) * 1000.0
        process_calls["raw"] += 1
        record("raw", raw_hits, query, raw_ms)

        started = time.perf_counter()
        stripped_hits = ranker.search(stripped_query, k=args.k)
        stripped_ms = (time.perf_counter() - started) * 1000.0
        process_calls["stripped"] += 1
        record("stripped", stripped_hits, query, stripped_ms)

        remaining = list(terms)
        removal_order = sorted(
            range(len(terms)),
            key=lambda position: (-df.get(terms[position], 0), position, terms[position]),
        )
        removed: list[str] = []
        started = time.perf_counter()
        backoff_hits = ranker.search(" ".join(remaining), k=args.k)
        process_calls["backoff"] += 1
        retries = 0
        for position in removal_order[: args.max_backoff]:
            if backoff_hits or len(remaining) <= 1:
                break
            token = terms[position]
            remaining.remove(token)
            removed.append(token)
            retries += 1
            backoff_hits = ranker.search(" ".join(remaining), k=args.k)
            process_calls["backoff"] += 1
        backoff_ms = (time.perf_counter() - started) * 1000.0
        retry_counts[str(retries)] += 1
        if retries and backoff_hits:
            success_after_retry += 1
        record("backoff", backoff_hits, query, backoff_ms)

        gold = set(query.get("gold_pages") or [])
        per_query.append(
            {
                "id": query["id"],
                "type": query["type"],
                "raw_nonempty": bool(raw_hits),
                "stripped_query": stripped_query,
                "stripped_nonempty": bool(stripped_hits),
                "backoff_query": " ".join(remaining),
                "backoff_removed": removed,
                "backoff_removed_df": [df.get(token, 0) for token in removed],
                "backoff_retries": retries,
                "backoff_nonempty": bool(backoff_hits),
                "gold_rank": {
                    "raw": next(
                        (i for i, hit in enumerate(raw_hits, 1) if hit.page_id in gold), None
                    ),
                    "stripped": next(
                        (i for i, hit in enumerate(stripped_hits, 1) if hit.page_id in gold),
                        None,
                    ),
                    "backoff": next(
                        (i for i, hit in enumerate(backoff_hits, 1) if hit.page_id in gold),
                        None,
                    ),
                },
            }
        )
        if number % 50 == 0:
            print(f"[qmd-search-arm] {number}/{len(queries)}", flush=True)

    db_sha_after = file_sha256(db_path)
    if db_sha_after != db_sha_before:
        raise RuntimeError("조회 중 qmd index.sqlite가 변경됐다; 결과를 채택하지 않는다")

    variants = {
        name: summarize(
            variant_rows[name],
            variant_type_rows[name],
            variant_latencies[name],
            process_calls=process_calls[name],
            empty_queries=empty_queries[name],
        )
        for name in ("raw", "stripped", "backoff")
    }
    variants["backoff"]["retry_counts"] = dict(sorted(retry_counts.items()))
    variants["backoff"]["success_after_retry"] = success_after_retry
    variants["backoff"]["max_retries"] = args.max_backoff

    output = {
        "schema_version": "1.0",
        "experiment": "qmd_search_particle_stripping",
        "seed": query_payload.get("seed"),
        "corpus_pages": query_payload.get("corpus_pages"),
        "queries": len(queries),
        "k": args.k,
        "collection": COLLECTION,
        "index_dir": args.index_dir.as_posix(),
        "index_sha256_before": db_sha_before,
        "index_sha256_after": db_sha_after,
        "index_unchanged": True,
        "reindexed": False,
        "manifest": manifest,
        "stripping": "bench/rankers/structural.py query_terms",
        "df_estimator": "existing rendered Markdown structural-token prefix document frequency",
        "backoff_rule": "on empty, remove highest estimated-DF token; tie by original position; at most 3 retries",
        "variants": variants,
        "per_query": per_query,
    }
    write_json(args.out, output)
    print(args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

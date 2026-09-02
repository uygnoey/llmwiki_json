#!/usr/bin/env python3
"""구조 랭커 감사 스크립트가 공유하는 결정론적 실행 도우미."""

from __future__ import annotations

import hashlib
import json
import math
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
BENCH = ROOT / "bench"
sys.path.insert(0, str(BENCH))
sys.path.insert(0, str(ROOT))

from harness import TYPES, aggregate, query_metrics  # noqa: E402
from rankers import structural  # noqa: E402


DEFAULT_CORPUS = BENCH / "frozen" / "corpus"
DEFAULT_QUERIES = BENCH / "frozen" / "queries.json"
DEFAULT_MANIFEST = BENCH / "frozen" / "MANIFEST.json"
DEFAULT_INDEX_ROOT = BENCH / "index_audit"
DEFAULT_RESULTS = BENCH / "results_audit"

REPORTED_METRICS = (
    "recall@1",
    "recall@5",
    "recall@10",
    "mrr@10",
    "block_recall@5",
    "stale_above",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_corpus(root: Path) -> str:
    """MANIFEST.json 생성 때 사용한 상대 경로+파일 바이트 해시."""
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.json")):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def verify_frozen(corpus: Path, queries_path: Path) -> dict[str, Any]:
    manifest = json.loads(DEFAULT_MANIFEST.read_text(encoding="utf-8"))
    observed = {
        "pages": sum(1 for _ in corpus.rglob("*.json")),
        "corpus_sha256": sha256_corpus(corpus),
        "queries_sha256": sha256_file(queries_path),
    }
    expected = {
        "pages": int(manifest["pages"]),
        "corpus_sha256": str(manifest["corpus_sha256"]),
        "queries_sha256": str(manifest["queries_sha256"]),
    }
    if observed != expected:
        raise RuntimeError(f"동결 코퍼스 MANIFEST 불일치: {observed} != {expected}")
    return {"expected": expected, "observed": observed, "matches": True}


def load_queries(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    queries = payload.get("queries") if isinstance(payload, dict) else None
    if not isinstance(queries, list) or len(queries) != 500:
        raise RuntimeError(f"500문항 queries.json이 아니다: {path}")
    counts = {kind: 0 for kind in TYPES}
    for query in queries:
        counts[str(query.get("type"))] = counts.get(str(query.get("type")), 0) + 1
    if any(counts.get(kind) != 100 for kind in TYPES):
        raise RuntimeError(f"유형별 100문항 구성이 아니다: {counts}")
    return payload, queries


def ensure_index(corpus: Path, index_root: Path, rebuild: bool) -> Path:
    index_dir = index_root / "structural"
    if rebuild or not (index_dir / "structural.db").is_file():
        structural.StructuralRanker.build(corpus, index_dir)
    return index_dir


def _compact_metrics(metrics: dict[str, float]) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    for key in REPORTED_METRICS:
        value = metrics.get(key, float("nan"))
        out[key] = value if math.isfinite(value) else None
    return out


def run_arm(
    name: str,
    overrides: dict[str, float],
    *,
    corpus: Path,
    index_dir: Path,
    queries: list[dict[str, Any]],
    k: int = 10,
) -> dict[str, Any]:
    """모듈 전역 상수만 임시 교체하여 한 arm을 500문항에 실행한다."""
    originals: dict[str, Any] = {}
    for constant, value in overrides.items():
        if not hasattr(structural, constant):
            raise AttributeError(f"structural.py에 상수가 없다: {constant}")
        originals[constant] = getattr(structural, constant)
        setattr(structural, constant, value)

    ranker = None
    try:
        ranker = structural.StructuralRanker.load(corpus, index_dir)
        rows: list[dict[str, float]] = []
        by_type: dict[str, list[dict[str, float]]] = {kind: [] for kind in TYPES}
        rank_counts: dict[str, dict[str, int]] = {kind: {} for kind in TYPES}
        for query in queries:
            hits = ranker.search(str(query["text"]), k=k)
            row = query_metrics(hits, query, k)
            rows.append(row)
            kind = str(query["type"])
            by_type[kind].append(row)
            gold = set(query.get("gold_pages") or [])
            rank = next(
                (position for position, hit in enumerate(hits, start=1) if hit.page_id in gold),
                None,
            )
            label = str(rank) if rank is not None else ">10"
            rank_counts[kind][label] = rank_counts[kind].get(label, 0) + 1
        return {
            "name": name,
            "overrides": overrides,
            "queries": len(queries),
            "overall": _compact_metrics(aggregate(rows)),
            "by_type": {
                kind: _compact_metrics(aggregate(by_type[kind])) for kind in TYPES
            },
            "gold_rank_counts": rank_counts,
        }
    finally:
        if ranker is not None:
            ranker.db.close()
        for constant, value in originals.items():
            setattr(structural, constant, value)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )


#!/usr/bin/env python3
"""structural2 후보에 JSON history.at 최신성 신호를 선택적으로 얹는다."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from bench.rankers.base import BuildStats, Hit, load_pages
from bench.rankers.structural2 import Structural2Ranker


class HistoryAtRanker:
    """상위 100개 structural2 후보를 history.at 분위수로 재정렬하는 wrapper."""

    name = "structural2_history_at"
    DEFAULT_WEIGHT = 0.02
    CANDIDATES = 100

    def __init__(self, base: Structural2Ranker, recency: dict[str, float], weight: float):
        self.base = base
        self.recency = recency
        self.weight = weight

    @classmethod
    def build(cls, corpus_dir: Path, index_dir: Path, **opts: Any) -> BuildStats:
        return Structural2Ranker.build(corpus_dir, index_dir, **opts)

    @classmethod
    def load(cls, corpus_dir: Path, index_dir: Path, **opts: Any) -> "HistoryAtRanker":
        pages = load_pages(Path(corpus_dir))
        dates: dict[str, str] = {}
        for page in pages:
            values = [
                str(row.get("at") or "")
                for row in page.get("history") or []
                if isinstance(row, dict)
            ]
            dates[str(page["id"])] = max(values, default=str(page.get("updated") or ""))
        ordered = sorted(set(dates.values()))
        denom = max(1, len(ordered) - 1)
        quantile = {value: idx / denom for idx, value in enumerate(ordered)}
        recency = {pid: quantile[value] for pid, value in dates.items()}
        base_opts = {key: value for key, value in opts.items() if key != "history_weight"}
        return cls(
            Structural2Ranker.load(corpus_dir, index_dir, **base_opts),
            recency,
            float(opts.get("history_weight", cls.DEFAULT_WEIGHT)),
        )

    def search(self, query: str, k: int = 10) -> list[Hit]:
        hits = self.base.search(query, k=max(k, self.CANDIDATES))
        return sorted(
            hits,
            key=lambda hit: (-(hit.score + self.weight * self.recency.get(hit.page_id, 0.0)), hit.page_id),
        )[:k]


RANKER = HistoryAtRanker

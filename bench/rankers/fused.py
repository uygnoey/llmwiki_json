"""구조 우선 + 벡터 폴백. coordinator 소유.

구조 랭커가 자신 있게 답하면 그대로 쓰고, 근거가 약할 때만 벡터를 부른다.
목적은 '구조 vs 벡터' 택일이 아니라 '구조를 주 경로로 두면 벡터 호출을
얼마나 줄이면서 어디까지 따라잡는가' 를 재는 것이다.

margin = (1위 점수 - 2위 점수) / 1위 점수 로 자신감을 잰다. 절대 점수는
질문마다 스케일이 달라 문턱을 못 잡지만, 1·2위 간격은 비교 가능하다.
"""
from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

from .base import BuildStats, Hit, dir_bytes


def _impl(mod_name: str):
    mod = importlib.import_module(f"rankers.{mod_name}")
    cls = getattr(mod, "RANKER", None)
    if cls is None:
        for attr in vars(mod).values():
            if isinstance(attr, type) and hasattr(attr, "search") and hasattr(attr, "build"):
                cls = attr
                break
    if cls is None:
        raise RuntimeError(f"rankers.{mod_name}: Ranker 구현 없음")
    return cls


class FusedRanker:
    name = "fused"

    def __init__(self, structural: Any, vector: Any, min_top: float, min_margin: float):
        self.structural = structural
        self.vector = vector
        self.min_top = min_top
        self.min_margin = min_margin
        self.fallbacks = 0
        self.calls = 0

    @classmethod
    def build(cls, corpus_dir: Path, index_dir: Path, **opts: Any) -> BuildStats:
        s_dir = Path(index_dir) / "structural"
        v_dir = Path(index_dir) / "vector"
        s = _impl("structural").build(corpus_dir, s_dir)
        v = _impl("vector").build(corpus_dir, v_dir, mode=opts.get("vmode", "vsearch"))
        return BuildStats(
            elapsed_ms=(s.elapsed_ms or 0) + (v.elapsed_ms or 0),
            index_bytes=dir_bytes(Path(index_dir)),
            notes={"structural": s.notes, "vector": v.notes,
                   "min_top": opts.get("min_top", 3.0),
                   "min_margin": opts.get("min_margin", 0.15)},
        )

    @classmethod
    def load(cls, corpus_dir: Path, index_dir: Path, **opts: Any) -> "FusedRanker":
        s = _impl("structural").load(corpus_dir, Path(index_dir) / "structural")
        v = _impl("vector").load(corpus_dir, Path(index_dir) / "vector",
                                 mode=opts.get("vmode", "vsearch"))
        return cls(s, v, float(opts.get("min_top", 3.0)), float(opts.get("min_margin", 0.15)))

    def _confident(self, hits: list[Hit]) -> bool:
        if not hits:
            return False
        top = hits[0].score
        if top < self.min_top:
            return False
        if len(hits) == 1:
            return True
        margin = (top - hits[1].score) / top if top > 0 else 0.0
        return margin >= self.min_margin

    def search(self, query: str, k: int = 10) -> list[Hit]:
        self.calls += 1
        hits = self.structural.search(query, k=k)
        if self._confident(hits):
            return hits
        self.fallbacks += 1
        vhits = self.vector.search(query, k=k)
        if not vhits:
            return hits
        # 구조 결과를 앞에 두고 벡터가 새로 찾은 것을 뒤에 잇는다.
        # 구조가 이미 맞힌 것을 벡터가 밀어내면 순손실이므로 순서를 뒤집지 않는다.
        seen = {h.page_id for h in hits}
        merged = list(hits)
        for h in vhits:
            if h.page_id not in seen:
                merged.append(Hit(page_id=h.page_id, score=h.score, block_ids=h.block_ids))
                seen.add(h.page_id)
        return merged[:k]


RANKER = FusedRanker

#!/usr/bin/env python3
"""구조 랭커 v3 — 이제 `scripts/llmwiki_index.py` 의 얇은 껍데기다.

색인·검색 코드는 프레임워크 모듈로 옮겨 갔고(FINAL_PROPOSAL §6-2), 이 파일은 bench 의 랭커 계약
(bench/rankers/base.py) 을 그 모듈 위에 얹기만 한다. 코드가 둘로 갈라지지 않게 여기서는 아무것도
다시 구현하지 않는다.

structural2 와 다른 점(모듈이 가진 것): H5 식별자 조각 색인, H1 heading·제목 block 을 근거에서 제외,
절대 신호(`search_signals`), supersedes fork/cycle 상태. H6(heading 경로 색인)은 옵션 `heading_paths=True`
로만 켠다 — 기본은 꺼져 있다.

색인 파일은 structural3.db 다(내용은 index/search.sqlite 와 같은 표).
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

from .base import BuildStats, Hit, dir_bytes

_ROOT = Path(__file__).resolve().parents[2]
try:
    from scripts import llmwiki_index as IDX
except ImportError:  # bench 를 저장소 루트 밖에서 실행할 때
    sys.path.insert(0, str(_ROOT))
    from scripts import llmwiki_index as IDX  # type: ignore[no-redef]

# 같은 이름으로 다시 내보낸다 — arms_v3 와 옛 분석 스크립트가 이 이름을 쓴다.
tokenize = IDX.tokenize
STOP_2G = IDX.STOP_2G
heading_paths = IDX.heading_paths
EVIDENCE_SKIP_KINDS = IDX.EVIDENCE_SKIP_KINDS
MAX_EVIDENCE = IDX.MAX_EVIDENCE
HEADING_SEP = IDX.HEADING_SEP
STOP_WORDS = IDX.STOP_WORDS


class Structural3Ranker:
    name = "structural3"
    DB_NAME = "structural3.db"

    def __init__(self, idx: IDX.Index, opts: dict[str, Any] | None = None):
        self.idx = idx
        self.db = idx.db
        self.opts = dict(opts or {})
        self.last_signals: dict[str, float] = {}

    @property
    def noev(self) -> frozenset[int]:
        return self.idx.noev

    @property
    def nblocks(self) -> int:
        return self.idx.nblocks

    # ------------------------------------------------------------ build
    @classmethod
    def build(cls, corpus_dir: Path, index_dir: Path, **opts: Any) -> BuildStats:
        t0 = time.perf_counter()
        corpus_dir = Path(corpus_dir)
        index_dir = Path(index_dir)
        index_dir.mkdir(parents=True, exist_ok=True)
        for stale in index_dir.glob(cls.DB_NAME + "*"):
            stale.unlink()
        docs = IDX.load_docs(corpus_dir, corpus_dir.parent)
        stats = IDX.build(docs, index_dir / cls.DB_NAME,
                          heading_paths=bool(opts.get("heading_paths", False)))
        stats["tokenizer"] = ("한글 음절 2-gram + 라틴/숫자 낱말 + `_ . - : /` 분할 조각"
                              + ("; block 앞에 heading 경로 (H6)" if stats["heading_paths"] else ""))
        stats["module"] = "scripts/llmwiki_index.py"
        return BuildStats(elapsed_ms=round((time.perf_counter() - t0) * 1000.0, 1),
                          index_bytes=dir_bytes(index_dir), notes=stats)

    # ------------------------------------------------------------- load
    @classmethod
    def load(cls, corpus_dir: Path, index_dir: Path, **opts: Any) -> "Structural3Ranker":
        dbp = Path(index_dir) / cls.DB_NAME
        if not dbp.exists():
            raise FileNotFoundError(f"색인이 없다: {dbp} (build 를 먼저 돌려라)")
        return cls(IDX.open_ro(dbp), opts)

    # ------------------------------------------------------------ search
    def term_idf(self, terms: list[str]) -> dict[str, float]:
        return self.idx.term_idf(terms)

    def search(self, query: str, k: int = 10) -> list[Hit]:
        result = self.idx.search(query, k, fold=bool(self.opts.get("fold", True)))
        self.last_signals = dict(result.signals)
        return [Hit(page_id=h.page_id, score=h.score, block_ids=list(h.block_ids)) for h in result.hits]

    def search_signals(self, query: str, k: int = 10) -> tuple[list[Hit], dict[str, float]]:
        hits = self.search(query, k)
        return hits, dict(self.last_signals)


RANKER = Structural3Ranker

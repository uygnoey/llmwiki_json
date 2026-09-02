"""현재 프로덕션 어휘 랭커의 정직한 scan-per-query 기준선."""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Any

from .base import BuildStats, Hit

from scripts.llmwiki_context import (
    MAX_BLOCKS,
    load_corpus,
    query_tokens,
    rank,
    rank_blocks,
)


class BaselineRanker:
    """`scripts/llmwiki_context.py`의 현재 랭킹 동작을 그대로 감싼다."""

    name = "baseline"

    def __init__(self, corpus_dir: Path) -> None:
        self.corpus_dir = Path(corpus_dir).resolve()
        self._view: tempfile.TemporaryDirectory[str] | None = None
        if (self.corpus_dir / "wiki").is_dir():
            self.root = self.corpus_dir
        elif self.corpus_dir.name == "wiki":
            self.root = self.corpus_dir.parent
        else:
            # load_corpus를 복제하지 않고 bench corpus를 root/wiki 모양으로만 보인다.
            self._view = tempfile.TemporaryDirectory(prefix="llmwiki-bench-baseline-")
            self.root = Path(self._view.name)
            (self.root / "wiki").symlink_to(self.corpus_dir, target_is_directory=True)

    @classmethod
    def build(cls, corpus_dir: Path, index_dir: Path, **opts: Any) -> BuildStats:
        # 이 arm에는 색인이 없다. 이전 실행의 찌꺼기만 비워 통계를 정직하게 만든다.
        target = Path(index_dir)
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True, exist_ok=True)
        return BuildStats(
            elapsed_ms=0.0,
            index_bytes=0,
            notes={"mode": "production scan per query", "indexed": False},
        )

    @classmethod
    def load(cls, corpus_dir: Path, index_dir: Path, **opts: Any) -> "BaselineRanker":
        return cls(corpus_dir)

    def search(self, query: str, k: int = 10) -> list[Hit]:
        tokens = query_tokens(query)
        docs = load_corpus(self.root)
        ranked, idf = rank(docs, tokens)
        hits: list[Hit] = []
        for item in ranked[: max(0, k)]:
            blocks = rank_blocks(item.doc, tokens, idf, MAX_BLOCKS)
            hits.append(Hit(
                page_id=item.doc.page_id,
                score=float(item.score),
                block_ids=[str(block["id"]) for block in blocks],
            ))
        return hits


RANKER = BaselineRanker

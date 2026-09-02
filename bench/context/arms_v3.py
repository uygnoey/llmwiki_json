#!/usr/bin/env python3
"""v3 페이로드 arm — 이제 `scripts/llmwiki_index.py` 의 투영·렌더를 그대로 부른다.

행 단위 선택(W4)·block 단위 예산 채움·P/B/E 형식·supersedes 접기는 전부 모듈에 있다
(`project_graph`, `select_rows`, `render_graph`). 이 파일에 남은 것은 bench 계약(Entry/Payload,
`lines()`/`run()`)과 등급 사다리(강/중/중1/약/무주입) 뿐이다 — 등급 경계는 harness_v3 가 보정하고,
제품은 그중 무주입 문턱만 옵션(`LLMWIKI_CONTEXT_SILENCE`) 으로 노출한다.

정본은 읽지 않는다. 색인(structural3.db = search.sqlite 와 같은 표) 만 읽는다.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from context import arms as A
from rankers.structural3 import IDX, Structural3Ranker

PAGES = IDX.PAGES                # 10
ROW_CHARS = IDX.ROW_CHARS        # 320
ROW_MIN_TRUNC = IDX.ROW_MIN_TRUNC
MID_BODY_PAGES = IDX.MID_BODY_PAGES
WEAK_LINES = IDX.WEAK_LINES
CUT = IDX.CUT
GRADE_SIGNAL = IDX.SILENCE_SIGNAL
# 등급 경계 — harness_v3 가 자연 세트 홀수 id 에서 고른 값 (bench/results_v3/calibration.json).
T_NONE = 770.0771466733672
T_WEAK = 770.0771466733672
T_STRONG = 776.5670159034736
GRADES = IDX.GRADES
GRAPH3_HEAD = IDX.GRAPH_HEAD
WEAK_HEAD = IDX.WEAK_HEAD
TAIL = IDX.TAIL

# 같은 이름으로 다시 내보낸다 (옛 분석 스크립트용).
norm_ws = IDX.norm_ws
split_rows = IDX.split_rows
select_rows = IDX.select_rows
derive_signals = IDX.derive_signals


def query_weights(ranker: Structural3Ranker, query: str) -> dict[str, float]:
    return IDX.query_weights(ranker.idx, query)


@dataclass
class Entry3(A.Entry):
    text: str = ""              # 실제로 실린 본문 (harness 가 span 을 block 에 귀속시킬 때 쓴다)


def _entry(p: IDX.Placed) -> Entry3:
    return Entry3(p.page_id, p.block_id, p.body, p.status, p.text)


class V3Arm(A.V2Base):
    name = "v3"

    def __init__(self, ranker: Structural3Ranker, ctx: Any = None, **opts: Any):
        opts.setdefault("cut", CUT)
        super().__init__(ranker, ctx, **opts)
        self.ranker: Structural3Ranker = ranker
        self.signal = str(opts.get("signal", GRADE_SIGNAL))
        self.t_none = float(opts.get("t_none", T_NONE))
        self.t_weak = float(opts.get("t_weak", T_WEAK))
        self.t_strong = float(opts.get("t_strong", T_STRONG))
        self.force_grade = opts.get("grade")            # 측정용: 등급을 고정한다
        self.mid_pages = int(opts.get("mid_pages", MID_BODY_PAGES))
        self.weak_lines = int(opts.get("weak_lines", WEAK_LINES))
        self.row_chars = int(opts.get("row_chars", ROW_CHARS))
        self.max_tokens = int(opts.get("max_tokens", 10 ** 9))
        self.last_signals: dict[str, float] = {}
        self.last_grade = ""
        self._groups: list[IDX.Group] = []
        self._wt: dict[str, float] = {}

    # ------------------------------------------------------------ 검색 + 신호
    def fetch(self, query: str, k: int):
        result = self.ranker.idx.search(query, k=self.k or k)
        self.last_signals = derive_signals(result.signals)
        hits = result.hits
        groups = IDX.project_graph(self.ranker.idx, hits, cut=self.cut)
        self._groups = groups
        self._wt = IDX.query_weights(self.ranker.idx, query)
        kept = {g.page_id for g in groups}
        hits = [h for h in hits if h.page_id in kept]
        pages = {g.page_id: g for g in groups}
        blocks = {b["id"]: b for g in groups for b in g.blocks}
        return hits, pages, blocks

    def grade_of(self, sig: dict[str, float]) -> str:
        if self.force_grade:
            return str(self.force_grade)
        v = float(sig.get(self.signal, 0.0))
        if v >= self.t_strong:
            return "strong"
        if v >= self.t_weak:
            return "mid"
        if v >= self.t_none:
            return "weak"
        return "none"

    def _resolve(self, query: str, grade: str | None) -> tuple[str, int]:
        hits, _pages, _blocks = self.fetch(query, PAGES)
        if not hits:
            self.last_grade = "none"
            return "none", self.mid_pages
        grade = grade or self.grade_of(self.last_signals)
        mid_pages = 1 if grade == "mid1" else self.mid_pages
        if grade == "mid1":
            grade = "mid"
        self.last_grade = grade
        return grade, mid_pages

    # ------------------------------------------------------------ 줄 만들기
    def lines(self, query: str, grade: str | None = None) -> tuple[list[tuple[str, Entry3, dict[str, Any]]], str]:
        grade, mid_pages = self._resolve(query, grade)
        if not self._groups:
            return [], "no-match"
        if grade == "none":
            return [], "structural3/none"
        body_grade = "strong" if grade == "weak" else grade    # weak 도 주소 항목은 필요하다
        out: list[tuple[str, Entry3, dict[str, Any]]] = []
        for rows in IDX._lines(self._groups, self._wt, grade=body_grade, mid_pages=mid_pages,
                               row_chars=self.row_chars):
            for line, placed in rows:
                if grade == "weak" and placed.body:
                    placed = IDX.Placed(placed.page_id, placed.block_id, False, "address")
                    line = ""
                out.append((line, _entry(placed), {}))
        return out, f"structural3/{self.last_grade}"

    # ------------------------------------------------------------ 예산 채움
    def run(self, query: str, budget: int, grade: str | None = None) -> A.Payload:
        grade, mid_pages = self._resolve(query, grade)
        if not self._groups:
            return A.Payload("", [], "no-match")
        reason = f"structural3/{self.last_grade}"
        if grade == "none":
            return A.Payload("", [], reason)
        rendered = IDX.render_graph(self._groups, self._wt, max_bytes=budget, max_tokens=self.max_tokens,
                                    grade=grade, mid_pages=mid_pages, weak_lines=self.weak_lines,
                                    row_chars=self.row_chars)
        manifest = [_entry(p) for p in rendered.placed if p.status != "always"]
        return A.Payload(rendered.text, manifest, reason)

#!/usr/bin/env python3
"""과제 H-4: history.at 신호 실험이 "신호 무가치" 인지 "실험 무력" 인지 가른다.

codex 는 structural2 top-100 에 날짜 분위수 × 0.02 를 더해 recall@5 변화 0 을 얻었다.
여기서는 (a) fold 켬/끔 × weight 0 ~ 1.0 sweep, (b) top-10 인접 score 간격 분포
(0.02 로 뒤집을 수 있는 간격이 몇 개인지), (c) 코퍼스 날짜의 구조(체인 위치 날짜 vs
ordinal 유래 2026 날짜) 를 함께 적는다. 정확도만 보므로 다른 실험과 동시에 돌려도 된다.
"""
from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from bench.harness import aggregate, query_metrics  # noqa: E402
from bench.incremental.history_signal import HistoryAtRanker  # noqa: E402
from bench.incremental.run_experiment import CORPUS, load_page_files, load_queries  # noqa: E402
from bench.rankers.structural2 import Structural2Ranker  # noqa: E402

INDEX = ROOT / "bench" / "index_review_inc" / "signal" / "structural2"
RESULTS = ROOT / "bench" / "results_review_inc"
WEIGHTS = (0.0, 0.02, 0.05, 0.1, 0.2, 0.5, 1.0)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def evaluate(ranker, queries) -> dict[str, Any]:
    rows, per_type, tops = [], {}, []
    for q in queries:
        hits = ranker.search(q["text"], 10)
        row = query_metrics(hits, q, 10)
        rows.append(row)
        per_type.setdefault(q["type"], []).append(row)
        tops.append([h.page_id for h in hits])
    keys = ("recall@1", "recall@5", "recall@10", "mrr@10", "ndcg@10", "stale_above")
    overall = aggregate(rows)
    return {
        "overall": {k: overall[k] for k in keys},
        "by_type": {t: {k: aggregate(r)[k] for k in ("recall@5", "mrr@10", "stale_above")} for t, r in per_type.items()},
        "_tops": tops,
    }


def main() -> int:
    queries = load_queries()
    pages, _sources = load_page_files(CORPUS)
    if not (INDEX / "structural2.db").exists():
        Structural2Ranker.build(CORPUS, INDEX)

    # --- 날짜 구조
    dates = {pid: max((str(h.get("at") or "") for h in p.get("history") or []), default=str(p.get("updated") or "")) for pid, p in pages.items()}
    ordered = sorted(set(dates.values()))
    denom = max(1, len(ordered) - 1)
    quant = {d: i / denom for i, d in enumerate(ordered)}
    gold_q, stale_q, other_q = [], [], []
    for q in queries:
        if q["type"] != "temporal":
            continue
        gold_q.extend(quant[dates[p]] for p in q["gold_pages"])
        stale_q.extend(quant[dates[p]] for p in q["stale_pages"])
    chain_ids = {p for q in queries if q["type"] == "temporal" for p in q["gold_pages"] + q["stale_pages"]}
    other_q = [quant[dates[p]] for p in pages if p not in chain_ids]
    date_facts = {
        "unique_dates": len(ordered),
        "chain_position_dates": ["2023-02-01", "2024-04-15", "2025-06-20", "2026-08-30"],
        "non_chain_pages_all_2026": all(dates[p].startswith("2026-") for p in pages if p not in chain_ids),
        "temporal_gold_quantile_mean": round(statistics.fmean(gold_q), 4),
        "temporal_stale_quantile_mean": round(statistics.fmean(stale_q), 4),
        "non_chain_quantile_mean": round(statistics.fmean(other_q), 4),
        "share_non_chain_pages_newer_than_median_temporal_gold": round(
            sum(1 for v in other_q if v > statistics.median(gold_q)) / len(other_q), 4),
    }

    # --- score 간격: 0.02 가 뒤집을 수 있는 인접 쌍이 얼마나 되나
    base = Structural2Ranker.load(CORPUS, INDEX)
    gaps_top1, gaps_any, gold_margin = [], [], []
    for q in queries:
        hits = base.search(q["text"], 10)
        scores = [h.score for h in hits]
        if len(scores) >= 2:
            gaps_top1.append(scores[0] - scores[1])
            gaps_any.extend(scores[i] - scores[i + 1] for i in range(len(scores) - 1))
        gold = set(q["gold_pages"])
        pos = [i for i, h in enumerate(hits) if h.page_id in gold]
        if pos and pos[0] + 1 < len(hits):
            gold_margin.append(scores[pos[0]] - scores[pos[0] + 1])
    def frac_below(vals, thr):
        return round(sum(1 for v in vals if v < thr) / len(vals), 4) if vals else None
    gap_facts = {
        "top1_top2_gap_median": round(statistics.median(gaps_top1), 4),
        "top1_top2_gap_below_0.02": frac_below(gaps_top1, 0.02),
        "any_adjacent_gap_below_0.02": frac_below(gaps_any, 0.02),
        "any_adjacent_gap_below_0.1": frac_below(gaps_any, 0.1),
        "first_gold_margin_below_0.02": frac_below(gold_margin, 0.02),
        "first_gold_margin_below_0.1": frac_below(gold_margin, 0.1),
    }
    base.db.close()

    # --- sweep
    arms = {}
    ref = {}
    for fold in (True, False):
        for w in WEIGHTS:
            if w == 0.0:
                r = Structural2Ranker.load(CORPUS, INDEX, fold=fold)
            else:
                r = HistoryAtRanker.load(CORPUS, INDEX, fold=fold, history_weight=w)
            ev = evaluate(r, queries)
            (r.base.db if hasattr(r, "base") else r.db).close()
            key = f"fold={'on' if fold else 'off'},w={w}"
            if w == 0.0:
                ref[fold] = ev["_tops"]
            changed = sum(1 for a, b in zip(ev["_tops"], ref[fold]) if a != b)
            changed_top1 = sum(1 for a, b in zip(ev["_tops"], ref[fold]) if a[:1] != b[:1])
            arms[key] = {"overall": ev["overall"], "by_type": ev["by_type"],
                         "changed_top10_vs_w0": changed, "changed_top1_vs_w0": changed_top1}
            print(key, ev["overall"], "temporal", ev["by_type"]["temporal"], "changed", changed, flush=True)
    write_json(RESULTS / "signal.json", {"date_facts": date_facts, "gap_facts": gap_facts, "arms": arms})
    print(json.dumps({"date_facts": date_facts, "gap_facts": gap_facts}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

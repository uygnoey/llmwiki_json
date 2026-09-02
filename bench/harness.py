#!/usr/bin/env python3
"""검색 벤치마크 harness. bench/SPEC.md 의 지표를 계산한다.

사용:
  python3 bench/harness.py --corpus bench/corpus --queries bench/queries.json \
      --rankers baseline,structural,vector --k 10
"""
from __future__ import annotations

import argparse
import importlib
import json
import math
import statistics
import sys
import time
import traceback
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bench"))
sys.path.insert(0, str(ROOT))  # scripts/ 를 import 하는 랭커(baseline)를 위해

from rankers.base import BuildStats, Hit, dir_bytes  # noqa: E402

TYPES = ("exact", "relation", "temporal", "crosslingual", "paraphrase")


# ------------------------------------------------------------------ 지표
def dcg(gains: list[float]) -> float:
    return sum(g / math.log2(i + 2) for i, g in enumerate(gains))


def query_metrics(hits: list[Hit], q: dict[str, Any], k: int) -> dict[str, float]:
    ranked = [h.page_id for h in hits[:k]]
    gold = set(q.get("gold_pages") or [])
    stale = set(q.get("stale_pages") or [])
    out: dict[str, float] = {}

    for n in (1, 5, 10):
        top = ranked[:n]
        inter = len(gold.intersection(top))
        out[f"recall@{n}"] = inter / len(gold) if gold else 0.0
        out[f"hit@{n}"] = 1.0 if inter else 0.0
    out["precision@5"] = len(gold.intersection(ranked[:5])) / 5.0 if gold else 0.0

    # MRR@10 — 첫 정답의 역순위
    out["mrr@10"] = 0.0
    for i, pid in enumerate(ranked[:10]):
        if pid in gold:
            out["mrr@10"] = 1.0 / (i + 1)
            break

    # nDCG@10 — 이진 적합도
    gains = [1.0 if pid in gold else 0.0 for pid in ranked[:10]]
    ideal = [1.0] * min(len(gold), 10)
    out["ndcg@10"] = (dcg(gains) / dcg(ideal)) if ideal and dcg(ideal) > 0 else 0.0

    # block_recall@5 — gold_blocks 있는 질문만 (없으면 None 처리용 -1)
    gold_blocks = set(q.get("gold_blocks") or [])
    if gold_blocks:
        got: set[str] = set()
        for h in hits[:5]:
            got.update(h.block_ids or [])
        out["block_recall@5"] = len(gold_blocks & got) / len(gold_blocks)
    else:
        out["block_recall@5"] = -1.0

    # staleness — 낡은 page 가 모든 정답보다 위에 있는가 (낮을수록 좋다)
    if stale:
        pos = {pid: i for i, pid in enumerate(ranked)}
        best_gold = min((pos[g] for g in gold if g in pos), default=None)
        best_stale = min((pos[s] for s in stale if s in pos), default=None)
        if best_stale is None:
            out["stale_above"] = 0.0
        elif best_gold is None:
            out["stale_above"] = 1.0
        else:
            out["stale_above"] = 1.0 if best_stale < best_gold else 0.0
    else:
        out["stale_above"] = -1.0
    return out


def aggregate(rows: list[dict[str, float]]) -> dict[str, float]:
    if not rows:
        return {}
    agg: dict[str, float] = {}
    for key in rows[0]:
        vals = [r[key] for r in rows if r[key] >= 0.0]  # -1 은 해당 없음
        agg[key] = round(statistics.fmean(vals), 4) if vals else float("nan")
        agg[f"{key}__n"] = len(vals)
    return agg


def pct(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    s = sorted(values)
    idx = min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1))))
    return round(s[idx], 3)


# ------------------------------------------------------------------ 실행
def run_ranker(name: str, corpus: Path, queries: list[dict[str, Any]],
               k: int, index_root: Path, opts: dict[str, Any],
               rebuild: bool) -> dict[str, Any]:
    label = name if not opts else name + "[" + ",".join(f"{k}={v}" for k, v in sorted(opts.items())) + "]"
    result: dict[str, Any] = {"ranker": label, "module": name, "opts": opts}
    try:
        mod = importlib.import_module(f"rankers.{name}")
    except Exception as exc:
        result["status"] = "unavailable"
        result["error"] = f"import: {exc}"
        return result

    cls = getattr(mod, "RANKER", None)
    if cls is None:
        for attr in vars(mod).values():
            if isinstance(attr, type) and hasattr(attr, "search") and hasattr(attr, "build"):
                cls = attr
                break
    if cls is None:
        result["status"] = "unavailable"
        result["error"] = "모듈에 Ranker 구현이 없다 (RANKER 를 노출해라)"
        return result

    suffix = "" if not opts else "-" + "-".join(f"{k}_{v}" for k, v in sorted(opts.items()))
    index_dir = index_root / f"{name}{suffix}"
    try:
        if rebuild or not index_dir.exists():
            t0 = time.perf_counter()
            stats = cls.build(corpus, index_dir, **opts)
            built_ms = (time.perf_counter() - t0) * 1000.0
            if isinstance(stats, BuildStats):
                result["build_ms"] = round(stats.elapsed_ms or built_ms, 1)
                result["index_bytes"] = stats.index_bytes or dir_bytes(index_dir)
                result["build_notes"] = stats.notes
            else:
                result["build_ms"] = round(built_ms, 1)
                result["index_bytes"] = dir_bytes(index_dir)
        else:
            result["build_ms"] = None
            result["index_bytes"] = dir_bytes(index_dir)
        ranker = cls.load(corpus, index_dir, **opts)
    except Exception as exc:
        result["status"] = "unavailable"
        result["error"] = f"build/load: {exc}"
        result["traceback"] = traceback.format_exc()[-1500:]
        return result

    dumped: list[dict[str, Any]] = []
    per_type: dict[str, list[dict[str, float]]] = {t: [] for t in TYPES}
    all_rows: list[dict[str, float]] = []
    lat: list[float] = []
    failures = 0
    for q in queries:
        t0 = time.perf_counter()
        try:
            hits = ranker.search(q["text"], k=k)
        except Exception:
            failures += 1
            hits = []
        lat.append((time.perf_counter() - t0) * 1000.0)
        row = query_metrics(hits, q, k)
        all_rows.append(row)
        per_type.setdefault(q.get("type", "?"), []).append(row)
        dumped.append({"id": q.get("id"), "type": q.get("type"),
                       "ranked": [h.page_id for h in hits[:k]],
                       "blocks": [list(h.block_ids or []) for h in hits[:k]]})

    result["status"] = "ok"
    result["queries"] = len(queries)
    result["search_failures"] = failures
    result["latency_ms"] = {"p50": pct(lat, 50), "p95": pct(lat, 95),
                            "p99": pct(lat, 99), "mean": round(statistics.fmean(lat), 3)}
    result["overall"] = aggregate(all_rows)
    result["by_type"] = {t: aggregate(rows) for t, rows in per_type.items() if rows}
    result["_per_query"] = dumped   # 오라클 융합 분석용. 요약 파일에서는 뺀다.
    return result


def table(results: list[dict[str, Any]]) -> str:
    cols = ["recall@5", "recall@10", "mrr@10", "ndcg@10", "block_recall@5", "stale_above"]
    lines = ["", "=== 전체 ===",
             f"{'ranker':<26}" + "".join(f"{c:>16}" for c in cols) + f"{'p50 ms':>10}{'p95 ms':>10}"]
    for r in results:
        if r["status"] != "ok":
            lines.append(f"{r['ranker']:<26}  UNAVAILABLE — {r.get('error', '')[:70]}")
            continue
        o = r["overall"]
        row = f"{r['ranker']:<26}" + "".join(f"{o.get(c, float('nan')):>16.4f}" for c in cols)
        row += f"{r['latency_ms']['p50']:>10}{r['latency_ms']['p95']:>10}"
        lines.append(row)

    for t in TYPES:
        head = False
        for r in results:
            if r["status"] != "ok" or t not in r.get("by_type", {}):
                continue
            if not head:
                lines += ["", f"=== {t} ===",
                          f"{'ranker':<26}" + "".join(f"{c:>16}" for c in cols)]
                head = True
            o = r["by_type"][t]
            lines.append(f"{r['ranker']:<26}"
                         + "".join(f"{o.get(c, float('nan')):>16.4f}" for c in cols))
    lines += ["", "stale_above 는 낮을수록 좋다 (낡은 주장이 정답보다 위에 온 비율).",
              "block_recall@5 / stale_above 의 nan 은 해당 유형에 라벨이 없다는 뜻."]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default="bench/corpus")
    ap.add_argument("--queries", default="bench/queries.json")
    ap.add_argument("--rankers", default="baseline,structural,vector")
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--limit", type=int, default=0, help="질문 수 상한 (0=전부)")
    ap.add_argument("--per-type", type=int, default=0,
                    help="유형마다 N개씩 층화 표본 (비싼 arm 용). 0=전부")
    ap.add_argument("--types", default="", help="쉼표로 유형 필터")
    ap.add_argument("--index-root", default="bench/index")
    ap.add_argument("--out", default="bench/results")
    ap.add_argument("--no-rebuild", action="store_true", help="기존 색인 재사용")
    ap.add_argument("--opt", action="append", default=[],
                    help="ranker 옵션. 예: structural:use_aliases=false")
    args = ap.parse_args()

    corpus = Path(args.corpus)
    payload = json.loads(Path(args.queries).read_text(encoding="utf-8"))
    queries = payload["queries"] if isinstance(payload, dict) else payload
    if args.types:
        keep = {t.strip() for t in args.types.split(",")}
        queries = [q for q in queries if q.get("type") in keep]
    if args.per_type:
        picked: list[dict[str, Any]] = []
        seen: dict[str, int] = {}
        for q in queries:
            t = q.get("type", "?")
            if seen.get(t, 0) < args.per_type:
                picked.append(q)
                seen[t] = seen.get(t, 0) + 1
        queries = picked
    if args.limit:
        queries = queries[:args.limit]

    opts_by_ranker: dict[str, dict[str, Any]] = {}
    for raw in args.opt:
        name, _, kv = raw.partition(":")
        key, _, val = kv.partition("=")
        parsed: Any = val
        if val.lower() in {"true", "false"}:
            parsed = val.lower() == "true"
        elif val.isdigit():
            parsed = int(val)
        opts_by_ranker.setdefault(name, {})[key] = parsed

    pages = payload.get("corpus_pages") if isinstance(payload, dict) else len(list(corpus.rglob("*.json")))
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    results = []
    arms: list[tuple[str, dict[str, Any]]] = []
    for spec in [n.strip() for n in args.rankers.split(",") if n.strip()]:
        # "vector:mode=query" 처럼 arm 단위 옵션을 인라인으로 받는다
        mod, _, inline = spec.partition(":")
        o = dict(opts_by_ranker.get(mod, {}))
        for kv in [x for x in inline.split(";") if x]:
            key, _, val = kv.partition("=")
            pv: Any = val
            if val.lower() in {"true", "false"}:
                pv = val.lower() == "true"
            elif val.isdigit():
                pv = int(val)
            o[key] = pv
        arms.append((mod, o))

    for name, arm_opts in arms:
        print(f"[harness] {name} {arm_opts or ''} 실행 중 … (질문 {len(queries)}개)", file=sys.stderr)
        r = run_ranker(name, corpus, queries, args.k, Path(args.index_root),
                       arm_opts, rebuild=not args.no_rebuild)
        r["corpus_pages"] = pages
        results.append(r)
        safe = r["ranker"].replace("/", "_").replace(" ", "")
        if args.per_type:
            safe += f"-sample{args.per_type}"
        dump = r.pop("_per_query", None)
        if dump is not None:
            (out_dir / f"{pages}p-{safe}.perquery.json").write_text(
                json.dumps(dump, ensure_ascii=False), encoding="utf-8")
        (out_dir / f"{pages}p-{safe}.json").write_text(
            json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[harness] {name}: {r['status']}", file=sys.stderr)

    print(table(results))
    (out_dir / f"{pages}p-summary.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

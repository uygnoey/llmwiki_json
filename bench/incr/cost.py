#!/usr/bin/env python3
"""증분 build 비용 — cold build, page 1/10/100/1000 변경 증분(5회 중앙값), 100라운드 누적, 바이트 동일.

    python3 bench/incr/cost.py --name frozen    # bench/frozen/corpus 10,000 page (임시 root 로 복사)
    python3 bench/incr/cost.py --name personal --wiki ~/llmwiki/wiki   # 개인 위키 복사본 (원본은 읽기만)

결과: bench/results_incr/cost_<name>.json. 임시 root 는 --work (기본 bench/index_incr/<name>) 아래.
제품 CLI 와 같은 함수(`llmwiki.build`) 를 부르므로 시간은 build 전체이고, `phases` 에 단계별 시간이 있다.
"""
from __future__ import annotations

import argparse
import json
import shutil
import time
from pathlib import Path
from typing import Any

from common import (ROOT, FROZEN_CORPUS, IDX, artifact_prints, clone_root, db_stats, derived_queries,
                    frozen_queries, llmwiki, load_pages, median, prepare_root, query_times, write_json, write_page)


def edit_body(root: Path, pages: dict, sources: dict, ids: list[str], tag: str) -> list[str]:
    rels = []
    for i, pid in enumerate(ids):
        page = json.loads(json.dumps(pages[pid]))
        bid = page["block_order"][0]
        block = page["blocks"][bid]
        text = str(block.get("source_text") or "") + f" {tag}-{i:04d}"
        block["source_text"] = text
        block["data"] = {**(block.get("data") or {}), "text": text}
        write_page(root, sources[pid], page)
        pages[pid] = page
        rels.append(sources[pid])
    return rels


def timed_build(ws: "llmwiki.Workspace", **kw: Any) -> dict[str, Any]:
    t = time.perf_counter()
    stats = llmwiki.build(ws, **kw)
    stats["wall_ms"] = round((time.perf_counter() - t) * 1000, 1)
    return stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True)
    ap.add_argument("--wiki", help="정본 디렉터리 (기본: bench/frozen/corpus)")
    ap.add_argument("--work", help="임시 root 를 둘 곳")
    ap.add_argument("--rounds", type=int, default=100)
    ap.add_argument("--repeat", type=int, default=5)
    args = ap.parse_args()
    wiki = Path(args.wiki).expanduser() if args.wiki else FROZEN_CORPUS
    work = Path(args.work) if args.work else ROOT / "bench" / "index_incr" / args.name
    base = prepare_root(work / "base", wiki, allow_aliases=wiki == FROZEN_CORPUS)
    ws = llmwiki.Workspace(base)
    pages, sources = load_pages(base)
    ids = sorted(pages)
    n_pages = len(ids)
    queries = frozen_queries() if wiki == FROZEN_CORPUS else derived_queries(pages)
    out: dict[str, Any] = {"name": args.name, "pages": n_pages, "queries": len(queries),
                           "sqlite": IDX.sqlite3.sqlite_version, "machine": {}}
    print(f"[{args.name}] {n_pages} pages, {len(queries)} queries", flush=True)

    # cold build ×3
    colds = [timed_build(ws, full=True) for _ in range(3)]
    out["cold_build"] = {"ms": median([c["wall_ms"] for c in colds]), "runs": [c["wall_ms"] for c in colds],
                         "phases": colds[-1]["phases"], "index_bytes": colds[-1]["index"]["bytes"]}
    out["query_cold"] = query_times(base, queries)
    print("cold", out["cold_build"], out["query_cold"], flush=True)
    snapshot = work / "snapshot"
    clone_root(base, snapshot)

    # 증분: 1/10/100/1000 (page 의 25% 를 넘으면 전량이라 그 아래로 자른다)
    scales = [n for n in (1, 10, 100, 1000) if n <= n_pages * llmwiki.FULL_FRACTION]
    out["incremental"] = {}
    for n in scales:
        runs: list[dict[str, Any]] = []
        for r in range(args.repeat):
            clone_root(snapshot, base)
            ws = llmwiki.Workspace(base)
            pages_r = json.loads(json.dumps({pid: pages[pid] for pid in ids[:n]}))
            rels = edit_body(base, {**pages, **pages_r}, sources, ids[:n], f"증분-{r}")
            st = timed_build(ws, changed=rels)
            assert st["mode"] == "incremental", st
            runs.append({"ms": st["wall_ms"], "phases": st["phases"], "delta": st["delta"]})
        out["incremental"][str(n)] = {"ms": median([r["ms"] for r in runs]), "runs": [r["ms"] for r in runs],
                                      "phases_last": runs[-1]["phases"],
                                      "index_ms": median([r["phases"].get("index", 0.0) for r in runs])}
        print("incremental", n, out["incremental"][str(n)], flush=True)

    # 100 라운드 × 10 page 누적 — 라운드마다 다른 page
    clone_root(snapshot, base)
    ws = llmwiki.Workspace(base)
    curve: list[dict[str, Any]] = []
    per_round = 10
    for r in range(args.rounds):
        chunk = ids[(r * per_round) % n_pages:(r * per_round) % n_pages + per_round]
        rels = edit_body(base, pages, sources, chunk, f"라운드-{r}")
        st = timed_build(ws, changed=rels)
        assert st["mode"] == "incremental", st
        row = {"round": r + 1, "ms": st["wall_ms"], "index_ms": st["phases"].get("index", 0.0),
               "published": db_stats(ws.search_db), "work": db_stats(ws.work_db)}
        if (r + 1) in (1, 10, 25, 50, 100) or r + 1 == args.rounds:
            row["query"] = query_times(base, queries)
        curve.append(row)
        if "query" in row:
            print("round", row, flush=True)
    out["rounds"] = {"per_round": per_round, "ms_median": median([c["ms"] for c in curve]),
                     "ms_max": max(c["ms"] for c in curve), "index_ms_median": median([c["index_ms"] for c in curve]),
                     "checkpoints": [c for c in curve if "query" in c]}
    inc_prints = artifact_prints(base)
    inc_sig = None
    from common import signatures  # noqa: E402
    inc_sig = signatures(base, queries)
    cold = timed_build(ws, full=True)
    cold_prints = artifact_prints(base)
    cold_sig = signatures(base, queries)
    out["after_rounds"] = {
        "bytes_identical_to_cold": inc_prints == cold_prints,
        "search_sqlite_identical": inc_prints.get("index/search.sqlite") == cold_prints.get("index/search.sqlite"),
        "differing": sorted(k for k in set(inc_prints) | set(cold_prints) if inc_prints.get(k) != cold_prints.get(k)),
        "results_identical": inc_sig == cold_sig,
        "cold_ms": cold["wall_ms"],
    }
    print("after rounds", out["after_rounds"], flush=True)
    write_json(ROOT / "bench" / "results_incr" / f"cost_{args.name}.json", out)
    shutil.rmtree(snapshot, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""과제 H-2: 비용 주장 재현 + 증분 누적 곡선(compact/merge 없이).

A. 5회 중앙값: structural2 전체 build, segmented 전체 build, 증분(4 종류 × 1/10/100/1000),
   load 시간(hook 이 매 프로세스마다 내는 비용), 500문항 조회 지연, map.json 쓰기 비용
   (prototype 의 page-only map vs production 형태 pages+blocks map), 변경 감지 비용
   (revision 비교 / mtime scan / 전체 sha 재계산).
B. 누적: 10 page 본문 수정을 100 라운드 연속 적용. 라운드별 갱신 ms, sqlite bytes,
   freelist, 체크포인트마다 500문항 p50/p95. 끝에 전체 재빌드·structural2 와 대조하고
   VACUUM INTO 로 바이트가 정규화되는지 본다.
C. churn: 매 라운드 이전 라운드의 추가 10 page 를 지우고 새 10 page 를 넣기를 100 라운드.
"""
from __future__ import annotations

import copy
import json
import shutil
import sqlite3
import statistics
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from bench.harness import pct  # noqa: E402
from bench.incremental.run_experiment import (  # noqa: E402
    CORPUS,
    build_case,
    hit_signature,
    load_page_files,
    load_queries,
)
from bench.incremental.segment_index import (  # noqa: E402
    SegmentedRanker,
    canonical,
    make_map,
    sha_text,
    write_map,
)
from bench.rankers.base import dir_bytes  # noqa: E402
from bench.rankers.structural2 import Structural2Ranker  # noqa: E402
from scripts.llmwiki import dump, safe_name  # noqa: E402

INDEX = ROOT / "bench" / "index_review_inc" / "cost"
RESULTS = ROOT / "bench" / "results_review_inc"
SEG_BASE_SRC = ROOT / "bench" / "index_inc" / "task_f" / "segment_base"
REPS = 5


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def med(values: list[float]) -> float:
    return round(statistics.median(values), 3)


def ms(fn) -> float:
    t0 = time.perf_counter()
    fn()
    return (time.perf_counter() - t0) * 1000.0


def replace_file(overlay: Path, rel: str, page: dict[str, Any]) -> None:
    path = overlay / rel
    if path.is_symlink() or path.exists():
        path.unlink()
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, page)


def db_stats(dbp: Path) -> dict[str, Any]:
    db = sqlite3.connect(f"file:{dbp}?mode=ro", uri=True)
    page_count = db.execute("PRAGMA page_count").fetchone()[0]
    freelist = db.execute("PRAGMA freelist_count").fetchone()[0]
    db.close()
    return {"bytes": dbp.stat().st_size, "page_count": page_count, "freelist_count": freelist,
            "freelist_ratio": round(freelist / page_count, 4) if page_count else 0.0}


def query_latency(ranker, queries, passes: int = 1) -> dict[str, float]:
    lat: list[float] = []
    for _ in range(passes):
        for q in queries:
            t0 = time.perf_counter()
            ranker.search(q["text"], 10)
            lat.append((time.perf_counter() - t0) * 1000.0)
    return {"p50": pct(lat, 50), "p95": pct(lat, 95), "mean": round(statistics.fmean(lat), 3)}


# ------------------------------------------------------------------ A
def phase_a(queries, pages, sources, base_map) -> dict[str, Any]:
    out: dict[str, Any] = {}
    s2_dir = INDEX / "s2"
    seg_dir = INDEX / "seg_full"
    runs = [Structural2Ranker.build(CORPUS, s2_dir).elapsed_ms for _ in range(REPS)]
    out["structural2_full_build_ms"] = {"runs": runs, "median": med(runs), "bytes": dir_bytes(s2_dir)}
    runs = [SegmentedRanker.build(CORPUS, seg_dir).elapsed_ms for _ in range(REPS)]
    out["segmented_full_build_ms"] = {"runs": runs, "median": med(runs), "bytes": dir_bytes(seg_dir)}
    print("A build", out, flush=True)

    # load 비용 (프로세스마다 한 번): 새 connection + 구조 재료화
    def load_seg():
        r = SegmentedRanker.load(CORPUS, seg_dir); r.db.close()
    def load_s2():
        r = Structural2Ranker.load(CORPUS, s2_dir); r.db.close()
    out["load_ms"] = {"segmented": med([ms(load_seg) for _ in range(REPS)]),
                      "structural2": med([ms(load_s2) for _ in range(REPS)])}
    # 첫 질의 포함(cold) vs warm
    def first_query(cls, d):
        r = cls.load(CORPUS, d); r.search(queries[0]["text"], 10); r.db.close()
    out["load_plus_first_query_ms"] = {"segmented": med([ms(lambda: first_query(SegmentedRanker, seg_dir)) for _ in range(REPS)]),
                                       "structural2": med([ms(lambda: first_query(Structural2Ranker, s2_dir)) for _ in range(REPS)])}
    r_seg = SegmentedRanker.load(CORPUS, seg_dir)
    r_s2 = Structural2Ranker.load(CORPUS, s2_dir)
    out["query_latency_ms"] = {"segmented": query_latency(r_seg, queries, 2), "structural2": query_latency(r_s2, queries, 2)}
    # 조회가 읽는 posting 행 수: 질문 토큰의 df 합(segmented 는 SQL 에 MAX_POST 상한이 없다)
    from bench.rankers.structural2 import tokenize
    rows_read = []
    for q in queries:
        terms = sorted(set(tokenize(q["text"])))
        marks = ",".join("?" * len(terms))
        rows_read.append(sum(df for _t, df in r_seg.db.execute(f"SELECT term,df FROM termstat WHERE term IN ({marks})", terms)))
    out["segmented_posting_rows_touched_per_query"] = {"p50": pct(rows_read, 50), "p95": pct(rows_read, 95), "max": max(rows_read)}
    r_seg.db.close(); r_s2.db.close()
    print("A load/query", out["load_ms"], out["load_plus_first_query_ms"], out["query_latency_ms"], out["segmented_posting_rows_touched_per_query"], flush=True)

    # 증분 5회 중앙값
    overlay = INDEX / "case_corpus"
    inc_work = INDEX / "inc_work"
    case_map = INDEX / "case_map.json"
    inc: dict[str, Any] = {}
    for scenario in ("body", "add", "delete", "supersedes"):
        for count in (1, 10, 100, 1000):
            payload, _changed = build_case(pages, sources, base_map, scenario, count, overlay)
            write_map(case_map, payload)
            runs = []
            for _ in range(REPS):
                if inc_work.exists():
                    shutil.rmtree(inc_work)
                shutil.copytree(seg_dir, inc_work)
                runs.append(SegmentedRanker.incremental_update(overlay, inc_work, case_map)["elapsed_ms"])
            inc[f"{scenario}/{count}"] = {"runs": runs, "median": med(runs)}
            print("A inc", scenario, count, med(runs), flush=True)
    out["incremental_ms"] = inc
    shutil.rmtree(overlay, ignore_errors=True); shutil.rmtree(inc_work, ignore_errors=True)

    # map.json 쓰기 비용: prototype(page-only) vs production 형태(pages+blocks, data_url)
    proto_map = base_map
    prod_map = {"schema_version": "1.0", "pages": {}, "blocks": {}}
    for pid in sorted(pages):
        p = pages[pid]
        prod_map["pages"][pid] = {"source": "wiki/" + sources[pid], "pointer": "", "data_url": f"pages/{safe_name(pid)}", "sha256": base_map["pages"][pid]["sha256"]}
        for bid in p["block_order"]:
            prod_map["blocks"][bid] = {"page_id": pid, "pointer": f"/blocks/{bid}", "kind": p["blocks"][bid]["kind"], "data_url": f"pages/{safe_name(pid)}"}
    tmp = INDEX / "map_write"
    tmp.mkdir(parents=True, exist_ok=True)
    out["map_write_ms"] = {
        "prototype_pages_only_pretty": med([ms(lambda: write_map(tmp / "proto.json", proto_map)) for _ in range(REPS)]),
        "production_pages_blocks_pretty": med([ms(lambda: dump(tmp / "prod.json", prod_map, pretty=True)) for _ in range(REPS)]),
        "production_pages_blocks_compact": med([ms(lambda: dump(tmp / "prodc.json", prod_map)) for _ in range(REPS)]),
        "prototype_bytes": (tmp / "proto.json").stat().st_size,
        "production_pretty_bytes": (tmp / "prod.json").stat().st_size,
        "production_compact_bytes": (tmp / "prodc.json").stat().st_size,
        "map_read_parse_ms_production": med([ms(lambda: json.loads((tmp / "prod.json").read_text(encoding="utf-8"))) for _ in range(REPS)]),
    }
    print("A map", out["map_write_ms"], flush=True)

    # 변경 감지 비용
    rev = tmp / "revision.json"
    write_json(rev, {"revision": "x" * 64})
    def rev_compare():
        for _ in range(100):
            json.loads(rev.read_text(encoding="utf-8"))["revision"] == "x" * 64
    def mtime_scan():
        newest = 0
        for path in CORPUS.rglob("*.json"):
            newest = max(newest, path.stat().st_mtime_ns)
    def hash_all():
        make_map(CORPUS)
    def hash_changed(n=10):
        for pid in sorted(pages)[:n]:
            path = CORPUS / sources[pid]
            sha_text(canonical(json.loads(path.read_text(encoding="utf-8"))))
    out["change_detection_ms"] = {
        "revision_read_compare": round(med([ms(rev_compare) for _ in range(REPS)]) / 100, 4),
        "mtime_scan_10k": med([ms(mtime_scan) for _ in range(REPS)]),
        "hash_all_10k_make_map": med([ms(hash_all) for _ in range(3)]),
        "hash_changed_10": med([ms(hash_changed) for _ in range(REPS)]),
    }
    print("A detect", out["change_detection_ms"], flush=True)
    return out


# ------------------------------------------------------------------ B
def phase_b(queries, pages, sources, base_map, rounds: int = 100, per_round: int = 10) -> dict[str, Any]:
    overlay = INDEX / "acc_corpus"
    work = INDEX / "acc_work"
    case_map = INDEX / "acc_map.json"
    payload, _ = build_case(pages, sources, base_map, "body", 0, overlay)
    if work.exists():
        shutil.rmtree(work)
    shutil.copytree(SEG_BASE_SRC, work)
    ids = sorted(pages)
    checkpoints = {0, 1, 10, 25, 50, 100}
    series = []
    latency = {}
    r = SegmentedRanker.load(overlay, work)
    latency["0"] = query_latency(r, queries)
    r.db.close()
    live: dict[str, dict[str, Any]] = {}
    for rnd in range(1, rounds + 1):
        for i in range(per_round):
            pid = ids[((rnd - 1) * per_round + i) % len(ids)]
            page = copy.deepcopy(live.get(pid) or pages[pid])
            bid = page["block_order"][0]
            block = page["blocks"][bid]
            text = str(block.get("source_text") or "") + f" 누적수정-{rnd:04d}"
            block["source_text"] = text
            block["data"] = {**(block.get("data") or {}), "text": text}
            block["fingerprint"] = sha_text(text)
            live[pid] = page
            replace_file(overlay, sources[pid], page)
            payload["pages"][pid]["sha256"] = sha_text(canonical(page))
        write_map(case_map, payload)
        delta = SegmentedRanker.incremental_update(overlay, work, case_map)
        st = db_stats(work / "segments.sqlite")
        series.append({"round": rnd, "update_ms": delta["elapsed_ms"], **st})
        if rnd in checkpoints:
            r = SegmentedRanker.load(overlay, work)
            latency[str(rnd)] = query_latency(r, queries)
            r.db.close()
            print("B round", rnd, series[-1], latency[str(rnd)], flush=True)
    full = INDEX / "acc_full"
    s2 = INDEX / "acc_s2"
    full_stats = SegmentedRanker.build(overlay, full)
    s2_stats = Structural2Ranker.build(overlay, s2)
    r_inc = SegmentedRanker.load(overlay, work)
    r_full = SegmentedRanker.load(overlay, full)
    r_s2 = Structural2Ranker.load(overlay, s2)
    sig_inc = [hit_signature(r_inc.search(q["text"], 10)) for q in queries]
    sig_full = [hit_signature(r_full.search(q["text"], 10)) for q in queries]
    sig_s2 = [hit_signature(r_s2.search(q["text"], 10)) for q in queries]
    for x in (r_inc, r_full, r_s2):
        x.db.close()
    # VACUUM INTO 정규화
    vac_inc = INDEX / "acc_vac_inc.sqlite"
    vac_full = INDEX / "acc_vac_full.sqlite"
    for target in (vac_inc, vac_full):
        target.unlink(missing_ok=True)
    def vacuum_into(src: Path, dst: Path):
        db = sqlite3.connect(src)
        db.execute("VACUUM INTO ?", (str(dst),))
        db.close()
    vac_ms = ms(lambda: vacuum_into(work / "segments.sqlite", vac_inc))
    vacuum_into(full / "segments.sqlite", vac_full)
    result = {
        "rounds": rounds, "per_round": per_round,
        "update_ms": {"first": series[0]["update_ms"], "median": med([s["update_ms"] for s in series]), "last": series[-1]["update_ms"],
                       "max": max(s["update_ms"] for s in series)},
        "bytes": {"round0": SEG_BASE_SRC.joinpath("segments.sqlite").stat().st_size, "last": series[-1]["bytes"], "full_rebuild": (full / "segments.sqlite").stat().st_size},
        "freelist_last": {k: series[-1][k] for k in ("page_count", "freelist_count", "freelist_ratio")},
        "latency_by_round": latency,
        "final_inc_vs_full_mismatch": sum(1 for a, b in zip(sig_inc, sig_full) if a != b),
        "final_inc_vs_structural2_mismatch": sum(1 for a, b in zip(sig_inc, sig_s2) if a != b),
        "final_full_build_ms": full_stats.elapsed_ms,
        "final_s2_build_ms": s2_stats.elapsed_ms,
        "raw_sqlite_bytes_equal": (work / "segments.sqlite").read_bytes() == (full / "segments.sqlite").read_bytes(),
        "vacuum_into_ms": round(vac_ms, 3),
        "vacuum_into_bytes": vac_inc.stat().st_size,
        "vacuumed_sqlite_bytes_equal": vac_inc.read_bytes() == vac_full.read_bytes(),
        "series": series,
    }
    for d in (overlay, work, full, s2):
        shutil.rmtree(d, ignore_errors=True)
    case_map.unlink(missing_ok=True)
    return result


# ------------------------------------------------------------------ C
def phase_c(queries, pages, sources, base_map, rounds: int = 100, per_round: int = 10) -> dict[str, Any]:
    overlay = INDEX / "churn_corpus"
    work = INDEX / "churn_work"
    case_map = INDEX / "churn_map.json"
    payload, _ = build_case(pages, sources, base_map, "body", 0, overlay)
    if work.exists():
        shutil.rmtree(work)
    shutil.copytree(SEG_BASE_SRC, work)
    ids = sorted(pages)
    series = []
    prev: list[tuple[str, str]] = []
    for rnd in range(1, rounds + 1):
        for pid, rel in prev:
            (overlay / rel).unlink()
            del payload["pages"][pid]
        prev = []
        for i in range(per_round):
            template = pages[ids[(rnd * per_round + i) % len(ids)]]
            page = copy.deepcopy(template)
            pid = f"page:churn-{rnd:04d}-{i:02d}"
            slug = f"churn-{rnd:04d}-{i:02d}"
            page["id"], page["slug"], page["title"] = pid, slug, f"churn {rnd} {i}"
            blocks, order = {}, []
            for seq, old in enumerate(page["block_order"]):
                nb = f"block:{slug}:{seq}"
                b = copy.deepcopy(page["blocks"][old]); b["id"] = nb
                blocks[nb] = b; order.append(nb)
            page["blocks"], page["block_order"] = blocks, order
            page["links"] = [l for l in page.get("links") or [] if l.get("kind") != "supersedes"]
            for l in page["links"]:
                l["block_id"] = order[0]
            rel = f"churn/{slug}.json"
            replace_file(overlay, rel, page)
            payload["pages"][pid] = {"source": rel, "pointer": "", "sha256": sha_text(canonical(page))}
            prev.append((pid, rel))
        payload["pages"] = dict(sorted(payload["pages"].items()))
        write_map(case_map, payload)
        delta = SegmentedRanker.incremental_update(overlay, work, case_map)
        series.append({"round": rnd, "update_ms": delta["elapsed_ms"], **db_stats(work / "segments.sqlite")})
        if rnd in (1, 10, 50, 100):
            print("C round", rnd, series[-1], flush=True)
    r = SegmentedRanker.load(overlay, work)
    lat = query_latency(r, queries)
    r.db.close()
    full = INDEX / "churn_full"
    SegmentedRanker.build(overlay, full)
    result = {
        "rounds": rounds, "per_round": per_round,
        "update_ms": {"median": med([s["update_ms"] for s in series]), "max": max(s["update_ms"] for s in series)},
        "bytes": {"round0": SEG_BASE_SRC.joinpath("segments.sqlite").stat().st_size, "last": series[-1]["bytes"], "full_rebuild": (full / "segments.sqlite").stat().st_size},
        "freelist_last": {k: series[-1][k] for k in ("page_count", "freelist_count", "freelist_ratio")},
        "latency_last": lat,
        "series": series,
    }
    for d in (overlay, work, full):
        shutil.rmtree(d, ignore_errors=True)
    case_map.unlink(missing_ok=True)
    return result


def main() -> int:
    queries = load_queries()
    pages, sources = load_page_files(CORPUS)
    base_map = make_map(CORPUS)
    INDEX.mkdir(parents=True, exist_ok=True)
    out = {"reps": REPS}
    out["A"] = phase_a(queries, pages, sources, base_map)
    write_json(RESULTS / "cost.json", out)
    out["B_accumulate"] = phase_b(queries, pages, sources, base_map)
    write_json(RESULTS / "cost.json", out)
    out["C_churn"] = phase_c(queries, pages, sources, base_map)
    write_json(RESULTS / "cost.json", out)
    print("done", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""과제 H-2 보조: 갱신 중 조회. journal_mode=DELETE 프로토타입에서 hook(읽기) 과
build(쓰기) 가 겹치면 어떤 일이 생기는지 잰다 — 오류인지, 대기(지연 spike) 인지."""
from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from bench.incremental.run_experiment import CORPUS, build_case, load_page_files, load_queries  # noqa: E402
from bench.incremental.segment_index import SegmentedRanker, make_map, write_map  # noqa: E402

INDEX = ROOT / "bench" / "index_review_inc" / "conc"
RESULTS = ROOT / "bench" / "results_review_inc"
SEG_BASE_SRC = ROOT / "bench" / "index_inc" / "task_f" / "segment_base"


def main() -> int:
    queries = load_queries()
    pages, sources = load_page_files(CORPUS)
    base_map = make_map(CORPUS)
    overlay = INDEX / "corpus"
    work = INDEX / "work"
    case_map = INDEX / "map.json"
    payload, _ = build_case(pages, sources, base_map, "body", 1000, overlay)
    write_map(case_map, payload)
    out = {}
    for label, timeout in (("python_default_timeout_5s", 5.0), ("timeout_0", 0.0)):
        if work.exists():
            shutil.rmtree(work)
        shutil.copytree(SEG_BASE_SRC, work)
        # 읽기 connection: 프로토타입 load 와 같은 mode=ro, timeout 만 바꿔 본다
        dbp = work / "segments.sqlite"
        db = sqlite3.connect(f"file:{dbp}?mode=ro", uri=True, check_same_thread=False, timeout=timeout)
        reader = SegmentedRanker(db, {})
        lat, errors, stop = [], [], threading.Event()
        opens_err = []

        def read_loop():
            i = 0
            while not stop.is_set():
                q = queries[i % len(queries)]
                t0 = time.perf_counter()
                try:
                    reader.search(q["text"], 10)
                except sqlite3.Error as exc:
                    errors.append(f"{type(exc).__name__}: {exc}")
                lat.append((time.perf_counter() - t0) * 1000.0)
                # 갱신 중 새 프로세스가 색인을 여는 상황
                try:
                    r2 = SegmentedRanker.load(overlay, work) if timeout else None
                    if r2 is not None:
                        r2.db.close()
                except sqlite3.Error as exc:
                    opens_err.append(f"{type(exc).__name__}: {exc}")
                i += 1

        th = threading.Thread(target=read_loop)
        th.start()
        time.sleep(0.3)
        t0 = time.perf_counter()
        try:
            delta = SegmentedRanker.incremental_update(overlay, work, case_map)
            write_ms = delta["elapsed_ms"]
            write_err = None
        except sqlite3.Error as exc:
            write_ms = (time.perf_counter() - t0) * 1000.0
            write_err = f"{type(exc).__name__}: {exc}"
        time.sleep(0.3)
        stop.set(); th.join()
        db.close()
        lat_sorted = sorted(lat)
        out[label] = {
            "reader_queries": len(lat), "reader_errors": len(errors), "reader_error_examples": errors[:3],
            "open_errors": len(opens_err), "open_error_examples": opens_err[:3],
            "reader_latency_ms": {"p50": round(lat_sorted[len(lat) // 2], 3), "max": round(lat_sorted[-1], 3)},
            "writer_ms": round(write_ms, 3), "writer_error": write_err,
        }
        print(label, out[label], flush=True)
    shutil.rmtree(overlay, ignore_errors=True); shutil.rmtree(work, ignore_errors=True)
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "concurrency.json").write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

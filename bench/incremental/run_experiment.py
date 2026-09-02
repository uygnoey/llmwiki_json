#!/usr/bin/env python3
"""과제 F의 비용·증분·동일성·JSON 신호 실험을 한 번에 재현한다."""
from __future__ import annotations

import argparse
import copy
import json
import shutil
import statistics
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from bench.harness import aggregate, pct, query_metrics
from bench.incremental.history_signal import HistoryAtRanker
from bench.incremental.segment_index import (
    SegmentedRanker,
    canonical,
    make_map,
    map_root,
    sha_text,
    write_map,
)
from bench.rankers.base import Hit, dir_bytes
from bench.rankers.structural2 import Structural2Ranker
from scripts.llmwiki import Workspace, dump, export_markdown, project


CORPUS = ROOT / "bench" / "frozen" / "corpus"
QUERIES_PATH = ROOT / "bench" / "frozen" / "queries.json"
RESULTS = ROOT / "bench" / "results_inc"
INDEX = ROOT / "bench" / "index_inc" / "task_f"
SEED = 1234
SCALES = (1, 10, 100, 1000)
SCENARIOS = ("body", "add", "delete", "supersedes")
DERIVED = ("catalog.json", "map.json", "search.json", "graph.json", "routes.json")


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def median(values: list[float]) -> float:
    return round(statistics.median(values), 3)


def load_queries() -> list[dict[str, Any]]:
    return json.loads(QUERIES_PATH.read_text(encoding="utf-8"))["queries"]


def load_page_files(corpus: Path) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    pages: dict[str, dict[str, Any]] = {}
    sources: dict[str, str] = {}
    for path in sorted(corpus.rglob("*.json")):
        value = json.loads(path.read_text(encoding="utf-8"))
        values = value if isinstance(value, list) else [value]
        for page in values:
            pid = str(page["id"])
            pages[pid] = page
            sources[pid] = path.relative_to(corpus).as_posix()
    return pages, sources


def measure_costs(rounds: int = 3) -> dict[str, Any]:
    temp = INDEX / "cost_workspace"
    if temp.exists():
        shutil.rmtree(temp)
    (temp / "tools" / "schema").mkdir(parents=True)
    (temp / "tools" / "config").mkdir(parents=True)
    (temp / "wiki").symlink_to(CORPUS, target_is_directory=True)
    shutil.copy2(ROOT / "tools" / "config" / "groups.json", temp / "tools" / "config" / "groups.json")
    schema = json.loads((ROOT / "tools" / "schema" / "page.schema.json").read_text(encoding="utf-8"))
    # frozen 계약의 제안 필드 aliases만 임시 schema에 허용한다. 저장소 schema는 그대로다.
    schema["properties"]["aliases"] = {
        "type": "object",
        "additionalProperties": {"type": "array", "items": {"type": "string"}},
    }
    write_json(temp / "tools" / "schema" / "page.schema.json", schema)
    ws = Workspace(temp)

    project_ms: list[float] = []
    payloads: dict[str, Any] = {}
    for _ in range(rounds):
        t0 = time.perf_counter()
        payloads = project(ws)
        project_ms.append((time.perf_counter() - t0) * 1000.0)
    write_start = time.perf_counter()
    for name in DERIVED:
        dump(ws.index / name, payloads[name], pretty=True)
    write_ms = (time.perf_counter() - write_start) * 1000.0
    derived_bytes = {name: (ws.index / name).stat().st_size for name in DERIVED}
    normalized_map = copy.deepcopy(payloads["map.json"])
    corpus_prefix = str(CORPUS.resolve()) + "/"
    for entry in normalized_map["pages"].values():
        source = str(entry.get("source") or "")
        if source.startswith(corpus_prefix):
            entry["source"] = "wiki/" + source[len(corpus_prefix):]
    normalized_map_bytes = len(
        (json.dumps(normalized_map, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    )

    markdown_ms: list[float] = []
    markdown_dir = temp / "markdown_out"
    for _ in range(rounds):
        t0 = time.perf_counter()
        export_markdown(ws, markdown_dir)
        markdown_ms.append((time.perf_counter() - t0) * 1000.0)
    md_files = sorted(markdown_dir.glob("*.md"))
    markdown_payload_bytes = sum(path.stat().st_size for path in md_files)
    manifest_bytes = (markdown_dir / "manifest.json").stat().st_size

    corpus_files = sorted(CORPUS.rglob("*.json"))
    corpus_bytes = sum(path.stat().st_size for path in corpus_files)
    derived_total = sum(derived_bytes.values())
    search_bytes = derived_bytes["search.json"]
    search_text_bytes = sum(len(row["text"].encode("utf-8")) for row in payloads["search.json"])

    actual_pages = []
    for path in sorted((ROOT / "wiki").rglob("*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and str(value.get("id") or "").startswith("page:"):
            actual_pages.append(value)
    actual_sources = [page for page in actual_pages if page.get("type") == "source"]
    source_text_sizes = [
        len(str((page.get("source_snapshot") or {}).get("text") or "").encode("utf-8"))
        for page in actual_sources
        if page.get("source_snapshot")
    ]
    all_snapshot_sizes = [
        len(str((page.get("source_snapshot") or {}).get("text") or "").encode("utf-8"))
        for page in actual_pages
        if page.get("source_snapshot")
    ]
    source_ratio = len(actual_sources) / len(actual_pages) if actual_pages else 0.0
    projected_sources = round(10000 * source_ratio)
    source_average = statistics.fmean(source_text_sizes) if source_text_sizes else 0.0
    estimate_actual_mix = round(projected_sources * source_average)
    estimate_frozen_mix = round(5000 * source_average)  # frozen 계약: source 50%
    estimate_all_snapshots = round(10000 * statistics.fmean(all_snapshot_sizes)) if all_snapshot_sizes else 0

    result = {
        "seed": SEED,
        "pages": len(corpus_files),
        "rounds": rounds,
        "temporary_alias_schema_extension": True,
        "project_ms": {"runs": [round(v, 3) for v in project_ms], "median": median(project_ms)},
        "derived_write_ms": round(write_ms, 3),
        "derived_bytes": derived_bytes,
        "map_path_normalization": {
            "measured_symlink_map_bytes": derived_bytes["map.json"],
            "production_relative_path_estimate_bytes": normalized_map_bytes,
            "symlink_absolute_path_overhead_bytes": derived_bytes["map.json"] - normalized_map_bytes,
            "estimated": True,
        },
        "derived_total_bytes": derived_total,
        "canonical_corpus_bytes": corpus_bytes,
        "search_json": {
            "bytes": search_bytes,
            "text_field_bytes": search_text_bytes,
            "share_of_derived": round(search_bytes / derived_total, 6),
            "share_of_canonical_plus_derived": round(search_bytes / (corpus_bytes + derived_total), 6),
        },
        "markdown": {
            "runs_ms": [round(v, 3) for v in markdown_ms],
            "median_ms": median(markdown_ms),
            "pages": len(md_files),
            "payload_bytes": markdown_payload_bytes,
            "manifest_bytes": manifest_bytes,
            "total_bytes": markdown_payload_bytes + manifest_bytes,
            "share_of_canonical": round((markdown_payload_bytes + manifest_bytes) / corpus_bytes, 6),
        },
        "source_snapshot_text_estimate": {
            "estimated": True,
            "actual_wiki_pages": len(actual_pages),
            "actual_wiki_source_pages": len(actual_sources),
            "actual_source_page_ratio": round(source_ratio, 6),
            "actual_source_snapshots_sampled": len(source_text_sizes),
            "actual_source_snapshot_mean_bytes": round(source_average, 3),
            "projected_source_pages_at_10000_actual_mix": projected_sources,
            "projected_text_bytes_at_10000_actual_mix": estimate_actual_mix,
            "projected_text_bytes_at_10000_frozen_50pct_sources": estimate_frozen_mix,
            "upper_scenario_all_pages_snapshot_mean_bytes": estimate_all_snapshots,
            "share_of_canonical_actual_mix": round(estimate_actual_mix / (corpus_bytes + estimate_actual_mix), 6),
        },
    }
    write_json(RESULTS / "costs.json", result)
    return result


def _replace_overlay_file(overlay: Path, rel: str, page: dict[str, Any]) -> None:
    path = overlay / rel
    if path.is_symlink() or path.exists():
        path.unlink()
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, page)


def build_case(
    base_pages: dict[str, dict[str, Any]],
    base_sources: dict[str, str],
    base_map: dict[str, Any],
    scenario: str,
    count: int,
    overlay: Path,
) -> tuple[dict[str, Any], list[str]]:
    if overlay.exists():
        shutil.rmtree(overlay)
    overlay.mkdir(parents=True)
    for rel in sorted(set(base_sources.values())):
        dst = overlay / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.symlink_to(CORPUS / rel)
    payload = copy.deepcopy(base_map)
    ids = sorted(base_pages)
    changed: list[str] = []

    if scenario == "body":
        selected = ids[:count]
        for idx, pid in enumerate(selected):
            page = copy.deepcopy(base_pages[pid])
            bid = page["block_order"][0]
            block = page["blocks"][bid]
            text = str(block.get("source_text") or "") + f" 증분본문수정-{idx:04d}"
            block["source_text"] = text
            data = dict(block.get("data") or {})
            data["text"] = text
            block["data"] = data
            block["fingerprint"] = sha_text(text)
            rel = base_sources[pid]
            _replace_overlay_file(overlay, rel, page)
            payload["pages"][pid]["sha256"] = sha_text(canonical(page))
            changed.append(pid)
    elif scenario == "add":
        templates = ids[:count]
        for idx, template_id in enumerate(templates):
            page = copy.deepcopy(base_pages[template_id])
            pid = f"page:inc-added-{idx:06d}"
            slug = f"inc-added-{idx:06d}"
            page["id"] = pid
            page["slug"] = slug
            page["title"] = f"증분 추가 문서 {idx:06d}"
            remap: dict[str, str] = {}
            new_blocks: dict[str, Any] = {}
            new_order: list[str] = []
            for seq, old_bid in enumerate(page["block_order"]):
                new_bid = f"block:inc-added-{idx:06d}-{seq:03d}"
                remap[old_bid] = new_bid
                block = copy.deepcopy(page["blocks"][old_bid])
                block["id"] = new_bid
                new_blocks[new_bid] = block
                new_order.append(new_bid)
            page["blocks"] = new_blocks
            page["block_order"] = new_order
            for link in page.get("links") or []:
                if link.get("block_id") in remap:
                    link["block_id"] = remap[link["block_id"]]
            page["history"] = [{"at": "2026-09-02", "action": "incremental-add", "actor": "bench-inc"}]
            rel = f"sources/{slug}.json"
            _replace_overlay_file(overlay, rel, page)
            payload["pages"][pid] = {
                "source": rel,
                "pointer": "",
                "sha256": sha_text(canonical(page)),
            }
            changed.append(pid)
    elif scenario == "delete":
        selected = ids[:count]
        for pid in selected:
            rel = base_sources[pid]
            path = overlay / rel
            path.unlink()
            del payload["pages"][pid]
            changed.append(pid)
    elif scenario == "supersedes":
        candidates = [
            pid
            for pid in ids
            if not any(link.get("kind") == "supersedes" for link in base_pages[pid].get("links") or [])
        ]
        selected = candidates[:count]
        for idx, pid in enumerate(selected):
            page = copy.deepcopy(base_pages[pid])
            target = ids[(ids.index(pid) + 5000 + idx) % len(ids)]
            if target == pid:
                target = ids[(ids.index(pid) + 1) % len(ids)]
            bid = page["block_order"][0]
            page.setdefault("links", []).append(
                {"target": target, "kind": "supersedes", "block_id": bid}
            )
            rel = base_sources[pid]
            _replace_overlay_file(overlay, rel, page)
            payload["pages"][pid]["sha256"] = sha_text(canonical(page))
            changed.append(pid)
    else:
        raise ValueError(scenario)
    payload["pages"] = dict(sorted(payload["pages"].items()))
    return payload, changed


def hit_signature(hits: list[Hit]) -> list[tuple[str, float, tuple[str, ...]]]:
    return [(hit.page_id, hit.score, tuple(hit.block_ids)) for hit in hits]


def compare_500(
    left: SegmentedRanker,
    right: SegmentedRanker,
    queries: list[dict[str, Any]],
) -> dict[str, Any]:
    mismatches = []
    t_left: list[float] = []
    t_right: list[float] = []
    for query in queries:
        t0 = time.perf_counter()
        a = hit_signature(left.search(query["text"], 10))
        t_left.append((time.perf_counter() - t0) * 1000.0)
        t0 = time.perf_counter()
        b = hit_signature(right.search(query["text"], 10))
        t_right.append((time.perf_counter() - t0) * 1000.0)
        if a != b:
            mismatches.append({"id": query["id"], "incremental": a, "full": b})
    return {
        "queries": len(queries),
        "exact_hits_scores_blocks": not mismatches,
        "mismatch_count": len(mismatches),
        "mismatch_examples": mismatches[:3],
        "incremental_index_search_ms": {"p50": pct(t_left, 50), "p95": pct(t_left, 95)},
        "full_index_search_ms": {"p50": pct(t_right, 50), "p95": pct(t_right, 95)},
    }


def measure_staleness(revision_path: Path, expected: str, rounds: int = 7) -> dict[str, Any]:
    revision_path.parent.mkdir(parents=True, exist_ok=True)
    write_json(revision_path, {"schema_version": "1.0", "revision": expected})
    loops = 2000
    root_runs = []
    for _ in range(rounds):
        t0 = time.perf_counter()
        ok = True
        for _n in range(loops):
            value = json.loads(revision_path.read_text(encoding="utf-8"))["revision"]
            ok = ok and value == expected
        if not ok:
            raise AssertionError("revision 비교 실패")
        root_runs.append((time.perf_counter() - t0) * 1000.0 / loops)
    mtime_runs = []
    matched = 0
    for _ in range(rounds):
        t0 = time.perf_counter()
        matched = 0
        newest = 0
        for path in CORPUS.rglob("*.json"):
            newest = max(newest, path.stat().st_mtime_ns)
            matched += 1
        if newest <= 0:
            raise AssertionError("mtime scan 실패")
        mtime_runs.append((time.perf_counter() - t0) * 1000.0)
    result = {
        "pages": matched,
        "rounds": rounds,
        "revision_root_compare_ms": {"runs": [round(v, 6) for v in root_runs], "median": median(root_runs)},
        "mtime_scan_ms": {"runs": [round(v, 3) for v in mtime_runs], "median": median(mtime_runs)},
        "speedup": round(median(mtime_runs) / median(root_runs), 2),
    }
    write_json(RESULTS / "staleness.json", result)
    return result


def evaluate_signal(queries: list[dict[str, Any]], structural_index: Path) -> dict[str, Any]:
    base = Structural2Ranker.load(CORPUS, structural_index)
    history = HistoryAtRanker.load(CORPUS, structural_index, history_weight=0.02)
    arms: dict[str, list[dict[str, float]]] = {"structural2": [], "history_at": []}
    per_type: dict[str, dict[str, list[dict[str, float]]]] = {
        name: {} for name in arms
    }
    latency: dict[str, list[float]] = {name: [] for name in arms}
    changed_rankings = 0
    for query in queries:
        outputs: dict[str, list[Hit]] = {}
        for name, ranker in (("structural2", base), ("history_at", history)):
            t0 = time.perf_counter()
            hits = ranker.search(query["text"], 10)
            latency[name].append((time.perf_counter() - t0) * 1000.0)
            outputs[name] = hits
            row = query_metrics(hits, query, 10)
            arms[name].append(row)
            per_type[name].setdefault(query["type"], []).append(row)
        if [h.page_id for h in outputs["structural2"]] != [h.page_id for h in outputs["history_at"]]:
            changed_rankings += 1
    result_arms = {}
    for name in arms:
        result_arms[name] = {
            "overall": aggregate(arms[name]),
            "by_type": {kind: aggregate(rows) for kind, rows in per_type[name].items()},
            "latency_ms": {
                "p50": pct(latency[name], 50),
                "p95": pct(latency[name], 95),
                "mean": round(statistics.fmean(latency[name]), 3),
            },
        }
    result = {
        "signal": "history.at",
        "implementation": "structural2 top-100 후보에 날짜 분위수 * 0.02를 더하는 wrapper",
        "queries": len(queries),
        "changed_top10_rankings": changed_rankings,
        "arms": result_arms,
        "recall_at_5_delta": round(
            result_arms["history_at"]["overall"]["recall@5"]
            - result_arms["structural2"]["overall"]["recall@5"],
            4,
        ),
    }
    write_json(RESULTS / "signal_history_at.json", result)
    return result


def measure_incremental() -> dict[str, Any]:
    queries = load_queries()
    pages, sources = load_page_files(CORPUS)
    base_map = make_map(CORPUS)
    structural_index = INDEX / "structural2"
    structural_runs = []
    structural_stats = None
    for _ in range(3):
        structural_stats = Structural2Ranker.build(CORPUS, structural_index)
        structural_runs.append(structural_stats.elapsed_ms)
    assert structural_stats is not None

    segment_base = INDEX / "segment_base"
    segment_stats = SegmentedRanker.build(CORPUS, segment_base)
    base_segment = SegmentedRanker.load(CORPUS, segment_base)
    base_structural = Structural2Ranker.load(CORPUS, structural_index)
    base_mismatches = []
    for query in queries:
        a = hit_signature(base_segment.search(query["text"], 10))
        b = hit_signature(base_structural.search(query["text"], 10))
        if a != b:
            base_mismatches.append(query["id"])

    rows = []
    overlay = INDEX / "case_corpus"
    inc_work = INDEX / "inc_work"
    full_work = INDEX / "full_work"
    case_map_path = INDEX / "case_map.json"
    for scenario in SCENARIOS:
        for count in SCALES:
            payload, changed_ids = build_case(
                pages, sources, base_map, scenario, count, overlay
            )
            write_map(case_map_path, payload)
            if inc_work.exists():
                shutil.rmtree(inc_work)
            shutil.copytree(segment_base, inc_work)
            delta = SegmentedRanker.incremental_update(overlay, inc_work, case_map_path)
            full_stats = SegmentedRanker.build(overlay, full_work)
            inc_ranker = SegmentedRanker.load(overlay, inc_work)
            full_ranker = SegmentedRanker.load(overlay, full_work)
            equality = compare_500(inc_ranker, full_ranker, queries)
            inc_ranker.db.close()
            full_ranker.db.close()
            inc_db = inc_work / "segments.sqlite"
            full_db = full_work / "segments.sqlite"
            inc_map = json.loads((inc_work / "map.json").read_text(encoding="utf-8"))
            full_map = json.loads((full_work / "map.json").read_text(encoding="utf-8"))
            rows.append(
                {
                    "scenario": scenario,
                    "requested_pages": count,
                    "changed_ids": changed_ids[:10],
                    "changed_ids_truncated": len(changed_ids) > 10,
                    "delta": delta,
                    "incremental_ms": delta["elapsed_ms"],
                    "full_rebuild_ms": full_stats.elapsed_ms,
                    "speedup": round(full_stats.elapsed_ms / delta["elapsed_ms"], 3),
                    "incremental_index_bytes": dir_bytes(inc_work),
                    "full_index_bytes": dir_bytes(full_work),
                    "sqlite_bytes_equal": inc_db.read_bytes() == full_db.read_bytes(),
                    "map_root_equal": map_root(inc_map) == map_root(full_map),
                    "query_equivalence": equality,
                }
            )
            write_json(RESULTS / "incremental.partial.json", {"cases": rows})
            shutil.rmtree(inc_work)
            shutil.rmtree(full_work)
            shutil.rmtree(overlay)
    case_map_path.unlink(missing_ok=True)

    signal = evaluate_signal(queries, structural_index)
    stale = measure_staleness(INDEX / "revision.json", map_root(base_map))
    result = {
        "seed": SEED,
        "pages": len(pages),
        "queries": len(queries),
        "structural2_full_build": {
            "runs_ms": structural_runs,
            "median_ms": median(structural_runs),
            "index_bytes": structural_stats.index_bytes,
        },
        "segmented_full_build": {
            "elapsed_ms": segment_stats.elapsed_ms,
            "index_bytes": segment_stats.index_bytes,
            "notes": segment_stats.notes,
        },
        "segmented_vs_structural2_base": {
            "exact_hits_scores_blocks": not base_mismatches,
            "mismatch_count": len(base_mismatches),
            "mismatch_examples": base_mismatches[:10],
        },
        "cases": rows,
        "signal_summary": {
            "signal": signal["signal"],
            "recall_at_5_delta": signal["recall_at_5_delta"],
        },
        "staleness_summary": stale,
    }
    write_json(RESULTS / "incremental.json", result)
    (RESULTS / "incremental.partial.json").unlink(missing_ok=True)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--costs-only", action="store_true")
    parser.add_argument("--incremental-only", action="store_true")
    args = parser.parse_args()
    RESULTS.mkdir(parents=True, exist_ok=True)
    INDEX.mkdir(parents=True, exist_ok=True)
    if not args.incremental_only:
        costs = measure_costs()
        print(json.dumps({"costs": costs}, ensure_ascii=False, indent=2))
    if not args.costs_only:
        incremental = measure_incremental()
        print(json.dumps({"incremental": incremental}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

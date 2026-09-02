#!/usr/bin/env python3
"""과제 H-3: Markdown 경로 비용 주장(§a) 재현과 가정 검증.

codex 의 cost_workspace 방식(임시 root + wiki symlink → frozen corpus, aliases 만 schema 에
허용) 을 그대로 재현해 project()/export_markdown()/build()/stale_index() 를 5회 중앙값으로
재고, source_snapshot 추정의 근거를 실제 wiki/ 6 page 에서 다시 센다.
"""
from __future__ import annotations

import json
import shutil
import statistics
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from bench.incremental.run_experiment import CORPUS  # noqa: E402
from scripts.llmwiki import Workspace, build, canonical, dump, export_markdown, project, render_markdown, stale_index  # noqa: E402

WS = ROOT / "bench" / "index_review_inc" / "ws"
RESULTS = ROOT / "bench" / "results_review_inc"
REPS = 5
DERIVED = ("catalog.json", "map.json", "search.json", "graph.json", "routes.json", "stats.json")


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def med(v): return round(statistics.median(v), 3)


def ms(fn):
    t0 = time.perf_counter(); fn(); return (time.perf_counter() - t0) * 1000.0


def make_ws() -> Workspace:
    if WS.exists():
        shutil.rmtree(WS)
    (WS / "tools" / "schema").mkdir(parents=True)
    (WS / "tools" / "config").mkdir(parents=True)
    (WS / "wiki").symlink_to(CORPUS, target_is_directory=True)
    shutil.copy2(ROOT / "tools" / "config" / "groups.json", WS / "tools" / "config" / "groups.json")
    schema = json.loads((ROOT / "tools" / "schema" / "page.schema.json").read_text(encoding="utf-8"))
    schema["properties"]["aliases"] = {"type": "object", "additionalProperties": {"type": "array", "items": {"type": "string"}}}
    write_json(WS / "tools" / "schema" / "page.schema.json", schema)
    return Workspace(WS)


def main() -> int:
    ws = make_ws()
    out: dict[str, Any] = {"reps": REPS}
    payloads = None
    runs = []
    for _ in range(REPS):
        t0 = time.perf_counter(); payloads = project(ws); runs.append((time.perf_counter() - t0) * 1000.0)
    out["project_ms"] = {"runs": [round(r, 1) for r in runs], "median": med(runs)}
    for name in DERIVED:
        dump(ws.index / name, payloads[name], pretty=True)
    out["derived_bytes"] = {name: (ws.index / name).stat().st_size for name in DERIVED}
    out["search_text_bytes"] = sum(len(r["text"].encode("utf-8")) for r in payloads["search.json"])
    out["corpus_bytes"] = sum(p.stat().st_size for p in CORPUS.rglob("*.json"))
    print("project", out["project_ms"], out["derived_bytes"], flush=True)

    md_dir = WS / "markdown_out"
    runs = [ms(lambda: export_markdown(ws, md_dir)) for _ in range(REPS)]
    md_files = sorted(md_dir.glob("*.md"))
    out["export_markdown_ms"] = {"runs": [round(r, 1) for r in runs], "median": med(runs)}
    out["markdown_bytes"] = {"md": sum(p.stat().st_size for p in md_files), "manifest": (md_dir / "manifest.json").stat().st_size, "files": len(md_files)}
    print("markdown", out["export_markdown_ms"], out["markdown_bytes"], flush=True)

    # production build() 전체(=project + index/*.json + viewer/public/data + 10k shard) 와 stale_index()
    runs = [ms(lambda: build(ws)) for _ in range(3)]
    out["production_build_ms"] = {"runs": [round(r, 1) for r in runs], "median": med(runs)}
    runs = [ms(lambda: stale_index(ws)) for _ in range(3)]
    out["stale_index_ms"] = {"runs": [round(r, 1) for r in runs], "median": med(runs), "note": "index/map.json vs project() 전량 재투영"}
    print("build/stale", out["production_build_ms"], out["stale_index_ms"], flush=True)

    # 실제 wiki/ 의 source_snapshot 과 markdown 비율
    real = []
    for path in sorted((ROOT / "wiki").rglob("*.json")):
        try:
            p = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if not (isinstance(p, dict) and str(p.get("id") or "").startswith("page:")):
            continue
        snap = (p.get("source_snapshot") or {}).get("text") or ""
        md = render_markdown(p)
        real.append({"file": path.relative_to(ROOT).as_posix(), "type": p.get("type"), "file_bytes": path.stat().st_size,
                     "snapshot_bytes": len(snap.encode("utf-8")), "markdown_bytes": len(md.encode("utf-8")),
                     "blocks": len(p.get("blocks") or {}),
                     "block_chars_mean": round(statistics.fmean(len(b.get("source_text") or "") for b in p["blocks"].values()), 1)})
    tot_file = sum(r["file_bytes"] for r in real); tot_snap = sum(r["snapshot_bytes"] for r in real); tot_md = sum(r["markdown_bytes"] for r in real)
    out["real_wiki"] = {"pages": real, "pages_with_snapshot": sum(1 for r in real if r["snapshot_bytes"]),
                        "snapshot_share_of_file_bytes": round(tot_snap / tot_file, 4),
                        "markdown_share_of_file_bytes": round(tot_md / tot_file, 4),
                        "frozen_pages_with_snapshot": sum(1 for p in list(CORPUS.rglob("*.json"))[:200] if "source_snapshot" in json.loads(p.read_text(encoding="utf-8")))}
    # frozen 코퍼스 block 길이
    lens = []
    for p in list(sorted(CORPUS.rglob("*.json")))[:2000]:
        page = json.loads(p.read_text(encoding="utf-8"))
        lens.extend(len(b.get("source_text") or "") for b in page["blocks"].values())
    out["frozen_block_chars"] = {"mean": round(statistics.fmean(lens), 1), "max": max(lens), "sampled_pages": 2000}
    write_json(RESULTS / "mdcost.json", out)
    print(json.dumps(out["real_wiki"], ensure_ascii=False, indent=1), out["frozen_block_chars"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

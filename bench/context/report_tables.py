#!/usr/bin/env python3
"""results_ctx 의 summary 에서 CONTEXT_REPORT.md 용 markdown 표를 찍는다."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TYPES = ("exact", "relation", "temporal", "crosslingual", "paraphrase")


def f(v, d=3):
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.{d}f}"
    return str(v)


def main(out_dir: str = "bench/results_ctx") -> int:
    summary = json.loads((ROOT / out_dir / "500q-summary.json").read_text(encoding="utf-8"))
    by = {(s["arm"], s["budget"]): s for s in summary}
    arms = []
    for s in summary:
        if s["arm"] not in arms:
            arms.append(s["arm"])
    print("## 예산별 비교표\n")
    print("| arm | 예산 | gold_block | gold_page | gold_addr | stale_body | stale_leak | bytes 평균 | est_tokens | B/정답 | p50 ms | p95 ms |")
    print("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for arm in arms:
        for b in (2000, 4000, 6000):
            s = by.get((arm, b))
            if not s:
                continue
            o = s["overall"]
            print(f"| {arm} | {b} | {f(o['gold_block_in_payload'])} | {f(o['gold_page_in_payload'])} | "
                  f"{f(o['gold_addr_in_payload'])} | {f(o['stale_body_in_payload'])} | {f(o['stale_leak'])} | "
                  f"{f(o['payload_bytes_mean'],0)} | {f(o['est_tokens_mean'],0)} | {f(o['bytes_per_gold_hit'],0)} | "
                  f"{o['latency_ms']['p50']} | {o['latency_ms']['p95']} |")
    print("\n## 유형별 (예산 6000)\n")
    print("| arm | " + " | ".join(f"{t} blk / B/정답" for t in TYPES) + " |")
    print("|---|" + "---:|" * len(TYPES))
    for arm in arms:
        s = by.get((arm, 6000))
        if not s:
            continue
        cells = []
        for t in TYPES:
            o = s["by_type"].get(t, {})
            cells.append(f"{f(o.get('gold_block_in_payload'),2)} / {f(o.get('bytes_per_gold_hit'),0)}")
        print(f"| {arm} | " + " | ".join(cells) + " |")
    print("\n## 하루 100 프롬프트 비용 (예산 6000)\n")
    print("| arm | est_tokens/프롬프트 | tokens/일 | $/월 Opus 5 | $/월 Sonnet 5 |")
    print("|---|---:|---:|---:|---:|")
    for arm in arms:
        s = by.get((arm, 6000))
        if not s:
            continue
        o = s["overall"]
        u = o["usd_per_month_100_prompts"]
        print(f"| {arm} | {f(o['est_tokens_mean'],0)} | {o['tokens_per_day_100_prompts']:,} | {u['claude-opus-5']} | {u['claude-sonnet-5']} |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(*sys.argv[1:]))

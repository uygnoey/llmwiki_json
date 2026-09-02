#!/usr/bin/env python3
"""컨텍스트 페이로드 하네스 — 훅이 실제로 LLM 에 넣는 문자열을 잰다.

검색 순위(bench/harness.py)가 아니라 **페이로드**를 잰다: 정답 block 이 예산 안에
들어갔는지, 몇 바이트를 써서 들어갔는지, 낡은 page 의 block 이 "대체됨" 표시
없이 섞였는지, 검색+투영+렌더 전체 지연.

사용:
  python3 bench/context/harness_ctx.py --corpus bench/frozen/corpus \
      --queries bench/frozen/queries.json --arms production,v2-text,v2-graph,v2-graph-json,v2-address \
      --budgets 2000,4000,6000 --index-root bench/index_ctx --out bench/results_ctx

지표(질문마다, 예산마다):
  payload_bytes / est_tokens      UTF-8 바이트, scripts.llmwiki_context.est_tokens (3바이트=1토큰)
  gold_page_in_payload            gold page 의 머리가 페이로드에 있다
  gold_block_in_payload           gold block 의 본문이 페이로드에 있다 (arm 의 manifest 와 본문 문자열 검사 둘 다 참)
  gold_addr_in_payload            gold block 의 주소(id) 가 페이로드에 있다 (본문 유무 무관)
  stale_body_in_payload           stale page 의 block 본문이 페이로드에 있다
  stale_leak                      stale 본문이 있는데 그 page 에 "대체됨" 표시가 없다
  bytes_per_gold_hit              Σbytes / Σ(전달된 gold block 수)
  latency_ms                      검색+투영+렌더 전체 (production 은 정본 스캔 포함)
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "bench"))
sys.path.insert(0, str(ROOT))

from rankers.base import load_pages  # noqa: E402
from rankers.structural2 import Structural2Ranker  # noqa: E402
from context.arms import (  # noqa: E402
    CtxIndex, Payload, ProductionArm, V2AddressArm, V2GraphArm, V2GraphJsonArm, V2TextArm,
    stage2_bytes,
)
from scripts import llmwiki_context as C  # noqa: E402

TYPES = ("exact", "relation", "temporal", "crosslingual", "paraphrase")
ARMS = ("production", "v2-text", "v2-graph", "v2-graph-json", "v2-address")
BODY_KEY = 60           # block 본문 앞 60자(공백 제거)로 "본문이 실렸다"를 판정
PROMPTS_PER_DAY = 100
PRICE_PER_MTOK = {"claude-opus-5": 5.0, "claude-sonnet-5": 2.0}   # 입력 $/M, claude-api skill 표 (2026-06-24)


def squash(text: str) -> str:
    return "".join(str(text or "").split())


def pct(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    s = sorted(values)
    return round(s[min(len(s) - 1, int(round(p / 100.0 * (len(s) - 1))))], 3)


# --------------------------------------------------------------------------- oracle
class Oracle:
    """정답 판정용 정본 뷰. arm 은 이걸 보지 못한다."""

    def __init__(self, corpus_dir: Path):
        self.pages: dict[str, dict[str, Any]] = {}
        self.block_key: dict[str, str] = {}
        self.page_blocks: dict[str, list[str]] = {}
        for p in load_pages(corpus_dir):
            self.pages[p["id"]] = p
            for bid in p.get("block_order") or list(p["blocks"]):
                b = p["blocks"].get(bid)
                if isinstance(b, dict):
                    self.block_key[bid] = squash(C.block_text(b))[:BODY_KEY]
                    self.page_blocks.setdefault(p["id"], []).append(bid)


def judge(payload: Payload, q: dict[str, Any], oracle: Oracle) -> dict[str, Any]:
    text = payload.text
    flat = squash(text)
    gold_pages = list(q.get("gold_pages") or [])
    gold_blocks = list(q.get("gold_blocks") or [])
    stale_pages = list(q.get("stale_pages") or [])
    man_pages = {e.page_id for e in payload.manifest}
    man_body = {e.block_id for e in payload.manifest if e.block_id and e.body}
    man_addr = {e.block_id for e in payload.manifest if e.block_id}
    marked = {e.page_id for e in payload.manifest if e.status == "superseded"}

    hits = 0
    mismatch = 0
    for bid in gold_blocks:
        key = oracle.block_key.get(bid, "")
        in_text = bool(key) and key in flat
        in_man = bid in man_body
        if in_text != in_man:
            mismatch += 1
        if in_text and in_man:
            hits += 1
    stale_body = 0
    leak = 0
    for pid in stale_pages:
        for bid in oracle.page_blocks.get(pid, []):
            key = oracle.block_key.get(bid, "")
            if key and key in flat:
                stale_body = 1
                if pid not in marked:
                    leak = 1
    nbytes = len(text.encode("utf-8"))
    return {
        "payload_bytes": nbytes,
        "est_tokens": C.est_tokens(text) if text else 0,
        "injected": 1 if text else 0,
        "gold_page_in_payload": 1 if any(p in man_pages for p in gold_pages) else 0,
        "gold_block_in_payload": 1 if gold_blocks and hits == len(gold_blocks) else 0,
        "gold_addr_in_payload": 1 if gold_blocks and all(b in man_addr for b in gold_blocks) else 0,
        "gold_hits": hits,
        "manifest_mismatch": mismatch,
        "stale_body_in_payload": stale_body if stale_pages else -1,
        "stale_leak": leak if stale_pages else -1,
        "blocks_in_payload": len(man_body),
        "pages_in_payload": len(man_pages),
        "reason": payload.reason,
    }


# --------------------------------------------------------------------------- run
def run_arm(name: str, arm: Any, queries: list[dict[str, Any]], budgets: list[int],
            oracle: Oracle) -> dict[int, list[dict[str, Any]]]:
    rows: dict[int, list[dict[str, Any]]] = {b: [] for b in budgets}
    for i, q in enumerate(queries):
        if name == "production":
            result, pages, text6000, ms = arm.prepare(q["text"])
            for b in budgets:
                t0 = time.perf_counter()
                payload = arm.render(result, pages, b, text6000)
                ms_b = ms + (time.perf_counter() - t0) * 1000
                row = judge(payload, q, oracle)
                row.update({"id": q["id"], "type": q["type"], "latency_ms": round(ms_b, 3)})
                rows[b].append(row)
        else:
            for b in budgets:
                t0 = time.perf_counter()
                payload = arm.run(q["text"], b)
                ms_b = (time.perf_counter() - t0) * 1000
                row = judge(payload, q, oracle)
                row.update({"id": q["id"], "type": q["type"], "latency_ms": round(ms_b, 3)})
                if name == "v2-address":
                    # 2단계: 주소가 맞았다고 치고 gold block 만 llmwiki_get 으로 집는 비용
                    s2 = 0
                    for bid in q.get("gold_blocks") or []:
                        page = oracle.pages.get(next((p for p in q["gold_pages"]
                                                      if bid in oracle.page_blocks.get(p, [])), ""))
                        if page:
                            s2 += stage2_bytes(page, [bid])
                    row["stage2_bytes"] = s2
                rows[b].append(row)
        if (i + 1) % 50 == 0:
            print(f"[ctx] {name}: {i + 1}/{len(queries)}", file=sys.stderr)
    return rows


def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {}
    out: dict[str, Any] = {"n": len(rows)}
    for key in ("injected", "gold_page_in_payload", "gold_block_in_payload", "gold_addr_in_payload"):
        out[key] = round(statistics.fmean(r[key] for r in rows), 4)
    for key in ("stale_body_in_payload", "stale_leak"):
        vals = [r[key] for r in rows if r[key] >= 0]
        out[key] = round(statistics.fmean(vals), 4) if vals else None
        out[key + "__n"] = len(vals)
    out["manifest_mismatch"] = sum(r["manifest_mismatch"] for r in rows)
    total_bytes = sum(r["payload_bytes"] for r in rows)
    total_hits = sum(r["gold_hits"] for r in rows)
    out["payload_bytes_mean"] = round(total_bytes / len(rows), 1)
    out["payload_bytes_p50"] = pct([r["payload_bytes"] for r in rows], 50)
    out["est_tokens_mean"] = round(statistics.fmean(r["est_tokens"] for r in rows), 1)
    out["bytes_per_gold_hit"] = round(total_bytes / total_hits, 1) if total_hits else None
    per = [r["payload_bytes"] / r["gold_hits"] for r in rows if r["gold_hits"]]
    out["bytes_per_gold_hit_when_hit"] = round(statistics.fmean(per), 1) if per else None
    out["blocks_in_payload_mean"] = round(statistics.fmean(r["blocks_in_payload"] for r in rows), 2)
    out["pages_in_payload_mean"] = round(statistics.fmean(r["pages_in_payload"] for r in rows), 2)
    lat = [r["latency_ms"] for r in rows]
    out["latency_ms"] = {"p50": pct(lat, 50), "p95": pct(lat, 95), "mean": round(statistics.fmean(lat), 3)}
    if any("stage2_bytes" in r for r in rows):
        out["stage2_bytes_mean"] = round(statistics.fmean(r.get("stage2_bytes", 0) for r in rows), 1)
        out["two_stage_bytes_mean"] = round(out["payload_bytes_mean"] + out["stage2_bytes_mean"], 1)
    tokens_day = out["est_tokens_mean"] * PROMPTS_PER_DAY
    out["tokens_per_day_100_prompts"] = round(tokens_day)
    out["usd_per_month_100_prompts"] = {m: round(tokens_day * 30 / 1e6 * p, 2) for m, p in PRICE_PER_MTOK.items()}
    return out


def table(summary: list[dict[str, Any]]) -> str:
    cols = [("gold_block_in_payload", "gold_blk"), ("gold_page_in_payload", "gold_pg"),
            ("gold_addr_in_payload", "gold_addr"), ("stale_leak", "stale_leak"),
            ("payload_bytes_mean", "bytes"), ("est_tokens_mean", "tok"),
            ("bytes_per_gold_hit", "B/gold")]
    lines = [f"{'arm':<16}{'budget':>7}" + "".join(f"{h:>11}" for _k, h in cols) + f"{'p50ms':>9}{'p95ms':>9}"]
    for s in summary:
        o = s["overall"]
        row = f"{s['arm']:<16}{s['budget']:>7}"
        for k, _h in cols:
            v = o.get(k)
            row += f"{'-':>11}" if v is None else (f"{v:>11.3f}" if isinstance(v, float) and v < 10 else f"{v:>11}")
        row += f"{o['latency_ms']['p50']:>9}{o['latency_ms']['p95']:>9}"
        lines.append(row)
    return "\n".join(lines)


def merge(out_dir: Path) -> int:
    summary: list[dict[str, Any]] = []
    for path in sorted(out_dir.glob("*q-*.json")):
        if path.name.endswith("-summary.json"):
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        data.pop("per_query", None)
        summary.append(data)
    order = {a: i for i, a in enumerate(ARMS)}
    summary.sort(key=lambda s: (order.get(s["arm"].split("[")[0], 99), s["arm"], s["budget"]))
    tag = f"{summary[0]['queries']}q" if summary else "0q"
    (out_dir / f"{tag}-summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                                                  encoding="utf-8")
    print(table(summary))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default="bench/frozen/corpus")
    ap.add_argument("--queries", default="bench/frozen/queries.json")
    ap.add_argument("--arms", default=",".join(ARMS))
    ap.add_argument("--budgets", default="2000,4000,6000")
    ap.add_argument("--index-root", default="bench/index_ctx")
    ap.add_argument("--out", default="bench/results_ctx")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--per-type", type=int, default=0)
    ap.add_argument("--no-rebuild", action="store_true")
    ap.add_argument("--merge", action="store_true",
                    help="실행하지 않고 --out 의 per-arm 결과를 모아 summary 와 표를 다시 만든다")
    args = ap.parse_args()
    if args.merge:
        return merge(Path(args.out))

    corpus = Path(args.corpus).resolve()
    index_root = Path(args.index_root).resolve()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = json.loads(Path(args.queries).read_text(encoding="utf-8"))
    queries = payload["queries"] if isinstance(payload, dict) else payload
    if args.per_type:
        seen: dict[str, int] = {}
        picked = []
        for q in queries:
            if seen.get(q["type"], 0) < args.per_type:
                picked.append(q)
                seen[q["type"]] = seen.get(q["type"], 0) + 1
        queries = picked
    if args.limit:
        queries = queries[:args.limit]
    budgets = [int(b) for b in args.budgets.split(",") if b.strip()]
    arms = [a.strip() for a in args.arms.split(",") if a.strip()]

    build_notes: dict[str, Any] = {}
    s2_dir = index_root / "structural2"
    if not args.no_rebuild or not (s2_dir / "structural2.db").exists():
        stats = Structural2Ranker.build(corpus, s2_dir)
        build_notes["structural2"] = {"elapsed_ms": stats.elapsed_ms, "bytes": stats.index_bytes}
    if not args.no_rebuild or not (index_root / "ctx.sqlite").exists():
        build_notes["ctx"] = CtxIndex.build(corpus, index_root)
    print(f"[ctx] index: {build_notes}", file=sys.stderr)

    oracle = Oracle(corpus)
    ranker = Structural2Ranker.load(corpus, s2_dir)
    ctx = CtxIndex.load(index_root)
    prod_root = index_root / "root"
    if not (prod_root / "wiki").is_dir():
        raise SystemExit(f"production 뷰가 없다: {prod_root}/wiki (하드링크 뷰를 먼저 만들어라)")
    def rk(o: dict[str, Any]) -> Structural2Ranker:
        # fold=false 같은 structural2 조회 옵션은 랭커에 넘긴다 (같은 색인, 조회 규칙만 다름)
        ropts = {k: (str(v).lower() == "true") for k, v in o.items() if k == "fold"}
        return Structural2Ranker.load(corpus, s2_dir, **ropts) if ropts else ranker

    impl = {
        "production": lambda **o: ProductionArm(prod_root),
        "v2-text": lambda **o: V2TextArm(rk(o), ctx, **o),
        "v2-graph": lambda **o: V2GraphArm(rk(o), ctx, **o),
        "v2-graph-json": lambda **o: V2GraphJsonArm(rk(o), ctx, **o),
        "v2-address": lambda **o: V2AddressArm(rk(o), ctx, **o),
    }
    summary: list[dict[str, Any]] = []
    tag = f"{len(queries)}q"
    for spec in arms:
        # "v2-graph:cut=0.5;k=5" 처럼 arm 옵션을 인라인으로 받는다 (bench/harness.py 와 같은 꼴)
        name, _, inline = spec.partition(":")
        opts: dict[str, Any] = {}
        for kv in [x for x in inline.split(";") if x]:
            key, _, val = kv.partition("=")
            opts[key] = float(val) if "." in val else int(val) if val.isdigit() else val
        label = name if not opts else name + "[" + ",".join(f"{k}={v}" for k, v in sorted(opts.items())) + "]"
        print(f"[ctx] {label} 실행 중 … (질문 {len(queries)}개, 예산 {budgets})", file=sys.stderr)
        rows = run_arm(name, impl[name](**opts), queries, budgets, oracle)
        name = label
        for b in budgets:
            rs = rows[b]
            entry = {"arm": name, "opts": opts, "budget": b, "queries": len(rs), "corpus_pages": payload.get("corpus_pages"),
                     "overall": aggregate(rs),
                     "by_type": {t: aggregate([r for r in rs if r["type"] == t]) for t in TYPES
                                 if any(r["type"] == t for r in rs)},
                     "build": build_notes}
            summary.append(entry)
            (out_dir / f"{tag}-{name}-{b}.json").write_text(
                json.dumps({**entry, "per_query": rs}, ensure_ascii=False, indent=1), encoding="utf-8")
    (out_dir / f"{tag}-summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                                                  encoding="utf-8")
    print(table(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

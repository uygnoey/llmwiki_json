#!/usr/bin/env python3
"""v3 프로토타입 자연 세트 하네스 — production / v2-graph[cut=0.5] / v3 를 같은 판정으로 잰다.

bench/natural_eval/harness_nat.py 의 Oracle·judge·aggregate 를 가져다 쓰고 다음을 더한다.
  - 정답 span 귀속: span 이 gold block, gold page 의 개정 체인(같은 head 를 가진 page) 의 block, 그 밖의 block 중
    어디서 나왔는지를 나눠 `answer_gold_chain` / `answer_other` 두 값으로 센다 (리뷰 §6-5).
  - 등급 보정: v3 의 등급(강/중/중1/약/무주입) 페이로드를 문항마다 미리 만들고, 절대 신호 하나에 경계 셋
    (무주입<약<중<강) 을 자연 세트 홀수 id 에서 고른 뒤 짝수 id 로 잰다. unrelated 100 평균 바이트 상한을 여러 개
    (CAPS) 두고 상한마다 answerable 정답 전달 최대인 경계를 찾는다 → frontier. 기본 경계는 홀수 id 에서
    v2-graph 의 전달률을 잃지 않는 가장 작은 상한이다.
  - 실패 분류(a/b/c/d/e + g): 6000 B 에서 arm 마다 문항당 하나. g = 등급 때문에 본문이 보류된 것.

사용:
  python3 bench/context/harness_v3.py --index-root bench/index_v3/nat --out bench/results_v3
새 파일은 --index-root 와 --out 아래에만 쓴다. 정본·bench/natural·기존 arm/harness 는 읽기만 한다.
"""
from __future__ import annotations

import argparse
import bisect
import json
import statistics
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "bench"))
sys.path.insert(0, str(ROOT))

from context import arms as A  # noqa: E402
from context import arms_v3 as V3  # noqa: E402
from natural_eval import harness_nat as H  # noqa: E402
from rankers import structural2 as S2  # noqa: E402
from rankers import structural3 as S3  # noqa: E402
from scripts import llmwiki_context as C  # noqa: E402

BUDGETS = (2000, 4000, 6000)
MAIN_BUDGET = 6000
TYPES = ("exact", "relation", "temporal", "crosslingual", "paraphrase", "long")
CAPS = (300.0, 500.0, 750.0, 1000.0, 1500.0, 2000.0, 2500.0, 3000.0, 1e9)
CANDIDATE_SIGNALS = ("top_block_impact", "top1_best_block", "content_raw_top", "raw_top", "raw_x_cov",
                     "raw_x_idfcov", "impact_x_cov", "impact_x_idfcov", "content_raw_x_cov",
                     "content_raw_x_idfcov", "coverage", "idf_coverage")
LADDERS = {"mid2": ("none", "weak", "mid", "strong"), "mid1": ("none", "weak", "mid1", "strong")}
FIXED_GRADES = ("strong", "mid", "mid1", "weak", "none")
GRID = 120


def norm_ws(v: Any) -> str:
    return H.norm_ws(v)


# --------------------------------------------------------------------------- 판정
def chain_of(ctx: A.CtxIndex) -> dict[str, set[str]]:
    """page → 같은 head 를 가진 page 집합 (개정 체인)."""
    heads = dict(ctx.db.execute("SELECT page_id, head FROM page"))
    by_head: dict[str, set[str]] = defaultdict(set)
    for pid, h in heads.items():
        by_head[h].add(pid)
    return {pid: by_head[h] for pid, h in heads.items()}


def judge3(payload: A.Payload, q: dict[str, Any], oracle: H.Oracle, selected: set[str], latency_ms: float,
           chain: dict[str, set[str]], *, clip_chars: int | None = H.PREFIX_CHARS,
           grade: str = "", signals: dict[str, float] | None = None) -> dict[str, Any]:
    row = H.judge(payload, q, oracle, selected, latency_ms, clip_chars=clip_chars)
    expected = bool(q.get("expects_injection"))
    span = norm_ws(q.get("answer_span"))
    gold_blocks = set(q.get("gold_blocks") or [])
    gold_pages = set(q.get("gold_pages") or [])
    chain_pages: set[str] = set()
    for gp in gold_pages:
        chain_pages |= chain.get(gp, {gp})
    source = ""
    gold_body_span_missing = None
    if expected and span:
        found_gold = found_chain = found_other = False
        gold_in_body = False
        for e in payload.manifest:
            if not (e.body and e.block_id):
                continue
            text = getattr(e, "text", "") or ""
            if not text:
                text = C.clip(oracle.blocks.get(e.block_id, ""), clip_chars or 10**9)
            hit = span in norm_ws(text)
            if e.block_id in gold_blocks:
                gold_in_body = True
                found_gold |= hit
            elif e.page_id in chain_pages:
                found_chain |= hit
            else:
                found_other |= hit
        if found_gold:
            source = "gold"
        elif found_chain:
            source = "chain"
        elif found_other or row["answer_delivered"]:
            source = "other"
        gold_body_span_missing = int(gold_in_body and not found_gold)
    row["answer_source"] = source
    row["answer_gold_chain"] = int(source in ("gold", "chain")) if expected else None
    row["answer_other"] = int(source == "other") if expected else None
    row["gold_body_span_missing"] = gold_body_span_missing if expected else None
    row["grade"] = grade
    if signals is not None:
        row["signals"] = {k: round(float(v), 4) for k, v in signals.items()}
    return row


def aggregate3(rows: list[dict[str, Any]]) -> dict[str, Any]:
    agg = H.aggregate(rows)
    ans = [r for r in rows if r["expects_injection"]]
    unrel = [r for r in rows if not r["expects_injection"]]
    agg["answer_gold_chain"] = H.rate([r["answer_gold_chain"] for r in ans])
    agg["answer_other"] = H.rate([r["answer_other"] for r in ans])
    agg["gold_body_span_missing"] = H.rate([r["gold_body_span_missing"] for r in ans])
    agg["bytes_answerable_mean"] = round(statistics.fmean(r["payload_bytes"] for r in ans), 1) if ans else None
    agg["bytes_unrelated_mean"] = round(statistics.fmean(r["payload_bytes"] for r in unrel), 1) if unrel else None
    hard = [r for r in unrel if r.get("hardness") == "hard"]
    off = [r for r in unrel if r.get("hardness") != "hard"]
    agg["bytes_unrelated_hard_mean"] = round(statistics.fmean(r["payload_bytes"] for r in hard), 1) if hard else None
    agg["bytes_unrelated_off_mean"] = round(statistics.fmean(r["payload_bytes"] for r in off), 1) if off else None
    agg["injected_unrelated_hard"] = H.rate([r["injected"] for r in hard])
    agg["injected_unrelated_off"] = H.rate([r["injected"] for r in off])
    agg["grades_answerable"] = dict(Counter(r.get("grade", "") for r in ans))
    agg["grades_unrelated"] = dict(Counter(r.get("grade", "") for r in unrel))
    agg["by_type"] = {}
    for t in TYPES:
        sub = [r for r in ans if r["type"] == t]
        if sub:
            agg["by_type"][t] = {"n": len(sub),
                                 "answer_delivered": H.rate([r["answer_delivered"] for r in sub]),
                                 "answer_gold_chain": H.rate([r["answer_gold_chain"] for r in sub]),
                                 "selected": H.rate([r["selected"] for r in sub]),
                                 "injected": H.rate([r["injected"] for r in sub]),
                                 "bytes": round(statistics.fmean(r["payload_bytes"] for r in sub), 1)}
    return agg


# --------------------------------------------------------------------------- arm 실행
def run_production(arm: H.ExactProductionArm, q, oracle, chain) -> dict[int, dict[str, Any]]:
    result, pages, text6000, prep_ms = arm.prepare(q["text"])
    selected = H.selected_from_pages(pages)
    out = {}
    for budget in BUDGETS:
        t0 = time.perf_counter()
        payload = arm.render(result, pages, budget, text6000)
        ms = prep_ms + (time.perf_counter() - t0) * 1000
        out[budget] = judge3(payload, q, oracle, selected, ms, chain)
    return out


def run_v2(arm: A.V2GraphArm, q, oracle, chain) -> dict[int, dict[str, Any]]:
    out = {}
    for budget in BUDGETS:
        t0 = time.perf_counter()
        items, _r = arm.lines(q["text"])
        selected = H.selected_from_graph_items(items)
        payload = arm.run(q["text"], budget)
        ms = (time.perf_counter() - t0) * 1000
        out[budget] = judge3(payload, q, oracle, selected, ms, chain)
    return out


def run_v3(arm: V3.V3Arm, q, oracle, chain, grade: str | None = None) -> dict[int, dict[str, Any]]:
    out = {}
    for budget in BUDGETS:
        t0 = time.perf_counter()
        items, _r = arm.lines(q["text"], grade)
        selected = H.selected_from_graph_items(items)
        payload = arm.run(q["text"], budget, grade)
        ms = (time.perf_counter() - t0) * 1000
        out[budget] = judge3(payload, q, oracle, selected, ms, chain, grade=arm.last_grade,
                             signals=arm.last_signals)
    return out


# --------------------------------------------------------------------------- 등급 보정
def parity(qid: str) -> int:
    return int("".join(ch for ch in qid if ch.isdigit()) or 0) % 2


class Ladder:
    """한 신호로 정렬한 문항에서 (t_none, t_weak, t_strong) 조합을 prefix-sum 으로 O(1) 평가한다."""

    def __init__(self, rows: list[dict[str, Any]], signal: str, grades: tuple[str, ...], budget: int):
        self.signal, self.grades, self.budget = signal, grades, budget
        rows = sorted(rows, key=lambda r: (r["sig"].get(signal, 0.0), r["id"]))
        self.vals = [r["sig"].get(signal, 0.0) for r in rows]
        n = len(rows)
        # 등급별 prefix sums: [ans_n, ans_deliv, ans_chain, ans_bytes, unrel_n, unrel_bytes]
        self.ps: dict[str, list[list[float]]] = {}
        for g in grades:
            acc = [0.0] * 6
            ps = [list(acc)]
            for r in rows:
                jr = r["grade_rows"][g][budget]
                if r["expects_injection"]:
                    acc[0] += 1
                    acc[1] += jr["answer_delivered"]
                    acc[2] += jr["answer_gold_chain"]
                    acc[3] += jr["payload_bytes"]
                else:
                    acc[4] += 1
                    acc[5] += jr["payload_bytes"]
                ps.append(list(acc))
            self.ps[g] = ps
        self.n = n
        uniq = sorted(set(self.vals))
        if len(uniq) > GRID:
            uniq = [uniq[int(i * (len(uniq) - 1) / (GRID - 1))] for i in range(GRID)]
        mx = uniq[-1] if uniq else 0.0
        self.cands = [0.0] + uniq + [mx + max(1.0, abs(mx))]
        self.idx = [bisect.bisect_left(self.vals, v) for v in self.cands]   # 경계 값 → 첫 index

    def eval_idx(self, i_none: int, i_weak: int, i_strong: int) -> dict[str, float]:
        cuts = [0, i_none, i_weak, i_strong, self.n]           # none: [0,i_none) weak: [i_none,i_weak) ...
        tot = [0.0] * 6
        for g, lo, hi in zip(self.grades, cuts[:-1], cuts[1:]):
            if hi <= lo:
                continue
            a, b = self.ps[g][lo], self.ps[g][hi]
            for k in range(6):
                tot[k] += b[k] - a[k]
        an, un = tot[0], tot[4]
        return {"answer_delivered": tot[1] / an if an else 0.0, "answer_gold_chain": tot[2] / an if an else 0.0,
                "bytes_answerable": tot[3] / an if an else 0.0, "bytes_unrelated": tot[5] / un if un else 0.0}

    def best_for_caps(self, caps: tuple[float, ...]) -> dict[float, dict[str, Any] | None]:
        """모든 (t_none ≤ t_weak ≤ t_strong) 조합을 한 번 훑어 상한마다 최선을 남긴다."""
        best: dict[float, tuple | None] = {c: None for c in caps}
        C_ = self.cands
        I = self.idx
        m = len(C_)
        for a in range(m):
            for b in range(a, m):
                for c in range(b, m):
                    e = self.eval_idx(I[a], I[b], I[c])
                    bu = e["bytes_unrelated"]
                    key = (round(e["answer_delivered"], 6), round(e["answer_gold_chain"], 6), -bu, -e["bytes_answerable"])
                    for cap in caps:
                        if bu <= cap:
                            cur = best[cap]
                            if cur is None or key > cur[0]:
                                best[cap] = (key, a, b, c, e)
        out: dict[float, dict[str, Any] | None] = {}
        for cap, v in best.items():
            out[cap] = None if v is None else {"t_none": C_[v[1]], "t_weak": C_[v[2]], "t_strong": C_[v[3]], **v[4]}
        return out


def evaluate_boundaries(rows, signal, grades, th: dict[str, float], budget: int) -> dict[str, Any]:
    tot = defaultdict(float)
    cnt = Counter()
    for r in rows:
        v = r["sig"].get(signal, 0.0)
        g = grades[3] if v >= th["t_strong"] else grades[2] if v >= th["t_weak"] else grades[1] if v >= th["t_none"] else grades[0]
        jr = r["grade_rows"][g][budget]
        key = "ans" if r["expects_injection"] else "unrel"
        cnt[key] += 1
        cnt[f"{key}_{g}"] += 1
        tot[f"{key}_bytes"] += jr["payload_bytes"]
        if key == "ans":
            tot["deliv"] += jr["answer_delivered"]
            tot["chain"] += jr["answer_gold_chain"]
        else:
            tot["unrel_inj"] += jr["injected"]
    an, un = cnt["ans"], cnt["unrel"]
    return {"signal": signal, "ladder": grades, **th,
            "answer_delivered": round(tot["deliv"] / an, 4) if an else None,
            "answer_gold_chain": round(tot["chain"] / an, 4) if an else None,
            "bytes_answerable": round(tot["ans_bytes"] / an, 1) if an else None,
            "bytes_unrelated": round(tot["unrel_bytes"] / un, 1) if un else None,
            "injected_unrelated": round(tot["unrel_inj"] / un, 3) if un else None,
            "n_answerable": an, "n_unrelated": un,
            "grades": {k: v for k, v in cnt.items() if "_" in k}}


def calibrate(rows: list[dict[str, Any]], budget: int, v2_odd_answer: float) -> dict[str, Any]:
    odd = [r for r in rows if parity(r["id"]) == 1]
    even = [r for r in rows if parity(r["id"]) == 0]
    ladders_odd = {(s, name): Ladder(odd, s, g, budget) for s in CANDIDATE_SIGNALS for name, g in LADDERS.items()}
    best_odd = {key: lad.best_for_caps(CAPS) for key, lad in ladders_odd.items()}
    frontier = []
    for cap in CAPS:
        best = None
        for (s, name), lad in ladders_odd.items():
            b = best_odd[(s, name)][cap]
            if not b:
                continue
            key = (round(b["answer_delivered"], 6), round(b["answer_gold_chain"], 6), -b["bytes_unrelated"])
            if best is None or key > best[0]:
                best = (key, s, name, b)
        if not best:
            continue
        _k, s, name, b = best
        th = {"t_none": b["t_none"], "t_weak": b["t_weak"], "t_strong": b["t_strong"]}
        frontier.append({"cap": cap, "signal": s, "ladder": name,
                         "train_odd": evaluate_boundaries(odd, s, LADDERS[name], th, budget),
                         "test_even": evaluate_boundaries(even, s, LADDERS[name], th, budget),
                         "full": evaluate_boundaries(rows, s, LADDERS[name], th, budget)})
    # 기본 경계: 홀수 id 에서 v2-graph 의 전달률을 잃지 않는 가장 작은 상한
    chosen = next((f for f in frontier if (f["train_odd"]["answer_delivered"] or 0) >= v2_odd_answer), frontier[-1])
    # 같은 상한에서 신호별 최선 (홀수 → 짝수)
    per_signal = []
    for (s, name), lad in ladders_odd.items():
        b = best_odd[(s, name)][chosen["cap"]]
        if not b:
            continue
        th = {"t_none": b["t_none"], "t_weak": b["t_weak"], "t_strong": b["t_strong"]}
        per_signal.append({"signal": s, "ladder": name,
                           "train_odd": evaluate_boundaries(odd, s, LADDERS[name], th, budget),
                           "test_even": evaluate_boundaries(even, s, LADDERS[name], th, budget),
                           "full": evaluate_boundaries(rows, s, LADDERS[name], th, budget)})
    per_signal.sort(key=lambda x: (-(x["train_odd"]["answer_delivered"] or 0), -(x["train_odd"]["answer_gold_chain"] or 0)))
    # 반대 방향: 짝수로 고르고 홀수로 잰다 (고른 신호·사다리·상한 고정)
    lad_even = Ladder(even, chosen["signal"], LADDERS[chosen["ladder"]], budget)
    be = lad_even.best_for_caps((chosen["cap"],))[chosen["cap"]]
    reverse = None
    if be:
        th = {"t_none": be["t_none"], "t_weak": be["t_weak"], "t_strong": be["t_strong"]}
        reverse = {"train_even": evaluate_boundaries(even, chosen["signal"], LADDERS[chosen["ladder"]], th, budget),
                   "test_odd": evaluate_boundaries(odd, chosen["signal"], LADDERS[chosen["ladder"]], th, budget)}
    return {"budget": budget, "caps": list(CAPS), "v2_odd_answer": v2_odd_answer,
            "n_odd": len(odd), "n_even": len(even),
            "chosen": {"cap": chosen["cap"], "signal": chosen["signal"], "ladder": chosen["ladder"],
                       "t_none": chosen["train_odd"]["t_none"], "t_weak": chosen["train_odd"]["t_weak"],
                       "t_strong": chosen["train_odd"]["t_strong"]},
            "frontier": frontier, "per_signal_at_chosen_cap": per_signal, "reverse_direction": reverse}


# --------------------------------------------------------------------------- 실패 분류
def classify_graph(q, oracle, ranker, arm, budget: int, grade: str | None = None) -> str:
    gold_page, gold_block = q["gold_pages"][0], q["gold_blocks"][0]
    span = norm_ws(q["answer_span"])
    full = ranker.search(q["text"], k=400)
    ids = [h.page_id for h in full]
    rank = ids.index(gold_page) + 1 if gold_page in ids else None
    hits = arm.fetch(q["text"], A.PAGES_GRAPH)[0]
    in_hits = any(h.page_id == gold_page for h in hits)
    is_v3 = isinstance(arm, V3.V3Arm)
    items, _ = arm.lines(q["text"], grade) if is_v3 else arm.lines(q["text"])
    pay = arm.run(q["text"], budget, grade) if is_v3 else arm.run(q["text"], budget)
    sel = {e.block_id for _l, e, _o in items if e.body and e.block_id}
    flat = norm_ws(pay.text)
    body = {e.block_id for e in pay.manifest if e.body}
    if span and span in flat:
        return "e" if gold_block in body else "e-other"
    if not in_hits:
        return "a-cut" if rank is not None and rank <= A.PAGES_GRAPH else "a-rank"
    if is_v3 and arm.last_grade in ("weak", "none"):
        return "g"
    addr = {e.block_id for _l, e, _o in items if e.block_id and e.status == "address"}
    if gold_block in addr and gold_block not in sel:
        return "g"
    if gold_block not in sel:
        return "b"
    if gold_block not in body:
        return "c"
    return "d"


def classify_production(q, root: Path, budget: int) -> str:
    gold_page, gold_block = q["gold_pages"][0], q["gold_blocks"][0]
    span = norm_ws(q["answer_span"])
    raw = C.retrieve(root, q["text"], min_score=0.0, min_coverage=0.0,
                     min_matched=0, hint_score=0.0, limit=10**6)
    raw_ids = [h.doc.page_id for h in raw.hits]
    p_rank = raw_ids.index(gold_page) + 1 if gold_page in raw_ids else None
    res = C.retrieve(root, q["text"])
    hint = res.reason.startswith("hint")
    if hint:
        pages = [C.project_hit(h, res.tokens, res.idf, max_blocks=0) for h in res.hits]
        text = C.render_hint(res, pages, max_bytes=budget, max_tokens=C.MAX_TOKENS)
    else:
        pages = [C.project_hit(h, res.tokens, res.idf, max_blocks=C.MAX_BLOCKS) for h in res.hits]
        text = C.render(res, pages, max_bytes=budget, max_tokens=C.MAX_TOKENS)
    man = H.production_manifest(text, pages, res.reason)
    sel = H.selected_from_pages(pages)
    flat = norm_ws(text)
    in_hits = any(h.doc.page_id == gold_page for h in res.hits)
    body = {e.block_id for e in man.manifest if e.body}
    if span and span in flat:
        return "e" if gold_block in body else "e-other"
    if not res.hits or not in_hits:
        if res.reason == "below-threshold":
            return "a-gate" if (p_rank or 99) <= C.MAX_PAGES else "a-rank"
        if hint:
            return "a-hint" if in_hits or (p_rank or 99) <= C.MAX_PAGES else "a-rank"
        return "a-rank"
    if hint:
        return "a-hint"
    if gold_block not in sel:
        return "b"
    if gold_block not in body:
        return "c"
    return "d"


# --------------------------------------------------------------------------- main
def fmt(v: Any, d: int = 3) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.{d}f}"
    return str(v)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="bench/natural/root")
    ap.add_argument("--queries", default="bench/natural/queries.json")
    ap.add_argument("--index-root", default="bench/index_v3/nat")
    ap.add_argument("--out", default="bench/results_v3")
    ap.add_argument("--no-rebuild", action="store_true")
    ap.add_argument("--h6", action="store_true",
                    help="heading 경로를 block 본문 앞에 붙여 색인한다 (기본 꺼짐 — 제품과 같다)")
    args = ap.parse_args()

    root = Path(args.root).resolve()
    corpus = root / "wiki"
    index_root = Path(args.index_root).resolve()
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    d = json.loads(Path(args.queries).read_text(encoding="utf-8"))
    queries = d["queries"]
    oracle = H.Oracle(corpus)
    H.validate_contract(queries, oracle)
    hardness = {q["id"]: q.get("hardness") for q in queries}

    build: dict[str, Any] = {}
    s2_dir, s3_dir = index_root / "structural2", index_root / "structural3"
    if not args.no_rebuild or not (s2_dir / "structural2.db").exists():
        st = S2.Structural2Ranker.build(corpus, s2_dir)
        build["structural2"] = {"elapsed_ms": st.elapsed_ms, "index_bytes": st.index_bytes, "notes": st.notes}
    if not args.no_rebuild or not (s3_dir / "structural3.db").exists():
        st = S3.Structural3Ranker.build(corpus, s3_dir, heading_paths=args.h6)
        build["structural3"] = {"elapsed_ms": st.elapsed_ms, "index_bytes": st.index_bytes, "notes": st.notes}
    if not args.no_rebuild or not (index_root / "ctx.sqlite").exists():
        build["ctx"] = A.CtxIndex.build(corpus, index_root)
    r2 = S2.Structural2Ranker.load(corpus, s2_dir)
    r3 = S3.Structural3Ranker.load(corpus, s3_dir)
    ctx = A.CtxIndex.load(index_root)
    chain = chain_of(ctx)

    prod = H.ExactProductionArm(root)
    v2 = A.V2GraphArm(r2, ctx, cut=0.5)
    v3_fixed = V3.V3Arm(r3, ctx)

    # ---- 1) production / v2 / v3 등급 고정 — 문항별 행
    per: dict[str, dict[str, dict[int, dict[str, Any]]]] = {"production": {}, "v2-graph[cut=0.5]": {}}
    for g in FIXED_GRADES:
        per[f"v3[{g}]"] = {}
    cal_rows: list[dict[str, Any]] = []
    for i, q in enumerate(queries, 1):
        per["production"][q["id"]] = run_production(prod, q, oracle, chain)
        per["v2-graph[cut=0.5]"][q["id"]] = run_v2(v2, q, oracle, chain)
        grade_rows = {}
        for g in FIXED_GRADES:
            grade_rows[g] = run_v3(v3_fixed, q, oracle, chain, grade=g)
            per[f"v3[{g}]"][q["id"]] = grade_rows[g]
        cal_rows.append({"id": q["id"], "type": q["type"], "expects_injection": bool(q["expects_injection"]),
                         "sig": dict(v3_fixed.last_signals), "grade_rows": grade_rows})
        if i % 20 == 0:
            print(f"[v3] {i}/{len(queries)}", file=sys.stderr)

    # ---- 2) 등급 보정
    v2_odd = [per["v2-graph[cut=0.5]"][q["id"]][MAIN_BUDGET]["answer_delivered"]
              for q in queries if q["expects_injection"] and parity(q["id"]) == 1]
    v2_odd_answer = statistics.fmean(v2_odd)
    t0 = time.perf_counter()
    cal = calibrate(cal_rows, MAIN_BUDGET, v2_odd_answer)
    cal["elapsed_s"] = round(time.perf_counter() - t0, 1)
    ch = cal["chosen"]
    print(f"[v3] 등급 경계: {ch} ({cal['elapsed_s']} s)", file=sys.stderr)
    (out / "calibration.json").write_text(json.dumps(cal, ensure_ascii=False, indent=1), encoding="utf-8")

    # ---- 3) 보정된 v3 (기본) — 등급은 신호로 정하고 행은 등급별 행에서 고른다 (같은 결정론)
    grades = LADDERS[ch["ladder"]]
    v3 = V3.V3Arm(r3, ctx, signal=ch["signal"], t_none=ch["t_none"], t_weak=ch["t_weak"], t_strong=ch["t_strong"])
    per["v3"] = {}
    for r in cal_rows:
        g = v3.grade_of(r["sig"])
        g = {"strong": grades[3], "mid": grades[2], "weak": grades[1], "none": grades[0]}[g]
        per["v3"][r["id"]] = {b: {**r["grade_rows"][g][b], "grade": g} for b in BUDGETS}
    # frontier 의 다른 상한들도 arm 으로 남긴다 (500 B 상한 포함)
    for f in cal["frontier"]:
        if f["cap"] == ch["cap"] or f["cap"] >= 1e9:
            continue
        label = f"v3[cap={int(f['cap'])}]"
        th = f["train_odd"]
        fg = LADDERS[f["ladder"]]
        per[label] = {}
        for r in cal_rows:
            v = r["sig"].get(f["signal"], 0.0)
            g = fg[3] if v >= th["t_strong"] else fg[2] if v >= th["t_weak"] else fg[1] if v >= th["t_none"] else fg[0]
            per[label][r["id"]] = {b: {**r["grade_rows"][g][b], "grade": g} for b in BUDGETS}

    # ---- 4) 요약 + 파일
    summary = []
    for arm_name, rows_by_q in per.items():
        file_label = arm_name.replace("[", "-").replace("]", "").replace("=", "-").replace(".", "_")
        for b in BUDGETS:
            rows = []
            for q in queries:
                row = dict(rows_by_q[q["id"]][b])
                row["hardness"] = hardness[q["id"]]
                rows.append(row)
            entry = {"arm": arm_name, "budget": b, "overall": aggregate3(rows)}
            summary.append(entry)
            (out / f"nat-{file_label}-{b}.json").write_text(
                json.dumps({**entry, "per_query": rows}, ensure_ascii=False, indent=1), encoding="utf-8")

    # ---- 5) 실패 분류 (6000 B)
    v3_mid_pages = 1 if ch["ladder"] == "mid1" else V3.MID_BODY_PAGES
    v3 = V3.V3Arm(r3, ctx, signal=ch["signal"], t_none=ch["t_none"], t_weak=ch["t_weak"], t_strong=ch["t_strong"],
                  mid_pages=v3_mid_pages)
    anatomy: dict[str, dict[str, str]] = {"production": {}, "v2-graph[cut=0.5]": {}, "v3[strong]": {}, "v3": {}}
    ans_q = [q for q in queries if q["expects_injection"]]
    for q in ans_q:
        anatomy["production"][q["id"]] = classify_production(q, root, MAIN_BUDGET)
        anatomy["v2-graph[cut=0.5]"][q["id"]] = classify_graph(q, oracle, r2, v2, MAIN_BUDGET)
        anatomy["v3[strong]"][q["id"]] = classify_graph(q, oracle, r3, v3_fixed, MAIN_BUDGET, grade="strong")
        anatomy["v3"][q["id"]] = classify_graph(q, oracle, r3, v3, MAIN_BUDGET)
    qtype = {q["id"]: q["type"] for q in queries}
    anatomy_tables = {}
    for arm_name, cls_of in anatomy.items():
        t: dict[str, Counter] = defaultdict(Counter)
        for qid, c in cls_of.items():
            t[qtype[qid]][c] += 1
            t["all"][c] += 1
        anatomy_tables[arm_name] = {k: dict(v) for k, v in t.items()}
    (out / "anatomy_v3.json").write_text(json.dumps({"budget": MAIN_BUDGET, "tables": anatomy_tables,
                                                     "per_query": anatomy}, ensure_ascii=False, indent=1),
                                         encoding="utf-8")

    dataset = {"pages": len(oracle.pages), "queries": len(queries),
               "answerable": len(ans_q), "unrelated": len(queries) - len(ans_q)}
    (out / "summary.json").write_text(json.dumps({"dataset": dataset, "build": build, "calibration": ch,
                                                  "summary": summary}, ensure_ascii=False, indent=1),
                                      encoding="utf-8")

    # ---- 6) 표 (markdown)
    L: list[str] = []
    L.append("## 예산·arm별 (자연 284)")
    L.append("")
    L.append("| arm | B | answer | gold/chain | other | selected | truncated(prefix) | gold본문·span없음 | stale leak | unrel 주입 (hard/off) | bytes 전체 | bytes ans | bytes unrel (hard/off) | tokens | p50 ms |")
    L.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for e in summary:
        o = e["overall"]
        L.append(f"| {e['arm']} | {e['budget']} | {fmt(o['answer_delivered'])} | {fmt(o['answer_gold_chain'])} | "
                 f"{fmt(o['answer_other'])} | {fmt(o['selected'])} | {fmt(o['truncated'])} | {fmt(o['gold_body_span_missing'])} | "
                 f"{fmt(o['stale_leak'])} | {fmt(o['injected_unrelated'], 2)} ({fmt(o['injected_unrelated_hard'], 2)}/{fmt(o['injected_unrelated_off'], 2)}) | "
                 f"{fmt(o['payload_bytes_mean'], 0)} | {fmt(o['bytes_answerable_mean'], 0)} | {fmt(o['bytes_unrelated_mean'], 0)} "
                 f"({fmt(o['bytes_unrelated_hard_mean'], 0)}/{fmt(o['bytes_unrelated_off_mean'], 0)}) | {fmt(o['est_tokens_mean'], 0)} | "
                 f"{fmt(o['latency_ms']['p50'], 2)} |")
    L.append("")
    L.append(f"## 유형별 answer / gold-chain ({MAIN_BUDGET} B)")
    L.append("")
    L.append("| arm | " + " | ".join(TYPES) + " | 전체 |")
    L.append("|---|" + "---:|" * (len(TYPES) + 1))
    for e in summary:
        if e["budget"] != MAIN_BUDGET:
            continue
        o = e["overall"]
        cells = [f"{fmt(o['by_type'][t]['answer_delivered'])} / {fmt(o['by_type'][t]['answer_gold_chain'])}" for t in TYPES]
        L.append(f"| {e['arm']} | " + " | ".join(cells) + f" | {fmt(o['answer_delivered'])} / {fmt(o['answer_gold_chain'])} |")
    L.append("")
    L.append(f"## 등급 분포 ({MAIN_BUDGET} B)")
    L.append("")
    for e in summary:
        if e["arm"].startswith("v3") and "cap" in e["arm"] or e["arm"] == "v3":
            if e["budget"] == MAIN_BUDGET:
                L.append(f"- {e['arm']}: answerable {e['overall']['grades_answerable']}  unrelated {e['overall']['grades_unrelated']}")
    L.append("")
    L.append("## 등급 경계 보정 — 상한별 frontier (홀수 id 로 고름 → 짝수 id 로 잼)")
    L.append("")
    L.append(f"v2-graph 홀수 answer {v2_odd_answer:.3f}. 기본 경계 = 홀수 answer 가 이 값 이상인 가장 작은 상한 → **cap {int(ch['cap'])} B, {ch['signal']}, {ch['ladder']}, "
             f"t_none {ch['t_none']:.4g} / t_weak {ch['t_weak']:.4g} / t_strong {ch['t_strong']:.4g}**")
    L.append("")
    L.append("| unrel 상한 | 신호 | 사다리 | t_none | t_weak | t_strong | 홀수 answer | 홀수 unrel B | 짝수 answer | 짝수 unrel B | 전체 answer | 전체 gold/chain | 전체 unrel B (주입률) | 전체 ans B |")
    L.append("|---:|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for f in cal["frontier"]:
        a, t, u = f["train_odd"], f["test_even"], f["full"]
        cap = "∞" if f["cap"] >= 1e9 else str(int(f["cap"]))
        L.append(f"| {cap} | {f['signal']} | {f['ladder']} | {a['t_none']:.4g} | {a['t_weak']:.4g} | {a['t_strong']:.4g} | "
                 f"{fmt(a['answer_delivered'])} | {fmt(a['bytes_unrelated'], 0)} | {fmt(t['answer_delivered'])} | {fmt(t['bytes_unrelated'], 0)} | "
                 f"{fmt(u['answer_delivered'])} | {fmt(u['answer_gold_chain'])} | {fmt(u['bytes_unrelated'], 0)} ({fmt(u['injected_unrelated'], 2)}) | {fmt(u['bytes_answerable'], 0)} |")
    L.append("")
    L.append(f"신호별 최선 (상한 {int(ch['cap'])} B):")
    L.append("")
    L.append("| 신호 | 사다리 | t_none | t_weak | t_strong | 홀수 answer | 홀수 unrel B | 짝수 answer | 짝수 unrel B | 전체 answer | 전체 unrel B |")
    L.append("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for p in cal["per_signal_at_chosen_cap"]:
        a, t, u = p["train_odd"], p["test_even"], p["full"]
        L.append(f"| {p['signal']} | {p['ladder']} | {a['t_none']:.4g} | {a['t_weak']:.4g} | {a['t_strong']:.4g} | {fmt(a['answer_delivered'])} | "
                 f"{fmt(a['bytes_unrelated'], 0)} | {fmt(t['answer_delivered'])} | {fmt(t['bytes_unrelated'], 0)} | {fmt(u['answer_delivered'])} | {fmt(u['bytes_unrelated'], 0)} |")
    L.append("")
    rv = cal.get("reverse_direction")
    if rv:
        a, t = rv["train_even"], rv["test_odd"]
        L.append(f"반대 방향(짝수로 고름 → 홀수로 잼, 같은 신호·사다리·상한): 경계 {a['t_none']:.4g} / {a['t_weak']:.4g} / {a['t_strong']:.4g}, "
                 f"짝수 answer {fmt(a['answer_delivered'])} unrel {fmt(a['bytes_unrelated'], 0)} B → 홀수 answer {fmt(t['answer_delivered'])} unrel {fmt(t['bytes_unrelated'], 0)} B")
        L.append("")
    L.append(f"## 실패 분류 ({MAIN_BUDGET} B, answerable {len(ans_q)})")
    L.append("")
    for arm_name, t in anatomy_tables.items():
        cls = sorted({c for v in t.values() for c in v})
        L.append(f"{arm_name}:")
        L.append("")
        L.append("| 유형 | " + " | ".join(cls) + " | n | 전달률 |")
        L.append("|---|" + "---:|" * (len(cls) + 2))
        for ty in list(TYPES) + ["all"]:
            row = t.get(ty, {})
            n = sum(row.values())
            e = sum(v for k, v in row.items() if k.startswith("e"))
            L.append(f"| {ty} | " + " | ".join(str(row.get(c, 0)) for c in cls) + f" | {n} | {fmt(e / n if n else None)} |")
        L.append("")
    L.append("## build")
    L.append("")
    L.append("```")
    L.append(json.dumps(build, ensure_ascii=False, indent=1))
    L.append("```")
    (out / "tables.md").write_text("\n".join(L), encoding="utf-8")
    print("\n".join(L))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

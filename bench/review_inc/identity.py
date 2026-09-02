#!/usr/bin/env python3
"""과제 H-1: 증분 색인 == 전체 재빌드 동일성 주장을 다시, 더 적대적으로 확인한다.

codex 의 16개 조합(body/add/delete/supersedes × 1/10/100/1000)을 그대로 다시 돌리고,
추가로 체인 head 교체·head 삭제·중간 삭제·df 대량 이동·slug 개명·block 제거·
파일 이동·빈 본문 조합을 넣는다. 비교 대상은 둘이다.

  (1) segmented 증분  vs  segmented 전체 재빌드   (codex 가 비교한 것)
  (2) segmented 증분  vs  structural2 전체 재빌드 (실제 제품 기준 랭커)

그리고 각 조합에서 "base 결과와 달라진 문항 수"를 함께 적는다. 이 수가 0 이면
그 조합의 동일성 검사는 검정력이 없다(무엇을 바꿔도 결과가 같은 문항만 본 셈).

bench/incremental/*.py, bench/rankers/*.py 는 import 만 한다. 색인·임시 root 는
bench/index_review_inc/identity/ 아래, 결과는 bench/results_review_inc/identity.json.
"""
from __future__ import annotations

import copy
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

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
from bench.rankers.structural2 import Structural2Ranker  # noqa: E402

INDEX = ROOT / "bench" / "index_review_inc" / "identity"
RESULTS = ROOT / "bench" / "results_review_inc"
SEG_BASE_SRC = ROOT / "bench" / "index_inc" / "task_f" / "segment_base"   # 읽기만


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def replace_file(overlay: Path, rel: str, page: dict[str, Any]) -> None:
    path = overlay / rel
    if path.is_symlink() or path.exists():
        path.unlink()
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, page)


def set_modified(payload: dict[str, Any], pid: str, page: dict[str, Any]) -> None:
    payload["pages"][pid]["sha256"] = sha_text(canonical(page))


# ---------------------------------------------------------------- 추가 mutation
# 모두 (pages, sources, payload, overlay, count, queries) -> changed ids 를 돌려준다.
# overlay 는 build_case(..., "body", 0, overlay) 로 만든 심링크 사본 위에서 시작한다.

def chain_heads(pages: dict[str, dict[str, Any]]) -> tuple[dict[str, str], dict[str, str]]:
    """supersedes 간선에서 succ(dst→src) 와 head 를 structural2 규칙 그대로 계산한다."""
    lookup: dict[str, str] = {}
    for pid in sorted(pages):
        p = pages[pid]
        for key in (pid, str(p.get("slug") or ""), str(p.get("title") or "")):
            if key:
                lookup.setdefault(key, pid)
    succ: dict[str, str] = {}
    for pid in sorted(pages):
        for link in pages[pid].get("links") or []:
            if link.get("kind") != "supersedes":
                continue
            raw = str(link.get("target") or "")
            dst = lookup.get(raw) or lookup.get(raw[5:] if raw.startswith("page:") else "page:" + raw)
            if dst and dst != pid:
                succ.setdefault(dst, pid)
    head: dict[str, str] = {}
    for start in succ:
        cur, guard = start, 0
        while cur in succ and guard < 32:
            cur, guard = succ[cur], guard + 1
        head[start] = cur
    return succ, head


def temporal_golds(queries: list[dict[str, Any]]) -> list[str]:
    out: list[str] = []
    for q in queries:
        if q["type"] == "temporal":
            for pid in q["gold_pages"]:
                if pid not in out:
                    out.append(pid)
    return out


def mut_chain_extend(pages, sources, payload, overlay, count, queries):
    """temporal gold(체인 head) 를 대체하는 새 page 를 추가한다. head 가 바뀌어야 한다."""
    changed = []
    for idx, head_id in enumerate(temporal_golds(queries)[:count]):
        head = pages[head_id]
        pid = f"page:inc-newhead-{idx:05d}"
        slug = f"inc-newhead-{idx:05d}"
        page = copy.deepcopy(head)
        page["id"], page["slug"] = pid, slug
        page["title"] = f"증분 신판 {idx:05d}"
        # block 은 하나만: head 의 temporal block 을 그대로 복사(어휘 동일) + supersedes anchor
        src_bid = head["block_order"][0]
        for bid in head["block_order"]:
            if bid.endswith(":temporal"):
                src_bid = bid
        block = copy.deepcopy(head["blocks"][src_bid])
        nbid = f"block:{slug}:temporal"
        block["id"] = nbid
        page["blocks"] = {nbid: block}
        page["block_order"] = [nbid]
        page["links"] = [{"target": head["slug"], "kind": "supersedes", "block_id": nbid, "label": head["slug"], "anchor": ""}]
        page["history"] = [{"at": "2026-12-31", "action": "inc-newhead", "actor": "review"}]
        page["created"] = page["updated"] = "2026-12-31"
        rel = f"syntheses/{slug}.json"
        replace_file(overlay, rel, page)
        payload["pages"][pid] = {"source": rel, "pointer": "", "sha256": sha_text(canonical(page))}
        changed.append(pid)
    return changed


def mut_chain_head_delete(pages, sources, payload, overlay, count, queries):
    """temporal gold(체인 head) 를 지운다. 이전 판이 새 head 가 되어야 한다."""
    changed = []
    for head_id in temporal_golds(queries)[:count]:
        (overlay / sources[head_id]).unlink()
        del payload["pages"][head_id]
        changed.append(head_id)
    return changed


def mut_chain_middle_delete(pages, sources, payload, overlay, count, queries):
    """길이 3 이상 체인의 중간 page 를 지운다. 체인이 끊겨 head 귀속이 달라진다."""
    succ, head = chain_heads(pages)
    preds = set(succ.values())
    middles = sorted(pid for pid in succ if pid in preds)
    changed = []
    for pid in middles[:count]:
        (overlay / sources[pid]).unlink()
        del payload["pages"][pid]
        changed.append(pid)
    return changed


def mut_df_shift(pages, sources, payload, overlay, count, queries):
    """count 개 page 의 첫 block 에 질문 200개 본문을 통째로 넣는다. 질문 토큰 df 가 크게 움직인다."""
    blob = " ".join(q["text"] for q in queries[:200])
    changed = []
    for idx, pid in enumerate(sorted(pages)[:count]):
        page = copy.deepcopy(pages[pid])
        bid = page["block_order"][0]
        block = page["blocks"][bid]
        text = str(block.get("source_text") or "") + " " + blob
        block["source_text"] = text
        block["data"] = {**(block.get("data") or {}), "text": text}
        block["fingerprint"] = sha_text(text)
        replace_file(overlay, sources[pid], page)
        set_modified(payload, pid, page)
        changed.append(pid)
    return changed


def mut_slug_rename(pages, sources, payload, overlay, count, queries):
    """들어오는 간선이 많은 page 의 slug 를 바꾼다. 남은 간선은 dangling 이 되어 hub 감쇠와 확산이 달라진다."""
    indeg: dict[str, int] = {}
    for p in pages.values():
        for link in p.get("links") or []:
            indeg[str(link.get("target") or "")] = indeg.get(str(link.get("target") or ""), 0) + 1
    by_slug = {p["slug"]: pid for pid, p in pages.items()}
    targets = [by_slug[s] for s, _n in sorted(indeg.items(), key=lambda kv: (-kv[1], kv[0])) if s in by_slug]
    changed = []
    for idx, pid in enumerate(targets[:count]):
        page = copy.deepcopy(pages[pid])
        page["slug"] = f"inc-renamed-{idx:05d}"
        page["title"] = f"증분 개명 {idx:05d}"
        replace_file(overlay, sources[pid], page)
        set_modified(payload, pid, page)
        changed.append(pid)
    return changed


def mut_block_remove(pages, sources, payload, overlay, count, queries):
    """page 의 첫 block 을 제거하고 나머지 순서를 뒤집는다. block seq 가 어긋난다."""
    changed = []
    for pid in sorted(pages)[:count]:
        page = copy.deepcopy(pages[pid])
        order = list(page["block_order"])
        if len(order) < 2:
            continue
        gone = order.pop(0)
        del page["blocks"][gone]
        page["block_order"] = list(reversed(order))
        page["links"] = [l for l in page.get("links") or [] if l.get("block_id") != gone]
        replace_file(overlay, sources[pid], page)
        set_modified(payload, pid, page)
        changed.append(pid)
    return changed


def mut_move_source(pages, sources, payload, overlay, count, queries):
    """내용은 같고 파일 경로만 옮긴다. sha 동일·source 상이 → modified 로 잡혀야 한다."""
    changed = []
    for idx, pid in enumerate(sorted(pages)[:count]):
        old = overlay / sources[pid]
        rel = f"moved/{idx:05d}.json"
        old.unlink()
        replace_file(overlay, rel, pages[pid])
        payload["pages"][pid]["source"] = rel
        changed.append(pid)
    return changed


def mut_empty_blocks(pages, sources, payload, overlay, count, queries):
    """모든 block 본문을 비운다(제목만 남는다). block 0개 page 처리."""
    changed = []
    for pid in sorted(pages)[:count]:
        page = copy.deepcopy(pages[pid])
        for bid in page["block_order"]:
            page["blocks"][bid]["source_text"] = ""
            page["blocks"][bid]["data"] = {"text": ""}
        replace_file(overlay, sources[pid], page)
        set_modified(payload, pid, page)
        changed.append(pid)
    return changed


def mut_title_collide(pages, sources, payload, overlay, count, queries):
    """page 의 제목을 다른 page(질문 gold) 의 slug 와 같게 한다. lookup 우선순위 충돌."""
    golds = [q["gold_pages"][0] for q in queries if q["type"] == "relation"]
    changed = []
    ids = sorted(pages)
    for idx, gold in enumerate(golds[:count]):
        pid = ids[idx]  # 사전순 앞 page 가 gold 의 slug 를 제목으로 가진다 → lookup setdefault 가 먼저 잡는다
        if pid == gold:
            continue
        page = copy.deepcopy(pages[pid])
        page["title"] = pages[gold]["slug"]
        replace_file(overlay, sources[pid], page)
        set_modified(payload, pid, page)
        changed.append(pid)
    return changed


EXTRA: dict[str, tuple[Callable[..., list[str]], tuple[int, ...]]] = {
    "chain_extend": (mut_chain_extend, (1, 10, 100)),
    "chain_head_delete": (mut_chain_head_delete, (1, 10, 100)),
    "chain_middle_delete": (mut_chain_middle_delete, (1, 10, 100)),
    "df_shift": (mut_df_shift, (1, 10, 100)),
    "slug_rename": (mut_slug_rename, (1, 10, 100)),
    "block_remove": (mut_block_remove, (10, 100)),
    "move_source": (mut_move_source, (10, 100)),
    "empty_blocks": (mut_empty_blocks, (10, 100)),
    "title_collide": (mut_title_collide, (10, 100)),
}


# ---------------------------------------------------------------- 비교
def run_all(ranker, queries) -> list[list[tuple[str, float, tuple[str, ...]]]]:
    return [hit_signature(ranker.search(q["text"], 10)) for q in queries]


def diff(a, b, queries) -> dict[str, Any]:
    bad = [i for i, (x, y) in enumerate(zip(a, b)) if x != y]
    page_only = [i for i in bad if [h[0] for h in a[i]] != [h[0] for h in b[i]]]
    ex = []
    for i in bad[:3]:
        ex.append({"id": queries[i]["id"], "type": queries[i]["type"], "left": a[i][:3], "right": b[i][:3]})
    return {"mismatch": len(bad), "page_order_mismatch": len(page_only), "examples": ex}


def main() -> int:
    queries = load_queries()
    pages, sources = load_page_files(CORPUS)
    base_map = make_map(CORPUS)
    INDEX.mkdir(parents=True, exist_ok=True)

    s2_base_dir = INDEX / "structural2_base"
    t0 = time.perf_counter()
    Structural2Ranker.build(CORPUS, s2_base_dir)
    print(f"structural2 base build {time.perf_counter()-t0:.2f}s", flush=True)
    seg_base_dir = INDEX / "segment_base"
    if not (seg_base_dir / "segments.sqlite").exists():
        shutil.copytree(SEG_BASE_SRC, seg_base_dir)
    base_seg = SegmentedRanker.load(CORPUS, seg_base_dir)
    base_s2 = Structural2Ranker.load(CORPUS, s2_base_dir)
    base_sig_seg = run_all(base_seg, queries)
    base_sig_s2 = run_all(base_s2, queries)
    base_seg.db.close(); base_s2.db.close()
    base_check = diff(base_sig_seg, base_sig_s2, queries)
    print("base seg vs s2:", base_check["mismatch"], flush=True)

    overlay = INDEX / "case_corpus"
    inc_work = INDEX / "inc_work"
    full_work = INDEX / "full_work"
    s2_work = INDEX / "s2_work"
    case_map = INDEX / "case_map.json"
    rows: list[dict[str, Any]] = []

    plan: list[tuple[str, int, Callable[..., tuple[dict[str, Any], list[str]]]]] = []
    for scenario in ("body", "add", "delete", "supersedes"):
        for count in (1, 10, 100, 1000):
            plan.append((scenario, count, lambda s=scenario, c=count: build_case(pages, sources, base_map, s, c, overlay)))
    for name, (fn, counts) in EXTRA.items():
        for count in counts:
            def make(fn=fn, c=count):
                payload, _ = build_case(pages, sources, base_map, "body", 0, overlay)
                changed = fn(pages, sources, payload, overlay, c, queries)
                payload["pages"] = dict(sorted(payload["pages"].items()))
                return payload, changed
            plan.append((name, count, make))

    for scenario, count, make in plan:
        t_case = time.perf_counter()
        payload, changed = make()
        write_map(case_map, payload)
        for d in (inc_work, full_work, s2_work):
            if d.exists():
                shutil.rmtree(d)
        shutil.copytree(seg_base_dir, inc_work)
        delta = SegmentedRanker.incremental_update(overlay, inc_work, case_map)
        full = SegmentedRanker.build(overlay, full_work)
        s2 = Structural2Ranker.build(overlay, s2_work)
        r_inc = SegmentedRanker.load(overlay, inc_work)
        r_full = SegmentedRanker.load(overlay, full_work)
        r_s2 = Structural2Ranker.load(overlay, s2_work)
        sig_inc = run_all(r_inc, queries)
        sig_full = run_all(r_full, queries)
        sig_s2 = run_all(r_s2, queries)
        for r in (r_inc, r_full, r_s2):
            r.db.close()
        row = {
            "scenario": scenario,
            "requested": count,
            "changed": len(changed),
            "changed_ids": changed[:5],
            "delta": {k: v for k, v in delta.items() if k != "map_root"},
            "incremental_ms": delta["elapsed_ms"],
            "segmented_full_ms": full.elapsed_ms,
            "structural2_full_ms": s2.elapsed_ms,
            "inc_vs_segfull": diff(sig_inc, sig_full, queries),
            "inc_vs_structural2": diff(sig_inc, sig_s2, queries),
            "segfull_vs_structural2": diff(sig_full, sig_s2, queries),
            "changed_vs_base": diff(sig_inc, base_sig_seg, queries)["mismatch"],
            "changed_vs_base_pages": diff(sig_inc, base_sig_seg, queries)["page_order_mismatch"],
            "changed_vs_base_by_type": {},
            "case_wall_s": round(time.perf_counter() - t_case, 2),
        }
        by_type: dict[str, int] = {}
        for i, q in enumerate(queries):
            if [h[0] for h in sig_inc[i]] != [h[0] for h in base_sig_seg[i]]:
                by_type[q["type"]] = by_type.get(q["type"], 0) + 1
        row["changed_vs_base_by_type"] = by_type
        rows.append(row)
        print(f"{scenario:20s} {count:5d} inc={delta['elapsed_ms']:8.1f}ms segfull={full.elapsed_ms:8.1f} s2={s2.elapsed_ms:8.1f} "
              f"inc≠segfull={row['inc_vs_segfull']['mismatch']} inc≠s2={row['inc_vs_structural2']['mismatch']} "
              f"changed_vs_base={row['changed_vs_base']} pages={row['changed_vs_base_pages']} {by_type}", flush=True)
        write_json(RESULTS / "identity.partial.json", {"base_seg_vs_s2": base_check, "cases": rows})
    for d in (inc_work, full_work, s2_work, overlay):
        shutil.rmtree(d, ignore_errors=True)
    case_map.unlink(missing_ok=True)
    write_json(RESULTS / "identity.json", {"base_seg_vs_s2": base_check, "cases": rows})
    (RESULTS / "identity.partial.json").unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

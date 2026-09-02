#!/usr/bin/env python3
"""증분 build == cold build 동일성 — bench/review_inc/identity.py 의 39 조합을 제품 경로로 다시 돈다.

    python3 bench/incr/identity.py            # bench/frozen/corpus 10,000 page · 500문항

조합: body/add/delete/supersedes × 1/10/100/1000 (codex 16) + chain_extend/chain_head_delete/
chain_middle_delete/df_shift/slug_rename × 1/10/100 + block_remove/move_source/empty_blocks/title_collide × 10/100
(codex P 23) = 39. 각 조합에서 base 사본에 변경을 가하고 `llmwiki.build(changed=힌트)` 로 증분, 같은 정본을
`build(full=True)` 로 cold 굽는다. 비교: 500문항 top-10 의 (page_id, score, block_ids) 와 산출물 바이트
(index/ 전부 + viewer/public/data). base 대비 순서가 바뀐 문항 수(검정력) 도 적는다.

원본 identity.py 는 run_experiment(export_markdown) 을 import 해 지금 저장소에서 돌지 않으므로 mutation 을
여기로 옮겼다. slug_rename 은 제품 validate 가 id == 'page:'+slug 를 요구해 id 도 함께 바꾼다(파일 경로는 그대로).
결과: bench/results_incr/identity.json.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import time
from pathlib import Path
from typing import Any, Callable

from common import (ROOT, FROZEN_CORPUS, artifact_prints, clone_root, diff_signatures, frozen_queries, llmwiki,
                    load_pages, prepare_root, signatures, write_json, write_page)

Mutation = Callable[[Path, dict, dict, int, list], list[str]]


def sha_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def set_block_text(page: dict[str, Any], bid: str, text: str) -> None:
    block = page["blocks"][bid]
    block["source_text"] = text
    block["data"] = {**(block.get("data") or {}), "text": text}
    block["fingerprint"] = sha_text(text)


# ---------------------------------------------------------------- codex 16
def mut_body(root, pages, sources, count, queries):
    out = []
    for idx, pid in enumerate(sorted(pages)[:count]):
        page = copy.deepcopy(pages[pid])
        bid = page["block_order"][0]
        set_block_text(page, bid, str(page["blocks"][bid].get("source_text") or "") + f" 증분본문수정-{idx:04d}")
        write_page(root, sources[pid], page)
        out.append(sources[pid])
    return out


def mut_add(root, pages, sources, count, queries):
    out = []
    for idx, template_id in enumerate(sorted(pages)[:count]):
        page = copy.deepcopy(pages[template_id])
        pid, slug = f"page:inc-added-{idx:06d}", f"inc-added-{idx:06d}"
        page["id"], page["slug"], page["title"] = pid, slug, f"증분 추가 문서 {idx:06d}"
        remap, blocks, order = {}, {}, []
        for seq, old in enumerate(page["block_order"]):
            nb = f"block:inc-added-{idx:06d}-{seq:03d}"
            remap[old] = nb
            block = copy.deepcopy(page["blocks"][old])
            block["id"] = nb
            blocks[nb] = block
            order.append(nb)
        page["blocks"], page["block_order"] = blocks, order
        for link in page.get("links") or []:
            if link.get("block_id") in remap:
                link["block_id"] = remap[link["block_id"]]
        page["history"] = [{"at": "2026-09-02", "action": "incremental-add", "actor": "bench-inc"}]
        rel = f"wiki/sources/{slug}.json"
        write_page(root, rel, page)
        out.append(rel)
    return out


def mut_delete(root, pages, sources, count, queries):
    out = []
    for pid in sorted(pages)[:count]:
        (root / sources[pid]).unlink()
        out.append(sources[pid])
    return out


def mut_supersedes(root, pages, sources, count, queries):
    ids = sorted(pages)
    cands = [pid for pid in ids if not any(l.get("kind") == "supersedes" for l in pages[pid].get("links") or [])]
    out = []
    for idx, pid in enumerate(cands[:count]):
        page = copy.deepcopy(pages[pid])
        target = ids[(ids.index(pid) + 5000 + idx) % len(ids)]
        if target == pid:
            target = ids[(ids.index(pid) + 1) % len(ids)]
        page.setdefault("links", []).append({"target": target, "kind": "supersedes", "block_id": page["block_order"][0]})
        write_page(root, sources[pid], page)
        out.append(sources[pid])
    return out


# ---------------------------------------------------------------- codex P 23
def chain_heads(pages):
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
    return succ


def temporal_golds(queries):
    out: list[str] = []
    for q in queries:
        if q["type"] == "temporal":
            for pid in q["gold_pages"]:
                if pid not in out:
                    out.append(pid)
    return out


def mut_chain_extend(root, pages, sources, count, queries):
    out = []
    for idx, head_id in enumerate(temporal_golds(queries)[:count]):
        head = pages[head_id]
        pid, slug = f"page:inc-newhead-{idx:05d}", f"inc-newhead-{idx:05d}"
        page = copy.deepcopy(head)
        page["id"], page["slug"], page["title"] = pid, slug, f"증분 신판 {idx:05d}"
        src_bid = head["block_order"][0]
        for bid in head["block_order"]:
            if bid.endswith(":temporal"):
                src_bid = bid
        block = copy.deepcopy(head["blocks"][src_bid])
        nbid = f"block:{slug}:temporal"
        block["id"] = nbid
        page["blocks"], page["block_order"] = {nbid: block}, [nbid]
        page["links"] = [{"target": head["slug"], "kind": "supersedes", "block_id": nbid, "label": head["slug"], "anchor": ""}]
        page["history"] = [{"at": "2026-12-31", "action": "inc-newhead", "actor": "review"}]
        page["created"] = page["updated"] = "2026-12-31"
        rel = f"wiki/syntheses/{slug}.json"
        write_page(root, rel, page)
        out.append(rel)
    return out


def mut_chain_head_delete(root, pages, sources, count, queries):
    out = []
    for head_id in temporal_golds(queries)[:count]:
        (root / sources[head_id]).unlink()
        out.append(sources[head_id])
    return out


def mut_chain_middle_delete(root, pages, sources, count, queries):
    succ = chain_heads(pages)
    preds = set(succ.values())
    out = []
    for pid in sorted(pid for pid in succ if pid in preds)[:count]:
        (root / sources[pid]).unlink()
        out.append(sources[pid])
    return out


def mut_df_shift(root, pages, sources, count, queries):
    blob = " ".join(q["text"] for q in queries[:200])
    out = []
    for pid in sorted(pages)[:count]:
        page = copy.deepcopy(pages[pid])
        bid = page["block_order"][0]
        set_block_text(page, bid, str(page["blocks"][bid].get("source_text") or "") + " " + blob)
        write_page(root, sources[pid], page)
        out.append(sources[pid])
    return out


def mut_slug_rename(root, pages, sources, count, queries):
    indeg: dict[str, int] = {}
    for p in pages.values():
        for link in p.get("links") or []:
            t = str(link.get("target") or "")
            indeg[t] = indeg.get(t, 0) + 1
    by_slug = {p["slug"]: pid for pid, p in pages.items()}
    targets = [by_slug[s] for s, _n in sorted(indeg.items(), key=lambda kv: (-kv[1], kv[0])) if s in by_slug]
    out = []
    for idx, pid in enumerate(targets[:count]):
        page = copy.deepcopy(pages[pid])
        page["slug"] = f"inc-renamed-{idx:05d}"
        page["id"] = "page:" + page["slug"]              # 제품 validate: id == page:slug
        page["title"] = f"증분 개명 {idx:05d}"
        write_page(root, sources[pid], page)
        out.append(sources[pid])
    return out


def mut_block_remove(root, pages, sources, count, queries):
    out = []
    for pid in sorted(pages)[:count]:
        page = copy.deepcopy(pages[pid])
        order = list(page["block_order"])
        if len(order) < 2:
            continue
        gone = order.pop(0)
        del page["blocks"][gone]
        page["block_order"] = list(reversed(order))
        page["links"] = [l for l in page.get("links") or [] if l.get("block_id") != gone]
        write_page(root, sources[pid], page)
        out.append(sources[pid])
    return out


def mut_move_source(root, pages, sources, count, queries):
    out = []
    for idx, pid in enumerate(sorted(pages)[:count]):
        (root / sources[pid]).unlink()
        rel = f"wiki/moved/{idx:05d}.json"
        write_page(root, rel, pages[pid])
        out.extend([sources[pid], rel])
    return out


def mut_empty_blocks(root, pages, sources, count, queries):
    out = []
    for pid in sorted(pages)[:count]:
        page = copy.deepcopy(pages[pid])
        for bid in page["block_order"]:
            page["blocks"][bid]["source_text"] = ""
            page["blocks"][bid]["data"] = {"text": ""}
        write_page(root, sources[pid], page)
        out.append(sources[pid])
    return out


def mut_title_collide(root, pages, sources, count, queries):
    golds = [q["gold_pages"][0] for q in queries if q["type"] == "relation"]
    ids = sorted(pages)
    out = []
    for idx, gold in enumerate(golds[:count]):
        pid = ids[idx]
        if pid == gold:
            continue
        page = copy.deepcopy(pages[pid])
        page["title"] = pages[gold]["slug"]
        write_page(root, sources[pid], page)
        out.append(sources[pid])
    return out


PLAN: list[tuple[str, Mutation, tuple[int, ...]]] = [
    ("body", mut_body, (1, 10, 100, 1000)),
    ("add", mut_add, (1, 10, 100, 1000)),
    ("delete", mut_delete, (1, 10, 100, 1000)),
    ("supersedes", mut_supersedes, (1, 10, 100, 1000)),
    ("chain_extend", mut_chain_extend, (1, 10, 100)),
    ("chain_head_delete", mut_chain_head_delete, (1, 10, 100)),
    ("chain_middle_delete", mut_chain_middle_delete, (1, 10, 100)),
    ("df_shift", mut_df_shift, (1, 10, 100)),
    ("slug_rename", mut_slug_rename, (1, 10, 100)),
    ("block_remove", mut_block_remove, (10, 100)),
    ("move_source", mut_move_source, (10, 100)),
    ("empty_blocks", mut_empty_blocks, (10, 100)),
    ("title_collide", mut_title_collide, (10, 100)),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", help="임시 root 를 둘 곳 (기본 bench/index_incr/identity)")
    ap.add_argument("--only", help="시나리오 이름 (쉼표)")
    args = ap.parse_args()
    work = Path(args.work) if args.work else ROOT / "bench" / "index_incr" / "identity"
    queries = frozen_queries()
    base = prepare_root(work / "base", FROZEN_CORPUS, allow_aliases=True)
    t0 = time.perf_counter()
    st = llmwiki.build(llmwiki.Workspace(base), full=True)
    print(f"base cold build {st['ms']} ms ({round(time.perf_counter() - t0, 1)} s wall)", flush=True)
    pages, sources = load_pages(base)
    base_sig = signatures(base, queries)
    case_root = work / "case"
    rows: list[dict[str, Any]] = []
    only = set(args.only.split(",")) if args.only else None
    for name, fn, counts in PLAN:
        if only and name not in only:
            continue
        for count in counts:
            t_case = time.perf_counter()
            clone_root(base, case_root)
            ws = llmwiki.Workspace(case_root)
            hint = fn(case_root, pages, sources, count, queries)
            t = time.perf_counter()
            inc = llmwiki.build(ws, changed=hint)
            inc_ms = round((time.perf_counter() - t) * 1000, 1)
            inc_sig = signatures(case_root, queries)
            inc_prints = artifact_prints(case_root)
            t = time.perf_counter()
            cold = llmwiki.build(ws, full=True)
            cold_ms = round((time.perf_counter() - t) * 1000, 1)
            cold_sig = signatures(case_root, queries)
            cold_prints = artifact_prints(case_root)
            d = diff_signatures(inc_sig, cold_sig, queries)
            vs_base = diff_signatures(inc_sig, base_sig, queries)
            by_type: dict[str, int] = {}
            for i, q in enumerate(queries):
                if [h[0] for h in inc_sig[i]] != [h[0] for h in base_sig[i]]:
                    by_type[q["type"]] = by_type.get(q["type"], 0) + 1
            row = {"scenario": name, "requested": count, "changed_files": len(hint), "mode": inc["mode"],
                   "reason": inc["reason"], "delta": inc["delta"], "reindexed": inc.get("index", {}).get("reindexed", 0),
                   "incremental_ms": inc_ms, "incremental_phases": inc["phases"], "cold_ms": cold_ms,
                   "mismatch": d["mismatch"], "examples": d["examples"],
                   "bytes_identical": inc_prints == cold_prints,
                   "search_sqlite_identical": inc_prints.get("index/search.sqlite") == cold_prints.get("index/search.sqlite"),
                   "differing": sorted(k for k in set(inc_prints) | set(cold_prints) if inc_prints.get(k) != cold_prints.get(k)),
                   "changed_vs_base": vs_base["mismatch"], "changed_vs_base_pages": vs_base["page_order_mismatch"],
                   "changed_vs_base_by_type": by_type, "case_wall_s": round(time.perf_counter() - t_case, 1)}
            rows.append(row)
            print(f"{name:20s} {count:5d} mode={inc['mode']:11s} inc={inc_ms:8.1f}ms cold={cold_ms:8.1f}ms "
                  f"mismatch={d['mismatch']} bytes={'same' if row['bytes_identical'] else 'DIFF'} "
                  f"vs_base={vs_base['mismatch']} pages={vs_base['page_order_mismatch']} {by_type}", flush=True)
            write_json(ROOT / "bench" / "results_incr" / "identity.partial.json", {"cases": rows})
    summary = {"cases": len(rows), "all_incremental": all(r["mode"] == "incremental" for r in rows),
               "mismatch_total": sum(r["mismatch"] for r in rows),
               "bytes_identical_all": all(r["bytes_identical"] for r in rows)}
    write_json(ROOT / "bench" / "results_incr" / "identity.json", {"summary": summary, "cases": rows})
    (ROOT / "bench" / "results_incr" / "identity.partial.json").unlink(missing_ok=True)
    print(summary, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

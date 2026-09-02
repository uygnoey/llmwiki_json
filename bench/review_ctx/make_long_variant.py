#!/usr/bin/env python3
"""500문항 중 유형별 20개 gold block을 600/900/1200자로 늘린 정본 뷰."""
from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CORPUS = ROOT / "bench/frozen/corpus"
QUERIES = ROOT / "bench/frozen/queries.json"
OUT = ROOT / "bench/review_ctx/long_root/wiki"
OUT_QUERIES = ROOT / "bench/review_ctx/long_queries.json"
OUT_MANIFEST = ROOT / "bench/review_ctx/long_manifest.json"
TARGETS = (600, 900, 1200)


def main() -> int:
    payload = json.loads(QUERIES.read_text(encoding="utf-8"))
    queries = payload["queries"] if isinstance(payload, dict) else payload
    seen: dict[str, int] = {}
    chosen = []
    for query in queries:
        kind = query["type"]
        if seen.get(kind, 0) < 20:
            chosen.append(query)
            seen[kind] = seen.get(kind, 0) + 1
    wanted = {bid: (query["id"], TARGETS[i % len(TARGETS)])
              for i, query in enumerate(chosen) for bid in query["gold_blocks"]}
    manifest: dict[str, dict[str, object]] = {}
    OUT.mkdir(parents=True, exist_ok=True)
    for source in sorted(CORPUS.rglob("*.json")):
        target = OUT / source.relative_to(CORPUS)
        target.parent.mkdir(parents=True, exist_ok=True)
        page = json.loads(source.read_text(encoding="utf-8"))
        changed = False
        for bid, block in (page.get("blocks") or {}).items():
            if bid not in wanted:
                continue
            qid, target_chars = wanted[bid]
            original = str((block.get("data") or {}).get("text") or block.get("source_text") or "")
            sentinel = f" [FULL-BODY-END:{qid}]"
            filler_unit = " 긴 문서의 중간 근거를 보존하는 검증용 문장이다."
            filler = ""
            while len(original + filler + sentinel) < target_chars:
                filler += filler_unit
            long_text = (original + filler + sentinel)[:target_chars - len(sentinel)] + sentinel
            block.setdefault("data", {})["text"] = long_text
            block["source_text"] = long_text
            manifest[bid] = {
                "query_id": qid,
                "page_id": page["id"],
                "chars": len(long_text),
                "original_chars": len(original),
                "text": long_text,
            }
            changed = True
        if changed:
            target.write_text(json.dumps(page, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        elif not target.exists():
            os.link(source, target)
    OUT_QUERIES.write_text(
        json.dumps({"corpus_pages": payload.get("corpus_pages"), "queries": chosen},
                   ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    OUT_MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"queries": len(chosen), "modified_blocks": len(manifest),
                      "lengths": sorted({row["chars"] for row in manifest.values()}),
                      "files": sum(1 for _ in OUT.rglob("*.json"))}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

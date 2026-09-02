#!/usr/bin/env python3
"""기존 llmwiki_bench 색인을 재색인하지 않고 paraphrase 표본을 직접 조회한다."""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from common import DEFAULT_CORPUS, DEFAULT_QUERIES, DEFAULT_RESULTS, ROOT, write_json


COLLECTION = "llmwiki_bench"


def result_key(raw_path: Any) -> str:
    raw = str(raw_path or "").split("?", 1)[0]
    if raw.startswith("qmd://"):
        raw = urlparse(raw).path.lstrip("/")
    return Path(unquote(raw)).name


def profile(query: dict[str, Any]) -> str:
    match = re.search(r"난이도=([^;]+)", str(query.get("notes", "")))
    return match.group(1) if match else "unknown"


def distractors(query: dict[str, Any]) -> list[str]:
    return str(query["notes"]).split("distractors=", 1)[1].rstrip(".").split(",")


def page_body(page: dict[str, Any]) -> str:
    return "\n".join(
        str(page["blocks"][block_id].get("source_text", ""))
        for block_id in page.get("block_order", page.get("blocks", {}))
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queries", type=Path, default=DEFAULT_QUERIES)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument(
        "--index-dir",
        type=Path,
        default=ROOT / "bench" / "index" / "vector-mode_vsearch",
    )
    parser.add_argument("--sample", type=int, default=10)
    parser.add_argument("--limit", type=int, default=10_000)
    parser.add_argument("--out", type=Path, default=DEFAULT_RESULTS / "vector_probe.json")
    args = parser.parse_args()

    index_dir = args.index_dir.resolve()
    db_path = index_dir / ".qmd" / "index.sqlite"
    page_map_path = index_dir / "page-map.json"
    if not db_path.is_file() or not page_map_path.is_file():
        raise RuntimeError(f"기존 qmd 색인이 없다(재색인하지 않음): {index_dir}")

    payload = json.loads(args.queries.read_text(encoding="utf-8"))
    queries = [query for query in payload["queries"] if query["type"] == "paraphrase"]
    selected = queries[: args.sample]
    page_map = json.loads(page_map_path.read_text(encoding="utf-8"))
    targets = {
        page_id
        for query in selected
        for page_id in query["gold_pages"] + distractors(query)
    }
    pages = {}
    for path in sorted(args.corpus.rglob("*.json")):
        page = json.loads(path.read_text(encoding="utf-8"))
        if page["id"] in targets:
            pages[page["id"]] = page

    env = os.environ.copy()
    env["PWD"] = str(index_dir)
    probes = []
    for query in selected:
        command = [
            "qmd",
            "vsearch",
            str(query["text"]),
            "-c",
            COLLECTION,
            "--format",
            "csv",
            "-n",
            str(args.limit),
        ]
        proc = subprocess.run(
            command,
            cwd=index_dir,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"qmd vsearch 실패({proc.returncode}): {(proc.stderr or proc.stdout).strip()}"
            )
        # qmd 2.0.1은 큰 `--format json` 결과를 닫는 따옴표 없이 끝내지만 CSV는
        # 같은 순위/점수를 끝까지 제공한다. snippet의 마지막 레코드가 잘려도
        # 앞의 file/score 필드는 온전하므로 DictReader로 직접 읽는다.
        rows = list(csv.DictReader(io.StringIO(proc.stdout)))

        ranked = []
        seen = set()
        for row in rows:
            filename = result_key(row.get("path") or row.get("file") or row.get("uri"))
            page_id = page_map.get(filename)
            if not page_id or page_id in seen:
                continue
            ranked.append(
                {
                    "page_id": page_id,
                    "score": float(row.get("score", 0.0)),
                }
            )
            seen.add(page_id)

        position = {row["page_id"]: index + 1 for index, row in enumerate(ranked)}
        score = {row["page_id"]: row["score"] for row in ranked}
        gold = query["gold_pages"][0]
        ds = distractors(query)
        identifier = next(
            (token for token in str(query["text"]).split() if token.endswith("-nimbus")),
            "",
        )
        gold_text = page_body(pages[gold])
        distractor_texts = {page_id: page_body(pages[page_id]) for page_id in ds}
        probes.append(
            {
                "id": query["id"],
                "profile": profile(query),
                "query": query["text"],
                "returned_pages": len(ranked),
                "gold": {
                    "page_id": gold,
                    "rank": position.get(gold),
                    "rank_lower_bound_if_missing": (
                        None if gold in position else len(ranked) + 1
                    ),
                    "score": score.get(gold),
                    "evidence": gold_text,
                },
                "distractors": [
                    {
                        "page_id": page_id,
                        "rank": position.get(page_id),
                        "rank_lower_bound_if_missing": (
                            None if page_id in position else len(ranked) + 1
                        ),
                        "score": score.get(page_id),
                        "evidence": distractor_texts[page_id],
                    }
                    for page_id in ds
                ],
                "top10": [
                    {
                        **row,
                        "role": (
                            "gold"
                            if row["page_id"] == gold
                            else "distractor"
                            if row["page_id"] in ds
                            else "other"
                        ),
                    }
                    for row in ranked[:10]
                ],
                "retrievable_information": {
                    "identifier": identifier,
                    "gold_contains_identifier": bool(identifier and identifier in gold_text),
                    "all_distractors_contain_identifier": all(
                        identifier in text for text in distractor_texts.values()
                    ),
                    "gold_contains_literal_query": str(query["text"]) in gold_text,
                    "all_distractors_contain_literal_query": all(
                        str(query["text"]) in text for text in distractor_texts.values()
                    ),
                },
                "qmd_trace": proc.stderr.splitlines(),
            }
        )

    output = {
        "schema_version": "1.0",
        "seed": payload.get("seed"),
        "collection": COLLECTION,
        "index_dir": args.index_dir.as_posix(),
        "reindexed": False,
        "command_template": "qmd vsearch <query> -c llmwiki_bench --format csv -n 10000",
        "selection": "first N paraphrase queries; deterministic",
        "probes": probes,
    }
    write_json(args.out, output)
    print(args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

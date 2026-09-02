#!/usr/bin/env python3
"""동결 합성 코퍼스의 정답 누출·무작위 링크·실제 wiki 대비 특성을 센다."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from common import (
    DEFAULT_CORPUS,
    DEFAULT_QUERIES,
    DEFAULT_RESULTS,
    ROOT,
    load_queries,
    verify_frozen,
    write_json,
)


sys.path.insert(0, str(ROOT / "bench"))
from rankers.structural import query_terms, tokenize  # noqa: E402


TOKEN_RE = re.compile(r"[0-9A-Za-z가-힣._:-]+")
PAIR_RE = re.compile(r"한국어 쌍 (page:[^와]+)와 related")


def source_tokens(text: str) -> set[str]:
    return {token.lower() for token in TOKEN_RE.findall(text)}


def body(page: dict[str, Any]) -> str:
    return "\n".join(
        str(page["blocks"][block_id].get("source_text", ""))
        for block_id in page.get("block_order", page.get("blocks", {}))
    )


def distractors(query: dict[str, Any]) -> list[str]:
    notes = str(query.get("notes", ""))
    if "distractors=" in notes:
        return notes.split("distractors=", 1)[1].rstrip(".").split(",")
    if "근접 오답 " in notes:
        return notes.split("근접 오답 ", 1)[1].split("에는", 1)[0].split(",")
    if "; page:" in notes and "는 두 이름만" in notes:
        return ("page:" + notes.split("; page:", 1)[1].split("는 두 이름만", 1)[0]).split(",")
    return []


def profile(query: dict[str, Any]) -> str:
    notes = str(query.get("notes", ""))
    match = re.search(r"난이도=([^;]+)", notes)
    return match.group(1) if match else "unknown"


def rank_distribution(
    queries: list[dict[str, Any]], per_query_path: Path
) -> dict[str, dict[str, int]]:
    rows = {
        row["id"]: row
        for row in json.loads(per_query_path.read_text(encoding="utf-8"))
    }
    out: dict[str, Counter[str]] = {}
    for query in queries:
        if query["type"] != "paraphrase":
            continue
        ranked = rows[query["id"]]["ranked"]
        gold = query["gold_pages"][0]
        rank = str(ranked.index(gold) + 1) if gold in ranked else ">10"
        out.setdefault(profile(query), Counter())[rank] += 1
    return {key: dict(sorted(value.items())) for key, value in out.items()}


def load_pages(root: Path) -> list[dict[str, Any]]:
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(root.rglob("*.json"))
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--queries", type=Path, default=DEFAULT_QUERIES)
    parser.add_argument(
        "--per-query",
        type=Path,
        default=ROOT / "bench" / "results" / "10000p-structural.perquery.json",
    )
    parser.add_argument("--wiki", type=Path, default=ROOT / "wiki")
    parser.add_argument("--out", type=Path, default=DEFAULT_RESULTS / "corpus_probe.json")
    args = parser.parse_args()

    manifest = verify_frozen(args.corpus, args.queries)
    query_payload, queries = load_queries(args.queries)
    pages = load_pages(args.corpus)
    by_id = {page["id"]: page for page in pages}
    by_slug = {page["slug"]: page["id"] for page in pages}
    bodies = {page_id: body(page).lower() for page_id, page in by_id.items()}
    body_tokens = {page_id: set(tokenize(text)) for page_id, text in bodies.items()}

    edges: dict[str, list[tuple[str, str, str]]] = {}
    incoming: dict[str, list[tuple[str, str, str]]] = {page_id: [] for page_id in by_id}
    for page_id, page in by_id.items():
        page_edges = []
        for edge in page.get("links", []):
            target = by_slug.get(str(edge.get("target")), str(edge.get("target")))
            item = (target, str(edge.get("kind")), str(edge.get("block_id")))
            page_edges.append(item)
            incoming.setdefault(target, []).append((page_id, item[1], item[2]))
        edges[page_id] = page_edges

    exact = [query for query in queries if query["type"] == "exact"]
    relation = [query for query in queries if query["type"] == "relation"]
    temporal = [query for query in queries if query["type"] == "temporal"]
    crosslingual = [query for query in queries if query["type"] == "crosslingual"]
    paraphrase = [query for query in queries if query["type"] == "paraphrase"]

    exact_probe = {
        "queries": len(exact),
        "distractor_contains_literal_query": sum(
            all(query["text"].lower() in bodies[page_id] for page_id in distractors(query))
            for query in exact
        ),
        "identifier_document_frequency": Counter(),
    }
    for query in exact:
        identifier = next(term for term in query_terms(query["text"]) if term.startswith("cfg.pipeline."))
        df = sum(identifier in tokens for tokens in body_tokens.values())
        exact_probe["identifier_document_frequency"][str(df)] += 1
    exact_probe["identifier_document_frequency"] = dict(
        sorted(exact_probe["identifier_document_frequency"].items())
    )

    relation_probe = {
        "queries": len(relation),
        "gold_block_owns_related_edge": 0,
        "all_distractors_contain_both_identifiers": 0,
        "distractor_owns_gold_relation_edge": 0,
        "profiles": dict(Counter(profile(query) for query in relation)),
        "block_hidden_true": sum("block-hidden=true" in query["notes"] for query in relation),
    }
    for query in relation:
        gold = query["gold_pages"][0]
        gold_block = query["gold_blocks"][0]
        if any(kind == "related" and block_id == gold_block for _, kind, block_id in edges[gold]):
            relation_probe["gold_block_owns_related_edge"] += 1
        identifiers = [term for term in query_terms(query["text"]) if term.endswith(("-astra", "-boreal"))]
        ds = distractors(query)
        if all(all(term in body_tokens[page_id] for term in identifiers) for page_id in ds):
            relation_probe["all_distractors_contain_both_identifiers"] += 1
        if any(any(kind == "related" for _, kind, _ in edges[page_id]) for page_id in ds):
            relation_probe["distractor_owns_gold_relation_edge"] += 1

    temporal_probe = {
        "queries": len(temporal),
        "profiles": dict(Counter(profile(query) for query in temporal)),
        "gold_has_supersedes_edge": 0,
        "all_strong_weak_stale_more_lexical": 0,
    }
    for query in temporal:
        gold = query["gold_pages"][0]
        if any(kind == "supersedes" for _, kind, _ in edges[gold]):
            temporal_probe["gold_has_supersedes_edge"] += 1
        if profile(query) in {"strong", "weak"}:
            qtokens = source_tokens(query["text"])
            current_overlap = len(qtokens & source_tokens(bodies[gold]))
            if all(
                len(qtokens & source_tokens(bodies[stale])) > current_overlap
                for stale in query["stale_pages"]
            ):
                temporal_probe["all_strong_weak_stale_more_lexical"] += 1

    cross_probe = {
        "queries": len(crosslingual),
        "profiles": dict(Counter(profile(query) for query in crosslingual)),
        "paired_korean_contains_literal_query": 0,
        "paired_korean_related_to_gold": 0,
        "gold_zero_surface_overlap": 0,
    }
    for query in crosslingual:
        match = PAIR_RE.search(query["notes"])
        paired = match.group(1) if match else ""
        gold = query["gold_pages"][0]
        if paired and query["text"].lower() in bodies[paired]:
            cross_probe["paired_korean_contains_literal_query"] += 1
        if paired and any(target == gold and kind == "related" for target, kind, _ in edges[paired]):
            cross_probe["paired_korean_related_to_gold"] += 1
        if not (source_tokens(query["text"]) & source_tokens(bodies[gold])):
            cross_probe["gold_zero_surface_overlap"] += 1

    zero_queries = [query for query in paraphrase if profile(query) == "zero"]
    random_edge_rows = []
    distractor_to_gold = 0
    carrier_to_gold = 0
    carrier_to_gold_edges = 0
    non_wiki_carrier_edges = 0
    gold_to_carrier = 0
    for query in zero_queries:
        gold = query["gold_pages"][0]
        ds = set(distractors(query))
        carriers = {
            page_id
            for page_id, tokens in body_tokens.items()
            if tokens & set(query_terms(query["text"]))
        }
        direct_in = [item for item in incoming.get(gold, []) if item[0] in carriers]
        direct_out = [item for item in edges[gold] if item[0] in carriers]
        direct_distractor = [item for item in incoming.get(gold, []) if item[0] in ds]
        distractor_to_gold += bool(direct_distractor)
        carrier_to_gold += bool(direct_in)
        gold_to_carrier += bool(direct_out)
        carrier_to_gold_edges += len(direct_in)
        non_wiki_carrier_edges += sum(kind != "wiki" for _, kind, _ in direct_in)
        if direct_in or direct_out or direct_distractor:
            random_edge_rows.append(
                {
                    "id": query["id"],
                    "carrier_pages": len(carriers),
                    "carrier_to_gold": direct_in,
                    "gold_to_carrier": direct_out,
                    "distractor_to_gold": direct_distractor,
                }
            )

    paraphrase_probe = {
        "queries": len(paraphrase),
        "profiles": dict(Counter(profile(query) for query in paraphrase)),
        "zero_gold_surface_overlap": sum(
            not (source_tokens(query["text"]) & source_tokens(bodies[query["gold_pages"][0]]))
            for query in zero_queries
        ),
        "all_distractors_contain_literal_query": sum(
            all(query["text"].lower() in bodies[page_id] for page_id in distractors(query))
            for query in paraphrase
        ),
        "zero_distractor_to_gold_questions": distractor_to_gold,
        "zero_query_token_carrier_to_gold_questions": carrier_to_gold,
        "zero_query_token_carrier_to_gold_edges": carrier_to_gold_edges,
        "zero_non_wiki_carrier_to_gold_edges": non_wiki_carrier_edges,
        "zero_gold_to_query_token_carrier_questions": gold_to_carrier,
        "random_edge_details": random_edge_rows,
        "structural_gold_rank_distribution": rank_distribution(queries, args.per_query),
    }

    wiki_pages = load_pages(args.wiki)
    wiki_edges = [edge for page in wiki_pages for edge in page.get("links", [])]
    actual_wiki = {
        "pages": len(wiki_pages),
        "pages_with_aliases": sum(bool(page.get("aliases")) for page in wiki_pages),
        "links": len(wiki_edges),
        "link_kinds": dict(Counter(str(edge.get("kind")) for edge in wiki_edges)),
        "related_links": sum(edge.get("kind") == "related" for edge in wiki_edges),
        "supersedes_links": sum(edge.get("kind") == "supersedes" for edge in wiki_edges),
        "pages_with_sources": sum(bool(page.get("sources")) for page in wiki_pages),
    }

    payload = {
        "schema_version": "1.0",
        "seed": query_payload.get("seed"),
        "manifest": manifest,
        "synthetic_corpus": {
            "pages": len(pages),
            "links": sum(len(items) for items in edges.values()),
            "outgoing_links_per_page": sorted({len(items) for items in edges.values()}),
            "pages_with_base_template": sum(
                ("구조적 맥락" in text or "structural context" in text)
                for text in bodies.values()
            ),
        },
        "by_type": {
            "exact": exact_probe,
            "relation": relation_probe,
            "temporal": temporal_probe,
            "crosslingual": cross_probe,
            "paraphrase": paraphrase_probe,
        },
        "actual_wiki": actual_wiki,
    }
    write_json(args.out, payload)
    print(args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


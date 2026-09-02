#!/usr/bin/env python3
"""자연 문서에서 qmd vector, lexical-vector 융합, block chunk를 평가한다."""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import statistics
import subprocess
import sys
import threading
import time
from collections import Counter, defaultdict, deque
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote, urlparse


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "bench"))
sys.path.insert(0, str(ROOT))

from rankers.structural2 import Structural2Ranker  # noqa: E402
from review_nat.signals_review import structural_extra  # noqa: E402


INDEX = ROOT / "bench/index_vec_nat"
RESULTS = ROOT / "bench/results_vec_nat"
CORPUS = ROOT / "bench/natural/root/wiki"
QUERIES = ROOT / "bench/natural/queries.json"
STRUCTURAL_INDEX = ROOT / "bench/index_nat/structural2"
PAGE_COLLECTION = "vec_nat_pages"
BLOCK_COLLECTION = "vec_nat_blocks"
RRF_K = 60  # 표준 RRF 상수. 자연 세트에서 고르지 않았다.
TYPES = ("exact", "relation", "temporal", "crosslingual", "paraphrase", "long")


def qmd_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PWD"] = str(INDEX)
    return env


def parse_last_json_array(text: str) -> list[dict[str, Any]]:
    """qmd CLI의 진행 문구 뒤 마지막 JSON 배열을 찾는다."""
    decoder = json.JSONDecoder()
    candidates = [match.start() for match in re.finditer(r"\[\s*\{", text)]
    for start in candidates:
        try:
            value, end = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, list) and not text[start + end:].strip():
            return [row for row in value if isinstance(row, dict)]
    raise RuntimeError(f"qmd CLI JSON 배열을 못 찾음: {text[-1000:]}")


def raw_vector_top_score(query: str) -> tuple[float, float]:
    """qmd explain의 rank 정규화 전 vector backend score를 얻는다.

    MCP ``query`` tool의 공개 score는 단일 vec 목록에서도 1/rank로 정규화돼
    모든 질문의 1위가 1이다. 무주입 신호 평가는 같은 qmd typed-vec 검색을
    CLI ``--explain``으로 한 번 더 실행해 실제 cosine 계열 점수를 쓴다.
    """
    normalized_query = " ".join(str(query).split())
    started = time.perf_counter()
    proc = subprocess.run(
        [
            "qmd", "query", f"vec: {normalized_query}",
            "-c", PAGE_COLLECTION,
            "--format", "json",
            "-n", "1",
            "--min-score", "0",
            "--no-rerank",
            "--explain",
        ],
        cwd=INDEX,
        env=qmd_env(),
        text=True,
        capture_output=True,
        check=False,
    )
    latency_ms = (time.perf_counter() - started) * 1000.0
    if proc.returncode:
        raise RuntimeError(
            f"qmd typed-vec explain 실패(exit {proc.returncode}): "
            f"{(proc.stderr or proc.stdout).strip()}"
        )
    rows = parse_last_json_array(proc.stdout)
    if not rows:
        return 0.0, round(latency_ms, 4)
    scores = ((rows[0].get("explain") or {}).get("vectorScores") or [])
    return (float(scores[0]) if scores else 0.0), round(latency_ms, 4)


def quantile(values: Iterable[float], fraction: float) -> float | None:
    seq = sorted(float(value) for value in values)
    if not seq:
        return None
    index = max(0, min(len(seq) - 1, math.ceil(fraction * len(seq)) - 1))
    return round(seq[index], 4)


def distribution(values: Iterable[float]) -> dict[str, float | int | None]:
    seq = [float(value) for value in values]
    return {
        "n": len(seq),
        "min": round(min(seq), 4) if seq else None,
        "p05": quantile(seq, 0.05),
        "p50": quantile(seq, 0.50),
        "p95": quantile(seq, 0.95),
        "p99": quantile(seq, 0.99),
        "max": round(max(seq), 4) if seq else None,
        "mean": round(statistics.fmean(seq), 4) if seq else None,
    }


def result_filename(value: Any) -> str:
    text = str(value or "").split("?", 1)[0]
    if text.startswith("qmd://"):
        text = urlparse(text).path
    return Path(unquote(text)).name


class ResidentQmd:
    """project-local qmd mcp 한 프로세스를 전체 질문 동안 상주시킨다."""

    def __init__(self) -> None:
        self.proc = subprocess.Popen(
            ["qmd", "mcp"],
            cwd=INDEX,
            env=qmd_env(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self.request_id = 0
        self.stderr: deque[str] = deque(maxlen=200)
        self.stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self.stderr_thread.start()
        self._initialize()

    def _drain_stderr(self) -> None:
        assert self.proc.stderr is not None
        for line in self.proc.stderr:
            self.stderr.append(line.rstrip())

    def _send(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if self.proc.poll() is not None:
            raise RuntimeError(
                f"qmd mcp가 종료됨(exit {self.proc.returncode}): {' | '.join(self.stderr)}"
            )
        self.request_id += 1
        request = {"jsonrpc": "2.0", "id": self.request_id, "method": method}
        if params is not None:
            request["params"] = params
        assert self.proc.stdin is not None
        assert self.proc.stdout is not None
        self.proc.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError(
                    f"qmd mcp 응답 전에 EOF: {' | '.join(self.stderr)}"
                )
            try:
                response = json.loads(line)
            except json.JSONDecodeError:
                continue
            if response.get("id") != self.request_id:
                continue
            if "error" in response:
                raise RuntimeError(f"qmd mcp {method} 오류: {response['error']}")
            return response

    def _initialize(self) -> None:
        self._send(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "vector-nat-bench", "version": "1.0"},
            },
        )
        assert self.proc.stdin is not None
        notification = {
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
            "params": {},
        }
        self.proc.stdin.write(json.dumps(notification) + "\n")
        self.proc.stdin.flush()

    def vector(self, query: str, collection: str, limit: int = 10) -> tuple[list[dict[str, Any]], float]:
        started = time.perf_counter()
        response = self._send(
            "tools/call",
            {
                "name": "query",
                "arguments": {
                    "searches": [{"type": "vec", "query": query}],
                    "limit": limit,
                    "minScore": 0,
                    "collections": [collection],
                    "rerank": False,
                },
            },
        )
        latency_ms = (time.perf_counter() - started) * 1000.0
        result = response.get("result") or {}
        if result.get("isError"):
            raise RuntimeError(f"qmd query tool 오류: {result}")
        structured = result.get("structuredContent") or {}
        rows = structured.get("results") or []
        if not isinstance(rows, list):
            raise RuntimeError(f"qmd query results 형식 오류: {structured}")
        return [row for row in rows if isinstance(row, dict)], round(latency_ms, 4)

    def close(self) -> None:
        if self.proc.stdin is not None and not self.proc.stdin.closed:
            self.proc.stdin.close()
        try:
            self.proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)


def rrf(first: list[str], second: list[str]) -> list[str]:
    scores: dict[str, float] = defaultdict(float)
    for values in (first, second):
        for rank, value in enumerate(values, 1):
            scores[value] += 1.0 / (RRF_K + rank)
    return sorted(scores, key=lambda value: (-scores[value], value))


def recall_at(values: list[str], gold: set[str], k: int) -> bool:
    return bool(set(values[:k]) & gold)


def metric(rows: list[dict[str, Any]], key: str, k: int, query_type: str | None = None) -> float:
    chosen = [
        row for row in rows
        if row["expects_injection"] and (query_type is None or row["type"] == query_type)
    ]
    if not chosen:
        return 0.0
    return round(
        sum(recall_at(row[key], set(row["gold_pages"]), k) for row in chosen) / len(chosen),
        4,
    )


def metric_table(rows: list[dict[str, Any]]) -> dict[str, Any]:
    methods = ("lexical", "vector", "rrf", "conditional", "oracle")
    out: dict[str, Any] = {}
    for query_type in (*TYPES, "all"):
        type_arg = None if query_type == "all" else query_type
        out[query_type] = {
            method: {
                "recall@5": metric(rows, method, 5, type_arg),
                "recall@10": metric(rows, method, 10, type_arg),
            }
            for method in methods
        }
    return out


def parity(row: dict[str, Any]) -> int:
    return int(str(row["id"])[1:]) % 2


def weak_at(row: dict[str, Any], best_threshold: float, raw_threshold: float) -> bool:
    signal = row["weak_signal"]
    return bool(
        float(signal["top1_best"]) < best_threshold
        or float(signal["content_raw"]) < raw_threshold
    )


def choose_conditional_thresholds(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """홀수 id에서 목표 유형 top-5를 최대화하고 호출이 적은 경계를 고른다."""
    train = [row for row in rows if parity(row) == 1]
    target = [row for row in train if row["type"] in {"crosslingual", "paraphrase"}]
    answerable = [row for row in train if row["expects_injection"]]
    best_values = sorted({float(row["weak_signal"]["top1_best"]) for row in train})
    raw_values = sorted({float(row["weak_signal"]["content_raw"]) for row in train})
    best_floor = min(best_values) - max(1.0, abs(min(best_values))) * 1e-9
    raw_floor = min(raw_values) - max(1.0, abs(min(raw_values))) * 1e-9
    best_candidates = [best_floor] + [math.nextafter(value, math.inf) for value in best_values]
    raw_candidates = [raw_floor] + [math.nextafter(value, math.inf) for value in raw_values]
    chosen: tuple[tuple[int, int, int, float, float], float, float] | None = None
    for best_threshold in best_candidates:
        for raw_threshold in raw_candidates:
            calls = sum(weak_at(row, best_threshold, raw_threshold) for row in train)
            target_hits = sum(
                recall_at(
                    row["rrf"] if weak_at(row, best_threshold, raw_threshold) else row["lexical"],
                    set(row["gold_pages"]),
                    5,
                )
                for row in target
            )
            all_hits = sum(
                recall_at(
                    row["rrf"] if weak_at(row, best_threshold, raw_threshold) else row["lexical"],
                    set(row["gold_pages"]),
                    5,
                )
                for row in answerable
            )
            objective = (target_hits, -calls, all_hits, -best_threshold, -raw_threshold)
            if chosen is None or objective > chosen[0]:
                chosen = (objective, best_threshold, raw_threshold)
    assert chosen is not None
    _objective, best_threshold, raw_threshold = chosen
    return {
        "best_threshold": best_threshold,
        "raw_threshold": raw_threshold,
        "selection": (
            "홀수 id crosslingual+paraphrase recall@5 정답 수 최대; "
            "동률이면 홀수 전체 vector 호출 수 최소, 홀수 answerable recall@5 최대"
        ),
    }


def conditional_split_metrics(
    rows: list[dict[str, Any]], best_threshold: float, raw_threshold: float, split: int
) -> dict[str, Any]:
    chosen = [row for row in rows if parity(row) == split]
    answerable = [row for row in chosen if row["expects_injection"]]
    unrelated = [row for row in chosen if not row["expects_injection"]]

    def call_rate(group: list[dict[str, Any]]) -> float:
        return round(sum(weak_at(row, best_threshold, raw_threshold) for row in group) / len(group), 4) if group else 0.0

    def recall(group: list[dict[str, Any]], query_type: str | None, k: int) -> float:
        group = [row for row in group if query_type is None or row["type"] == query_type]
        return round(
            sum(
                recall_at(
                    row["rrf"] if weak_at(row, best_threshold, raw_threshold) else row["lexical"],
                    set(row["gold_pages"]),
                    k,
                )
                for row in group
            ) / len(group), 4
        ) if group else 0.0

    return {
        "split": "odd-train" if split == 1 else "even-test",
        "n": len(chosen),
        "call_rate": call_rate(chosen),
        "answerable_call_rate": call_rate(answerable),
        "unrelated_call_rate": call_rate(unrelated),
        "recall": {
            query_type: {
                "recall@5": recall(answerable, None if query_type == "all" else query_type, 5),
                "recall@10": recall(answerable, None if query_type == "all" else query_type, 10),
            }
            for query_type in ("crosslingual", "paraphrase", "all")
        },
    }


def best_score_gate(rows: list[dict[str, Any]], fpr_limit: float = 0.05) -> dict[str, Any]:
    train = [row for row in rows if parity(row) == 1]
    test = [row for row in rows if parity(row) == 0]
    positives = [row for row in train if row["expects_injection"]]
    negatives = [row for row in train if not row["expects_injection"]]
    values = sorted({float(row["vector_backend_top_score"]) for row in train}, reverse=True)
    maximum = values[0] if values else 0.0
    candidates = [maximum + 1e-9, *values]
    best: tuple[float, float, float] | None = None
    for threshold in candidates:
        fpr = sum(row["vector_backend_top_score"] >= threshold for row in negatives) / len(negatives)
        if fpr > fpr_limit:
            continue
        tpr = sum(row["vector_backend_top_score"] >= threshold for row in positives) / len(positives)
        if best is None or (tpr, -fpr, -threshold) > (best[1], -best[2], -best[0]):
            best = (threshold, tpr, fpr)
    assert best is not None
    threshold, tpr, fpr = best

    def rate(group: list[dict[str, Any]]) -> float:
        return round(sum(row["vector_backend_top_score"] >= threshold for row in group) / len(group), 4) if group else 0.0

    def split_result(split_rows: list[dict[str, Any]]) -> dict[str, Any]:
        split_pos = [row for row in split_rows if row["expects_injection"]]
        split_neg = [row for row in split_rows if not row["expects_injection"]]
        hard = [row for row in split_neg if row.get("hardness") == "hard"]
        off = [row for row in split_neg if row.get("hardness") != "hard"]
        return {
            "answerable_injection": rate(split_pos),
            "unrelated_fpr": rate(split_neg),
            "hard_fpr": rate(hard),
            "off_topic_fpr": rate(off),
            "by_type_injection": {
                query_type: rate([row for row in split_pos if row["type"] == query_type])
                for query_type in TYPES
            },
        }

    return {
        "fpr_limit": fpr_limit,
        "threshold": threshold,
        "selection": "홀수 id에서 unrelated FPR <= 0.05인 문턱 중 answerable 주입 최대",
        "odd_train": split_result(train),
        "even_test": split_result(test),
    }


def auc(rows: list[dict[str, Any]]) -> float:
    pos = [float(row["vector_backend_top_score"]) for row in rows if row["expects_injection"]]
    neg = [float(row["vector_backend_top_score"]) for row in rows if not row["expects_injection"]]
    wins = sum(1.0 if p > n else 0.5 if p == n else 0.0 for p in pos for n in neg)
    return round(wins / (len(pos) * len(neg)), 4)


def structural_block_lookup(ranker: Structural2Ranker, query: str) -> dict[str, list[str]]:
    _page_lex, page_blocks = ranker._lex(query)
    if not page_blocks:
        return {}
    page_ids = {int(rid): str(page_id) for rid, page_id in ranker.db.execute("SELECT rid,page_id FROM page")}
    block_rids = [brid for values in page_blocks.values() for _score, brid in values[:3]]
    block_ids: dict[int, str] = {}
    for offset in range(0, len(block_rids), 900):
        chunk = block_rids[offset:offset + 900]
        if not chunk:
            continue
        sql = "SELECT rid,block_id FROM blk WHERE rid IN (%s)" % ",".join("?" * len(chunk))
        block_ids.update((int(rid), str(block_id)) for rid, block_id in ranker.db.execute(sql, chunk))
    return {
        page_ids[page_rid]: [block_ids[brid] for _score, brid in values[:3] if block_ids.get(brid)]
        for page_rid, values in page_blocks.items()
        if page_rid in page_ids
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reuse-pages", action="store_true")
    parser.add_argument("--suffix", default="", help="두 번째 결정성 실행 파일명 suffix")
    args = parser.parse_args()
    if args.suffix and not re.fullmatch(r"[0-9A-Za-z_-]+", args.suffix):
        raise SystemExit("--suffix는 영문/숫자/_/-만 허용")
    if not (INDEX / ".qmd/index.sqlite").is_file():
        raise SystemExit(f"project-local qmd 색인이 없다: {INDEX}")
    if not (STRUCTURAL_INDEX / "structural2.db").is_file():
        raise SystemExit(f"structural2 색인이 없다: {STRUCTURAL_INDEX}")
    RESULTS.mkdir(parents=True, exist_ok=True)

    query_payload = json.loads(QUERIES.read_text(encoding="utf-8"))
    queries = query_payload["queries"]
    page_map = json.loads((INDEX / "page-map.json").read_text(encoding="utf-8"))
    block_map = json.loads((INDEX / "block-map.json").read_text(encoding="utf-8"))
    ranker = Structural2Ranker.load(CORPUS, STRUCTURAL_INDEX)
    suffix = f"-{args.suffix}" if args.suffix else ""
    page_result_path = RESULTS / f"page-results{suffix}.json"

    resident = ResidentQmd()
    try:
        if args.reuse_pages and page_result_path.is_file():
            page_payload = json.loads(page_result_path.read_text(encoding="utf-8"))
            rows = page_payload["per_query"]
            page_warmup = float(page_payload["latency_ms"]["warmup"])
        else:
            _warm_rows, page_warmup = resident.vector(
                queries[0]["text"], PAGE_COLLECTION, 10
            )
            rows = []
            for index, query in enumerate(queries, 1):
                vector_raw, latency_ms = resident.vector(query["text"], PAGE_COLLECTION, 10)
                backend_score, score_probe_ms = raw_vector_top_score(query["text"])
                vector = [
                    page_map[name]
                    for row in vector_raw
                    if (name := result_filename(row.get("file"))) in page_map
                ]
                vector_scores = [
                    float(row.get("score") or 0.0)
                    for row in vector_raw
                    if result_filename(row.get("file")) in page_map
                ]
                lexical_hits = ranker.search(query["text"], k=10)
                lexical = [hit.page_id for hit in lexical_hits]
                signals = structural_extra(ranker, query["text"])
                fused = rrf(lexical, vector)
                oracle5 = list(dict.fromkeys([*lexical[:5], *vector[:5]]))
                oracle10 = list(dict.fromkeys([*lexical[:10], *vector[:10]]))
                # oracle는 순위가 아니라 집합 상한이다. @5/@10 계산을 한 key로 하기
                # 위해 앞 5/10 슬롯에 해당 합집합을 각각 둔다.
                oracle = oracle5 + [value for value in oracle10 if value not in oracle5]
                block_candidates = structural_block_lookup(ranker, query["text"])
                row = {
                    "id": query["id"],
                    "type": query["type"],
                    "hardness": query.get("hardness"),
                    "expects_injection": bool(query["expects_injection"]),
                    "gold_pages": query.get("gold_pages") or [],
                    "gold_blocks": query.get("gold_blocks") or [],
                    "lexical": lexical,
                    "vector": vector,
                    "vector_scores": vector_scores,
                    "vector_top_score": vector_scores[0] if vector_scores else 0.0,
                    "vector_backend_top_score": backend_score,
                    "score_probe_latency_ms": score_probe_ms,
                    "rrf": fused,
                    "conditional": lexical,
                    "oracle": oracle,
                    "oracle_top5_set": oracle5,
                    "oracle_top10_set": oracle10,
                    "weak_lexical": False,
                    "weak_signal": {
                        "top1_best": signals["s_top1_best"],
                        "content_raw": signals["s_content_raw"],
                    },
                    "lexical_blocks_in_vector_pages": {
                        page_id: block_candidates.get(page_id, []) for page_id in vector
                    },
                    "latency_ms": latency_ms,
                }
                rows.append(row)
                if index % 10 == 0 or index == len(queries):
                    print(f"[page] {index}/{len(queries)}", flush=True)
            page_payload = {
                "schema_version": "1.0",
                "protocol": "qmd mcp resident; typed vec search; rerank=false",
                "latency_ms": {
                    "warmup": page_warmup,
                    "resident": distribution(row["latency_ms"] for row in rows),
                },
                "per_query": rows,
            }
            page_result_path.write_text(
                json.dumps(page_payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

        calibration = choose_conditional_thresholds(rows)
        best_threshold = float(calibration["best_threshold"])
        raw_threshold = float(calibration["raw_threshold"])
        for row in rows:
            row["weak_lexical"] = weak_at(row, best_threshold, raw_threshold)
            row["conditional"] = row["rrf"] if row["weak_lexical"] else row["lexical"]
        calibration["odd_train"] = conditional_split_metrics(
            rows, best_threshold, raw_threshold, 1
        )
        calibration["even_test"] = conditional_split_metrics(
            rows, best_threshold, raw_threshold, 0
        )
        # page checkpoint에도 최종 홀짝 보정 조건을 보존한다.
        page_payload = {
            "schema_version": "1.0",
            "protocol": "qmd mcp resident; typed vec search; rerank=false",
            "conditional_calibration": calibration,
            "latency_ms": {
                "warmup": page_warmup,
                "resident": distribution(row["latency_ms"] for row in rows),
            },
            "per_query": rows,
        }
        page_result_path.write_text(
            json.dumps(page_payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        answerable = [row for row in rows if row["expects_injection"]]
        _warm_rows, block_warmup = resident.vector(
            next(query["text"] for query in queries if query["expects_injection"]),
            BLOCK_COLLECTION,
            10,
        )
        query_by_id = {query["id"]: query for query in queries}
        for index, row in enumerate(answerable, 1):
            vector_raw, latency_ms = resident.vector(
                query_by_id[row["id"]]["text"], BLOCK_COLLECTION, 10
            )
            block_values = []
            block_pages = []
            block_scores = []
            for raw in vector_raw:
                name = result_filename(raw.get("file"))
                mapped = block_map.get(name)
                if not mapped:
                    continue
                block_values.append(mapped["block_id"])
                block_pages.append(mapped["page_id"])
                block_scores.append(float(raw.get("score") or 0.0))
            row["vector_blocks"] = block_values
            row["vector_block_pages"] = block_pages
            row["vector_block_scores"] = block_scores
            row["block_latency_ms"] = latency_ms
            if index % 10 == 0 or index == len(answerable):
                print(f"[block] {index}/{len(answerable)}", flush=True)
    finally:
        resident.close()

    # Oracle 집합 상한은 순서화하지 않고 직접 다시 센다.
    table = metric_table(rows)
    for query_type in (*TYPES, "all"):
        chosen = [
            row for row in rows
            if row["expects_injection"] and (query_type == "all" or row["type"] == query_type)
        ]
        for k in (5, 10):
            table[query_type]["oracle"][f"recall@{k}"] = round(
                sum(bool(set(row[f"oracle_top{k}_set"]) & set(row["gold_pages"])) for row in chosen)
                / len(chosen),
                4,
            )

    weak_rows = [row for row in rows if row["weak_lexical"]]
    weak_answerable = [row for row in weak_rows if row["expects_injection"]]
    weak_unrelated = [row for row in weak_rows if not row["expects_injection"]]
    resident_latency = distribution(row["latency_ms"] for row in rows)
    called_latency = distribution(row["latency_ms"] for row in weak_rows)
    call_rate = len(weak_rows) / len(rows)
    block_rows = [row for row in rows if row["expects_injection"]]

    def page_block_pipeline(row: dict[str, Any], k: int) -> bool:
        gold_pages = set(row["gold_pages"])
        gold_blocks = set(row["gold_blocks"])
        return any(
            page_id in gold_pages
            and bool(set(row["lexical_blocks_in_vector_pages"].get(page_id, [])) & gold_blocks)
            for page_id in row["vector"][:k]
        )

    page_found_lex_failed = [
        row for row in block_rows
        if recall_at(row["vector"], set(row["gold_pages"]), 5)
        and not page_block_pipeline(row, 5)
    ]
    signal_groups = {
        "answerable": [row for row in rows if row["expects_injection"]],
        "unrelated": [row for row in rows if not row["expects_injection"]],
        "hard_negative": [
            row for row in rows if not row["expects_injection"] and row.get("hardness") == "hard"
        ],
        "off_topic": [
            row for row in rows if not row["expects_injection"] and row.get("hardness") != "hard"
        ],
    }
    build = json.loads((INDEX / "build.json").read_text(encoding="utf-8"))
    payload = {
        "schema_version": "1.0",
        "dataset": {
            "queries": len(rows),
            "answerable": len(block_rows),
            "unrelated": len(rows) - len(block_rows),
            "by_type": dict(Counter(row["type"] for row in rows)),
        },
        "build": build,
        "page_retrieval": table,
        "conditional": {
            "definition": (
                f"vector 호출 iff s_top1_best < {best_threshold:.9g} OR "
                f"s_content_raw < {raw_threshold:.9g}"
            ),
            "source": "리뷰 §3 absolute signal의 하위 구간; 경계는 홀수 id에서 선택",
            "calibration": calibration,
            "calls": len(weak_rows),
            "call_rate": round(call_rate, 4),
            "answerable_call_rate": round(len(weak_answerable) / len(block_rows), 4),
            "unrelated_call_rate": round(
                len(weak_unrelated) / max(1, len(rows) - len(block_rows)), 4
            ),
            "by_type_call_rate": {
                query_type: round(
                    sum(row["weak_lexical"] for row in rows if row["type"] == query_type)
                    / max(1, sum(row["type"] == query_type for row in rows)), 4
                )
                for query_type in TYPES
            },
            "called_latency_ms": called_latency,
            "expected_latency_ms_resident_p50": round(
                call_rate * float(resident_latency["p50"] or 0.0), 4
            ),
            "expected_latency_ms_called_mean": round(
                call_rate * float(called_latency["mean"] or 0.0), 4
            ),
        },
        "latency_ms": {
            "page_warmup": page_warmup,
            "page_resident": resident_latency,
            "block_warmup": block_warmup,
            "block_resident": distribution(row["block_latency_ms"] for row in block_rows),
        },
        "no_injection_signal": {
            "score_source": (
                "qmd typed-vec --no-rerank --explain의 1위 vectorScores[0]; "
                "MCP 공개 score는 1/rank 정규화라 무주입 신호로 쓸 수 없음"
            ),
            "score_probe_latency_ms": distribution(row["score_probe_latency_ms"] for row in rows),
            "auc": auc(rows),
            "distributions": {
                name: distribution(row["vector_backend_top_score"] for row in group)
                for name, group in signal_groups.items()
            },
            "gate_at_fpr_5pct": best_score_gate(rows),
        },
        "block_retrieval": {
            "lexical_blocks_per_page": 3,
            "vector_page_recall@5": round(
                sum(recall_at(row["vector"], set(row["gold_pages"]), 5) for row in block_rows)
                / len(block_rows), 4
            ),
            "vector_page_then_lexical_block@5": round(
                sum(page_block_pipeline(row, 5) for row in block_rows) / len(block_rows), 4
            ),
            "lexical_block_success_given_vector_page@5": round(
                sum(page_block_pipeline(row, 5) for row in block_rows)
                / max(1, sum(recall_at(row["vector"], set(row["gold_pages"]), 5) for row in block_rows)),
                4,
            ),
            "vector_block_recall@5": round(
                sum(recall_at(row["vector_blocks"], set(row["gold_blocks"]), 5) for row in block_rows)
                / len(block_rows), 4
            ),
            "vector_block_recall@10": round(
                sum(recall_at(row["vector_blocks"], set(row["gold_blocks"]), 10) for row in block_rows)
                / len(block_rows), 4
            ),
            "expected_latency_ms_at_conditional_call_rate": round(
                call_rate
                * float(distribution(row["block_latency_ms"] for row in block_rows)["p50"] or 0.0),
                4,
            ),
            "page_found_but_lexical_block_failed_n": len(page_found_lex_failed),
            "block_vector_recovery@5_on_failures": round(
                sum(recall_at(row["vector_blocks"], set(row["gold_blocks"]), 5) for row in page_found_lex_failed)
                / max(1, len(page_found_lex_failed)), 4
            ),
            "by_type": {
                query_type: {
                    "page_vector@5": round(
                        sum(recall_at(row["vector"], set(row["gold_pages"]), 5) for row in block_rows if row["type"] == query_type)
                        / max(1, sum(row["type"] == query_type for row in block_rows)), 4
                    ),
                    "page_then_lexical_block@5": round(
                        sum(page_block_pipeline(row, 5) for row in block_rows if row["type"] == query_type)
                        / max(1, sum(row["type"] == query_type for row in block_rows)), 4
                    ),
                    "block_vector@5": round(
                        sum(recall_at(row["vector_blocks"], set(row["gold_blocks"]), 5) for row in block_rows if row["type"] == query_type)
                        / max(1, sum(row["type"] == query_type for row in block_rows)), 4
                    ),
                }
                for query_type in TYPES
            },
        },
        "per_query": rows,
    }
    (RESULTS / f"results{suffix}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({
        "page_retrieval": table,
        "conditional": payload["conditional"],
        "latency_ms": payload["latency_ms"],
        "no_injection_signal": payload["no_injection_signal"],
        "block_retrieval": payload["block_retrieval"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

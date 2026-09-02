"""증분 build 벤치의 공용 부품 — 임시 root 준비, 질문 집합, 검색 서명, 산출물 지문.

제품 코드(`scripts/llmwiki.py build --changed`, `scripts/llmwiki_index.py`) 만 부른다. 정본·bench/frozen 은
읽기만 하고, 임시 root 는 호출자가 준 디렉터리 아래에만 만든다. 표준 라이브러리만.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import statistics
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import llmwiki  # noqa: E402
import llmwiki_index as IDX  # noqa: E402

FROZEN_CORPUS = ROOT / "bench" / "frozen" / "corpus"
FROZEN_QUERIES = ROOT / "bench" / "frozen" / "queries.json"


def median(values: list[float]) -> float:
    return round(statistics.median(values), 1) if values else 0.0


def pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    return round(s[min(len(s) - 1, int(round(q / 100 * (len(s) - 1))))], 3)


def prepare_root(dest: Path, wiki_src: Path, *, allow_aliases: bool = False) -> Path:
    """wiki_src(정본 디렉터리) 를 dest/wiki 로 복사하고 tools/config·schema 를 붙인 임시 root."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    shutil.copytree(wiki_src, dest / "wiki")
    shutil.copytree(ROOT / "tools" / "config", dest / "tools" / "config")
    shutil.copytree(ROOT / "tools" / "schema", dest / "tools" / "schema")
    if allow_aliases:                       # frozen 코퍼스의 제안 필드 — 저장소 schema 는 바꾸지 않는다
        schema_path = dest / "tools" / "schema" / "page.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        schema["properties"]["aliases"] = {}
        schema_path.write_text(json.dumps(schema, ensure_ascii=False, indent=2), encoding="utf-8")
    return dest


def clone_root(src: Path, dest: Path) -> Path:
    """base root(정본 + 산출물) 의 사본 — 각 시나리오가 같은 출발점에서 시작한다.

    macOS 에서 Finder/Spotlight 가 지우는 중인 디렉터리에 .DS_Store 를 만들어 rmtree 가 "not empty" 로
    실패할 수 있어 몇 번 다시 시도한다."""
    for attempt in range(5):
        if not dest.exists():
            break
        try:
            shutil.rmtree(dest)
        except OSError:
            time.sleep(0.2 * (attempt + 1))
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    shutil.copytree(src, dest, symlinks=False, ignore=shutil.ignore_patterns(".DS_Store"), dirs_exist_ok=True)
    return dest


def load_pages(root: Path) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """{page_id: page}, {page_id: root 상대 경로}."""
    pages: dict[str, dict[str, Any]] = {}
    sources: dict[str, str] = {}
    for path in sorted((root / "wiki").rglob("*.json")):
        if path.name.startswith(".") or path.name == "log.json":
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        for page in value if isinstance(value, list) else [value]:
            if isinstance(page, dict) and page.get("id") and isinstance(page.get("blocks"), dict):
                pages[str(page["id"])] = page
                sources[str(page["id"])] = path.relative_to(root).as_posix()
    return pages, sources


def write_page(root: Path, rel: str, page: dict[str, Any]) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(page, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def frozen_queries() -> list[dict[str, Any]]:
    return json.loads(FROZEN_QUERIES.read_text(encoding="utf-8"))["queries"]


def derived_queries(pages: dict[str, dict[str, Any]], n: int = 200) -> list[dict[str, Any]]:
    """질문 집합이 없는 위키(개인 위키) 용: 제목과 본문 첫 문장에서 결정적으로 뽑는다."""
    out: list[dict[str, Any]] = []
    for pid in sorted(pages):
        page = pages[pid]
        title = str(page.get("title") or "")
        body = ""
        for bid in page.get("block_order") or []:
            b = page["blocks"].get(bid) or {}
            if b.get("kind") in ("heading", "thematic_break"):
                continue
            body = IDX.block_text(b)
            if body.strip():
                break
        words = " ".join(body.split()[:8])
        if title:
            out.append({"id": f"t:{pid}", "type": "title", "text": title})
        if words:
            out.append({"id": f"b:{pid}", "type": "body", "text": words})
        if len(out) >= n:
            break
    return out[:n]


def signatures(root: Path, queries: list[dict[str, Any]], k: int = 10) -> list[list[tuple]]:
    idx = IDX.open_ro(root / "index" / "search.sqlite")
    try:
        return [[(h.page_id, h.score, tuple(h.block_ids)) for h in idx.search(q["text"], k=k).hits]
                for q in queries]
    finally:
        idx.close()


def query_times(root: Path, queries: list[dict[str, Any]], repeat: int = 1) -> dict[str, float]:
    """warm 조회 시간(ms) p50/p95 와, 프로세스가 새로 열 때의 첫 조회(open 포함)."""
    t0 = time.perf_counter()
    idx = IDX.open_ro(root / "index" / "search.sqlite")
    idx.search(queries[0]["text"])
    first = (time.perf_counter() - t0) * 1000
    times: list[float] = []
    try:
        for _ in range(repeat):
            for q in queries:
                t = time.perf_counter()
                idx.search(q["text"])
                times.append((time.perf_counter() - t) * 1000)
    finally:
        idx.close()
    return {"p50": pct(times, 50), "p95": pct(times, 95), "open_and_first_ms": round(first, 2)}


def artifact_prints(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for base in (root / "index", root / "viewer" / "public" / "data"):
        if not base.exists():
            continue
        for p in sorted(base.rglob("*")):
            if (p.is_file() and not p.name.startswith("search.work.") and p.name != ".DS_Store"
                    and not p.name.endswith(("-wal", "-shm"))):
                out[p.relative_to(root).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def db_stats(path: Path) -> dict[str, Any]:
    import sqlite3
    if not path.is_file():
        return {}
    db = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
    try:
        return {"bytes": path.stat().st_size,
                "freelist": db.execute("PRAGMA freelist_count").fetchone()[0],
                "page_count": db.execute("PRAGMA page_count").fetchone()[0]}
    finally:
        db.close()


def diff_signatures(a: list[list[tuple]], b: list[list[tuple]], queries: list[dict[str, Any]]) -> dict[str, Any]:
    bad = [i for i, (x, y) in enumerate(zip(a, b)) if x != y]
    page_only = [i for i in bad if [h[0] for h in a[i]] != [h[0] for h in b[i]]]
    return {"mismatch": len(bad), "page_order_mismatch": len(page_only),
            "examples": [{"id": queries[i]["id"], "left": a[i][:3], "right": b[i][:3]} for i in bad[:3]]}


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

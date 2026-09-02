#!/usr/bin/env python3
"""page 단위 SQLite 세그먼트로 structural2 의미를 증분 유지한다.

structural2의 impact 정렬 BLOB은 전역 DF/평균 block 길이를 구워 넣기 때문에
작은 변경도 많은 posting을 다시 써야 한다. 이 프로토타입은 block별 TF를 page
세그먼트로 저장하고 impact를 조회 때 계산한다. map.json의 page sha256 델타만
읽어 변경 page의 row를 교체하며, 그래프/대체 chain은 load 때 결정적으로 만든다.
"""
from __future__ import annotations

import hashlib
import json
import math
import shutil
import sqlite3
import struct
import time
from pathlib import Path
from typing import Any, Iterable

from bench.rankers.base import BuildStats, Hit, block_text, dir_bytes
from bench.rankers.structural2 import (
    AGG,
    BM25_B,
    BM25_K1,
    CAND_PAGES,
    EDGE_W,
    FRONTIER,
    HUB_DAMP,
    IDF_POW,
    LEX_TAIL_W,
    MAX_DF_FRAC,
    MAX_EVIDENCE,
    MAX_FANOUT,
    MAX_POST,
    MAX_QUERY_TERMS,
    MIN_SEED,
    SEEDS,
    STALE_SHOW,
    STEPS,
    STEP_DECAY,
    TIE,
    TIE_EPS,
    W_ANCHOR,
    W_CONFLICT,
    W_CURRENT,
    W_GRAPH,
    _norm_text,
    tokenize,
)


SCHEMA = """
PRAGMA journal_mode=DELETE;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=OFF;
CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT) WITHOUT ROWID;
CREATE TABLE page(
  page_id TEXT PRIMARY KEY, source TEXT NOT NULL, sha256 TEXT NOT NULL,
  slug TEXT NOT NULL, title TEXT NOT NULL, projects TEXT NOT NULL,
  tags TEXT NOT NULL, history_at TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE block(
  block_key TEXT PRIMARY KEY, page_id TEXT NOT NULL, block_id TEXT NOT NULL,
  seq INTEGER NOT NULL, length INTEGER NOT NULL, kind TEXT NOT NULL,
  unresolved INTEGER NOT NULL, norm_text TEXT NOT NULL
) WITHOUT ROWID;
CREATE INDEX block_page ON block(page_id);
CREATE TABLE post(
  term TEXT NOT NULL, block_key TEXT NOT NULL, page_id TEXT NOT NULL,
  tf INTEGER NOT NULL, PRIMARY KEY(term, block_key)
) WITHOUT ROWID;
CREATE INDEX post_page ON post(page_id);
CREATE TABLE termstat(term TEXT PRIMARY KEY, df INTEGER NOT NULL) WITHOUT ROWID;
CREATE TABLE edge(
  src TEXT NOT NULL, ord INTEGER NOT NULL, target TEXT NOT NULL,
  kind TEXT NOT NULL, block_id TEXT NOT NULL,
  PRIMARY KEY(src, ord)
) WITHOUT ROWID;
"""


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def map_root(payload: dict[str, Any]) -> str:
    return sha_text(canonical(payload))


def _page_values(path: Path) -> Iterable[dict[str, Any]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    for page in value if isinstance(value, list) else [value]:
        if isinstance(page, dict) and page.get("id") and page.get("blocks"):
            yield page


def make_map(corpus_dir: Path) -> dict[str, Any]:
    """프로덕션 map.json과 같은 page sha256 핵심만 결정적으로 만든다."""
    root = Path(corpus_dir)
    pages: dict[str, dict[str, str]] = {}
    for path in sorted(root.rglob("*.json")):
        if path.name.startswith("."):
            continue
        for page in _page_values(path):
            pid = str(page["id"])
            if pid in pages:
                raise ValueError(f"duplicate page id: {pid}")
            pages[pid] = {
                "source": path.relative_to(root).as_posix(),
                "pointer": "",
                "sha256": sha_text(canonical(page)),
            }
    return {"schema_version": "1.0", "pages": dict(sorted(pages.items()))}


def write_map(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _source_path(corpus_dir: Path, source: str) -> Path:
    candidate = Path(source)
    return candidate if candidate.is_absolute() else Path(corpus_dir) / candidate


def _float32(value: float) -> float:
    """structural2 BLOB의 array('f') 반올림을 재현한다."""
    return struct.unpack("=f", struct.pack("=f", value))[0]


class SegmentedRanker:
    """정규화 page 세그먼트와 query-time impact를 쓰는 structural2 변형."""

    name = "segmented"

    def __init__(self, db: sqlite3.Connection, opts: dict[str, Any]):
        self.db = db
        self.graph = str(opts.get("graph", "ppr"))
        self.fold = bool(opts.get("fold", True))
        self.steps = int(opts.get("steps", STEPS))
        self.idf_pow = float(opts.get("idf_pow", IDF_POW))
        self.agg = str(opts.get("agg", AGG))
        self.tie = str(opts.get("tie", TIE))
        self.min_seed = float(opts.get("min_seed", MIN_SEED))
        self.tie_eps = float(opts.get("tie_eps", TIE_EPS))
        self.edge_w = dict(EDGE_W)
        if "wiki_w" in opts:
            self.edge_w["wiki"] = float(opts["wiki_w"])
        self.hub = bool(opts.get("hub", HUB_DAMP))
        meta = dict(db.execute("SELECT k,v FROM meta"))
        self.nblocks = int(meta.get("n_blocks") or 1)
        self.total_len = int(meta.get("total_len") or 0)
        self.avg_len = self.total_len / self.nblocks if self.nblocks else 1.0
        self._load_structure()

    @classmethod
    def build(cls, corpus_dir: Path, index_dir: Path, **opts: Any) -> BuildStats:
        t0 = time.perf_counter()
        target = Path(index_dir)
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True, exist_ok=True)
        payload = make_map(Path(corpus_dir))
        mp = target / "map.json"
        write_map(mp, payload)
        dbp = target / "segments.sqlite"
        db = sqlite3.connect(dbp)
        db.executescript(SCHEMA)
        db.executemany("INSERT INTO meta VALUES(?,?)", [
            ("n_pages", "0"), ("n_blocks", "0"), ("total_len", "0"),
            ("map_root", ""),
        ])
        db.commit()
        db.close()
        delta = cls.incremental_update(Path(corpus_dir), target, mp)
        return BuildStats(
            elapsed_ms=round((time.perf_counter() - t0) * 1000.0, 3),
            index_bytes=dir_bytes(target),
            notes={"layout": "page segments + query-time BM25 impact", **delta},
        )

    @classmethod
    def load(cls, corpus_dir: Path, index_dir: Path, **opts: Any) -> "SegmentedRanker":
        dbp = Path(index_dir) / "segments.sqlite"
        if not dbp.exists():
            raise FileNotFoundError(f"색인이 없다: {dbp}")
        db = sqlite3.connect(f"file:{dbp}?mode=ro", uri=True, check_same_thread=False)
        return cls(db, opts)

    @staticmethod
    def _delete_page(db: sqlite3.Connection, page_id: str) -> None:
        counts = db.execute(
            "SELECT term,count(*) FROM post WHERE page_id=? GROUP BY term", (page_id,)
        ).fetchall()
        for term, count in counts:
            db.execute("UPDATE termstat SET df=df-? WHERE term=?", (count, term))
        db.execute("DELETE FROM termstat WHERE df<=0")
        db.execute("DELETE FROM post WHERE page_id=?", (page_id,))
        db.execute("DELETE FROM block WHERE page_id=?", (page_id,))
        db.execute("DELETE FROM edge WHERE src=?", (page_id,))
        db.execute("DELETE FROM page WHERE page_id=?", (page_id,))

    @staticmethod
    def _insert_page(
        db: sqlite3.Connection,
        page: dict[str, Any],
        source: str,
        digest: str,
    ) -> None:
        pid = str(page["id"])
        history_at = max(
            (str(h.get("at") or "") for h in page.get("history") or [] if isinstance(h, dict)),
            default=str(page.get("updated") or ""),
        )
        db.execute(
            "INSERT INTO page VALUES(?,?,?,?,?,?,?,?)",
            (
                pid,
                source,
                digest,
                str(page.get("slug") or ""),
                str(page.get("title") or ""),
                canonical(page.get("projects") or []),
                canonical(page.get("tags") or []),
                history_at,
            ),
        )
        blocks: list[tuple[str, str, str, int, int, str, int, str]] = []
        posts: list[tuple[str, str, str, int]] = []
        term_counts: dict[str, int] = {}

        def add_block(seq: int, bid: str, text: str, kind: str, unresolved: int) -> None:
            tokens = tokenize(text)
            if not tokens:
                return
            key = f"{pid}\x1f{seq:06d}"
            blocks.append((key, pid, bid, seq, len(tokens), kind, unresolved, _norm_text(text)))
            counts: dict[str, int] = {}
            for term in tokens:
                counts[term] = counts.get(term, 0) + 1
            for term, tf in sorted(counts.items()):
                posts.append((term, key, pid, tf))
                term_counts[term] = term_counts.get(term, 0) + 1

        title = str(page.get("title") or "")
        if title:
            add_block(0, "", title, "title", 0)
        source_blocks = page.get("blocks") or {}
        order = page.get("block_order") or list(source_blocks)
        for seq, raw_bid in enumerate(order, 1):
            block = source_blocks.get(raw_bid)
            if not isinstance(block, dict):
                continue
            text = block_text(block)
            if not text:
                continue
            bid = str(block.get("id") or raw_bid)
            unresolved = int(
                block.get("kind") == "conflict"
                and (block.get("resolution") or {}).get("status") != "resolved"
            )
            add_block(seq, bid, text, str(block.get("kind") or ""), unresolved)
        db.executemany("INSERT INTO block VALUES(?,?,?,?,?,?,?,?)", blocks)
        db.executemany("INSERT INTO post VALUES(?,?,?,?)", posts)
        for term, count in sorted(term_counts.items()):
            db.execute(
                "INSERT INTO termstat VALUES(?,?) "
                "ON CONFLICT(term) DO UPDATE SET df=df+excluded.df",
                (term, count),
            )
        edge_rows = []
        for ord_, link in enumerate(page.get("links") or []):
            if not isinstance(link, dict):
                continue
            edge_rows.append(
                (
                    pid,
                    ord_,
                    str(link.get("target") or ""),
                    str(link.get("kind") or "wiki"),
                    str(link.get("block_id") or ""),
                )
            )
        db.executemany("INSERT INTO edge VALUES(?,?,?,?,?)", edge_rows)

    @classmethod
    def incremental_update(
        cls,
        corpus_dir: Path,
        index_dir: Path,
        map_path: Path,
    ) -> dict[str, Any]:
        """map page sha 델타로 delete/replace/insert한 시간과 건수를 돌려준다."""
        t0 = time.perf_counter()
        payload = json.loads(Path(map_path).read_text(encoding="utf-8"))
        fresh = payload.get("pages") or {}
        dbp = Path(index_dir) / "segments.sqlite"
        db = sqlite3.connect(dbp)
        old = {
            pid: {"source": source, "sha256": digest}
            for pid, source, digest in db.execute("SELECT page_id,source,sha256 FROM page")
        }
        old_ids, new_ids = set(old), set(fresh)
        added = sorted(new_ids - old_ids)
        deleted = sorted(old_ids - new_ids)
        modified = sorted(
            pid
            for pid in old_ids & new_ids
            if old[pid]["sha256"] != fresh[pid].get("sha256")
            or old[pid]["source"] != fresh[pid].get("source")
        )
        db.execute("BEGIN IMMEDIATE")
        for pid in deleted + modified:
            cls._delete_page(db, pid)
        for pid in added + modified:
            entry = fresh[pid]
            path = _source_path(Path(corpus_dir), str(entry["source"]))
            matches = [p for p in _page_values(path) if str(p["id"]) == pid]
            if len(matches) != 1:
                raise ValueError(f"{pid}: source에서 정확히 한 page를 찾지 못함: {path}")
            page = matches[0]
            digest = sha_text(canonical(page))
            if digest != entry.get("sha256"):
                raise ValueError(f"{pid}: map sha256 불일치")
            cls._insert_page(db, page, str(entry["source"]), digest)
        nblocks, total_len = db.execute(
            "SELECT count(*),coalesce(sum(length),0) FROM block"
        ).fetchone()
        n_pages = db.execute("SELECT count(*) FROM page").fetchone()[0]
        root = map_root(payload)
        db.executemany(
            "INSERT INTO meta VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            [
                ("n_pages", str(n_pages)),
                ("n_blocks", str(nblocks)),
                ("total_len", str(total_len)),
                ("map_root", root),
            ],
        )
        db.commit()
        db.close()
        write_map(Path(index_dir) / "map.json", payload)
        return {
            "elapsed_ms": round((time.perf_counter() - t0) * 1000.0, 3),
            "added": len(added),
            "modified": len(modified),
            "deleted": len(deleted),
            "changed": len(added) + len(modified) + len(deleted),
            "map_root": root,
        }

    def _load_structure(self) -> None:
        pages = list(
            self.db.execute(
                "SELECT page_id,slug,title,projects,tags,history_at FROM page ORDER BY page_id"
            )
        )
        self.page_ids = [row[0] for row in pages]
        self.page_set = set(self.page_ids)
        self.page_meta = {
            row[0]: {
                "projects": json.loads(row[3]),
                "tags": json.loads(row[4]),
                "history_at": row[5],
            }
            for row in pages
        }
        lookup: dict[str, str] = {}
        for pid, slug, title, _projects, _tags, _history in pages:
            for key in (pid, slug, title):
                if key:
                    lookup.setdefault(key, pid)
        edges: list[tuple[str, str, str, str]] = []
        for src, _ord, raw, kind, bid in self.db.execute(
            "SELECT src,ord,target,kind,block_id FROM edge ORDER BY src,ord"
        ):
            dst = lookup.get(raw) or lookup.get(raw[5:] if raw.startswith("page:") else "page:" + raw)
            if not dst or dst == src:
                continue
            edges.append((src, dst, kind, bid))

        self.anchor_blocks: set[tuple[str, str]] = set()
        for src, _dst, kind, bid in edges:
            if kind in ("related", "supersedes"):
                self.anchor_blocks.add((src, bid))

        succ: dict[str, str] = {}
        sup_block: dict[str, str] = {}
        for src, dst, kind, bid in edges:
            if kind == "supersedes":
                succ.setdefault(dst, src)
                sup_block.setdefault(src, bid)
        self.head: dict[str, str] = {}
        for start in succ:
            cur, guard = start, 0
            while cur in succ and guard < 32:
                cur, guard = succ[cur], guard + 1
            self.head[start] = cur
        self.sup_block = sup_block

        own: dict[tuple[str, str], str] = {}
        for src, dst, _kind, bid in edges:
            own.setdefault((src, dst), bid)
        adj: dict[tuple[str, str], float] = {}
        weak_in: dict[str, int] = {}
        for src, dst, kind, _bid in edges:
            if kind == "supersedes":
                continue
            weight = self.edge_w.get(kind, self.edge_w["wiki"])
            for a, b in ((src, dst), (dst, src)):
                adj[(a, b)] = max(adj.get((a, b), 0.0), weight)
                if kind != "related":
                    weak_in[b] = weak_in.get(b, 0) + 1
        if self.hub:
            adj = {
                (a, b): (w if w >= 1.0 else w / (1.0 + math.log(weak_in.get(b, 1))))
                for (a, b), w in adj.items()
            }
        by_src: dict[str, list[tuple[float, str]]] = {}
        for (a, b), weight in adj.items():
            by_src.setdefault(a, []).append((-round(weight, 6), b))
        self.adj: dict[str, list[tuple[str, float, str]]] = {}
        for src in sorted(by_src):
            self.adj[src] = [
                (dst, -neg_weight, own.get((dst, src), ""))
                for neg_weight, dst in sorted(by_src[src])[:MAX_FANOUT]
            ]

    def _lex(
        self, query: str
    ) -> tuple[dict[str, float], dict[str, list[tuple[float, str, str]]]]:
        terms = sorted(set(tokenize(query)))
        if not terms:
            return {}, {}
        marks = ",".join("?" * len(terms))
        rows = self.db.execute(
            f"SELECT term,df FROM termstat WHERE term IN ({marks})", terms
        ).fetchall()
        rows.sort(key=lambda row: (row[1], row[0]))
        cap = MAX_DF_FRAC * self.nblocks
        kept = [row for row in rows if row[1] <= cap] or rows[:1]
        block_scores: dict[str, float] = {}
        block_info: dict[str, tuple[str, str]] = {}
        n = self.nblocks
        for term, df in kept[:MAX_QUERY_TERMS]:
            idf = math.log(1.0 + (n - df + 0.5) / (df + 0.5))
            scored: list[tuple[float, str, str, str]] = []
            for key, pid, tf, bid, length, kind, unresolved in self.db.execute(
                "SELECT p.block_key,p.page_id,p.tf,b.block_id,b.length,b.kind,b.unresolved "
                "FROM post p JOIN block b ON b.block_key=p.block_key WHERE p.term=?",
                (term,),
            ):
                mult = 1.0
                if (pid, bid) in self.anchor_blocks:
                    mult *= W_ANCHOR
                if kind == "current":
                    mult *= W_CURRENT
                if kind == "conflict" and unresolved:
                    mult *= W_CONFLICT
                impact = idf * tf * (BM25_K1 + 1.0) / (
                    tf + BM25_K1 * (1.0 - BM25_B + BM25_B * length / self.avg_len)
                )
                impact = _float32(round(impact * mult, 5))
                scored.append((impact, key, pid, bid))
            scored.sort(key=lambda row: (-row[0], row[1]))
            query_weight = idf ** (self.idf_pow - 1.0)
            for impact, key, pid, bid in scored[:MAX_POST]:
                block_scores[key] = block_scores.get(key, 0.0) + impact * query_weight
                block_info[key] = (pid, bid)
        if not block_scores:
            return {}, {}
        page_blocks: dict[str, list[tuple[float, str, str]]] = {}
        for key, score in block_scores.items():
            pid, bid = block_info[key]
            page_blocks.setdefault(pid, []).append((score, key, bid))
        page_lex: dict[str, float] = {}
        for pid, values in page_blocks.items():
            values.sort(reverse=True)
            page_lex[pid] = values[0][0] + LEX_TAIL_W * sum(v[0] for v in values[1:])
        return page_lex, page_blocks

    def _ppr(
        self, page_lex: dict[str, float], top: float
    ) -> tuple[dict[str, float], dict[str, tuple[float, str]]]:
        seeds = sorted(page_lex, key=lambda pid: (-page_lex[pid], pid))[:SEEDS]
        mass = {
            pid: page_lex[pid] / top
            for pid in seeds
            if page_lex[pid] / top >= self.min_seed
        }
        score: dict[str, float] = {}
        via: dict[str, tuple[float, str]] = {}
        self._sender: dict[str, tuple[float, str]] = {}
        use_max = self.agg == "max"
        came: dict[str, set[str]] = {}
        for step in range(self.steps):
            nxt: dict[str, float] = {}
            nxt_from: dict[str, set[str]] = {}
            decay = STEP_DECAY**step
            for src in sorted(mass):
                for dst, weight, own in self.adj.get(src, []):
                    if weight <= 0.0 or dst in came.get(src, ()):
                        continue
                    value = mass[src] * weight
                    nxt[dst] = max(nxt.get(dst, 0.0), value) if use_max else nxt.get(dst, 0.0) + value
                    nxt_from.setdefault(dst, set()).add(src)
                    if step == 0 and weight >= 1.0 and value > self._sender.get(dst, (0.0, ""))[0]:
                        self._sender[dst] = (value, src)
                    if value * decay > via.get(dst, (0.0, ""))[0]:
                        via[dst] = (value * decay, own)
            for dst, value in nxt.items():
                score[dst] = score.get(dst, 0.0) + value * decay
            mass = dict(sorted(nxt.items(), key=lambda item: (-item[1], item[0]))[:FRONTIER])
            came = nxt_from
        return score, via

    def search(self, query: str, k: int = 10) -> list[Hit]:
        page_lex, page_blocks = self._lex(query)
        if not page_lex:
            return []
        top = max(page_lex.values()) or 1.0
        candidates = sorted(page_lex, key=lambda pid: (-page_lex[pid], pid))[:CAND_PAGES]
        score = {pid: page_lex[pid] / top for pid in candidates}
        graph: dict[str, float] = {}
        via: dict[str, tuple[float, str]] = {}
        if self.graph == "ppr":
            graph, via = self._ppr(page_lex, top)
            for pid, value in graph.items():
                score[pid] = score.get(pid, 0.0) + W_GRAPH * value
        if self.tie == "receiver" and graph:
            base = dict(score)
            for receiver, (_mass, sender) in sorted(self._sender.items()):
                if (
                    receiver in page_lex
                    and sender in base
                    and receiver in base
                    and base[receiver] < base[sender] <= base[receiver] * (1.0 + self.tie_eps)
                ):
                    score[receiver] = max(score[receiver], base[sender] + 1e-4)

        lifted: set[str] = set()
        if self.fold:
            original = sorted(score)
            for pid in original:
                head = self.head.get(pid, pid)
                if head != pid and (head not in score or score[pid] > score[head]):
                    score[head] = score[pid]
                    lifted.add(head)
            for pid in original:
                head = self.head.get(pid, pid)
                if head != pid:
                    score[pid] = score[head] * STALE_SHOW

        final = sorted(score, key=lambda pid: (-score[pid], pid))[:k]
        hits: list[Hit] = []
        for pid in final:
            evidence: list[str] = []
            if pid in lifted and self.sup_block.get(pid):
                evidence.append(self.sup_block[pid])
            if pid in via and via[pid][1] and via[pid][1] not in evidence:
                evidence.append(via[pid][1])
            for _value, _key, bid in page_blocks.get(pid, [])[:MAX_EVIDENCE]:
                if bid and bid not in evidence:
                    evidence.append(bid)
            hits.append(Hit(pid, round(score[pid], 6), evidence[: MAX_EVIDENCE + 1]))
        return hits


RANKER = SegmentedRanker

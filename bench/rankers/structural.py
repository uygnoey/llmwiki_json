#!/usr/bin/env python3
"""구조 랭커 — JSON 정본의 구조(대체 관계·alias·block 인접)를 점수에 쓴다.

색인 3층 (모두 `<index_dir>/structural.db` 한 파일 안):

1. 어휘층  FTS5 `tokenize='trigram'` + BM25.
   한국어 조사/어미 때문에 unicode61 은 못 쓴다. 대신 색인 텍스트의 모든 낱말
   앞에 표식 문자(MARK)를 붙여 넣는다. 그러면 3글자짜리 trigram `MARK+2글자`가
   "낱말 시작"에 고정된 prefix 검색이 되어, 질문의 `정책`이 본문의 `정책은`을
   찾는다. 질문 쪽에서는 조사/어미를 떼어 어간으로 맞춘다.
   조회 비용은 질문 토큰 수 × 각 토큰의 posting list 길이다. 코퍼스를 훑지 않는다.

2. 개념층  alias -> page 매핑. page 의 선택 필드 `aliases:{ko:[],en:[]}` 와
   title/tags 에서 만든다. 질문 토큰의 1~3-gram 으로 조회해 개념 page 를 찾고,
   그 개념의 **다른 표면형을 질문에 되먹인다**. crosslingual/paraphrase 는
   이 되먹임이 유일한 경로다(정본 본문에 질문 표면형이 없으므로).

3. 그래프층  block 단위 인접. `links[].block_id` 가 실제 block 을 가리키므로
   간선마다 근거 block 을 들고 있다. supersedes 는 따로 접어 체인 깊이를 만든다.

점수:
    score = BM25_정규화 × supersede_gate + concept_hop + provenance (+ relation)

가중치는 아래 상수 블록에 모아 두었다. bench/queries.json 으로 튜닝하지 않았다
(과적합 방지). 값은 각 층의 신뢰도에 대한 사전 판단이다.
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
import time
import unicodedata
from pathlib import Path
from typing import Any, Iterable

from .base import BuildStats, Hit, block_text, dir_bytes

# --------------------------------------------------------------------- 상수
# 어휘층
MARK = "␟"              # 낱말 시작 표식. 본문에 등장할 일이 없는 문자
MIN_TERM_CHARS = 2           # MARK 포함 3글자 = trigram 1개가 되는 최소 길이
MAX_QUERY_TERMS = 12         # 질문당 FTS 조회 횟수 상한 (비용을 토큰 수에 묶는다)
MAX_BLOCKS_PER_TERM = 400    # 토큰당 가져올 block 상한
MAX_DF_FRAC = 0.30           # 이보다 흔한 토큰은 버린다 (불용어 방지)
# 코퍼스의 0.2% 에서만 보이는 토큰은 사실상 식별자다(버전, 설정키, 고유명).
# BM25 의 idf 는 로그라서 이런 토큰 하나가 흔한 낱말 예닐곱 개에게 밀린다.
# 보정은 어휘층 **안에서** 곱한다. 밖에서 더하면 supersede_gate 를 우회해
# 강등해 둔 낡은 page 가 도로 올라온다.
RARE_DF_FRAC = 0.002
W_RARE_MATCH = 0.60
CAND_PAGES = 200             # 후처리에 넘길 후보 page 수
LEX_TAIL_W = 0.25            # page 점수 = 최고 block + 0.25 × 나머지 block 합

# 개념층
W_ALIAS_EXACT = 1.00         # aliases 필드
W_ALIAS_TITLE = 0.70         # title
W_ALIAS_TAG = 0.25           # tags
MAX_ALIAS_FANOUT = 12        # 이보다 많은 page 를 가리키는 alias key 는 무시
MAX_ALIAS_NGRAM = 3
W_EXPANDED_TERM = 0.90       # alias 로 되먹인 토큰의 어휘 기여 계수
MAX_EXPANDED_TERMS = 6
MAX_EXPAND_DF_FRAC = 0.02    # 흔한 표면형은 되먹이지 않는다

# 그래프층
W_CONCEPT_SELF = 0.20        # alias 로 직접 맞은 개념 page 자신
W_CONCEPT_HOP = 0.25         # 그 개념에서 1-hop. 직접 어휘 근거보다 약해야 한다
MAX_HOP_FANOUT = 40
W_RELATION = 0.30            # 두 질문 토큰을 잇는 간선이 있는 page
MAX_RELATION_SRC = 60
# `related` 는 큐레이터가 "같은 것을 다루는 다른 문서"라고 손으로 적어 둔 간선이다
# (한국어/영어 짝 등). 어휘가 한 글자도 안 겹치는 질문은 이 간선을 건너는 것
# 말고는 길이 없다. `supersedes` 가 "시간축의 같은 문서"라면 `related` 는
# "표현축의 같은 문서"이므로 관련성을 거의 그대로 물려받는다. 다만 대체와 달리
# 원본도 여전히 유효하므로 1.0 미만으로 둬서 직접 맞은 page 가 위에 남게 한다.
W_RELATED_HOP = 0.85
MAX_RELATED_SRC = 15         # 상위 어휘 후보에서만 건넌다
MIN_RELATED_SRC_LEX = 0.50   # 약하게 맞은 page 에서는 건너지 않는다

# 시간축 — 대체된 page 를 지운다(X)가 아니라 강등한다
GATE_SUPERSEDED = 0.30
GATE_CHAIN_DECAY = 0.70      # 체인 깊이가 깊을수록 더 강등
W_SUPERSEDE_FORWARD = 1.00   # 낡은 판이 질문에 맞으면 그 자리를 현행판에 넘긴다
FORWARD_EPS = 0.02           # 정확히 그 자리 '바로 위'에 놓는다

# 근거(provenance)
W_PROV_SOURCES = 0.05        # page.sources 가 비어있지 않다
W_PROV_REFS = 0.03           # 맞은 block 이 refs 를 들고 있다
# 큐레이터가 손으로 건 `related`/`supersedes` 를 달고 있는 block 은, 같은 낱말을
# 그냥 스쳐 언급한 block 보다 강한 주장이다. 어휘 점수만 보면 질문 문장을 그대로
# 베낀 짧은 언급이 늘 이기므로, 이 구조 차이를 어휘층 안에서 반영한다.
W_CURATED_ANCHOR = 0.35
W_BLOCK_CURRENT = 0.04       # kind=="current" block 이 맞았다
W_BLOCK_CONFLICT = -0.03     # 미판정 conflict block 이 맞았다

MAX_EVIDENCE_BLOCKS = 3
MAX_RELATION_EVIDENCE = 3   # 관계 주장 block 은 따로 담는다

# --------------------------------------------------------------------- 토큰화
_WORD = re.compile(r"[0-9A-Za-zㄱ-ㆎ가-힣_]"
                   r"[0-9A-Za-zㄱ-ㆎ가-힣_.\-:/]*")
_HANGUL = re.compile(r"^[가-힣]+$")
_TRIM = re.compile(r"[.\-:/]+$")
# `project-000001와`, `v2.3.1은` 처럼 라틴/숫자 어간 뒤에 조사가 그대로 붙는다.
# 이걸 안 떼면 정본의 `project-000001` 과 영영 안 만난다.
_LATIN_TAIL = re.compile(r"^(.*[^가-힣])([가-힣]+)$")

# 길이 내림차순으로 떼어 본다. 어간이 2글자 미만이 되면 떼지 않는다.
_PARTICLES = ("으로부터", "에서는", "에게서", "이라고", "라고는", "에서도", "까지도",
              "으로는", "이라는", "에게는", "부터", "까지", "에서", "에게", "으로",
              "라고", "이나", "이란", "이든", "한테", "보다", "처럼", "마다", "조차",
              "밖에", "이다", "한다", "된다", "이며", "하며", "하고", "이고", "은가",
              "는가", "인가", "나요", "니까", "지만", "면서", "는데", "은데",
              "은", "는", "이", "가", "을", "를", "의", "에", "도", "만", "와", "과",
              "로", "야", "라", "며", "고", "든", "나")


def _strip_particle(tok: str) -> str:
    """한국어 조사/어미를 뗀 어간. 순수 한글 3글자 이상에만 적용한다."""
    if len(tok) < 3 or not _HANGUL.match(tok):
        return tok
    for p in _PARTICLES:
        if len(p) < len(tok) - 1 and tok.endswith(p):
            return tok[: -len(p)]
    return tok


def tokenize(text: str) -> list[str]:
    text = unicodedata.normalize("NFKC", str(text)).lower()
    out = []
    for m in _WORD.finditer(text):
        t = _TRIM.sub("", m.group(0))
        if not t:
            continue
        tail = _LATIN_TAIL.match(t)
        if tail:
            stem = _TRIM.sub("", tail.group(1))
            if stem:
                out.append(stem)
                if len(tail.group(2)) >= 2:   # 조사가 아니라 합성어일 수도 있다
                    out.append(tail.group(2))
                continue
        out.append(t)
    return out


def index_text(text: str) -> str:
    """색인용 표현. 낱말마다 MARK 를 앞에 붙여 trigram 을 낱말 시작에 고정한다."""
    return " ".join(MARK + t for t in tokenize(text))


def query_terms(text: str) -> list[str]:
    """조회용 어간 토큰. 원형이 어간의 확장이므로 어간만 써도 prefix 로 잡힌다."""
    seen, out = set(), []
    for t in tokenize(text):
        s = _strip_particle(t)
        if len(s) < MIN_TERM_CHARS or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def _match_expr(term: str) -> str:
    return '"' + (MARK + term).replace('"', '""') + '"'


def alias_key(text: str) -> str:
    return " ".join(_strip_particle(t) for t in tokenize(text))


# --------------------------------------------------------------------- 스키마
SCHEMA = """
PRAGMA journal_mode=OFF;
PRAGMA synchronous=OFF;
CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE page(
  rid INTEGER PRIMARY KEY, page_id TEXT, slug TEXT, ptype TEXT,
  nsrc INTEGER, superseded INTEGER, depth INTEGER, head INTEGER);
CREATE TABLE blk(
  rid INTEGER PRIMARY KEY, prid INTEGER, block_id TEXT,
  nref INTEGER, is_current INTEGER, is_conflict INTEGER, anchor INTEGER);
CREATE VIRTUAL TABLE ftx USING fts5(txt, tokenize='trigram', content='');
CREATE VIRTUAL TABLE ftx_v USING fts5vocab(ftx, 'row');
CREATE TABLE alias(key TEXT, prid INTEGER, w REAL, df INTEGER);
CREATE TABLE aterm(prid INTEGER, term TEXT);
CREATE TABLE edge(src INTEGER, dst INTEGER, kind TEXT, block_id TEXT);
"""
INDEXES = """
CREATE INDEX blk_prid ON blk(prid);
CREATE INDEX alias_key ON alias(key);
CREATE INDEX aterm_prid ON aterm(prid);
CREATE INDEX edge_src ON edge(src);
CREATE INDEX edge_dst ON edge(dst);
"""


def _page_aliases(page: dict[str, Any]) -> dict[str, list[str]]:
    """스키마에 없는 제안 필드라 위치를 조금 너그럽게 찾는다."""
    for holder in (page, page.get("meta") or {}, page.get("data") or {}):
        if not isinstance(holder, dict):
            continue
        a = holder.get("aliases")
        if isinstance(a, dict):
            return {k: [str(x) for x in v] for k, v in a.items() if isinstance(v, list)}
        if isinstance(a, list):
            return {"und": [str(x) for x in a]}
    return {}


def _iter_pages(corpus_dir: Path) -> Iterable[dict[str, Any]]:
    for p in sorted(Path(corpus_dir).rglob("*.json")):
        if p.name.startswith("."):
            continue
        try:
            value = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for page in value if isinstance(value, list) else [value]:
            if isinstance(page, dict) and page.get("id") and page.get("blocks"):
                yield page


# --------------------------------------------------------------------- 랭커
class StructuralRanker:
    name = "structural"

    def __init__(self, db: sqlite3.Connection, use_aliases: bool, nblocks: int):
        self.db = db
        self.use_aliases = use_aliases
        self.nblocks = max(1, nblocks)

    # ------------------------------------------------------------- build
    @classmethod
    def build(cls, corpus_dir: Path, index_dir: Path, **opts: Any) -> BuildStats:
        use_aliases = bool(opts.get("use_aliases", True))
        t0 = time.perf_counter()
        index_dir = Path(index_dir)
        index_dir.mkdir(parents=True, exist_ok=True)
        dbp = index_dir / "structural.db"
        for stale in index_dir.glob("structural.db*"):
            stale.unlink()
        db = sqlite3.connect(dbp)
        db.executescript(SCHEMA)

        prid_of: dict[str, int] = {}          # slug 와 page_id 둘 다 키로 넣는다
        pages_rows, blk_rows, ftx_rows = [], [], []
        alias_rows: list[tuple[str, int, float]] = []
        aterm_rows: list[tuple[int, str]] = []
        raw_links: list[tuple[int, str, str, str]] = []
        n_pages = n_blocks = n_links = 0

        for page in _iter_pages(corpus_dir):
            rid = len(pages_rows) + 1
            slug = str(page.get("slug") or "")
            pid = str(page["id"])
            prid_of.setdefault(pid, rid)
            if slug:
                prid_of.setdefault(slug, rid)
            title = str(page.get("title") or "")
            prid_of.setdefault(title, rid)
            ptype = str(page.get("type") or "")
            nsrc = len(page.get("sources") or [])
            pages_rows.append((rid, pid, slug, ptype, nsrc))
            n_pages += 1

            blocks = page.get("blocks") or {}
            order = page.get("block_order") or list(blocks)
            for bid in order:
                b = blocks.get(bid)
                if not isinstance(b, dict):
                    continue
                txt = block_text(b)
                if not txt:
                    continue
                brid = len(blk_rows) + 1
                res = b.get("resolution") or {}
                blk_rows.append((brid, rid, str(b.get("id") or bid),
                                 len(b.get("refs") or []),
                                 1 if b.get("kind") == "current" else 0,
                                 1 if (b.get("kind") == "conflict"
                                       and res.get("status") != "resolved") else 0))
                ftx_rows.append((brid, index_text(txt)))
                n_blocks += 1

            # 개념층 재료
            if use_aliases:
                surfaces: list[tuple[str, float]] = []
                for _lang, names in _page_aliases(page).items():
                    for nm in names:
                        surfaces.append((nm, W_ALIAS_EXACT))
                if title:
                    surfaces.append((title, W_ALIAS_TITLE))
                if ptype in ("concept", "entity"):
                    for tg in page.get("tags") or []:
                        surfaces.append((str(tg), W_ALIAS_TAG))
                seen_key = set()
                for nm, w in surfaces:
                    key = alias_key(nm)
                    if not key or len(key) < MIN_TERM_CHARS:
                        continue
                    if key not in seen_key:
                        seen_key.add(key)
                        alias_rows.append((key, rid, w))
                    # 되먹임(expand)은 **큐레이션된 aliases 필드에서만** 뽑는다.
                    # title/tags 는 진입 키로만 쓴다 — "개념 정의" 같은 일반명이
                    # 질문에 되먹여지면 같은 종류의 page 를 통째로 끌어온다.
                    if w == W_ALIAS_EXACT:
                        for t in query_terms(nm):
                            aterm_rows.append((rid, t))

            for link in page.get("links") or []:
                if not isinstance(link, dict):
                    continue
                tgt = str(link.get("target") or "")
                if tgt:
                    raw_links.append((rid, tgt, str(link.get("kind") or "wiki"),
                                      str(link.get("block_id") or "")))

        db.executemany("INSERT INTO page(rid,page_id,slug,ptype,nsrc,superseded,depth,head)"
                       " VALUES(?,?,?,?,?,0,0,0)", pages_rows)
        db.executemany("INSERT INTO blk VALUES(?,?,?,?,?,?,0)", blk_rows)
        db.executemany("INSERT INTO ftx(rowid,txt) VALUES(?,?)", ftx_rows)

        # 링크 해석: slug / page_id / 제목 무엇으로 적혀도 받는다
        edges = []
        for src, tgt, kind, bid in raw_links:
            dst = prid_of.get(tgt)
            if dst is None:
                dst = prid_of.get(tgt[5:] if tgt.startswith("page:") else "page:" + tgt)
            if dst is None or dst == src:
                continue
            edges.append((src, dst, kind, bid))
            n_links += 1
        db.executemany("INSERT INTO edge VALUES(?,?,?,?)", edges)
        db.execute("UPDATE blk SET anchor=1 WHERE block_id IN "
                   "(SELECT block_id FROM edge WHERE kind IN ('related','supersedes'))")

        # supersedes 체인 접기 — 대체당한 쪽의 깊이를 센다
        succ: dict[int, list[int]] = {}
        for src, dst, kind, _bid in edges:
            if kind == "supersedes":
                succ.setdefault(dst, []).append(src)   # dst 는 src 에게 대체당했다
        depth: dict[int, int] = {}
        head: dict[int, int] = {}
        for start in succ:
            d, cur, guard = 0, start, 0
            while cur in succ and guard < 16:
                cur = succ[cur][0]
                d += 1
                guard += 1
            depth[start] = d
            head[start] = cur          # 체인 끝 = 현행판
        if depth:
            db.executemany("UPDATE page SET superseded=1, depth=?, head=? WHERE rid=?",
                           [(depth[r], head[r], r) for r in depth])

        if use_aliases:
            df: dict[str, int] = {}
            for key, _r, _w in alias_rows:
                df[key] = df.get(key, 0) + 1
            db.executemany("INSERT INTO alias VALUES(?,?,?,?)",
                           [(k, r, w, df[k]) for k, r, w in alias_rows
                            if df[k] <= MAX_ALIAS_FANOUT])
            db.executemany("INSERT INTO aterm VALUES(?,?)", set(aterm_rows))

        db.executescript(INDEXES)
        db.executemany("INSERT INTO meta VALUES(?,?)",
                       [("n_pages", str(n_pages)), ("n_blocks", str(n_blocks)),
                        ("use_aliases", "1" if use_aliases else "0")])
        db.commit()
        db.execute("PRAGMA optimize")
        db.execute("VACUUM")
        db.close()

        elapsed = (time.perf_counter() - t0) * 1000.0
        return BuildStats(
            elapsed_ms=round(elapsed, 1),
            index_bytes=dir_bytes(index_dir),
            notes={"pages": n_pages, "blocks": n_blocks, "edges": n_links,
                   "superseded_pages": len(depth),
                   "supersede_chains": len(set(head.values())), "alias_keys": len(set(
                       k for k, _, _ in alias_rows)) if use_aliases else 0,
                   "use_aliases": use_aliases,
                   "tokenizer": "fts5 trigram + 낱말시작 표식"})

    # -------------------------------------------------------------- load
    @classmethod
    def load(cls, corpus_dir: Path, index_dir: Path, **opts: Any) -> "StructuralRanker":
        # corpus_dir 는 쓰지 않는다. 조회는 색인만 본다.
        dbp = Path(index_dir) / "structural.db"
        if not dbp.exists():
            raise FileNotFoundError(f"색인이 없다: {dbp} (build 를 먼저 돌려라)")
        db = sqlite3.connect(f"file:{dbp}?mode=ro", uri=True,
                             check_same_thread=False)
        meta = dict(db.execute("SELECT k,v FROM meta"))
        built_aliases = meta.get("use_aliases") == "1"
        use_aliases = bool(opts.get("use_aliases", True)) and built_aliases
        return cls(db, use_aliases, int(meta.get("n_blocks") or 1))

    # ------------------------------------------------------------ 어휘층
    def _lex(self, terms: list[tuple[str, float]]) -> tuple[
            dict[int, float], dict[int, list[tuple[float, int]]], dict[int, set[str]]]:
        """토큰별로 FTS 를 한 번씩 친다. 비용은 토큰 수 × posting 길이."""
        cap = MAX_DF_FRAC * self.nblocks
        scored: list[tuple[int, str, float]] = []   # (df추정, term, weight)
        for term, w in terms:
            est = self._est_df(term)
            if est == 0:
                continue
            scored.append((est, term, w))
        scored.sort()
        if len(scored) > 1:
            kept = [s for s in scored if s[0] <= cap] or scored[:1]
        else:
            kept = scored

        blk_score: dict[int, float] = {}
        blk_terms: dict[int, set[str]] = {}
        rare_cap = RARE_DF_FRAC * self.nblocks
        self._rare_terms = {t for est, t, _w in kept[:MAX_QUERY_TERMS]
                            if est <= rare_cap}
        for est, term, w in kept[:MAX_QUERY_TERMS]:
            try:
                rows = self.db.execute(
                    "SELECT rowid, bm25(ftx) FROM ftx WHERE ftx MATCH ?"
                    " ORDER BY bm25(ftx) LIMIT ?",
                    (_match_expr(term), MAX_BLOCKS_PER_TERM)).fetchall()
            except sqlite3.OperationalError:
                continue
            # df 는 색인에서 이미 공짜로 얻었다. 변별력이 큰 토큰을 더 세게 준다.
            # (bm25 의 idf 는 흔한 한국어 어미·상투어를 충분히 눌러 주지 못한다)
            idf = math.log(1.0 + (self.nblocks - est + 0.5) / (est + 0.5))
            for brid, score in rows:
                # sqlite 의 bm25 는 작을수록 좋다. 부호를 뒤집어 더한다.
                blk_score[brid] = blk_score.get(brid, 0.0) + (-float(score)) * w * idf
                blk_terms.setdefault(brid, set()).add(term)
        if not blk_score:
            self._rare_terms = set()
            return {}, {}, {}

        rids = list(blk_score)
        meta: dict[int, tuple[int, int, int, int, str, int]] = {}
        for i in range(0, len(rids), 900):
            chunk = rids[i:i + 900]
            q = ("SELECT rid,prid,block_id,nref,is_current,is_conflict,anchor"
                 " FROM blk WHERE rid IN (%s)" % ",".join("?" * len(chunk)))
            for rid, prid, bid, nref, cur, conf, anc in self.db.execute(q, chunk):
                meta[rid] = (prid, nref, cur, conf, bid, anc)

        page_blocks: dict[int, list[tuple[float, int]]] = {}
        page_terms: dict[int, set[str]] = {}
        for brid, sc in blk_score.items():
            m = meta.get(brid)
            if not m:
                continue
            if m[5]:
                sc *= 1.0 + W_CURATED_ANCHOR
            if blk_terms.get(brid, ()) & self._rare_terms:
                sc *= 1.0 + W_RARE_MATCH
            page_blocks.setdefault(m[0], []).append((sc, brid))
            page_terms.setdefault(m[0], set()).update(blk_terms.get(brid, ()))

        # page 점수 = 가장 잘 맞은 block + 나머지의 꼬리. 단순 합이면 흔한 낱말이
        # 여러 block 에 흩어진 긴 page 가, 희소한 토큰이 한 block 에 정확히 박힌
        # page 를 이긴다. 위키에서는 후자가 답이다.
        page_lex: dict[int, float] = {}
        for prid, blocks in page_blocks.items():
            blocks.sort(reverse=True)
            page_lex[prid] = blocks[0][0] + LEX_TAIL_W * sum(sc for sc, _ in blocks[1:])
        self._blk_meta = meta
        return page_lex, page_blocks, page_terms

    def _est_df(self, term: str) -> int:
        """phrase 의 df 상한 = 구성 trigram 들의 df 최소값.

        fts5vocab 은 색인의 term 목록을 그대로 보여주는 가상 테이블이라
        `term=?` 조회가 색인 seek 한 번이다. 코퍼스를 훑지 않는다.
        """
        s = MARK + term
        if len(s) < 3:
            return 0
        best = None
        for i in range(len(s) - 2):
            row = self.db.execute("SELECT doc FROM ftx_v WHERE term=?",
                                  (s[i:i + 3],)).fetchone()
            if row is None:
                return 0
            best = int(row[0]) if best is None else min(best, int(row[0]))
        return best or 0

    # ------------------------------------------------------------ 개념층
    def _alias_hits(self, toks: list[str]) -> dict[int, float]:
        keys = set()
        for n in range(1, MAX_ALIAS_NGRAM + 1):
            for i in range(len(toks) - n + 1):
                keys.add(" ".join(toks[i:i + n]))
        keys = [k for k in keys if len(k) >= MIN_TERM_CHARS]
        if not keys:
            return {}
        hits: dict[int, float] = {}
        for i in range(0, len(keys), 900):
            chunk = keys[i:i + 900]
            q = ("SELECT prid,w FROM alias WHERE df<=? AND key IN (%s)"
                 % ",".join("?" * len(chunk)))
            for prid, w in self.db.execute(q, [MAX_ALIAS_FANOUT] + chunk):
                hits[prid] = max(hits.get(prid, 0.0), float(w))
        return hits

    def _expand(self, concept_rids: Iterable[int], have: set[str]) -> list[str]:
        rids = list(concept_rids)
        if not rids:
            return []
        out: list[str] = []
        q = ("SELECT term FROM aterm WHERE prid IN (%s)" % ",".join("?" * len(rids)))
        for (term,) in self.db.execute(q, rids):
            if term not in have and term not in out:
                out.append(term)
        cap = MAX_EXPAND_DF_FRAC * self.nblocks
        out = [t for t in out if 0 < self._est_df(t) <= cap]
        out.sort(key=lambda t: (-len(t), t))
        return out[:MAX_EXPANDED_TERMS]

    # ------------------------------------------------------------ 그래프층
    def _hop(self, concept_rids: dict[int, float]) -> dict[int, tuple[float, str]]:
        if not concept_rids:
            return {}
        rids = list(concept_rids)
        out: dict[int, tuple[float, str]] = {}
        ph = ",".join("?" * len(rids))
        for src, dst, bid in self.db.execute(
                "SELECT src,dst,block_id FROM edge WHERE src IN (%s) LIMIT ?" % ph,
                rids + [MAX_HOP_FANOUT * len(rids)]):
            w = concept_rids[src] * W_CONCEPT_HOP
            if w > out.get(dst, (0.0, ""))[0]:
                out[dst] = (w, bid)
        for src, dst, bid in self.db.execute(
                "SELECT src,dst,block_id FROM edge WHERE dst IN (%s) LIMIT ?" % ph,
                rids + [MAX_HOP_FANOUT * len(rids)]):
            w = concept_rids[dst] * W_CONCEPT_HOP
            if w > out.get(src, (0.0, ""))[0]:
                out[src] = (w, bid)
        return out

    def _related_hop(self, page_lex: dict[int, float], max_lex: float
                     ) -> dict[int, tuple[float, str]]:
        """어휘로 잘 맞은 page 에서 `related` 간선 하나를 건넌다.

        crosslingual 이 여기서 풀린다. 한국어 질문이 한국어 짝 page 를 때리면
        그 page 의 `related` 가 영어 정본을 가리킨다. 간선이 들고 있는
        `block_id` 가 그대로 근거 block 이 된다.
        """
        srcs = sorted(page_lex, key=lambda p: -page_lex[p])[:MAX_RELATED_SRC]
        srcs = [p for p in srcs if page_lex[p] / max_lex >= MIN_RELATED_SRC_LEX]
        if not srcs:
            return {}
        ph = ",".join("?" * len(srcs))
        out: dict[int, tuple[float, str]] = {}

        # 근거 block 은 **결과로 내보내는 page 위에 있는 것**을 쓴다. 같은 짝이
        # 양방향 간선으로 적혀 있으면 상대 쪽 block 이 아니라 자기 쪽을 집는다.
        best: dict[int, tuple[float, int, str]] = {}   # dst -> (w, own?, block)

        def offer(dst: int, src: int, bid: str, own: bool) -> None:
            w = (page_lex[src] / max_lex) * W_RELATED_HOP
            key = (w, 1 if own else 0)
            cur = best.get(dst)
            if cur is None or key > (cur[0], cur[1]):
                best[dst] = (w, 1 if own else 0, bid)

        for src, dst, bid in self.db.execute(
                "SELECT src,dst,block_id FROM edge"
                " WHERE kind='related' AND src IN (%s)" % ph, srcs):
            offer(dst, src, bid, own=False)      # bid 는 src(질문에 맞은 쪽) 위에 있다
        for src, dst, bid in self.db.execute(
                "SELECT src,dst,block_id FROM edge"
                " WHERE kind='related' AND dst IN (%s)" % ph, srcs):
            offer(src, dst, bid, own=True)       # bid 는 src(내보낼 쪽) 위에 있다
        return {d: (w, bid) for d, (w, _own, bid) in best.items()}

    def _relation(self, page_blocks: dict[int, list[tuple[float, int]]],
                  cand: list[int]) -> dict[int, tuple[float, list[str]]]:
        """관계 주장 block 을 집는다.

        `links[].block_id` 덕분에 간선마다 그 링크를 담은 block 을 안다.
        **질문에 맞은 block 이 동시에 다른 후보 page 로 가는 링크를 들고 있다면**
        그 block 이 바로 두 대상의 관계를 주장하는 문장이다. 어휘 랭커는
        block 이 어디를 가리키는지 모르므로 이 구분을 못 한다.
        """
        src_list = cand[:MAX_RELATION_SRC]
        if len(src_list) < 2:
            return {}
        want = set(cand)
        matched: dict[int, set[str]] = {}
        for prid in src_list:
            matched[prid] = {self._blk_meta[brid][4]
                             for _sc, brid in page_blocks.get(prid, [])}
        ph = ",".join("?" * len(src_list))
        out: dict[int, tuple[float, list[str]]] = {}
        for src, dst, bid in self.db.execute(
                "SELECT src,dst,block_id FROM edge WHERE src IN (%s)" % ph, src_list):
            if not bid or dst not in want or bid not in matched.get(src, ()):
                continue
            sc, evs = out.get(src, (0.0, []))
            if bid not in evs:
                evs.append(bid)
            out[src] = (min(W_RELATION * 2, sc + W_RELATION), evs)
        return out

    # ----------------------------------------------------------- search
    def search(self, query: str, k: int = 10) -> list[Hit]:
        toks = query_terms(query)
        if not toks:
            return []
        terms: list[tuple[str, float]] = [(t, 1.0) for t in toks]

        concepts: dict[int, float] = {}
        if self.use_aliases:
            concepts = self._alias_hits(toks)
            for extra in self._expand(concepts, set(toks)):
                terms.append((extra, W_EXPANDED_TERM))

        page_lex, page_blocks, page_terms = self._lex(terms)
        hop = self._hop(concepts) if concepts else {}
        lex_top = max(page_lex.values(), default=0.0) or 1.0
        rhop = self._related_hop(page_lex, lex_top) if page_lex else {}

        cand = set(page_lex) | set(concepts) | set(hop) | set(rhop)
        if not cand:
            return []
        ranked_by_lex = sorted(page_lex, key=lambda p: -page_lex[p])[:CAND_PAGES]
        # 식별자급 토큰을 담은 page 는 흔한 낱말 점수에 밀려 잘려 나가면 안 된다.
        rare = {p for p, ts in page_terms.items() if ts & self._rare_terms}
        if rare:
            ranked_by_lex = sorted(set(ranked_by_lex) | rare,
                                   key=lambda p: -page_lex[p])
        rel = self._relation(page_blocks, ranked_by_lex)

        keep = (set(ranked_by_lex) | set(concepts) | set(hop)
                | set(rel) | set(rhop))
        rids = list(keep)
        info: dict[int, tuple[str, str, int, int, int, int]] = {}

        def fetch(rid_list: list[int]) -> None:
            for i in range(0, len(rid_list), 900):
                chunk = rid_list[i:i + 900]
                q = ("SELECT rid,page_id,ptype,nsrc,superseded,depth,head FROM page"
                     " WHERE rid IN (%s)" % ",".join("?" * len(chunk)))
                for rid, pid, ptype, nsrc, sup, dep, hd in self.db.execute(q, chunk):
                    info[rid] = (pid, ptype, nsrc, sup, dep, hd)

        fetch(rids)
        max_lex = max((page_lex[p] for p in keep if p in page_lex), default=0.0) or 1.0

        # 시간축 전진: 낡은 판이 질문에 맞으면 그 **자리**를 현행판이 물려받는다.
        # 강등만 하면 정답인 현행판까지 같이 묻힌다. 대체 관계가 뜻하는 바는
        # "이 질문에 맞는 그 문서의 현재 판은 저것" 이므로, 낡은 판이 차지했을
        # 순위 바로 위에 현행판을 놓는 것이 정확한 해석이다.
        forward: dict[int, float] = {}
        for prid in list(keep):
            m = info.get(prid)
            if not m or not m[3] or not m[5] or m[5] == prid:
                continue
            gain = (page_lex.get(prid, 0.0) / max_lex) * W_SUPERSEDE_FORWARD \
                + FORWARD_EPS
            if gain > forward.get(m[5], 0.0):
                forward[m[5]] = gain
        if forward:
            new = [r for r in forward if r not in info]
            if new:
                fetch(new)
            keep |= set(forward)

        # 현행판의 근거 block 은 "무엇을 대체했는지"를 적어 둔 그 block 이다.
        # 시점 질문의 답은 본문 아무 단락이 아니라 이 판본 전환 문장이다.
        head_evidence: dict[int, str] = {}
        heads = [r for r in keep if r in forward or (info.get(r) and not info[r][3])]
        for i in range(0, len(heads), 900):
            chunk = heads[i:i + 900]
            q2 = ("SELECT src,block_id FROM edge WHERE kind='supersedes'"
                  " AND src IN (%s)" % ",".join("?" * len(chunk)))
            for src, bid in self.db.execute(q2, chunk):
                if bid:
                    head_evidence.setdefault(src, bid)
        hits: list[Hit] = []
        for prid in keep:
            meta = info.get(prid)
            if not meta:
                continue
            pid, _ptype, nsrc, sup, dep, _head = meta
            lex_n = page_lex.get(prid, 0.0) / max_lex

            gate = 1.0
            if sup:
                gate = GATE_SUPERSEDED * (GATE_CHAIN_DECAY ** max(0, dep - 1))
            score = lex_n * gate

            if prid in concepts:
                score += concepts[prid] * W_CONCEPT_SELF
            if prid in hop:
                score += hop[prid][0]
            if prid in rhop:
                score += rhop[prid][0]
            if prid in rel:
                score += rel[prid][0]
            if prid in forward:
                score += forward[prid]

            blocks = sorted(page_blocks.get(prid, []), reverse=True)
            if nsrc:
                score += W_PROV_SOURCES
            for _sc, brid in blocks[:MAX_EVIDENCE_BLOCKS]:
                _p, nref, cur, conf, _b, _a = self._blk_meta[brid]
                if nref:
                    score += W_PROV_REFS
                if cur:
                    score += W_BLOCK_CURRENT
                if conf:
                    score += W_BLOCK_CONFLICT

            evidence: list[str] = []
            if prid in head_evidence:
                evidence.append(head_evidence[prid])
            if prid in rel:
                evidence.extend(rel[prid][1][:MAX_RELATION_EVIDENCE])
            for _sc, brid in blocks[:MAX_EVIDENCE_BLOCKS]:
                bid = self._blk_meta[brid][4]
                if bid and bid not in evidence:
                    evidence.append(bid)
            for extra in (hop.get(prid), rhop.get(prid)):
                if extra and extra[1] and extra[1] not in evidence:
                    evidence.append(extra[1])
            hits.append(Hit(page_id=pid, score=round(score, 6),
                            block_ids=evidence[:MAX_EVIDENCE_BLOCKS
                                               + MAX_RELATION_EVIDENCE]))

        hits.sort(key=lambda h: (-h.score, h.page_id))
        return hits[:k]


RANKER = StructuralRanker

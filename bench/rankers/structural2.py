#!/usr/bin/env python3
"""구조 랭커 v2 — 표준 라이브러리만 쓰는 역색인 + 간선 가중 확산 + 대체 체인 접기.

structural.py(v1) 와 같은 계약(bench/rankers/base.py)이지만 층을 셋으로 줄였다.

1. 어휘층  순수 Python 역색인. FTS5 도, 낱말시작 표식(MARK) 도, 조사 사전도 없다.
   - 한글 run 은 음절 2-gram, 라틴/숫자 run 은 낱말 하나가 토큰이다.
     조사는 색인이 아니라 토큰화가 흡수한다: `정책은` → `정책`,`책은` 이므로
     질문의 `정책` 은 그대로 맞고, `책은` 은 흔한 2-gram 이라 idf 가 눌러 준다.
     `cfg.pipeline.000의` 는 문자 종류가 바뀌는 자리에서 잘려 `cfg.pipeline.000` 이 된다.
   - posting 은 build 때 BM25 impact(idf 포함) 를 계산해 **impact 내림차순**으로
     저장한다. 조회는 토큰마다 posting 앞부분(MAX_POST)만 읽으므로 비용이
     토큰 수 × MAX_POST 로 묶인다. 저장은 sqlite 일반 테이블(BLOB) 이라
     sqlite 빌드 옵션에 기대지 않는다.
   - block 의 구조 신호(큐레이션 간선을 든 anchor, current, 미판정 conflict,
     여러 page 에 똑같이 복사된 boilerplate) 는 build 때 impact 에 곱해 둔다.
     조회 시점에 block 을 다시 들여다보지 않는다.

2. 그래프층  간선 종류별 가중치를 build 때 인접 리스트에 구워 두고, 조회 때는
   어휘 상위 후보를 seed 로 bounded 확산(personalized PageRank 꼴)을 2 step 돈다.
   `related` 1-hop, concept 1-hop 같은 종류별 특수 규칙이 없다. 간선을 받은
   page 의 근거 block 은 그 page 위에 있는 역방향 간선의 block 이다(있으면).

3. 시간축  supersedes 체인은 build 때 head 로 접는다. 조회 규칙은 하나다:
   "낡은 page 의 점수는 head 에 귀속되고, 낡은 page 는 head 아래 '대체됨'
   자리에만 남는다." 강등(gate)·전진(forward)·head 근거 block 이 이 한 규칙에서
   나온다 — head 의 근거 block 은 build 때 page 행에 적어 둔 supersedes anchor 다.

옵션(**opts): graph=ppr|none, fold=true|false, dup=true|false, wiki_w=<float>,
              hub=true|false, steps=<int>, idf_pow=<float>, agg=sum|max,
              tie=none|receiver, tie_eps=<float>, min_seed=<float>
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
import time
import unicodedata
from array import array
from pathlib import Path
from typing import Any

from .base import BuildStats, Hit, block_text, dir_bytes, load_pages

# ------------------------------------------------------------------ 상수
# 어휘층
BM25_K1 = 1.2
BM25_B = 0.75
MAX_POST = 400            # 토큰당 읽는 posting 수 (impact 상위)
MAX_QUERY_TERMS = 24      # 질문당 posting 조회 횟수 상한 (df 오름차순으로 고른다)
MAX_DF_FRAC = 0.30        # 이보다 흔한 토큰은 버린다 (유일한 토큰이면 남긴다)
LEX_TAIL_W = 0.25         # page = 최고 block + 0.25 × 나머지 합
IDF_POW = 3.0             # 질문 토큰 가중 = idf^IDF_POW. 한 낱말이 2-gram 여럿으로
                          # 쪼개져 흔한 낱말이 과대 대표되므로, 희소 토큰 쪽으로 기울인다
                          # (1/2/2.5/3/4 를 재 봤다: 1 은 crosslingual 이 0, 2.5 이상은 같다)
CAND_PAGES = 200
# block 구조 계수 (build 때 impact 에 곱한다)
W_ANCHOR = 1.35           # related/supersedes 간선을 든 block = 큐레이션된 주장
W_CURRENT = 1.04
W_CONFLICT = 0.97
DUP_DEFAULT = False       # 복제 block 감쇠. 동결 코퍼스의 +0.08 은 생성기 인공물(distractor 가
                          # 6 page 에 바이트 동일)에서만 나왔다(V2_REPORT §8). 옵션 dup=true 로 켠다
DUP_MIN_PAGES = 2         # 같은 본문이 이보다 많은 page 에 있으면 boilerplate
# 그래프층
SEEDS = 30                # 어휘 상위 seed 수
STEPS = 2
FRONTIER = 50             # step 마다 살려 두는 node 수
# 질량은 방금 온 간선으로 되돌아가지 않는다(non-backtracking). related 는 대칭
# 간선이라 되돌아가게 두면 2 step 째에 seed 가 제 짝을 거쳐 스스로를 부풀려
# 정작 건너간 page 가 묻힌다.
W_GRAPH = 0.85            # 확산 점수 계수 (직접 맞은 page 가 위에 남게 1 미만)
STEP_DECAY = 0.5
AGG = "max"               # 한 node 에 모이는 질량을 합(sum, PageRank 식)할지 최댓값(max,
                          # 가장 센 경로 하나)으로 볼지. sum 은 300 page 처럼 작고 촘촘한
                          # 그래프에서 seed 30개의 wiki 간선이 한 page 에 쌓여 직접 맞은 page
                          # 를 넘어선다(recall@5 0.29). max 는 related 간선으로만 넘을 수 있다.
EDGE_W = {"related": 1.0, "wiki": 0.15}   # 없는 kind 는 wiki 취급
HUB_DAMP = True           # wiki 류 간선 가중 ÷ (1 + ln 들어오는 wiki 간선 수). 수백 page 가
                          # 언급하는 허브로 가는 간선은 그만큼 약한 근거다. related 는 큐레이터가
                          # "같은 것"이라고 적은 1:1 주장이라 감쇠하지 않는다
MAX_FANOUT = 64           # page 당 저장하는 인접 항목 수 (가중치 내림차순). 조회 비용 상한
# 시간축
STALE_SHOW = 0.30         # 낡은 page = head 점수 × 0.30 (head 바로 아래 "대체됨")
# crosslingual 동률 실험 (V2_REPORT §8). 기본은 둘 다 꺼져 있다.
TIE = "none"              # receiver: 1.0 간선으로 질량을 받은 page 가 자체 어휘 근거도 있고
                          # 보낸 page 와 TIE_EPS 안이면 보낸 page 바로 위에 놓는다
TIE_EPS = 0.05
MIN_SEED = 0.0            # v1 의 문턱: 어휘 비율이 이보다 낮은 seed 는 질량을 보내지 않는다
# 근거
MAX_EVIDENCE = 3

_RUN = re.compile(r"[0-9a-z_][0-9a-z_.\-:/]*|[가-힣]+")
_TRIM = re.compile(r"[.\-:/]+$")


# ------------------------------------------------------------------ 토큰화
def tokenize(text: str) -> list[str]:
    """한글 run → 음절 2-gram, 그 외 run → 낱말. 조사/어미 사전이 없다."""
    text = unicodedata.normalize("NFKC", str(text)).lower()
    out: list[str] = []
    for m in _RUN.finditer(text):
        run = m.group(0)
        if "가" <= run[0] <= "힣":
            out.extend(run[i:i + 2] for i in range(len(run) - 1))
        else:
            run = _TRIM.sub("", run)
            if len(run) >= 2:
                out.append(run)
    return out


def _norm_text(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).split())


SCHEMA = """
PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;
CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE page(rid INTEGER PRIMARY KEY, page_id TEXT, head INTEGER, sup_block TEXT);
CREATE TABLE blk(rid INTEGER PRIMARY KEY, prid INTEGER, block_id TEXT);
CREATE TABLE post(term TEXT PRIMARY KEY, df INTEGER, ids BLOB, ws BLOB) WITHOUT ROWID;
CREATE TABLE adj(src INTEGER, dst INTEGER, w REAL, own_block TEXT);
"""


# ------------------------------------------------------------------ 랭커
class Structural2Ranker:
    name = "structural2"

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

    # ------------------------------------------------------------ build
    @classmethod
    def build(cls, corpus_dir: Path, index_dir: Path, **opts: Any) -> BuildStats:
        t0 = time.perf_counter()
        use_dup = bool(opts.get("dup", DUP_DEFAULT))
        edge_w = dict(EDGE_W)
        if "wiki_w" in opts:
            edge_w["wiki"] = float(opts["wiki_w"])
        index_dir = Path(index_dir)
        index_dir.mkdir(parents=True, exist_ok=True)
        dbp = index_dir / "structural2.db"
        for stale in index_dir.glob("structural2.db*"):
            stale.unlink()

        pages = sorted(load_pages(corpus_dir), key=lambda p: str(p["id"]))
        rid_of: dict[str, int] = {}
        for i, p in enumerate(pages, 1):
            for key in (str(p["id"]), str(p.get("slug") or ""), str(p.get("title") or "")):
                if key:
                    rid_of.setdefault(key, i)

        # --- block 텍스트와 구조 플래그
        blk_rows: list[tuple[int, int, str]] = []          # (brid, prid, block_id)
        blk_toks: list[list[str]] = []
        blk_mult: list[float] = []
        blk_key: list[str] = []                             # dup 판정용 본문 키
        text_pages: dict[str, set[int]] = {}
        edges: list[tuple[int, int, str, str]] = []        # (src, dst, kind, block_id)
        for prid, p in enumerate(pages, 1):
            anchors: set[str] = set()
            for link in p.get("links") or []:
                if not isinstance(link, dict):
                    continue
                tgt = str(link.get("target") or "")
                dst = rid_of.get(tgt) or rid_of.get(
                    tgt[5:] if tgt.startswith("page:") else "page:" + tgt)
                if not dst or dst == prid:
                    continue
                kind = str(link.get("kind") or "wiki")
                bid = str(link.get("block_id") or "")
                edges.append((prid, dst, kind, bid))
                if kind in ("related", "supersedes"):
                    anchors.add(bid)
            blocks = p.get("blocks") or {}
            title = str(p.get("title") or "")
            if title:                                     # 제목은 근거 없는 가상 block
                blk_rows.append((len(blk_rows) + 1, prid, ""))
                blk_toks.append(tokenize(title))
                blk_mult.append(1.0)
                blk_key.append("")
            for bid in p.get("block_order") or list(blocks):
                b = blocks.get(bid)
                if not isinstance(b, dict):
                    continue
                txt = block_text(b)
                if not txt:
                    continue
                bid = str(b.get("id") or bid)
                mult = 1.0
                if bid in anchors:
                    mult *= W_ANCHOR
                if b.get("kind") == "current":
                    mult *= W_CURRENT
                if b.get("kind") == "conflict" and (b.get("resolution") or {}).get("status") != "resolved":
                    mult *= W_CONFLICT
                blk_rows.append((len(blk_rows) + 1, prid, bid))
                blk_toks.append(tokenize(txt))
                blk_mult.append(mult)
                key = _norm_text(txt)
                blk_key.append(key)
                text_pages.setdefault(key, set()).add(prid)
        if use_dup:
            # 같은 본문이 여러 page 에 복사돼 있으면 어느 page 의 근거도 아니다.
            for i, key in enumerate(blk_key):
                n = len(text_pages[key]) if key else 1
                if n >= DUP_MIN_PAGES:
                    blk_mult[i] /= n

        # --- BM25 impact posting (impact 내림차순)
        nblk = len(blk_toks)
        avg_len = (sum(len(t) for t in blk_toks) / nblk) if nblk else 1.0
        tf_lists: dict[str, list[tuple[int, int]]] = {}
        for brid, toks in enumerate(blk_toks, 1):
            counts: dict[str, int] = {}
            for t in toks:
                counts[t] = counts.get(t, 0) + 1
            for t, c in counts.items():
                tf_lists.setdefault(t, []).append((brid, c))
        post_rows = []
        for term in sorted(tf_lists):
            plist = tf_lists[term]
            df = len(plist)
            idf = math.log(1.0 + (nblk - df + 0.5) / (df + 0.5))
            scored = []
            for brid, tf in plist:
                ln = len(blk_toks[brid - 1])
                imp = idf * tf * (BM25_K1 + 1) / (tf + BM25_K1 * (1 - BM25_B + BM25_B * ln / avg_len))
                scored.append((round(imp * blk_mult[brid - 1], 5), brid))
            scored.sort(key=lambda x: (-x[0], x[1]))
            ids = array("I", [b for _w, b in scored])
            ws = array("f", [w for w, _b in scored])
            post_rows.append((term, df, ids.tobytes(), ws.tobytes()))

        # --- supersedes 체인 접기: 낡은 page → head, head 의 근거 block
        succ: dict[int, int] = {}
        sup_block: dict[int, str] = {}
        for src, dst, kind, bid in edges:
            if kind == "supersedes":
                succ.setdefault(dst, src)               # dst 는 src 에게 대체당했다
                sup_block.setdefault(src, bid)          # src 위의 "X 를 대체한다" block
        head: dict[int, int] = {}
        for start in succ:
            cur, guard = start, 0
            while cur in succ and guard < 32:
                cur, guard = succ[cur], guard + 1
            head[start] = cur

        # --- 확산용 인접 리스트 (양방향, kind 가중치 구움). supersedes 는 접기가 맡는다.
        own: dict[tuple[int, int], str] = {}
        for src, dst, kind, bid in edges:
            own.setdefault((src, dst), bid)             # src 위에서 dst 를 가리키는 block
        adj: dict[tuple[int, int], float] = {}
        weak_in: dict[int, int] = {}                    # wiki 류 간선의 in-degree (양방향)
        for src, dst, kind, _bid in edges:
            if kind == "supersedes":
                continue
            w = edge_w.get(kind, edge_w["wiki"])
            for a, b in ((src, dst), (dst, src)):
                adj[(a, b)] = max(adj.get((a, b), 0.0), w)
                if kind != "related":
                    weak_in[b] = weak_in.get(b, 0) + 1
        if bool(opts.get("hub", HUB_DAMP)):
            adj = {(a, b): (w if w >= 1.0 else w / (1.0 + math.log(weak_in.get(b, 1))))
                   for (a, b), w in adj.items()}
        by_src: dict[int, list[tuple[float, int]]] = {}
        for (a, b), w in adj.items():
            by_src.setdefault(a, []).append((-round(w, 6), b))
        adj_rows = []
        for a in sorted(by_src):
            for nw, b in sorted(by_src[a])[:MAX_FANOUT]:
                adj_rows.append((a, b, -nw, own.get((b, a), "")))
        blkmap = array("I", [prid for _r, prid, _b in blk_rows])

        db = sqlite3.connect(dbp)
        db.executescript(SCHEMA)
        db.executemany("INSERT INTO page VALUES(?,?,?,?)",
                       [(i, str(p["id"]), head.get(i, i), sup_block.get(i, ""))
                        for i, p in enumerate(pages, 1)])
        db.executemany("INSERT INTO blk VALUES(?,?,?)", blk_rows)
        db.executemany("INSERT INTO post VALUES(?,?,?,?)", post_rows)
        db.executemany("INSERT INTO adj VALUES(?,?,?,?)", adj_rows)
        db.execute("CREATE INDEX adj_src ON adj(src)")
        db.executemany("INSERT INTO meta VALUES(?,?)",
                       [("n_pages", str(len(pages))), ("n_blocks", str(nblk)),
                        ("dup", "1" if use_dup else "0"), ("wiki_w", repr(edge_w["wiki"])),
                        ("blkmap", blkmap.tobytes())])   # block rid → page rid (4B/block)
        db.commit()
        db.execute("VACUUM")
        db.close()
        return BuildStats(
            elapsed_ms=round((time.perf_counter() - t0) * 1000.0, 1),
            index_bytes=dir_bytes(index_dir),
            notes={"pages": len(pages), "blocks": nblk, "terms": len(post_rows),
                   "postings": sum(len(v) for v in tf_lists.values()),
                   "edges": len(edges), "adj_rows": len(adj_rows),
                   "superseded_pages": len(head), "dup": use_dup,
                   "edge_w": edge_w,
                   "tokenizer": "한글 음절 2-gram + 라틴/숫자 낱말; 표준 라이브러리 역색인"})

    # ------------------------------------------------------------- load
    @classmethod
    def load(cls, corpus_dir: Path, index_dir: Path, **opts: Any) -> "Structural2Ranker":
        dbp = Path(index_dir) / "structural2.db"
        if not dbp.exists():
            raise FileNotFoundError(f"색인이 없다: {dbp} (build 를 먼저 돌려라)")
        db = sqlite3.connect(f"file:{dbp}?mode=ro", uri=True, check_same_thread=False)
        r = cls(db, opts)
        meta = dict(db.execute("SELECT k,v FROM meta"))
        r.nblocks = int(meta.get("n_blocks") or 1)
        r.blkmap = array("I")
        r.blkmap.frombytes(meta["blkmap"])
        return r

    # ------------------------------------------------------------ 어휘층
    def _lex(self, query: str) -> tuple[dict[int, float], dict[int, list[tuple[float, int]]]]:
        terms = sorted(set(tokenize(query)))
        if not terms:
            return {}, {}
        q = "SELECT term, df, substr(ids,1,?), substr(ws,1,?) FROM post WHERE term IN (%s)" \
            % ",".join("?" * len(terms))
        rows = self.db.execute(q, [4 * MAX_POST, 4 * MAX_POST] + terms).fetchall()
        rows.sort(key=lambda r: (r[1], r[0]))
        cap = MAX_DF_FRAC * self.nblocks
        kept = [r for r in rows if r[1] <= cap] or rows[:1]
        blk: dict[int, float] = {}
        n = self.nblocks
        for _term, df, ids, ws in kept[:MAX_QUERY_TERMS]:
            a, w = array("I"), array("f")
            a.frombytes(ids)
            w.frombytes(ws)
            qw = math.log(1.0 + (n - df + 0.5) / (df + 0.5)) ** (self.idf_pow - 1.0)
            for brid, imp in zip(a, w):
                blk[brid] = blk.get(brid, 0.0) + imp * qw
        if not blk:
            return {}, {}
        page_blocks: dict[int, list[tuple[float, int]]] = {}
        bm = self.blkmap
        for brid, sc in blk.items():
            page_blocks.setdefault(bm[brid - 1], []).append((sc, brid))
        page_lex: dict[int, float] = {}
        for prid, bl in page_blocks.items():
            bl.sort(reverse=True)
            page_lex[prid] = bl[0][0] + LEX_TAIL_W * sum(s for s, _ in bl[1:])
        return page_lex, page_blocks

    # ----------------------------------------------------------- 그래프층
    def _ppr(self, page_lex: dict[int, float], top: float
             ) -> tuple[dict[int, float], dict[int, tuple[float, str]]]:
        seeds = sorted(page_lex, key=lambda p: (-page_lex[p], p))[:SEEDS]
        mass = {p: page_lex[p] / top for p in seeds if page_lex[p] / top >= self.min_seed}
        score: dict[int, float] = {}
        via: dict[int, tuple[float, str]] = {}
        self._sender: dict[int, tuple[float, int]] = {}   # dst → (w, src) 가장 센 1.0 간선
        use_max = self.agg == "max"
        came: dict[int, set[int]] = {}          # node → 직전 step 에 질량을 보내 준 node 들
        for step in range(self.steps):
            srcs = sorted(mass)
            nxt: dict[int, float] = {}
            nxt_from: dict[int, set[int]] = {}
            decay = STEP_DECAY ** step
            for i in range(0, len(srcs), 900):
                chunk = srcs[i:i + 900]
                for src, dst, w, own in self.db.execute(
                        "SELECT src,dst,w,own_block FROM adj WHERE src IN (%s)" % ",".join("?" * len(chunk)), chunk):
                    if w <= 0.0 or dst in came.get(src, ()):
                        continue
                    m = mass[src] * w
                    nxt[dst] = max(nxt.get(dst, 0.0), m) if use_max else nxt.get(dst, 0.0) + m
                    nxt_from.setdefault(dst, set()).add(src)
                    if step == 0 and w >= 1.0 and m > self._sender.get(dst, (0.0, 0))[0]:
                        self._sender[dst] = (m, src)
                    if m * decay > via.get(dst, (0.0, ""))[0]:
                        via[dst] = (m * decay, own)
            for dst, m in nxt.items():
                score[dst] = score.get(dst, 0.0) + m * decay
            mass = dict(sorted(nxt.items(), key=lambda kv: (-kv[1], kv[0]))[:FRONTIER])
            came = nxt_from
        return score, via

    # ------------------------------------------------------------- search
    def search(self, query: str, k: int = 10) -> list[Hit]:
        page_lex, page_blocks = self._lex(query)
        if not page_lex:
            return []
        top = max(page_lex.values()) or 1.0
        cand = sorted(page_lex, key=lambda p: (-page_lex[p], p))[:CAND_PAGES]
        score = {p: page_lex[p] / top for p in cand}
        graph, via = ({}, {})
        if self.graph == "ppr":
            graph, via = self._ppr(page_lex, top)
            for p, g in graph.items():
                score[p] = score.get(p, 0.0) + W_GRAPH * g

        if self.tie == "receiver" and graph:
            # 1.0 간선으로 건너간 page 가 자체 어휘 근거도 있고 보낸 page 와 거의 동률이면
            # 받은 쪽을 보낸 쪽 바로 위에 둔다 (related = "같은 것의 다른 표현").
            base = dict(score)                       # 한 번에 판정한다 (서로 밀어 올리기 방지)
            for v, (_m, u) in sorted(self._sender.items()):
                if v in page_lex and u in base and v in base \
                        and base[v] < base[u] <= base[v] * (1.0 + self.tie_eps):
                    score[v] = max(score[v], base[u] + 1e-4)

        rids = sorted(score)
        info: dict[int, tuple[str, int, str]] = {}
        for i in range(0, len(rids), 900):
            chunk = rids[i:i + 900]
            for rid, pid, head, sb in self.db.execute(
                    "SELECT rid,page_id,head,sup_block FROM page WHERE rid IN (%s)" % ",".join("?" * len(chunk)), chunk):
                info[rid] = (pid, head, sb)

        # 시간축: 낡은 page 의 점수는 head 에 귀속, 낡은 page 는 head 아래에만 남는다.
        lifted: set[int] = set()
        if self.fold:
            for p in rids:
                h = info[p][1]
                if h != p:
                    if h not in score or score[p] > score[h]:
                        score[h] = score[p]
                        lifted.add(h)
            for p in rids:
                h = info[p][1]
                if h != p:
                    score[p] = score[h] * STALE_SHOW
            new = [h for h in score if h not in info]
            for i in range(0, len(new), 900):
                chunk = new[i:i + 900]
                for rid, pid, head, sb in self.db.execute(
                        "SELECT rid,page_id,head,sup_block FROM page WHERE rid IN (%s)" % ",".join("?" * len(chunk)), chunk):
                    info[rid] = (pid, head, sb)

        final = sorted(score, key=lambda p: (-score[p], info[p][0]))[:k]
        need = [brid for p in final for _s, brid in page_blocks.get(p, [])[:MAX_EVIDENCE]]
        bid_of: dict[int, str] = {}
        if need:
            for brid, bid in self.db.execute(
                    "SELECT rid,block_id FROM blk WHERE rid IN (%s)" % ",".join("?" * len(need)), need):
                bid_of[brid] = bid
        hits: list[Hit] = []
        for p in final:
            pid, _h, sb = info[p]
            ev: list[str] = []
            if p in lifted and sb:
                ev.append(sb)
            if p in via and via[p][1] and via[p][1] not in ev:
                ev.append(via[p][1])
            for _s, brid in page_blocks.get(p, [])[:MAX_EVIDENCE]:
                bid = bid_of.get(brid, "")
                if bid and bid not in ev:
                    ev.append(bid)
            hits.append(Hit(page_id=pid, score=round(score[p], 6), block_ids=ev[:MAX_EVIDENCE + 1]))
        return hits


RANKER = Structural2Ranker

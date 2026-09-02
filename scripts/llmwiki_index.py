#!/usr/bin/env python3
"""llmwiki_json 검색 색인 — `index/search.sqlite`.

`llmwiki.py build` 가 정본(`wiki/**/*.json`)에서 굽는 파생물이다. 훅(`llmwiki_context.py`)은
이 파일을 읽기 전용으로 열어 검색·부분 그래프 투영·렌더를 하고, 정본은 신선도 확인과
`llmwiki_get` 에서만 다시 읽는다. 표준 라이브러리만 쓴다.

세 층 (bench/rankers/structural2.py·structural3.py 에서 옮겨 왔다 — bench 는 이 모듈을 import 한다):

1. 어휘층  순수 Python 역색인. 한글 run 은 음절 2-gram, 라틴/숫자 run 은 낱말 + `_ . - : /` 경계로
   쪼갠 조각(원 토큰 유지). posting 은 term 마다 (block rid, tf, 길이, 구조 flag) 를 rid 순 BLOB 으로
   저장하고, 조회는 df·평균 길이를 live 값으로 읽어 BM25 impact 를 계산한 뒤 토큰마다 impact 상위
   MAX_POST 개만 쓴다. block 의 구조 신호(큐레이션 간선을 든 anchor, current, 미판정 conflict)는
   flag 로 두고 조회 때 곱한다 — 그래서 page 몇 개가 바뀌어도 그 page 의 항목만 고치면 된다.
2. 그래프층  간선 종류별 가중 인접 리스트를 build 때 굽고, 조회 때 어휘 상위 seed 에서 2 step
   bounded 확산을 돈다.
3. 시간축  supersedes 체인을 build 때 head 로 접는다. 낡은 page 의 점수는 head 에 귀속되고
   낡은 page 는 head 아래 "대체됨" 자리에만 남는다. fork(한 page 를 둘이 대체)·cycle 은 접지
   않고 상태로 남긴다.

근거 block 선택에서 heading·thematic_break·제목 가상 block 은 뺀다(ev=0). page 점수에는 쓴다.
H6(heading 경로를 block 본문 앞에 붙여 색인)은 옵션이고 기본은 꺼져 있다 — 자연 세트에서
+0.06 이지만 heading 어휘가 없는 질문에서는 −0.05 라 일반화가 확인되지 않았다.

투영 표(page·blk.text·link)까지 한 파일에 있어 훅이 정본을 스캔하지 않는다.

증분 갱신 (FINAL_PROPOSAL §6-4): `apply_delta` 가 바뀐 page 의 행만 한 트랜잭션으로 갈아 끼우고
`refresh_graph` 가 그 이웃의 adj·supersedes head 만 다시 계산한다. rid 는 배열 위치가 아니라 page id 의
해시(+block 위치) 라 증분본과 cold build 의 표 내용이 같고, publish(빈 파일에 DDL·PK 순 재작성 → `os.replace`)
바이트도 헤더까지 같다. 작업 DB 는 `index/search.work.sqlite`(WAL), 훅은 publish 본만 immutable 로 읽는다.
`revision.json.search_root` 는 발행본 바이트의 sha256 이다. 표 내용 지문(`logical_digest`, sqlite 버전과
무관) 은 parity 가 바이트가 다를 때 원인을 가르는 데 쓴다.

렌더 형식(P/B/E)은 bench/context/arms_v3.py 의 v3 그대로: P=page 한 줄, B=block(`slug#id 상태 | 본문`,
긴 block 은 질문 토큰 idf² 로 고른 행만), E=큐레이션 간선, A=주소만. 320자 상한은 마지막에 다시
자른다(review_v3 §3.2 의 321자).
"""
from __future__ import annotations

import hashlib
import heapq
import json
import math
import os
import re
import sqlite3
import unicodedata
from array import array
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = "2"
DB_NAME = "search.sqlite"
WORK_NAME = "search.work.sqlite"      # build 만 여는 작업 DB(WAL). publish 본은 여기서 새 파일로 다시 쓴다
STATE_NAME = "search.work.json"       # 지난 build 의 시작 시각·map root (mtime 스캔의 기준)

# ------------------------------------------------------------------ 어휘층
BM25_K1 = 1.2
BM25_B = 0.75
MAX_POST = 400            # 토큰당 쓰는 posting 수 (live impact 상위)
MAX_QUERY_TERMS = 24      # 질문당 posting 조회 횟수 상한 (df 오름차순)
MAX_DF_FRAC = 0.30        # 이보다 흔한 토큰은 버린다 (유일한 토큰이면 남긴다)
LEX_TAIL_W = 0.25         # page = 최고 block + 0.25 × 나머지 합
IDF_POW = 3.0             # 질문 토큰 가중 = idf^IDF_POW
CAND_PAGES = 200
W_ANCHOR = 1.35           # related/supersedes 간선을 든 block = 큐레이션된 주장
W_CURRENT = 1.04
W_CONFLICT = 0.97
# ------------------------------------------------------------------ 그래프층
SEEDS = 30
STEPS = 2
FRONTIER = 50
W_GRAPH = 0.85
STEP_DECAY = 0.5
EDGE_W = {"related": 1.0, "wiki": 0.15}   # 없는 kind 는 wiki 취급
HUB_DAMP = True
MAX_FANOUT = 64
# ------------------------------------------------------------------ 시간축·근거
STALE_SHOW = 0.30
MAX_EVIDENCE = 3
EVIDENCE_SKIP_KINDS = frozenset({"heading", "thematic_break"})
HEADING_SEP = " :: "
HEADING_PATH_SEP = " / "
CURATED = ("related", "supersedes")
# ------------------------------------------------------------------ 렌더
ROW_CHARS = 320           # block 하나의 본문 상한(글자)
ROW_MIN_TRUNC = 40        # 1위 행을 잘라 실을 때 남아 있어야 하는 최소 자리
PAGES = 10                # 그래프 투영에 넣는 page 수
CUT = 0.5                 # 1위 점수 대비 이 비율 아래인 page 는 싣지 않는다
MID_BODY_PAGES = 2
WEAK_LINES = 3
GRADES = ("strong", "mid", "weak", "none")
# 무주입 문턱(옵션). raw_top × coverage 가 이 값 미만이면 아무것도 넣지 않는다. 동결 자연 세트
# (bench/results_v3/calibration.json) 에서 고른 값이라 다른 위키에서는 그 위키의 질문 로그로
# 다시 잡아야 한다. 기본은 꺼져 있다(FINAL_PROPOSAL §4).
SILENCE_SIGNAL = "raw_x_cov"
SILENCE_T = 770.0

# 질문 기능어에서 나오는 2-gram. 순위에는 쓰지 않고 신호 `content_raw_top`·행 선택 가중에서만 뺀다.
STOP_WORDS = ["나요", "가요", "인가요", "인가", "무엇", "무엇인가요", "어떤", "어느", "언제", "얼마", "누구",
              "현재", "지금", "하나요", "되나요", "있나요", "까요", "인지", "는지", "무슨", "어떻게", "얼마인가요",
              "기준으로", "따르면", "몇", "무엇을", "어디", "어디에", "이며", "이고"]

_RUN = re.compile(r"[0-9a-z_][0-9a-z_.\-:/]*|[가-힣]+")
_TRIM = re.compile(r"[.\-:/]+$")
_SPLIT = re.compile(r"[_.\-:/]+")
_TABLE_SEP = re.compile(r"^\|?[\s\-:|]+\|?$")
_SENT = re.compile(r"(?<=[.!?。]) |(?<=다\.) ")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
LINK_SEPARATOR = re.compile(r"[\s_]+")
WIKILINK = re.compile(r"\[\[([^\]|#]+)(?:#([^\]|]+))?(?:\|([^\]]+))?\]\]")

# 이름이 붙은 자격증명: `password: x`, `"api_key": "x"`, `--token=x` 등.
SECRET = re.compile(r"(?i)(api[_-]?key|access[_-]?token|auth[_-]?token|password|passwd|"
                    r"secret|connection[_-]?string|private[_-]?key|client[_-]?secret|token)"
                    r"\"?\s*[:=]\s*\"?[^\s,;\"']+")
SECRET_MASK = r"\1: (접속 정보 생략)"
# 이름 없이 값만 나와도 자격증명인 것들 — 인증 헤더·PEM·알려진 토큰 접두·URL 계정·CLI 인자.
SECRET_EXTRA = (
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._\-+/=]{8,}"), r"\1 (접속 정보 생략)"),
    (re.compile(r"(?s)-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----"),
     "(접속 정보 생략)"),
    (re.compile(r"\b(sk-[A-Za-z0-9_\-]{16,}|gh[pousr]_[A-Za-z0-9]{16,}|"
                r"xox[baprs]-[A-Za-z0-9\-]{10,}|AIza[A-Za-z0-9_\-]{20,})"),
     "(접속 정보 생략)"),
    (re.compile(r"\b([a-z][a-z0-9+.\-]*://)[^\s/:@]+:[^\s/@]+@"), r"\1(접속 정보 생략)@"),
    (re.compile(r"(?i)(--?(?:password|passwd|token|api[_-]?key|secret|auth)\b)\s+\S+"),
     r"\1 (접속 정보 생략)"),
)

GRAPH_HEAD = (
    "<llmwiki-context v=3>\n"
    "정본(wiki/**/*.json) 부분 그래프. P=page(slug type updated src=근거 sources) "
    "B=block(<slug>#<id> 상태 | 본문. 긴 block 은 질문과 겹치는 행만, …=생략) E=간선(block kind→대상) "
    "A=주소만(본문 없음). 상태 cur=현재 주장, conflict=미판정 상충(양쪽 병기), "
    "sup→X=X 로 대체된 낡은 page(본문 생략, 인용 금지). "
    "근거 밖 내용은 모른다고 답하라. 더 필요하면 llmwiki_get(selector=\"<slug>#<id>\").\n")
WEAK_HEAD = (
    "<llmwiki-context v=3 weak>\n"
    "겹침이 약해 주소만. 답이 있다는 보장이 없다. 필요하면 llmwiki_get(selector=\"<slug>#<id>\").\n")
TAIL = "</llmwiki-context>"

SCHEMA = """
CREATE TABLE meta(k TEXT PRIMARY KEY, v) WITHOUT ROWID;
CREATE TABLE page(rid INTEGER PRIMARY KEY, page_id TEXT NOT NULL, slug TEXT, title TEXT, type TEXT,
                  created TEXT, updated TEXT, source TEXT, sha256 TEXT, projects TEXT, tags TEXT,
                  sources TEXT, summary TEXT, meta TEXT, head INTEGER, sup_block TEXT, sup_state TEXT,
                  unresolved INTEGER);
CREATE UNIQUE INDEX page_pid ON page(page_id);
CREATE INDEX page_slug ON page(slug);
CREATE INDEX page_title ON page(title);
CREATE TABLE lookup(key TEXT, prio INTEGER, page_id TEXT, rid INTEGER, PRIMARY KEY(key, prio, page_id)) WITHOUT ROWID;
CREATE INDEX lookup_rid ON lookup(rid);
CREATE TABLE blk(rid INTEGER PRIMARY KEY, prid INTEGER, block_id TEXT, kind TEXT, pos INTEGER,
                 ev INTEGER, unresolved INTEGER, text TEXT, refs TEXT, hpath TEXT, length INTEGER,
                 indexed INTEGER);
CREATE INDEX blk_bid ON blk(block_id);
CREATE INDEX blk_prid ON blk(prid);
CREATE TABLE post(term TEXT PRIMARY KEY, df INTEGER, ids BLOB, tfs BLOB, lens BLOB, flags BLOB) WITHOUT ROWID;
CREATE TABLE link(src INTEGER, ord INTEGER, key TEXT, kind TEXT, block_id TEXT, dst INTEGER,
                  PRIMARY KEY(src, ord)) WITHOUT ROWID;
CREATE INDEX link_key ON link(key);
CREATE INDEX link_dst ON link(dst);
CREATE INDEX link_sup ON link(kind) WHERE kind='supersedes';
CREATE INDEX link_block ON link(block_id);
CREATE TABLE adj(src INTEGER, dst INTEGER, w REAL, own_block TEXT, PRIMARY KEY(src, dst)) WITHOUT ROWID;
"""
TABLES = ("meta", "page", "lookup", "blk", "post", "link", "adj")


# ------------------------------------------------------------------ 공용 유틸
def norm(value: Any) -> str:
    return unicodedata.normalize("NFC", str(value or "")).strip()


def norm_ws(text: Any) -> str:
    return " ".join(str(text or "").split())


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def page_sha(page: dict[str, Any]) -> str:
    """`index/map.json` 의 `sha256` 과 같은 식 — llmwiki.sha(canonical(page))."""
    return hashlib.sha256(canonical(page).encode("utf-8")).hexdigest()


def redact(text: str) -> str:
    """자격증명처럼 보이는 값은 색인에도, 출력에도 남기지 않는다. 제어문자도 지운다."""
    text = _CONTROL.sub("", str(text or ""))
    text = SECRET.sub(SECRET_MASK, text)
    for pattern, mask in SECRET_EXTRA:
        text = pattern.sub(mask, text)
    return text


def est_tokens(text: str) -> int:
    """보수적 토큰 추정치: UTF-8 3바이트당 1토큰 (한글 1글자 = 1토큰으로 크게 잡는다)."""
    return math.ceil(len(text.encode("utf-8")) / 3)


def clip(text: str, limit: int) -> str:
    text = norm_ws(text)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def tokenize(text: str) -> list[str]:
    """한글 run → 음절 2-gram, 그 외 run → 낱말 + `_ . - : /` 로 쪼갠 조각(원 토큰 유지)."""
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
                parts = [p for p in _SPLIT.split(run) if len(p) >= 2]
                if len(parts) > 1:
                    out.extend(parts)
    return out


STOP_2G: frozenset[str] = frozenset(t for w in STOP_WORDS for t in tokenize(w))


def block_text(block: dict[str, Any]) -> str:
    """block 의 검색·표시용 본문. data.text/statement 가 있으면 그것, 없으면 source_text."""
    data = block.get("data") or {}
    if isinstance(data, dict):
        for key in ("text", "statement"):
            if isinstance(data.get(key), str) and data[key].strip():
                return data[key]
    text = block.get("source_text")
    return text if isinstance(text, str) else ""


def unresolved(block: dict[str, Any]) -> bool:
    resolution = block.get("resolution")
    status = resolution.get("status") if isinstance(resolution, dict) else None
    return block.get("kind") == "conflict" and status != "resolved"


def link_key(value: str) -> str:
    """대소문자·공백·`page:` 접두를 무시하는 비교 키 (llmwiki.link_key 와 같다)."""
    text = norm(value).casefold()
    if text.startswith("page:"):
        text = text[5:]
    return LINK_SEPARATOR.sub("-", text).strip("-")


def refs_in(text: str) -> list[str]:
    return sorted({norm(m.group(1)) for m in WIKILINK.finditer(text)})


def implied_links(page: dict[str, Any]) -> list[dict[str, Any]]:
    """선언된 links + block 본문의 [[위키링크]] + sources 의 page 참조 (llmwiki.implied_links 와 같다)."""
    links = [dict(link) for link in page.get("links") or [] if norm(link.get("target"))]
    seen = {(link_key(link["target"]), link.get("kind", "wiki")) for link in links}

    def add(target: str, kind: str, block_id: str = "") -> None:
        key = (link_key(target), kind)
        if not key[0] or key in seen:
            return
        seen.add(key)
        link = {"target": norm(target), "kind": kind}
        if block_id:
            link["block_id"] = block_id
        links.append(link)

    blocks = page.get("blocks") or {}
    for bid in page.get("block_order") or list(blocks):
        block = blocks.get(bid) or {}
        refs = block.get("refs")
        if refs is None:
            refs = refs_in(str(block.get("source_text", "")))
        for ref in refs:
            add(ref, "wiki", bid)
    for ref in page.get("sources") or []:
        prefix, sep, rest = norm(ref).partition(":")
        if sep and rest and prefix.lower() not in {"user", "raw"}:
            add(rest, "source")
    return links


def heading_paths(page: dict[str, Any]) -> dict[str, str]:
    """block id → 그 block 을 감싸는 heading 경로 (H6). heading block 자신은 ''."""
    blocks = page.get("blocks") or {}
    stack: list[tuple[int, str]] = []
    out: dict[str, str] = {}
    for bid in page.get("block_order") or list(blocks):
        b = blocks.get(bid)
        if not isinstance(b, dict):
            continue
        data = b.get("data") if isinstance(b.get("data"), dict) else {}
        if b.get("kind") == "heading":
            level = int(data.get("level") or 1)
            text = str(data.get("text") or block_text(b) or "")
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, text))
            out[str(b.get("id") or bid)] = ""
        else:
            out[str(b.get("id") or bid)] = HEADING_PATH_SEP.join(t for _l, t in stack)
    return out


def load_docs(wiki_dir: Path, root: Path | None = None) -> list[tuple[str, dict[str, Any]]]:
    """(root 상대 경로, page) 목록. 깨진 파일은 건너뛴다. build 에서만 쓴다."""
    wiki_dir = Path(wiki_dir)
    root = Path(root) if root else wiki_dir.parent
    docs: list[tuple[str, dict[str, Any]]] = []
    if not wiki_dir.is_dir():
        return docs
    for path in sorted(wiki_dir.rglob("*.json")):
        if path.name.startswith("."):
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        try:
            rel = path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            rel = path.as_posix()
        for page in value if isinstance(value, list) else [value]:
            if isinstance(page, dict) and page.get("id") and isinstance(page.get("blocks"), dict):
                docs.append((rel, page))
    docs.sort(key=lambda d: (str(d[1].get("id", "")), d[0]))
    return docs


# ------------------------------------------------------------------ rid
# page rid = page id 의 47비트 해시, block rid = page rid << 16 | (block_order 위치 + 1) (제목 가상 block 은 0).
# 배열 위치가 아니라 내용에서 나오므로 증분 갱신본과 cold build 의 표 내용이 같다 — 그래서
# PK 순으로 다시 써 publish 한 두 파일의 바이트가 같다. 47+16 = 63 비트라 sqlite INTEGER(부호 있는
# 64비트)와 array('q') 에 그대로 들어간다. 충돌은 build 가 오류로 낸다(10만 page 에서 확률 ~4e-5).
PRID_BITS = 47
BLOCK_BITS = 16
MAX_BLOCKS_PER_PAGE = (1 << BLOCK_BITS) - 1

# block 의 구조 신호는 posting 에 1바이트 flag 로 들어가고 조회 때 곱으로 푼다.
FLAG_ANCHOR, FLAG_CURRENT, FLAG_CONFLICT = 1, 2, 4


def _mult_of(flags: int) -> float:
    m = 1.0
    if flags & FLAG_ANCHOR:
        m *= W_ANCHOR
    if flags & FLAG_CURRENT:
        m *= W_CURRENT
    if flags & FLAG_CONFLICT:
        m *= W_CONFLICT
    return m


MULT = tuple(_mult_of(f) for f in range(8))


def page_rid(page_id: str) -> int:
    digest = hashlib.blake2b(str(page_id).encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") & ((1 << PRID_BITS) - 1)


def block_rid(prid: int, pos: int) -> int:
    """pos 는 block_order 위치(0부터), 제목 가상 block 은 -1."""
    return (prid << BLOCK_BITS) | (pos + 1)


def page_of(brid: int) -> int:
    return brid >> BLOCK_BITS


class IndexError_(ValueError):
    """증분 갱신이 할 수 없는 일(해시 충돌·중복 slug/block·문서 없음). 호출자는 cold build 로 떨어진다."""


# ------------------------------------------------------------------ 표 행 만들기
@dataclass
class PageRec:
    """정본 page 하나가 표에 남기는 행들. 디스크를 건드리지 않는 순수 변환의 결과다."""
    rid: int
    page_id: str
    page_row: tuple
    lookup_rows: list[tuple]                      # (key, prio, page_id, rid)
    links: list[tuple[str, str, str]]             # (key, kind, block_id) — ord 는 위치
    blocks: list[tuple]                           # blk 행 (flags 없이) + indexed 문자열
    posting: dict[str, list[tuple[int, int, int]]]  # term → [(brid, tf, len)] flags 는 나중에
    block_flags: dict[int, int]                   # brid → FLAG_CURRENT|FLAG_CONFLICT (anchor 는 해석 뒤)
    curated_blocks: dict[str, str]                # block_id → 그 block 이 든 큐레이션 link 의 key (첫 것)
    total_len: int
    n_indexed: int


def page_keys(page: dict[str, Any]) -> list[tuple[str, int]]:
    """(link_key, prio). id·slug 는 0, 제목은 1 — 제목은 id/slug 가 없을 때만 대상이 된다."""
    out: list[tuple[str, int]] = []
    seen: set[tuple[str, int]] = set()
    for key, prio in ((link_key(str(page.get("id") or "")), 0), (link_key(str(page.get("slug") or "")), 0),
                      (link_key(str(page.get("title") or "")), 1)):
        if key and (key, prio) not in seen:
            seen.add((key, prio))
            out.append((key, prio))
    return out


def page_record(rel: str, page: dict[str, Any], *, use_heading_paths: bool, sha256: str = "") -> PageRec:
    pid = str(page.get("id"))
    prid = page_rid(pid)
    blocks = page.get("blocks") or {}
    order = list(page.get("block_order") or list(blocks))
    if len(order) > MAX_BLOCKS_PER_PAGE:
        raise IndexError_(f"{pid}: {len(order)} blocks (max {MAX_BLOCKS_PER_PAGE})")
    links = [(link_key(str(l.get("target") or "")), str(l.get("kind") or "wiki"), str(l.get("block_id") or ""))
             for l in implied_links(page)]
    curated: dict[str, str] = {}
    for key, kind, bid in links:
        if kind in CURATED and bid and key:
            curated.setdefault(bid, key)
    blk_rows: list[tuple] = []
    posting: dict[str, list[tuple[int, int, int]]] = {}
    flags_of: dict[int, int] = {}
    total_len = 0
    n_indexed = 0

    def add_tokens(brid: int, toks: list[str]) -> None:
        nonlocal total_len, n_indexed
        counts: dict[str, int] = {}
        for t in toks:
            counts[t] = counts.get(t, 0) + 1
        for t, c in counts.items():
            posting.setdefault(t, []).append((brid, c, len(toks)))
        total_len += len(toks)
        n_indexed += 1

    title = redact(str(page.get("title") or ""))
    if title:                                     # 제목은 근거 없는 가상 block (ev=0)
        brid = block_rid(prid, -1)
        toks = tokenize(title)
        blk_rows.append((brid, prid, "", "title", -1, 0, 0, title, "[]", "", len(toks), 1))
        add_tokens(brid, toks)
        flags_of[brid] = 0
    paths = heading_paths(page) if use_heading_paths else {}
    bad = 0
    for pos, bid in enumerate(order):
        b = blocks.get(bid)
        if not isinstance(b, dict):
            continue
        brid = block_rid(prid, pos)
        bid = str(b.get("id") or bid)
        kind = str(b.get("kind") or "")
        unres = int(unresolved(b))
        bad += unres
        txt = block_text(b)
        refs = json.dumps(list(b.get("refs") or []), ensure_ascii=False)
        if not txt.strip():                       # 본문 없는 block 은 표에만 있고 posting 에는 없다
            blk_rows.append((brid, prid, bid, kind, pos, 0, unres, "", refs, "", 0, 0))
            continue
        txt = redact(txt)                         # 자격증명은 posting 에도 남지 않는다
        path = paths.get(bid, "")
        toks = tokenize(f"{path}{HEADING_SEP}{txt}" if path else txt)
        blk_rows.append((brid, prid, bid, kind, pos, 0 if kind in EVIDENCE_SKIP_KINDS else 1, unres,
                         txt, refs, path, len(toks), 1))
        add_tokens(brid, toks)
        flags_of[brid] = (FLAG_CURRENT if kind == "current" else 0) | (FLAG_CONFLICT if unres else 0)
    # 뷰어 파생물(catalog/graph) 이 정본 파일 없이 색인에서 나오도록 원문 값을 그대로 둔다
    meta_fields = {k: page.get(k) for k in ("title", "type", "created", "updated", "projects", "tags", "sources")}
    if "summary" in page:
        meta_fields["summary"] = page["summary"]
    meta = canonical(meta_fields)
    page_row = (prid, pid, str(page.get("slug") or ""), norm(page.get("title")), str(page.get("type") or ""),
                str(page.get("created") or ""), str(page.get("updated") or ""), rel, sha256 or page_sha(page),
                ",".join(page.get("projects") or []), ",".join(page.get("tags") or []),
                ",".join(page.get("sources") or []), redact(norm(page.get("summary"))), meta,
                prid, "", "", bad)
    lookup_rows = [(key, prio, pid, prid) for key, prio in page_keys(page)]
    return PageRec(prid, pid, page_row, lookup_rows, links, blk_rows, posting, flags_of, curated,
                   total_len, n_indexed)


def indexed_text(row: dict[str, Any]) -> str:
    """blk 행에서 색인 때 tokenize 한 문자열을 되살린다 — 옛 posting 을 지울 때 쓴다."""
    if row["hpath"]:
        return f"{row['hpath']}{HEADING_SEP}{row['text']}"
    return row["text"]


# ------------------------------------------------------------------ 해석 (link key → rid)
class Resolver:
    """lookup 표. 같은 key 는 id/slug(prio 0) 가 제목(prio 1) 보다, 같은 prio 면 page_id 사전순이 이긴다."""

    def __init__(self, db: sqlite3.Connection, *, preload: bool = False):
        self.db = db
        self.cache: dict[str, int | None] = {}
        self.table: dict[str, tuple[int, str, int]] | None = None
        if preload:
            table: dict[str, tuple[int, str, int]] = {}
            for key, prio, pid, rid in db.execute("SELECT key, prio, page_id, rid FROM lookup"):
                cur = table.get(key)
                if cur is None or (prio, pid) < (cur[0], cur[1]):
                    table[key] = (prio, pid, rid)
            self.table = table

    def __call__(self, key: str) -> int | None:
        if not key:
            return None
        if self.table is not None:
            hit = self.table.get(key)
            return hit[2] if hit else None
        if key in self.cache:
            return self.cache[key]
        row = self.db.execute("SELECT rid FROM lookup WHERE key=? ORDER BY prio, page_id LIMIT 1", (key,)).fetchone()
        self.cache[key] = row[0] if row else None
        return self.cache[key]


# ------------------------------------------------------------------ 델타
@dataclass
class Delta:
    added: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.added) + len(self.modified) + len(self.deleted)

    def __bool__(self) -> bool:
        return len(self) > 0


def map_delta(old_pages: dict[str, Any], new_pages: dict[str, Any]) -> Delta:
    """`index/map.json` 의 pages 두 판을 비교한다. sha 또는 source 가 다르면 modified, 파일 이동은 deleted+added 가
    아니라 같은 id 의 modified 다(id 가 정체성이다). 순수 함수."""
    delta = Delta()
    for pid in sorted(set(old_pages) | set(new_pages)):
        old, new = old_pages.get(pid), new_pages.get(pid)
        if old is None:
            delta.added.append(pid)
        elif new is None:
            delta.deleted.append(pid)
        elif (str(old.get("sha256") or ""), str(old.get("source") or "")) != \
                (str(new.get("sha256") or ""), str(new.get("source") or "")):
            delta.modified.append(pid)
    return delta


def _chunks(items: list, n: int = 900) -> Iterable[list]:
    for i in range(0, len(items), n):
        yield items[i:i + n]


def _in(db: sqlite3.Connection, sql: str, items: list, *, before: tuple = ()) -> list[tuple]:
    """`sql` 의 `{IN}` 자리에 items 를 900개씩 나눠 넣어 실행한다."""
    out: list[tuple] = []
    for chunk in _chunks(list(items)):
        out.extend(db.execute(sql.replace("{IN}", ",".join("?" * len(chunk))), (*before, *chunk)).fetchall())
    return out


def _neighbors(db: sqlite3.Connection, rids: set[int], n_pages: int) -> set[int]:
    """rids 와 해석된 link 로 이어진 page (양방향, 종류 무관, 자기 자신 제외)."""
    if not rids:
        return set()
    out: set[int] = set()
    if len(rids) * 2 >= max(n_pages, 1):
        rows = db.execute("SELECT src, dst FROM link WHERE dst IS NOT NULL").fetchall()
        for s, d in rows:
            if s in rids:
                out.add(d)
            if d in rids:
                out.add(s)
    else:
        items = sorted(rids)
        for s, d in _in(db, "SELECT src, dst FROM link WHERE dst IS NOT NULL AND src IN ({IN})", items):
            out.add(d)
        for s, d in _in(db, "SELECT src, dst FROM link WHERE dst IS NOT NULL AND dst IN ({IN})", items):
            out.add(s)
    return out - rids


BLK_COLS = ("rid", "prid", "block_id", "kind", "pos", "ev", "unresolved", "text", "refs", "hpath", "length", "indexed")


def apply_delta(db: sqlite3.Connection, docs_by_id: dict[str, tuple[str, dict[str, Any]]], delta: Delta, *,
                loader: Any = None, heading_paths: bool | None = None) -> dict[str, Any]:
    """델타를 한 트랜잭션(`BEGIN IMMEDIATE`) 으로 표에 반영한다.

    posting 은 base/delta 두 층이 아니라 term 행 하나를 제자리에서 고친다(brid 순 BLOB 에서 옛 항목을 빼고
    새 항목을 끼운다). df·평균 길이는 조회 때 live 로 계산하므로 cold build 와 점수가 같고, 표 내용이
    같으므로 publish 바이트도 같다. 돌려주는 것: {"touched": 간선이 바뀌었을 수 있는 page rid 집합(옛 이웃 포함),
    "reindexed": 해석이 바뀌어 다시 색인한 page 수, ...}. 호출자가 `refresh_graph(db, touched)` 를 이어 부른다.
    """
    meta = dict(db.execute("SELECT k, v FROM meta"))
    use_hp = str(meta.get("heading_paths") or "0") == "1"
    if heading_paths is not None and bool(heading_paths) != use_hp:
        raise IndexError_("heading_paths differs from the index")
    n_pages = int(meta.get("n_pages") or 0)
    n_blocks = int(meta.get("n_blocks") or 0)
    total_len = int(meta.get("total_len") or 0)

    db.execute("BEGIN IMMEDIATE")
    gone_ids = list(delta.deleted) + list(delta.modified)
    gone_rids = {page_rid(pid): pid for pid in gone_ids}
    for rid, pid in list(gone_rids.items()):      # 표에 있는 것만 지운다 (없는 id 는 무시)
        row = db.execute("SELECT page_id FROM page WHERE rid=?", (rid,)).fetchone()
        if row is None or row[0] != pid:
            del gone_rids[rid]
    new_recs: dict[int, PageRec] = {}
    for pid in list(delta.added) + list(delta.modified):
        if pid not in docs_by_id:
            raise IndexError_(f"{pid}: no document for the delta")
        rel, page = docs_by_id[pid]
        rec = page_record(rel, page, use_heading_paths=use_hp)
        if rec.rid in new_recs:
            raise IndexError_(f"page rid collision: {pid} vs {new_recs[rec.rid].page_id}")
        new_recs[rec.rid] = rec

    # 1. 옛 상태 — 간선(이웃)·해석 key. 본문만 바뀐 page(link 행이 그대로) 는 그래프를 건드리지 않는다.
    old_links: dict[int, list[tuple]] = {}
    old_chain: dict[int, tuple] = {}               # (head, sup_block, sup_state) — link 가 그대로면 그대로 둔다
    for rid in gone_rids:
        old_links[rid] = db.execute("SELECT ord, key, kind, block_id, dst FROM link WHERE src=? ORDER BY ord", (rid,)).fetchall()
        old_chain[rid] = db.execute("SELECT head, sup_block, sup_state FROM page WHERE rid=?", (rid,)).fetchone()
    changed_keys: set[str] = set()
    for rid in gone_rids:
        for (key,) in db.execute("SELECT key FROM lookup WHERE rid=?", (rid,)):
            changed_keys.add(key)
    for rec in new_recs.values():
        for key, _prio, _pid, _rid in rec.lookup_rows:
            changed_keys.add(key)

    # 2. lookup 갱신 — 해석은 이 뒤의 표 상태로 한다
    for rid in gone_rids:
        db.execute("DELETE FROM lookup WHERE rid=?", (rid,))
    for rec in new_recs.values():
        for row in rec.lookup_rows:
            db.execute("INSERT OR IGNORE INTO lookup VALUES(?,?,?,?)", row)
    remaining = n_pages - len(gone_rids) + len(new_recs)
    resolve = Resolver(db, preload=len(new_recs) * 4 >= max(remaining, 1))

    # 3. 바뀐 key 를 가리키던 다른 page 의 link 를 다시 해석한다. anchor(큐레이션 link 를 든 block) 가
    #    달라지면 그 page 의 posting flag 가 바뀌므로 통째로 다시 색인한다.
    dependents: dict[int, list[tuple[int, str, str, str, int | None]]] = {}
    dependents_touched: set[int] = set()           # 해석이 바뀐 page 와 그 옛·새 대상
    if changed_keys:
        rows = _in(db, "SELECT src, ord, key, kind, block_id, dst FROM link WHERE key IN ({IN})", sorted(changed_keys))
        for src, ordn, key, kind, bid, dst in rows:
            if src in gone_rids or src in new_recs:
                continue
            dependents.setdefault(src, []).append((ordn, key, kind, bid, dst))
    reindexed = 0
    for src, rows in sorted(dependents.items()):
        anchors_before: set[str] = set()
        anchors_after: set[str] = set()
        updates: list[tuple[int | None, int, int]] = []
        for ordn, key, kind, bid, dst in rows:
            new_dst = resolve(key)
            if new_dst == src:
                new_dst = None
            if new_dst != dst:
                dependents_touched.add(src)
                if dst is not None:
                    dependents_touched.add(dst)
                if new_dst is not None:
                    dependents_touched.add(new_dst)
            if kind in CURATED and bid:
                if dst is not None:
                    anchors_before.add(bid)
                if new_dst is not None:
                    anchors_after.add(bid)
            updates.append((new_dst, src, ordn))
        # anchor 는 page 의 모든 큐레이션 link 로 정해지므로 바뀐 key 의 link 만 봐도 차이는 정확하다
        if anchors_before != anchors_after:
            if loader is None:
                raise IndexError_(f"page rid {src}: anchors changed but no loader")
            prow = db.execute("SELECT page_id, source FROM page WHERE rid=?", (src,)).fetchone()
            page = loader(prow[0], prow[1])
            if not isinstance(page, dict) or str(page.get("id")) != prow[0]:
                raise IndexError_(f"{prow[0]}: document not found for re-index")
            rec = page_record(prow[1], page, use_heading_paths=use_hp)
            new_recs[src] = rec
            gone_rids[src] = prow[0]
            reindexed += 1
        else:
            db.executemany("UPDATE link SET dst=? WHERE src=? AND ord=?", updates)

    # 4. 지운다 — page/blk/link 행과 옛 posting 항목
    graph_changed: set[int] = set()                # link 행이 실제로 달라진 page
    removals: dict[str, set[int]] = {}
    for rid in sorted(gone_rids):
        for r in db.execute("SELECT %s FROM blk WHERE prid=?" % ",".join(BLK_COLS), (rid,)):
            row = dict(zip(BLK_COLS, r))
            if not row["indexed"]:
                continue
            for t in set(tokenize(indexed_text(row))):
                removals.setdefault(t, set()).add(row["rid"])
            n_blocks -= 1
            total_len -= int(row["length"] or 0)
        db.execute("DELETE FROM blk WHERE prid=?", (rid,))
        db.execute("DELETE FROM link WHERE src=?", (rid,))
        db.execute("DELETE FROM page WHERE rid=?", (rid,))
    n_pages -= len(gone_rids)

    # 5. 넣는다 — page/blk/link 행, 해석, anchor flag, 새 posting 항목
    additions: dict[str, list[tuple[int, int, int, int]]] = {}
    for rid in sorted(new_recs):
        rec = new_recs[rid]
        dup = db.execute("SELECT page_id FROM page WHERE rid=?", (rid,)).fetchone()
        if dup:
            raise IndexError_(f"page rid collision: {rec.page_id} vs {dup[0]}")
        if rec.page_row[2]:
            dup = db.execute("SELECT page_id FROM page WHERE slug=? AND rid!=?", (rec.page_row[2], rid)).fetchone()
            if dup:
                raise IndexError_(f"duplicate page slug: {rec.page_row[2]} ({rec.page_id}, {dup[0]})")
        db.execute("INSERT INTO page VALUES(%s)" % ",".join("?" * len(rec.page_row)), rec.page_row)
        link_rows = []
        anchors: set[str] = set()
        for ordn, (key, kind, bid) in enumerate(rec.links):
            dst = resolve(key)
            if dst == rid:
                dst = None
            link_rows.append((rid, ordn, key, kind, bid, dst))
            if dst is not None and kind in CURATED and bid:
                anchors.add(bid)
        db.executemany("INSERT INTO link VALUES(?,?,?,?,?,?)", link_rows)
        if [r[1:] for r in link_rows] != [tuple(r) for r in old_links.get(rid, [])]:
            graph_changed.add(rid)
        elif rid in old_chain and old_chain[rid]:
            # 본문만 바뀐 page: supersedes 체인 자리(head·sup_block·sup_state) 는 refresh 없이 그대로 잇는다
            db.execute("UPDATE page SET head=?, sup_block=?, sup_state=? WHERE rid=?", (*old_chain[rid], rid))
        for row in rec.blocks:
            if row[2]:
                dup = db.execute("SELECT prid FROM blk WHERE block_id=? AND prid!=?", (row[2], rid)).fetchone()
                if dup:
                    raise IndexError_(f"duplicate block id {row[2]} ({rec.page_id})")
        db.executemany("INSERT INTO blk VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", rec.blocks)
        flag_by_bid = {row[2]: row[0] for row in rec.blocks}
        flags = dict(rec.block_flags)
        for bid in anchors:
            brid = flag_by_bid.get(bid)
            if brid is not None and brid in flags:
                flags[brid] |= FLAG_ANCHOR
        for term, entries in rec.posting.items():
            lst = additions.setdefault(term, [])
            for brid, tf, ln in entries:
                lst.append((brid, tf, ln, flags[brid]))
        n_blocks += rec.n_indexed
        total_len += rec.total_len
    n_pages += len(new_recs)

    # 6. posting — term 행을 제자리에서 고친다
    _merge_postings(db, removals, additions)

    # 7. meta
    noev = array("q", [r[0] for r in db.execute("SELECT rid FROM blk WHERE indexed=1 AND ev=0 ORDER BY rid")])
    db.executemany("INSERT OR REPLACE INTO meta VALUES(?,?)", [
        ("n_pages", str(n_pages)), ("n_blocks", str(n_blocks)), ("total_len", str(total_len)),
        ("noev", noev.tobytes())])
    for rid in gone_rids:                          # 지워졌거나 (new_recs 에 없거나) link 가 달라진 page
        if rid not in new_recs:
            graph_changed.add(rid)
    old_nbrs_of_changed = _neighbors(db, graph_changed, n_pages)   # 새 이웃 — 옛 이웃은 아래
    touched = graph_changed | old_nbrs_of_changed | dependents_touched
    for rid in graph_changed:
        touched.update(d for _o, _k, _kind, _b, d in old_links.get(rid, []) if d is not None)
    return {"touched": touched, "added": len(delta.added), "modified": len(delta.modified),
            "deleted": len(delta.deleted), "reindexed": reindexed, "terms_touched": len(set(removals) | set(additions)),
            "n_pages": n_pages, "n_blocks": n_blocks}


def _decode(row: tuple | None) -> tuple[array, array, array, array]:
    ids, tfs, lens, flags = array("q"), array("H"), array("H"), array("B")
    if row:
        ids.frombytes(row[0])
        tfs.frombytes(row[1])
        lens.frombytes(row[2])
        flags.frombytes(row[3])
    return ids, tfs, lens, flags


def _merge_postings(db: sqlite3.Connection, removals: dict[str, set[int]],
                    additions: dict[str, list[tuple[int, int, int, int]]]) -> None:
    from bisect import bisect_left
    for term in sorted(set(removals) | set(additions)):
        row = db.execute("SELECT ids, tfs, lens, flags FROM post WHERE term=?", (term,)).fetchone()
        ids, tfs, lens, flags = _decode(row)
        rem = removals.get(term)
        adds = additions.get(term)
        if rem:
            if len(rem) * 32 < len(ids):              # 몇 개만 뺄 때는 C 탐색으로 자리를 찾아 지운다
                for brid in rem:
                    try:
                        i = ids.index(brid)
                    except ValueError:
                        continue
                    del ids[i], tfs[i], lens[i], flags[i]
            else:
                keep = [i for i, b in enumerate(ids) if b not in rem]
                ids = array("q", [ids[i] for i in keep])
                tfs = array("H", [tfs[i] for i in keep])
                lens = array("H", [lens[i] for i in keep])
                flags = array("B", [flags[i] for i in keep])
        if adds:
            if len(adds) <= 8 or len(adds) * 16 < len(ids):
                for brid, tf, ln, fl in adds:
                    i = bisect_left(ids, brid)
                    if i < len(ids) and ids[i] == brid:   # 같은 brid 가 남아 있으면 교체
                        tfs[i], lens[i], flags[i] = tf, ln, fl
                        continue
                    ids.insert(i, brid)
                    tfs.insert(i, tf)
                    lens.insert(i, ln)
                    flags.insert(i, fl)
            else:
                merged = {b: (t, l, f) for b, t, l, f in zip(ids, tfs, lens, flags)}
                for brid, tf, ln, fl in adds:
                    merged[brid] = (tf, ln, fl)
                keys = sorted(merged)
                ids = array("q", keys)
                tfs = array("H", [merged[b][0] for b in keys])
                lens = array("H", [merged[b][1] for b in keys])
                flags = array("B", [merged[b][2] for b in keys])
        if len(ids):
            db.execute("INSERT OR REPLACE INTO post VALUES(?,?,?,?,?,?)",
                       (term, len(ids), ids.tobytes(), tfs.tobytes(), lens.tobytes(), flags.tobytes()))
        elif row is not None:
            db.execute("DELETE FROM post WHERE term=?", (term,))


# ------------------------------------------------------------------ 그래프층·시간축 갱신
def refresh_graph(db: sqlite3.Connection, touched: set[int]) -> dict[str, int]:
    """`adj`(간선 종류별 가중·허브 감쇠·fanout 상한) 와 supersedes head 를 touched 주변만 다시 계산한다.

    touched 는 link 가 바뀐 page 와 그 옛·새 이웃이다. 간선 (a,b) 의 가중은 a–b 사이 간선과 b 의 약한
    in-degree 에만 달려 있다. 그래서 E = touched ∪ 이웃 (간선 집합이나 약한 in-degree 가 바뀐 끝점) 의
    행은 통째로 다시 쓰고, E 의 바깥 이웃 a 의 행 (a, b∈E) 는 감쇠 가중과 own_block 만 제자리에서
    고친다 — a 의 fanout 이 상한(MAX_FANOUT) 에 걸려 있으면 순서가 바뀔 수 있으니 그때만 a 도 통째로.
    허브(in-degree 수천) 에 간선 하나가 붙어도 허브 이웃 전부를 다시 계산하지 않는다.
    supersedes head 는 touched 가 속한 체인 성분만 다시 걷는다.
    """
    n_pages = int(dict(db.execute("SELECT k, v FROM meta WHERE k='n_pages'")).get("n_pages") or 0)
    ends = set(touched)                           # apply_delta 가 이미 옛·새 이웃까지 넣어 준다 = 바뀐 간선의 끝점
    whole = len(ends) * 2 >= max(n_pages, 1)
    outer: set[int] = set()
    if whole:                                     # 절반이 넘으면 전체를 다시 쓰는 편이 싸다
        region = {r[0] for r in db.execute("SELECT rid FROM page")}
        rows = db.execute("SELECT src, ord, dst, kind, block_id FROM link WHERE dst IS NOT NULL ORDER BY src, ord").fetchall()
    else:
        outer = _neighbors(db, ends, n_pages)
        big = {a for a, c in _in(db, "SELECT src, COUNT(*) FROM adj WHERE src IN ({IN}) GROUP BY src", sorted(outer))
               if c >= MAX_FANOUT}
        region = ends | big
        outer -= big
        items = sorted(region)
        rows = _in(db, "SELECT src, ord, dst, kind, block_id FROM link WHERE dst IS NOT NULL AND src IN ({IN})", items)
        rows += _in(db, "SELECT src, ord, dst, kind, block_id FROM link WHERE dst IS NOT NULL AND dst IN ({IN})", items)
        rows = sorted(set(rows))
    # 약한 in-degree: related·supersedes 가 아닌 해석된 간선이 닿는 수 (양 끝 모두, 간선마다)
    weak_in: dict[int, int] = {}
    if whole:
        for s, _o, d, kind, _b in rows:
            if kind not in ("related", "supersedes"):
                weak_in[s] = weak_in.get(s, 0) + 1
                weak_in[d] = weak_in.get(d, 0) + 1
    else:
        need = sorted({x for s, _o, d, _k, _b in rows for x in (s, d)})
        if len(need) * 5 >= max(n_pages, 1):      # 필요한 page 가 많으면 한 번에 세는 편이 싸다
            weak_in = dict(db.execute(
                "SELECT p, COUNT(*) FROM (SELECT src AS p FROM link WHERE dst IS NOT NULL AND kind NOT IN ('related','supersedes') "
                "UNION ALL SELECT dst FROM link WHERE dst IS NOT NULL AND kind NOT IN ('related','supersedes')) GROUP BY p").fetchall())
        else:                                     # 적으면 PK(src)·link_dst 색인으로 그 page 만 센다
            for chunk in _chunks(need):
                marks = ",".join("?" * len(chunk))
                for p, n in db.execute(
                        f"SELECT p, COUNT(*) FROM (SELECT src AS p FROM link WHERE src IN ({marks}) AND dst IS NOT NULL "
                        f"AND kind NOT IN ('related','supersedes') UNION ALL SELECT dst FROM link WHERE dst IN ({marks}) "
                        f"AND kind NOT IN ('related','supersedes')) GROUP BY p", (*chunk, *chunk)):
                    weak_in[p] = n
    own: dict[tuple[int, int], str] = {}
    pair_w: dict[tuple[int, int], float] = {}
    for s, _o, d, kind, bid in rows:                 # rows 는 (src, ord) 순 — own 은 첫 link
        own.setdefault((s, d), bid)
        if kind == "supersedes":
            continue
        w = EDGE_W.get(kind, EDGE_W["wiki"])
        for a, b in ((s, d), (d, s)):
            pair_w[(a, b)] = max(pair_w.get((a, b), 0.0), w)

    def damped(w: float, b: int) -> float:
        if HUB_DAMP and w < 1.0:
            return w / (1.0 + math.log(weak_in.get(b) or 1))
        return w

    by_src: dict[int, list[tuple[float, int]]] = {}
    patches: list[tuple[float, str, int, int]] = []
    for (a, b), w in pair_w.items():
        if a in region:
            by_src.setdefault(a, []).append((-round(damped(w, b), 6), b))
        elif a in outer and b in ends:               # 바깥 이웃: 감쇠와 own_block 만 제자리에서
            patches.append((round(damped(w, b), 6), own.get((b, a), ""), a, b))
    if whole:
        db.execute("DELETE FROM adj")
    else:
        for chunk in _chunks(sorted(region)):
            db.execute("DELETE FROM adj WHERE src IN (%s)" % ",".join("?" * len(chunk)), chunk)
    adj_rows = []
    for a in sorted(by_src):
        for nw, b in sorted(by_src[a])[:MAX_FANOUT]:
            adj_rows.append((a, b, -nw, own.get((b, a), "")))
    db.executemany("INSERT INTO adj VALUES(?,?,?,?)", adj_rows)
    if patches:
        db.executemany("UPDATE adj SET w=?, own_block=? WHERE src=? AND dst=?", patches)

    # 시간축: touched 가 속한 supersedes 성분만 다시 접는다. fork·cycle 은 접지 않고 상태로 남긴다.
    sup = db.execute("SELECT src, ord, dst, block_id FROM link WHERE kind='supersedes' AND dst IS NOT NULL ORDER BY src, ord").fetchall()
    undirected: dict[int, set[int]] = {}
    for s, _o, d, _b in sup:
        undirected.setdefault(s, set()).add(d)
        undirected.setdefault(d, set()).add(s)
    comp: set[int] = set()
    stack = [p for p in touched if p in undirected]
    while stack:
        p = stack.pop()
        if p in comp:
            continue
        comp.add(p)
        stack.extend(undirected.get(p, ()))
    members = comp | set(touched)
    succ: dict[int, list[int]] = {}
    sup_block: dict[int, str] = {}
    for s, _o, d, bid in sup:
        if s in members and d in members:
            lst = succ.setdefault(d, [])
            if s not in lst:
                lst.append(s)
            sup_block.setdefault(s, bid)
    head: dict[int, int] = {}
    state: dict[int, str] = {}
    for start in sorted(succ):
        cur, seen = start, [start]
        forked = False
        while cur in succ:
            nxt = sorted(succ[cur])
            if len(nxt) > 1:
                forked = True
            cur = nxt[0]
            if cur in seen:
                break
            seen.append(cur)
        if cur in seen[:-1] or cur == start:          # cycle: 최신판을 정할 수 없다
            for member in seen:
                head[member] = member
                state[member] = "cycle"
            continue
        head[start] = cur
        state[start] = "fork" if forked else "stale"
    db.executemany("UPDATE page SET head=?, sup_block=?, sup_state=? WHERE rid=?",
                   [(head.get(m, m), sup_block.get(m, ""), state.get(m, ""), m) for m in sorted(members)])
    return {"adj_rows": len(adj_rows), "region": len(region), "patched": len(patches), "chain_members": len(members)}


# ------------------------------------------------------------------ build · compact · publish
def _new_db(path: Path | str, *, revision: str, heading_paths: bool, map_root: str = "") -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=OFF")
    db.execute("PRAGMA synchronous=OFF")
    db.executescript(SCHEMA)
    db.executemany("INSERT INTO meta VALUES(?,?)", [
        ("schema", SCHEMA_VERSION), ("revision", revision), ("map_root", map_root),
        ("n_pages", "0"), ("n_blocks", "0"), ("total_len", "0"),
        ("heading_paths", "1" if heading_paths else "0"), ("noev", b"")])
    db.commit()
    return db


def fill(db: sqlite3.Connection, docs: Iterable[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    """빈 표에 정본 전체를 넣는다 — 증분과 같은 코드(apply_delta + refresh_graph) 를 '전부 추가' 로 돈다."""
    docs = sorted(docs, key=lambda d: (str(d[1].get("id", "")), d[0]))
    by_id: dict[str, tuple[str, dict[str, Any]]] = {}
    for rel, page in docs:
        pid = str(page.get("id"))
        if pid in by_id:
            raise IndexError_(f"duplicate page id: {pid}")
        by_id[pid] = (rel, page)
    stats = apply_delta(db, by_id, Delta(added=sorted(by_id)))
    graph = refresh_graph(db, stats["touched"])
    db.commit()
    stats.update(graph)
    return stats


def set_meta(db: sqlite3.Connection, **values: Any) -> None:
    db.executemany("INSERT OR REPLACE INTO meta VALUES(?,?)", [(k, v) for k, v in values.items()])


COMPACT_FREELIST = 0.10


def compact(db: sqlite3.Connection, *, force: bool = False) -> bool:
    """작업 DB 의 free page 가 10% 를 넘으면 VACUUM 한다. posting 은 제자리 교체라 delta/tomb 층이 없고,
    자리 낭비는 free page 로만 나타난다. build 안에서 foreground 로만 돈다."""
    free = db.execute("PRAGMA freelist_count").fetchone()[0]
    total = db.execute("PRAGMA page_count").fetchone()[0]
    if not force and (total == 0 or free / total <= COMPACT_FREELIST):
        return False
    db.execute("VACUUM")
    return True


def open_work(path: Path) -> sqlite3.Connection:
    """작업 DB(WAL). build 만 연다 — writer 는 한 번에 하나(BEGIN IMMEDIATE), 훅은 publish 본만 읽는다."""
    db = sqlite3.connect(path, timeout=10.0)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.execute("PRAGMA busy_timeout=10000")
    return db


DIGEST_ORDER = {"meta": "k", "page": "rid", "lookup": "key,prio,page_id", "blk": "rid", "post": "term",
                "link": "src,ord", "adj": "src,dst"}


def _pub_ddl() -> list[str]:
    """SCHEMA 를 `pub.` 스키마에 그대로 만드는 문장 목록 — 같은 순서라 sqlite_master·schema cookie 가 고정이다."""
    out: list[str] = []
    for stmt in SCHEMA.split(";"):
        stmt = " ".join(stmt.split())
        if not stmt:
            continue
        stmt = re.sub(r"^CREATE TABLE (\w+)", r"CREATE TABLE pub.\1", stmt)
        stmt = re.sub(r"^CREATE (UNIQUE )?INDEX (\w+)", r"CREATE \1INDEX pub.\2", stmt)
        out.append(stmt)
    return out


PUB_DDL = _pub_ddl()


def publish(db: sqlite3.Connection, path: Path) -> str:
    """작업 DB → 새 파일에 DDL 고정 순서로 표를 만들고 PK 순으로 행을 복사 → `os.replace`.
    훅은 반쯤 만들어진 파일을 볼 수 없다.

    같은 표 내용이면 헤더까지 같은 바이트다 — 모든 표가 WITHOUT ROWID 또는 내용 해시 rid 라 행 순서가
    이력과 무관하고, 발행본은 매번 빈 파일에서 같은 DDL·같은 행 순서로 다시 쓴다. `VACUUM INTO` 를 쓰지
    않는 이유: 그것은 원본의 schema cookie(+1)를 헤더에 복사해 작업 DB 의 compact(VACUUM) 횟수가 발행본
    바이트에 남았다(grok s7). 돌려주는 것은 발행본 바이트의 sha256 (`revision.json.search_root`).
    표 내용 지문이 따로 필요하면 `logical_digest` — 10,000 page 에서 0.3 초라 publish 마다 계산하지 않는다.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    out = path.with_name(path.name + ".tmp")
    for suffix in ("", "-journal", "-wal", "-shm"):
        try:
            os.unlink(str(out) + suffix)
        except FileNotFoundError:
            pass
    if db.in_transaction:
        db.commit()
    page_size = int(db.execute("PRAGMA main.page_size").fetchone()[0])
    db.execute("ATTACH DATABASE ? AS pub", (str(out),))
    try:
        db.execute(f"PRAGMA pub.page_size={page_size}")
        db.execute("PRAGMA pub.journal_mode=OFF")
        db.execute("PRAGMA pub.synchronous=OFF")
        db.execute("BEGIN")
        try:
            for stmt in PUB_DDL:
                db.execute(stmt)
            for table in TABLES:
                # ORDER BY 없는 `INSERT … SELECT *` 라야 sqlite 가 b-tree 를 키 순으로 직접 옮긴다(xfer 최적화 —
                # 인덱스도 원본 인덱스 순으로 채워 VACUUM 과 같은 크기·속도). 순회가 PK 순이므로 결정적이다.
                db.execute(f"INSERT INTO pub.{table} SELECT * FROM main.{table}")
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
    finally:
        db.execute("DETACH DATABASE pub")
    pub = sqlite3.connect(out)                     # publish 본은 WAL 로 표시해 둔다(훅은 immutable 로 읽는다)
    try:
        pub.execute("PRAGMA journal_mode=WAL")
        pub.commit()
    finally:
        pub.close()
    for target in (out, path):
        for suffix in ("-wal", "-shm"):
            try:
                os.unlink(str(target) + suffix)
            except FileNotFoundError:
                pass
    os.replace(out, path)
    return file_digest(path)


def work_path(path: Path) -> Path:
    """publish 경로 → 작업 DB 경로: search.sqlite → search.work.sqlite (bench 의 structural3.db → structural3.work.db)."""
    path = Path(path)
    return path.with_name(f"{path.stem}.work{path.suffix}")


def build(docs: Iterable[tuple[str, dict[str, Any]]], path: Path, *, revision: str = "",
          heading_paths: bool = False, map_root: str = "") -> dict[str, Any]:
    """cold build: 작업 DB 를 새로 만들어 채우고 `path` 로 publish 한다. 두 번 돌리면 같은 바이트다."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    work = work_path(path)
    for suffix in ("", "-journal", "-wal", "-shm"):
        try:
            os.unlink(str(work) + suffix)
        except FileNotFoundError:
            pass
    db = _new_db(work, revision=revision, heading_paths=heading_paths, map_root=map_root)
    try:
        stats = fill(db, docs)
        digest = publish(db, path)
        db.execute("PRAGMA journal_mode=WAL")          # 다음 증분 갱신을 위한 작업 DB
        db.commit()
    finally:
        db.close()
    return {"pages": stats["n_pages"], "blocks": stats["n_blocks"], "terms": stats["terms_touched"],
            "adj_rows": stats["adj_rows"], "heading_paths": heading_paths,
            "bytes": path.stat().st_size, "digest": digest, "mode": "full"}


def update(path: Path, docs_by_id: dict[str, tuple[str, dict[str, Any]]], delta: Delta, *, revision: str,
           map_root: str, expect_map_root: str, loader: Any = None, heading_paths: bool = False) -> dict[str, Any]:
    """증분 build: 작업 DB 가 `expect_map_root` 상태일 때만 델타를 반영하고 publish 한다.

    작업 DB 가 없거나 다른 map 에서 왔거나 표가 다르면 IndexError_ — 호출자가 cold build 로 떨어진다.
    `meta.map_root` 는 델타와 같은 트랜잭션에 들어가지만, 호출자는 publish 가 성공한 뒤에만
    `index/map.json`·`revision.json` 을 쓴다. publish 가 실패하면 다음 build 가 불일치를 보고 cold 로 간다.
    """
    path = Path(path)
    work = work_path(path)
    if not work.is_file():
        raise IndexError_("no work db")
    db = open_work(work)
    try:
        meta = dict(db.execute("SELECT k, v FROM meta"))
        if str(meta.get("schema") or "") != SCHEMA_VERSION:
            raise IndexError_("work db schema differs")
        if str(meta.get("map_root") or "") != expect_map_root:
            raise IndexError_("work db is not the published index")
        stats = apply_delta(db, docs_by_id, delta, loader=loader, heading_paths=heading_paths)
        graph = refresh_graph(db, stats["touched"])
        set_meta(db, revision=revision, map_root=map_root)
        db.commit()
        compacted = compact(db)
        digest = publish(db, path)
    except sqlite3.Error as exc:
        try:
            db.rollback()
        except sqlite3.Error:
            pass
        raise IndexError_(f"sqlite: {exc}") from exc
    finally:
        db.close()
    stats.update(graph)
    stats.pop("touched", None)
    return {**stats, "pages": stats["n_pages"], "blocks": stats["n_blocks"], "compacted": compacted,
            "bytes": path.stat().st_size, "digest": digest, "mode": "incremental"}


def build_memory(docs: Iterable[tuple[str, dict[str, Any]]], *, revision: str = "",
                 heading_paths: bool = False) -> "Index":
    """디스크에 쓰지 않는 색인 (llmwiki.py query 와 훅의 메모리 폴백이 정본에서 바로 만든다)."""
    db = _new_db(":memory:", revision=revision, heading_paths=heading_paths)
    fill(db, docs)
    return Index(db, str(revision))


def logical_digest(db: sqlite3.Connection) -> str:
    """표를 PK 순으로 canonical 직렬화한 sha256. sqlite 버전·페이지 배치와 무관한 내용 지문.

    직렬화는 sqlite 의 `quote()` (문자열은 '' 인용, BLOB 은 X'..' 16진, NULL 은 NULL, 실수는 %!.15g) 로
    C 쪽에서 하고 Python 은 행 단위로 해시만 한다 — 10,000 page 에서 0.1 초대.
    """
    h = hashlib.sha256()
    for table in TABLES:
        cols = [r[1] for r in db.execute(f"PRAGMA table_info({table})")]
        expr = " || char(31) || ".join(f"quote({c})" for c in cols)
        h.update(table.encode("utf-8"))
        for (line,) in db.execute(f"SELECT {expr} FROM {table} ORDER BY {DIGEST_ORDER[table]}"):
            h.update(line.encode("utf-8"))
            h.update(b"\x1e")
    return h.hexdigest()


def file_digest(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ------------------------------------------------------------------ 조회
PAGE_COLS = ("rid", "page_id", "slug", "title", "type", "created", "updated", "source", "sha256", "projects",
             "tags", "sources", "summary", "meta", "head", "sup_block", "sup_state", "unresolved")


@dataclass
class Hit:
    page_id: str
    score: float
    block_ids: list[str] = field(default_factory=list)
    head: str = ""                # head page id (자기 자신이면 낡지 않은 page)
    sup_state: str = ""           # '' | stale | fork | cycle
    slug: str = ""


@dataclass
class SearchResult:
    hits: list[Hit]
    signals: dict[str, float]


class Index:
    """열린 색인. 훅은 `open_ro` 로, build 는 `build_memory` 로 얻는다."""

    def __init__(self, db: sqlite3.Connection, revision: str = "", path: Path | None = None):
        self.db = db
        self.path = path
        meta = dict(db.execute("SELECT k, v FROM meta"))
        self.schema = str(meta.get("schema") or "")
        self.revision = str(meta.get("revision") or revision or "")
        self.heading_paths = str(meta.get("heading_paths") or "0") == "1"
        self.map_root = str(meta.get("map_root") or "")
        self.nblocks = int(meta.get("n_blocks") or 1)
        self.npages = int(meta.get("n_pages") or 0)
        total_len = int(meta.get("total_len") or 0)
        # 평균 길이·df 는 live 값 — cold build 와 증분본이 같은 점수를 낸다
        self.avg_len = (total_len / self.nblocks) if self.nblocks and total_len else 1.0
        noev = array("q")
        noev.frombytes(meta.get("noev") or b"")
        self.noev: frozenset[int] = frozenset(noev)
        self.last_signals: dict[str, float] = {}
        self._sender: dict[int, tuple[float, int]] = {}

    def close(self) -> None:
        try:
            self.db.close()
        except sqlite3.Error:
            pass

    # ---------------------------------------------------------- 어휘층
    def term_idf(self, terms: list[str]) -> dict[str, float]:
        if not terms:
            return {}
        out: dict[str, float] = {}
        n = self.nblocks
        for i in range(0, len(terms), 900):
            chunk = terms[i:i + 900]
            df = dict(self.db.execute("SELECT term, df FROM post WHERE term IN (%s)"
                                      % ",".join("?" * len(chunk)), chunk))
            for t in chunk:
                out[t] = math.log(1.0 + (n - df.get(t, 0) + 0.5) / (df.get(t, 0) + 0.5))
        return out

    def _lex(self, query: str) -> tuple[dict[int, float], dict[int, list[tuple[float, int]]]]:
        terms = sorted(set(tokenize(query)))
        n = self.nblocks
        sig = {"n_terms": float(len(terms)), "posted_terms": 0.0, "coverage": 0.0, "idf_coverage": 0.0,
               "raw_top": 0.0, "content_raw_top": 0.0, "top_block_impact": 0.0, "top1_best_block": 0.0,
               "top1_n_blocks": 0.0}
        self.last_signals = sig
        if not terms:
            return {}, {}
        rows: list[tuple] = []
        for i in range(0, len(terms), 900):
            chunk = terms[i:i + 900]
            q = "SELECT term, df, ids, tfs, lens, flags FROM post WHERE term IN (%s)" % ",".join("?" * len(chunk))
            rows.extend(self.db.execute(q, chunk).fetchall())
        rows.sort(key=lambda r: (r[1], r[0]))
        df_of = {r[0]: r[1] for r in rows}
        idf_all = {t: math.log(1.0 + (n - df_of.get(t, 0) + 0.5) / (df_of.get(t, 0) + 0.5)) for t in terms}
        sig["posted_terms"] = float(len(rows))
        sig["coverage"] = len(rows) / len(terms)
        sig["idf_coverage"] = sum(idf_all[t] for t in df_of) / (sum(idf_all.values()) or 1.0)
        cap = MAX_DF_FRAC * n
        kept = [r for r in rows if r[1] <= cap] or rows[:1]
        blk: dict[int, float] = {}
        blk_c: dict[int, float] = {}
        k1p = BM25_K1 + 1.0
        kc = BM25_K1 * (1.0 - BM25_B)
        kl = BM25_K1 * BM25_B / self.avg_len
        mult = MULT
        for term, _df, ids_b, tfs_b, lens_b, flags_b in kept[:MAX_QUERY_TERMS]:
            ids, tfs, lens, flags = _decode((ids_b, tfs_b, lens_b, flags_b))
            idf = idf_all[term]
            # live impact: build 때 굽지 않고 여기서 계산한다 — 증분 갱신 뒤에도 cold build 와 같은 값
            scored = [(idf * tf * k1p / (tf + kc + kl * ln) * mult[fl], brid)
                      for brid, tf, ln, fl in zip(ids, tfs, lens, flags)]
            if len(scored) > MAX_POST:
                scored = heapq.nlargest(MAX_POST, scored, key=lambda x: (x[0], -x[1]))
            qw = idf ** (IDF_POW - 1.0)
            content = term not in STOP_2G
            for imp, brid in scored:
                v = imp * qw
                blk[brid] = blk.get(brid, 0.0) + v
                if content:
                    blk_c[brid] = blk_c.get(brid, 0.0) + v
        if not blk:
            return {}, {}
        page_blocks: dict[int, list[tuple[float, int]]] = {}
        for brid, sc in blk.items():
            page_blocks.setdefault(brid >> BLOCK_BITS, []).append((sc, brid))
        page_lex: dict[int, float] = {}
        for prid, bl in page_blocks.items():
            bl.sort(reverse=True)
            page_lex[prid] = bl[0][0] + LEX_TAIL_W * sum(s for s, _ in bl[1:])
        if blk_c:
            pc: dict[int, list[float]] = {}
            for brid, sc in blk_c.items():
                pc.setdefault(brid >> BLOCK_BITS, []).append(sc)
            sig["content_raw_top"] = max(max(v) + LEX_TAIL_W * (sum(v) - max(v)) for v in pc.values())
        top = max(page_lex, key=lambda p: (page_lex[p], -p))
        sig["raw_top"] = page_lex[top]
        sig["top_block_impact"] = max(blk.values())
        sig["top1_best_block"] = page_blocks[top][0][0]
        sig["top1_n_blocks"] = float(len(page_blocks[top]))
        return page_lex, page_blocks

    # --------------------------------------------------------- 그래프층
    def _ppr(self, page_lex: dict[int, float], top: float
             ) -> tuple[dict[int, float], dict[int, tuple[float, str]]]:
        seeds = sorted(page_lex, key=lambda p: (-page_lex[p], p))[:SEEDS]
        mass = {p: page_lex[p] / top for p in seeds}
        score: dict[int, float] = {}
        via: dict[int, tuple[float, str]] = {}
        self._sender = {}
        came: dict[int, set[int]] = {}
        for step in range(STEPS):
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
                    nxt[dst] = max(nxt.get(dst, 0.0), m)
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

    def _page_info(self, rids: list[int]) -> dict[int, tuple[str, int, str, str, str]]:
        info: dict[int, tuple[str, int, str, str, str]] = {}
        for i in range(0, len(rids), 900):
            chunk = rids[i:i + 900]
            for rid, pid, head, sb, st, slug in self.db.execute(
                    "SELECT rid,page_id,head,sup_block,sup_state,slug FROM page WHERE rid IN (%s)"
                    % ",".join("?" * len(chunk)), chunk):
                info[rid] = (pid, head, sb, st, slug)
        return info

    # ------------------------------------------------------------ search
    def search(self, query: str, k: int = PAGES, *, fold: bool = True) -> SearchResult:
        page_lex, page_blocks = self._lex(query)
        if not page_lex:
            self.last_signals["top_score"] = 0.0
            self.last_signals["second_score"] = 0.0
            return SearchResult([], dict(self.last_signals))
        noev = self.noev
        if noev:
            for prid in list(page_blocks):
                page_blocks[prid] = [x for x in page_blocks[prid] if x[1] not in noev]
        top = max(page_lex.values()) or 1.0
        cand = sorted(page_lex, key=lambda p: (-page_lex[p], p))[:CAND_PAGES]
        score = {p: page_lex[p] / top for p in cand}
        graph, via = self._ppr(page_lex, top)
        for p, g in graph.items():
            score[p] = score.get(p, 0.0) + W_GRAPH * g

        rids = sorted(score)
        info = self._page_info(rids)
        lifted: set[int] = set()
        if fold:
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
            if new:
                info.update(self._page_info(new))

        ranked = sorted(score, key=lambda p: (-score[p], info[p][0]))
        final = ranked[:k]
        heads_of_stale: set[int] = set()
        if fold:
            # 접힌 head 가 상위 k 에 있으면 그 옛 page 는 k 밖이어도 `sup→head` 한 줄로 따라간다 — 옛 본문
            # 질문에 "무엇으로 대체됐는가" 가 답의 일부다. 점수(head × STALE_SHOW) 는 그대로다.
            final_set = set(final)
            final.extend(p for p in ranked[k:] if info[p][1] != p and info[p][1] in final_set)
            heads_of_stale = {info[p][1] for p in final if info[p][1] != p}
        need = [brid for p in final for _s, brid in page_blocks.get(p, [])[:MAX_EVIDENCE]]
        bid_of: dict[int, str] = {}
        for i in range(0, len(need), 900):
            chunk = need[i:i + 900]
            for brid, bid in self.db.execute(
                    "SELECT rid,block_id FROM blk WHERE rid IN (%s)" % ",".join("?" * len(chunk)), chunk):
                bid_of[brid] = bid
        hits: list[Hit] = []
        for p in final:
            pid, h, sb, st, slug = info[p]
            ev: list[str] = []
            if p in lifted and sb:
                ev.append(sb)
            if p in via and via[p][1] and via[p][1] not in ev:
                ev.append(via[p][1])
            for _s, brid in page_blocks.get(p, [])[:MAX_EVIDENCE]:
                bid = bid_of.get(brid, "")
                if bid and bid not in ev:
                    ev.append(bid)
            if not ev and h == p and (p in lifted or p in heads_of_stale):
                # 옛 page 가 접혀 올라온 head 인데 근거가 없다(supersedes 링크에 block_id 가 없고 질문 토큰도 head
                # 본문에 없다) — 첫 본문 block 을 근거로 삼는다. head 는 본문과 함께 나가야 하고, B 가 없는 P 는
                # 렌더가 버리기 때문이다(grok s5: grade=strong 인데 주입 0바이트).
                row = self.db.execute("SELECT block_id FROM blk WHERE prid=? AND ev=1 ORDER BY pos LIMIT 1",
                                      (p,)).fetchone()
                if row and row[0]:
                    ev.append(row[0])
            head_pid = info[h][0] if h in info else pid
            hits.append(Hit(page_id=pid, score=round(score[p], 6), block_ids=ev[:MAX_EVIDENCE + 1],
                            head=head_pid, sup_state=st, slug=slug))
        self.last_signals["top_score"] = float(hits[0].score) if hits else 0.0
        self.last_signals["second_score"] = float(hits[1].score) if len(hits) > 1 else 0.0
        return SearchResult(hits, dict(self.last_signals))

    # -------------------------------------------------------- 투영 조회
    def pages(self, ids: list[str]) -> dict[str, dict[str, Any]]:
        if not ids:
            return {}
        cols = PAGE_COLS
        out: dict[str, dict[str, Any]] = {}
        for i in range(0, len(ids), 900):
            chunk = ids[i:i + 900]
            for r in self.db.execute("SELECT * FROM page WHERE page_id IN (%s)" % ",".join("?" * len(chunk)), chunk):
                out[r[1]] = dict(zip(cols, r))
        return out

    def page_by_rid(self, rid: int) -> dict[str, Any] | None:
        cols = PAGE_COLS
        row = self.db.execute("SELECT * FROM page WHERE rid=?", (rid,)).fetchone()
        return dict(zip(cols, row)) if row else None

    def blocks(self, ids: list[str]) -> dict[str, dict[str, Any]]:
        if not ids:
            return {}
        cols = BLK_COLS
        out: dict[str, dict[str, Any]] = {}
        for i in range(0, len(ids), 900):
            chunk = ids[i:i + 900]
            for r in self.db.execute("SELECT * FROM blk WHERE block_id IN (%s)" % ",".join("?" * len(chunk)), chunk):
                out[r[2]] = dict(zip(cols, r))
        return out

    def edges(self, block_ids: list[str]) -> list[tuple[str, str, str, str]]:
        if not block_ids:
            return []
        out: list[tuple[str, str, str, str]] = []
        for i in range(0, len(block_ids), 900):
            chunk = block_ids[i:i + 900]
            # dst_block = 상대 page 에서 이쪽으로 오는 첫 link 의 block (없으면 '')
            out.extend(self.db.execute(
                "SELECT l.block_id, l.kind, p.page_id, COALESCE((SELECT r.block_id FROM link r "
                "WHERE r.src=l.dst AND r.dst=l.src ORDER BY r.ord LIMIT 1), '') "
                "FROM link l JOIN page p ON p.rid=l.dst WHERE l.dst IS NOT NULL AND l.block_id IN (%s)"
                % ",".join("?" * len(chunk)), chunk))
        out.sort()
        return out

    def lookup(self, selector: str) -> dict[str, Any] | None:
        """slug · page id · 제목 · block id 로 page 행 하나. `llmwiki_get` 의 주소 힌트다."""
        want = str(selector or "").strip()
        if not want:
            return None
        if "#" in want:
            want = want.split("#", 1)[0].strip()
        if want.startswith("block:"):
            parts = want.split(":")
            want = parts[1] if len(parts) > 1 else ""
        if want.startswith("page:"):
            want = want[5:]
        if not want:
            return None
        cols = PAGE_COLS
        row = self.db.execute(
            "SELECT * FROM page WHERE slug=? OR page_id=? OR lower(slug)=lower(?) OR lower(title)=lower(?) "
            "ORDER BY CASE WHEN slug=? THEN 0 WHEN page_id=? THEN 1 ELSE 2 END LIMIT 1",
            (want, "page:" + want, want, want, want, "page:" + want)).fetchone()
        return dict(zip(cols, row)) if row else None


def open_ro(path: Path, *, busy_timeout_ms: int = 2000) -> Index:
    """읽기 전용으로 연다. 훅 전용 — 파일이 없거나 표가 다르면 예외를 낸다(호출자가 fail-open).

    `immutable=1`: publish 는 언제나 `os.replace` 로 파일을 통째로 바꾸고 제자리 수정을 하지 않으므로
    읽는 쪽은 잠금도, `-wal`/`-shm` 사이드카도 필요 없다. macOS 의 시스템 Python(/usr/bin/python3,
    훅이 실제로 쓰는 인터프리터) 은 읽기 전용 연결로 WAL 사이드카를 만들지 못해 첫 호출이 실패했다.
    """
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(str(path))
    db = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True, check_same_thread=False)
    try:
        db.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        idx = Index(db, path=path)
    except sqlite3.Error:
        db.close()
        raise
    if idx.schema != SCHEMA_VERSION:
        db.close()
        raise ValueError(f"schema {idx.schema!r} != {SCHEMA_VERSION!r}")
    return idx


def read_revision(root: Path) -> str:
    try:
        value = json.loads((Path(root) / "index" / "revision.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    return str(value.get("revision") or "") if isinstance(value, dict) else ""


def newest_mtime(wiki_dir: Path) -> float:
    """wiki/ 아래 json 의 최신 mtime. 색인 파일보다 새 정본이 있으면 색인은 낡았다."""
    newest = 0.0
    stack = [str(wiki_dir)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    if entry.name.startswith("."):
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(entry.path)
                    elif entry.name.endswith(".json"):
                        try:
                            newest = max(newest, entry.stat().st_mtime)
                        except OSError:
                            pass
        except OSError:
            continue
    return newest


# ------------------------------------------------------------------ 신선도
def verify_hits(idx: Index, root: Path, hits: list[Hit]) -> dict[str, Any]:
    """hit page 의 정본 파일을 다시 읽어 색인의 sha 와 대조한다.

    돌려주는 것: {"pages": {page_id: 정본 page | None(지워짐)}, "changed": [page_id…], "missing": [...]}.
    바뀐 page 만 들어 있다 — 그대로인 page 는 색인이 정본과 같다.
    """
    root = Path(root)
    rows = idx.pages([h.page_id for h in hits])
    by_file: dict[str, list[str]] = {}
    for pid, row in rows.items():
        if row.get("source"):
            by_file.setdefault(row["source"], []).append(pid)
    out: dict[str, Any] = {"pages": {}, "changed": [], "missing": []}
    for rel, pids in sorted(by_file.items()):
        path = (root / rel)
        try:
            resolved = path.resolve()
            resolved.relative_to((root / "wiki").resolve())
            value = json.loads(resolved.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            value = None
        found: dict[str, dict[str, Any]] = {}
        for page in (value if isinstance(value, list) else [value]) if value is not None else []:
            if isinstance(page, dict) and page.get("id"):
                found[str(page["id"])] = page
        for pid in pids:
            page = found.get(pid)
            if page is None:
                out["pages"][pid] = None
                out["missing"].append(pid)
            elif page_sha(page) != rows[pid]["sha256"]:
                out["pages"][pid] = page
                out["changed"].append(pid)
    for h in hits:
        if h.page_id not in rows:
            out["pages"][h.page_id] = None
            out["missing"].append(h.page_id)
    return out


# ------------------------------------------------------------------ 투영
@dataclass
class Group:
    page_id: str
    slug: str
    type: str
    updated: str
    sources: str
    score: float
    stale: bool = False                  # head 가 다른 낡은 page — P 한 줄만
    head_slug: str = ""
    sup_state: str = ""
    file: str = ""
    title: str = ""
    summary: str = ""
    projects: str = ""
    tags: str = ""
    unresolved: int = 0
    blocks: list[dict[str, Any]] = field(default_factory=list)      # {id, kind, text, unresolved, refs}
    edges: dict[str, list[tuple[str, str]]] = field(default_factory=dict)   # block id → [(kind, target)]
    reread: bool = False                 # 정본에서 다시 읽은 page


def _slug_of(page_id: str) -> str:
    return page_id[5:] if page_id.startswith("page:") else page_id


def tail_of(block_id: str, slug: str) -> str:
    """block:<slug>:<tail> → <tail>. `llmwiki_get` 의 resolve_blocks 가 꼬리만 줘도 푼다."""
    prefix = f"block:{slug}:"
    return block_id[len(prefix):] if block_id.startswith(prefix) else block_id


def _blocks_from_page(page: dict[str, Any], ids: list[str]) -> dict[str, dict[str, Any]]:
    blocks = page.get("blocks") or {}
    order = [b for b in (page.get("block_order") or list(blocks)) if b in blocks]
    out: dict[str, dict[str, Any]] = {}
    for bid in ids:
        b = blocks.get(bid)
        if not isinstance(b, dict):
            continue
        out[bid] = {"block_id": bid, "kind": str(b.get("kind") or ""), "pos": order.index(bid) if bid in order else 0,
                    "ev": 0 if b.get("kind") in EVIDENCE_SKIP_KINDS else 1,
                    "unresolved": int(unresolved(b)), "text": redact(block_text(b)),
                    "refs": json.dumps(list(b.get("refs") or []), ensure_ascii=False)}
    return out


def project_graph(idx: Index, hits: list[Hit], *, cut: float = CUT,
                  overrides: dict[str, Any] | None = None) -> list[Group]:
    """hit → 렌더 재료. 본문은 `blk.text`(정본에서 build 때 복사·redact 된 것) 에서 읽는다.

    `overrides` 는 verify_hits 의 결과 — 바뀐 page 는 정본 page 에서 본문·메타를 채우고,
    지워진 page(None) 는 뺀다.
    """
    overrides = overrides or {}
    if hits and cut > 0:
        top = hits[0].score
        kept = {h.page_id for h in hits if h.score >= cut * top}
        # 낡은 page(head × STALE_SHOW) 는 cut 아래지만, 그 head 가 실리면 `sup→head` 한 줄로 같이 실린다.
        hits = [h for h in hits if h.page_id in kept or (h.head != h.page_id and h.head in kept)]
    hits = [h for h in hits if not (h.page_id in overrides and overrides[h.page_id] is None)]
    pages = idx.pages([h.page_id for h in hits])
    blocks = idx.blocks([b for h in hits for b in h.block_ids])
    edges = idx.edges([b for h in hits for b in h.block_ids])
    by_src: dict[str, list[tuple[str, str, str]]] = {}
    for src, kind, dst, dstb in edges:
        if kind in CURATED:
            by_src.setdefault(src, []).append((kind, dst, dstb))
    heads = sorted({p["head"] for p in pages.values()})
    head_rows: dict[int, dict[str, Any]] = {}
    for rid in heads:
        row = idx.page_by_rid(rid)
        if row:
            head_rows[rid] = row
    dst_ids = sorted({d for lst in by_src.values() for _k, d, _b in lst if d not in pages})
    dst_pages = idx.pages(dst_ids)
    groups: list[Group] = []
    for h in hits:
        p = pages.get(h.page_id)
        if not p:
            continue
        override = overrides.get(h.page_id)
        if isinstance(override, dict):
            slug = str(override.get("slug") or p["slug"])
            g = Group(h.page_id, slug, str(override.get("type") or ""), str(override.get("updated") or ""),
                      ",".join(override.get("sources") or []), h.score, file=p["source"],
                      title=norm(override.get("title")), summary=redact(norm(override.get("summary"))),
                      projects=",".join(override.get("projects") or []), tags=",".join(override.get("tags") or []),
                      unresolved=sum(1 for b in (override.get("blocks") or {}).values()
                                     if isinstance(b, dict) and unresolved(b)), reread=True)
            own_blocks = _blocks_from_page(override, h.block_ids)
        else:
            g = Group(h.page_id, p["slug"], p["type"], p["updated"], p["sources"], h.score, file=p["source"],
                      title=p["title"], summary=p["summary"], projects=p["projects"], tags=p["tags"],
                      unresolved=int(p["unresolved"] or 0))
            own_blocks = {b: blocks[b] for b in h.block_ids if b in blocks}
        g.sup_state = str(p.get("sup_state") or "")
        head_row = head_rows.get(p["head"])
        if head_row and head_row["page_id"] != h.page_id:
            g.stale = True
            g.head_slug = head_row["slug"] or _slug_of(head_row["page_id"])
            groups.append(g)
            continue
        for bid in h.block_ids:
            b = own_blocks.get(bid)
            if not b:
                continue
            g.blocks.append({"id": bid, "kind": b["kind"], "text": b["text"],
                             "unresolved": int(b["unresolved"] or 0), "refs": b.get("refs") or "[]"})
            for kind, dst, dstb in by_src.get(bid, []):
                dp = pages.get(dst) or dst_pages.get(dst)
                dslug = dp["slug"] if dp else _slug_of(dst)
                target = f"{dslug}#{tail_of(dstb, dslug)}" if dstb else dslug
                g.edges.setdefault(bid, []).append((kind, target))
        groups.append(g)
    # 낡은 page 의 `sup→head` 줄은 그 head 바로 뒤에 둔다 — 순위는 head × STALE_SHOW 라 뒤쪽인데, 예산이 뒤에서
    # 끊기면 "무엇으로 대체됐는가" 가 빠진다. head 가 실리지 않은 낡은 page 는 제자리다.
    heads = {g.slug for g in groups if not g.stale}
    trailing: dict[str, list[Group]] = {}
    for g in groups:
        if g.stale and g.head_slug in heads:
            trailing.setdefault(g.head_slug, []).append(g)
    if trailing:
        ordered: list[Group] = []
        for g in groups:
            if g.stale and g.head_slug in heads:
                continue
            ordered.append(g)
            ordered.extend(trailing.get(g.slug, []))
        groups = ordered
    return groups


# ------------------------------------------------------------------ 행 단위 선택 (W4)
def split_rows(raw: str) -> tuple[list[str], bool]:
    """block 원문을 행으로 쪼갠다. (rows, is_table). 표는 구분선 행을 뺀다. 한 줄짜리 긴 본문은 문장으로."""
    lines = [norm_ws(l) for l in str(raw).split("\n")]
    lines = [l for l in lines if l]
    if not lines:
        return [], False
    is_table = lines[0].startswith("|")
    if is_table:
        return [l for l in lines if not _TABLE_SEP.match(l)], True
    if len(lines) >= 2:
        return lines, False
    return [p.strip() for p in _SENT.split(lines[0]) if p.strip()], False


def query_weights(idx: Index, query: str) -> dict[str, float]:
    """질문 토큰 → idf². 기능어 2-gram 은 뺀다(전부 기능어면 유지)."""
    terms = sorted(set(tokenize(query)))
    kept = [t for t in terms if t not in STOP_2G] or terms
    idf = idx.term_idf(kept)
    return {t: idf.get(t, 0.0) ** 2 for t in kept}


def select_rows(raw: str, wt: dict[str, float], limit: int = ROW_CHARS) -> tuple[str, bool]:
    """(본문, 잘렸는가). limit 자 이하 block 은 통째로. 넘으면 행 단위로 고른다.

    표는 머리 행을 붙이고, 1위 행이 너무 길면 앞부분만 싣는다. 결과는 반드시 limit 자 이하다
    (생략 표시까지 포함해 마지막에 다시 자른다).
    """
    whole = norm_ws(raw)
    if len(whole) <= limit:
        return whole, False
    rows, is_table = split_rows(raw)
    if len(rows) <= 1:
        return clip(whole, limit), True

    def rscore(r: str) -> float:
        low = r.lower()
        return sum(w for t, w in wt.items() if t in low)

    order = sorted(range(len(rows)), key=lambda i: (-rscore(rows[i]), i))
    header = rows[0] if is_table and len(rows) > 1 else ""
    used = len(header) + 1 if header else 0
    chosen: list[int] = []
    trunc: dict[int, str] = {}
    for i in order:
        if header and i == 0:
            continue
        if chosen and rscore(rows[i]) <= 0.0:
            break
        size = len(rows[i]) + 1
        if used + size > limit:
            if not chosen and limit - used > ROW_MIN_TRUNC:
                trunc[i] = rows[i][: limit - used - 2].rstrip() + "…"
                chosen.append(i)
                used = limit
            continue
        chosen.append(i)
        used += size
    chosen.sort()
    parts = ([header] if header else []) + [trunc.get(i, rows[i]) for i in chosen]
    body = " ".join(parts)
    if len(chosen) < len(rows) - (1 if header else 0) and len(body) + 2 <= limit:
        body += " …"
    if len(body) > limit:
        body = body[: limit - 1].rstrip() + "…"
    return body, True


def derive_signals(sig: dict[str, float]) -> dict[str, float]:
    """검색기의 원 신호에 곱 신호를 더한다. 무주입 판정은 이 dict 의 SILENCE_SIGNAL 값으로 한다."""
    s = dict(sig)
    raw, imp, craw = s.get("raw_top", 0.0), s.get("top_block_impact", 0.0), s.get("content_raw_top", 0.0)
    cov, idfcov = s.get("coverage", 0.0), s.get("idf_coverage", 0.0)
    s["raw_x_cov"] = raw * cov
    s["raw_x_idfcov"] = raw * idfcov
    s["impact_x_cov"] = imp * cov
    s["impact_x_idfcov"] = imp * idfcov
    s["content_raw_x_cov"] = craw * cov
    s["content_raw_x_idfcov"] = craw * idfcov
    return s


# ------------------------------------------------------------------ 렌더 (P/B/E)
@dataclass
class Placed:
    page_id: str
    block_id: str = ""
    body: bool = False
    status: str = "none"       # cur | conflict | superseded | address | edge | always
    text: str = ""


@dataclass
class Rendered:
    text: str
    placed: list[Placed] = field(default_factory=list)
    bytes: int = 0
    tokens: int = 0
    dropped: int = 0           # 예산 때문에 통째로 빠진 page 수


def _status(g: Group, b: dict[str, Any] | None) -> str:
    if g.stale:
        return "superseded"
    if b and b.get("unresolved"):
        return "conflict"
    return "cur"


def _lines(groups: list[Group], wt: dict[str, float], *, grade: str, mid_pages: int, row_chars: int
           ) -> list[list[tuple[str, Placed]]]:
    """group → 줄 묶음. 첫 줄이 P 또는 A 다. 본문은 여기서 redact·행 선택·상한을 거친다."""
    out: list[list[tuple[str, Placed]]] = []
    n_body = 0
    for g in groups:
        slug = redact(g.slug)
        meta = f"{slug} {redact(g.type)} {redact(g.updated)}"
        if g.stale:
            out.append([(f"P {meta} sup→{redact(g.head_slug)}", Placed(g.page_id, "", False, "superseded"))])
            continue
        with_body = grade == "strong" or (grade == "mid" and n_body < mid_pages)
        if not with_body:
            addrs = ", ".join(f"{slug}#{tail_of(b['id'], g.slug)}" for b in g.blocks)
            rows = [(f"A {meta} → {redact(addrs)}", Placed(g.page_id, "", False, "address"))]
            rows.extend(("", Placed(g.page_id, b["id"], False, "address")) for b in g.blocks)
            out.append(rows)
            continue
        n_body += 1
        src = f" src={redact(g.sources)}" if g.sources else ""
        flag = f" sup?{g.sup_state}" if g.sup_state in ("fork", "cycle") else ""
        rows = [(f"P {meta}{src}{flag}", Placed(g.page_id, "", False, "cur"))]
        for b in g.blocks:
            st = _status(g, b)
            body, _cut = select_rows(redact(b["text"]), wt, row_chars)
            body = redact(body)
            if len(body) > row_chars:                 # redact 가 늘렸을 수도 있다 — 상한은 계약이다
                body = clip(body, row_chars)
            addr = f"{slug}#{tail_of(b['id'], g.slug)}"
            rows.append((f"B {addr} {st} | {body}", Placed(g.page_id, b["id"], True, st, body)))
            for kind, target in g.edges.get(b["id"], []):
                rows.append((f"E {addr} {kind}→{redact(target)}", Placed(g.page_id, b["id"], False, "edge")))
        out.append(rows)
    return out


def _fits(text: str, max_bytes: int, max_tokens: int) -> bool:
    return len(text.encode("utf-8")) <= max_bytes and est_tokens(text) <= max_tokens


def render_graph(groups: list[Group], wt: dict[str, float], *, max_bytes: int, max_tokens: int,
                 preamble: str = "", grade: str = "strong", mid_pages: int = MID_BODY_PAGES,
                 weak_lines: int = WEAK_LINES, row_chars: int = ROW_CHARS) -> Rendered:
    """page 단위로 묶되 채움은 block(줄) 단위. 바이트와 토큰 상한을 둘 다 지킨다.

    block 이 하나도 못 들어가는 page 의 P 줄은 예산만 먹으므로 싣지 않는다(낡은 page 의 `sup→` P 줄과
    주소(A) 줄은 그 자체가 정보라 남긴다). 접힌 head 는 search 가 근거 block 을 반드시 붙이므로(anchor →
    질문 토큰과 맞는 block → 첫 본문 block) 여기서 버려지지 않는다. 마지막에 전체를 다시 검사해 넘치면
    뒤에서부터 덜어낸다.
    """
    if grade == "none" or not groups:
        text = _always_only(preamble, max_bytes, max_tokens)
        return Rendered(text, [Placed("", "", False, "always")] if text else [],
                        len(text.encode("utf-8")), est_tokens(text) if text else 0)
    if grade == "weak":
        return _weak(groups, preamble, max_bytes=max_bytes, max_tokens=max_tokens, weak_lines=weak_lines)
    head = GRAPH_HEAD + (redact(preamble) + "\n" if preamble else "")
    line_groups = _lines(groups, wt, grade=grade, mid_pages=mid_pages, row_chars=row_chars)
    kept_groups: list[list[tuple[str, Placed]]] = []
    used_b = len(head.encode("utf-8")) + len(TAIL.encode("utf-8"))
    used_t = est_tokens(head) + est_tokens(TAIL)
    dropped = 0
    for rows in line_groups:
        head_line, head_placed = rows[0]
        hb, ht = len(head_line.encode("utf-8")) + 1, est_tokens(head_line + "\n")
        if used_b + hb > max_bytes or used_t + ht > max_tokens:
            dropped += 1
            continue
        chosen: list[tuple[str, Placed]] = []
        sb, st_ = hb, ht
        for line, placed in rows[1:]:
            lb = len(line.encode("utf-8")) + 1 if line else 0
            lt = est_tokens(line + "\n") if line else 0
            if used_b + sb + lb > max_bytes or used_t + st_ + lt > max_tokens:
                continue
            chosen.append((line, placed))
            sb += lb
            st_ += lt
        if not any(p.body for _l, p in chosen) and head_placed.status not in ("superseded", "address"):
            dropped += 1
            continue
        kept_groups.append([(head_line, head_placed), *chosen])
        used_b += sb
        used_t += st_
    while True:
        lines = [line for rows in kept_groups for line, _p in rows if line]
        text = head + "\n".join(lines) + ("\n" if lines else "") + TAIL
        if _fits(text, max_bytes, max_tokens) or not kept_groups:
            break
        kept_groups.pop()
        dropped += 1
    if not kept_groups:
        text = _always_only(preamble, max_bytes, max_tokens)
        return Rendered(text, [Placed("", "", False, "always")] if text else [],
                        len(text.encode("utf-8")), est_tokens(text) if text else 0, dropped)
    placed = [p for rows in kept_groups for _l, p in rows if p.status != "edge"]
    return Rendered(text, placed, len(text.encode("utf-8")), est_tokens(text), dropped)


def _weak(groups: list[Group], preamble: str, *, max_bytes: int, max_tokens: int, weak_lines: int) -> Rendered:
    head = WEAK_HEAD + (redact(preamble) + "\n" if preamble else "")
    rows: list[tuple[str, Placed]] = []
    for g in groups[:weak_lines]:
        slug = redact(g.slug)
        if g.stale:
            rows.append((f"- {slug} sup→{redact(g.head_slug)}", Placed(g.page_id, "", False, "superseded")))
        else:
            first = f"{slug}#{tail_of(g.blocks[0]['id'], g.slug)}" if g.blocks else slug
            rows.append((f"- {redact(first)}", Placed(g.page_id, "", False, "address")))
            rows.extend(("", Placed(g.page_id, b["id"], False, "address")) for b in g.blocks)
    kept: list[tuple[str, Placed]] = []
    while True:
        lines = [l for l, _p in (kept or rows) if l]
        text = head + "\n".join(lines) + ("\n" if lines else "") + TAIL
        if _fits(text, max_bytes, max_tokens):
            break
        pool = kept or rows
        # 마지막 주소 줄(과 그 block 항목)을 덜어낸다
        cut_at = max((i for i, (l, _p) in enumerate(pool) if l), default=-1)
        if cut_at < 0:
            return Rendered(_always_only(preamble, max_bytes, max_tokens), [], 0, 0)
        kept = pool[:cut_at]
        if not kept:
            text = _always_only(preamble, max_bytes, max_tokens)
            return Rendered(text, [], len(text.encode("utf-8")), est_tokens(text) if text else 0)
    placed = [p for _l, p in (kept or rows)]
    return Rendered(text, placed, len(text.encode("utf-8")), est_tokens(text))


def _always_only(preamble: str, max_bytes: int, max_tokens: int) -> str:
    """검색이 비어도 고정 page 는 나간다 — 그것이 '항상' 의 뜻이다. 상한은 여기서도 지킨다."""
    if not preamble:
        return ""
    text = "<llmwiki-context v=3>\n" + redact(preamble) + "\n" + TAIL
    return text if _fits(text, max_bytes, max_tokens) else ""


def grade_of(signals: dict[str, float], *, silence_t: float = 0.0, hint_t: float = 0.0,
             signal: str = SILENCE_SIGNAL) -> str:
    """무주입 판정. 두 문턱이 모두 0(기본) 이면 언제나 strong 이다.

    signal ≥ silence_t → strong. hint_t ≤ signal < silence_t → weak(주소만). 그 아래 → none.
    hint_t 를 주지 않으면 silence_t 아래는 전부 none 이다.
    """
    if silence_t <= 0 and hint_t <= 0:
        return "strong"
    v = float(signals.get(signal, 0.0))
    if silence_t <= 0 or v >= silence_t:
        return "strong"
    if hint_t > 0 and v >= hint_t:
        return "weak"
    return "none"


__all__ = [name for name in globals() if not name.startswith("_")]

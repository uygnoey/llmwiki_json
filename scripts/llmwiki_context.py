#!/usr/bin/env python3
"""llmwiki_json 자동 컨텍스트 주입 CLI.

사용자의 질문이 Codex / Claude Code 로 전달되기 전에 이 저장소의 정본 JSON
(`wiki/**/*.json`)에서 관련 근거를 찾아 압축된 컨텍스트로 주입한다.

설계 계약
--------
- 검색은 `index/search.sqlite`(`llmwiki.py build` 가 정본에서 굽는 색인) 를 읽기 전용으로
  연다. 색인이 없거나(`meta.revision` 이 `index/revision.json` 과 다르거나, 정본 파일이 색인보다
  새거나) 열 수 없으면 **지금까지의 정본 스캔 경로 그대로** 동작한다(fail-open, stats 에
  `fallback`). 색인 경로에서도 hit page 의 정본 sha 를 대조해 바뀐 page 는 정본을 다시 읽는다.
- 주입 형식은 P/B/E 부분 그래프(`llmwiki_index.render_graph`). block 본문은 앞 320자가 아니라
  질문과 겹치는 행을 고르고, 낡은 page(supersedes 로 대체됨) 는 `sup→head` 한 줄만 싣는다.
- 무주입 문턱은 **기본 꺼짐**. `LLMWIKI_CONTEXT_SILENCE` 로 켠다 (아래).
- `llmwiki_get` 은 색인을 주소 힌트로만 쓰고 최종 object 는 정본 파일 하나에서 읽는다.
- 어떤 오류가 나도 질문을 막지 않는다(fail-open: stdout 비우고 exit 0). 워치독 6초.
- 자격증명으로 보이는 문자열은 색인에도 출력에도 남기지 않는다.
- 어느 cwd 에서 실행해도 이 파일 위치 기준 절대경로로 저장소를 찾는다.

환경변수
--------
LLMWIKI_ROOT                저장소 루트 override (기본: 이 파일의 부모의 부모)
LLMWIKI_CONTEXT_DISABLE     1 이면 hook 이 아무것도 하지 않는다
LLMWIKI_CONTEXT_MAX_BYTES   주입 본문 UTF-8 바이트 상한 (기본 6000)
LLMWIKI_CONTEXT_MAX_TOKENS  주입 본문 추정 토큰 상한 (기본 2000)
LLMWIKI_CONTEXT_INDEX       0 이면 색인을 쓰지 않고 정본 스캔만 한다 (기본 1)
LLMWIKI_CONTEXT_SILENCE     무주입 문턱 T (기본 0 = 끔). raw_top × coverage < T 면 무주입.
                            동결 자연 세트 보정값은 770 이지만 위키마다 다시 재야 한다.
LLMWIKI_CONTEXT_HINT        주소만(weak) 문턱 (기본 0 = 끔). HINT ≤ 신호 < SILENCE 면 주소만
LLMWIKI_CONTEXT_TIMEOUT     hook 전체 워치독 초 (기본 6)
LLMWIKI_CONTEXT_LOG         지정하면 주입 통계를 JSONL 로 append (질문 원문은 저장 안 함)
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import shlex
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

try:  # package import (bench: `from scripts import llmwiki_context`)
    from . import llmwiki_index as IDX
except ImportError:  # run as a script or loaded from its file path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import llmwiki_index as IDX  # type: ignore[no-redef]

DEFAULT_ROOT = Path(__file__).resolve().parents[1]

ENV_ROOT = "LLMWIKI_ROOT"
ENV_DISABLE = "LLMWIKI_CONTEXT_DISABLE"
ENV_MAX_BYTES = "LLMWIKI_CONTEXT_MAX_BYTES"
ENV_MAX_TOKENS = "LLMWIKI_CONTEXT_MAX_TOKENS"
ENV_INDEX = "LLMWIKI_CONTEXT_INDEX"
ENV_MEMORY = "LLMWIKI_CONTEXT_MEMORY"
ENV_SILENCE = "LLMWIKI_CONTEXT_SILENCE"
ENV_HINT = "LLMWIKI_CONTEXT_HINT"
ENV_TIMEOUT = "LLMWIKI_CONTEXT_TIMEOUT"
ENV_LOG = "LLMWIKI_CONTEXT_LOG"
ENV_STATE_DIR = "LLMWIKI_STATE_DIR"

HOOK_EVENT = "UserPromptSubmit"

# 주입 예산 기본값. 매 프롬프트마다 붙으므로 보수적으로 잡는다.
MAX_BYTES = 6000
MAX_TOKENS = 2000
MAX_PAGES = 5
MAX_BLOCKS = 6
MAX_BLOCK_CHARS = 320
WATCHDOG_SECONDS = 6.0

# 색인 경로: 부분 그래프에 넣는 page 수와 1위 대비 컷. 무주입 문턱은 기본 꺼짐(0).
GRAPH_PAGES = IDX.PAGES
GRAPH_CUT = IDX.CUT
SILENCE_T_DEFAULT = 0.0
HINT_T_DEFAULT = 0.0

# 최후 스캔 경로 전용 문턱. 색인이 없으면 먼저 정본에서 메모리 색인을 만들어 색인 경로와
# 같은 검색·투영·렌더(P/B/E)를 쓰고, 그 build 마저 실패했을 때만 이 옛 스캔 경로로 온다.
# 둘 다 넘어야 본문(block)을 주입한다.
MIN_SCORE = 6.0
MIN_COVERAGE = 0.34

# 커버리지는 질문이 길어질수록 떨어진다. "…답해라. 표로 쓰지 마라." 같은
# 지시문이 붙으면 정본을 정확히 짚은 질문도 문턱에서 떨어진다. 서로 다른
# 토큰이 이만큼 한 page 에 맞았다면 그건 우연이 아니므로 커버리지를 면제한다.
# 점수는 코퍼스 크기(idf)에 따라 커지지만 이 개수는 그렇지 않다.
MIN_MATCHED = 5
# 본문 문턱은 못 넘었지만 이만큼은 겹치는 질문에는 본문 대신 "주소만" 준다.
# 침묵하면 클라이언트는 위키가 있다는 사실조차 모르고 지나간다.
HINT_SCORE = 2.5
HINT_COVERAGE = 0.3
HINT_MATCHED = 4
HINT_PAGES = 3
HINT_SUMMARY_CHARS = 110

# 질문과 상관없이 늘 앞에 붙는 page. 이 사람이 일하는 방식처럼 매번 알아야
# 하는 것을 여기 둔다. 예산은 따로 잡아 검색 결과를 밀어내지 않게 한다.
ALWAYS_CONFIG = "tools/config/context.json"
ALWAYS_MAX_BYTES = 800
ALWAYS_MAX_BLOCKS = 5
ALWAYS_BLOCK_CHARS = 150

# 조회(get) 기본값. 페이지를 통째로 싣지 않는 것이 기본이다.
GET_MODES = ("outline", "blocks", "page")
OUTLINE_CHARS = 120
META_FIELDS = ("id", "slug", "title", "type", "created", "updated", "projects", "tags",
               "sources", "raw_ref", "summary", "supersedes", "related", "links", "history")

TOKEN_RE = re.compile(r"[0-9A-Za-z_][0-9A-Za-z_.\-]*|[가-힣]+")
HANGUL_RE = re.compile(r"^[가-힣]+$")
# 자격증명 패턴은 색인 모듈이 가진다 — 색인에 저장되는 본문과 출력이 같은 규칙으로 지워진다.
SECRET = IDX.SECRET
SECRET_MASK = IDX.SECRET_MASK
SECRET_EXTRA = IDX.SECRET_EXTRA

# 질문에서 신호를 주지 않는 흔한 어휘. 한국어는 조사/어미가 붙은 형태까지 적는다.
STOPWORDS = {
    # 한국어
    "그리고", "그러면", "하지만", "그런데", "그래서", "관련", "관련해서", "대해", "대해서",
    "무엇", "무엇인가", "뭐야", "뭔가", "어떻게", "어떤", "어디", "언제", "왜", "누가",
    "알려줘", "알려주세요", "해줘", "해주세요", "정리해줘", "설명해줘", "설명", "요약",
    "요약해줘", "가능한가", "가능해", "있나요", "있어", "있는지", "인가요", "인가", "일까",
    "정도", "부분", "내용", "경우", "때문", "우리", "지금", "현재", "다시", "조금", "많이",
    "이거", "그거", "저거", "이것", "그것", "저것", "여기", "거기", "저기", "하나", "전부",
    "모두", "그냥", "좀", "안녕", "안녕하세요", "고마워", "감사합니다", "사항", "관하여",
    # 영어
    "the", "a", "an", "and", "or", "but", "if", "of", "to", "in", "on", "for", "with",
    "is", "are", "was", "were", "be", "been", "do", "does", "did", "can", "could",
    "should", "would", "will", "what", "how", "why", "when", "where", "who", "which",
    "this", "that", "these", "those", "it", "its", "you", "your", "me", "my", "we",
    "our", "please", "tell", "show", "explain", "about", "from", "into", "there",
    "here", "not", "no", "yes", "get", "set", "use", "using", "make", "need", "want",
}

FIELD_WEIGHT = {"title": 8.0, "tags": 5.0, "projects": 4.0, "summary": 4.0, "body": 1.5}
KIND_BONUS = {"conflict": 2.5, "current": 1.5, "table": 0.5, "list": 0.25}
# 제목은 page 머리에 이미 실리므로 heading block 은 예산만 먹는다.
SKIP_KINDS = {"heading", "thematic_break"}
CONFLICT_MARK = "\u26a0\ufe0f"


# --------------------------------------------------------------------------- utils
def norm(value: Any) -> str:
    return unicodedata.normalize("NFC", str(value or "")).strip()


def env_int(name: str, fallback: int) -> int:
    try:
        return max(0, int(os.environ[name]))
    except (KeyError, ValueError):
        return fallback


def env_float(name: str, fallback: float) -> float:
    try:
        return max(0.0, float(os.environ[name]))
    except (KeyError, ValueError):
        return fallback


def env_flag(name: str, fallback: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return fallback
    return raw.strip().lower() not in {"0", "false", "no", "off", ""}


def redact(text: str) -> str:
    """자격증명처럼 보이는 값은 절대 컨텍스트로 내보내지 않는다.

    이름이 붙은 형태(`password: …`)뿐 아니라 값만 나오는 형태 — 인증 헤더,
    PEM 블록, 알려진 토큰 접두, URL 에 박힌 계정 — 까지 지운다. 제어문자도 뺀다.
    """
    return IDX.redact(text)


def est_tokens(text: str) -> int:
    """보수적 토큰 추정치: UTF-8 3바이트당 1토큰.

    한글 1글자(3바이트)를 1토큰으로 세므로 실제보다 크게 잡힌다. 상한을
    넘기지 않는 쪽으로만 틀리는 추정이라 예산 가드로 안전하다.
    """
    return math.ceil(len(text.encode("utf-8")) / 3)


def clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


# --------------------------------------------------------------------------- tokens
def tokenize(text: str) -> list[str]:
    return [t.lower() for t in TOKEN_RE.findall(norm(text))]


def is_hangul(token: str) -> bool:
    return bool(HANGUL_RE.match(token))


def query_tokens(text: str) -> list[str]:
    """질문에서 신호가 되는 토큰만 남긴다(중복 제거, 등장 순서 유지)."""
    out: list[str] = []
    seen: set[str] = set()
    for token in tokenize(text):
        if token in STOPWORDS or token in seen:
            continue
        if len(token) < 2:
            continue
        seen.add(token)
        out.append(token)
    return out


def match_strength(token: str, haystack: str) -> float:
    """토큰이 haystack 에 얼마나 걸리는지 0.0~1.0.

    한국어는 조사/어미가 붙어 들어오므로 뒤에서부터 한 글자씩 깎아
    실제로 등장하는 가장 긴 접두사를 찾고, 길이 비율로 감점한다.
    영어도 굴절/복수형을 흡수하도록 4글자까지만 깎는다.
    """
    if not token or not haystack:
        return 0.0
    if token in haystack:
        return 1.0
    floor = 2 if is_hangul(token) else 4
    for size in range(len(token) - 1, floor - 1, -1):
        if token[:size] in haystack:
            return size / len(token)
    return 0.0


# --------------------------------------------------------------------------- corpus
@dataclass
class Doc:
    page: dict[str, Any]
    rel: str
    path: Path
    fields: dict[str, str] = field(default_factory=dict)

    @property
    def slug(self) -> str:
        return str(self.page.get("slug") or "")

    @property
    def page_id(self) -> str:
        return str(self.page.get("id") or "")


def load_corpus(root: Path) -> list[Doc]:
    """정본 shard 를 그대로 읽는다. 깨진 파일은 건너뛴다(fail-soft)."""
    root = Path(root).resolve()
    pages_dir = root / "wiki"
    docs: list[Doc] = []
    if not pages_dir.is_dir():
        return docs
    for path in sorted(pages_dir.rglob("*.json")):
        if path.name.startswith("."):
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for page in value if isinstance(value, list) else [value]:
            if not isinstance(page, dict) or not page.get("id") or not page.get("blocks"):
                continue
            try:
                rel = path.resolve().relative_to(root).as_posix()
            except ValueError:
                rel = path.as_posix()
            docs.append(Doc(page=page, rel=rel, path=path, fields=doc_fields(page)))
    return docs


def block_text(block: dict[str, Any]) -> str:
    data = block.get("data") or {}
    if isinstance(data, dict):
        for key in ("text", "statement"):
            if isinstance(data.get(key), str) and data[key].strip():
                return data[key]
    text = block.get("source_text")
    return text if isinstance(text, str) else ""


def doc_fields(page: dict[str, Any]) -> dict[str, str]:
    blocks = page.get("blocks") or {}
    order = page.get("block_order") or list(blocks)
    body = " ".join(block_text(blocks[b]) for b in order if b in blocks)
    return {
        "title": norm(page.get("title")).lower(),
        "tags": " ".join(norm(t) for t in page.get("tags") or []).lower(),
        "projects": " ".join(norm(p) for p in page.get("projects") or []).lower(),
        "summary": norm(page.get("summary")).lower(),
        "body": (norm(page.get("slug")) + " " + body).lower(),
    }


# --------------------------------------------------------------------------- ranking
@dataclass
class Hit:
    doc: Doc | None
    score: float
    matched: set[str]
    via: str = "canonical"
    page_id: str = ""
    slug: str = ""
    block_ids: list[str] = field(default_factory=list)
    head: str = ""               # 색인 경로: 대체한 head page id (자기 자신이면 낡지 않았다)

    def __post_init__(self) -> None:
        if self.doc is not None:
            self.page_id = self.page_id or self.doc.page_id
            self.slug = self.slug or self.doc.slug


def rank(docs: list[Doc], tokens: list[str]) -> tuple[list[Hit], dict[str, float]]:
    """정본 문서를 질문 토큰으로 점수화한다.

    필드별 가중치 × 토큰 강도 × idf. idf 는 코퍼스 내 문서 빈도에서 바로
    계산하므로 흔한 낱말("시스템")이 관련도를 부풀리지 않는다.
    """
    if not docs or not tokens:
        return [], {}
    total = len(docs)
    strengths: list[dict[str, dict[str, float]]] = []
    doc_freq: dict[str, int] = {t: 0 for t in tokens}
    for doc in docs:
        per_token: dict[str, dict[str, float]] = {}
        for token in tokens:
            per_field = {name: match_strength(token, hay) for name, hay in doc.fields.items()}
            if any(per_field.values()):
                doc_freq[token] += 1
                per_token[token] = per_field
        strengths.append(per_token)

    idf = {t: math.log(1.0 + total / (1.0 + doc_freq[t])) for t in tokens}
    hits: list[Hit] = []
    for doc, per_token in zip(docs, strengths):
        score = 0.0
        matched: set[str] = set()
        for token, per_field in per_token.items():
            best = 0.0
            for name, weight in FIELD_WEIGHT.items():
                best += weight * per_field.get(name, 0.0)
            if best <= 0:
                continue
            matched.add(token)
            score += idf[token] * best
        if score > 0:
            hits.append(Hit(doc=doc, score=round(score, 4), matched=matched))
    hits.sort(key=lambda h: (-h.score, h.doc.page_id))
    return hits, idf


def rank_blocks(doc: Doc, tokens: list[str], idf: dict[str, float],
                limit: int) -> list[dict[str, Any]]:
    """페이지 안에서 실제로 근거가 되는 block 만 고른다."""
    page = doc.page
    blocks = page.get("blocks") or {}
    order = [b for b in (page.get("block_order") or list(blocks)) if b in blocks]
    scored: list[tuple[float, int, str]] = []
    for position, bid in enumerate(order):
        block = blocks[bid]
        kind = str(block.get("kind") or "")
        if kind in SKIP_KINDS:
            continue
        text = block_text(block).lower()
        if not text:
            continue
        score = sum(idf.get(t, 1.0) * match_strength(t, text) for t in tokens)
        if flagged(block):
            # 미판정 상충은 질문과 직접 겹치지 않아도 함께 보여야 판단을 그르치지 않는다.
            score += KIND_BONUS["conflict"]
        elif score > 0:
            score += KIND_BONUS.get(kind, 0.0)
        if score > 0:
            scored.append((score, position, bid))
    scored.sort(key=lambda item: (-item[0], item[1]))
    chosen = {bid for _, _, bid in scored[:limit]}
    if not chosen:
        # 제목/요약만 걸린 페이지도 최소한 본문 한 조각은 근거로 준다.
        for bid in order:
            if blocks[bid].get("kind") in {"paragraph", "list", "current"}:
                chosen = {bid}
                break
    return [dict(blocks[bid], id=bid) for bid in order if bid in chosen]


def unresolved(block: dict[str, Any]) -> bool:
    resolution = block.get("resolution")
    status = resolution.get("status") if isinstance(resolution, dict) else None
    return block.get("kind") == "conflict" and status != "resolved"


def flagged(block: dict[str, Any]) -> bool:
    """`conflict` block 이 아니어도 본문에 상충 표시가 있으면 함께 보여준다."""
    return unresolved(block) or CONFLICT_MARK in block_text(block)


# --------------------------------------------------------------------------- retrieval
@dataclass
class Result:
    query: str
    root: Path
    hits: list[Hit]
    idf: dict[str, float]
    tokens: list[str]
    coverage: float
    reason: str
    mode: str = "scan"                     # index | memory | scan
    fallback: str = ""                     # 디스크 색인을 못 쓴 이유 (memory·scan 일 때)
    grade: str = ""                        # 색인 경로의 등급 strong|weak|none
    signals: dict[str, float] = field(default_factory=dict)
    stats: dict[str, Any] = field(default_factory=dict)


def retrieve(root: Path, query: str, *, limit: int = MAX_PAGES, min_score: float = MIN_SCORE,
             min_coverage: float = MIN_COVERAGE, min_matched: int = MIN_MATCHED,
             hint_score: float = HINT_SCORE) -> Result:
    """최후 스캔 경로 — 디스크 색인도 메모리 색인도 못 쓸 때의 fail-open 경로다."""
    tokens = query_tokens(query)
    docs = load_corpus(root)
    if not tokens:
        return Result(query, root, [], {}, tokens, 0.0, "no-content-tokens")
    if not docs:
        return Result(query, root, [], {}, tokens, 0.0, "empty-corpus")

    hits, idf = rank(docs, tokens)
    top = hits[0].score if hits else 0.0
    coverage = len(hits[0].matched) / len(tokens) if hits else 0.0
    reason = "canonical"

    if not hits:
        return Result(query, root, [], idf, tokens, 0.0, "no-match")
    # 숫자 하나가 우연히 겹친 것은 신호가 아니다.
    matched = len(hits[0].matched) if any(not t.isdigit() for t in hits[0].matched) else 0
    wide = min_matched > 0 and matched >= min_matched
    if top < min_score or (coverage < min_coverage and not wide):
        # 본문을 싣기엔 근거가 약하다. 그래도 주소는 안다면 주소만 넘긴다.
        # 클라이언트는 llmwiki_get 으로 필요한 block 만 집어 오면 된다.
        if (hint_score > 0 and matched > 0 and top >= hint_score
                and (coverage >= HINT_COVERAGE or matched >= HINT_MATCHED)):
            return Result(query, root, hits[:HINT_PAGES], idf, tokens, coverage,
                          f"hint:{reason}")
        return Result(query, root, [], idf, tokens, coverage, "below-threshold")
    return Result(query, root, hits[:limit], idf, tokens, coverage, reason)


# --------------------------------------------------------------------------- index path
def index_path(root: Path) -> Path:
    return Path(root) / "index" / IDX.DB_NAME


def open_index(root: Path) -> tuple[IDX.Index | None, str]:
    """신선한 색인이면 (Index, ""), 아니면 (None, 이유). 어떤 오류도 밖으로 내지 않는다.

    신선하다 = 파일이 있고 표가 맞고, `meta.revision` 이 `index/revision.json` 과 같고,
    `wiki/` 아래에 색인보다 새 json 이 없다. 셋 중 하나라도 확증할 수 없으면 정본 스캔이다.
    """
    root = Path(root)
    path = index_path(root)
    if not path.is_file():
        return None, "no-index"
    try:
        idx = IDX.open_ro(path)
    except Exception as exc:  # noqa: BLE001 - sqlite 오류·lock·schema 불일치 전부 스캔으로
        return None, f"open-error:{type(exc).__name__}"
    try:
        revision = IDX.read_revision(root)
        if not revision or idx.revision != revision:
            idx.close()
            return None, "revision-mismatch"
        if IDX.newest_mtime(root / "wiki") > path.stat().st_mtime:
            idx.close()
            return None, "stale-mtime"
    except Exception as exc:  # noqa: BLE001
        idx.close()
        return None, f"check-error:{type(exc).__name__}"
    return idx, ""


def retrieve_indexed(idx: IDX.Index, root: Path, query: str, *, limit: int = GRAPH_PAGES,
                     cut: float = GRAPH_CUT, silence_t: float = SILENCE_T_DEFAULT,
                     hint_t: float = HINT_T_DEFAULT, mode: str = "index"
                     ) -> tuple[Result, list[IDX.Group], dict[str, float]]:
    """색인 검색 → hit page 신선도 확인 → 부분 그래프 투영. 렌더는 하지 않는다.

    `mode` 는 색인의 출처다 — 디스크 `index` 또는 정본에서 방금 만든 `memory`. 검색·투영은 같다.
    """
    root = Path(root)
    tokens = query_tokens(query)
    if not tokens:
        return Result(query, root, [], {}, tokens, 0.0, "no-content-tokens", mode=mode), [], {}
    found = idx.search(query, k=limit)
    signals = IDX.derive_signals(found.signals)
    coverage = float(signals.get("coverage", 0.0))
    if not found.hits:
        return Result(query, root, [], {}, tokens, coverage, "no-match", mode=mode,
                      grade="none", signals=signals), [], {}
    grade = IDX.grade_of(signals, silence_t=silence_t, hint_t=hint_t)
    verify = IDX.verify_hits(idx, root, found.hits)
    groups = IDX.project_graph(idx, found.hits, cut=cut, overrides=verify["pages"])
    weights = IDX.query_weights(idx, query)
    hits = [Hit(None, g.score, set(), "index", page_id=g.page_id, slug=g.slug,
                block_ids=[b["id"] for b in g.blocks],
                head=f"page:{g.head_slug}" if g.stale else g.page_id) for g in groups]
    reason = {"strong": mode, "weak": f"hint:{mode}", "none": f"below-threshold:{mode}"}[grade]
    stats = {"reread": verify["changed"], "missing": verify["missing"], "hits": len(found.hits)}
    return Result(query, root, hits, {}, tokens, coverage, reason, mode=mode, grade=grade,
                  signals=signals, stats=stats), groups, weights


def open_memory_index(root: Path) -> tuple[IDX.Index | None, str]:
    """정본에서 바로 만든 메모리 색인 — 디스크 색인이 없거나 낡았을 때의 첫 번째 대안.

    `llmwiki.py query` 가 낡은 색인 앞에서 하는 것과 같다. 어떤 오류도 밖으로 내지 않고
    (None, 이유) 로 돌려주며, 그때 호출자는 최후 스캔 경로로 간다.
    """
    root = Path(root)
    try:
        docs = IDX.load_docs(root / "wiki", root)
        if not docs:
            return None, "memory-empty-corpus"
        return IDX.build_memory(docs, revision=IDX.read_revision(root)), ""
    except Exception as exc:  # noqa: BLE001 - 정본 손상 등 무엇이든 스캔으로
        return None, f"memory-error:{type(exc).__name__}"


def project_group(group: IDX.Group, weights: dict[str, float], *,
                  row_chars: int = MAX_BLOCK_CHARS, via: str = "index") -> dict[str, Any]:
    """색인 경로의 page projection — 스캔 경로의 `project_hit` 과 같은 모양이다."""
    blocks = []
    via = via or "index"
    for b in group.blocks:
        body, _cut = IDX.select_rows(b["text"], weights, row_chars)
        try:
            refs = json.loads(b.get("refs") or "[]")
        except json.JSONDecodeError:
            refs = []
        blocks.append({"id": b["id"], "kind": b["kind"], "text": redact(body), "refs": refs,
                       "resolution": ("unresolved" if b["unresolved"] else "resolved")
                       if b["kind"] == "conflict" else None,
                       "flagged": bool(b["unresolved"])})
    out = {
        "id": group.page_id, "slug": group.slug, "title": redact(group.title), "type": group.type,
        "updated": group.updated, "projects": group.projects.split(",") if group.projects else [],
        "tags": group.tags.split(",") if group.tags else [],
        "summary": clip(redact(group.summary), 240),
        "sources": group.sources.split(",") if group.sources else [],
        "raw_ref": None, "file": group.file, "abs_file": "",
        "score": round(group.score, 4), "via": via,
        "unresolved_conflicts": group.unresolved, "blocks": blocks,
    }
    if group.stale:
        out["superseded_by"] = group.head_slug
    if group.reread:
        out["reread"] = True
    return out


# --------------------------------------------------------------------------- projection
def project_hit(hit: Hit, tokens: list[str], idf: dict[str, float], *,
                max_blocks: int = MAX_BLOCKS,
                max_block_chars: int = MAX_BLOCK_CHARS) -> dict[str, Any]:
    """주입에 필요한 page/block/field 만 뽑는다. 페이지 전체를 싣지 않는다."""
    page = hit.doc.page
    # 0 을 달라고 했으면 0 개다. 주소만 넘기는 hint 경로가 이걸 쓴다.
    blocks = rank_blocks(hit.doc, tokens, idf, max_blocks) if max_blocks > 0 else []
    conflicts = sum(1 for b in (page.get("blocks") or {}).values() if flagged(b))
    return {
        "id": page.get("id"),
        "slug": page.get("slug"),
        "title": norm(page.get("title")),
        "type": page.get("type"),
        "updated": page.get("updated"),
        "projects": list(page.get("projects") or []),
        "tags": list(page.get("tags") or []),
        "summary": clip(redact(norm(page.get("summary"))), 240),
        "sources": list(page.get("sources") or []),
        "raw_ref": page.get("raw_ref"),
        "file": hit.doc.rel,
        "abs_file": str(hit.doc.path),
        "score": round(hit.score, 2),
        "via": hit.via,
        "unresolved_conflicts": conflicts,
        "blocks": [{
            "id": b["id"],
            "kind": b.get("kind"),
            "text": clip(redact(block_text(b)), max_block_chars),
            "refs": list(b.get("refs") or []),
            "resolution": (b.get("resolution") or {}).get("status", "unresolved")
            if b.get("kind") == "conflict" else None,
            "flagged": flagged(b),
        } for b in blocks],
    }


def render(result: Result, pages: list[dict[str, Any]], *, max_bytes: int = MAX_BYTES,
           max_tokens: int = MAX_TOKENS, preamble: str = "") -> str:
    """예산 안에서 페이지를 순서대로 채운다. 예산을 넘기면 거기서 멈춘다."""
    if not pages:
        return ""
    head = [
        "<llmwiki-context>",
        f"아래는 개인 지식 위키 정본(`{result.root}`)에서 이 질문에 대해 자동 검색한 근거다.",
        "정본은 `wiki/**/*.json` 이고, 파생물(`index/`, `viewer/public/data/`)은 "
        "`python3 scripts/llmwiki.py build` 로만 갱신한다.",
        "이 근거와 어긋나는 내용을 말하기 전에 해당 page/block 을 직접 확인하라. "
        "근거가 부족하면 모른다고 답하라.",
        "더 필요하면 `llmwiki_get(selector=\"<slug>\", blocks=[\"<block id>\"])` 로 "
        "그 block object 만 가져와라. page 전체 JSON 을 통째로 읽지 마라.",
        "",
    ]
    if preamble:
        head.extend([preamble, ""])
    tail = "</llmwiki-context>"
    note = "(예산 초과로 {n}개 page 생략 — `llmwiki_context.py search` 로 더 볼 수 있다)"
    fixed = len("\n".join(head).encode("utf-8")) + len(tail.encode("utf-8")) + 2
    reserve = len(note.format(n=len(pages)).encode("utf-8")) + 1
    body_budget = max_bytes - fixed - reserve
    token_budget = max_tokens - est_tokens("\n".join(head)) - est_tokens(tail) - est_tokens(note)

    out: list[str] = []
    used = 0
    for page in pages:
        chunk = render_page(page)
        size = len(chunk.encode("utf-8")) + 1
        if used + size > body_budget or est_tokens("\n".join(out) + chunk) > token_budget:
            break
        out.append(chunk)
        used += size
    if not out:
        # 최소 한 페이지의 머리말은 남긴다. block 없이도 어디를 볼지는 알려준다.
        head_only = render_page({**pages[0], "blocks": []})
        if len(head_only.encode("utf-8")) <= max(0, body_budget):
            out.append(head_only)
    if not out:
        return ""
    # 상한을 넘지 않는 것이 이 함수의 계약이다. 계산이 어긋나도 페이지를
    # 하나씩 덜어내며 계약을 지킨다(잘라낸 마크다운을 내보내지 않는다).
    while out:
        shown = list(out)
        if len(shown) < len(pages):
            shown.append(note.format(n=len(pages) - len(out)))
        text = "\n".join(head + shown + [tail])
        if len(text.encode("utf-8")) <= max_bytes and est_tokens(text) <= max_tokens:
            return text
        out.pop()
    return ""


def render_page(page: dict[str, Any]) -> str:
    lines = [f"### {page['id']} — {page['title']}"]
    meta = [f"type={page['type']}", f"updated={page['updated']}"]
    if page["projects"]:
        meta.append("projects=" + ",".join(page["projects"]))
    if page["tags"]:
        meta.append("tags=" + ",".join(page["tags"]))
    meta.append(f"score={page['score']}({page['via']})")
    lines.append("- " + " | ".join(meta))
    lines.append(f"- file: {page['file']}")
    if page.get("raw_ref"):
        lines.append(f"- raw: {page['raw_ref']}")
    if page["summary"]:
        lines.append(f"- summary: {page['summary']}")
    if page["sources"]:
        lines.append("- sources: " + ", ".join(page["sources"][:8]))
    if page["unresolved_conflicts"]:
        lines.append(f"- ⚠️ 미판정 상충 {page['unresolved_conflicts']}건 — 양쪽을 병기해서 답하라")
    for block in page["blocks"]:
        flag = block.get("flagged") and not block["text"].startswith(CONFLICT_MARK)
        mark = f"{CONFLICT_MARK} " if flag else ""
        kind = block["kind"]
        if block["resolution"]:
            kind = f"{kind}/{block['resolution']}"
        lines.append(f"  - {mark}[{block['id']}] ({kind}) {block['text']}")
    return "\n".join(lines)


def render_hint(result: Result, pages: list[dict[str, Any]], *, max_bytes: int = MAX_BYTES,
                max_tokens: int = MAX_TOKENS, preamble: str = "") -> str:
    """본문을 싣기엔 약한 매치. 주소와 가져오는 법만 넘긴다."""
    if not pages:
        return ""
    head = [
        "<llmwiki-context>",
        f"개인 지식 위키 정본(`{result.root}`)에서 이 질문과 **약하게** 겹치는 page 주소만 "
        "추렸다. 본문은 싣지 않았다 — 아래 page 가 답을 담고 있다고 가정하지 마라.",
        "필요하면 필요한 부분만 가져와라. page 전체 JSON 을 통째로 읽지 마라:",
        '  llmwiki_get(selector="<slug>")                        → block 목록(outline)',
        '  llmwiki_get(selector="<slug>", blocks=["<block id>"])  → 그 block object 만',
        "",
    ]
    if preamble:
        head.extend([preamble, ""])
    rows = []
    for page in pages:
        meta = f"{page['type']}, updated={page['updated']}"
        row = f"- {page['id']} ({meta}) — {page['title']}"
        summary = clip(page["summary"], HINT_SUMMARY_CHARS)
        if summary:
            row += f": {summary}"
        rows.append(row)
    tail = "</llmwiki-context>"
    while rows:
        text = "\n".join(head + rows + [tail])
        if len(text.encode("utf-8")) <= max_bytes and est_tokens(text) <= max_tokens:
            return text
        rows.pop()
    return ""


def render_always_only(root: Path, preamble: str, *, max_bytes: int = MAX_BYTES,
                       max_tokens: int = MAX_TOKENS) -> str:
    """검색이 비어도 고정 page 는 나간다 — 그것이 '항상' 의 뜻이다."""
    if not preamble:
        return ""
    text = "\n".join(["<llmwiki-context>", preamble, "</llmwiki-context>"])
    # 이 경로도 상한을 지킨다. 넘기면 아무것도 내지 않는 편이 낫다.
    if len(text.encode("utf-8")) > max_bytes or est_tokens(text) > max_tokens:
        return ""
    return text


def pinned_ids(root: Path) -> set[str]:
    return {f"page:{s}".lower() if not s.lower().startswith("page:") else s.lower()
            for s in always_slugs(root)}


def is_pinned(pinned: set[str], page_id: str, slug: str) -> bool:
    return str(page_id).lower() in pinned or f"page:{slug}".lower() in pinned


def build_context(root: Path, query: str, **options: Any) -> tuple[str, Result, list[dict[str, Any]]]:
    """질문 하나 → (주입 본문, Result, page projection).

    세 경로가 있고 호출자는 같은 문법(`<llmwiki-context v=3>` P/B/E)을 받는다.
      index  — 신선한 `index/search.sqlite`.
      memory — 색인이 없거나 낡거나 깨졌을 때 정본에서 바로 만든 메모리 색인. 검색·투영·렌더는
               index 와 같은 코드다. `stats.fallback` 에 디스크 색인을 못 쓴 이유를 남긴다.
      scan   — 메모리 색인 build 마저 실패했을 때(정본 손상 등)의 최후 수단. 옛 스캔 검색과
               옛 `<llmwiki-context>` 렌더다. 훅이 exit 0 을 지키기 위한 바닥이다.
    어느 쪽이든 고정 page, 바이트·토큰 상한, redact 는 같이 적용된다.
    """
    root = Path(root)
    max_bytes = options.pop("max_bytes", MAX_BYTES)
    max_tokens = options.pop("max_tokens", MAX_TOKENS)
    max_blocks = options.pop("max_blocks", MAX_BLOCKS)
    max_block_chars = options.pop("max_block_chars", MAX_BLOCK_CHARS)
    use_always = options.pop("use_always", True)
    use_index = options.pop("use_index", env_flag(ENV_INDEX, True))
    use_memory = options.pop("use_memory", env_flag(ENV_MEMORY, True))
    silence_t = options.pop("silence_t", env_float(ENV_SILENCE, SILENCE_T_DEFAULT))
    hint_t = options.pop("hint_t", env_float(ENV_HINT, HINT_T_DEFAULT))
    cut = options.pop("cut", GRAPH_CUT)
    # 고정 몫에는 자체 상한이 있고(전체의 절반 이내), 그 뒤 render 가 머리말까지
    # 포함해 전체 예산을 다시 검사한다. 여기서 또 빼면 검색 몫이 두 번 줄어든다.
    preamble = render_always(root, total=max_bytes) if use_always else ""
    pinned = pinned_ids(root) if preamble else set()

    graph_options = dict(max_bytes=max_bytes, max_tokens=max_tokens,
                         limit=options.get("limit", GRAPH_PAGES), cut=cut, silence_t=silence_t,
                         hint_t=hint_t, row_chars=max_block_chars)
    idx, why = open_index(root) if use_index else (None, "disabled")
    if idx is not None:
        try:
            return _build_indexed(idx, root, query, preamble, pinned, **graph_options)
        except Exception as exc:  # noqa: BLE001 - 색인 경로의 어떤 오류도 다음 경로로 떨어진다
            why = f"index-error:{type(exc).__name__}"
        finally:
            idx.close()

    # 메모리 색인: 디스크 색인을 못 쓴 이유(why)는 그대로 stats.fallback 에 남긴다.
    if use_memory:
        started = time.perf_counter()
        idx, memory_why = open_memory_index(root)
        if idx is not None:
            try:
                text, result, pages = _build_indexed(idx, root, query, preamble, pinned,
                                                     mode="memory", **graph_options)
                result.fallback = why
                result.stats["memory_build_ms"] = round((time.perf_counter() - started) * 1000, 1)
                return text, result, pages
            except Exception as exc:  # noqa: BLE001 - 메모리 경로의 오류도 스캔으로
                memory_why = f"memory-error:{type(exc).__name__}"
            finally:
                idx.close()
        why = f"{why};{memory_why}"
    else:
        why = f"{why};memory-disabled"

    scan_options = {k: v for k, v in options.items()
                    if k in {"limit", "min_score", "min_coverage", "min_matched", "hint_score"}}
    result = retrieve(root, query, **scan_options)
    result.fallback = why
    if preamble:
        # 고정으로 이미 실은 page 를 검색 결과로 또 싣지 않는다.
        kept = [h for h in result.hits if not is_pinned(pinned, h.page_id, h.slug)]
        if len(kept) != len(result.hits):
            result.hits = kept
    if result.reason.startswith("hint"):
        # 주소만 넘기는 경로다. block 을 뽑지 않으므로 예산도 거의 쓰지 않는다.
        pages = [project_hit(h, result.tokens, result.idf, max_blocks=0,
                             max_block_chars=max_block_chars) for h in result.hits]
        text = render_hint(result, pages, max_bytes=max_bytes, max_tokens=max_tokens,
                           preamble=preamble)
        return (text or render_always_only(root, preamble, max_bytes=max_bytes,
                                           max_tokens=max_tokens), result, pages)
    pages = [project_hit(h, result.tokens, result.idf, max_blocks=max_blocks,
                         max_block_chars=max_block_chars) for h in result.hits]
    text = render(result, pages, max_bytes=max_bytes, max_tokens=max_tokens,
                  preamble=preamble)
    return (text or render_always_only(root, preamble, max_bytes=max_bytes,
                                       max_tokens=max_tokens), result, pages)


def _build_indexed(idx: IDX.Index, root: Path, query: str, preamble: str, pinned: set[str], *,
                   max_bytes: int, max_tokens: int, limit: int, cut: float, silence_t: float,
                   hint_t: float, row_chars: int, mode: str = "index"
                   ) -> tuple[str, Result, list[dict[str, Any]]]:
    """index 와 memory 경로가 같이 쓰는 검색·투영·렌더. 출력 문법은 둘 다 P/B/E 다."""
    result, groups, weights = retrieve_indexed(idx, root, query, limit=limit, cut=cut,
                                               silence_t=silence_t, hint_t=hint_t, mode=mode)
    if pinned:
        keep = [not is_pinned(pinned, g.page_id, g.slug) for g in groups]
        groups = [g for g, k in zip(groups, keep) if k]
        result.hits = [h for h, k in zip(result.hits, keep) if k]
    pages = [project_group(g, weights, row_chars=row_chars, via=mode) for g in groups]
    grade = result.grade or ("none" if not groups else "strong")
    rendered = IDX.render_graph(groups, weights, max_bytes=max_bytes, max_tokens=max_tokens,
                                preamble=preamble, grade=grade, row_chars=row_chars)
    result.stats["placed"] = sum(1 for p in rendered.placed if p.body)
    result.stats["dropped_pages"] = rendered.dropped
    return rendered.text, result, pages


# --------------------------------------------------------------------------- always
def always_slugs(root: Path) -> list[str]:
    """`tools/config/context.json` 의 always 목록. 없으면 아무것도 고정하지 않는다."""
    try:
        value = json.loads((root / ALWAYS_CONFIG).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    items = value.get("always") if isinstance(value, dict) else None
    return [str(x) for x in items if str(x).strip()] if isinstance(items, list) else []


def always_budget(root: Path, *, total: int = MAX_BYTES) -> int:
    """고정 몫. 설정이 아무리 크게 잡혀 있어도 전체의 절반을 넘지 않는다."""
    ceiling = max(1, total // 2)
    try:
        value = json.loads((root / ALWAYS_CONFIG).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return min(ALWAYS_MAX_BYTES, ceiling)
    limit = value.get("always_max_bytes") if isinstance(value, dict) else None
    wanted = int(limit) if isinstance(limit, int) and limit > 0 else ALWAYS_MAX_BYTES
    return min(wanted, ceiling)


def render_always(root: Path, *, max_bytes: int | None = None,
                  max_blocks: int = ALWAYS_MAX_BLOCKS, total: int = MAX_BYTES) -> str:
    """고정 page 를 짧게. 요약과 앞쪽 block 몇 줄만 싣고 나머지는 검색에 맡긴다."""
    slugs = always_slugs(root)
    if not slugs:
        return ""
    budget = always_budget(root, total=total) if max_bytes is None else max_bytes
    lines: list[str] = []
    for slug in slugs:
        doc = find_doc(root, slug)
        if doc is None:
            continue
        page = doc.page
        lines.append(f"- {page.get('id')} — {norm(page.get('title'))}")
        summary = clip(redact(norm(page.get("summary"))), 200)
        blocks = page.get("blocks") or {}
        order = [b for b in (page.get("block_order") or list(blocks)) if b in blocks]
        body: list[str] = []
        for block_id in order:
            block = blocks[block_id]
            if block.get("kind") in SKIP_KINDS:
                continue
            text = clip(redact(block_text(block)), ALWAYS_BLOCK_CHARS)
            if text:
                body.append(text)
            if len(body) >= max_blocks:
                break
        # summary 는 대개 본문 앞머리를 그대로 옮겨 놓은 것이다. 둘 다 실으면
        # 좁은 예산에 같은 말이 두 번 들어간다. 목록 기호·공백 차이로 중복을
        # 놓치지 않도록 글자만 남겨 비교한다.
        def bare(value: str) -> str:
            return re.sub(r"[^0-9A-Za-z가-힣]", "", value)

        squashed = bare(" ".join(body))
        if summary and bare(summary)[:30] not in squashed:
            lines.append(f"  {summary}")
        lines.extend(f"  · {text}" for text in body)
    if not lines:
        return ""
    head = "이 사람과 일하는 방식이다. 매 질문에 함께 붙는다:"
    while lines:
        text = "\n".join([head, *lines])
        if len(text.encode("utf-8")) <= budget:
            return text
        lines.pop()
    return ""


# --------------------------------------------------------------------------- get
def find_doc(root: Path, selector: str) -> Doc | None:
    """slug · page id · block id · 제목 중 무엇으로 불러도 같은 page 를 찾는다.

    색인은 **주소 힌트**로만 쓴다: 색인이 가리키는 정본 파일 하나를 읽고 id 가 맞는지 확인한다.
    색인이 없거나 힌트가 빗나가면(지워짐·이동·id 불일치) 정본 전체를 스캔한다.
    """
    root = Path(root)
    want = str(selector or "").strip()
    if not want:
        return None
    hinted = _find_doc_indexed(root, want)
    if hinted is not None:
        return hinted
    if "#" in want:
        want = want.split("#", 1)[0].strip()
    if want.startswith("block:"):
        # block:<slug>:<fingerprint>:<n> — slug 는 kebab 이라 ':' 를 담지 않는다.
        parts = want.split(":")
        want = parts[1] if len(parts) > 1 else ""
    if want.startswith("page:"):
        want = want[5:]
    key = want.lower()
    for doc in load_corpus(root):
        if key in {doc.slug.lower(), doc.page_id.lower(), f"page:{doc.slug}".lower(),
                   norm(doc.page.get("title")).lower()}:
            return doc
    return None


def _find_doc_indexed(root: Path, selector: str) -> Doc | None:
    if not env_flag(ENV_INDEX, True):
        return None
    idx, _why = open_index(root)
    if idx is None:
        return None
    try:
        row = idx.lookup(selector)
    except Exception:  # noqa: BLE001
        row = None
    finally:
        idx.close()
    if not row or not row.get("source"):
        return None
    return read_doc(root, str(row["source"]), str(row["page_id"]))


def read_doc(root: Path, rel: str, page_id: str) -> Doc | None:
    """정본 파일 하나에서 page 하나. 파일은 반드시 root/wiki 안에 있어야 한다."""
    root = Path(root).resolve()
    path = (root / rel)
    try:
        resolved = path.resolve()
        resolved.relative_to(root / "wiki")
        value = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    for page in value if isinstance(value, list) else [value]:
        if isinstance(page, dict) and str(page.get("id")) == page_id and page.get("blocks"):
            try:
                rel_out = resolved.relative_to(root).as_posix()
            except ValueError:
                rel_out = resolved.as_posix()
            return Doc(page=page, rel=rel_out, path=resolved, fields={})
    return None


def selector_block(selector: str) -> str:
    """`slug#block-id` 또는 block id 를 통째로 준 경우의 block 부분."""
    want = str(selector or "").strip()
    if "#" in want:
        return want.split("#", 1)[1].strip()
    return want if want.startswith("block:") else ""


def resolve_blocks(page: dict[str, Any],
                   wanted: Iterable[str]) -> tuple[list[dict[str, Any]], list[str]]:
    """block id 를 해석한다. 꼬리만 준 축약형(`meta`)도 받는다."""
    blocks = page.get("blocks") or {}
    order = [b for b in (page.get("block_order") or list(blocks)) if b in blocks]
    found: list[dict[str, Any]] = []
    missing: list[str] = []
    seen: set[str] = set()
    for raw in wanted:
        key = str(raw or "").strip()
        if not key:
            continue
        hit = key if key in blocks else next(
            (b for b in order if b.endswith(f":{key}") or b.split(":")[-1] == key), "")
        if not hit:
            missing.append(key)
            continue
        if hit not in seen:
            seen.add(hit)
            found.append(blocks[hit])
    return found, missing


def block_view(block: dict[str, Any]) -> dict[str, Any]:
    """block object 하나. 정본 그대로이되 비밀만 지운다."""
    view = json.loads(redact(json.dumps(block, ensure_ascii=False)))
    view["conflict"] = flagged(block)
    return view


def page_meta(page: dict[str, Any], fields: Iterable[str] | None = None) -> dict[str, Any]:
    keys = [f for f in (fields or META_FIELDS) if f in META_FIELDS]
    out: dict[str, Any] = {}
    for key in keys or list(META_FIELDS):
        if key not in page:
            continue
        value = page[key]
        if key in {"links", "history"} and not fields:
            continue  # 명시적으로 부르지 않으면 싣지 않는다.
        out[key] = json.loads(redact(json.dumps(value, ensure_ascii=False))) \
            if isinstance(value, (dict, list)) else redact(norm(value))
    return out


def page_outline(page: dict[str, Any], *, block_chars: int = OUTLINE_CHARS,
                 fields: Iterable[str] | None = None) -> dict[str, Any]:
    """page 의 목차. block 본문 대신 어떤 block 이 어디 있는지만 보여준다."""
    blocks = page.get("blocks") or {}
    order = [b for b in (page.get("block_order") or list(blocks)) if b in blocks]
    rows = []
    for block_id in order:
        block = blocks[block_id]
        row: dict[str, Any] = {
            "id": block_id,
            "kind": block.get("kind"),
            "preview": clip(redact(block_text(block)), block_chars),
        }
        if block.get("refs"):
            row["refs"] = list(block["refs"])
        if block.get("kind") == "conflict":
            row["resolution"] = (block.get("resolution") or {}).get("status", "unresolved")
        if flagged(block):
            row["conflict"] = True
        rows.append(row)
    return {**page_meta(page, fields), "block_count": len(rows), "blocks": rows}


def get_page(root: Path, selector: str, *, mode: str = "outline",
             blocks: Iterable[str] | None = None, fields: Iterable[str] | None = None,
             block_chars: int = OUTLINE_CHARS) -> dict[str, Any]:
    """조회의 단일 진입점. 기본은 page 전체가 아니라 목차다."""
    if mode not in GET_MODES:
        return {"error": f"unknown mode: {mode}", "modes": list(GET_MODES)}
    doc = find_doc(root, selector)
    if doc is None:
        return {"error": f"page 없음: {selector}"}
    wanted = [b for b in (blocks or []) if str(b).strip()]
    inline = selector_block(selector)
    if inline:
        wanted = [inline, *wanted]
    if wanted and mode == "outline":
        mode = "blocks"  # block 을 지목했으면 그 block 을 달라는 뜻이다.
    if mode == "page":
        return json.loads(redact(json.dumps(doc.page, ensure_ascii=False)))
    if mode == "blocks":
        if not wanted:
            return {"error": "mode=blocks 에는 blocks 가 필요하다",
                    "hint": f"llmwiki_get(selector=\"{doc.slug}\") 로 block 목록을 먼저 봐라"}
        found, missing = resolve_blocks(doc.page, wanted)
        out: dict[str, Any] = {"id": doc.page_id, "slug": doc.slug,
                               "title": norm(doc.page.get("title")), "file": doc.rel,
                               "blocks": [block_view(b) for b in found]}
        if missing:
            out["missing"] = missing
        return out
    return {**page_outline(doc.page, block_chars=block_chars, fields=fields), "file": doc.rel}


# --------------------------------------------------------------------------- hook
def hook_prompt(payload: dict[str, Any]) -> str:
    for key in ("prompt", "user_prompt", "userPrompt", "message", "input", "text"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def should_skip(prompt: str) -> str:
    text = prompt.strip()
    if len(text) < 4:
        return "too-short"
    if text.startswith("/"):
        # 슬래시 커맨드 호출은 사용자의 질문이 아니다.
        return "slash-command"
    if "<llmwiki-context" in text:
        return "already-injected"
    return ""


def log_event(payload: dict[str, Any]) -> None:
    path = os.environ.get(ENV_LOG)
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError:
        pass


def run_hook(root: Path, stdin_text: str, *, max_bytes: int, max_tokens: int,
             max_pages: int = 0, **options: Any) -> tuple[str, dict[str, Any]]:
    """hook JSON 을 받아 hook JSON 을 돌려준다. 주입할 게 없으면 빈 문자열."""
    stats: dict[str, Any] = {"event": HOOK_EVENT, "injected": False}
    try:
        payload = json.loads(stdin_text) if stdin_text.strip() else {}
    except json.JSONDecodeError:
        stats["skip"] = "malformed-stdin"
        return "", stats
    if not isinstance(payload, dict):
        stats["skip"] = "non-object-stdin"
        return "", stats

    prompt = hook_prompt(payload)
    stats["client"] = "codex" if "turn_id" in payload else "claude"
    stats["cwd"] = payload.get("cwd")
    stats["prompt_chars"] = len(prompt)
    if not prompt:
        stats["skip"] = "no-prompt"
        return "", stats
    skip = should_skip(prompt)
    if skip:
        stats["skip"] = skip
        return "", stats

    if max_pages:
        options["limit"] = max_pages
    text, result, pages = build_context(root, prompt, max_bytes=max_bytes,
                                        max_tokens=max_tokens, **options)
    stats["reason"] = result.reason
    stats["mode"] = result.mode
    if result.fallback:
        stats["fallback"] = result.fallback
    if result.grade:
        stats["grade"] = result.grade
    if result.signals:
        stats["signals"] = {k: round(float(v), 3) for k, v in result.signals.items()
                            if k in ("raw_top", "coverage", "raw_x_cov", "top_score")}
    if result.stats.get("reread"):
        stats["reread"] = result.stats["reread"]
    if result.stats.get("missing"):
        stats["missing"] = result.stats["missing"]
    if "memory_build_ms" in result.stats:
        stats["memory_build_ms"] = result.stats["memory_build_ms"]
    stats["coverage"] = round(result.coverage, 3)
    stats["pages"] = [p["id"] for p in pages]
    if not text:
        return "", stats
    stats["injected"] = True
    stats["bytes"] = len(text.encode("utf-8"))
    stats["est_tokens"] = est_tokens(text)
    out = {"hookSpecificOutput": {"hookEventName": HOOK_EVENT, "additionalContext": text},
           "suppressOutput": True}
    return json.dumps(out, ensure_ascii=False), stats


# --------------------------------------------------------------------------- mcp
MCP_TOOLS = [
    {
        "name": "llmwiki_search",
        "description": "llmwiki_json 정본 JSON에서 질문과 관련된 page 후보를 점수순으로 찾는다 (읽기 전용).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "검색어 (한국어/영어)"},
                "limit": {"type": "integer", "description": "최대 page 수 (기본 5)"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "llmwiki_context",
        "description": "질문에 대한 주입용 근거 컨텍스트를 정본 page/block projection 으로 만든다 (읽기 전용).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_bytes": {"type": "integer"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "llmwiki_get",
        "description": (
            "정본 page 를 필요한 만큼만 읽는다 (읽기 전용). 기본은 page 전체가 아니라 "
            "block 목차(outline)다. 목차에서 필요한 block id 를 골라 blocks 로 다시 부르면 "
            "그 block object 만 돌려준다. page 전체 JSON 은 mode=\"page\" 를 명시할 때만 "
            "나가며, 보통은 필요 없다."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "selector": {"type": "string",
                             "description": "slug · page:slug · 제목 · slug#block-id · block id"},
                "blocks": {"type": "array", "items": {"type": "string"},
                           "description": "가져올 block id 들. 꼬리만 준 축약형도 받는다."},
                "fields": {"type": "array", "items": {"type": "string"},
                           "description": f"실을 page 필드. 가능: {', '.join(META_FIELDS)}"},
                "mode": {"type": "string", "enum": list(GET_MODES),
                         "description": "outline(기본) · blocks · page(통째로, 비쌈)"},
                "block": {"type": "string", "description": "blocks 의 단수형 (하위 호환)"},
            },
            "required": ["selector"],
        },
    },
]


def search_rows(root: Path, query: str, limit: int = MAX_PAGES) -> dict[str, Any]:
    """`llmwiki_search` 와 CLI `search` 의 공용 결과. `build_context` 와 같은 세 경로다:
    신선한 디스크 색인 → 정본에서 만든 메모리 색인 → 최후 스캔."""
    root = Path(root)
    idx, why = open_index(root) if env_flag(ENV_INDEX, True) else (None, "disabled")
    pages: list[dict[str, Any]] = []
    done = False
    if idx is not None:
        try:
            result, groups, weights = retrieve_indexed(idx, root, query, limit=max(1, limit), cut=0.0)
            pages = [project_group(g, weights) for g in groups]
            done = True
        except Exception as exc:  # noqa: BLE001 - 색인 오류는 다음 경로로
            why = f"index-error:{type(exc).__name__}"
        finally:
            idx.close()
    if not done and env_flag(ENV_MEMORY, True):
        idx, memory_why = open_memory_index(root)
        if idx is not None:
            try:
                result, groups, weights = retrieve_indexed(idx, root, query, limit=max(1, limit),
                                                           cut=0.0, mode="memory")
                result.fallback = why
                pages = [project_group(g, weights, via="memory") for g in groups]
                done = True
            except Exception as exc:  # noqa: BLE001
                memory_why = f"memory-error:{type(exc).__name__}"
            finally:
                idx.close()
        if not done:
            why = f"{why};{memory_why}"
    elif not done:
        why = f"{why};memory-disabled"
    if not done:
        result = retrieve(root, query, limit=max(1, limit), min_score=0.0, min_coverage=0.0)
        result.fallback = why
        pages = [project_hit(h, result.tokens, result.idf, max_blocks=MAX_BLOCKS) for h in result.hits]
    rows = []
    for p in pages:
        row = {k: p[k] for k in ("id", "slug", "title", "type", "updated", "score", "via", "file",
                                 "sources", "unresolved_conflicts")}
        row["summary"] = clip(redact(str(p.get("summary") or "")), 200)
        row["blocks"] = [b["id"] for b in p["blocks"]]
        if p.get("superseded_by"):
            row["superseded_by"] = p["superseded_by"]
        rows.append(row)
    payload: dict[str, Any] = {"query": result.query, "tokens": result.tokens, "reason": result.reason,
                               "mode": result.mode, "coverage": round(result.coverage, 3), "results": rows}
    if result.fallback:
        payload["fallback"] = result.fallback
    return payload


def mcp_call(root: Path, name: str, args: dict[str, Any]) -> str:
    if name == "llmwiki_search":
        payload = search_rows(root, str(args.get("query", "")), int(args.get("limit") or MAX_PAGES))
        return json.dumps(payload, ensure_ascii=False, indent=2)
    if name == "llmwiki_context":
        text, result, pages = build_context(root, str(args.get("query", "")),
                                            max_bytes=int(args.get("max_bytes") or MAX_BYTES))
        return text or f"(관련 근거 없음 — {result.reason})"
    if name == "llmwiki_get":
        blocks = list(args.get("blocks") or [])
        if args.get("block"):
            blocks.append(str(args["block"]))
        payload = get_page(root, str(args.get("selector", "")),
                           mode=str(args.get("mode") or "outline"),
                           blocks=blocks, fields=args.get("fields"))
        return json.dumps(payload, ensure_ascii=False, indent=2)
    return f"(알 수 없는 도구: {name})"


def mcp_serve(root: Path) -> int:
    """stdio JSON-RPC 2.0. 읽기 전용 도구만 노출한다."""
    info = {"name": "llmwiki", "version": "1.0.0"}
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        method, rid = request.get("method"), request.get("id")
        if method in {"notifications/initialized", "notifications/cancelled"}:
            continue
        try:
            if method == "initialize":
                result: Any = {"protocolVersion": "2024-11-05",
                               "capabilities": {"tools": {}},
                               "serverInfo": info}
            elif method == "tools/list":
                result = {"tools": MCP_TOOLS}
            elif method == "tools/call":
                params = request.get("params") or {}
                text = mcp_call(root, params.get("name", ""), params.get("arguments") or {})
                result = {"content": [{"type": "text", "text": text}]}
            elif method == "ping":
                result = {}
            else:
                if rid is not None:
                    respond(rid, error={"code": -32601, "message": f"unknown method {method}"})
                continue
        except Exception as exc:  # noqa: BLE001 - MCP 는 절대 죽지 않는다
            if rid is not None:
                respond(rid, error={"code": -32603, "message": str(exc)})
            continue
        if rid is not None:
            respond(rid, result=result)
    return 0


def respond(rid: Any, *, result: Any = None, error: Any = None) -> None:
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": rid}
    if error is not None:
        payload["error"] = error
    else:
        payload["result"] = result
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


# --------------------------------------------------------------------------- install
# 전역 hook 은 이 저장소 밖에서도 돌아야 하므로 인터프리터와 스크립트를 모두
# 절대경로로 못박는다. macOS 의 /usr/bin/python3 는 pyenv 를 바꿔도 사라지지
# 않으므로 1순위로 쓴다.
PYTHON_CANDIDATES = ("/usr/bin/python3", "/opt/homebrew/bin/python3", "/usr/local/bin/python3",
                     "/usr/local/opt/python3/bin/python3")
HOOK_MARKER = "llmwiki_context.py"
HOOK_TIMEOUT = 10
MIN_PYTHON = (3, 9)

# 클라이언트별 설정 파일. 경로는 설치 시점의 $HOME 에서 해석하므로 어떤 clone
# 경로에서도, 테스트의 가짜 HOME 에서도 같은 코드가 그대로 돈다.
CLIENT_FILES = {
    "codex": (".codex/hooks.json", ".codex/AGENTS.md"),
    "claude": (".claude/settings.json", ".claude/CLAUDE.md"),
}
CLIENTS = tuple(CLIENT_FILES)
# Codex 는 신뢰한 hook 의 지문을 config.toml 의 [hooks.state] 에 남긴다.
CODEX_CONFIG = ".codex/config.toml"
# 2026-09-02 이전에 검색에 쓰던 qmd 를 MCP 서버로 등록해 둔 기계가 있다. 그 collection 은
# 이제 없으니 조회해도 빈손이다. 우리가 등록한 것이 아니므로 떼지 않고, 어디에 남았는지와
# 떼는 명령만 verify/doctor 가 알려 준다.
LEGACY_QMD_MCP = "qmd"
CLAUDE_MCP_CONFIG = ".claude.json"
CODEX_TRUST_EVENT = "user_prompt_submit"
CODEX_STALE_TRUST = "codex-trust-stale"

# 실행할 수 없는 상황(스크립트 이동/삭제, 인터프리터 부재)에서도 stdin 을
# 비우고 조용히 성공한다. 훅이 프롬프트를 막는 일은 없어야 한다.
HOOK_TEMPLATE = (
    "if [ -r {script} ] && [ -x {python} ]; then "
    "{python} {script} hook 2>/dev/null || :; "
    "else {{ command -p cat 2>/dev/null || cat; }} >/dev/null 2>&1 || :; fi"
)
# Windows 의 클라이언트는 hook 을 cmd.exe 로 돌린다. 위 sh 문법은 한 글자도
# 통하지 않으므로 같은 뜻을 cmd 로 다시 쓴다. cmd 에는 `[ -r ]` 이 없어서
# 존재 검사 대신 실패를 받아 낸다 — 인터프리터가 없으면 9009, 스크립트가
# 없으면 파이썬이 2 로 죽고, 둘 다 `||` 오른쪽으로 떨어져 stdin 을 비우고
# 조용히 성공한다. `more` 가 POSIX 쪽 `cat >/dev/null` 자리다.
HOOK_TEMPLATE_WINDOWS = (
    "({python} {script} hook 2>nul) || (more>nul 2>nul)"
)


def home() -> Path:
    """설치 시점의 $HOME. import 시점에 굳히지 않아야 테스트가 격리된다."""
    return Path(os.environ.get("HOME") or Path.home())


def client_paths(name: str) -> tuple[Path, Path]:
    hooks, guide = CLIENT_FILES[name]
    return home() / hooks, home() / guide


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
    except (OSError, ValueError):
        return None


def legacy_qmd_mcp(root: Path) -> list[dict[str, str]]:
    """옛 qmd MCP 서버 등록이 남아 있는 자리를 찾는다. 고치지 않는다.

    Claude Code 는 user scope 등록을 `~/.claude.json` 의 `mcpServers` 에, local scope 를 같은
    파일의 `projects[<cwd>].mcpServers` 에, project scope 를 저장소의 `.mcp.json` 에 둔다.
    Codex 는 `~/.codex/config.toml` 의 `[mcp_servers.<이름>]` 표다.
    """
    name = LEGACY_QMD_MCP
    found: list[dict[str, str]] = []

    def has_qmd(obj: Any) -> bool:
        return isinstance(obj, dict) and name in (obj.get("mcpServers") or {})

    claude_cfg = home() / CLAUDE_MCP_CONFIG
    data = _read_json(claude_cfg)
    if has_qmd(data):
        found.append({"client": "claude", "where": str(claude_cfg),
                      "remove": f"claude mcp remove --scope user {name}"})
    projects = data.get("projects") if isinstance(data, dict) else None
    for cwd, proj in sorted((projects or {}).items()):
        if has_qmd(proj):
            found.append({"client": "claude", "where": f"{claude_cfg} projects[{cwd}]",
                          "remove": f"cd {shlex.quote(str(cwd))} && claude mcp remove --scope local {name}"})
    project_cfg = root / ".mcp.json"
    if has_qmd(_read_json(project_cfg)):
        found.append({"client": "claude", "where": str(project_cfg),
                      "remove": f"cd {shlex.quote(str(root))} && claude mcp remove --scope project {name}"})
    codex_cfg = home() / CODEX_CONFIG
    try:
        toml = codex_cfg.read_text(encoding="utf-8") if codex_cfg.is_file() else ""
    except OSError:
        toml = ""
    if re.search(rf'^\s*\[mcp_servers\.(?:"{name}"|{name})\]\s*$', toml, re.M):
        found.append({"client": "codex", "where": str(codex_cfg),
                      "remove": f"codex mcp remove {name}"})
    return found


def hook_python() -> str:
    """hook 에 못박을 인터프리터.

    pyenv/virtualenv 는 세션마다 바뀌고 지워질 수 있으므로, 시스템에 붙박이로
    있는 인터프리터를 먼저 고른다. 아무것도 없으면 지금 돌고 있는 것을 쓴다.
    """
    for candidate in PYTHON_CANDIDATES:
        if os.access(candidate, os.X_OK):
            return candidate
    return sys.executable


def win_quote(value: str) -> str:
    """cmd.exe 용 인용.

    `shlex.quote` 는 홑따옴표를 쓴다 — cmd 는 홑따옴표를 인용부호로 보지 않고
    경로의 일부로 읽으므로 `\'C:\\Python\\python.exe\'` 는 없는 파일이 된다.
    Windows 는 파일명에 `"` 를 허용하지 않으니 겹따옴표로 감싸는 것으로 끝이다.
    """
    return f'"{value}"'


def hook_command(python: str | None = None, script: Path | None = None,
                 *, windows: bool | None = None) -> str:
    """hook 에 박을 한 줄. 플랫폼마다 이 줄을 돌리는 셸이 다르다.

    `windows` 를 주지 않으면 지금 도는 OS 를 따른다. 설치기와 verify 가 같은
    함수를 쓰므로, 설치한 것과 점검하는 것이 어긋날 수 없다.
    """
    if windows is None:
        windows = os.name == "nt"
    quote = win_quote if windows else shlex.quote
    template = HOOK_TEMPLATE_WINDOWS if windows else HOOK_TEMPLATE
    return template.format(python=quote(python or hook_python()),
                           script=quote(str(script or Path(__file__).resolve())))


def load_json(path: Path, fallback: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fallback


def backup(path: Path, *, create: bool = True) -> Path | None:
    """설치 직전 상태의 사본을 딱 한 번 남긴다.

    이미 사본이 있으면 덮어쓰지 않는다 — 사본은 언제나 "우리가 처음 손대기
    전"을 가리켜야 롤백이 의미를 갖는다. 제거 경로에서는 새로 만들지 않는다
    (우리가 만든 파일의 사본은 쓰레기일 뿐이다).
    """
    target = path.with_suffix(path.suffix + ".llmwiki-bak")
    if target.exists():
        return target
    if not create or not path.exists():
        return None
    target.write_bytes(path.read_bytes())
    return target


def install_hook(path: Path, command: str, *, remove: bool) -> dict[str, Any]:
    """`hooks.UserPromptSubmit` 배열에 우리 그룹만 더하거나 뺀다.

    기존 그룹(Orca 등)은 읽고 그대로 되돌려 쓴다. 절대 덮어쓰지 않는다.
    """
    if not path.exists() and remove:
        return {"file": str(path), "changed": False, "reason": "missing"}
    config = load_json(path, {})
    if not isinstance(config, dict):
        return {"file": str(path), "changed": False, "reason": "unparsable"}
    hooks = config.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        return {"file": str(path), "changed": False, "reason": "unexpected-shape"}
    groups = hooks.get(HOOK_EVENT)
    if not isinstance(groups, list):
        groups = [] if groups is None else [groups]

    before = len(groups)
    kept = [g for g in groups
            if not (isinstance(g, dict) and HOOK_MARKER in json.dumps(g, ensure_ascii=False))]
    removed = before - len(kept)
    if not remove:
        kept.append({"hooks": [{"type": "command", "command": command,
                                "timeout": HOOK_TIMEOUT}]})
    hooks[HOOK_EVENT] = kept
    if not kept:
        hooks.pop(HOOK_EVENT)
    if not hooks:
        # 빈 hooks 껍데기를 남기지 않는다 — 제거 뒤 파일이 원래대로 보여야 한다.
        config.pop("hooks")

    saved = backup(path, create=not remove)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"file": str(path), "changed": True, "backup": str(saved) if saved else None,
            "groups_before": before, "groups_after": len(kept), "replaced": removed}


def install_guide(path: Path, body: str, *, remove: bool) -> dict[str, Any]:
    """전역 지침 파일에 우리 섹션만 더하거나 뺀다. 기존 본문은 보존한다."""
    start, end = "<!-- llmwiki-context:start -->", "<!-- llmwiki-context:end -->"
    current = path.read_text(encoding="utf-8") if path.exists() else ""
    stripped = re.sub(re.escape(start) + r".*?" + re.escape(end) + r"\n?", "", current,
                      flags=re.DOTALL)
    if remove:
        updated = stripped.rstrip() + "\n" if stripped.strip() else ""
    else:
        prefix = stripped.rstrip() + "\n\n" if stripped.strip() else ""
        updated = prefix + start + "\n" + body + "\n" + end + "\n"
    if updated == current:
        return {"file": str(path), "changed": False}
    saved = backup(path, create=not remove)
    if remove and not updated:
        # 우리가 만든 파일이면 빈 껍데기를 남기지 않는다.
        path.unlink(missing_ok=True)
        return {"file": str(path), "changed": True, "removed": True,
                "backup": str(saved) if saved else None}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(updated, encoding="utf-8")
    return {"file": str(path), "changed": True, "backup": str(saved) if saved else None}


def guide_body(root: Path, python: str | None = None) -> str:
    """전역 지침에 넣을 문단. 경로는 설치 시점에 해석해 그대로 박는다."""
    interpreter = shlex.quote(python or hook_python())
    script = shlex.quote(str(Path(__file__).resolve()))
    wiki_cli = shlex.quote(str(Path(__file__).resolve().parent / "llmwiki.py"))
    return "\n".join([
        "## llmwiki_json 개인 지식 위키",
        "",
        f"- 정본: `{root}/wiki/**/*.json`. 파생물(`index/`, `viewer/public/data/`)은 "
        f"`{interpreter} {wiki_cli} build` 로만 갱신한다. "
        "`raw/` 는 수정하지 않는다.",
        "- 질문마다 `UserPromptSubmit` hook 이 관련 근거를 `<llmwiki-context>` 블록으로 "
        "자동 주입한다. 그 안의 page/block ID 를 근거로 답한다. 주소만 온 경우도 있는데, "
        "그때는 본문이 실리지 않았다는 뜻이니 아래 조회로 필요한 block 만 가져온다.",
        "- 조회는 언제나 **필요한 object 단위**로 한다. page 전체 JSON 을 통째로 읽지 않는다:",
        f"  - block 목차: `{interpreter} {script} get <slug>`",
        f"  - block 하나: `{interpreter} {script} get <slug> --block <block id>`",
        f"  - 더 찾기: `{interpreter} {script} search \"<질문>\"`",
        "  - MCP 를 쓸 수 있으면 `llmwiki_get(selector, blocks=[...])` / `llmwiki_search` 가 "
        "같은 일을 한다.",
        f"- page 전체가 정말 필요할 때만 `{interpreter} {script} get <slug> --mode page` "
        f"또는 `{interpreter} {wiki_cli} get <slug>` 를 쓴다. 보통은 필요 없다.",
        "- 주입된 근거와 어긋나는 주장을 하기 전에 반드시 해당 page 를 다시 읽는다. "
        "`⚠️` 로 표시된 상충은 판정 전까지 양쪽을 병기한다.",
    ])


def do_install(root: Path, *, remove: bool, guides: bool,
               clients: Iterable[str] = CLIENTS, python: str | None = None) -> dict[str, Any]:
    command = hook_command(python)
    body = guide_body(root, python)
    report: dict[str, Any] = {"command": command, "clients": {}}
    for name in clients:
        if name not in CLIENT_FILES:
            raise WikiError(f"unknown client: {name}")
        hooks_path, guide_path = client_paths(name)
        previous = installed_command(hooks_path)
        if name == "codex" and not remove and previous and previous != command:
            # 명령이 바뀌면 Codex 는 신뢰를 다시 묻는다. verify 가 그 사실을
            # 알아야 "신뢰됨" 이라고 거짓말하지 않는다.
            mark_codex_trust_stale(hooks_path, installed_group_index(hooks_path))
        if name == "codex" and remove:
            clear_codex_trust_stale()
        entry: dict[str, Any] = {"hook": install_hook(hooks_path, command, remove=remove)}
        entry["command_changed"] = bool(previous) and previous != command and not remove
        if guides:
            entry["guide"] = install_guide(guide_path, body, remove=remove)
        report["clients"][name] = entry
    return report


# --------------------------------------------------------------------------- verify
def installed_group_index(path: Path) -> int:
    """hooks 배열에서 우리 그룹의 위치. 없으면 -1."""
    config = load_json(path, {})
    groups = (config.get("hooks") or {}).get(HOOK_EVENT) if isinstance(config, dict) else None
    for index, group in enumerate(groups if isinstance(groups, list) else []):
        if isinstance(group, dict) and HOOK_MARKER in json.dumps(group, ensure_ascii=False):
            return index
    return -1


def installed_command(path: Path) -> str:
    config = load_json(path, {})
    groups = (config.get("hooks") or {}).get(HOOK_EVENT) if isinstance(config, dict) else None
    for group in groups if isinstance(groups, list) else []:
        for hook in (group.get("hooks") or []) if isinstance(group, dict) else []:
            command = hook.get("command", "") if isinstance(hook, dict) else ""
            if HOOK_MARKER in command:
                return command
    return ""


def state_dir() -> Path:
    """설치기가 남기는 기록 자리. install.sh 와 같은 곳을 본다."""
    override = os.environ.get(ENV_STATE_DIR)
    return Path(override) if override else home() / ".llmwiki"


def codex_trust_hash(hooks_path: Path, index: int) -> str:
    """Codex 가 우리 hook 슬롯에 대해 기억하는 신뢰 지문. 없으면 빈 문자열.

    `[hooks.state."<hooks.json>:<event>:<group>:<hook>"]` 아래 `trusted_hash`.
    """
    if index < 0:
        return ""
    try:
        text = (home() / CODEX_CONFIG).read_text(encoding="utf-8")
    except OSError:
        return ""
    section = f'[hooks.state."{hooks_path}:{CODEX_TRUST_EVENT}:{index}:0"]'
    if section not in text:
        return ""
    tail = text.split(section, 1)[1].split("\n[", 1)[0]
    match = re.search(r'trusted_hash\s*=\s*"([^"]+)"', tail)
    return match.group(1) if match else ""


def codex_trust(hooks_path: Path, index: int) -> str:
    """Codex 가 지금 설치된 hook 명령을 신뢰하는지.

    해시 계산식은 공개돼 있지 않아 우리가 직접 검산할 수 없다. 대신 hook
    명령을 바꿀 때 그 시점의 지문을 `codex-trust-stale` 에 적어 둔다. Codex 는
    명령이 바뀌면 새 지문을 요구하므로, 저장된 지문이 아직 그대로면 사용자가
    다시 신뢰를 주지 않았다는 뜻이다 — 그때 trusted 라고 말하면 거짓이 된다.
    """
    if index < 0:
        return "not-installed"
    if not (home() / CODEX_CONFIG).exists():
        return "unknown"
    current = codex_trust_hash(hooks_path, index)
    if not current:
        return "review-required"
    stale = state_dir() / CODEX_STALE_TRUST
    try:
        if stale.read_text(encoding="utf-8").strip() == current:
            return "review-required"
    except OSError:
        pass
    return "trusted"


def mark_codex_trust_stale(hooks_path: Path, index: int) -> None:
    """hook 명령을 바꿨다 — 지금 지문은 옛 명령의 것이라고 적어 둔다."""
    current = codex_trust_hash(hooks_path, index)
    if not current:
        return
    target = state_dir() / CODEX_STALE_TRUST
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(current + "\n", encoding="utf-8")


def clear_codex_trust_stale() -> None:
    (state_dir() / CODEX_STALE_TRUST).unlink(missing_ok=True)


PROBE_MIN_CHARS = 40
PROBE_WORDS = 6
PROBE_KINDS = frozenset({"paragraph", "list", "table"})


def probe_query(docs: list[Doc]) -> tuple[str, str]:
    """end-to-end 확인용 (질문, 기대 page id). 정본 **본문 block** 에서 고른다.

    제목·heading 은 근거(B)에서 제외되므로 제목으로 물으면 1위 page 에 block 이 없어 본문이
    비고 probe 가 헛되이 실패한다. 그래서 본문 block(paragraph/list/table, 40자 이상) 중 가장
    긴 것에서 코퍼스 전체에 가장 드문 내용어 6개를 뽑아 질문으로 쓴다. supersedes 로 대체된
    page 는 본문이 나가지 않으므로 제외한다. 저장소 내용에 의존하지 않도록 코퍼스에서 직접
    고르므로 어떤 clone 에서도 같은 검사가 돈다.
    """
    superseded: set[str] = set()
    for doc in docs:
        for link in IDX.implied_links(doc.page):
            if str(link.get("kind") or "") == "supersedes":
                superseded.add(IDX.link_key(str(link.get("target") or "")))
    best_text, best_doc = "", None
    for doc in docs:
        keys = {IDX.link_key(doc.page_id), IDX.link_key(doc.slug),
                IDX.link_key(norm(doc.page.get("title")))}
        if keys & superseded:
            continue
        blocks = doc.page.get("blocks") or {}
        for bid in doc.page.get("block_order") or list(blocks):
            b = blocks.get(bid)
            if not isinstance(b, dict) or str(b.get("kind") or "") not in PROBE_KINDS:
                continue
            text = norm(redact(block_text(b)))
            if len(text) >= PROBE_MIN_CHARS and len(text) > len(best_text):
                best_text, best_doc = text, doc
    if best_doc is None:
        return "", ""
    # block 의 내용어 중 다른 page 에 가장 드문 것부터. 숫자만인 토큰은 신호가 아니다.
    seen: list[str] = []
    for tok in query_tokens(best_text):
        if not tok.isdigit() and tok not in seen:
            seen.append(tok)
    haystacks = [" ".join(d.fields.values()) for d in docs]

    def rarity(tok: str) -> tuple[int, int, int]:
        df = sum(1 for h in haystacks if tok in h)
        return (df, -len(tok), seen.index(tok))
    words = sorted(seen, key=rarity)[:PROBE_WORDS]
    words.sort(key=seen.index)
    return " ".join(words), best_doc.page_id


def verify(root: Path, *, clients: Iterable[str] = CLIENTS,
           python: str | None = None) -> dict[str, Any]:
    """설치 상태를 사실대로 보고한다. 고치지 않는다."""
    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: Any = "", *, warn: bool = False) -> None:
        item: dict[str, Any] = {"check": name, "ok": bool(ok), "detail": detail}
        if warn:  # 통과는 하지만 사용자가 손봐야 할 것이 있다
            item["warn"] = True
        checks.append(item)

    interpreter = python or hook_python()
    check("python", os.access(interpreter, os.X_OK), interpreter)
    check("repo", (root / "wiki").is_dir(), str(root))

    docs = load_corpus(root)
    check("corpus", bool(docs), f"{len(docs)} pages")

    expected = hook_command(python)
    for name in clients:
        hooks_path, guide_path = client_paths(name)
        index = installed_group_index(hooks_path)
        check(f"{name}.hook", index >= 0, f"{hooks_path} (group {index})")
        if index >= 0:
            found = installed_command(hooks_path)
            check(f"{name}.hook-current", found == expected,
                  "installed command matches this checkout" if found == expected
                  else "stale command — 다시 install 하라")
        guide = guide_path.read_text(encoding="utf-8") if guide_path.exists() else ""
        check(f"{name}.guide", "llmwiki-context:start" in guide, str(guide_path))
        if name == "codex":
            state = codex_trust(hooks_path, index)
            check("codex.trust", state == "trusted",
                  f"{state} — Codex 를 한 번 띄워 hook review 에서 신뢰를 주면 된다"
                  if state != "trusted" else "trusted")

    idx, why = open_index(root)
    if idx is not None:
        idx.close()
    # 색인이 없어도 훅은 정본에서 만든 메모리 색인으로 같은 형식을 낸다 — 사실만 적고 실패로 치지 않는다.
    check("search-index", True,
          f"{index_path(root)}" if idx is not None
          else f"{why} — 정본에서 메모리 색인을 만들어 동작한다 (build 를 돌리면 색인 경로)")

    # probe 는 두 검사로 나눈다 — 검색이 page 를 찾는지, 그 결과가 본문(B)까지 주입되는지.
    # 하나가 실패하면 어느 단계가 문제인지 detail 로 알 수 있다.
    query, expected = probe_query(docs)
    check("probe-query", bool(query),
          f"query={query!r} expect={expected}" if query
          else "본문 block(paragraph/list/table, 40자 이상)이 있는 page 가 없다")
    found: dict[str, Any] = {}
    if query:
        try:
            found = search_rows(root, query, MAX_PAGES)
        except Exception as exc:  # noqa: BLE001 - 검증은 절대 예외로 죽지 않는다
            found = {"error": type(exc).__name__, "results": []}
    ids = [r.get("id") for r in found.get("results") or []]
    check("probe-search", bool(ids),
          f"mode={found.get('mode', '?')} hits={len(ids)} "
          f"expected_hit={expected in ids} top={ids[0] if ids else None}"
          + (f" fallback={found['fallback']}" if found.get("fallback") else "")
          + (f" error={found['error']}" if found.get("error") else ""))

    payload = json.dumps({"prompt": query, "cwd": str(root),
                          "hook_event_name": HOOK_EVENT}, ensure_ascii=False)
    injected = ""
    stats: dict[str, Any] = {}
    if query:
        try:
            injected, stats = run_hook(root, payload, max_bytes=MAX_BYTES, max_tokens=MAX_TOKENS)
        except Exception as exc:  # noqa: BLE001
            injected = ""
            check("probe-error", False, type(exc).__name__)
    body = ""
    if injected:
        try:
            body = json.loads(injected)["hookSpecificOutput"]["additionalContext"]
        except (ValueError, KeyError, TypeError):
            body = ""
    has_block = any(line.startswith("B ") for line in body.splitlines())
    check("probe-injects", bool(body) and has_block,
          f"mode={stats.get('mode', '?')} reason={stats.get('reason', '?')} "
          f"bytes={stats.get('bytes', 0)} expected_page={expected in (stats.get('pages') or [])} "
          f"block_lines={has_block}"
          + (f" fallback={stats['fallback']}" if stats.get("fallback") else ""))

    noise = json.dumps({"prompt": "zzqq xxyy vvww 1234567", "cwd": str(root),
                        "hook_event_name": HOOK_EVENT}, ensure_ascii=False)
    quiet, _ = run_hook(root, noise, max_bytes=MAX_BYTES, max_tokens=MAX_TOKENS, use_always=False)
    check("probe-silent-on-noise", quiet == "", "정본과 한 토큰도 겹치지 않는 질문에는 주입하지 않는다")

    broken, _ = run_hook(root, "not json at all", max_bytes=MAX_BYTES, max_tokens=MAX_TOKENS)
    check("fail-open", broken == "", "malformed stdin 은 조용히 통과한다")

    # 옛 qmd MCP 등록은 우리가 만든 것이 아니라 떼지 않는다 — 있으면 warn 으로 떼는 명령을 알려 준다.
    legacy = legacy_qmd_mcp(root)
    check("legacy-qmd-mcp", True,
          "없음" if not legacy else
          "옛 qmd MCP 등록이 남아 있다 (collection 이 없어 빈손으로 답한다). 떼려면: "
          + " ; ".join(f"{e['remove']}  [{e['where']}]" for e in legacy),
          warn=bool(legacy))

    return {"ok": all(c["ok"] for c in checks), "root": str(root),
            "python": interpreter, "command": expected, "checks": checks}


# --------------------------------------------------------------------------- CLI
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="llmwiki-context",
        description="llmwiki_json 정본에서 질문 관련 근거를 찾아 컨텍스트로 주입한다")
    parser.add_argument("--root", help=f"저장소 루트 (기본: ${ENV_ROOT} 또는 {DEFAULT_ROOT})")
    sub = parser.add_subparsers(dest="command", required=True)

    for name, help_text in (("search", "관련 page 후보를 점수순으로 출력"),
                            ("context", "주입용 컨텍스트 본문을 출력")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("query")
        p.add_argument("--limit", type=int, default=GRAPH_PAGES,
                       help=f"page 수 상한 (색인 {GRAPH_PAGES}, 스캔 경로는 {MAX_PAGES} 로 잘린다)")
        p.add_argument("--max-bytes", type=int, default=env_int(ENV_MAX_BYTES, MAX_BYTES))
        p.add_argument("--max-tokens", type=int, default=env_int(ENV_MAX_TOKENS, MAX_TOKENS))
        p.add_argument("--max-blocks", type=int, default=MAX_BLOCKS)
        p.add_argument("--min-score", type=float, default=MIN_SCORE,
                       help="최후 스캔 경로의 본문 문턱 (--scan 일 때만)")
        p.add_argument("--min-coverage", type=float, default=MIN_COVERAGE)
        p.add_argument("--no-index", action="store_true", default=not env_flag(ENV_INDEX, True),
                       help="디스크 색인을 쓰지 않고 정본에서 메모리 색인을 만든다 "
                            "(기본: $LLMWIKI_CONTEXT_INDEX)")
        p.add_argument("--scan", action="store_true", default=not env_flag(ENV_MEMORY, True),
                       help="디스크 색인도 메모리 색인도 쓰지 않고 최후 스캔 경로로 간다 "
                            "(기본: $LLMWIKI_CONTEXT_MEMORY)")
        p.add_argument("--silence", type=float, default=env_float(ENV_SILENCE, SILENCE_T_DEFAULT),
                       help="무주입 문턱 raw_top×coverage (0=끔, 기본 $LLMWIKI_CONTEXT_SILENCE)")
        p.add_argument("--hint", type=float, default=env_float(ENV_HINT, HINT_T_DEFAULT),
                       help="주소만 문턱 (0=끔, 기본 $LLMWIKI_CONTEXT_HINT)")
        p.add_argument("--json", action="store_true", dest="as_json")

    hook = sub.add_parser("hook", help="UserPromptSubmit hook (stdin/stdout JSON)")
    hook.add_argument("--max-bytes", type=int, default=env_int(ENV_MAX_BYTES, MAX_BYTES))
    hook.add_argument("--max-tokens", type=int, default=env_int(ENV_MAX_TOKENS, MAX_TOKENS))
    hook.add_argument("--limit", type=int, default=0, help="page 수 상한 (0=경로별 기본값)")
    hook.add_argument("--no-index", action="store_true", default=not env_flag(ENV_INDEX, True))
    hook.add_argument("--scan", action="store_true", default=not env_flag(ENV_MEMORY, True))

    get = sub.add_parser("get", help="page 를 필요한 만큼만 읽는다 (기본: block 목차)")
    get.add_argument("selector", help="slug · page:slug · 제목 · slug#block-id · block id")
    get.add_argument("--block", action="append", default=[], dest="blocks",
                     help="가져올 block id (반복 가능, 꼬리만 준 축약형도 된다)")
    get.add_argument("--field", action="append", default=[], dest="fields",
                     help=f"실을 page 필드 (반복 가능). 가능: {', '.join(META_FIELDS)}")
    get.add_argument("--mode", choices=list(GET_MODES), default="outline",
                     help="outline(기본) · blocks · page(통째로, 비쌈)")
    get.add_argument("--preview-chars", type=int, default=OUTLINE_CHARS,
                     help="outline 에서 block 미리보기 길이")

    sub.add_parser("mcp", help="읽기 전용 MCP 서버 (stdio)")
    sub.add_parser("doctor", help="해석된 경로와 설정을 점검")

    for name, help_text in (("install", "Codex/Claude 전역 UserPromptSubmit hook 설치"),
                            ("verify", "설치 상태를 점검한다 (아무것도 고치지 않는다)")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--client", action="append", choices=sorted(CLIENTS), default=[],
                       help="대상 클라이언트 (반복 가능, 기본: 전부)")
        p.add_argument("--python", help="hook 에 못박을 인터프리터 절대경로")
        if name == "install":
            p.add_argument("--remove", action="store_true",
                           help="설치한 hook 과 지침만 되돌린다")
            p.add_argument("--no-guides", action="store_true",
                           help="~/.codex/AGENTS.md, ~/.claude/CLAUDE.md 는 건드리지 않는다")
            p.add_argument("--dry-run", action="store_true", help="바뀔 내용만 보여준다")
        else:
            p.add_argument("--json", action="store_true", dest="as_json")
    return parser


def arm_watchdog(seconds: float) -> None:
    """hook 이 어떤 이유로든 오래 걸리면 조용히 빠져나온다(질문을 막지 않는다)."""
    try:
        import signal

        def bail(_sig: int, _frame: Any) -> None:
            os._exit(0)

        signal.signal(signal.SIGALRM, bail)
        signal.setitimer(signal.ITIMER_REAL, seconds)
    except Exception:  # noqa: BLE001 - 워치독은 없으면 없는 대로 간다
        pass


def resolve_root(explicit: str | None) -> Path:
    return Path(explicit or os.environ.get(ENV_ROOT) or DEFAULT_ROOT).resolve()


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = resolve_root(args.root)

    if args.command == "hook":
        if env_flag(ENV_DISABLE, False):
            return 0
        arm_watchdog(env_float(ENV_TIMEOUT, WATCHDOG_SECONDS))
        try:
            stdin_text = sys.stdin.read()
        except Exception:  # noqa: BLE001
            return 0
        try:
            out, stats = run_hook(root, stdin_text, max_bytes=args.max_bytes,
                                  max_tokens=args.max_tokens, max_pages=args.limit,
                                  use_index=not (args.no_index or args.scan),
                                  use_memory=not args.scan)
        except Exception as exc:  # noqa: BLE001 - fail-open
            log_event({"event": HOOK_EVENT, "error": type(exc).__name__})
            return 0
        log_event(stats)
        if out:
            sys.stdout.write(out)
        return 0

    if args.command == "get":
        payload = get_page(root, args.selector, mode=args.mode, blocks=args.blocks,
                           fields=args.fields, block_chars=args.preview_chars)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 1 if payload.get("error") else 0

    if args.command == "mcp":
        return mcp_serve(root)

    if args.command in {"install", "verify"}:
        clients = tuple(args.client) or CLIENTS
        if args.command == "verify":
            report = verify(root, clients=clients, python=args.python)
            if args.as_json:
                print(json.dumps(report, ensure_ascii=False, indent=2))
            else:
                for item in report["checks"]:
                    mark = "warn" if item.get("warn") else ("ok  " if item["ok"] else "FAIL")
                    print(f"{mark} {item['check']}: {item['detail']}")
                print("모든 점검 통과" if report["ok"] else "점검 실패 — 위 FAIL 항목을 보라")
            return 0 if report["ok"] else 1
        if args.dry_run:
            plan = {"command": hook_command(args.python), "clients": {}}
            for name in clients:
                hooks_path, guide_path = client_paths(name)
                plan["clients"][name] = {
                    "hooks_file": str(hooks_path),
                    "hooks_exists": hooks_path.exists(),
                    "installed_group": installed_group_index(hooks_path),
                    "guide_file": str(guide_path),
                    "guide_installed": guide_path.exists()
                    and "llmwiki-context:start" in guide_path.read_text(encoding="utf-8"),
                }
            plan["guide"] = guide_body(root, args.python)
            print(json.dumps(plan, ensure_ascii=False, indent=2))
            return 0
        print(json.dumps(do_install(root, remove=args.remove, guides=not args.no_guides,
                                    clients=clients, python=args.python),
                         ensure_ascii=False, indent=2))
        return 0

    if args.command == "doctor":
        docs = load_corpus(root)
        idx, why = open_index(root)
        search_info: dict[str, Any] = {"file": str(index_path(root)), "fresh": idx is not None}
        if idx is not None:
            search_info.update({"pages": idx.npages, "blocks": idx.nblocks, "revision": idx.revision,
                                "heading_paths": idx.heading_paths})
            idx.close()
        else:
            search_info["fallback"] = why
        print(json.dumps({
            "root": str(root),
            "exists": root.is_dir(),
            "wiki_pages": len(docs),
            "index_present": sorted(p.name for p in (root / "index").glob("*.json")),
            "search_index": search_info,
            "index_enabled": env_flag(ENV_INDEX, True),
            "silence_threshold": env_float(ENV_SILENCE, SILENCE_T_DEFAULT),
            "max_bytes": env_int(ENV_MAX_BYTES, MAX_BYTES),
            "max_tokens": env_int(ENV_MAX_TOKENS, MAX_TOKENS),
            "disabled": env_flag(ENV_DISABLE, False),
            "script": str(Path(__file__).resolve()),
            # install.sh 는 자기가 고른 인터프리터를 --python 으로 넘기므로,
            # 실제로 박힌 것과 다를 수 있다. 이건 "아무도 안 정해줬을 때의 기본".
            "default_python": hook_python(),
            "clients": {name: {"hooks": str(h), "guide": str(g),
                               "installed_group": installed_group_index(h)}
                        for name in CLIENTS for h, g in [client_paths(name)]},
            # 옛 qmd MCP 등록이 남아 있으면 어디에, 어떻게 떼는지. 비어 있으면 없다.
            "legacy_qmd_mcp": legacy_qmd_mcp(root),
        }, ensure_ascii=False, indent=2))
        return 0

    if args.command == "search":
        if args.no_index or args.scan:
            os.environ[ENV_INDEX] = "0"
        if args.scan:
            os.environ[ENV_MEMORY] = "0"
        print(json.dumps(search_rows(root, args.query, args.limit), ensure_ascii=False, indent=2))
        return 0

    text, result, pages = build_context(
        root, args.query, limit=args.limit, use_index=not (args.no_index or args.scan),
        use_memory=not args.scan,
        silence_t=args.silence, hint_t=args.hint,
        min_score=args.min_score, min_coverage=args.min_coverage,
        max_bytes=args.max_bytes, max_tokens=args.max_tokens, max_blocks=args.max_blocks)

    if args.as_json:
        payload = {"query": result.query, "reason": result.reason, "mode": result.mode,
                   "coverage": round(result.coverage, 3),
                   "bytes": len(text.encode("utf-8")), "est_tokens": est_tokens(text),
                   "pages": pages, "text": text}
        if result.fallback:
            payload["fallback"] = result.fallback
        if result.grade:
            payload["grade"] = result.grade
        if result.signals:
            payload["signals"] = {k: round(float(v), 4) for k, v in result.signals.items()}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    if text:
        print(text)
    return 0


def main() -> None:
    try:
        raise SystemExit(run())
    except (BrokenPipeError, KeyboardInterrupt):
        raise SystemExit(0)


if __name__ == "__main__":
    main()

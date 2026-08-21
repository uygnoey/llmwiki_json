#!/usr/bin/env python3
"""llmwiki_json 자동 컨텍스트 주입 CLI.

사용자의 질문이 Codex / Claude Code 로 전달되기 전에 이 저장소의 정본 JSON
(`wiki/**/*.json`)에서 관련 근거를 찾아 압축된 컨텍스트로 주입한다.

설계 계약
--------
- 검색과 주입 모두 **정본 JSON 만** 읽는다. `index/*.json` 같은 파생물은
  주입 본문의 출처가 되지 않는다(경로 표기 보조로만 쓸 수 있다).
- qmd 는 **후보 탐색**에만 쓴다. qmd 가 찾아준 문서라도 최종 본문은 해당
  slug 의 정본 page/block/field projection 에서 다시 읽는다.
- 관련도가 낮으면 아무것도 주입하지 않는다(무주입).
- 어떤 오류가 나도 질문을 막지 않는다(fail-open: stdout 비우고 exit 0).
- 자격증명으로 보이는 문자열은 출력 직전에 마스킹한다.
- 어느 cwd 에서 실행해도 이 파일 위치 기준 절대경로로 저장소를 찾는다.

환경변수
--------
LLMWIKI_ROOT                저장소 루트 override (기본: 이 파일의 부모의 부모)
LLMWIKI_CONTEXT_DISABLE     1 이면 hook 이 아무것도 하지 않는다
LLMWIKI_CONTEXT_MAX_BYTES   주입 본문 UTF-8 바이트 상한 (기본 6000)
LLMWIKI_CONTEXT_MAX_TOKENS  주입 본문 추정 토큰 상한 (기본 2000)
LLMWIKI_CONTEXT_QMD         0 이면 qmd 후보 탐색을 끈다 (기본 1)
LLMWIKI_CONTEXT_QMD_COLLECTION  qmd collection 이름 (기본 llmwiki_json)
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
import subprocess
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

DEFAULT_ROOT = Path(__file__).resolve().parents[1]

ENV_ROOT = "LLMWIKI_ROOT"
ENV_DISABLE = "LLMWIKI_CONTEXT_DISABLE"
ENV_MAX_BYTES = "LLMWIKI_CONTEXT_MAX_BYTES"
ENV_MAX_TOKENS = "LLMWIKI_CONTEXT_MAX_TOKENS"
ENV_QMD = "LLMWIKI_CONTEXT_QMD"
ENV_QMD_COLLECTION = "LLMWIKI_CONTEXT_QMD_COLLECTION"
ENV_TIMEOUT = "LLMWIKI_CONTEXT_TIMEOUT"
ENV_LOG = "LLMWIKI_CONTEXT_LOG"
ENV_STATE_DIR = "LLMWIKI_STATE_DIR"

DEFAULT_QMD_COLLECTION = "llmwiki_json"
HOOK_EVENT = "UserPromptSubmit"

# 주입 예산 기본값. 매 프롬프트마다 붙으므로 보수적으로 잡는다.
MAX_BYTES = 6000
MAX_TOKENS = 2000
MAX_PAGES = 5
MAX_BLOCKS = 6
MAX_BLOCK_CHARS = 320
WATCHDOG_SECONDS = 6.0

# 주입 판정 문턱. 둘 다 넘어야 주입한다.
MIN_SCORE = 6.0
MIN_COVERAGE = 0.34
# 정본 점수가 이 구간이면 "약한 신호"로 보고 qmd 후보 탐색을 한 번 시도한다.
QMD_FLOOR = 1.0

TOKEN_RE = re.compile(r"[0-9A-Za-z_][0-9A-Za-z_.\-]*|[가-힣]+")
HANGUL_RE = re.compile(r"^[가-힣]+$")
SECRET = re.compile(r"(?i)(api[_-]?key|access[_-]?token|password|passwd|secret|connection[_-]?string)"
                    r"\s*[:=]\s*[^\s,;\"']+")
SECRET_MASK = r"\1: (접속 정보 생략)"

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
    """자격증명처럼 보이는 값은 절대 컨텍스트로 내보내지 않는다."""
    return SECRET.sub(SECRET_MASK, text)


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
    doc: Doc
    score: float
    matched: set[str]
    via: str = "canonical"


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


# --------------------------------------------------------------------------- qmd
def qmd_slugs(query: str, collection: str, limit: int, timeout: float) -> list[str]:
    """qmd 는 후보 slug 만 돌려준다. 본문은 절대 qmd 에서 가져오지 않는다."""
    try:
        proc = subprocess.run(
            ["qmd", "search", query, "-c", collection, "--format", "json", "-n", str(limit)],
            capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0 or not proc.stdout.strip():
        return []
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return []
    rows = payload.get("results") if isinstance(payload, dict) else payload
    slugs: list[str] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        raw = row.get("path") or row.get("file") or row.get("uri") or ""
        stem = Path(str(raw).split("?")[0]).stem
        if stem and stem not in slugs:
            slugs.append(stem)
    return slugs


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


def retrieve(root: Path, query: str, *, limit: int = MAX_PAGES, use_qmd: bool = True,
             collection: str = DEFAULT_QMD_COLLECTION, min_score: float = MIN_SCORE,
             min_coverage: float = MIN_COVERAGE, qmd_timeout: float = 2.0) -> Result:
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

    weak = top < min_score or coverage < min_coverage
    if weak and use_qmd and top >= QMD_FLOOR:
        # 정본 어휘가 질문과 살짝만 겹칠 때만 qmd 를 부른다. 완전 무관한
        # 질문에서는 qmd 를 호출하지 않으므로 지연도 붙지 않는다.
        by_slug = {d.slug: d for d in docs}
        promoted = [by_slug[s] for s in qmd_slugs(query, collection, limit, qmd_timeout)
                    if s in by_slug]
        if promoted:
            known = {h.doc.page_id for h in hits}
            extra, _ = rank(promoted, tokens)
            for hit in extra:
                hit.via = "qmd"
                if hit.doc.page_id in known:
                    for existing in hits:
                        if existing.doc.page_id == hit.doc.page_id:
                            existing.via = "canonical+qmd"
                            existing.score += 1.5
                else:
                    hits.append(hit)
            hits.sort(key=lambda h: (-h.score, h.doc.page_id))
            top = hits[0].score
            coverage = len(hits[0].matched) / len(tokens)
            reason = "canonical+qmd"

    if not hits:
        return Result(query, root, [], idf, tokens, 0.0, "no-match")
    if top < min_score or coverage < min_coverage:
        return Result(query, root, [], idf, tokens, coverage, "below-threshold")
    return Result(query, root, hits[:limit], idf, tokens, coverage, reason)


# --------------------------------------------------------------------------- projection
def project_hit(hit: Hit, tokens: list[str], idf: dict[str, float], *,
                max_blocks: int = MAX_BLOCKS,
                max_block_chars: int = MAX_BLOCK_CHARS) -> dict[str, Any]:
    """주입에 필요한 page/block/field 만 뽑는다. 페이지 전체를 싣지 않는다."""
    page = hit.doc.page
    blocks = rank_blocks(hit.doc, tokens, idf, max_blocks)
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
           max_tokens: int = MAX_TOKENS) -> str:
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
        "",
    ]
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


def build_context(root: Path, query: str, **options: Any) -> tuple[str, Result, list[dict[str, Any]]]:
    max_bytes = options.pop("max_bytes", MAX_BYTES)
    max_tokens = options.pop("max_tokens", MAX_TOKENS)
    max_blocks = options.pop("max_blocks", MAX_BLOCKS)
    max_block_chars = options.pop("max_block_chars", MAX_BLOCK_CHARS)
    result = retrieve(root, query, **options)
    pages = [project_hit(h, result.tokens, result.idf, max_blocks=max_blocks,
                         max_block_chars=max_block_chars) for h in result.hits]
    return render(result, pages, max_bytes=max_bytes, max_tokens=max_tokens), result, pages


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
    if "<llmwiki-context>" in text:
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


def run_hook(root: Path, stdin_text: str, *, use_qmd: bool, max_bytes: int, max_tokens: int,
             max_pages: int, collection: str) -> tuple[str, dict[str, Any]]:
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

    text, result, pages = build_context(root, prompt, limit=max_pages, use_qmd=use_qmd,
                                        collection=collection, max_bytes=max_bytes,
                                        max_tokens=max_tokens)
    stats["reason"] = result.reason
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
        "description": "page slug 또는 page id 로 정본 page 를 읽는다. block 을 주면 그 block 만 돌려준다 (읽기 전용).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "slug 또는 page:slug"},
                "block": {"type": "string", "description": "block id (선택)"},
            },
            "required": ["selector"],
        },
    },
]


def mcp_call(root: Path, name: str, args: dict[str, Any]) -> str:
    if name == "llmwiki_search":
        result = retrieve(root, str(args.get("query", "")),
                          limit=int(args.get("limit") or MAX_PAGES),
                          min_score=0.0, min_coverage=0.0)
        rows = [{"id": h.doc.page_id, "slug": h.doc.slug, "title": norm(h.doc.page.get("title")),
                 "type": h.doc.page.get("type"), "updated": h.doc.page.get("updated"),
                 "score": round(h.score, 2), "file": h.doc.rel,
                 "summary": clip(redact(norm(h.doc.page.get("summary"))), 200)}
                for h in result.hits]
        return json.dumps({"query": result.query, "results": rows}, ensure_ascii=False, indent=2)
    if name == "llmwiki_context":
        text, result, pages = build_context(root, str(args.get("query", "")),
                                            max_bytes=int(args.get("max_bytes") or MAX_BYTES))
        return text or f"(관련 근거 없음 — {result.reason})"
    if name == "llmwiki_get":
        selector = str(args.get("selector", "")).strip()
        wanted = selector[5:] if selector.startswith("page:") else selector
        for doc in load_corpus(root):
            if doc.slug != wanted and doc.page_id != selector:
                continue
            block_id = args.get("block")
            if block_id:
                block = (doc.page.get("blocks") or {}).get(str(block_id))
                if not block:
                    return f"(block 없음: {block_id})"
                return redact(json.dumps(block, ensure_ascii=False, indent=2))
            return redact(json.dumps(doc.page, ensure_ascii=False, indent=2))
        return f"(page 없음: {selector})"
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
        "자동 주입한다. 그 안의 page/block ID 를 근거로 답하고, 필요하면 원문을 직접 확인한다.",
        f"- 더 찾아야 하면 `{interpreter} {script} search \"<질문>\"` 또는 "
        f"`{interpreter} {wiki_cli} get <slug>` 를 쓴다.",
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


def probe_query(docs: list[Doc]) -> str:
    """정본에서 가장 신호가 강한 page 제목을 뽑아 end-to-end 확인에 쓴다.

    저장소 내용에 의존하지 않도록 코퍼스에서 직접 고르므로, 어떤 clone 에서도
    같은 검사가 돈다.
    """
    best = ""
    for doc in docs:
        title = norm(doc.page.get("title"))
        if len(query_tokens(title)) >= 2 and len(title) > len(best):
            best = title
    return best or (norm(docs[0].page.get("title")) if docs else "")


def verify(root: Path, *, clients: Iterable[str] = CLIENTS,
           python: str | None = None) -> dict[str, Any]:
    """설치 상태를 사실대로 보고한다. 고치지 않는다."""
    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: Any = "") -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

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

    query = probe_query(docs)
    payload = json.dumps({"prompt": query, "cwd": str(root),
                          "hook_event_name": HOOK_EVENT}, ensure_ascii=False)
    injected = ""
    if query:
        try:
            injected, _ = run_hook(root, payload, use_qmd=False, max_bytes=MAX_BYTES,
                                   max_tokens=MAX_TOKENS, max_pages=MAX_PAGES,
                                   collection=DEFAULT_QMD_COLLECTION)
        except Exception as exc:  # noqa: BLE001 - 검증은 절대 예외로 죽지 않는다
            injected = ""
            check("probe-error", False, type(exc).__name__)
    check("probe-injects", bool(injected), f"query={query!r}")

    noise = json.dumps({"prompt": "zzqq xxyy vvww 1234567", "cwd": str(root),
                        "hook_event_name": HOOK_EVENT}, ensure_ascii=False)
    quiet, _ = run_hook(root, noise, use_qmd=False, max_bytes=MAX_BYTES,
                        max_tokens=MAX_TOKENS, max_pages=MAX_PAGES,
                        collection=DEFAULT_QMD_COLLECTION)
    check("probe-silent-on-noise", quiet == "", "무관 질문에는 주입하지 않는다")

    broken, _ = run_hook(root, "not json at all", use_qmd=False, max_bytes=MAX_BYTES,
                         max_tokens=MAX_TOKENS, max_pages=MAX_PAGES,
                         collection=DEFAULT_QMD_COLLECTION)
    check("fail-open", broken == "", "malformed stdin 은 조용히 통과한다")

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
        p.add_argument("--limit", type=int, default=MAX_PAGES)
        p.add_argument("--max-bytes", type=int, default=env_int(ENV_MAX_BYTES, MAX_BYTES))
        p.add_argument("--max-tokens", type=int, default=env_int(ENV_MAX_TOKENS, MAX_TOKENS))
        p.add_argument("--max-blocks", type=int, default=MAX_BLOCKS)
        p.add_argument("--min-score", type=float, default=MIN_SCORE)
        p.add_argument("--min-coverage", type=float, default=MIN_COVERAGE)
        p.add_argument("--no-qmd", action="store_true", default=not env_flag(ENV_QMD, True),
                       help="qmd 후보 탐색 비활성화 (기본: $LLMWIKI_CONTEXT_QMD)")
        p.add_argument("--collection", default=os.environ.get(ENV_QMD_COLLECTION,
                                                              DEFAULT_QMD_COLLECTION))
        p.add_argument("--json", action="store_true", dest="as_json")

    hook = sub.add_parser("hook", help="UserPromptSubmit hook (stdin/stdout JSON)")
    hook.add_argument("--max-bytes", type=int, default=env_int(ENV_MAX_BYTES, MAX_BYTES))
    hook.add_argument("--max-tokens", type=int, default=env_int(ENV_MAX_TOKENS, MAX_TOKENS))
    hook.add_argument("--limit", type=int, default=MAX_PAGES)
    hook.add_argument("--no-qmd", action="store_true", default=not env_flag(ENV_QMD, True))
    hook.add_argument("--collection", default=os.environ.get(ENV_QMD_COLLECTION,
                                                             DEFAULT_QMD_COLLECTION))

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
            out, stats = run_hook(root, stdin_text, use_qmd=not args.no_qmd,
                                  max_bytes=args.max_bytes, max_tokens=args.max_tokens,
                                  max_pages=args.limit, collection=args.collection)
        except Exception as exc:  # noqa: BLE001 - fail-open
            log_event({"event": HOOK_EVENT, "error": type(exc).__name__})
            return 0
        log_event(stats)
        if out:
            sys.stdout.write(out)
        return 0

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
                    mark = "ok  " if item["ok"] else "FAIL"
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
        print(json.dumps({
            "root": str(root),
            "exists": root.is_dir(),
            "wiki_pages": len(docs),
            "index_present": sorted(p.name for p in (root / "index").glob("*.json")),
            "qmd_collection": os.environ.get(ENV_QMD_COLLECTION, DEFAULT_QMD_COLLECTION),
            "qmd_enabled": env_flag(ENV_QMD, True),
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
        }, ensure_ascii=False, indent=2))
        return 0

    text, result, pages = build_context(
        root, args.query, limit=args.limit, use_qmd=not args.no_qmd,
        collection=args.collection, min_score=args.min_score, min_coverage=args.min_coverage,
        max_bytes=args.max_bytes, max_tokens=args.max_tokens, max_blocks=args.max_blocks)

    if args.command == "search":
        payload = {"query": result.query, "tokens": result.tokens, "reason": result.reason,
                   "coverage": round(result.coverage, 3),
                   "results": [{k: p[k] for k in ("id", "slug", "title", "type", "updated",
                                                  "score", "via", "file", "sources",
                                                  "unresolved_conflicts", "summary")}
                               for p in pages]}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if args.as_json:
        print(json.dumps({"query": result.query, "reason": result.reason,
                          "coverage": round(result.coverage, 3),
                          "bytes": len(text.encode("utf-8")), "est_tokens": est_tokens(text),
                          "pages": pages, "text": text}, ensure_ascii=False, indent=2))
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

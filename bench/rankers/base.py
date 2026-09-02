"""랭커 계약. bench/SPEC.md 참조. 이 파일은 coordinator 소유 — 수정 금지."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


@dataclass
class Hit:
    page_id: str
    score: float
    block_ids: list[str] = field(default_factory=list)


@dataclass
class BuildStats:
    elapsed_ms: float
    index_bytes: int
    notes: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Ranker(Protocol):
    name: str

    @classmethod
    def build(cls, corpus_dir: Path, index_dir: Path, **opts: Any) -> BuildStats:
        """index_dir 를 통째로 재생성한다(멱등)."""

    @classmethod
    def load(cls, corpus_dir: Path, index_dir: Path, **opts: Any) -> "Ranker":
        """이미 build 된 색인을 연다."""

    def search(self, query: str, k: int = 10) -> list[Hit]:
        """score 내림차순. 색인을 다시 만들면 안 된다."""


def dir_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def load_pages(corpus_dir: Path) -> list[dict[str, Any]]:
    """코퍼스 전체를 읽는다. 랭커 build 에서만 쓰고 search 에서 쓰지 말 것."""
    import json
    pages: list[dict[str, Any]] = []
    for p in sorted(Path(corpus_dir).rglob("*.json")):
        if p.name.startswith("."):
            continue
        try:
            value = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for page in value if isinstance(value, list) else [value]:
            if isinstance(page, dict) and page.get("id") and page.get("blocks"):
                pages.append(page)
    return pages


def block_text(block: dict[str, Any]) -> str:
    data = block.get("data") or {}
    if isinstance(data, dict):
        for key in ("text", "caption"):
            if data.get(key):
                return str(data[key])
        if data.get("items"):
            return " ".join(str(i) for i in data["items"])
        if data.get("rows"):
            return " ".join(str(c) for row in data["rows"] for c in row)
    return str(block.get("source_text") or "")

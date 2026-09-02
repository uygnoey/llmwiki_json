"""격리된 qmd collection을 사용하는 벤치마크 arm."""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from .base import BuildStats, Hit, dir_bytes, load_pages

from scripts.llmwiki import render_markdown, safe_name


COLLECTION = "llmwiki_bench"
MARKDOWN_DIRNAME = "markdown"
PAGE_MAP_NAME = "page-map.json"
MODES = {"vsearch", "query", "search"}
MODEL_LINE = re.compile(
    r"^\s*(embed|generate|rerank):\s*[\"']?([^\"']+?)[\"']?\s*$"
)
VECTOR_DIMENSION = re.compile(r"embedding\s+float\[(\d+)]")


def _qmd_binary() -> str:
    binary = shutil.which("qmd")
    if not binary:
        raise RuntimeError(
            "qmd 실행 파일이 없습니다; Bun 1.3.14로 "
            "`bun install -g @tobilu/qmd`를 실행해야 합니다"
        )
    return binary


def _run_qmd(index_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """project-local qmd index만 사용해 사용자의 전역 collection을 격리한다."""
    env = os.environ.copy()
    # qmd의 getPwd()는 상속된 PWD를 우선한다. subprocess cwd와 반드시 맞춘다.
    env["PWD"] = str(index_dir)
    proc = subprocess.run(
        [_qmd_binary(), *args],
        cwd=index_dir,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        command = "qmd " + " ".join(args)
        raise RuntimeError(f"{command} 실패(exit {proc.returncode}): {detail}")
    return proc


def _mode(opts: dict[str, Any]) -> str:
    mode = str(opts.get("mode", "vsearch"))
    if mode not in MODES:
        raise ValueError(f"지원하지 않는 qmd mode: {mode} (허용: {sorted(MODES)})")
    return mode


def _model_uris(index_dir: Path) -> dict[str, str]:
    config = index_dir / ".qmd" / "index.yml"
    models: dict[str, str] = {}
    for line in config.read_text(encoding="utf-8").splitlines():
        match = MODEL_LINE.match(line)
        if match:
            models[match.group(1)] = match.group(2)
    return models


def _model_cache_path(model_uri: str) -> Path | None:
    filename = model_uri.rsplit("/", 1)[-1]
    cache_home = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    model_dir = cache_home / "qmd" / "models"
    matches = sorted(model_dir.glob(f"*{filename}"))
    return matches[0] if matches else None


def _run_embed(index_dir: Path, timeout_minutes: str, model_uri: str) -> dict[str, Any]:
    """embed와 첫 모델 다운로드 시간을 별도로 관측한다."""
    args = ("embed", "-c", COLLECTION, "--timeout", timeout_minutes)
    env = os.environ.copy()
    env["PWD"] = str(index_dir)
    cached_before = _model_cache_path(model_uri) is not None
    started = time.perf_counter()
    downloaded_at: float | None = None
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stdout_file, \
            tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stderr_file:
        proc = subprocess.Popen(
            [_qmd_binary(), *args],
            cwd=index_dir,
            env=env,
            stdout=stdout_file,
            stderr=stderr_file,
            text=True,
        )
        while proc.poll() is None:
            if (not cached_before and downloaded_at is None
                    and _model_cache_path(model_uri) is not None):
                downloaded_at = time.perf_counter()
            time.sleep(0.1)
        stdout_file.seek(0)
        stderr_file.seek(0)
        stdout = stdout_file.read()
        stderr = stderr_file.read()
    ended = time.perf_counter()
    if proc.returncode != 0:
        detail = (stderr or stdout).strip()
        raise RuntimeError(
            f"qmd {' '.join(args)} 실패(exit {proc.returncode}): {detail}"
        )
    total_ms = (ended - started) * 1000.0
    download_ms = 0.0 if cached_before else (
        ((downloaded_at or ended) - started) * 1000.0
    )
    return {
        "cached_before": cached_before,
        "download_ms": download_ms,
        "embed_total_ms": total_ms,
        "embed_work_ms": max(0.0, total_ms - download_ms),
        "stdout": stdout,
        "stderr": stderr,
    }


def _run_pull(index_dir: Path, models: dict[str, str]) -> dict[str, Any]:
    """query 모드의 generation/rerank 모델을 받고 완료 시점을 기록한다."""
    args = ("pull",)
    env = os.environ.copy()
    env["PWD"] = str(index_dir)
    cached_before = {
        role: _model_cache_path(uri) is not None for role, uri in models.items()
    }
    completed_ms: dict[str, float] = {}
    started = time.perf_counter()
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stdout_file, \
            tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stderr_file:
        proc = subprocess.Popen(
            [_qmd_binary(), *args],
            cwd=index_dir,
            env=env,
            stdout=stdout_file,
            stderr=stderr_file,
            text=True,
        )
        while proc.poll() is None:
            for role, uri in models.items():
                if (not cached_before[role] and role not in completed_ms
                        and _model_cache_path(uri) is not None):
                    completed_ms[role] = (time.perf_counter() - started) * 1000.0
            time.sleep(0.1)
        stdout_file.seek(0)
        stderr_file.seek(0)
        stdout = stdout_file.read()
        stderr = stderr_file.read()
    total_ms = (time.perf_counter() - started) * 1000.0
    if proc.returncode != 0:
        detail = (stderr or stdout).strip()
        raise RuntimeError(f"qmd pull 실패(exit {proc.returncode}): {detail}")
    return {
        "cached_before": cached_before,
        "download_completed_ms": {
            role: round(completed_ms.get(role, total_ms), 1)
            for role in models if not cached_before[role]
        },
        "pull_ms": round(total_ms, 1),
    }


def _model_stats(models: dict[str, str]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for role, uri in models.items():
        path = _model_cache_path(uri)
        out[role] = {
            "uri": uri,
            "bytes": path.stat().st_size if path and path.is_file() else 0,
        }
    return out


def _vector_dimensions(index_dir: Path) -> int | None:
    db_path = index_dir / ".qmd" / "index.sqlite"
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as db:
        row = db.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'vectors_vec'"
        ).fetchone()
    match = VECTOR_DIMENSION.search(str(row[0])) if row and row[0] else None
    return int(match.group(1)) if match else None


def _device_note(index_dir: Path) -> str:
    output = _run_qmd(index_dir, "doctor").stdout
    for line in output.splitlines():
        if "device probe:" in line:
            return line.strip().lstrip("✓⚠✗ ")
    return "unknown"


def _render_corpus(corpus_dir: Path, markdown_dir: Path) -> dict[str, str]:
    pages = sorted(load_pages(corpus_dir), key=lambda page: str(page["id"]))
    mapping: dict[str, str] = {}
    for page in pages:
        filename = Path(safe_name(str(page["id"]))).with_suffix(".md").name
        if filename in mapping:
            raise RuntimeError(f"markdown 파일명 충돌: {filename}")
        (markdown_dir / filename).write_text(render_markdown(page), encoding="utf-8")
        mapping[filename] = str(page["id"])
    return mapping


def _result_key(raw_path: Any) -> str:
    raw = str(raw_path or "").split("?", 1)[0]
    if raw.startswith("qmd://"):
        parsed = urlparse(raw)
        raw = parsed.path.lstrip("/")
    return Path(unquote(raw)).name


class VectorRanker:
    name = "vector"

    def __init__(self, index_dir: Path, page_map: dict[str, str], mode: str) -> None:
        self.index_dir = Path(index_dir).resolve()
        self.page_map = page_map
        self.mode = mode

    @classmethod
    def build(cls, corpus_dir: Path, index_dir: Path, **opts: Any) -> BuildStats:
        _qmd_binary()
        mode = _mode(opts)
        started = time.perf_counter()
        target = Path(index_dir).resolve()
        if target.exists():
            shutil.rmtree(target)
        markdown_dir = target / MARKDOWN_DIRNAME
        markdown_dir.mkdir(parents=True, exist_ok=True)

        page_map = _render_corpus(Path(corpus_dir), markdown_dir)
        (target / PAGE_MAP_NAME).write_text(
            json.dumps(page_map, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )

        # `qmd init`은 .qmd/index.sqlite를 index_dir 안에 만든다. 따라서 기존
        # ~/.config/qmd collection과 ~/.cache/qmd/index.sqlite는 건드리지 않는다.
        _run_qmd(target, "init")
        _run_qmd(
            target,
            "collection", "add", str(markdown_dir),
            "--name", COLLECTION,
        )
        models = _model_uris(target)
        model_uri = models.get("embed", "unknown")
        pull = _run_pull(target, models) if mode == "query" else None
        embed_timeout = str(opts.get("embed_timeout_minutes", 0))
        embed = _run_embed(target, embed_timeout, model_uri)

        elapsed_ms = (time.perf_counter() - started) * 1000.0
        version = _run_qmd(target, "--version").stdout.strip()
        device = _device_note(target)
        model_path = _model_cache_path(model_uri)
        model_bytes = model_path.stat().st_size if model_path and model_path.is_file() else 0
        embed_work_ms = float(embed["embed_work_ms"])
        estimate_10000_ms = (
            embed_work_ms * 10000.0 / len(page_map) if page_map else 0.0
        )
        return BuildStats(
            elapsed_ms=elapsed_ms,
            index_bytes=dir_bytes(target),
            notes={
                "collection": COLLECTION,
                "mode": mode,
                "markdown_pages": len(page_map),
                "qmd_version": version,
                "models": _model_stats(models),
                "embed_model": model_uri,
                "embed_model_bytes": model_bytes,
                "embed_dimensions": _vector_dimensions(target),
                "model_cached_before": embed["cached_before"],
                "model_download_ms": round(float(embed["download_ms"]), 1),
                "embed_command_ms": round(float(embed["embed_total_ms"]), 1),
                "embed_work_ms": round(embed_work_ms, 1),
                "device": device,
                "estimated_10000_embed_ms": round(estimate_10000_ms, 1),
                "pull": pull,
                "search_command": f"qmd {mode} --format json",
                "isolated_project_index": True,
            },
        )

    @classmethod
    def load(cls, corpus_dir: Path, index_dir: Path, **opts: Any) -> "VectorRanker":
        _qmd_binary()
        mode = _mode(opts)
        target = Path(index_dir).resolve()
        map_path = target / PAGE_MAP_NAME
        if not (target / ".qmd" / "index.sqlite").is_file() or not map_path.is_file():
            raise RuntimeError(f"qmd 벤치 색인이 없습니다: {target}")
        value = json.loads(map_path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise RuntimeError(f"잘못된 page map: {map_path}")
        return cls(
            target,
            {str(key): str(page_id) for key, page_id in value.items()},
            mode,
        )

    def search(self, query: str, k: int = 10) -> list[Hit]:
        if k <= 0:
            return []
        proc = _run_qmd(
            self.index_dir,
            self.mode, query,
            "-c", COLLECTION,
            "--format", "json",
            "-n", str(k),
        )
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"qmd search JSON 해석 실패: {exc}") from exc
        rows = payload.get("results") if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            raise RuntimeError("qmd search JSON에 results 배열이 없습니다")

        hits: list[Hit] = []
        seen: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            filename = _result_key(row.get("path") or row.get("file") or row.get("uri"))
            page_id = self.page_map.get(filename)
            if not page_id or page_id in seen:
                continue
            try:
                score = float(row.get("score", 0.0))
            except (TypeError, ValueError):
                score = 0.0
            hits.append(Hit(page_id=page_id, score=score, block_ids=[]))
            seen.add(page_id)
            if len(hits) >= k:
                break
        return hits


RANKER = VectorRanker

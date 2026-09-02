#!/usr/bin/env python3
"""자연 문서 qmd 벡터 색인을 project-local 디렉터리에 재현한다.

정본 JSON의 ``source_text``를 그대로 이어 붙인 page 문서와, block 하나를
문서 하나로 만든 chunk 문서를 함께 색인한다. 모든 qmd subprocess는 cwd와
PWD를 ``bench/index_vec_nat``로 고정해 사용자 전역 collection을 보지 않는다.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
CORPUS = ROOT / "bench/natural/root/wiki"
INDEX = ROOT / "bench/index_vec_nat"
PAGE_DIR = INDEX / "markdown_pages"
BLOCK_DIR = INDEX / "markdown_blocks"
PAGE_COLLECTION = "vec_nat_pages"
BLOCK_COLLECTION = "vec_nat_blocks"
PAGE_MAP = INDEX / "page-map.json"
BLOCK_MAP = INDEX / "block-map.json"
BUILD_RESULT = INDEX / "build.json"
MODEL_LINE = re.compile(r'^\s*embed:\s*["\']?([^"\']+?)["\']?\s*$')


def qmd_binary() -> str:
    path = shutil.which("qmd")
    if not path:
        raise RuntimeError("qmd 실행 파일이 없다")
    return path


def env_for_index() -> dict[str, str]:
    env = os.environ.copy()
    env["PWD"] = str(INDEX)
    return env


def run_qmd(*args: str) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        [qmd_binary(), *args],
        cwd=INDEX,
        env=env_for_index(),
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode:
        detail = (proc.stderr or proc.stdout).strip()
        raise RuntimeError(f"qmd {' '.join(args)} 실패(exit {proc.returncode}): {detail}")
    return proc


def dir_bytes(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def safe_file(value: str) -> str:
    lead = re.sub(r"[^0-9A-Za-z가-힣._-]+", "-", value).strip("-.")[:80]
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    return f"{lead or 'item'}-{digest}.md"


def load_pages() -> list[dict[str, Any]]:
    pages: list[dict[str, Any]] = []
    for path in sorted(CORPUS.rglob("*.json")):
        value = json.loads(path.read_text(encoding="utf-8"))
        values = value if isinstance(value, list) else [value]
        pages.extend(v for v in values if isinstance(v, dict) and v.get("id"))
    return sorted(pages, key=lambda page: str(page["id"]))


def page_frontmatter(page: dict[str, Any]) -> str:
    return "\n".join([
        "---",
        f"title: {json.dumps(str(page.get('title', '')), ensure_ascii=False)}",
        f"type: {page.get('type', '')}",
        f"created: {page.get('created', '')}",
        f"updated: {page.get('updated', '')}",
        f"tags: [{', '.join(str(x) for x in page.get('tags') or [])}]",
        f"projects: [{', '.join(str(x) for x in page.get('projects') or [])}]",
        "---",
    ])


def render_page(page: dict[str, Any]) -> str:
    """정본 block 순서/경계와 영속 block id를 보존한 page 문서."""
    blocks = page.get("blocks") or {}
    rendered = [page_frontmatter(page), f"<!-- page-id: {page['id']} -->"]
    for block_id in page.get("block_order") or list(blocks):
        block = blocks.get(block_id)
        if not isinstance(block, dict):
            continue
        canonical_id = str(block.get("id") or block_id)
        rendered.append(
            f"<!-- block-id: {canonical_id} -->\n{str(block.get('source_text') or '').rstrip()}"
        )
    return "\n\n".join(rendered).rstrip() + "\n"


def render_block(page: dict[str, Any], block: dict[str, Any], block_id: str) -> str:
    """block 하나를 독립 vector document로 만든다."""
    title = str(page.get("title") or page["id"])
    kind = str(block.get("kind") or "")
    return "\n\n".join([
        "\n".join([
            "---",
            f"title: {json.dumps(f'{title} [{kind}]', ensure_ascii=False)}",
            f"type: {page.get('type', '')}",
            f"projects: [{', '.join(str(x) for x in page.get('projects') or [])}]",
            "---",
        ]),
        f"<!-- page-id: {page['id']} -->",
        f"<!-- block-id: {block_id} -->",
        str(block.get("source_text") or "").rstrip(),
    ]).rstrip() + "\n"


def render_all() -> tuple[dict[str, str], dict[str, dict[str, str]]]:
    PAGE_DIR.mkdir(parents=True, exist_ok=True)
    BLOCK_DIR.mkdir(parents=True, exist_ok=True)
    page_map: dict[str, str] = {}
    block_map: dict[str, dict[str, str]] = {}
    for page in load_pages():
        page_name = safe_file(str(page["id"]))
        if page_name in page_map:
            raise RuntimeError(f"page markdown 파일명 충돌: {page_name}")
        (PAGE_DIR / page_name).write_text(render_page(page), encoding="utf-8")
        page_map[page_name] = str(page["id"])
        blocks = page.get("blocks") or {}
        for block_id in page.get("block_order") or list(blocks):
            block = blocks.get(block_id)
            if not isinstance(block, dict):
                continue
            canonical_id = str(block.get("id") or block_id)
            block_name = safe_file(canonical_id)
            if block_name in block_map:
                raise RuntimeError(f"block markdown 파일명 충돌: {block_name}")
            (BLOCK_DIR / block_name).write_text(
                render_block(page, block, canonical_id), encoding="utf-8"
            )
            block_map[block_name] = {
                "page_id": str(page["id"]),
                "block_id": canonical_id,
                "kind": str(block.get("kind") or ""),
            }
    PAGE_MAP.write_text(
        json.dumps(page_map, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    BLOCK_MAP.write_text(
        json.dumps(block_map, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return page_map, block_map


def embed_model_uri() -> str:
    config = INDEX / ".qmd/index.yml"
    for line in config.read_text(encoding="utf-8").splitlines():
        match = MODEL_LINE.match(line)
        if match:
            return match.group(1)
    raise RuntimeError(f"embed model 설정을 못 찾음: {config}")


def cached_model(uri: str) -> Path | None:
    filename = uri.rsplit("/", 1)[-1]
    cache_root = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    matches = sorted((cache_root / "qmd/models").glob(f"*{filename}"))
    return next((path for path in matches if path.is_file() and path.stat().st_size > 1024), None)


def timed_qmd(*args: str) -> tuple[float, str, str]:
    started = time.perf_counter()
    proc = run_qmd(*args)
    return (round((time.perf_counter() - started) * 1000.0, 1), proc.stdout, proc.stderr)


def device_probe() -> str:
    doctor = run_qmd("doctor").stdout
    for line in doctor.splitlines():
        if "device probe:" in line:
            return line.strip().lstrip("✓⚠✗ ")
    return "unknown"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--reuse", action="store_true",
        help="이미 완성된 build.json이 있으면 색인을 다시 만들지 않는다",
    )
    args = parser.parse_args()
    if args.reuse and BUILD_RESULT.is_file():
        print(BUILD_RESULT.read_text(encoding="utf-8"))
        return 0
    if INDEX.exists() and any(INDEX.iterdir()):
        raise SystemExit(
            f"색인 디렉터리가 이미 비어 있지 않다: {INDEX}; 재사용은 --reuse로 명시한다"
        )
    INDEX.mkdir(parents=True, exist_ok=True)

    total_started = time.perf_counter()
    page_map, block_map = render_all()
    init_ms, _out, _err = timed_qmd("init")
    model_uri = embed_model_uri()
    model_path = cached_model(model_uri)
    if model_path is None:
        raise RuntimeError(
            f"네트워크 없이 쓸 qmd embed 모델이 cache에 없다: {model_uri}"
        )

    add_page_ms, _out, _err = timed_qmd(
        "collection", "add", str(PAGE_DIR), "--name", PAGE_COLLECTION
    )
    page_embed_ms, page_out, page_err = timed_qmd(
        "embed", "-c", PAGE_COLLECTION, "--timeout", "0"
    )
    after_page_bytes = dir_bytes(INDEX)

    add_block_ms, _out, _err = timed_qmd(
        "collection", "add", str(BLOCK_DIR), "--name", BLOCK_COLLECTION
    )
    block_embed_ms, block_out, block_err = timed_qmd(
        "embed", "-c", BLOCK_COLLECTION, "--timeout", "0"
    )
    version = run_qmd("--version").stdout.strip()
    device = device_probe()
    result = {
        "schema_version": "1.0",
        "corpus": str(CORPUS.relative_to(ROOT)),
        "index": str(INDEX.relative_to(ROOT)),
        "isolation": {"cwd": str(INDEX), "PWD": str(INDEX), "project_local": True},
        "qmd_version": version,
        "device": device,
        "model": {"uri": model_uri, "path": str(model_path), "bytes": model_path.stat().st_size},
        "pages": len(page_map),
        "blocks": len(block_map),
        "collections": {"pages": PAGE_COLLECTION, "blocks": BLOCK_COLLECTION},
        "timing_ms": {
            "init": init_ms,
            "collection_add_pages": add_page_ms,
            "embed_pages": page_embed_ms,
            "collection_add_blocks": add_block_ms,
            "embed_blocks": block_embed_ms,
            "total": round((time.perf_counter() - total_started) * 1000.0, 1),
        },
        "bytes_after_pages": after_page_bytes,
        "index_bytes": dir_bytes(INDEX),
        "embed_output": {
            "pages_stdout": page_out.strip(),
            "pages_stderr": page_err.strip(),
            "blocks_stdout": block_out.strip(),
            "blocks_stderr": block_err.strip(),
        },
    }
    BUILD_RESULT.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

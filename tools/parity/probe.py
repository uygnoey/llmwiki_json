#!/usr/bin/env python3
"""Parity corpus — Python(정본) 쪽 답안지.

`tools/parity/cases.json` 을 읽어 case 마다 한 줄짜리 문자열 답을 낸다.
`probe.ts` 가 같은 입력으로 같은 모양의 답을 내고, `parity.py corpus` 가 둘을
대조한다. 어느 쪽도 상대의 출력을 입력으로 쓰지 않는다 — 같은 원본에서 독립
실행해야 대조가 의미를 가진다.

출력: {"runtime": "...", "results": {case_id: answer}}
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any

CASES = Path(__file__).resolve().parent / "cases.json"
# Python 정규식의 inline flag. JS 에는 없어서 분리해야 하는 지점을 그대로 드러낸다.
INLINE_FLAG = re.compile(r"^\(\?([aiLmsux]+)\)")


def dumps(value: Any, *, pretty: bool) -> str:
    """scripts/llmwiki.py 의 dump() 와 같은 규칙."""
    return json.dumps(value, ensure_ascii=False, indent=2 if pretty else None,
                      separators=None if pretty else (",", ":"))


def run_regex(spec: dict[str, Any]) -> str:
    pattern, text = spec["pattern"], spec["text"]
    flags = 0
    match = INLINE_FLAG.match(pattern)
    if match:
        for letter in match.group(1):
            flags |= {"i": re.I, "m": re.M, "s": re.S, "x": re.X, "a": re.A}.get(letter, 0)
        pattern = pattern[match.end():]
    found = [[group for group in ([m.group(0)] + list(m.groups()))]
             for m in re.finditer(pattern, text, flags | re.M)]
    return dumps(found, pretty=False)


def answer(case: dict[str, Any]) -> str:
    kind, value = case["kind"], case["input"]
    if kind == "json":
        return dumps(value, pretty=False)
    if kind == "json_pretty":
        return dumps(value, pretty=True)
    if kind == "sha256":
        return hashlib.sha256(str(value).encode("utf-8")).hexdigest()
    if kind == "nfc":
        normalized = unicodedata.normalize("NFC", str(value))
        return " ".join(f"U+{ord(ch):04X}" for ch in normalized)
    if kind == "casefold":
        return dumps([str(item).casefold() for item in value], pretty=False)
    if kind == "sort":
        return dumps(sorted(str(item) for item in value), pretty=False)
    if kind == "codepoints":
        text = str(value)
        return dumps({"length": len(text), "head2": text[:2]}, pretty=False)
    if kind == "regex":
        return run_regex(value)
    raise SystemExit(f"unknown case kind: {kind}")


def main() -> None:
    cases = json.loads(CASES.read_text(encoding="utf-8"))["cases"]
    results = {case["id"]: answer(case) for case in cases}
    sys.stdout.write(json.dumps(
        {"runtime": f"python {sys.version_info.major}.{sys.version_info.minor}."
                    f"{sys.version_info.micro}",
         "unicode": unicodedata.unidata_version, "results": results},
        ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()

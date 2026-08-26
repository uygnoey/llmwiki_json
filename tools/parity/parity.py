#!/usr/bin/env python3
"""Parity 하네스 — 정본 구현과 후보 구현이 정말 같은 것을 내는지 대조한다.

두 가지 일을 한다.

`corpus`
    두 런타임의 원시 의미론을 같은 입력으로 대조한다. Python 을 oracle 로,
    Bun 을 candidate 로 각각 **독립 실행**한다 — 한쪽 출력을 다른 쪽 입력으로
    쓰지 않는다. 그래야 "같은 답"이 우연이 아님을 알 수 있다.

`sweep`
    corpus 는 손으로 고른 입력이라 case 가 밟지 않은 구간을 보지 못한다. 값
    공간을 통째로 훑어 그 사각을 없앤다.

`build`
    `llmwiki.py build` 를 shadow 디렉터리에 두 번 돌려 산출물이 바이트 단위로
    같은지 본다. 공식 `index/` 와 `viewer/public/data/` 는 건드리지 않는다.
    `--candidate '<명령>'` 을 주면 그 명령을 세 번째 build 로 보고 같은 방식으로
    대조한다 — 나중에 TS build 가 생기면 그대로 꽂으면 된다.

어느 쪽도 저장소의 정본이나 공식 산출물을 쓰지 않는다.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
CASES = HERE / "cases.json"
PROBE_PY = HERE / "probe.py"
PROBE_TS = HERE / "probe.ts"
SWEEP_PY = HERE / "sweep.py"
SWEEP_TS = HERE / "sweep.ts"
ARTIFACT_DIRS = ("index", "viewer/public/data")
# corpus 는 답이 다를 수 있다는 것 자체가 결과다. 프로세스 종료 코드는
# "기록해 둔 예상과 어긋났는가" 만 본다.
OK, DIVERGED, MISSING = "match", "diverge", "missing"


def bun() -> str | None:
    return os.environ.get("LLMWIKI_BUN") or shutil.which("bun")


def python() -> str:
    return os.environ.get("LLMWIKI_PYTHON") or sys.executable


def load_cases() -> list[dict[str, Any]]:
    return json.loads(CASES.read_text(encoding="utf-8"))["cases"]


def run_probe(command: list[str], label: str) -> dict[str, Any]:
    proc = subprocess.run(command, capture_output=True, text=True, cwd=ROOT)
    if proc.returncode != 0:
        raise SystemExit(f"{label} probe 실패 (exit {proc.returncode})\n{proc.stderr}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{label} probe 가 JSON 을 내지 않았다: {exc}\n{proc.stdout[:400]}")


def corpus(args: argparse.Namespace) -> int:
    cases = load_cases()
    oracle = run_probe([python(), str(PROBE_PY)], "python")
    runner = bun()
    candidate: dict[str, Any] | None = None
    if runner and PROBE_TS.exists():
        candidate = run_probe([runner, str(PROBE_TS)], "bun")

    rows: list[dict[str, Any]] = []
    for case in cases:
        cid = case["id"]
        expected = oracle["results"].get(cid)
        row = {"id": cid, "expect": case["expect"], "mitigation": case["mitigation"],
               "oracle": expected}
        if candidate is None:
            row["naive"] = MISSING
            row["mitigated"] = MISSING
        else:
            got = candidate.get("results", {}).get(cid)
            fixed = candidate.get("mitigated", {}).get(cid, got)
            row["naive"] = OK if got == expected else DIVERGED
            row["mitigated"] = OK if fixed == expected else DIVERGED
            row["candidate"] = got
            row["candidate_mitigated"] = fixed
        rows.append(row)

    report = {
        "oracle_runtime": oracle.get("runtime"), "oracle_unicode": oracle.get("unicode"),
        "candidate_runtime": (candidate or {}).get("runtime"),
        "cases": len(rows),
        "naive_diverged": sorted(r["id"] for r in rows if r["naive"] == DIVERGED),
        "mitigated_diverged": sorted(r["id"] for r in rows if r["mitigated"] == DIVERGED),
        "unexpected": sorted(r["id"] for r in rows
                             if r["naive"] != MISSING
                             and (r["naive"] == OK) != (r["expect"] == OK)),
        "rows": rows,
    }
    if args.as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1 if report["unexpected"] or report["mitigated_diverged"] else 0

    print(f"oracle    : {report['oracle_runtime']} (Unicode {report['oracle_unicode']})")
    print(f"candidate : {report['candidate_runtime'] or '없음 — bun 이나 probe.ts 가 없다'}")
    print()
    width = max(len(r["id"]) for r in rows)
    for row in rows:
        mark = {OK: "=", DIVERGED: "≠", MISSING: "?"}
        print(f"  {row['id']:<{width}}  기대 {row['expect']:<8}"
              f" 순진 {mark[row['naive']]}  완화 {mark[row['mitigated']]}")
    print()
    print(f"순진한 구현이 갈라진 case : {len(report['naive_diverged'])}/{len(rows)}")
    print(f"완화 후에도 남은 case     : {len(report['mitigated_diverged'])}"
          f"{' — ' + ', '.join(report['mitigated_diverged']) if report['mitigated_diverged'] else ''}")
    if report["unexpected"]:
        print(f"기록과 어긋난 case        : {', '.join(report['unexpected'])}")
        print("  cases.json 의 expect 를 고치거나, 런타임이 바뀐 것인지 확인해라.")
    return 1 if report["unexpected"] or report["mitigated_diverged"] else 0


def sweep(args: argparse.Namespace) -> int:
    """corpus 가 놓친 것을 값 공간 전체로 훑는다 — 실제로 버그를 잡은 쪽이다."""
    runner = bun()
    if not runner or not SWEEP_TS.exists():
        print("bun 이나 sweep.ts 가 없다 — 훑기를 건너뛴다")
        return 0
    oracle = subprocess.run([python(), str(SWEEP_PY)], capture_output=True, text=True, cwd=ROOT)
    if oracle.returncode != 0:
        raise SystemExit(f"sweep.py 실패\n{oracle.stderr}")
    candidate = subprocess.run([runner, str(SWEEP_TS)], input=oracle.stdout,
                               capture_output=True, text=True, cwd=ROOT)
    if candidate.returncode != 0:
        raise SystemExit(f"sweep.ts 실패\n{candidate.stderr}")
    report = json.loads(candidate.stdout)

    if args.as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"candidate : {report['runtime']}")
        for key, label in (("floats", "float 표기"), ("casefold", "casefold")):
            section = report[key]
            print(f"  {label:<10} 불일치 {section['mismatched']}/{section['checked']}")
            for example in section["examples"]:
                print(f"    ≠ {example}")
    return 1 if any(report[key]["mismatched"] for key in ("floats", "casefold")) else 0


def fingerprint(root: Path) -> dict[str, str]:
    """산출물 트리 -> {저장소 상대경로: sha256}. 경로도 내용도 계약이다."""
    out: dict[str, str] = {}
    for rel in ARTIFACT_DIRS:
        base = root / rel
        if not base.exists():
            continue
        for path in sorted(base.rglob("*")):
            if path.is_file():
                out[path.relative_to(root).as_posix()] = hashlib.sha256(
                    path.read_bytes()).hexdigest()
    return out


def shadow_build(command: list[str], label: str) -> dict[str, str]:
    """정본만 복사한 임시 저장소에서 build 를 돌리고 산출물 지문을 돌려준다."""
    with tempfile.TemporaryDirectory(prefix="llmwiki-parity-") as tmp:
        shadow = Path(tmp) / "repo"
        shadow.mkdir()
        for rel in ("wiki", "tools/config", "tools/schema", "scripts"):
            source = ROOT / rel
            if source.exists():
                shutil.copytree(source, shadow / rel,
                                ignore=shutil.ignore_patterns("__pycache__"))
        env = {**os.environ, "LLMWIKI_ROOT": str(shadow)}
        proc = subprocess.run(command, capture_output=True, text=True, cwd=shadow, env=env)
        if proc.returncode != 0:
            raise SystemExit(f"{label} build 실패 (exit {proc.returncode})\n{proc.stderr}")
        return fingerprint(shadow)


def build(args: argparse.Namespace) -> int:
    oracle_cmd = [python(), str(ROOT / "scripts" / "llmwiki.py"), "build"]
    first = shadow_build(oracle_cmd, "python#1")
    second = shadow_build(oracle_cmd, "python#2")
    report: dict[str, Any] = {
        "files": len(first),
        "self_consistent": first == second,
        "differing": sorted(k for k in set(first) | set(second) if first.get(k) != second.get(k)),
    }
    if args.candidate:
        candidate = shadow_build(args.candidate.split(), "candidate")
        report["candidate_files"] = len(candidate)
        report["candidate_matches"] = candidate == first
        report["candidate_only"] = sorted(set(candidate) - set(first))
        report["oracle_only"] = sorted(set(first) - set(candidate))
        report["content_differs"] = sorted(
            k for k in set(first) & set(candidate) if first[k] != candidate[k])

    if args.as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"산출물 파일 수      : {report['files']}")
        print(f"두 번 돌려 동일     : {'예' if report['self_consistent'] else '아니오'}")
        if report["differing"]:
            print(f"  달라진 파일: {', '.join(report['differing'])}")
        if args.candidate:
            print(f"후보 구현과 동일    : {'예' if report['candidate_matches'] else '아니오'}")
            for key, label in (("candidate_only", "후보에만 있는 파일"),
                               ("oracle_only", "정본에만 있는 파일"),
                               ("content_differs", "내용이 다른 파일")):
                if report.get(key):
                    print(f"  {label}: {', '.join(report[key])}")
    if not report["self_consistent"]:
        return 1
    return 0 if not args.candidate or report["candidate_matches"] else 1


def main() -> None:
    parser = argparse.ArgumentParser(prog="parity", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name, handler, help_text in (
            ("corpus", corpus, "원시 의미론(직렬화·Unicode·정렬·정규식)을 두 런타임에서 대조"),
            ("sweep", sweep, "값 공간 전체 훑기 — corpus 가 놓치는 구간을 잡는다"),
            ("build", build, "build 산출물의 바이트 결정성 — shadow 디렉터리에서만 돈다")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--json", action="store_true", dest="as_json")
        p.set_defaults(handler=handler)
    sub.choices["build"].add_argument(
        "--candidate", help="후보 build 명령 (예: 'bun tools/parity/build.ts')")
    args = parser.parse_args()
    raise SystemExit(args.handler(args))


if __name__ == "__main__":
    main()

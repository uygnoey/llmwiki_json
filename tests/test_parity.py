"""Parity 하네스 — 정본 구현이 결정적인지, 후보 구현이 그것과 같은지.

오늘의 값어치는 두 가지다. build 가 바이트 단위로 결정적이라는 계약을 실제
산출물로 지키고, TS 포팅이 반드시 막아야 하는 지점 목록(cases.json)을 런타임이
바뀌면 소리가 나도록 고정해 둔다.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

from tests.support import REPO

PARITY = REPO / "tools" / "parity"
CASES = PARITY / "cases.json"
DRIVER = PARITY / "parity.py"
PROBE_TS = PARITY / "probe.ts"
SWEEP_TS = PARITY / "sweep.ts"
KINDS = {"json", "json_pretty", "sha256", "nfc", "casefold", "sort", "codepoints", "regex"}


def run(*argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(DRIVER), *argv],
                          capture_output=True, text=True, cwd=REPO)


class CorpusShapeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cases = json.loads(CASES.read_text(encoding="utf-8"))["cases"]

    def test_every_case_is_well_formed(self) -> None:
        seen: set[str] = set()
        for case in self.cases:
            self.assertNotIn(case["id"], seen, "case id 는 유일해야 한다")
            seen.add(case["id"])
            self.assertIn(case["kind"], KINDS, case["id"])
            self.assertIn(case["expect"], {"match", "diverge"}, case["id"])
            self.assertTrue(case["mitigation"].strip(), f"{case['id']}: 완화책을 적어라")

    def test_the_python_oracle_answers_every_case(self) -> None:
        proc = subprocess.run([sys.executable, str(PARITY / "probe.py")],
                              capture_output=True, text=True, cwd=REPO)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        results = json.loads(proc.stdout)["results"]
        self.assertEqual(sorted(results), sorted(case["id"] for case in self.cases))
        for cid, answer in results.items():
            self.assertIsInstance(answer, str, cid)


class BuildDeterminismTest(unittest.TestCase):
    """정본 build 는 같은 입력에서 같은 바이트를 내야 한다 — shadow 에서만 돈다."""

    def test_build_is_byte_identical_across_runs(self) -> None:
        proc = run("build", "--json")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        report = json.loads(proc.stdout)
        self.assertTrue(report["self_consistent"], report["differing"])
        self.assertGreater(report["files"], 0)

    def test_the_harness_leaves_official_artifacts_alone(self) -> None:
        index = REPO / "index"
        before = {p: p.read_bytes() for p in sorted(index.rglob("*.json"))}
        run("build", "--json")
        self.assertEqual({p: p.read_bytes() for p in sorted(index.rglob("*.json"))}, before)


@unittest.skipUnless(shutil.which("bun") and PROBE_TS.exists(),
                     "bun 이나 probe.ts 가 없다 — 후보 런타임 대조는 건너뛴다")
class CandidateParityTest(unittest.TestCase):
    def setUp(self) -> None:
        proc = run("corpus", "--json")
        self.report = json.loads(proc.stdout)

    def test_recorded_divergences_still_hold(self) -> None:
        """cases.json 의 expect 와 실제가 어긋나면 둘 중 하나가 낡은 것이다."""
        self.assertEqual(self.report["unexpected"], [],
                         "런타임이 바뀌었거나 cases.json 이 낡았다")

    def test_every_divergence_has_a_working_mitigation(self) -> None:
        self.assertEqual(self.report["mitigated_diverged"], [],
                         "완화책을 적용해도 Python 답과 일치하지 않는 case 가 남았다")


@unittest.skipUnless(shutil.which("bun") and SWEEP_TS.exists(),
                     "bun 이나 sweep.ts 가 없다 — 대량 훑기는 건너뛴다")
class SweepTest(unittest.TestCase):
    """corpus 만으로는 부족하다는 것을 이 시험이 대신 기억한다.

    첫 후보 구현은 손으로 고른 24개 case 를 전부 통과하고도 |v|>=1e16 에서
    float 표기가 틀렸다. 값 공간을 훑어야 그런 것이 잡힌다.
    """

    def test_no_mismatch_across_the_whole_value_space(self) -> None:
        proc = run("sweep", "--json")
        report = json.loads(proc.stdout)
        self.assertEqual(report["floats"]["mismatched"], 0, report["floats"]["examples"])
        self.assertEqual(report["casefold"]["mismatched"], 0, report["casefold"]["examples"])
        self.assertGreater(report["floats"]["checked"], 5000)
        self.assertGreater(report["casefold"]["checked"], 1_000_000)
        self.assertEqual(proc.returncode, 0)

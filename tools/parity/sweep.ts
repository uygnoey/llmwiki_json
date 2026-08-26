/** 대량 훑기 — 후보(Bun) 쪽.
 *
 * `sweep.py` 가 표본과 정답을 stdin 으로 넘겨 준다. 여기서는 답만 내고
 * 판정은 하지 않는다 — 정답을 보고 맞추는 일이 없도록, 틀린 개수와 예시만
 * 세어서 돌려준다. 표본을 스스로 고르지 않는 것이 핵심이다: 첫 후보 구현은
 * 자기가 고른 표본으로는 통과했지만 |v|>=1e16 에서 틀렸다. */
import { unicode15Casefold } from "./casefold";
import { PythonFloat, pythonDumps } from "./serialize";

interface Payload {
  floats: { count: number; values: number[]; answers: string[] };
  casefold: { scalars: number; changed: Record<string, string> };
}

const payload = JSON.parse(await Bun.stdin.text()) as Payload;

// JSON.parse 는 18.0 을 정수 18 로 만들어 버린다. 원문 답에서 float 임을
// 되살려야 Python 의 표기 규칙을 시험할 수 있다.
const floatMismatches: string[] = [];
let floatBad = 0;
for (let index = 0; index < payload.floats.values.length; index++) {
  const expected = payload.floats.answers[index];
  const got = pythonDumps(new PythonFloat(payload.floats.values[index]));
  if (got !== expected) {
    floatBad++;
    if (floatMismatches.length < 10) floatMismatches.push(`python=${expected} bun=${got}`);
  }
}

const foldMismatches: string[] = [];
let foldBad = 0;
let foldChecked = 0;
for (let cp = 0; cp < 0x110000; cp++) {
  if (cp >= 0xd800 && cp <= 0xdfff) continue;
  const char = String.fromCodePoint(cp);
  const expected = payload.casefold.changed[char] ?? char;
  const got = unicode15Casefold(char);
  foldChecked++;
  if (got !== expected) {
    foldBad++;
    if (foldMismatches.length < 10) {
      foldMismatches.push(`U+${cp.toString(16).toUpperCase().padStart(4, "0")} python=${JSON.stringify(expected)} bun=${JSON.stringify(got)}`);
    }
  }
}

process.stdout.write(JSON.stringify({
  runtime: `bun ${Bun.version}`,
  floats: {checked: payload.floats.values.length, mismatched: floatBad, examples: floatMismatches},
  casefold: {checked: foldChecked, mismatched: foldBad, examples: foldMismatches},
}, null, 2) + "\n");

import { parseOrderedJson, pythonDumps, PythonFloat, type OrderedJson } from "./serialize";
import { unicode15Casefold } from "./casefold";

type PlainCase = { id: string; kind: string; input: any; mitigation: string };
type OrderedCase = Map<string, OrderedJson>;

const casesUrl = new URL("./cases.json", import.meta.url);
const source = await Bun.file(casesUrl).text();
const plainCases = JSON.parse(source).cases as PlainCase[];
const orderedRoot = parseOrderedJson(source) as Map<string, OrderedJson>;
const orderedCases = orderedRoot.get("cases") as OrderedCase[];

function mapGet<T extends OrderedJson>(map: Map<string, OrderedJson>, key: string): T {
  return map.get(key) as T;
}

function naiveRegex(spec: { pattern: string; text: string }): string {
  try {
    const regex = new RegExp(spec.pattern, "gm");
    return JSON.stringify([...spec.text.matchAll(regex)].map((match) => [...match]));
  } catch {
    // 컴파일 실패도 regex 답의 배열 형식을 유지해야 corpus 전체를 끝까지 비교할 수 있다.
    return JSON.stringify([]);
  }
}

function naiveAnswer(test: PlainCase): string {
  const value = test.input;
  switch (test.kind) {
    case "json": return JSON.stringify(value);
    case "json_pretty": return JSON.stringify(value, null, 2);
    case "sha256": return new Bun.CryptoHasher("sha256").update(String(value)).digest("hex");
    case "nfc": return [...String(value).normalize("NFC")]
      .map((char) => `U+${char.codePointAt(0)!.toString(16).toUpperCase().padStart(4, "0")}`).join(" ");
    case "casefold": return JSON.stringify(value.map((item: unknown) => String(item).toLowerCase()));
    case "sort": return JSON.stringify(value.map(String).sort());
    case "codepoints": {
      const text = String(value);
      return JSON.stringify({ length: text.length, head2: text.slice(0, 2) });
    }
    case "regex": return naiveRegex(value);
    default: throw new Error(`unknown case kind: ${test.kind}`);
  }
}

function compareCodePoints(left: string, right: string): number {
  const a = [...left].map((char) => char.codePointAt(0)!);
  const b = [...right].map((char) => char.codePointAt(0)!);
  for (let index = 0; index < Math.min(a.length, b.length); index++) {
    if (a[index] !== b[index]) return a[index] - b[index];
  }
  return a.length - b.length;
}

function pythonRegex(pattern: string): RegExp {
  let flags = "gm";
  const inline = pattern.match(/^\(\?([aiLmsux]+)\)/);
  if (inline) {
    pattern = pattern.slice(inline[0].length);
    if (inline[1].includes("i")) flags += "i";
    if (inline[1].includes("s")) flags += "s";
  }
  // Python의 \d는 Unicode Decimal_Number이므로 JS의 ASCII 전용 escape를 그대로 쓰면 안 된다.
  pattern = pattern.replace(/(^|[^\\])\\d/g, "$1\\p{Decimal_Number}");
  if (pattern.includes("\\p{Decimal_Number}") && !flags.includes("u")) flags += "u";
  return new RegExp(pattern, [...new Set(flags)].join(""));
}

function mitigatedRegex(spec: Map<string, OrderedJson>): string {
  const pattern = mapGet<string>(spec, "pattern");
  const text = mapGet<string>(spec, "text");
  const matches = [...text.matchAll(pythonRegex(pattern))]
    .map((match) => [...match].map((group) => group === undefined ? null : group));
  return pythonDumps(matches);
}

function primitiveString(value: OrderedJson): string {
  if (value instanceof PythonFloat) return String(value.value);
  return String(value);
}

function mitigatedAnswer(test: OrderedCase): string {
  const kind = mapGet<string>(test, "kind");
  const value = mapGet<OrderedJson>(test, "input");
  switch (kind) {
    case "json": return pythonDumps(value);
    case "json_pretty": return pythonDumps(value, true);
    case "sha256": return new Bun.CryptoHasher("sha256").update(primitiveString(value)).digest("hex");
    case "nfc": return [...primitiveString(value).normalize("NFC")]
      .map((char) => `U+${char.codePointAt(0)!.toString(16).toUpperCase().padStart(4, "0")}`).join(" ");
    case "casefold": return pythonDumps((value as OrderedJson[]).map((item) => unicode15Casefold(primitiveString(item))));
    case "sort": return pythonDumps((value as OrderedJson[]).map(primitiveString).sort(compareCodePoints));
    case "codepoints": {
      const points = [...primitiveString(value)];
      return pythonDumps(new Map<string, OrderedJson>([["length", points.length], ["head2", points.slice(0, 2).join("")]]));
    }
    case "regex": return mitigatedRegex(value as Map<string, OrderedJson>);
    default: throw new Error(`unknown case kind: ${kind}`);
  }
}

const results = Object.fromEntries(plainCases.map((test) => [test.id, naiveAnswer(test)]));
const mitigated = Object.fromEntries(orderedCases.map((test) => [mapGet<string>(test, "id"), mitigatedAnswer(test)]));
const versions = process.versions as Record<string, string | undefined>;

process.stdout.write(JSON.stringify({
  runtime: `bun ${Bun.version}`,
  ...(versions.unicode ? { unicode: versions.unicode } : {}),
  results,
  mitigated,
}, null, 2) + "\n");

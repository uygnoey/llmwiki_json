export class PythonFloat {
  constructor(readonly value: number) {}
}

export type OrderedJson = null | boolean | string | number | PythonFloat |
  OrderedJson[] | Map<string, OrderedJson>;

function quote(value: string): string {
  return JSON.stringify(value);
}

function exponentOf(decimal: string): number {
  const lower = decimal.toLowerCase();
  if (lower.includes("e")) return Number(lower.slice(lower.indexOf("e") + 1));
  const unsigned = lower.startsWith("-") ? lower.slice(1) : lower;
  const [integer, fraction = ""] = unsigned.split(".");
  if (integer !== "0") return integer.length - 1;
  const first = fraction.search(/[1-9]/);
  return -first - 1;
}

function scientificFromDecimal(decimal: string): string {
  const negative = decimal.startsWith("-");
  const unsigned = negative ? decimal.slice(1) : decimal;
  const exponent = exponentOf(decimal);
  // Number#toString already supplies the shortest round-tripping digits. At
  // magnitudes where JS still chooses fixed notation but Python chooses
  // scientific notation, the fixed representation merely appends place-value
  // zeroes. They are not significant digits in Python's float repr.
  const digits = unsigned.replace(".", "").replace(/^0+/, "").replace(/0+$/, "");
  const coefficient = digits.length > 1 ? `${digits[0]}.${digits.slice(1)}` : digits;
  const sign = exponent < 0 ? "-" : "+";
  return `${negative ? "-" : ""}${coefficient}e${sign}${Math.abs(exponent).toString().padStart(2, "0")}`;
}

function pythonFloat(value: number): string {
  if (Number.isNaN(value)) return "NaN";
  if (value === Infinity) return "Infinity";
  if (value === -Infinity) return "-Infinity";
  if (Object.is(value, -0)) return "-0.0";

  const decimal = value.toString();
  const exponent = exponentOf(decimal);
  if (decimal.includes("e")) {
    const [coefficient, rawExponent] = decimal.toLowerCase().split("e");
    const numericExponent = Number(rawExponent);
    const sign = numericExponent < 0 ? "-" : "+";
    return `${coefficient}e${sign}${Math.abs(numericExponent).toString().padStart(2, "0")}`;
  }
  if (exponent < -4 || exponent >= 16) return scientificFromDecimal(decimal);
  return Number.isInteger(value) ? `${decimal}.0` : decimal;
}

function entries(value: Map<string, unknown> | Record<string, unknown>): [string, unknown][] {
  if (value instanceof Map) return [...value.entries()];
  return Object.keys(value).map((key) => [key, value[key]]);
}

function encode(value: unknown, pretty: boolean, level: number): string {
  if (value === null || value === undefined) return "null";
  if (typeof value === "string") return quote(value);
  if (typeof value === "boolean") return value ? "true" : "false";
  if (value instanceof PythonFloat) return pythonFloat(value.value);
  if (typeof value === "number") {
    if (!Number.isFinite(value)) return pythonFloat(value);
    return Object.is(value, -0) ? "0" : value.toString();
  }

  const next = level + 1;
  const pad = (depth: number) => "  ".repeat(depth);
  if (Array.isArray(value)) {
    if (value.length === 0) return "[]";
    const parts = value.map((item) => encode(item, pretty, next));
    return pretty
      ? `[\n${pad(next)}${parts.join(`,\n${pad(next)}`)}\n${pad(level)}]`
      : `[${parts.join(",")}]`;
  }

  if (value instanceof Map || (typeof value === "object" && value !== null)) {
    const pairs = entries(value as Map<string, unknown> | Record<string, unknown>);
    if (pairs.length === 0) return "{}";
    const parts = pairs.map(([key, item]) =>
      `${quote(key)}${pretty ? ": " : ":"}${encode(item, pretty, next)}`
    );
    return pretty
      ? `{\n${pad(next)}${parts.join(`,\n${pad(next)}`)}\n${pad(level)}}`
      : `{${parts.join(",")}}`;
  }
  throw new TypeError(`직렬화할 수 없는 값: ${typeof value}`);
}

export function pythonDumps(value: unknown, pretty = false): string {
  // JS 객체는 정수형 키를 먼저 열거하므로, 원래 삽입 순서가 계약이면 Map을 넘겨야 한다.
  // number만으로는 JSON의 18과 18.0을 구분할 수 없어 float 토큰은 PythonFloat로 보존한다.
  return encode(value, pretty, 0);
}

export function parseOrderedJson(source: string): OrderedJson {
  let cursor = 0;
  const fail = (message: string): never => {
    throw new SyntaxError(`${message} (${cursor})`);
  };
  const whitespace = () => {
    while (/\s/.test(source[cursor] ?? "")) cursor++;
  };
  const parseString = (): string => {
    const start = cursor++;
    while (cursor < source.length) {
      if (source[cursor] === "\\") {
        cursor += 2;
      } else if (source[cursor++] === '"') {
        return JSON.parse(source.slice(start, cursor));
      }
    }
    return fail("끝나지 않은 문자열");
  };
  const parseValue = (): OrderedJson => {
    whitespace();
    if (source[cursor] === '"') return parseString();
    if (source[cursor] === "[") {
      cursor++;
      const result: OrderedJson[] = [];
      whitespace();
      if (source[cursor] === "]") { cursor++; return result; }
      while (true) {
        result.push(parseValue());
        whitespace();
        if (source[cursor] === "]") { cursor++; return result; }
        if (source[cursor++] !== ",") fail("배열 구분자 오류");
      }
    }
    if (source[cursor] === "{") {
      cursor++;
      const result = new Map<string, OrderedJson>();
      whitespace();
      if (source[cursor] === "}") { cursor++; return result; }
      while (true) {
        whitespace();
        if (source[cursor] !== '"') fail("객체 키 오류");
        const key = parseString();
        whitespace();
        if (source[cursor++] !== ":") fail("객체 구분자 오류");
        result.set(key, parseValue());
        whitespace();
        if (source[cursor] === "}") { cursor++; return result; }
        if (source[cursor++] !== ",") fail("객체 항목 구분자 오류");
      }
    }
    for (const [token, value] of [["true", true], ["false", false], ["null", null]] as const) {
      if (source.startsWith(token, cursor)) { cursor += token.length; return value; }
    }
    const match = source.slice(cursor).match(/^-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?/);
    if (!match) return fail("값 오류");
    cursor += match[0].length;
    const number = Number(match[0]);
    return /[.eE]/.test(match[0]) ? new PythonFloat(number) : number;
  };

  const result = parseValue();
  whitespace();
  if (cursor !== source.length) fail("뒤에 남은 입력");
  return result;
}

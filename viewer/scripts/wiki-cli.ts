/** package.json 스크립트에서 llmwiki CLI 를 부르는 통로.
 *
 * `python3` 를 그대로 부르면 Windows(=`python`)나 uv 로 받은 Python 만 있는
 * 기계에서 깨진다. 해석은 wiki-data.ts 한 곳에서만 하고 여기서는 그 결과를 쓴다.
 *
 * 사용법: bun run scripts/wiki-cli.ts build [추가 인자...]
 *         bun run scripts/wiki-cli.ts --tests      (백엔드 unittest 스위트) */
import { spawnSync } from "node:child_process";
import { resolve } from "node:path";
import { resolvePython } from "./wiki-data";

const projectRoot = resolve(import.meta.dirname, "..", "..");
const python = resolvePython();

if (!python) {
  console.error("[llmwiki] Python 3.9+ 를 찾지 못했다 — LLMWIKI_PYTHON 에 실행 파일 경로를 주거나 "
    + "저장소 루트에서 scripts/install.sh (Windows 는 scripts\\install.ps1) 로 마련한다");
  process.exit(1);
}

const argv = process.argv.slice(2);
const args = argv[0] === "--tests"
  ? ["-m", "unittest", "discover", "-s", "tests", "-v", ...argv.slice(1)]
  : ["scripts/llmwiki.py", ...argv];

const result = spawnSync(python, args, {cwd: projectRoot, stdio: "inherit"});
process.exit(result.status ?? 1);

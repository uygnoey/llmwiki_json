/** wiki 정본이 바뀌면 파생물을 다시 만드는 공용 로직.
 *
 * 개발 서버(vite)와 정적 배포용 감시자가 같은 코드를 쓴다.
 * 산출물 생성 자체는 `scripts/llmwiki.py build` 가 정본이므로 여기서는 호출만 한다. */
import { spawn, spawnSync } from "node:child_process";
import { readdirSync, statSync } from "node:fs";
import { join, resolve } from "node:path";

/** 이 컴퓨터에서 실제로 도는 Python 을 찾는다.
 *
 * `python3` 는 어디에나 있는 이름이 아니다 — Windows 에는 `python` 뿐이고,
 * scripts/install.sh 가 uv 로 받아 온 Python 은 PATH 에 올라가지도 않는다.
 * `LLMWIKI_PYTHON` 이 있으면 그것을 최우선으로 쓴다. */
const PYTHON_CANDIDATES = [process.env.LLMWIKI_PYTHON, "python3", "python", "py"].filter(
  (value): value is string => Boolean(value),
);

let cachedPython: string | undefined;

export function resolvePython(): string | undefined {
  if (cachedPython) return cachedPython;
  for (const candidate of PYTHON_CANDIDATES) {
    const probe = spawnSync(candidate, ["-c", "import sys;raise SystemExit(0 if sys.version_info>=(3,9) else 1)"], {stdio: "ignore"});
    if (!probe.error && probe.status === 0) { cachedPython = candidate; return candidate; }
  }
  return undefined;
}

export interface WikiDataOptions {
  /** 저장소 루트. 이 아래에 wiki/ 와 scripts/ 가 있다. */
  projectRoot: string;
  /** build 가 성공한 뒤 호출된다. */
  onBuilt?: () => void;
  /** 저장이 연달아 일어날 때 묶는 시간(ms). */
  debounceMs?: number;
}

/** build 를 직렬로 돌린다. 실행 중에 또 요청이 오면 끝난 뒤 한 번만 더 돌린다. */
export function createWikiData({projectRoot, onBuilt, debounceMs = 250}: WikiDataOptions) {
  let building = false;
  let pending = false;
  let timer: ReturnType<typeof setTimeout> | undefined;

  const build = () => {
    if (building) { pending = true; return; }
    const python = resolvePython();
    if (!python) {
      console.error("[llmwiki] Python 3.9+ 를 찾지 못해 파생물을 만들지 못했다 — "
        + "LLMWIKI_PYTHON 에 실행 파일 경로를 주거나 scripts/install.sh 로 마련한다");
      return;
    }
    building = true;
    const child = spawn(python, ["scripts/llmwiki.py", "build"], {cwd: projectRoot, stdio: "inherit"});
    const done = (ok: boolean) => {
      building = false;
      if (ok) onBuilt?.();
      if (pending) { pending = false; build(); }
    };
    child.on("error", (error) => {
      console.error(`[llmwiki] ${python} 실행 실패 — ${error.message}`);
      done(false);
    });
    child.on("close", (code) => {
      if (code !== 0) console.error(`[llmwiki] build 실패 (exit ${code})`);
      done(code === 0);
    });
  };

  const schedule = () => {
    if (timer) clearTimeout(timer);
    timer = setTimeout(build, debounceMs);
  };

  return {build, schedule};
}

/** wiki 아래 json 의 경로 → "mtime:size". 추가·삭제·수정이 모두 이 비교로 잡힌다. */
export function snapshot(wikiRoot: string): Map<string, string> {
  const out = new Map<string, string>();
  const walk = (dir: string) => {
    for (const entry of readdirSync(dir, {withFileTypes: true})) {
      const path = join(dir, entry.name);
      if (entry.isDirectory()) { walk(path); continue; }
      if (!entry.name.endsWith(".json") && entry.name !== "log.jsonl") continue;
      try {
        const stat = statSync(path);
        out.set(path, `${stat.mtimeMs}:${stat.size}`);
      } catch { /* 감시 중 사라진 파일은 다음 회차에 반영된다 */ }
    }
  };
  walk(wikiRoot);
  return out;
}

const same = (a: Map<string, string>, b: Map<string, string>) =>
  a.size === b.size && [...a].every(([key, value]) => b.get(key) === value);

/** 주기적으로 스냅샷을 비교해 build 를 건다.
 *
 * inotify 가 아니라 폴링을 쓰는 이유: Docker Desktop 의 bind mount 는 호스트의 파일 변경
 * 이벤트를 컨테이너 안으로 전달하지 않는다. 개발 서버는 호스트에서 도니 chokidar 를 쓰고,
 * 컨테이너 감시자는 이 함수를 쓴다. */
export function pollWikiData(options: WikiDataOptions & {intervalMs?: number}) {
  const {projectRoot, intervalMs = 2000} = options;
  const wikiRoot = resolve(projectRoot, "wiki");
  const {build} = createWikiData({...options, debounceMs: 0});

  build();
  let previous = snapshot(wikiRoot);
  console.log(`[llmwiki-watch] 감시 시작 — ${previous.size}개 파일, ${intervalMs}ms 주기`);

  return setInterval(() => {
    const current = snapshot(wikiRoot);
    if (same(previous, current)) return;
    previous = current;
    build();
  }, intervalMs);
}

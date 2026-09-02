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

/** build 를 직렬로 돌린다. 실행 중에 또 요청이 오면 끝난 뒤 한 번만 더 돌린다.
 *
 * 바뀐 파일 경로를 알면 `build --changed <경로…>` 로 넘겨 색인의 그 page 행만 갈아 끼우게 한다
 * (debounce 동안 모인 경로는 중복을 빼고 한 번에 넘긴다). 경로는 힌트일 뿐이라 build 쪽이
 * mtime 스캔으로 힌트 밖의 변경을 잡으면 스스로 전량으로 떨어지고, 증분 build 가 실패하면
 * 여기서 인자 없는 build 로 한 번 더 시도한다. 경로 없이 부르면 처음부터 전량이다. */
export function createWikiData({projectRoot, onBuilt, debounceMs = 250}: WikiDataOptions) {
  let building = false;
  let pending = false;
  let pendingFull = false;
  let timer: ReturnType<typeof setTimeout> | undefined;
  const queued = new Set<string>();

  const run = (changed: string[] | undefined, done: (ok: boolean) => void) => {
    const python = resolvePython();
    if (!python) {
      console.error("[llmwiki] Python 3.9+ 를 찾지 못해 파생물을 만들지 못했다 — "
        + "LLMWIKI_PYTHON 에 실행 파일 경로를 주거나 scripts/install.sh 로 마련한다");
      done(false);
      return;
    }
    const args = buildArgs(changed);
    const child = spawn(python, args, {cwd: projectRoot, stdio: "inherit"});
    child.on("error", (error) => {
      console.error(`[llmwiki] ${python} 실행 실패 — ${error.message}`);
      done(false);
    });
    child.on("close", (code) => {
      if (code !== 0) console.error(`[llmwiki] build 실패 (exit ${code})${changed ? " — 인자 없이 다시 시도한다" : ""}`);
      done(code === 0);
    });
  };

  /** 경로를 주면 증분, 안 주면 전량. 실행 중이면 경로를 모아 두었다가 끝난 뒤 한 번 더 돈다. */
  const build = (changed?: string[]) => {
    if (changed) for (const path of changed) queued.add(path);
    else pendingFull = true;
    if (building) { pending = true; return; }
    const paths = pendingFull ? undefined : [...queued].sort();
    queued.clear();
    pendingFull = false;
    building = true;
    const finish = (ok: boolean) => {
      building = false;
      if (ok) onBuilt?.();
      if (pending) { pending = false; build(); }
    };
    run(paths, (ok) => {
      if (ok || !paths) { finish(ok); return; }
      run(undefined, finish);
    });
  };

  /** 저장 이벤트마다 부른다. 바뀐 파일 경로를 주면 debounce 동안 모아 `--changed` 로 넘긴다. */
  const schedule = (path?: string) => {
    if (path) queued.add(path);
    else pendingFull = true;
    if (timer) clearTimeout(timer);
    timer = setTimeout(() => { timer = undefined; build(); }, debounceMs);
  };

  return {build, schedule};
}

/** `scripts/llmwiki.py build` 의 인자. 경로가 있으면 `--changed` 뒤에 붙인다 (증분 힌트). */
export function buildArgs(changed?: string[]): string[] {
  const args = ["scripts/llmwiki.py", "build"];
  if (changed && changed.length > 0) args.push("--changed", ...changed);
  return args;
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

/** 두 스냅샷 사이에 추가·삭제·수정된 경로 (정렬, 중복 없음). */
export function changedPaths(previous: Map<string, string>, current: Map<string, string>): string[] {
  const out = new Set<string>();
  for (const [key, value] of current) if (previous.get(key) !== value) out.add(key);
  for (const key of previous.keys()) if (!current.has(key)) out.add(key);
  return [...out].sort();
}

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
    const changed = changedPaths(previous, current);
    previous = current;
    build(changed);
  }, intervalMs);
}

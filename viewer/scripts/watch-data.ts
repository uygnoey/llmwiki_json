/** 정적 배포(nginx)용 감시자. `bun run watch:data` 또는 compose 의 watcher 서비스가 띄운다.
 *
 * 개발 서버는 vite 플러그인이 같은 일을 하므로 이 파일이 필요 없다. */
import { resolve } from "node:path";
import { pollWikiData } from "./wiki-data";

const projectRoot = resolve(import.meta.dirname, "..", "..");
const intervalMs = Number(process.env.LLMWIKI_WATCH_INTERVAL_MS ?? 2000);

pollWikiData({
  projectRoot,
  intervalMs,
  onBuilt: () => console.log(`[llmwiki-watch] build 완료 ${new Date().toLocaleTimeString()}`),
});

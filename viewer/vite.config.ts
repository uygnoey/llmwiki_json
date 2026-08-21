import { defineConfig } from "vite";
import type { Plugin } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { spawn } from "node:child_process";
import { resolve } from "node:path";

function liveWikiData(): Plugin {
  const projectRoot = resolve(__dirname, "..");
  let building = false;
  let pending = false;
  let debounce: ReturnType<typeof setTimeout> | undefined;
  return {
    name: "llmwiki-live-data",
    configureServer(server) {
      const build = () => {
        if (building) { pending = true; return; }
        building = true;
        const child = spawn("python3", ["scripts/llmwiki.py", "build"], {cwd: projectRoot, stdio: "inherit"});
        child.on("close", (code) => {
          building = false;
          if (code === 0) server.ws.send({type: "custom", event: "llmwiki:data", data: {at: Date.now()}});
          if (pending) { pending = false; build(); }
        });
      };
      const wikiRoot = resolve(projectRoot, "wiki");
      server.watcher.add(wikiRoot);
      server.watcher.on("all", (_event, path) => {
        if (path.startsWith(wikiRoot) && path.endsWith(".json")) {
          if (debounce) clearTimeout(debounce);
          debounce = setTimeout(build, 250);
        }
      });
      build();
    },
  };
}

export default defineConfig({
  root: resolve(__dirname),
  plugins: [liveWikiData(), react(), tailwindcss()],
  resolve: { alias: { "@": resolve(__dirname, "src") } },
  publicDir: resolve(__dirname, "public"),
  build: { outDir: resolve(__dirname, "dist"), emptyOutDir: true },
  server: { host: "127.0.0.1", port: 4173 },
});

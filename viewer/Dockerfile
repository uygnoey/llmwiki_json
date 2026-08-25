# SPA 번들만 이미지에 굽는다. 파생 데이터(/data)는 compose 에서 bind mount 로 붙인다.
FROM oven/bun:1.3.14-slim AS base

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates python3 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY viewer/package.json viewer/bun.lock ./viewer/
RUN cd viewer && bun install --frozen-lockfile

FROM base AS builder

COPY scripts/llmwiki.py ./scripts/llmwiki.py
COPY tools/config ./tools/config
COPY tools/schema ./tools/schema
COPY wiki ./wiki
COPY viewer ./viewer

RUN cd viewer && bun run build

# wiki 변경을 감시해 파생물을 다시 만든다. 감시 로직은 viewer/scripts 의 TypeScript 다.
FROM base AS watcher

WORKDIR /repo
CMD ["bun", "run", "/repo/viewer/scripts/watch-data.ts"]

FROM nginx:1.27-alpine AS viewer

COPY viewer/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=builder /app/viewer/dist /usr/share/nginx/html

EXPOSE 80

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
  CMD wget -qO- http://127.0.0.1/ >/dev/null || exit 1

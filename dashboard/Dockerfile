# The news_bot dashboard (https://evidence.dev): an Evidence site built once
# here in SPA mode, plus refresh.mjs, which re-reads the bot's database whenever
# it changes. nginx (the `dashboard` compose service) serves what this writes.
#
# Build this on a machine with RAM to spare, not on the Pi: the site build needs
# more than 1 GB of JS heap. scripts/ship_dashboard.sh builds it on the Mac
# (arm64, like the Pi) and loads it on the Pi. The refresh at run time is small.
FROM node:22-bookworm-slim@sha256:18052396bf1d0c6e0a8c58181b5c1457da19409efbd5032a3f4d86974e74d13c AS build

ENV SEND_ANONYMOUS_USAGE_STATS=no
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY evidence.config.yaml svelte.config.js ./
COPY sources ./sources
COPY pages ./pages
RUN VITE_EVIDENCE_SPA=true npm run build

FROM node:22-bookworm-slim@sha256:18052396bf1d0c6e0a8c58181b5c1457da19409efbd5032a3f4d86974e74d13c

ENV SEND_ANONYMOUS_USAGE_STATS=no \
    NODE_ENV=production \
    HOME=/tmp
WORKDIR /app
COPY --from=build /app/package.json /app/evidence.config.yaml ./
COPY --from=build /app/node_modules ./node_modules
COPY --from=build /app/sources ./sources
COPY --from=build /app/build ./build
COPY refresh.mjs ./
# refresh.mjs snapshots the bot's DB to this tmpfs path before each refresh.
RUN ln -s /tmp/trade_history.db sources/trade_history/trade_history.db
# The site volume is created from this directory on first use, so it starts
# out owned by `node` and writable by the refresher.
RUN mkdir /site && chown node:node /site

USER node
CMD ["node", "refresh.mjs"]

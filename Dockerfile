# unified-agent hub image (task #1194)
#
#   docker build -t unified-agent .
#   docker run -d -v ua-data:/data -p 127.0.0.1:8765:8765 unified-agent
#
# The first start runs `unified-agent init` and prints the generated bearer
# token exactly once to the container log (see deploy/docker/entrypoint.sh).
# A hosted start (Railway, or UA_HOSTED=1) never prints it; see
# docs/deploy/railway.md.

ARG UV_VERSION=0.11.32

# --- uv binary (pinned) ----------------------------------------------------
# Base images are pinned by multi-arch index digest; .github/dependabot.yml
# bumps the node and python digests. The uv digest belongs to UV_VERSION, so
# overriding that build arg without updating it fails the build (fail closed).
FROM ghcr.io/astral-sh/uv:${UV_VERSION}@sha256:df4cae8f3a96d175e2e5f992e597550000edbe78fdc2594d5cd8de1a217f504c AS uvbin

# --- Control Center SPA (packages/web → dist, not committed to git) --------
# Mirrors the `web` job in .github/workflows/ci.yml: protocol builds first,
# then client, then web. Like CI, protocol and web use `npm ci` (web since task
# #1293); client keeps `npm install` because its lockfile does not satisfy
# `npm ci` (see the CI client job and tests/test_install_assets.py).
FROM node:26-slim@sha256:e6bedef217677b276c591795e169840edd9096dbf13f29df8ef8d1856b8820bc AS web
WORKDIR /build
COPY packages/protocol ./packages/protocol
COPY packages/client ./packages/client
COPY packages/web ./packages/web
WORKDIR /build/packages/protocol
RUN npm ci && npm run build
WORKDIR /build/packages/client
RUN npm install && npm run build
WORKDIR /build/packages/web
RUN npm ci && npm run build

# --- Python dependencies --------------------------------------------------
# 3.14 (matches .python-version): every locked native dep ships a cp314/abi3
# manylinux wheel (onnxruntime cp314, sqlite-vec py3-manylinux,
# tree-sitter-language-pack cp310-abi3), so no apt build toolchain is needed.
FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928 AS builder
COPY --from=uvbin /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY src ./src
# Editable install keeps src/unified_agent/webui/spa.py resolving
# REPO_ROOT=/app, which is where the web dist and docs catalog are copied.
RUN uv sync --frozen --no-dev --extra pdf

# --- runtime ---------------------------------------------------------------
FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928 AS runtime

ARG VCS_REF=""
ARG VERSION=""
ARG SOURCE_URL=""

LABEL org.opencontainers.image.source="${SOURCE_URL}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.version="${VERSION}"

RUN groupadd --gid 10001 ua \
    && useradd --uid 10001 --gid ua --home-dir /data --shell /usr/sbin/nologin ua \
    && mkdir -p /data/workspace \
    && chown -R ua:ua /data

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY pyproject.toml uv.lock ./
COPY src ./src
# Runtime doc read: /api/capability-catalog serves docs/capability-catalog/catalog.json.
COPY docs/capability-catalog ./docs/capability-catalog
COPY --from=web /build/packages/web/dist ./packages/web/dist
COPY deploy/docker/entrypoint.sh /app/deploy/docker/entrypoint.sh

ENV PATH="/app/.venv/bin:${PATH}" \
    UA_HOME=/data \
    UA_WORKSPACE=/data/workspace \
    UA_TRANSPORT=http \
    UA_HTTP_HOST=0.0.0.0 \
    UA_HTTP_PORT=8765 \
    UA_ENFORCE_AUTH=1 \
    UA_EDITION=enterprise \
    PYTHONUNBUFFERED=1

USER ua
# No VOLUME instruction: Railway rejects it ("docker VOLUME ... is not
# supported, use Railway Volumes", task #1441), and compose declares its own
# named volume. Mount persistent storage at /data on every host.
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8765/readyz',timeout=4).status==200 else 1)"]

ENTRYPOINT ["/app/deploy/docker/entrypoint.sh"]

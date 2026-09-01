# syntax=docker/dockerfile:1
# Single-image deploy (RC1-195): node builds the web app, python serves it
# same-origin with the FastAPI API. No separate web server, no CORS in prod.

# --- stage 1: build the web app ---
FROM node:20-slim AS web
WORKDIR /web
COPY apps/web/package.json apps/web/package-lock.json ./
RUN npm ci
COPY apps/web/ ./
# reads apps/web/.env.production (relative API base) -> /web/dist
RUN npm run build

# --- stage 2: python runtime ---
FROM python:3.12-slim AS runtime
RUN pip install --no-cache-dir uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# Install just the API package and the workspace deps it pulls in (RC1-357).
# --all-packages requires every member of [tool.uv.workspace] to exist in the
# build context; apps/mcp and apps/evals are not copied here and have no place
# in a production image. --package keeps this correct as the workspace grows.
COPY pyproject.toml uv.lock ./
COPY packages/ ./packages/
COPY apps/api/ ./apps/api/
RUN uv sync --package launch-planner-api --frozen --no-dev

# Runtime assets: the flagship fixtures the API serves, and the built web app.
COPY fixtures/ ./fixtures/
COPY --from=web /web/dist ./web-dist

# Git metadata for Datadog source-code integration (RC1-356). Declared after
# the expensive build steps so a new commit sha busts only this layer. The
# image has no git at runtime, so the sha must arrive as a build arg.
ARG GIT_SHA=""

ENV LPA_WEB_DIST=/app/web-dist \
    LPA_DATABASE_URL=sqlite:////data/launch_planner.db \
    LPA_PUBLIC_DEMO=true \
    LPA_ENVIRONMENT=production \
    DD_GIT_COMMIT_SHA=$GIT_SHA \
    DD_GIT_REPOSITORY_URL=https://github.com/snacksnack/launch-planner-agent \
    DD_VERSION=$GIT_SHA

EXPOSE 8080
# --no-sync is load-bearing: `uv run` re-syncs the workspace by default, which
# fails in this image for the same reason --all-packages did. Without it the
# image builds green and crash-loops on boot (RC1-357).
CMD ["uv", "run", "--no-sync", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]

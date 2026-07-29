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

# Install the Python workspace (cached unless deps change).
COPY pyproject.toml uv.lock ./
COPY packages/ ./packages/
COPY apps/api/ ./apps/api/
RUN uv sync --all-packages --frozen --no-dev

# Runtime assets: the flagship fixtures the API serves, and the built web app.
COPY fixtures/ ./fixtures/
COPY --from=web /web/dist ./web-dist

ENV LPA_WEB_DIST=/app/web-dist \
    LPA_DATABASE_URL=sqlite:////data/launch_planner.db \
    LPA_PUBLIC_DEMO=true \
    LPA_ENVIRONMENT=production

EXPOSE 8080
CMD ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]

# Repo Archaeologist: API + website in one image.
#   docker build -t repo-archaeologist .
#   docker run -p 8765:8765 -e GEMINI_API_KEY=... -e ARCH_ACCESS_CODE=... repo-archaeologist
# All settings are environment variables (see DEPLOY.md); no secrets are baked in.

# -- website ---------------------------------------------------------------------------
FROM node:22-slim AS web
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund --loglevel=error
COPY frontend/ ./
# builds into /app/src/archaeologist/web/static (vite.config.ts default)
RUN npm run build

# -- API --------------------------------------------------------------------------------
FROM python:3.12-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.11.4 /uv /uvx /bin/

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --extra embed --extra outline --no-install-project
COPY src/ ./src/
COPY --from=web /app/src/archaeologist/web/static ./src/archaeologist/web/static
RUN uv sync --frozen --no-dev --extra embed --extra outline

RUN useradd --create-home --uid 1000 app && mkdir -p /data && chown app /data
USER app

# Public-server defaults; override any of them with -e / the host's environment settings.
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PORT=8765 \
    ARCH_HOME=/data \
    ARCH_WEB_ALLOW_LOCAL=0 \
    ARCH_CORS_ORIGINS=* \
    ARCH_RUNS_PER_HOUR=20 \
    ARCH_MAX_REPO_MB=500 \
    GIT_TERMINAL_PROMPT=0

EXPOSE 8765
CMD ["sh", "-c", "exec arch serve --host 0.0.0.0 --port \"$PORT\" --no-open"]

# syntax=docker/dockerfile:1
# job-scout: the daily pipeline in one image (PLAN.md Phase 6). Built and run by
# deploy/compose.yaml; see deploy/README.md. config/ and /var/lib/job-scout are mounted.

ARG PYTHON_VERSION=3.12
# Pinned, and updated deliberately: CLI flags can change between releases (PLAN.md Phase 6).
ARG CLAUDE_CODE_VERSION=2.1.296
ARG UV_VERSION=0.12.17

# --- claude CLI: npm ships a native binary per platform; only the binary is kept -----------
FROM node:22-bookworm-slim AS claude
ARG CLAUDE_CODE_VERSION
RUN npm install --global --no-audit --no-fund "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
 && cp "$(npm root --global)/@anthropic-ai/claude-code/bin/claude.exe" /claude \
 && /claude --version

# --- Python dependencies and the project, from uv.lock ------------------------------------
FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv
FROM python:${PYTHON_VERSION}-slim-bookworm AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

# --- Runtime -------------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim-bookworm
ARG UID=1000
ARG GID=1000
RUN groupadd --gid "${GID}" job-scout \
 && useradd --uid "${UID}" --gid "${GID}" --home-dir /var/lib/job-scout/home --no-create-home \
      --shell /usr/sbin/nologin job-scout
COPY --from=claude /claude /usr/local/bin/claude
COPY --from=build /app/.venv /app/.venv
COPY alembic.ini /app/alembic.ini
COPY deploy/entrypoint.sh /usr/local/bin/entrypoint
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    JOB_SCOUT_CONFIG_DIR=/app/config \
    HOME=/var/lib/job-scout/home
WORKDIR /app
USER ${UID}:${GID}
ENTRYPOINT ["entrypoint"]
CMD ["run"]

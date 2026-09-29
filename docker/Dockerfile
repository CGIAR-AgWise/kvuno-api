# syntax=docker/dockerfile:1
# Copyright (c) 2026 Munywele Consulting LTD. All rights reserved.

ARG POETRY_VERSION=2.3.2
FROM ghcr.io/masgeek/python-3.14-poetry:${POETRY_VERSION} AS builder

WORKDIR /app

# poetry.lock pins the dependency set; without it the build re-resolves on every
# image and drifts from CI. README.md is required because pyproject.toml declares
# `readme = "README.md"`, and Poetry validates that at install time.
COPY pyproject.toml poetry.lock README.md ./

RUN --mount=type=cache,target=/root/.cache/pypoetry \
    poetry install --no-root --only main


FROM python:3.14-slim AS runtime

# Set by CI from the git tag (or branch name for dev builds); see
# .github/workflows/docker-build-reusable.yml. Surfaces in /health and the
# OpenAPI Info block via app/config.py.
ARG APP_VERSION=0.0.0-dev

ENV APP_VERSION=${APP_VERSION}
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    HOME=/app \
    SERVER_PORT=80

# Match these to your host user (id -u / id -g) so bind-mounted files keep
# correct ownership. Defaults are the common single-user Linux/macOS 1000.
ARG UID=1000
ARG GID=1000

RUN groupadd -g ${GID} app \
    && useradd -u ${UID} -g app -d /app -s /bin/bash --no-log-init app \
    && install -d -o app -g app /app

WORKDIR /app

# venv stays root-owned and read-only to the app user. It must land at the same
# path as in the builder, since venv shebangs are absolute.
COPY --from=builder /app/.venv /app/.venv
COPY --chown=app:app . .
RUN chmod +x /app/docker/entrypoint.sh

USER app

EXPOSE 80

# Applies pending Alembic migrations, then execs the CMD below. Set
# RUN_MIGRATIONS=false to skip.
ENTRYPOINT ["/app/docker/entrypoint.sh"]

CMD ["python3", "run.py"]
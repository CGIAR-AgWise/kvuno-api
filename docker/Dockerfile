# syntax=docker/dockerfile:1
# Copyright (c) 2026 Munywele Consulting LTD. All rights reserved.
#
# Single image definition for both dev and prod runs.
#
# Two independent switches:
#
#   WITH_DEV  (build arg)  include the dev dependency group
#                           true  -> adds ruff, pytest, pip-audit, ...
#                           false -> runtime deps only (use for anything published)
#
#   APP_MODE  (env, baked from the build arg, overridable at run time)
#                           dev  -> `python3 run.py`  (Flask dev server, reloader)
#                           prod -> gunicorn
#
# Why they are separate: whether dev dependencies exist is fixed when the image
# is built, but which server runs is a runtime concern. Switching APP_MODE to
# prod on an image built WITH_DEV=true works; switching it on an image built
# WITH_DEV=false also works, but the image carries no test tooling. There is no
# way to add packages at run time, so WITH_DEV cannot be a runtime switch.
#
# Examples:
#   docker build -f docker/Dockerfile --build-arg WITH_DEV=true  --build-arg APP_MODE=dev  -t kvuno:dev .
#   docker build -f docker/Dockerfile --build-arg WITH_DEV=false --build-arg APP_MODE=prod -t kvuno:prod .

ARG POETRY_VERSION=2.3.2
FROM ghcr.io/masgeek/python-3.14-poetry:${POETRY_VERSION} AS builder

WORKDIR /app

# poetry.lock pins the dependency set; without it the build re-resolve on every
# image and drifts from CI. README.md is required because pyproject.toml declares
# `readme = "README.md"`, and Poetry validates that at install time.
COPY pyproject.toml poetry.lock README.md ./

ARG WITH_DEV=false

RUN --mount=type=cache,target=/root/.cache/pypoetry \
    if [ "$WITH_DEV" = "true" ]; then \
        poetry install --no-root --no-ansi; \
    else \
        poetry install --no-root --only main --no-ansi; \
    fi

FROM python:3.14-slim AS runtime

ARG APP_MODE=dev

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    HOME=/app \
    APP_MODE=${APP_MODE}

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

EXPOSE 5000

# /health works in both modes, so the check is always safe to run.
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:5000/health')" || exit 1

# Applies pending Alembic migrations, then starts the server: gunicorn when
# APP_MODE=prod, otherwise the CMD below. Set RUN_MIGRATIONS=false to skip —
# required if you run more than one replica, so they don't race to migrate.
ENTRYPOINT ["/app/docker/entrypoint.sh"]

CMD ["python3", "run.py"]

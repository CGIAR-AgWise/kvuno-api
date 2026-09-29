ARG POETRY_VERSION=2.3.2
FROM ghcr.io/masgeek/python-3.14-poetry:${POETRY_VERSION} AS builder

WORKDIR /app

# poetry.lock pins the dependency set; without it the build re-resolve on every
# image and drifts from CI. README.md is required because pyproject.toml declares
# `readme = "README.md"`, and Poetry validates that at install time.
COPY pyproject.toml poetry.lock README.md ./

RUN poetry install --no-root --without dev --no-ansi

FROM python:3.14-slim AS runtime

# Set by CI from the git tag (or branch name for dev builds); see
# .github/workflows/docker-build-reusable.yml. Surfaces in /health and the
# OpenAPI Info block via app/config.py.
ARG APP_VERSION=0.0.0-dev

ENV APP_VERSION=${APP_VERSION}
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    HOME=/app

RUN groupadd -r app && useradd -r -g app -d /app -s /sbin/nologin app

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY --chown=app:app . .
RUN chown app:app /app && chmod +x /app/docker/entrypoint.sh

USER app

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:80/health')" || exit 1

# Applies pending Alembic migrations, then execs the CMD below. Set
# RUN_MIGRATIONS=false to skip — required if you run more than one replica, so
# they don't race to migrate the same schema.
ENTRYPOINT ["/app/docker/entrypoint.sh"]

# Loads app/gunicorn_config.py, so bind_ip / bind_port / workers / LOG_LEVEL
# env vars actually take effect. Gunicorn would otherwise auto-load only
# gunicorn.conf.py from the working directory, which does not exist here.
CMD ["gunicorn", "-c", "app/gunicorn_config.py", "wsgi:app"]
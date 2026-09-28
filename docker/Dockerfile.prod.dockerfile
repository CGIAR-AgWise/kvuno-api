ARG POETRY_VERSION=2.3.2
FROM ghcr.io/masgeek/python-3.14-poetry:${POETRY_VERSION} AS builder

WORKDIR /app

# poetry.lock pins the dependency set; without it the build re-resolve on every
# image and drifts from CI. README.md is required because pyproject.toml declares
# `readme = "README.md"`, and Poetry validates that at install time.
COPY pyproject.toml poetry.lock README.md ./

RUN poetry install --no-root --without dev --no-ansi

FROM python:3.14-slim AS runtime

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

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:5000/health')" || exit 1

# Applies pending Alembic migrations, then execs the CMD below. Set
# RUN_MIGRATIONS=false to skip — required if you run more than one replica, so
# they don't race to migrate the same schema.
ENTRYPOINT ["/app/docker/entrypoint.sh"]

CMD ["gunicorn", "-b", "0.0.0.0:5000", "-w", "4", "--timeout", "60", "wsgi:app"]
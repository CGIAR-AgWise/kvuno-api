# AGENTS.md — kvuno-api

Flask (flask-openapi3) + SQLAlchemy + PostGIS API that ingests agricultural `.RDS`/`.parquet` files and serves crop planting data. Background ingestion via Celery/Redis. Python 3.13, Poetry.

## Commands

```bash
poetry install                     # venv is in-project (.venv), see poetry.toml
poetry run ruff check .            # lint (CI gate; no ruff config -> defaults, line length 88)
poetry run pytest -v               # all tests
poetry run pytest tests/test_config.py -v      # single file
poetry run pytest -k build_db_url              # single test
poetry run pip-audit --strict      # CI also runs this; deps are security-gated
python run.py                      # dev server (dev console script: `poetry run dev`)
python dev_worker.py               # Celery worker, auto-picks --pool solo on Windows
python housekeeping.py [--dry-run|--watch|--batch-size N] [dir]   # one-off ETL, no Celery
```

Order that matters: `ruff check .` -> `pytest`. There is no typecheck step (pylint config in `.pylintrc` is not wired into CI).

## Environment quirks (high value)

- `app/config.py` **raises at import time** if `build_db_url()` finds neither `DB_URL` nor all of `DB_USER`/`DB_PASSWORD`/`DB_NAME`. There is no `JWT_SECRET` — auth uses opaque tokens, so no signing key is needed. Anything importing `app.*` (including the Celery worker and Alembic) needs a populated `.env` (`.env.example` is the template; `.env` is git-ignored).
- **Postgres driver is psycopg3, not psycopg2.** `poetry.lock` pins SQLAlchemy 2.1.x, which flipped the default dialect: `postgresql://` (what `build_db_url()` produces) now resolves to `PGDialect_psycopg`, so the `psycopg` v3 package is required — without it the app dies on connect with `ModuleNotFoundError: No module named 'psycopg'`. `psycopg2-binary` is still declared but is now dead weight unless the URL is explicitly `postgresql+psycopg2://`. ⚠️ `psycopg` (not `psycopg[binary]`) loads **system libpq**, which `python:3.14-slim` does not have — a container needs `psycopg[binary]` or `libpq` installed.
- `DB_URL` wins over the individual `DB_*` vars. `DB_DRIVER=sqlite` + `DB_NAME=file.db` gives a zero-infra local dev setup; Postgres path uses `postgis/postgis:17-3.5`.
- Tests rely on `pytest-env` (`pyproject.toml [tool.pytest.ini_options]`) forcing `DB_URL=sqlite:///:memory:` so app imports don't hit real config. Test suite is **unit-only** — no DB, no Redis, no API-server fixture. If you add tests, don't assume an app context or client exists.
- CI runs a JS audit step only if `pnpm-lock.yaml` exists; it is absent, so that step is skipped. `node_modules/` in the repo root is unrelated to the Python toolchain.

## Architecture

- `app/__init__.py::create_app()` is the only wiring point: builds the `OpenAPI` app (`doc_prefix="/api-docs"`), CORS, rate limiter, error handlers, security headers, DB, then registers API blueprints + `app/routes/main.py`. `run.py` pushes an app context at import time — anything importing `run` gets one.
- API layer uses **flask-openapi3 `APIBlueprint`s**, not Flask blueprints. `app/api/planting_data.py` intentionally splits into `public_api` and `protected_api` sharing the same `url_prefix` (`/api/v1/planting-data`); only `protected_api` carries the JWT `abp_security`. Both are registered in `register_apis`.
- `app/routes/main.py::register_app_routes` holds all `/ui/*` HTML pages, resumable-upload chunk endpoints, and `/health`. `/` redirects to `/ui/jobs` (README still claims `/api-docs`).
- Layering: `api/` (HTTP) -> `repo/` (queries) -> `models/kvuno.py` (ORM, GeoAlchemy2). `dto/` holds Pydantic request/response models, including all filter validation. `services/` holds ingestion logic (`housekeeper.py`, `progress_store.py`, `watch_handler.py`); `tasks.py` is a thin Celery wrapper that builds its own Flask app + `MyDb` for app context — it does **not** import `create_app`.
- Auth: `app/auth.py::require_auth` accepts `Authorization: Bearer {id}|{secret}` or a `token` cookie; HTML requests get redirected to `/ui/login`, API clients get 401 JSON. Any new `/ui` route or protected API must use it.
- Ingestion is resumable: SHA-256 checksum dedupe in `file_imports`, per-file `offset` checkpoints, batch inserts in savepoints. Don't restructure the row-commit/offset-persist order in `services/housekeeper.py` without reading it fully.

## Database / migrations

- Alembic `sqlalchemy.url = %(DB_URL)s`; `alembic/env.py` injects `build_db_url()` at runtime, so no URL is ever hardcoded.
- After changing `app/models/kvuno.py`: `alembic revision --autogenerate -m "..."`, then **edit the generated file by hand** — env.py's `include_object` already skips PostGIS bookkeeping tables, but SQLite vs Postgres need manual branches via `app/utils/migration_utils.py` (`get_integer_column_type()` returns `Integer` on SQLite, `BigInteger` elsewhere).
- **Migrations run from the container entrypoint, not from `create_app()`.** `docker/entrypoint.sh` calls `scripts/run_migrations.py` before the API starts, gated on `RUN_MIGRATIONS` (default true). Outside Docker, run `alembic upgrade head` yourself or the app will 500 on missing tables. `run_migrations()` (`app/__init__.py`) is a no-op when the DB is unreachable, so a slow-starting database does not block boot. The worker image has no entrypoint and never migrates — it only receives work from the already-migrated API, and Celery's `autoretry_for=(Exception,)` covers the brief window otherwise.
- `python model-generator.py` regenerates `app/models/kvuno.py` from a live DB via `sqlacodegen` (**it truncates the file to a dummy comment first**, so it destroys hand-written models — only run it deliberately, with a clean git tree).

## Docker

`docker-compose.yml` defines only `base`, `api`, and `worker` — Postgres/PostGIS and Redis are **not** compose services anymore, so nothing declares `depends_on` and `DB_HOST`/`CELERY_BROKER_URL` must point at externally running instances. All Dockerfiles live in `docker/`; `docker-compose.yml` and `.dockerignore` stay at the repo root because the build context is the repo root. Images: `docker/Dockerfile` (dev), `docker/Dockerfile.prod.dockerfile` (prod), `docker/Dockerfile.worker`.

**Container registry is GHCR, not Docker Hub.** Images are `ghcr.io/cgiar-agwise/kvuno-api` and `ghcr.io/cgiar-agwise/kvuno-worker`. `docker-build.yml` passes the full `ghcr.io/...` path as `image_name`; the reusable workflow logs in with the built-in `GITHUB_TOKEN` (`permissions: packages: write`), so there are no `DOCKER_USERNAME`/`DOCKER_PASSWORD` secrets. If you add an image, use the full registry path and keep the `permissions` block on every calling job. Build cache tags (`<image>:cache-<branch>`) are also stored in GHCR.

## Conventions

- Commits are Conventional Commits (`feat:`, `fix:`, `docs:`, `chore:`, `test:`), lowercase, short imperative subject. Match that.
- Rate limits come from `RATE_LIMIT_*` env vars in flask-limiter syntax (`"10 per hour"`, not bare ints) — the README's integer table is outdated.
- Logging goes through `app/utils/logging.py::SharedLogger` (loguru) in app code; the standalone scripts use loguru directly.
- Docs live in `README.md` plus `docs/` (DESIGN = UI/UX audit + routes, DATA-FLOW = ingestion/serving/schema, DEPLOYMENT = compose + images). Verify against code before trusting them — several details have drifted. The former SECURITY / IMPLEMENTATION / PERFORMANCE_IMPROVEMENT files were deleted once their task lists were fully complete; security controls are summarised in the "Existing Security Controls" table of git history and enforced in code (`app/auth.py`, `app/rate_limit.py`, `app/utils/downloader.py` SSRF guards).
- **The app version is never hardcoded.** `app/config.py` reads `APP_VERSION`, falling back to `GITHUB_REF_NAME` then `0.0.0-dev`. The Dockerfiles declare `ARG APP_VERSION` in the runtime stage and the reusable workflow passes it as a build arg, so `/health` and the OpenAPI `Info` block report the tag that was built. `version-release.yml` derives the tag from git history via `masgeek/github-tag-action`, which has **no** `version_file` input — the tag is the source of truth, so `pyproject.toml`'s `version` is cosmetic only (the project is `package-mode = false` and is never published).
- `HOUSEKEEPING_ENABLED=false` (default) runs the whole app without Redis/Celery; uploads are stored and reported as "no worker".
- **Rate limiting has a blanket default that also covered static assets.** `app/rate_limit.py` sets `default_limits` (now 600/hour, 5000/day, overridable via `RATE_LIMIT_DEFAULT_HOURLY` / `RATE_LIMIT_DEFAULT_DAILY`) which applies to any route without its own limit. `limiter.exempt(app.send_static_file)` is set in `create_app()` — without it a single page view costs ~5 requests and the old `50 per hour` default was exhausted after ~10 views. If legitimate users see 429s, raise the default, not the per-endpoint limits.
- `PROXY_FIX_HOPS` (default 0) wraps the app in werkzeug `ProxyFix` so `get_remote_address()` returns the real client rather than the reverse proxy. **Leave it 0 unless a trusted proxy sets the forwarded headers** — with it on and no proxy in front, a client can spoof `X-Forwarded-For` and bypass every limit. Set it to the number of proxies in front of the app (1 for a single nginx/Dokploy).
- `RATE_LIMIT_STORAGE` defaults to `memory://`, which is per-process: with multiple gunicorn workers the counters are inconsistent and a client can slip through by landing on another worker. Point it at Redis (already required for Celery) for shared counters. It is applied by the `Limiter` constructor, not after `init_app`.
- The explore table uses **paged navigation on purpose** — do not replace it with infinite scroll. Rationale in `docs/DESIGN.md`.
- **Upload preview is expensive for RDS and bounded by `MAX_FILE_SIZE`.** `pyreadr.read_r()` deserialises the *whole* file, so previewing 5 rows costs the same as a full load; Parquet is cheap by contrast. `app/utils/preview.py` caches the result in a `.preview.json` sidecar (keyed on size+mtime) and returns `elapsed_ms`. The chunked path (`/ui/upload/complete`) enforces `MAX_FILE_SIZE` *during* the merge and returns 413 — previously the cap was client-side only, so one request could fill the disk and then block on a full parse.
- **Uploads convert RDS to Parquet once, at upload time.** Both upload paths call `convert_for_ingestion()`, which writes a `.parquet` alongside and deletes the `.RDS`, so the preview and the worker both read the cheap format instead of re-parsing the whole file. It returns the original path on failure, so a bad file becomes a slow upload rather than a failed one — check the returned `file` value, not the upload name, it may be `.parquet` when `.rds` went in.
- `pandas.read_parquet` has **no `nrows` argument** (it forwards kwargs to `pyarrow.read_table`, which rejects it). `app/utils/preview.py` uses `pyarrow.parquet.ParquetFile` directly: `.schema.names` for columns, `.iter_batches(batch_size=n)` for rows.
- No `package.json`: third-party JS/CSS loads from jsDelivr/unpkg at runtime; the root `node_modules/` is a leftover, not a build input.
- All three app Dockerfiles (`docker/Dockerfile`, `docker/Dockerfile.prod.dockerfile`, `docker/Dockerfile.worker`) start their builder stage with `ARG POETRY_VERSION=2.3.2` + `FROM ghcr.io/masgeek/python-3.14-poetry:${POETRY_VERSION} AS builder`. Poetry itself lives in the shared `docker/Dockerfile.base`, installed with the official installer into `POETRY_HOME=/opt/poetry` and version-checked in the same layer; the app Dockerfiles only run `poetry install`.
- The builder base is `ghcr.io/masgeek/python-3.14-poetry:2.3.2`, built from `docker/Dockerfile.base` — note it lives under the `masgeek` namespace while the app images are `ghcr.io/cgiar-agwise/*`. **CI does not build or push it**; build it locally with `docker compose build base` (optionally `docker push` it). Its `image:` tag in compose must exactly match the `${POETRY_VERSION}` the app Dockerfiles reference, or Docker tries to pull a nonexistent tag.
- **The Poetry version must match the one that generated `poetry.lock`** — different 2.x releases compute a different content-hash for `pyproject.toml` and the build aborts with "pyproject.toml changed significantly". There is no automated check for this, so bumping Poetry means editing **all five** of: `docker/Dockerfile.base` (ARG), the three app Dockerfiles in `docker/` (ARG), and `docker-compose.yml` (`base` service `image:` tag *and* build arg) — then regenerating the lock with that same Poetry and rebuilding the base. Miss one and the failure looks unrelated to Poetry.
- The builder stages copy `pyproject.toml poetry.lock README.md`. The lock is required (otherwise the build re-resolves every time and drifts from CI); `README.md` is required because `pyproject.toml` declares `readme = "README.md"` and Poetry validates it — hence the `!README.md` negation at the end of `.dockerignore`, which must stay *after* the `*.md` rule it overrides.
- The app listens on **port 80** in both images. `run.py` defaults `SERVER_PORT` to 80, and `app/gunicorn_config.py` defaults `BIND_PORT` to 80. Compose maps host `5000` → container `80` and adds `cap_add: [NET_BIND_SERVICE]`, because the containers run as non-root `app` and ports below 1024 are privileged on Linux.
- The prod image really loads that config: `CMD ["gunicorn", "-c", "app/gunicorn_config.py", "wsgi:app"]`. Gunicorn only auto-loads `gunicorn.conf.py` from the working directory, so without `-c` the `BIND_IP`/`BIND_PORT`/`WORKERS`/`THREADS`/`TIMEOUT`/`LOG_LEVEL` env vars in that file are inert. It defaults to `worker_class=gthread` (2 workers x 4 threads) because the SSE endpoint would otherwise exhaust the sync worker pool.
- Dev and prod use **separate** Dockerfiles: `docker/Dockerfile` (CMD `python3 run.py`) and `docker/Dockerfile.prod.dockerfile` (CMD gunicorn, adds HEALTHCHECK). They share `docker/entrypoint.sh`, which applies migrations then `exec`s the image CMD — there is no build or runtime mode variable.
- `.dockerignore` **must stay at the repo root**, not in `docker/` — Docker only reads it from the build-context root, and the context is the repo root. Moving it silently disables every rule, including the `!README.md` negation Poetry needs.
- Known dead code path: `GET /ui/progress/<file_name>` reads a `.progress.json` file nothing writes (progress moved to `services/progress_store.py`), which breaks the jobs detail modal and its Retry button.

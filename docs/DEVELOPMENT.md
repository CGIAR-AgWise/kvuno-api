# Development

Overview, quick start and the other guides: [README.md](../README.md).

Working on the code: where things are, how the processes check their dependencies, the frontend, and running without PostgreSQL or Redis.

## Project Structure

```
kvuno/
├── app/
│   ├── __init__.py           # Application factory (Flask OpenAPI)
│   ├── celery_app.py         # Celery app instance
│   ├── config.py             # App constants and configuration
│   ├── gunicorn_config.py    # Gunicorn server configuration
│   ├── tasks.py              # Celery task definitions
│   ├── auth.py               # require_auth decorator
│   ├── api/
│   │   ├── admin.py          # Account approval + roles (/api/v1/admin, admins only)
│   │   ├── planting_data.py  # Planting data API (public_api + protected_api)
│   │   ├── quality.py        # Data quality endpoints
│   │   ├── upload.py         # File upload API blueprint
│   │   └── user.py           # User auth API blueprint
│   ├── cache.py              # Redis-backed @api_cache / invalidate_cache
│   ├── rate_limit.py         # Flask-Limiter instance (shared)
│   ├── dto/
│   │   ├── auth.py           # Auth request/response DTOs
│   │   ├── planting_recommendation.py # Response DTOs (Pydantic models)
│   │   ├── data_filters.py   # Filter DTOs with validation
│   │   └── upload.py         # Upload request/response DTOs
│   ├── models/
│   │   ├── database_conn.py  # Database connection manager
│   │   └── kvuno.py          # SQLAlchemy ORM models
│   ├── repo/
│   │   ├── planting_recommendation.py # PlantingRecommendation repo
│   │   ├── file_import.py    # FileImport repository
│   │   └── import_conflict.py# ImportConflict repository
│   ├── routes/
│   │   ├── main.py           # JSON /ui/* endpoints, /ui/process, SSE, /health
│   │   └── uploads.py        # tus upload server + upload lifecycle (/ui/uploads/*)
│   ├── services/
│   │   ├── housekeeper.py    # Ingestion pipeline (process_file, load_rds_to_db, CLI)
│   │   ├── mailer.py         # Optional SMTP: activation, rejection, new-registration emails
│   │   ├── uploads.py        # Upload storage, background inspection, column-mapping rules
│   │   ├── progress_store.py # Job progress (Redis primary, DB fallback)
│   │   └── watch_handler.py  # watchdog directory watcher
│   ├── static/               # css/ favicon served by Flask
│   └── utils/
│       ├── logging.py        # setup_logging() / get_logger() (structlog)
│       ├── downloader.py     # RDSDownloader (auth, SSRF guards)
│       ├── rds_to_parquet.py # RDS → Parquet batch converter
│       └── migration_utils.py# Dialect-aware column utilities
│
├── frontend/                 # React SPA (Vite + TypeScript, built with pnpm)
│   ├── src/
│   │   ├── App.tsx           # Client-side routes (lazy-loaded, code-split)
│   │   ├── api.ts            # Typed fetch client for /api and /ui
│   │   ├── pages/            # Login, Register, Jobs, Explore, Upload, Quality, Tokens, AdminUsers
│   │   ├── components/       # Layout (sidebar), AuthShell, StatusBadge, Pagination, RunPanel
│   │   ├── context/          # AuthContext, ToastContext
│   │   └── hooks/            # useJobStream (SSE)
│   ├── Dockerfile            # node build stage → nginx runtime
│   ├── nginx.conf            # Serves the SPA, reverse-proxies /api, /ui, /health
│   └── pnpm-lock.yaml
│
├── alembic/                  # Database migration scripts
│   └── versions/             # Migration versions
├── scripts/
│   ├── run_migrations.py     # Standalone migration runner (deploy step)
│   ├── preflight.py          # Startup dependency check (container entrypoints)
│   └── gen_config_docs.py    # Generates .env.example + the Configuration tables
│
├── logs/                     # Log output directory
├── static/data/              # RDS data files for ingestion (HOUSEKEEPING_DATA_DIR)
│
├── .env.example              # Environment variable template
├── docker-compose.yml        # Multi-service Docker setup (base, api, web, worker)
├── docker/
│   ├── Dockerfile.base           # Shared builder base: Python 3.14 + pinned Poetry
│   ├── Dockerfile                # Dev API image (python3 run.py)
│   ├── Dockerfile.prod.dockerfile# Production API image (Gunicorn)
│   ├── Dockerfile.worker         # Worker image: Celery, or housekeeping via HOUSEKEEPING_MODE
│   ├── entrypoint.sh             # Preflight + migrations, then exec the image CMD
│   └── entrypoint.worker.sh      # Preflight, then Celery or housekeeping.py
├── dev_worker.py             # Dev Celery worker launcher (auto solo pool on Windows)
├── housekeeping.py           # Typer CLI over housekeeper.run() (scan, watch, dry run)
├── model-generator.py        # ORM model code generator
├── pyproject.toml            # Project metadata and dependencies
├── run.py                    # Dev server entry point
└── wsgi.py                   # WSGI entry point for Gunicorn
```

## Startup checks (preflight)

Before doing any work, both the API and the worker verify their dependencies and exit non-zero if a **required** one is unreachable, so the container is restarted rather than booting into a state where every request fails. Implemented in `app/utils/preflight.py` and run from `docker/entrypoint.sh` (API), `docker/entrypoint.worker.sh` (worker), and `run.py`.

| Dependency | API | Worker |
|---|---|---|
| Database | always required | always required |
| Redis | required only when `HOUSEKEEPING_ENABLED=true` | required, unless `HOUSEKEEPING_MODE` is set |
| Data directory writable | reported, never blocks | reported, never blocks |

Redis is optional for the API because with housekeeping off, progress is stored in PostgreSQL and nothing is enqueued — that is a supported configuration. A Celery worker with no broker is useless, so the worker requires Redis — except in `HOUSEKEEPING_MODE` (`oneshot`/`watch`/`dryrun`), where the image runs `housekeeping.py`, which imports straight into the database. An invalid setting (an unknown `HOUSEKEEPING_MODE` included) fails the preflight at once, without retrying.

The `PREFLIGHT_*` settings (retries, delay, timeout, opt-out) are listed under [Configuration](CONFIGURATION.md).

`run.py` is the one exception to fail-fast: with `FLASK_DEBUG=true` it logs a warning and starts anyway, so local development is not blocked by a database or broker that is not running. Containers still refuse to start, because the entrypoint check is independent of it.

## Frontend Architecture

The UI is a standalone **React 19 + TypeScript SPA** in `frontend/`, built with **Vite** and **pnpm**, and served by **nginx** — the Flask app renders no pages. `frontend/nginx.conf` serves the built bundle and reverse-proxies `/api`, `/ui`, `/health`, and `/api-docs` (Swagger UI; spec at `/api-docs/openapi.json`) to the `api` service, so production is **same-origin**: no CORS preflight, and the bearer token never crosses origins. The remaining `/ui/*` JSON endpoints (tus uploads under `/ui/uploads`, `/ui/process`, `/ui/columns`, jobs SSE) still run on Flask and are reached through that proxy. Routes are code-split with `React.lazy`, so the heavy pages (Explore pulls in Leaflet, markercluster, and the heat layer) do not weigh down the initial bundle. `vite dev` proxies the same paths to the API, so local development is same-origin too. The target defaults to `http://127.0.0.1:5000` — the compose `api` service publishes host `5000` → container `80` — and is overridable with `VITE_DEV_PROXY_TARGET` (see `frontend/.env.example`).

| Layer | Value |
|---|---|
| Framework | React 19 + react-router 6 + TypeScript 5.7 |
| Build tool | Vite 8 (`tsc -b && vite build`), package manager pnpm |
| Page routes | `/login`, `/register`, `/upload`, `/jobs`, `/explore`, `/quality`, `/tokens`, `/admin/users` (administrators) |
| Runtime | nginx (SPA + reverse proxy) |
| Dev server | `pnpm dev` in `frontend/` — Vite on `:5173`, proxying to the API at `VITE_DEV_PROXY_TARGET` (default `http://127.0.0.1:5000`) |
| Image | `ghcr.io/cgiar-agwise/kvuno-web` (`frontend/Dockerfile`, node build → nginx runtime) |

## Local Development without Postgres

The API can run on SQLite for UI and auth work — no Postgres needed:

```bash
# Edit .env
DB_DRIVER=sqlite
DB_NAME=kvuno.db
HOUSEKEEPING_ENABLED=false

# Create the schema (works end-to-end on SQLite), then run
alembic upgrade head
python run.py
```

Ingestion and the spatial endpoints still need PostgreSQL/PostGIS: the batch insert uses Postgres `ON CONFLICT`, and on SQLite `coordinates` is a plain text column with no `ST_*` functions.

## Local Development without Redis / Celery

When `HOUSEKEEPING_ENABLED=false` (default), the app runs entirely without Redis:

- The API serves data normally
- Files submitted from the Upload page are received and inspected in the API process and imported in a separate `ingest_runner` process it starts (never inside a gunicorn worker), with progress on the Jobs page
- The startup scan imports what is in the data folder, in one API process
- Only `POST /api/v1/data/upload` and Jobs-page retries of plain data-folder files need the worker: without it they are saved and reported as such
- Set `HOUSEKEEPING_ENABLED=true` (and run the worker, with Redis) to hand those to Celery

See [docs/DEPLOYMENT.md](DEPLOYMENT.md) for full details on Compose configuration, environment variables, running commands inside containers, and troubleshooting.

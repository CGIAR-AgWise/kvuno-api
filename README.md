# KVuno API

A Flask-based REST API for processing agricultural RDS (R Data Serialization) files containing crop planting data. It extracts optimized sowing dates, crop varieties, and geographic coordinates from RDS files, loads them into a database (SQLite/MySQL/PostgreSQL), and serves the data through a paginated, filterable API endpoint. Duplicate files are tracked via checksums to avoid re-imports.

Built for the [AgWISE-EiA](https://agwise.cgiar.org) initiative (Alliance for a Green Revolution in Africa / Excellence in Agronomy).

## Features

- **RDS File Ingestion** — Reads `.RDS` files using `pyreadr`, processes data in chunks with batch inserts
- **Resumable Uploads** — The SPA uploads over the [tus](https://tus.io) protocol; an interrupted upload resumes from the last byte, and the file is inspected (RDS → Parquet, preview, checksum, suggested column mapping) in the background
- **Deduplication** — SHA-256 checksums track processed files to prevent duplicate imports
- **Background Processing** — Celery + Redis worker for async file ingestion; separately deployable
- **REST API** — OpenAPI 3.0 compliant, auto-generated docs at `/api-docs`
- **Paginated & Filterable Queries** — Filter by coordinates + radius, country, province, variety, season type, optimal date, planting option
- **Spatial Data Support** — PostGIS `POINT` geometry (SRID 4326) with `ST_DWithin` radius filtering in metres (geography cast)
- **Database** — PostgreSQL/PostGIS. SQLite can run the migrations and serve the API for UI work, but ingestion (Postgres `ON CONFLICT`) and the spatial queries need Postgres
- **Health Check** — `GET /health` returns app name, build version, and database status
- **Dockerized** — Dev, production, and worker Dockerfiles with docker-compose; images published to GitHub Container Registry
- **Database Migrations** — Alembic-managed schema evolution
- **Token Authentication** — Sanctum-style opaque `{id}|{secret}` tokens (SHA-256 hashed at rest) with BCrypt password hashing; no signing key, and revoking a token is a single row delete
- **React Frontend** — Standalone Vite + React 18 + TypeScript SPA in `frontend/`, built with pnpm and served by nginx
- **Login & Registration UI** — SPA routes `/login` and `/register`; self-registration is limited to the email domains in `REGISTRATION_ALLOWED_DOMAINS` and is **disabled when that is unset**
- **Token Management** — List and revoke tokens at the SPA route `/tokens`
- **CORS** — Cross-origin support enabled globally
- **Request Rate Limiting** — Flask-Limiter available for route protection

## Tech Stack

 | Component | Technology |
|---|---|---|
| Framework | Flask (via flask-openapi3) |
| ORM | SQLAlchemy (flask-sqlalchemy) |
| Database | SQLite / PostgreSQL (psycopg 3, `psycopg[binary]`) |
| Migrations | Alembic |
| Spatial | GeoAlchemy2 / PostGIS |
| RDS Parsing | pyreadr + pandas |
| Authentication | Opaque bearer tokens (`{id}|{secret}`, SHA-256 at rest) + bcrypt |
| Background Tasks | Celery + Redis (Kombu transport) |
| Logging | loguru |
| Serving | Waitress (dev) / Gunicorn (prod) |
| Frontend | React 18 + TypeScript, Vite 6, pnpm, react-router 6; served by nginx |
| Containerization | Docker + docker-compose |
| CI/CD | GitHub Actions |

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
│   │   ├── uploads.py        # Upload storage, background inspection, column-mapping rules
│   │   ├── progress_store.py # Job progress (Redis primary, DB fallback)
│   │   └── watch_handler.py  # watchdog directory watcher
│   ├── static/               # css/ favicon served by Flask
│   └── utils/
│       ├── logging.py        # SharedLogger (loguru wrapper)
│       ├── downloader.py     # RDSDownloader (auth, SSRF guards)
│       ├── rds_to_parquet.py # RDS → Parquet batch converter
│       └── migration_utils.py# Dialect-aware column utilities
│
├── frontend/                 # React SPA (Vite + TypeScript, built with pnpm)
│   ├── src/
│   │   ├── App.tsx           # Client-side routes (lazy-loaded, code-split)
│   │   ├── api.ts            # Typed fetch client for /api and /ui
│   │   ├── pages/            # Login, Register, Jobs, Explore, Upload, Quality, Tokens
│   │   ├── components/       # Layout, Pagination
│   │   ├── context/          # AuthContext, ToastContext
│   │   └── hooks/            # useJobStream (SSE)
│   ├── Dockerfile            # node build stage → nginx runtime
│   ├── nginx.conf            # Serves the SPA, reverse-proxies /api, /ui, /health
│   └── pnpm-lock.yaml
│
├── alembic/                  # Database migration scripts
│   └── versions/             # Migration versions
├── scripts/
│   └── run_migrations.py     # Standalone migration runner (deploy step)
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
│   ├── Dockerfile.worker         # Celery worker Docker image
│   └── entrypoint.sh             # Migrations, then exec the image CMD
├── dev_worker.py             # Dev Celery worker launcher (auto solo pool on Windows)
├── housekeeping.py           # Thin CLI wrapper over app/services/housekeeper.py
├── model-generator.py        # ORM model code generator
├── pyproject.toml            # Project metadata and dependencies
├── run.py                    # Dev server entry point
└── wsgi.py                   # WSGI entry point for Gunicorn
```

## Installation

### Prerequisites

- Python 3.13+
- Poetry (`pip install poetry`)

### Setup

```bash
# Clone the repository
git clone git@github.com:CGIAR-AgWise/kvuno-api.git
cd kvuno-api

# Install dependencies
poetry install

# Copy environment variables
cp .env.example .env
```

Edit `.env` with your database connection:

```env
# Required
DB_USER=postgres
DB_PASSWORD=postgres
DB_NAME=agwise_api

# Full URL takes priority over individual parts
# DB_URL="postgresql://user:pass@host:5432/kvuno"

# Individual parts
DB_DRIVER=postgresql
DB_HOST=127.0.0.1
DB_PORT=5432

# For SQLite:
# DB_DRIVER=sqlite
# DB_NAME=kvuno.db
```

### Make targets

A root `Makefile` wraps the common commands; `make help` (or plain `make`) lists every target. A few you will use most:

```bash
make install     # poetry install + pnpm install in frontend/
make check       # what CI checks: lint, tests, SPA build
make dev         # API dev server (python run.py)
make migration MSG="add uploads index"   # autogenerate, then edit it by hand
```

### Database Migrations

Migrations are applied by the container entrypoint (`docker/entrypoint.sh`) before the API starts. Locally, run them explicitly:

```bash
# Apply migrations
alembic upgrade head

# Or use the standalone runner (same thing, builds a minimal app context)
python scripts/run_migrations.py

# Create a new migration (after model changes)
alembic revision --autogenerate -m "description"
```

### Running the Application (Development)

```bash
# API server only (no background processing)
python run.py            # or: poetry run dev
```

```bash
# With background processing (requires Redis + Celery worker)
python dev_worker.py     # auto-selects --pool solo on Windows, prefork elsewhere
```

The API will be available at `http://localhost:5000` (host) and the Swagger UI at `http://localhost:5000/api-docs`. The container listens on port 80 internally; compose maps host 5000 to it.

The API container serves **no HTML** — the pages live in the `frontend/` SPA. Start the Vite dev server alongside it (see [Frontend Architecture](#frontend-architecture)); the Flask container still owns the JSON endpoints the SPA calls.

Set `HOUSEKEEPING_ENABLED=false` (default) to skip the 2-second probe for a Celery worker.

Set `HOUSEKEEPING_ENABLED=true` to enqueue file-uploads to the Celery worker automatically.

### Startup checks (preflight)

Before doing any work, both the API and the worker verify their dependencies and exit non-zero if a **required** one is unreachable, so the container is restarted rather than booting into a state where every request fails. Implemented in `app/utils/preflight.py` and run from `docker/entrypoint.sh` (API), `docker/entrypoint.worker.sh` (worker), and `run.py`.

| Dependency | API | Worker |
|---|---|---|
| Database | always required | always required |
| Redis | required only when `HOUSEKEEPING_ENABLED=true` | always required |

Redis is optional for the API because with housekeeping off, progress is stored in PostgreSQL and nothing is enqueued — that is a supported configuration. A Celery worker with no broker is useless under any configuration, so it always requires Redis.

| Variable | Default | Purpose |
|---|---|---|
| `PREFLIGHT_ENABLED` | `true` | Set `false` to skip the check entirely |
| `PREFLIGHT_RETRIES` | `30` | Attempts before giving up |
| `PREFLIGHT_DELAY_SECONDS` | `2` | Wait between attempts (60s total tolerance) |
| `PREFLIGHT_TIMEOUT_SECONDS` | `3` | Per-probe connect timeout |

`run.py` is the one exception to fail-fast: with `FLASK_DEBUG=true` it logs a warning and starts anyway, so local development is not blocked by a database or broker that is not running. Containers still refuse to start, because the entrypoint check is independent of it.

---

### Frontend Architecture

The UI is a standalone **React 18 + TypeScript SPA** in `frontend/`, built with **Vite** and **pnpm**, and served by **nginx** — the Flask app renders no pages. `frontend/nginx.conf` serves the built bundle and reverse-proxies `/api`, `/ui`, `/health`, and `/api-docs` (Swagger UI; spec at `/api-docs/openapi.json`) to the `api` service, so production is **same-origin**: no CORS preflight, and the bearer token never crosses origins. The remaining `/ui/*` JSON endpoints (tus uploads under `/ui/uploads`, `/ui/process`, `/ui/columns`, jobs SSE) still run on Flask and are reached through that proxy. Routes are code-split with `React.lazy`, so the heavy pages (Explore pulls in Leaflet, markercluster, and the heat layer) do not weigh down the initial bundle. `vite dev` proxies the same paths to the API, so local development is same-origin too. The target defaults to `http://127.0.0.1:5000` — the compose `api` service publishes host `5000` → container `80` — and is overridable with `VITE_DEV_PROXY_TARGET` (see `frontend/.env.example`).

| Layer | Value |
|---|---|
| Framework | React 18 + react-router 6 + TypeScript 5.7 |
| Build tool | Vite 6 (`tsc -b && vite build`), package manager pnpm |
| Page routes | `/login`, `/register`, `/jobs`, `/explore`, `/upload`, `/quality`, `/tokens` |
| Runtime | nginx (SPA + reverse proxy) |
| Dev server | `pnpm dev` in `frontend/` — Vite on `:5173`, proxying to the API at `VITE_DEV_PROXY_TARGET` (default `http://127.0.0.1:5000`) |
| Image | `ghcr.io/cgiar-agwise/kvuno-web` (`frontend/Dockerfile`, node build → nginx runtime) |

### Docker Deployment

`docker-compose.yml` defines `base` (build-only, never started), `api` (production image, Gunicorn), `api-dev` (Flask dev server, only with `--profile dev`, host `127.0.0.1:5002`), `web` (nginx serving the React SPA on host `127.0.0.1:5001`), and `worker` (Celery). The app listens on port 80 inside the container and compose maps host `127.0.0.1:5000` to it. Open the UI at `http://localhost:5001`, not at the API port. All published ports bind to loopback only; there is no `WEB_PORT` variable — change the mapping in the compose file or an override.

Database migrations are applied by the `api` container's entrypoint when `RUN_MIGRATIONS=true` — the entrypoint's own default — but `docker-compose.yml` overrides it to `${RUN_MIGRATIONS:-false}`, so under compose either set it or run `docker compose exec api python scripts/run_migrations.py` after every upgrade. Keep it `false` if you run more than one API replica so they don't race to migrate the same schema. The `worker` deliberately does **not** migrate: it is only handed work by the already-running API.

> **Set `REGISTRATION_ALLOWED_DOMAINS`** (e.g. `cgiar.org,iita.org`) on the `api`, or nobody can sign up: unset or empty disables `POST /api/v1/users/register` (`403`). Existing accounts and login are unaffected.

> PostgreSQL/PostGIS and Redis are **not** compose services — they are expected to run elsewhere. Point `DB_HOST` and `CELERY_BROKER_URL` at whatever is reachable, or add them back via a `docker-compose.override.yml`.

```bash
# Build the shared builder base FIRST — the app images FROM it
docker compose build base

# Build and start
docker compose up --build -d

# Apply migrations (compose defaults RUN_MIGRATIONS to false)
docker compose exec api python scripts/run_migrations.py

# Verify
curl http://localhost:5000/health
```

> `base` is never started; it exists so the three application images share one pinned Poetry install. Skipping `docker compose build base` makes them fail trying to pull it from GHCR.

### Docker — Individual Services

```bash
# API only (no background processing)
docker compose up -d api

# API + worker
docker compose up -d api worker

# SPA only (API + web), no background processing
docker compose up -d api web
```

### Container Images (GHCR)

Images are published to GitHub Container Registry, not Docker Hub:

| Trigger | Images | Tags pushed |
|---|---|---|
| push to `develop`, after `UnitTests` passes | api (dev server), worker, web | `:develop`, `:sha-<short>` |
| push to `main`, after `UnitTests` passes | api (gunicorn), worker, web | `:production`, `:sha-<short>` |
| release tag (created by `version-release.yml` on each `main` push) | api (gunicorn), worker, web | `:latest`, `:<tag>`, `:sha-<short>` |

All three images (`ghcr.io/cgiar-agwise/kvuno-api`, `-worker`, `-web`) are built on every path. `:latest` is the latest **release**; `:sha-<short>` is immutable, for rollbacks. Pull requests build every Dockerfile without pushing, so a broken image fails the PR. Details and the rules the pipeline depends on: [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md#9-container-registry-ghcr).
CI publishes with the built-in `GITHUB_TOKEN` (`permissions: packages: write`) — no `DOCKER_USERNAME` / `DOCKER_PASSWORD` secrets are needed. To pull on a server, authenticate with a classic PAT that has `read:packages`:

```bash
echo "$GHCR_TOKEN" | docker login ghcr.io -u <github-username> --password-stdin
```

Packages are private by default; set them to public in the org settings if you want anonymous pulls.

### Base Image (GHCR)

The builder base is `ghcr.io/masgeek/python-3.14-poetry:2.3.2` — `python:3.14-slim` plus a pinned Poetry. CI does not build or push it; build it locally with `docker compose build base`, and `docker push` it if you want to share it with other projects.

The Poetry version is the **tag**, not part of the name, so a Poetry bump publishes a new tag on the same package rather than creating a new one.

### Upgrading Poetry

`poetry.lock` is only valid for the Poetry release that generated it: different 2.x releases compute a different content-hash for `pyproject.toml`, and the build fails with *"pyproject.toml changed significantly"*. There is no automated check, so a bump must touch five places in one commit — `ARG POETRY_VERSION` in `docker/Dockerfile.base` and in all three application Dockerfiles under `docker/`, plus the `base` service `image:` tag **and** `POETRY_VERSION:` build arg in `docker-compose.yml`. Then regenerate the lock with that same Poetry and rebuild the base image. See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md#5-image-variants).

### Local Development without Postgres

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

### Local Development without Redis / Celery

When `HOUSEKEEPING_ENABLED=false` (default), the app runs entirely without Redis:

- The API serves data normally
- File uploads are still received and inspected (that runs in the API process); submitting saves the mapping and leaves the upload `ready` with a message saying no worker is configured
- Set `HOUSEKEEPING_ENABLED=true` to enable background enqueuing (requires Redis accessible)

See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) for full details on Compose configuration, environment variables, running commands inside containers, and troubleshooting.

## Usage

### File Processing (Background Worker)

File ingestion is handled by a Celery worker. When `HOUSEKEEPING_ENABLED=true`, the `/api/v1/data/upload` endpoint enqueues a task; the Celery worker processes it asynchronously.

Redis is **not** a compose service; run it externally and point `CELERY_BROKER_URL` / `CELERY_RESULT_BACKEND` at it.

```bash
# Start Redis (standalone Docker container, not part of docker-compose.yml)
docker run -d --name kvuno-redis -p 6379:6379 redis:7

# Start the Celery worker (Native — Linux)
celery -A app.celery_app worker --loglevel=info

# Start the Celery worker (Native — Windows, use solo pool)
celery -A app.celery_app worker --loglevel=info --pool solo

# Start the Celery worker (Docker)
docker compose up --build -d worker
```

Tasks are acked late and re-queued if the worker dies (`task_reject_on_worker_lost`), so `CELERY_VISIBILITY_TIMEOUT` (default 6 h) must exceed your longest single-file ingest or Redis hands a still-running task to a second worker. A per-file `<file>.ingest.lock` makes any such duplicate exit quietly. A database outage mid-file re-raises so Celery retries from the stored offset; a bad file is recorded as `error` at its last position and is not retried. Each submission (and each startup scan) is its own processing run, so the Jobs page keeps one history row per attempt.

### Uploading Files (SPA)

The Upload page sends files with the in-house tus client (`frontend/src/lib/tus.ts`) to a tus 1.0 server (core + creation) in `app/routes/uploads.py`. The server issues the upload id and appends bytes at an offset, so a dropped connection, closed tab, or API restart resumes from the last byte. Each upload is a row in the `uploads` table and moves through:

```
receiving → received → inspecting → ready | failed → submitted
```

1. **Receive** — bytes land in `HOUSEKEEPING_DATA_DIR/uploads/<id>/data.part`. The size cap (`MAX_FILE_SIZE_MB`) and extension are checked at creation, before any bytes arrive
2. **Inspect** — on a background thread in the API process (no Celery needed): RDS → Parquet conversion, preview rows, SHA-256, duplicate lookup against `file_imports`, and a suggested column mapping
3. **Map** — the page shows the server's suggested mapping, which is exactly what will be sent. `lat` and `lon` must be mapped, and each database column at most once
4. **Submit** — `POST /ui/uploads/<id>/process` saves the mapping and enqueues ingestion. The worker uses that mapping as-is (not merged over `RDS_COLUMN_MAP`). If the broker is unreachable the API returns `503` and the upload goes back to `ready`

Uploads live under `uploads/` rather than the data directory root on purpose: the startup scan (`load_rds_to_db`) ingests every file in the root with the default mapping. Unsubmitted uploads idle longer than `UPLOAD_ABANDON_AGE` are deleted at worker boot. The tus `HEAD`/`PATCH` and the status `GET` are exempt from the default rate limit, since a large file is many requests.

### Legacy Housekeeping Script

The `housekeeping.py` script processes files directly (without Celery) — useful for one-off bulk imports:

```bash
# Process all files in static/data/
python housekeeping.py

# Custom directory
python housekeeping.py /path/to/data

# Dry-run
python housekeeping.py --dry-run

# Watch mode
python housekeeping.py --watch
```

What happens during a run:
1. **Health check** — `SELECT 1` confirms the database is reachable
2. **Remote download** — Downloads files from `REMOTE_RDS_URLS` (if configured)
3. **Deduplication** — SHA-256 checksum lookup in `file_imports` table; fully imported files are skipped
4. **Resumable processing** — Files with a stored `offset` resume from that row; incremental checkpoints commit every `checkpoint_interval` batches
5. **Batch insert** — Records are inserted in savepoint-protected batches; individual batch failures are logged and skipped
6. **Graceful shutdown** — `Ctrl+C` (or `SIGTERM`) commits completed batches and persists the offset for later resumption; a second `Ctrl+C` force-quits immediately. These handlers are installed by the CLI only — gunicorn and Celery keep their own signal handling
7. **Telemetry** — Structured JSON events (`housekeeping.start/end`, `file.download_start/end`, `file.processing_start/end`) are written to stderr for monitoring ingestion

Remote files are configured via environment variables:

| Variable | Description |
|---|---|
| `REMOTE_RDS_URLS` | Semicolon-delimited URLs of remote `.RDS` files to download |
| `REMOTE_RDS_TOKEN` | Bearer token for authenticated downloads |
| `REMOTE_RDS_COOKIES` | Cookie header (e.g. `session=abc; token=xyz`) |
| `REMOTE_RDS_HEADERS` | Custom headers as `key: value; key2: value2` |

Downloads are SSRF-guarded (`app/utils/downloader.py`): HTTPS only unless `REMOTE_RDS_ALLOW_HTTP=true`; private, loopback, link-local and metadata addresses refused; redirects followed by hand (max 5), each hop re-checked, with the bearer token and `REMOTE_RDS_COOKIES` dropped when a redirect changes host; optional `REMOTE_RDS_ALLOWED_DOMAINS` (exact host or subdomain); `REMOTE_RDS_MAX_BYTES` (default 1 GiB) per file; and a post-connect peer-IP check against DNS rebinding (best effort, skipped behind an HTTP proxy).

Convert large `.RDS` files to `.parquet` for faster processing:

```bash
python -c "from app.utils.rds_to_parquet import batch_convert; batch_convert('static/data/')"
```

### Authentication

All `/ui/*` JSON endpoints and most API routes require authentication. Obtain a token via:

```bash
curl -X POST http://localhost:5000/api/v1/users/login \
  -H "Content-Type: application/json" \
  -d '{"username": "johndoe", "password": "securePass123"}'
# Returns: {"msg": "login success", "access_token": "1|a1b2c3d4e5f6..."}
```

Use the token in subsequent requests:

```bash
curl http://localhost:5000/api/v1/planting-data/ \
  -H "Authorization: Bearer 1|a1b2c3d4e5f6..."
```

Or sign in through the SPA at `/login` — the token is stored as a cookie for browser navigation (`Secure` when the page is served over HTTPS). An unauthenticated browser hitting a protected endpoint is redirected to `SPA_LOGIN_PATH` (default `/login`); API clients still get a `401` JSON body. In the SPA, a `401` from any API call (an expired or revoked token) signs the user out and returns them to `/login?next=<page>`.

Registration (`POST /api/v1/users/register`) is open only to email addresses whose domain is listed in `REGISTRATION_ALLOWED_DOMAINS` — exact, case-insensitive match, so `sub.cgiar.org` is not covered by `cgiar.org`. When the variable is unset or empty, registration is **disabled** (`403`). Passwords longer than 72 UTF-8 bytes (bcrypt's limit) are rejected at registration (`422`); a login with one is simply `401`.

Error responses never carry exception text: a failing endpoint returns a generic `500` body and logs the exception server-side. Request validation errors (bad query/body parameters, including an unknown `sort_col`/`sort_dir`) are `422`, flask-openapi3's validation status.

### API Endpoints

> Rows marked *SPA route* are client-side React routes served by nginx — Flask has no HTML endpoints. In production nginx reverse-proxies `/api`, `/ui`, and `/health` to the `api` service, so the browser sees a single origin.

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET` | `/` | — | Redirects to `SPA_ROOT_URL` (default `/`) |
| `GET` | `/health` | — | Health check with database status |
| `GET` | `/login`, `/register` | — | SPA routes — login and registration pages (nginx) |
| `GET` | `/jobs` | Required | SPA route — job list (nginx) |
| `GET` | `/explore` | Required | SPA route — map explorer (nginx) |
| `GET` | `/upload` | Required | SPA route — file upload (nginx) |
| `GET` | `/quality` | Required | SPA route — data quality dashboard (nginx) |
| `GET` | `/tokens` | Required | SPA route — token management (nginx) |
| `GET` | `/ui/jobs/data` | Required | JSON: job list |
| `GET` | `/ui/jobs/events` | Required | SSE: live job updates (Redis pub/sub, 3s poll fallback) |
| `POST` | `/ui/uploads/tus` | Required | tus create — `Upload-Length` + `Upload-Metadata filename`; size and extension checked before any bytes (`413`/`415`) |
| `HEAD` | `/ui/uploads/tus/<id>` | Required | tus offset probe — `Upload-Offset` to resume from |
| `PATCH` | `/ui/uploads/tus/<id>` | Required | tus append at `Upload-Offset` (`409` offset mismatch, `423` another request holds the upload) |
| `GET` | `/ui/uploads` | Required | The caller's unfinished uploads (survives a page reload) |
| `GET` | `/ui/uploads/<id>` | Required | Upload status, preview, duplicate info, suggested mapping |
| `POST` | `/ui/uploads/<id>/process` | Required | Validate + save the column mapping, enqueue ingestion |
| `DELETE` | `/ui/uploads/<id>` | Required | Discard an upload that has not been submitted |
| `POST` | `/ui/process` | Required | (Re)process a stored file — Jobs page Retry; omit `column_map` to re-use the saved one |
| `GET` | `/ui/progress/<file_name>` | Required | One job's progress record (same shape as a `/ui/jobs/data` item), or `status: unknown` |
| `GET` | `/ui/columns` | Required | Mappable DB columns, aliases, and `max_file_size` |
| `POST` | `/api/v1/users/register` | — | Register a new account (email domain must be in `REGISTRATION_ALLOWED_DOMAINS`, else `403`) |
| `POST` | `/api/v1/users/login` | — | Authenticate and get a token |
| `POST` | `/api/v1/users/logout` | Required | Revoke the current token (bearer header or `token` cookie) |
| `POST` | `/api/v1/users/tokens` | Required | Create a new API token |
| `GET` | `/api/v1/users/tokens` | Required | List active tokens (paginated) |
| `DELETE` | `/api/v1/users/tokens/<id>` | Required | Revoke a specific token |
| `POST` | `/api/v1/data/upload` | Required | Upload an RDS/parquet file |
| `GET` | `/api/v1/planting-data/` | **Public** | Paginated, filterable crop data |
| `GET` | `/api/v1/planting-data/filters` | Required | Distinct filter values (paginated per column) |
| `GET` | `/api/v1/planting-data/coordinates` | Required | Map coordinates (paginated) |
| `GET` | `/api/v1/planting-data/clusters` | Required | Spatial clusters (paginated by cluster: `pages` from `total_clusters`; bad `zoom`/bounds → `400`) |
| `GET` | `/api/v1/planting-data/export` | Required | Export data (CSV/JSON, paginated; `0` values are kept) |
| `GET` | `/api/v1/quality/stats` | Required | Quality statistics |
| `GET` | `/api/v1/quality/conflicts` | Required | Import conflicts (paginated) |

### Pagination

Every collection endpoint is paginated server-side. `page` defaults to `1` and
`per_page` to `100`, clamped to a maximum of `500` (`MAX_PER_PAGE` in
`app/dto/pagination.py`). `per_page` is clamped rather than rejected, and `page`
is floored at `1` so a malformed value cannot produce a negative offset.

Responses carry `total`, `pages`, `current_page`, and `per_page` alongside their
rows. `page` is 1-based. `/planting-data/filters` is the one exception to a
single row axis: its columns are paginated independently, and `totals` / `pages`
are reported per column so a client can tell when each is exhausted.

`/quality/stats` returns aggregates rather than a record list, so it has no
pagination; `register`, `login`, `logout`, `upload`, and token creation return
single objects.

> `GET /api/v1/planting-data/` is intentionally **public** (rate-limited via `RATE_LIMIT_DATA`); the supporting endpoints under the same prefix require a token.

### Query Parameters for `/api/v1/planting-data/`

| Parameter | Type | Description |
|---|---|---|
| `page` | int | Page number (default: 1) |
| `per_page` | int | Items per page (default: 100, max 500) |
| `coordinates` | string | Center point for radius search (`lon,lat`) |
| `radius` | float | Search radius in metres (requires `coordinates`; measured on the geography, not in degrees) |
| `country` | string | Country name (partial ILIKE match) |
| `province` | string | Province name (partial ILIKE match) |
| `variety` | string | Crop variety exact match |
| `season_type` | string | Season type exact match |
| `opt_date` | string | Optimal sowing date (`YYYY-MM-DD`) |
| `planting_option` | string | Planting option exact match |
| `sort_col` / `sort_dir` | string | Server-side sorting used by the explore UI. `sort_col` must be one of `id`, `check_sum`, `country`, `province`, `lon`, `lat`, `variety`, `season_type`, `opt_date`, `planting_option`, `created_at`, `updated_at`; `sort_dir` `asc`/`desc`. Anything else is `422` |

### Response Format

```json
{
  "data": [
    {
      "id": 1,
      "lat": -17.85,
      "lon": 25.92,
      "country": "Zambia",
      "province": "Southern",
      "variety": "Soybean",
      "season_type": "Main",
      "opt_date": "2024-11-15",
      "planting_option": 1,
      "check_sum": "a1b2c3d4e5f6...",
      "coordinates": "POINT(25.92 -17.85)"
    }
  ],
  "total": 42,
  "pages": 5,
  "current_page": 1,
  "per_page": 10
}
```

## Configuration

Key environment variables (see `.env.example`):

| Variable | Description | Default | Required |
|---|---|---|---|
| `DB_URL` | Full database connection string (overrides DB_*) | — | |
| `DB_DRIVER` | Database driver | `postgresql` | |
| `DB_HOST` | Database host | `127.0.0.1` | |
| `DB_PORT` | Database port | `5432` | |
| `DB_USER` | Database user | — | **Yes** (unless `DB_URL`) |
| `DB_PASSWORD` | Database password | — | **Yes** (unless `DB_URL`) |
| `DB_NAME` | Database name | `kvuno.db` if sqlite | **Yes** (unless `DB_URL`) |
| `TOKEN_TTL_DAYS` | Token expiration in days (`0` = never) | `0` | |
| `REGISTRATION_ALLOWED_DOMAINS` | Comma-separated email domains allowed to self-register (exact, case-insensitive; no subdomain matching). **Unset/empty disables registration** (`403`) | — (disabled) | To allow sign-up |
| `RATE_LIMIT_REGISTER` | flask-limiter limit string | `10 per hour` | |
| `RATE_LIMIT_LOGIN` | flask-limiter limit string | `20 per hour` | |
| `RATE_LIMIT_UPLOAD` | flask-limiter limit string | `10 per hour` | |
| `RATE_LIMIT_DATA` | flask-limiter limit string | `120 per minute` | |
| `RATE_LIMIT_COORDINATES_BULK` | flask-limiter limit string (bulk coordinates endpoint) | `20 per minute` | |
| `RATE_LIMIT_DEFAULT_HOURLY` | Blanket limit for routes with no explicit limit — a bare **count**; the code appends `per hour` | `600` (= `600 per hour`) | |
| `RATE_LIMIT_DEFAULT_DAILY` | Same, daily — a bare count; the code appends `per day` | `5000` (= `5000 per day`) | |
| `PROXY_FIX_HOPS` | Number of trusted proxies in front of the app; enables real client IPs. Set `0` if nothing proxies the app, or `X-Forwarded-For` can be forged | `1` | |
| `RATE_LIMIT_STORAGE` | Rate-limit backend URI | `memory://` | |
| `CORS_ORIGINS` | Comma-separated allowed CORS origins | `http://127.0.0.1:5000,http://localhost:5173,http://127.0.0.1:5173` | |
| `SPA_ROOT_URL` | Target of the `GET /` redirect | `/` | |
| `SPA_LOGIN_PATH` | Where `require_auth` sends an unauthenticated browser | `/login` | |
| `FLASK_DEBUG` | Enable debug mode | `false` | |
| `SERVER_HOST` | Bind address | `0.0.0.0` | |
| `SERVER_PORT` | Bind port for the dev server | `80` | |
| `BIND_PORT` | Gunicorn bind port (`app/gunicorn_config.py`) | `80` | |
| `BIND_IP` | Gunicorn bind address | `0.0.0.0` | |
| `WORKER_CLASS` | Gunicorn worker class — `gthread` so SSE doesn't exhaust workers | `gthread` | |
| `WORKERS` | Gunicorn worker processes | `2` | |
| `THREADS` | Threads per worker | `4` | |
| `TIMEOUT` | Request timeout (s) — raised from the 30s default for large uploads | `120` | |
| `ACCESSLOG` / `ERRORLOG` | Gunicorn log targets; `-` means stdout/stderr | `-` | |
| `LOG_LEVEL` | Log level for both the app logger and Gunicorn | `INFO` | |
| `DEBUG_DB` | Echo SQL statements | `false` | |
| `SERVER_URL_PROD` | Production server URL | `https://kvuno.agwise.org` | |
| `MAX_FILE_SIZE_MB` | Upload size cap, enforced at tus creation before any bytes are accepted | `20` | |
| `CLEANUP_AGE` | Age after which completed jobs' files are purged at worker boot (`1d`, `12h`, …) | `1d` | |
| `UPLOAD_ABANDON_AGE` | Idle age after which an unsubmitted upload is deleted at worker boot (same format) | `7d` | |
| `UPLOAD_INSPECT_STALE_SECONDS` | An upload stuck in `inspecting` this long is re-claimed on the next status poll | `180` | |
| `HOUSEKEEPING_ENABLED` | Enable Celery background processing | `false` | |
| `HOUSEKEEPING_DATA_DIR` | Directory watched for uploads | `static/data` | |
| `HOUSEKEEPING_BATCH_SIZE` | Rows per batch insert | `2000` | |
| `HOUSEKEEPING_CHUNK_SIZE` | Rows per in-memory chunk | `5000` | |
| `HOUSEKEEPING_CHECKPOINT_INTERVAL` | Batches between offset commits | `50` | |
| `HOUSEKEEPING_MAX_WORKERS` | Max concurrent file-processing subtasks | `1` | |
| `RDS_COLUMN_MAP` | JSON map of RDS column → ORM field | built-in defaults | |
| `CELERY_BROKER_URL` | Redis URL for Celery broker | `redis://localhost:6379/0` | |
| `CELERY_RESULT_BACKEND` | Redis URL for Celery results | `redis://localhost:6379/0` | |
| `CELERY_TASK_DEFAULT_QUEUE` | Queue name for task isolation | `kvuno` | |
| `CELERY_TASK_MAX_RETRIES` | Max retries per task | `10` | |
| `CELERY_TASK_RETRY_DELAY` | Retry delay in seconds | `60` | |
| `CELERY_VISIBILITY_TIMEOUT` | Redis visibility timeout (s) before an unacked task is redelivered; must exceed the longest single-file ingest | `21600` | |
| `INGEST_LOCK_STALE_SECONDS` | Age (s) at which a per-file `<file>.ingest.lock` with no refresh is taken over as dead | `600` | |
| `REMOTE_RDS_URLS` | Semicolon-delimited remote file URLs | — | |
| `REMOTE_RDS_TOKEN` | Bearer token for remote downloads | — | |
| `REMOTE_RDS_COOKIES` | Cookie header (comma-separated `k=v` pairs) | — | |
| `REMOTE_RDS_HEADERS` | Custom headers (`key: value; key2: value2`) | — | |
| `REMOTE_RDS_ALLOW_HTTP` | Permit non-HTTPS remote URLs | `false` | |
| `REMOTE_RDS_ALLOWED_DOMAINS` | Comma-separated domain allow-list (exact host or a true subdomain) | — (any) | |
| `REMOTE_RDS_MAX_BYTES` | Max bytes per remote download, enforced while streaming | `1073741824` (1 GiB) | |

## CI/CD

GitHub Actions workflows:

- **PR Checks** (`.github/workflows/pr-checks.yml`) — `ruff check .` + `pip-audit --strict`, a `frontend` job running `pnpm install` + `pnpm run build` in `frontend/`, then `pytest` on Python 3.13 and 3.14 (test job depends on lint and frontend)
- **Version Bumping** — Automated version tags on main
- **Auto PR** — Creates release PRs from version bumps
- **TODO Scanner** — Scans codebase for TODO/FIXME markers
- **Docker Build** — Builds and pushes images to GHCR after PR Checks pass (dev image + `kvuno-web` on `develop`, prod + worker on `main`, all three on tags; see [Container Images](#container-images-ghcr))

> `pyproject.toml` pins Python `>=3.13,<4.0`; the Docker images use `python:3.14-slim`.

## License

Project maintained by [Sammy Barasa](mailto:s.barasa@cgiar.org) as part of the SFP initiative.

# KVuno API

A Flask-based REST API for processing agricultural RDS (R Data Serialization) files containing crop planting data. It extracts optimized sowing dates, crop varieties, and geographic coordinates from RDS files, loads them into a database (SQLite/MySQL/PostgreSQL), and serves the data through a paginated, filterable API endpoint. Duplicate files are tracked via checksums to avoid re-imports.

Built for the [AgWISE-EiA](https://agwise.cgiar.org) initiative (Alliance for a Green Revolution in Africa / Excellence in Agronomy).

## Features

- **RDS File Ingestion** — Reads `.RDS` files using `pyreadr`, processes data in chunks with batch inserts
- **Resumable Uploads** — The SPA uploads over the [tus](https://tus.io) protocol; an interrupted upload resumes from the last byte, and the file is inspected (RDS → Parquet, preview, checksum, suggested column mapping) in the background
- **Deduplication** — SHA-256 checksums track processed files to prevent duplicate imports
- **Background Processing** — SPA uploads and a data-folder scan are processed on background threads in the API (no Redis needed); a Celery + Redis worker is optional and separately deployable
- **REST API** — OpenAPI 3.0 compliant, auto-generated docs at `/api-docs`
- **Paginated & Filterable Queries** — Filter by coordinates + radius, country, province, variety, season type, optimal date, planting option
- **Spatial Data Support** — PostGIS `POINT` geometry (SRID 4326) with `ST_DWithin` radius filtering in metres (geography cast)
- **Database** — PostgreSQL/PostGIS. SQLite can run the migrations and serve the API for UI work, but ingestion (Postgres `ON CONFLICT`) and the spatial queries need Postgres
- **Health Check** — `GET /health` returns app name, build version, and database status
- **Dockerized** — Dev, production, and worker Dockerfiles with docker-compose; images published to GitHub Container Registry
- **Database Migrations** — Alembic-managed schema evolution
- **Token Authentication** — Sanctum-style opaque `{id}|{secret}` tokens (SHA-256 hashed at rest) with BCrypt password hashing; no signing key, and revoking a token is a single row delete
- **React Frontend** — Standalone Vite + React 19 + TypeScript SPA in `frontend/`, built with pnpm and served by nginx, in AKILIMO's colours
- **Login & Registration UI** — SPA routes `/login` and `/register`; anyone can register, but a new account is **pending** until an administrator approves it
- **Account Approval** — Administrators (`ADMIN_EMAILS`, or accounts given the admin role) approve, reject, disable, and promote accounts at the SPA route `/admin/users`; optional SMTP email tells users and admins what happened
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
| Logging | structlog (JSON in production, console in development) |
| Serving | Waitress (dev) / Gunicorn (prod) |
| Frontend | React 19 + TypeScript, Vite 8, pnpm, react-router 6, Bootstrap 5 (CSS only); served by nginx |
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

Edit `.env`: the database settings (`DB_*`, or `DB_URL`) are the only ones a local setup must fill in; every other line shows its default. For SQLite set `DB_DRIVER=sqlite` and `DB_NAME=kvuno.db`.

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
# API server — also processes SPA uploads and scans the data folder in-process
python run.py            # or: poetry run dev
```

```bash
# With background processing (requires Redis + Celery worker)
python dev_worker.py     # auto-selects --pool solo on Windows, prefork elsewhere
```

The API will be available at `http://localhost:5000` (host) and the Swagger UI at `http://localhost:5000/api-docs`. The container listens on port 80 internally; compose maps host 5000 to it.

The API container serves **no HTML** — the pages live in the `frontend/` SPA. Start the Vite dev server alongside it (see [Frontend Architecture](#frontend-architecture)); the Flask container still owns the JSON endpoints the SPA calls.

The API processes files itself, on background threads: files submitted from the Upload page, and a scan of the data folder (`HOUSEKEEPING_DATA_DIR`) at startup. Neither needs Redis or Celery. `HOUSEKEEPING_ENABLED=true` additionally hands `POST /api/v1/data/upload` files and Jobs-page retries of plain data-folder files to the Celery worker. See [How files get processed](#how-files-get-processed).

### Startup checks (preflight)

Before doing any work, both the API and the worker verify their dependencies and exit non-zero if a **required** one is unreachable, so the container is restarted rather than booting into a state where every request fails. Implemented in `app/utils/preflight.py` and run from `docker/entrypoint.sh` (API), `docker/entrypoint.worker.sh` (worker), and `run.py`.

| Dependency | API | Worker |
|---|---|---|
| Database | always required | always required |
| Redis | required only when `HOUSEKEEPING_ENABLED=true` | required, unless `HOUSEKEEPING_MODE` is set |
| Data directory writable | reported, never blocks | reported, never blocks |

Redis is optional for the API because with housekeeping off, progress is stored in PostgreSQL and nothing is enqueued — that is a supported configuration. A Celery worker with no broker is useless, so the worker requires Redis — except in `HOUSEKEEPING_MODE` (`oneshot`/`watch`/`dryrun`), where the image runs `housekeeping.py`, which imports straight into the database. An invalid setting (an unknown `HOUSEKEEPING_MODE` included) fails the preflight at once, without retrying.

The `PREFLIGHT_*` settings (retries, delay, timeout, opt-out) are listed under [Configuration](#configuration).

`run.py` is the one exception to fail-fast: with `FLASK_DEBUG=true` it logs a warning and starts anyway, so local development is not blocked by a database or broker that is not running. Containers still refuse to start, because the entrypoint check is independent of it.

---

### Frontend Architecture

The UI is a standalone **React 18 + TypeScript SPA** in `frontend/`, built with **Vite** and **pnpm**, and served by **nginx** — the Flask app renders no pages. `frontend/nginx.conf` serves the built bundle and reverse-proxies `/api`, `/ui`, `/health`, and `/api-docs` (Swagger UI; spec at `/api-docs/openapi.json`) to the `api` service, so production is **same-origin**: no CORS preflight, and the bearer token never crosses origins. The remaining `/ui/*` JSON endpoints (tus uploads under `/ui/uploads`, `/ui/process`, `/ui/columns`, jobs SSE) still run on Flask and are reached through that proxy. Routes are code-split with `React.lazy`, so the heavy pages (Explore pulls in Leaflet, markercluster, and the heat layer) do not weigh down the initial bundle. `vite dev` proxies the same paths to the API, so local development is same-origin too. The target defaults to `http://127.0.0.1:5000` — the compose `api` service publishes host `5000` → container `80` — and is overridable with `VITE_DEV_PROXY_TARGET` (see `frontend/.env.example`).

| Layer | Value |
|---|---|
| Framework | React 18 + react-router 6 + TypeScript 5.7 |
| Build tool | Vite 6 (`tsc -b && vite build`), package manager pnpm |
| Page routes | `/login`, `/register`, `/upload`, `/jobs`, `/explore`, `/quality`, `/tokens`, `/admin/users` (administrators) |
| Runtime | nginx (SPA + reverse proxy) |
| Dev server | `pnpm dev` in `frontend/` — Vite on `:5173`, proxying to the API at `VITE_DEV_PROXY_TARGET` (default `http://127.0.0.1:5000`) |
| Image | `ghcr.io/cgiar-agwise/kvuno-web` (`frontend/Dockerfile`, node build → nginx runtime) |

### Docker Deployment

`docker-compose.yml` defines `base` (build-only, never started), `data-init` (one-shot: gives the data volume to the containers' `app` user), `api` (production image, Gunicorn), `api-dev` (Flask dev server, only with `--profile dev`, host `127.0.0.1:5002`), `web` (nginx serving the React SPA on host `127.0.0.1:5001`), and `worker` (Celery, or housekeeping via `HOUSEKEEPING_MODE`). The app listens on port 80 inside the container and compose maps host `127.0.0.1:5000` to it. Open the UI at `http://localhost:5001`, not at the API port. All published ports bind to loopback only; there is no `WEB_PORT` variable — change the mapping in the compose file or an override.

Database migrations are applied by the `api` container's entrypoint when `RUN_MIGRATIONS=true` — the entrypoint's own default — but `docker-compose.yml` overrides it to `${RUN_MIGRATIONS:-false}`, so under compose either set it or run `docker compose exec api python scripts/run_migrations.py` after every upgrade. Keep it `false` if you run more than one API replica so they don't race to migrate the same schema. The `worker` deliberately does **not** migrate: it is only handed work by the already-running API.

> **Set `ADMIN_EMAILS`** (e.g. `you@cgiar.org`) on the `api`, or nobody can approve new accounts: anyone can register, but every new account stays `pending` until an administrator approves it. Accounts that existed before the approval migration are `approved` and unaffected. See [Accounts and approval](#accounts-and-approval).

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
# API only (processes SPA uploads, and runs the startup scan in one process)
docker compose up -d api

# API + worker (adds the Celery path; needs Redis)
docker compose up -d api worker

# API + web UI
docker compose up -d api web
```

### Container Images (GHCR)

Images are published to GitHub Container Registry, not Docker Hub:

| Trigger | Images | Tags pushed |
|---|---|---|
| push to `develop`, after `UnitTests` passes | only the images whose contents changed (api = dev server) | `:develop`, `:sha-<short>` |
| push to `main`, after `UnitTests` passes → automatic release tag | api (gunicorn), worker, web | `:latest`, `:<tag>`, `:production`, `:sha-<short>` |

Images: `ghcr.io/cgiar-agwise/kvuno-api`, `-worker`, `-web`. `:latest` is the latest **release**; `:sha-<short>` is immutable, for rollbacks. CI is built to save minutes: it runs only the checks a change can affect (docs-only changes run almost nothing), PRs build only the Docker images whose build inputs changed, and `main` is built once per release. Details and the rules the pipeline depends on: [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md#9-container-registry-ghcr).
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
- Files submitted from the Upload page are received, inspected **and processed** in the API process, with progress on the Jobs page
- The startup scan imports what is in the data folder, in one API process
- Only `POST /api/v1/data/upload` and Jobs-page retries of plain data-folder files need the worker: without it they are saved and reported as such
- Set `HOUSEKEEPING_ENABLED=true` (and run the worker, with Redis) to hand those to Celery

See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) for full details on Compose configuration, environment variables, running commands inside containers, and troubleshooting.

## Usage

### How files get processed

| Source | Where it is processed | Needs Redis/Celery |
|---|---|---|
| Upload page (tus) → **Process** | Background thread in the API process; the upload goes to `processing` | No |
| Files already in `HOUSEKEEPING_DATA_DIR` | Startup scan (`STARTUP_SCAN`, on by default), once per deployment: queued for the Celery worker when `HOUSEKEEPING_ENABLED=true`, otherwise run by **one** API process — the others, and workers recycled within `STARTUP_SCAN_COOLDOWN_SECONDS`, skip | No |
| `POST /api/v1/data/upload` | Celery worker when `HOUSEKEEPING_ENABLED=true`; otherwise saved for the next scan | Yes, to process at once |
| Jobs page **Retry** | An upload: API thread, re-using its saved mapping. A plain data-folder file: Celery when `HOUSEKEEPING_ENABLED=true` | Only for plain files |
| `python housekeeping.py` / `HOUSEKEEPING_MODE` | The CLI process, or the worker container in housekeeping mode | No |

Every path ends in the same `process_file()`: resumable from a stored offset, deduplicated by checksum, with progress on the Jobs page. Several processes can scan the same folder safely: RDS → Parquet conversion takes a per-file lock and writes atomically, and ingestion takes a per-file lock, so a file is never converted or imported twice at once.

### File Processing (Celery Worker)

The worker is optional. With `HOUSEKEEPING_ENABLED=true`, `POST /api/v1/data/upload` and retries of plain data-folder files are queued for it.

Redis is **not** a compose service; run it externally and point `CELERY_BROKER_URL` / `CELERY_RESULT_BACKEND` at it.

```bash
# Start Redis (standalone Docker container, not part of docker-compose.yml)
docker run -d --name kvuno-redis -p 6379:6379 redis:7

# Start the Celery worker (Native — Linux)
celery -A app.celery_app worker --loglevel="${LOG_LEVEL:-info}"

# Start the Celery worker (Native — Windows, use solo pool)
celery -A app.celery_app worker --loglevel="${LOG_LEVEL:-info}" --pool solo

# Start the Celery worker (Docker)
docker compose up --build -d worker
```

Tasks are acked late and re-queued if the worker dies (`task_reject_on_worker_lost`), so `CELERY_VISIBILITY_TIMEOUT` (default 6 h) must exceed your longest single-file ingest or Redis hands a still-running task to a second worker. A per-file `<file>.ingest.lock` makes any such duplicate exit quietly. A database outage mid-file re-raises so Celery retries from the stored offset; a bad file is recorded as `error` at its last position and is not retried.

**Restarting the worker does not lose work.** On shutdown the running file stops at the next chunk boundary, commits its rows and position together, and shows "Paused for a worker restart"; when the worker starts again it clears its own stale locks and resumes every job it left unfinished, from where it stopped. Compose gives it `stop_grace_period: 2m` for this.

### Uploading Files (SPA)

The Upload page sends files with the in-house tus client (`frontend/src/lib/tus.ts`) to a tus 1.0 server (core + creation) in `app/routes/uploads.py`. The server issues the upload id and appends bytes at an offset, so a dropped connection, closed tab, or API restart resumes from the last byte. Each upload is a row in the `uploads` table and moves through:

```
receiving → received → inspecting → ready | failed → submitted → processing
```

1. **Receive** — bytes land in `HOUSEKEEPING_DATA_DIR/uploads/<id>/data.part`. The size cap (`MAX_FILE_SIZE_MB`) and extension are checked at creation, before any bytes arrive
2. **Inspect** — on a background thread in the API process (no Celery needed): RDS → Parquet conversion, preview rows, SHA-256, duplicate lookup against `file_imports`, and a suggested column mapping
3. **Map** — the page shows the server's suggested mapping, which is exactly what will be sent. `lat` and `lon` must be mapped, and each database column at most once
4. **Process** — `POST /ui/uploads/<id>/process` saves the mapping, sets the upload to `processing` and imports it on a background thread in the API, using that mapping as-is (not merged over `RDS_COLUMN_MAP`). No worker or Redis is needed; progress shows on the Jobs page. A submitted or processing upload can no longer be discarded (`409`)

Uploads live under `uploads/` rather than the data directory root on purpose: the startup scan (`load_rds_to_db`) ingests every file in the root with the default mapping. Unfinished uploads idle longer than `UPLOAD_ABANDON_AGE` are deleted when an API process starts. The tus `HEAD`/`PATCH` and the status `GET` are exempt from the default rate limit, since a large file is many requests.

#### Converting large RDS files locally

Reading an RDS takes about 150× its size in memory (`pyreadr` reads the whole file), so the server can refuse a large one with *"Reading this R file needs more memory than the server allows"*. Convert it on a machine with more memory and upload the Parquet instead. `scripts/convert_rds.py` uses the same converter as the server, shows the read time and the rows written, keeps the RDS, never leaves a partial `.parquet`, and exits 1 if any file failed:

```bash
python scripts/convert_rds.py data/field.RDS                 # writes data/field.parquet
python scripts/convert_rds.py data/ --out-dir converted/     # every .rds directly in data/
python scripts/convert_rds.py data/field.RDS --overwrite     # replace an existing .parquet
make convert-rds ARGS="data/ -o converted/"
```

From R, `arrow::write_parquet(df, "field.parquet")` does the same.

### Housekeeping (on-demand import)

`housekeeping.py` imports the files in a folder directly, without Celery — for bulk loads or a manual re-scan. It is a Typer CLI over `housekeeper.run()`; `python housekeeping.py --help` lists the options.

```bash
python housekeeping.py                    # scan HOUSEKEEPING_DATA_DIR once
python housekeeping.py /path/to/data      # another folder
python housekeeping.py --dry-run          # report what would be imported, skipped or resumed
python housekeeping.py --watch            # keep running; import new files as they appear (-w)
python housekeeping.py --batch-size 5000 --chunk-size 20000 --checkpoint-interval 25
```

In Docker, run it in the worker container (same data volume, more memory than the API):

```bash
docker compose exec worker python housekeeping.py --dry-run
```

Or start the worker image **as** a housekeeping job instead of Celery by setting `HOUSEKEEPING_MODE` on it: `oneshot` (scan once and exit), `watch` (keep importing new files) or `dryrun`. Leave it unset for the normal Celery worker.

What happens during a run:
1. **Health check** — `SELECT 1` confirms the database is reachable
2. **Remote download** — Downloads files from `REMOTE_RDS_URLS` (if configured)
3. **Deduplication** — SHA-256 checksum lookup in `file_imports` table; fully imported files are skipped
4. **Resumable processing** — Files with a stored `offset` resume from that row; incremental checkpoints commit every `checkpoint_interval` batches
5. **Batch insert** — Records are inserted in savepoint-protected batches; individual batch failures are logged and skipped
6. **Graceful shutdown** — `Ctrl+C` (or `SIGTERM`) stops at the next chunk boundary, committing its rows and the offset together for later resumption; a second `Ctrl+C` force-quits immediately. These handlers are installed by `housekeeper.run()` (the CLI) only — gunicorn and Celery keep their own signal handling
7. **Telemetry** — Structured log events (`housekeeping.start/end`, `file.download_start/end`, `file.processing_start/end`) go through the app's logger, as JSON lines in production

Remote files are configured with the `REMOTE_RDS_*` settings (see [Configuration](#configuration)).

Downloads are SSRF-guarded (`app/utils/downloader.py`): HTTPS only unless `REMOTE_RDS_ALLOW_HTTP=true`; private, loopback, link-local and metadata addresses refused; redirects followed by hand (max 5), each hop re-checked, with the bearer token and `REMOTE_RDS_COOKIES` dropped when a redirect changes host; optional `REMOTE_RDS_ALLOWED_DOMAINS` (exact host or subdomain); `REMOTE_RDS_MAX_BYTES` (default 1 GiB) per file; and a post-connect peer-IP check against DNS rebinding (best effort, skipped behind an HTTP proxy).

`.RDS` files in the data folder are converted to `.parquet` automatically before every scan (and by the watcher as they arrive): Parquet streams in chunks, whereas an RDS must be read into memory whole.

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

Registration (`POST /api/v1/users/register`) is open to any valid email address, but the new account is **pending** until an administrator approves it — see [Accounts and approval](#accounts-and-approval). Passwords longer than 72 UTF-8 bytes (bcrypt's limit) are rejected at registration (`422`); a login with one is simply `401`.

### Accounts and approval

Every new account starts `pending` (the register response is `201` with `"status": "pending"`) and can neither sign in nor use a token until an administrator approves it. A login with the right password for an account that is not active is `403` with `{"msg", "status"}` (`pending`, `rejected`, or `disabled`); a wrong password is always `401`, so an account's status is only revealed to someone who knows its password. Disabling or rejecting an account revokes all its tokens, and any token of an inactive account stops working at once (`401`).

Administrators are:

- **Owners** — the addresses in `ADMIN_EMAILS` (comma-separated, case-insensitive, read on every request). Registering with one of them skips the queue, and an owner cannot be disabled or demoted from the UI; access can always be recovered by editing that variable.
- Approved accounts that an administrator gave the **admin** role.

To bootstrap the first administrator:

1. Set `ADMIN_EMAILS=you@example.org` on the `api` and restart it.
2. Register at `/register` with that address — the account is approved immediately and you are signed in.
3. The navbar shows **Accounts** (`/admin/users`), with a badge counting pending requests. Approve, reject (with an optional reason), disable, re-activate, or make other approved accounts admins from there. You cannot change your own account, and owners show no actions.

Accounts that existed before migration `a8c4e2f1d9b3` are backfilled `approved`, so the upgrade locks nobody out.

Email is optional. With `SMTP_HOST` and `MAIL_FROM` set, the API sends an activation email on approval (linking `SPA_ROOT_URL` + `/login`), a rejection email with the optional reason, and a new-registration notice to every `ADMIN_EMAILS` address (linking `SPA_ROOT_URL` + `/admin/users`); the reviewer can untick "Email the user about my decision" to skip the user's email. Links are only included when `SPA_ROOT_URL` is an absolute `http(s)` URL. Without SMTP configured nothing is sent and the API log shows `"email not sent: SMTP_HOST/MAIL_FROM unset" (logged with user ids, never addresses)` for each message instead. Sending runs on a background thread and a failure is logged, never raised.

Error responses never carry exception text: a failing endpoint returns a generic `500` body and logs the exception server-side. Request validation errors (bad query/body parameters, including an unknown `sort_col`/`sort_dir`) are `422`, flask-openapi3's validation status.

### API Endpoints

> Rows marked *SPA route* are client-side React routes served by nginx — Flask has no HTML endpoints. In production nginx reverse-proxies `/api`, `/ui`, and `/health` to the `api` service, so the browser sees a single origin.

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET` | `/` | — | Browsers: a landing page linking the web app and API docs. Other clients: redirect to `SPA_ROOT_URL`, or a JSON pointer when it is unset |
| `GET` | `/health` | — | Health check with database status |
| `GET` | `/login`, `/register` | — | SPA routes — login and registration pages (nginx) |
| `GET` | `/jobs` | Required | SPA route — job list (nginx) |
| `GET` | `/explore` | Required | SPA route — map explorer (nginx) |
| `GET` | `/upload` | Required | SPA route — file upload (nginx) |
| `GET` | `/quality` | Required | SPA route — data quality dashboard (nginx) |
| `GET` | `/tokens` | Required | SPA route — token management (nginx) |
| `GET` | `/admin/users` | Admin | SPA route — account approval (nginx; non-admins are sent to `/jobs`) |
| `GET` | `/ui/jobs/data` | Required | JSON: job list |
| `GET` | `/ui/jobs/runs` | Required | Recent processing runs (files submitted together), newest first |
| `GET` | `/ui/jobs/events` | Required | SSE: live job updates (Redis pub/sub, 3s poll fallback) |
| `POST` | `/ui/uploads/tus` | Required | tus create — `Upload-Length` + `Upload-Metadata filename`; size and extension checked before any bytes (`413`/`415`) |
| `HEAD` | `/ui/uploads/tus/<id>` | Required | tus offset probe — `Upload-Offset` to resume from |
| `PATCH` | `/ui/uploads/tus/<id>` | Required | tus append at `Upload-Offset` (`409` offset mismatch, `423` another request holds the upload) |
| `GET` | `/ui/uploads` | Required | The caller's unfinished uploads (survives a page reload) |
| `GET` | `/ui/uploads/<id>` | Required | Upload status, preview, duplicate info, suggested mapping |
| `POST` | `/ui/uploads/<id>/process` | Required | Validate + save the column mapping, then process it in the API (`processing`) |
| `DELETE` | `/ui/uploads/<id>` | Required | Discard an upload that is not yet submitted or processing (`409` otherwise) |
| `POST` | `/ui/process` | Required | (Re)process a stored file — Jobs page Retry; omit `column_map` to re-use the saved one |
| `GET` | `/ui/progress/<file_name>` | Required | One job's progress record (same shape as a `/ui/jobs/data` item), or `status: unknown` |
| `GET` | `/ui/columns` | Required | Mappable DB columns, aliases, and `max_file_size` |
| `POST` | `/api/v1/users/register` | — | Register a new account — `201` with `status: pending` (or `approved` for an `ADMIN_EMAILS` address); `400` invalid email, `409` taken |
| `POST` | `/api/v1/users/login` | — | Authenticate and get a token (`401` wrong credentials, `403` + `status` when the account is pending/rejected/disabled) |
| `GET` | `/api/v1/users/me` | Required | The signed-in account: `id`, `username`, `email`, `status`, `role`, `is_admin` |
| `GET` | `/api/v1/admin/users` | Admin | Accounts, newest first (paginated; filter `status`, search `q` on username/email) plus `counts` per status |
| `POST` | `/api/v1/admin/users/<id>/approve` | Admin | Approve a pending, rejected, or disabled account; optional body `{"notify": true}` |
| `POST` | `/api/v1/admin/users/<id>/reject` | Admin | Reject a pending account and revoke its tokens; optional body `{"reason": "...", "notify": true}` (reason ≤ 500 chars) |
| `POST` | `/api/v1/admin/users/<id>/disable` | Admin | Disable an approved account and revoke all its tokens |
| `POST` | `/api/v1/admin/users/<id>/role` | Admin | `{"role": "admin"}` or `{"role": "user"}` — approved accounts only (`409` otherwise) |
| `POST` | `/api/v1/users/logout` | Required | Revoke the current token (bearer header or `token` cookie) |
| `POST` | `/api/v1/users/tokens` | Required | Create a new API token |
| `GET` | `/api/v1/users/tokens` | Required | List active tokens (paginated) |
| `DELETE` | `/api/v1/users/tokens/<id>` | Required | Revoke a specific token |
| `POST` | `/api/v1/data/upload` | Required | Upload RDS/Parquet files (queued for the worker when `HOUSEKEEPING_ENABLED=true`) |
| `GET` | `/api/v1/planting-data/` | **Public** | Paginated, filterable crop data. Each record has `coordinates` as a GeoJSON Point, `opt_date` as in the source (`30-Nov`, no year) plus `opt_month_day` (`--11-30`), and ISO 8601 UTC `created_at`/`updated_at` |
| `GET` | `/api/v1/planting-data/filters` | Required | Distinct filter values (paginated per column) |
| `GET` | `/api/v1/planting-data/coordinates` | Required | Map coordinates (paginated) |
| `GET` | `/api/v1/planting-data/clusters` | Required | Spatial clusters (paginated by cluster: `pages` from `total_clusters`; bad `zoom`/bounds → `400`) |
| `GET` | `/api/v1/planting-data/export` | Required | Export data (CSV/JSON, paginated; `0` values are kept) |
| `GET` | `/api/v1/quality/stats` | Required | Quality statistics |
| `GET` | `/api/v1/quality/conflicts` | Required | Import conflicts (paginated) |
| `GET` | `/api/v1/quality/duplicates` | Required | Conflicts grouped by country/province/variety/season/date, most repeated first, with counts per source (paginated by group) |

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

Every setting is a field of `Settings` in [`app/config.py`](app/config.py) — the single source of truth for its name, type, default and description. Code reads them with `get_settings().NAME`, never `os.getenv`. Values are validated when a process starts: an invalid one (`SMTP_PORT=abc`, `LOG_LEVEL=LOUD`, `CLEANUP_AGE=soon`) stops the API, worker or preflight with every problem listed, instead of being silently replaced by the default; the preflight also warns about a set variable that looks like a setting but is not one (`LOG_LEVLE`). An empty value (compose's `${X:-}`) means "use the default".

`.env.example` and the tables below are **generated** from that class — change a setting in `app/config.py`, then run `make config-docs` (CI runs `python scripts/gen_config_docs.py --check` and fails on drift).

<!-- config:start (generated by scripts/gen_config_docs.py; do not edit) -->

**Application**

| Variable | Default | Description |
|---|---|---|
| `ENVIRONMENT` | `development` | production/prod → JSON log lines (for Loki etc.); anything else → readable console logs. |
| `APP_VERSION` | — | Version reported by /health and the API docs; set by the image build. |
| `GITHUB_REF_NAME` | — | Fallback for APP_VERSION when built in GitHub Actions. |
| `SPA_ROOT_URL` | — | The web app's URL: target of `GET /` for API clients, linked from the landing page, and the base of links in emails (only when an absolute http(s) URL). |
| `SPA_LOGIN_PATH` | `/login` | Where `require_auth` sends an unauthenticated browser (a client-side SPA route). |

**Logging**

| Variable | Default | Description |
|---|---|---|
| `LOG_LEVEL` | `INFO` | The one log level for every process (API, worker, gunicorn, migrations). One of: DEBUG, INFO, WARNING, ERROR, CRITICAL. |
| `LOG_DESTINATION` | `stdout` | Where logs go; keep stdout in containers (what docker logs / Dokploy collect). One of: stdout, stderr, file. |
| `LOG_ROTATION` | `size` | Rotation for LOG_DESTINATION=file. One of: size, daily. |
| `LOG_FILE_PATH` | — | Log file for LOG_DESTINATION=file. |
| `LOG_MAX_BYTES` | `10485760` | Rotate the log file past this size (LOG_ROTATION=size). |
| `LOG_BACKUP_COUNT` | `5` | Rotated log files to keep. |
| `DEBUG_DB` | `false` | Log every SQL statement, whatever LOG_LEVEL says (very verbose). |

**Database**

| Variable | Default | Description |
|---|---|---|
| `DB_URL` | — | Full SQLAlchemy URL; wins over the DB_* parts below. |
| `DB_DRIVER` | `postgresql` | postgresql, or sqlite for UI/auth work without Postgres (ingestion and spatial queries need Postgres). |
| `DB_HOST` | `127.0.0.1` | Database host (from inside a container: not localhost). |
| `DB_PORT` | `5432` | Database port. |
| `DB_USER` | — | Database user. Required unless DB_URL is set or DB_DRIVER=sqlite. |
| `DB_PASSWORD` | — | Database password. Required unless DB_URL is set or DB_DRIVER=sqlite. |
| `DB_NAME` | — | Database name. Required unless DB_URL is set; with DB_DRIVER=sqlite the file name (default kvuno.db). |
| `RUN_MIGRATIONS` | `true` | Container entrypoint applies migrations before start (compose sets false). |

**Web server (development) and HTTP**

| Variable | Default | Description |
|---|---|---|
| `FLASK_DEBUG` | `false` | Debug mode for `python run.py`; it then starts even when the preflight fails. |
| `SERVER_HOST` | `0.0.0.0` | Bind address for `python run.py`. |
| `SERVER_PORT` | `80` | Port for `python run.py`. |
| `SERVER_URL_PROD` | `https://kvuno.agwise.org` | Server listed in the OpenAPI document. |
| `SERVER_URL_PROD_2` | `https://kvuno.akilimo.org` | Second server listed in the OpenAPI document. |
| `CORS_ORIGINS` | `http://127.0.0.1:5000,http://localhost:5173,http://127.0.0.1:5173` | Comma-separated origins allowed cross-origin (only `pnpm dev`; production is same-origin). |
| `PROXY_FIX_HOPS` | `1` | Trusted proxies in front of the app, for real client IPs. 0 when nothing proxies it, or X-Forwarded-For can be forged to evade rate limits. |
| `SORT_JSON` | `false` | Sort keys in JSON responses. |
| `COORDINATES_BULK_MAX_ROWS` | `5000` | Map points returned by the bulk coordinates endpoint (a random sample beyond this). |

**Gunicorn (production web server)**

| Variable | Default | Description |
|---|---|---|
| `BIND_IP` | `0.0.0.0` | Gunicorn bind address. |
| `BIND_PORT` | `80` | Gunicorn port. |
| `BACKLOG` | `2048` | Gunicorn listen backlog. |
| `WORKER_CLASS` | `gthread` | Gunicorn worker class (gthread: the SSE stream needs threads). |
| `WORKERS` | `2` | Gunicorn worker processes. |
| `THREADS` | `4` | Threads per gunicorn worker. |
| `WORKER_CONNECTIONS` | `1000` | Max simultaneous clients per worker. |
| `WORKER_TMP_DIR` | `/dev/shm` | Gunicorn heartbeat directory. |
| `PRELOAD_APP` | `false` | Load the app before forking (unsafe with SQLAlchemy; leave off). |
| `TIMEOUT` | `120` | Seconds before a silent worker is killed (raised from 30 for inline API upload conversion). |
| `GRACEFUL_TIMEOUT` | `30` | Seconds to finish requests on restart. |
| `KEEPALIVE` | `5` | Keep-alive seconds. |
| `MAX_REQUESTS` | `1000` | Recycle a worker after this many requests. |
| `MAX_REQUESTS_JITTER` | `100` | Random spread so workers do not recycle together. |
| `ACCESSLOG` | `-` | Access log target ('-' = stdout). |
| `ERRORLOG` | `-` | Error log target ('-' = stderr). |

**Accounts and authentication**

| Variable | Default | Description |
|---|---|---|
| `ADMIN_EMAILS` | — | Comma-separated owner addresses (case-insensitive): administrators who cannot be demoted, approved at once on registration, and sent new-registration notices. Unset: nobody can approve new accounts unless one already holds the admin role. |
| `TOKEN_TTL_DAYS` | `0` | Days a sign-in token lasts; 0 = no expiry. |

**Email (optional)**

| Variable | Default | Description |
|---|---|---|
| `SMTP_HOST` | — | SMTP server; unset → no email is sent, and the log says so. |
| `SMTP_PORT` | `587` | SMTP port. |
| `SMTP_USERNAME` | — | SMTP login, skipped when empty (Mailgun: postmaster@<domain>). |
| `SMTP_PASSWORD` | — | SMTP password. |
| `SMTP_SECURITY` | `starttls` | starttls (587), ssl (465) or none (local relay). One of: starttls, ssl, none. |
| `SMTP_TIMEOUT_SECONDS` | `15` | SMTP connect/send timeout. |
| `MAIL_FROM` | — | Sender address; unset = no email is sent. |
| `MAIL_APP_NAME` | `KVuno` | Name used in the From: header and message text. |

**Uploads and data files**

| Variable | Default | Description |
|---|---|---|
| `MAX_FILE_SIZE_MB` | `20` | Largest upload accepted, in MB; enforced at tus creation, before any bytes arrive. |
| `UPLOAD_ABANDON_AGE` | `7d` | Remove uploads idle this long (30m, 12h, 1d, 2w). |
| `UPLOAD_INSPECT_STALE_SECONDS` | `180` | An upload `inspecting` this long is re-claimed on the next status poll. |
| `CLEANUP_AGE` | `1d` | Remove files of jobs completed this long ago (30m, 12h, 1d, 2w). |

**Ingestion (housekeeping)**

| Variable | Default | Description |
|---|---|---|
| `HOUSEKEEPING_ENABLED` | `false` | Hand /api/v1/data/upload and Jobs retries of plain data-folder files to the Celery worker (needs Redis). SPA uploads and the startup scan run in the API process either way. |
| `STARTUP_SCAN` | `true` | Import new files in HOUSEKEEPING_DATA_DIR when the API starts: queued for the Celery worker when HOUSEKEEPING_ENABLED, otherwise run by one API process. |
| `STARTUP_SCAN_COOLDOWN_SECONDS` | `300` | After a startup scan, API processes booting within this many seconds (sibling gunicorn workers, recycled ones) skip theirs. Minimum 60. |
| `HOUSEKEEPING_MODE` | — | Worker image only: run housekeeping.py instead of Celery — oneshot (scan once and exit), watch (keep importing new files) or dryrun (report only). Unset: the normal Celery worker. |
| `HOUSEKEEPING_DATA_DIR` | `static/data` | Directory holding data files; uploads go to its `uploads/` subdirectory. |
| `HOUSEKEEPING_BATCH_SIZE` | `2000` | Rows per insert batch. |
| `HOUSEKEEPING_CHUNK_SIZE` | `5000` | Rows read per chunk (sizes insert batches, not memory). |
| `HOUSEKEEPING_CHECKPOINT_INTERVAL` | `50` | Batches between offset checkpoints. |
| `HOUSEKEEPING_MAX_WORKERS` | `1` | Parallel files in a directory scan. |
| `INGEST_LOCK_STALE_SECONDS` | `600` | Take over a per-file ingest lock silent this long. |
| `INGEST_HOLD_UNMAPPED` | `true` | Hold data-folder files whose columns the default mapping does not cover (no lat/lon) for mapping on the Upload page, instead of importing them with a mapping that finds no coordinates. |
| `INGEST_DUPLICATE_CHECK` | `true` | Recognise files already imported (by SHA-256): warn at upload and skip at import. Off: a re-sent file is imported again; rows already stored are still not duplicated (unique constraint). |
| `INGEST_CONFLICT_LOGGING` | `true` | Record rows that clash with stored ones in import_conflicts (the Quality page). Off: faster imports; clashing rows are still skipped, just not recorded. |
| `RDS_COLUMN_MAP` | — | JSON object overriding the default source→DB column map. |

**Celery / Redis**

| Variable | Default | Description |
|---|---|---|
| `CELERY_BROKER_URL` | `redis://localhost:6379/0` | Redis URL for the Celery broker (also SSE and the cache). |
| `CELERY_RESULT_BACKEND` | `redis://localhost:6379/0` | Celery result backend URL. |
| `CELERY_TASK_DEFAULT_QUEUE` | `kvuno` | Celery queue name. |
| `CELERY_TASK_MAX_RETRIES` | `10` | Retries for a task failing on a transient error. |
| `CELERY_TASK_RETRY_DELAY` | `60` | Seconds between task retries. |
| `CELERY_VISIBILITY_TIMEOUT` | `21600` | Seconds before Redis redelivers an unacked task, even a running one: must exceed the longest single-file ingest. |
| `CELERY_CONCURRENCY` | — | Worker processes for `python dev_worker.py`. |
| `WORKER_MAX_MEMORY_PER_CHILD` | `2048` | MB; recycle a worker child past this (set above one file's peak). |
| `WORKER_MAX_TASKS_PER_CHILD` | `50` | Recycle a worker child after this many tasks. |

**Job progress**

| Variable | Default | Description |
|---|---|---|
| `JOB_STALE_AFTER_SECONDS` | `1800` | A processing job silent this long is shown as stale. |
| `JOB_PROGRESS_TTL_SECONDS` | `604800` | Redis expiry of a job's progress entry. |
| `JOB_PROGRESS_MIN_INTERVAL` | `1.0` | Minimum seconds between published progress updates. |
| `JOB_DB_CHECKPOINT_SECONDS` | `5.0` | Minimum seconds between durable progress writes. |

**Remote RDS downloads**

| Variable | Default | Description |
|---|---|---|
| `REMOTE_RDS_URLS` | — | Semicolon-separated HTTPS URLs fetched before a scan. |
| `REMOTE_RDS_TOKEN` | — | Bearer token for the downloads. |
| `REMOTE_RDS_COOKIES` | — | Cookies, comma-separated `name=value` pairs. |
| `REMOTE_RDS_HEADERS` | — | Extra headers, comma-separated `Name: value` pairs. |
| `REMOTE_RDS_ALLOWED_DOMAINS` | — | Comma-separated hosts downloads may come from (exact or subdomain). |
| `REMOTE_RDS_ALLOW_HTTP` | `false` | Allow plain-HTTP download URLs. |
| `REMOTE_RDS_MAX_BYTES` | `1073741824` | Largest download accepted, in bytes (checked while streaming). |

**Rate limiting**

| Variable | Default | Description |
|---|---|---|
| `RATE_LIMIT_REGISTER` | `10 per hour` | Registration limit (flask-limiter syntax). |
| `RATE_LIMIT_LOGIN` | `20 per hour` | Sign-in limit. |
| `RATE_LIMIT_UPLOAD` | `10 per hour` | API upload limit. |
| `RATE_LIMIT_DATA` | `120 per minute` | Planting-data endpoints limit. |
| `RATE_LIMIT_COORDINATES_BULK` | `20 per minute` | Bulk coordinates endpoint limit. |
| `RATE_LIMIT_DEFAULT_HOURLY` | `600` | Requests per hour (a bare count) for any route without its own limit. If real users see 429s, raise this rather than the per-endpoint limits. |
| `RATE_LIMIT_DEFAULT_DAILY` | `5000` | Requests per day (a bare count), as above. |
| `RATE_LIMIT_STORAGE` | `memory://` | Counter storage; use Redis with several gunicorn workers. |

**Startup preflight**

| Variable | Default | Description |
|---|---|---|
| `PREFLIGHT_ENABLED` | `true` | Check the database (and Redis when required) before starting; false skips it. |
| `PREFLIGHT_TIMEOUT_SECONDS` | `3` | Timeout per check. |
| `PREFLIGHT_RETRIES` | `30` | Attempts before giving up. |
| `PREFLIGHT_DELAY_SECONDS` | `2` | Seconds between attempts (30 x 2 s = 60 s tolerance). |
| `PREFLIGHT_ROLE` | `api` | Role when the preflight is run without an argument. One of: api, worker. |

**Developer tooling**

| Variable | Default | Description |
|---|---|---|
| `OUTFILE_PATH` | `app/models/kvuno.py` | model-generator.py output file. |
| `EXCLUDED_TABLES` | `spatial_ref_sys,alembic_version` | model-generator.py tables to skip. |

<!-- config:end -->

## CI/CD

GitHub Actions workflows:

- **PR Checks** (`.github/workflows/pr-checks.yml`) — `ruff check .` + `pip-audit --strict`, a `frontend` job running `pnpm install` + `pnpm run build` in `frontend/`, and `pytest` on Python 3.14 — in one Python job; a `changes` job skips whichever of the Python, frontend and PR Docker jobs the diff cannot affect
- **Version Bumping** — Automated version tags on main
- **Auto PR** — Creates release PRs from version bumps
- **TODO Scanner** — Scans codebase for TODO/FIXME markers
- **Docker Build** — Builds and pushes images to GHCR after PR Checks pass (dev image + `kvuno-web` on `develop`, prod + worker on `main`, all three on tags; see [Container Images](#container-images-ghcr))

> `pyproject.toml` pins Python `>=3.13,<4.0`; the Docker images use `python:3.14-slim`.

## License

Project maintained by [Sammy Barasa](mailto:s.barasa@cgiar.org) as part of the SFP initiative.

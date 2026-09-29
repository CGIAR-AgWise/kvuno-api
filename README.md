# KVuno API

A Flask-based REST API for processing agricultural RDS (R Data Serialization) files containing crop planting data. It extracts optimized sowing dates, crop varieties, and geographic coordinates from RDS files, loads them into a database (SQLite/MySQL/PostgreSQL), and serves the data through a paginated, filterable API endpoint. Duplicate files are tracked via checksums to avoid re-imports.

Built for the [AgWISE-EiA](https://agwise.cgiar.org) initiative (Alliance for a Green Revolution in Africa / Excellence in Agronomy).

## Features

- **RDS File Ingestion** — Reads `.RDS` files using `pyreadr`, processes data in chunks with batch inserts
- **Deduplication** — SHA-256 checksums track processed files to prevent duplicate imports
- **Background Processing** — Celery + Redis worker for async file ingestion; separately deployable
- **REST API** — OpenAPI 3.0 compliant, auto-generated docs at `/api-docs`
- **Paginated & Filterable Queries** — Filter by coordinates + radius, country, province, variety, season type, optimal date, planting option
- **Spatial Data Support** — PostGIS `POINT` geometry (SRID 4326) with `ST_DWithin` radius filtering
- **Multi-Database** — SQLite and PostgreSQL/PostGIS (the spatial features require PostGIS)
- **Health Check** — `GET /health` returns app name, build version, and database status
- **Dockerized** — Dev, production, and worker Dockerfiles with docker-compose; images published to GitHub Container Registry
- **Database Migrations** — Alembic-managed schema evolution
- **Token Authentication** — Sanctum-style opaque `{id}|{secret}` tokens (SHA-256 hashed at rest) with BCrypt password hashing; no signing key, and revoking a token is a single row delete
- **Login & Registration UI** — Web forms at `/ui/login` and `/ui/register`
- **Token Management** — List and revoke tokens at `/ui/tokens`
- **CORS** — Cross-origin support enabled globally
- **Request Rate Limiting** — Flask-Limiter available for route protection

## Tech Stack

 | Component | Technology |
|---|---|---|
| Framework | Flask (via flask-openapi3) |
| ORM | SQLAlchemy (flask-sqlalchemy) |
| Database | SQLite / PostgreSQL (psycopg2) |
| Migrations | Alembic |
| Spatial | GeoAlchemy2 / PostGIS |
| RDS Parsing | pyreadr + pandas |
| Authentication | Opaque bearer tokens (`{id}|{secret}`, SHA-256 at rest) + bcrypt |
| Background Tasks | Celery + Redis (Kombu transport) |
| Logging | loguru |
| Serving | Waitress (dev) / Gunicorn (prod) |
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
│   │   └── main.py           # HTML /ui/* routes, resumable upload, /health
│   ├── services/
│   │   ├── housekeeper.py    # Ingestion pipeline (process_file, load_rds_to_db, CLI)
│   │   ├── progress_store.py # Job progress (Redis primary, DB fallback)
│   │   └── watch_handler.py  # watchdog directory watcher
│   ├── templates/            # Jinja pages (base, jobs, upload, explore, quality, login…)
│   ├── static/               # css/ js/ favicon served by Flask
│   └── utils/
│       ├── logging.py        # SharedLogger (loguru wrapper)
│       ├── downloader.py     # RDSDownloader (auth, SSRF guards)
│       ├── rds_to_parquet.py # RDS → Parquet batch converter
│       └── migration_utils.py# Dialect-aware column utilities
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
├── docker-compose.yml        # Multi-service Docker setup (base, api, worker)
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

Set `HOUSEKEEPING_ENABLED=false` (default) to skip the 2-second probe for a Celery worker.

Set `HOUSEKEEPING_ENABLED=true` to enqueue file-uploads to the Celery worker automatically.

### Docker Deployment

`docker-compose.yml` defines three services: `base` (build-only, never started), `api` (Flask dev server), and `worker` (Celery). The app listens on port 80 inside the container and compose maps host `5000` to it.

Database migrations are applied by the `api` container's entrypoint before the server starts — there is no separate migration service. Set `RUN_MIGRATIONS=false` to skip, which is what you want if you run more than one API replica so they don't race to migrate the same schema. The `worker` deliberately does **not** migrate: it is only handed work by the already-running API.

> PostgreSQL/PostGIS and Redis are **not** compose services — they are expected to run elsewhere. Point `DB_HOST` and `CELERY_BROKER_URL` at whatever is reachable, or add them back via a `docker-compose.override.yml`.

```bash
# Build the shared builder base FIRST — the app images FROM it
docker compose build base

# Build and start
docker compose up --build -d


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
```

### Container Images (GHCR)

Images are published to GitHub Container Registry, not Docker Hub:

| Trigger | Image | Dockerfile | Tags pushed |
|---|---|---|---|
| `develop` | `ghcr.io/cgiar-agwise/kvuno-api` | `docker/Dockerfile` (dev) | `:latest`, `:develop` |
| `develop` | `ghcr.io/cgiar-agwise/kvuno-worker` | `docker/Dockerfile.worker` (Celery) | `:latest`, `:develop` |
| `main` | `ghcr.io/cgiar-agwise/kvuno-api` | `docker/Dockerfile.prod.dockerfile` (prod) | `:latest`, `:production` |
| any tag | `ghcr.io/cgiar-agwise/kvuno-api` | `docker/Dockerfile.prod.dockerfile` (prod) | `:latest`, `:<tag>`, `:production` |
| any tag | `ghcr.io/cgiar-agwise/kvuno-worker` | `docker/Dockerfile.worker` | `:latest`, `:<tag>`, `:production` |

Branch builds live in `docker-build.yml` (triggered by PR Checks); tag builds live in `docker-release.yml` (triggered by `push: tags`). Splitting them keeps each job's condition to a single branch check. **The worker is not built on `main`** — production should deploy the worker from a release tag.

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

The app runs with SQLite for local development — no Postgres needed:

```bash
# Edit .env
DB_DRIVER=sqlite
DB_NAME=kvuno.db
HOUSEKEEPING_ENABLED=false

# Run
python run.py
```

### Local Development without Redis / Celery

When `HOUSEKEEPING_ENABLED=false` (default), the app runs entirely without Redis:

- The API serves data normally
- File uploads return a warning response saying no worker is available
- Set `HOUSEKEEPING_ENABLED=true` to enable background enqueuing (requires Redis accessible)

See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) for full details on Compose configuration, environment variables, running commands inside containers, and troubleshooting.

## Usage

### File Processing (Background Worker)

File ingestion is handled by a Celery worker. When `HOUSEKEEPING_ENABLED=true`, the `/api/v1/upload/` endpoint enqueues a task; the Celery worker processes it asynchronously.

```bash
# Start Redis (Docker)
docker compose up -d redis

# Start the Celery worker (Native — Linux)
celery -A app.celery_app worker --loglevel=info

# Start the Celery worker (Native — Windows, use solo pool)
celery -A app.celery_app worker --loglevel=info --pool solo

# Start the Celery worker (Docker)
docker compose up --build -d worker
```

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
6. **Graceful shutdown** — `Ctrl+C` (or `SIGTERM`) commits completed batches and persists the offset for later resumption; a second `Ctrl+C` force-quits immediately
7. **Telemetry** — Structured JSON events (`housekeeping.start/end`, `file.download_start/end`, `file.processing_start/end`) are written to stderr for monitoring ingestion

Remote files are configured via environment variables:

| Variable | Description |
|---|---|
| `REMOTE_RDS_URLS` | Semicolon-delimited URLs of remote `.RDS` files to download |
| `REMOTE_RDS_TOKEN` | Bearer token for authenticated downloads |
| `REMOTE_RDS_COOKIES` | Cookie header (e.g. `session=abc; token=xyz`) |
| `REMOTE_RDS_HEADERS` | Custom headers as `key: value; key2: value2` |

Convert large `.RDS` files to `.parquet` for faster processing:

```bash
python -c "from app.utils.rds_to_parquet import batch_convert; batch_convert('static/data/')"
```

### Authentication

All UI routes (`/ui/*`) and most API routes require authentication. Obtain a token via:

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

Or use the web UI at `/ui/login` to sign in — the token is stored as a cookie for browser navigation.

### API Endpoints

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET` | `/` | — | Redirects to `/ui/jobs` |
| `GET` | `/health` | — | Health check with database status |
| `GET` | `/ui/login` | — | Login page |
| `GET` | `/ui/register` | — | Registration page |
| `GET` | `/ui/jobs` | Required | Job list |
| `GET` | `/ui/jobs/data` | Required | JSON: job list |
| `GET` | `/ui/jobs/events` | Required | SSE: live job updates (Redis pub/sub, 3s poll fallback) |
| `GET` | `/ui/upload` | Required | File upload UI (resumable.js) |
| `POST` | `/ui/upload/resumable` | Required | Receive one upload chunk (resumable.js) |
| `GET` | `/ui/upload/resumable` | Required | Chunk probe — 200 if exists, 204 otherwise |
| `POST` | `/ui/upload/complete` | Required | Merge chunks, return columns + sample rows |
| `POST` | `/ui/process` | Required | Save column mapping and start ingestion |
| `GET` | `/ui/progress/<file_name>` | Required | Per-file progress JSON |
| `GET` | `/ui/explore` | Required | Map explorer |
| `GET` | `/ui/quality` | Required | Data quality dashboard |
| `GET` | `/ui/columns` | Required | Mappable DB columns + aliases |
| `GET` | `/ui/tokens` | Required | Token management |
| `POST` | `/api/v1/users/register` | — | Register a new account |
| `POST` | `/api/v1/users/login` | — | Authenticate and get a token |
| `POST` | `/api/v1/users/logout` | Required | Revoke the current token |
| `POST` | `/api/v1/users/tokens` | Required | Create a new API token |
| `GET` | `/api/v1/users/tokens` | Required | List active tokens (paginated) |
| `DELETE` | `/api/v1/users/tokens/<id>` | Required | Revoke a specific token |
| `POST` | `/api/v1/data/upload` | Required | Upload an RDS/parquet file |
| `GET` | `/api/v1/planting-data/` | **Public** | Paginated, filterable crop data |
| `GET` | `/api/v1/planting-data/filters` | Required | Distinct filter values (paginated per column) |
| `GET` | `/api/v1/planting-data/coordinates` | Required | Map coordinates (paginated) |
| `GET` | `/api/v1/planting-data/clusters` | Required | Spatial clusters (paginated) |
| `GET` | `/api/v1/planting-data/export` | Required | Export data (CSV/JSON, paginated) |
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
| `radius` | float | Search radius in meters (requires `coordinates`) |
| `country` | string | Country name (partial ILIKE match) |
| `province` | string | Province name (partial ILIKE match) |
| `variety` | string | Crop variety exact match |
| `season_type` | string | Season type exact match |
| `opt_date` | string | Optimal sowing date (`YYYY-MM-DD`) |
| `planting_option` | string | Planting option exact match |
| `sort_col` / `sort_dir` | string | Server-side sorting used by the explore UI |

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
| `RATE_LIMIT_REGISTER` | flask-limiter limit string | `10 per hour` | |
| `RATE_LIMIT_LOGIN` | flask-limiter limit string | `20 per hour` | |
| `RATE_LIMIT_UPLOAD` | flask-limiter limit string | `10 per hour` | |
| `RATE_LIMIT_DATA` | flask-limiter limit string | `120 per minute` | |
| `RATE_LIMIT_DEFAULT_HOURLY` | Blanket limit for routes with no explicit limit (UI pages) | `600` | |
| `RATE_LIMIT_DEFAULT_DAILY` | Same, daily | `5000` | |
| `PROXY_FIX_HOPS` | Number of trusted proxies in front of the app; enables real client IPs | `0` | |
| `RATE_LIMIT_STORAGE` | Rate-limit backend URI | `memory://` | |
| `CORS_ORIGINS` | Comma-separated allowed CORS origins | `http://127.0.0.1:5000` | |
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
| `LOG_LEVEL` | Gunicorn log level | `INFO` | |
| `LOG_LEVEL` | Logging level | `DEBUG` | |
| `DEBUG_DB` | Echo SQL statements | `false` | |
| `SERVER_URL_PROD` | Production server URL | `https://kvuno.agwise.org` | |
| `MAX_FILE_SIZE_MB` | Upload size cap (API-enforced) | `20` | |
| `CLEANUP_AGE` | Age of completed files purged at startup (`1d`, `12h`, …) | `1d` | |
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
| `REMOTE_RDS_URLS` | Semicolon-delimited remote file URLs | — | |
| `REMOTE_RDS_TOKEN` | Bearer token for remote downloads | — | |
| `REMOTE_RDS_COOKIES` | Cookie header (comma-separated `k=v` pairs) | — | |
| `REMOTE_RDS_HEADERS` | Custom headers (`key: value; key2: value2`) | — | |
| `REMOTE_RDS_ALLOW_HTTP` | Permit non-HTTPS remote URLs | `false` | |
| `REMOTE_RDS_ALLOWED_DOMAINS` | Comma-separated domain allow-list | — (any) | |

## CI/CD

GitHub Actions workflows:

- **PR Checks** (`.github/workflows/pr-checks.yml`) — `ruff check .` + `pip-audit --strict`, then `pytest` on Python 3.13 and 3.14 (test job depends on lint)
- **Version Bumping** — Automated version tags on main
- **Auto PR** — Creates release PRs from version bumps
- **TODO Scanner** — Scans codebase for TODO/FIXME markers
- **Docker Build** — Builds and pushes images to GHCR after PR Checks pass (dev image on `develop`, prod + worker on `main`; see [Container Images](#container-images-ghcr))

> `pyproject.toml` pins Python `>=3.13,<4.0`; the Docker images use `python:3.14-slim`.

## License

Project maintained by [Sammy Barasa](mailto:s.barasa@cgiar.org) as part of the SFP initiative.

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
- **Health Check** — `GET /health` endpoint with database connectivity status
- **Dockerized** — Dev, production, and worker Dockerfiles with docker-compose; images published to GitHub Container Registry
- **Database Migrations** — Alembic-managed schema evolution
- **JWT Authentication** — Sanctum-style `{id}|{secret}` tokens with BCrypt password hashing
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
| Authentication | JWT (HS256) + bcrypt |
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
├── docker-compose.yml        # Multi-service Docker setup (api, worker, migrate)
├── Dockerfile                # Dev Docker image
├── Dockerfile.prod.dockerfile# Production Docker image
├── Dockerfile.worker         # Celery worker Docker image
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

Edit `.env` with your database connection and a random JWT secret:

```env
# Required
JWT_SECRET=your-random-secret-here
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

Migrations are **not** run automatically at app startup. Run them explicitly:

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

The API will be available at `http://localhost:5000` and the Swagger UI at `http://localhost:5000/api-docs`.

Set `HOUSEKEEPING_ENABLED=false` (default) to skip the 2-second probe for a Celery worker.

Set `HOUSEKEEPING_ENABLED=true` to enqueue file-uploads to the Celery worker automatically.

### Docker Deployment

`docker-compose.yml` defines three services: `api` (Flask dev server, port 5000), `worker` (Celery), and `migrate` (one-shot migration runner).

> PostgreSQL/PostGIS and Redis are **not** compose services — they are expected to run elsewhere. Point `DB_HOST` and `CELERY_BROKER_URL` at whatever is reachable, or add them back via a `docker-compose.override.yml`.

```bash
# Build and start
docker compose up --build -d

# Apply migrations (separate deploy step — the app does not migrate on startup)
docker compose run --rm migrate

# Verify
curl http://localhost:5000/health
```

### Docker — Individual Services

```bash
# API only (no background processing)
docker compose up -d api

# API + worker
docker compose up -d api worker
```

### Container Images (GHCR)

Images are published to GitHub Container Registry, not Docker Hub:

| Image | Dockerfile | Built on |
|---|---|---|
| `ghcr.io/cgiar-agwise/kvuno-api` | `Dockerfile` (dev server, Python 3.14) | `develop` |
| `ghcr.io/cgiar-agwise/kvuno-api` | `Dockerfile.prod.dockerfile` (Gunicorn) | `main` |
| `ghcr.io/cgiar-agwise/kvuno-worker` | `Dockerfile.worker` (Celery) | `main`, `develop`, `beta/*` |

CI publishes with the built-in `GITHUB_TOKEN` (`permissions: packages: write`) — no `DOCKER_USERNAME` / `DOCKER_PASSWORD` secrets are needed. To pull on a server, authenticate with a classic PAT that has `read:packages`:

```bash
echo "$GHCR_TOKEN" | docker login ghcr.io -u <github-username> --password-stdin
```

Packages are private by default; set them to public in the org settings if you want anonymous pulls.

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
| `GET` | `/api/v1/users/tokens` | Required | List active tokens |
| `DELETE` | `/api/v1/users/tokens/<id>` | Required | Revoke a specific token |
| `POST` | `/api/v1/data/upload` | Required | Upload an RDS/parquet file |
| `GET` | `/api/v1/planting-data/` | **Public** | Paginated, filterable crop data |
| `GET` | `/api/v1/planting-data/filters` | Required | Distinct filter values |
| `GET` | `/api/v1/planting-data/coordinates` | Required | Map coordinates |
| `GET` | `/api/v1/planting-data/clusters` | Required | Spatial clusters |
| `GET` | `/api/v1/planting-data/export` | Required | Export data (CSV/JSON) |
| `GET` | `/api/v1/quality/stats` | Required | Quality statistics |
| `GET` | `/api/v1/quality/conflicts` | Required | Import conflicts |

> `GET /api/v1/planting-data/` is intentionally **public** (rate-limited via `RATE_LIMIT_DATA`); the supporting endpoints under the same prefix require a token.

### Query Parameters for `/api/v1/planting-data/`

| Parameter | Type | Description |
|---|---|---|
| `page` | int | Page number (default: 1) |
| `per_page` | int | Items per page (default: 50, max 500) |
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
| `JWT_SECRET` | Secret key for JWT signing | — | **Yes** |
| `TOKEN_TTL_DAYS` | Token expiration in days (`0` = never) | `0` | |
| `RATE_LIMIT_REGISTER` | flask-limiter limit string | `10 per hour` | |
| `RATE_LIMIT_LOGIN` | flask-limiter limit string | `20 per hour` | |
| `RATE_LIMIT_UPLOAD` | flask-limiter limit string | `10 per hour` | |
| `RATE_LIMIT_DATA` | flask-limiter limit string | `120 per minute` | |
| `RATE_LIMIT_STORAGE` | Rate-limit backend URI | `memory://` | |
| `CORS_ORIGINS` | Comma-separated allowed CORS origins | `http://127.0.0.1:5000` | |
| `FLASK_DEBUG` | Enable debug mode | `false` | |
| `SERVER_HOST` | Bind address | `0.0.0.0` | |
| `SERVER_PORT` | Bind port | `5000` (`run.py` falls back to `80`) | |
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

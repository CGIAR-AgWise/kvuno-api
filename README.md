# KVuno API

A Flask-based REST API for agricultural planting data delivered as R data files (`.RDS`) or Parquet. Every RDS is **converted to Parquet** before it is imported, because Parquet can be read a chunk at a time and an RDS cannot. The API extracts optimal sowing dates, crops, varieties and coordinates, loads them into PostgreSQL/PostGIS, and serves them through paginated, filterable endpoints. Files are tracked by checksum, so the same file is never imported twice.

Built for the [AgWISE-EiA](https://agwise.cgiar.org) initiative (Alliance for a Green Revolution in Africa / Excellence in Agronomy).

## Features

- **RDS → Parquet Conversion** — Every `.RDS` is converted to `.parquet` before import (see [What happens to an RDS file](#what-happens-to-an-rds-file)); Parquet is then streamed in chunks with batch inserts
- **Resumable Uploads** — The SPA uploads over the [tus](https://tus.io) protocol; an interrupted upload resumes from the last byte, and the file is inspected (RDS → Parquet conversion, preview, checksum, suggested column mapping) in the background
- **Deduplication** — SHA-256 checksums track processed files to prevent duplicate imports
- **Background Processing** — A Celery + Redis worker reads, converts and imports files, off the API; without one, the API starts each import in a separate process and scans the data folder itself, so Redis is optional
- **REST API** — OpenAPI 3.0 compliant, auto-generated docs at `/api-docs`
- **Crop Types** — Every row records its crop (`crop_type`), from a mapped file column or the crop chosen when the file was mapped; the same place, variety, season and date for two crops are two rows
- **Paginated & Filterable Queries** — Filter by coordinates + radius, crop type, country, province, variety, season type, optimal date, planting option; `page`/`per_page` are documented on every paginated endpoint in Swagger UI
- **Spatial Data Support** — PostGIS `POINT` geometry (SRID 4326) with `ST_DWithin` radius filtering in metres (geography cast)
- **Database** — PostgreSQL/PostGIS. SQLite can run the migrations and serve the API for UI work, but ingestion (Postgres `ON CONFLICT`) and the spatial queries need Postgres
- **Health Check** — `GET /health` returns app name, build version, and database status; `?deep=1` also reports Redis, Celery-worker and data-directory state (status `degraded`, still 200, when one is down)
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
|---|---|
| Framework | Flask (via flask-openapi3) |
| ORM | SQLAlchemy (flask-sqlalchemy) |
| Database | SQLite / PostgreSQL (psycopg 3, `psycopg[binary]`) |
| Migrations | Alembic |
| Spatial | GeoAlchemy2 / PostGIS |
| RDS Parsing | pyreadr + pandas |
| Authentication | Opaque bearer tokens (`{id}\|{secret}`, SHA-256 at rest) + bcrypt |
| Background Tasks | Celery + Redis (Kombu transport) |
| Logging | structlog (JSON in production, console in development) |
| Serving | Waitress (dev) / Gunicorn (prod) |
| Frontend | React 19 + TypeScript, Vite 8, pnpm, react-router 6, Bootstrap 5 (CSS only); served by nginx |
| Containerization | Docker + docker-compose |
| CI/CD | GitHub Actions |

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
# API server — without a worker it also starts uploaded files' imports and scans the data folder
python run.py            # or: poetry run dev
```

```bash
# With background processing (requires Redis + Celery worker)
python dev_worker.py     # auto-selects --pool solo on Windows, prefork elsewhere
```

The API will be available at `http://localhost:5000` (host) and the Swagger UI at `http://localhost:5000/api-docs`. The container listens on port 80 internally; compose maps host 5000 to it.

The API container serves **no HTML** — the pages live in the `frontend/` SPA. Start the Vite dev server alongside it (see [Frontend Architecture](docs/DEVELOPMENT.md#frontend-architecture)); the Flask container still owns the JSON endpoints the SPA calls.

Without Redis or Celery the app still processes files: Upload-page files are imported in a separate `ingest_runner` process the API starts, and the data folder (`HOUSEKEEPING_DATA_DIR`) is scanned at startup. With `HOUSEKEEPING_ENABLED=true` and a Celery worker running, the worker reads and imports every file instead, which is how production runs. See [How files get processed](docs/INGESTION.md#how-files-get-processed).

## What happens to an RDS file

Reading an RDS (`pyreadr`) loads the **whole** file into memory, about 150 times its size on disk, and it cannot be read in chunks. Parquet can. So every RDS is converted to Parquet first, and only the Parquet is imported:

| How the RDS arrives | When it is converted | What is kept |
|---|---|---|
| Upload page | While the upload is inspected, before you map its columns. The page shows the progress ("Reading the file…", then rows written) | `<id>.parquet`; the RDS is deleted |
| Copied into the data folder (`HOUSEKEEPING_DATA_DIR`) | Before every scan, and by the watcher as files arrive. The Jobs page shows it as a **Converting** job | `<name>.parquet` with the RDS's modified time; the RDS is deleted |
| `POST /api/v1/data/upload` | By the worker, before importing (a **Converting** job); without a worker, in the request | The Parquet; the RDS is deleted |

Conversion runs in a **child process**, on the worker when there is one. A file too big for the memory available stops only that process, and the upload fails with *"Reading this R file needs more memory than the server allows"*. An unreadable file fails with *"not a readable R data file"*. In either case, convert it on a machine with more memory and upload the Parquet instead:

```bash
python scripts/convert_rds.py path/to/field.RDS        # writes path/to/field.parquet, keeps the RDS
```

From R, `arrow::write_parquet(df, "field.parquet")` does the same. Details: [docs/INGESTION.md](docs/INGESTION.md#converting-large-rds-files-locally).

## Documentation

| Guide | What it covers |
|---|---|
| [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) | Project structure, startup checks, the frontend, running without PostgreSQL or Redis |
| [docs/INGESTION.md](docs/INGESTION.md) | How files get processed: uploads, the data folder, the Celery worker, housekeeping, maintenance scripts |
| [docs/API.md](docs/API.md) | Authentication, accounts, endpoints, pagination, query parameters, response format |
| [docs/CONFIGURATION.md](docs/CONFIGURATION.md) | Every setting with its default (generated from `app/config.py`) |
| [docs/DOCKER.md](docs/DOCKER.md) | Docker Compose, images on GHCR, the builder base, upgrading Poetry |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | The full deployment guide: environment, running, volumes, troubleshooting, CI images |
| [docs/DATA-FLOW.md](docs/DATA-FLOW.md) | The ingestion and serving pipelines step by step, and the database schema |
| [docs/DESIGN.md](docs/DESIGN.md) | UI/UX decisions and the routes map |
| [AGENTS.md](AGENTS.md) | Notes for coding agents: conventions, traps, and why things are the way they are |

## CI/CD

GitHub Actions workflows:

- **PR Checks** (`.github/workflows/pr-checks.yml`) — `ruff check .` + `pip-audit --strict`, a `frontend` job running `pnpm install` + `pnpm run build` in `frontend/`, and `pytest` on Python 3.14 — in one Python job; a `changes` job skips whichever of the Python, frontend and PR Docker jobs the diff cannot affect
- **Version Bumping** — Automated version tags on main
- **Auto PR** — Creates release PRs from version bumps
- **TODO Scanner** — Scans codebase for TODO/FIXME markers
- **Docker Build** — Builds and pushes images to GHCR after PR Checks pass (dev image + `kvuno-web` on `develop`, prod + worker on `main`, all three on tags; see [Container Images](docs/DOCKER.md#container-images-ghcr))

> `pyproject.toml` pins Python `>=3.13,<4.0`; the Docker images use `python:3.14-slim`.

## License

Project maintained by [Sammy Barasa](mailto:s.barasa@cgiar.org) as part of the SFP initiative.

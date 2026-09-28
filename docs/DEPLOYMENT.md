# Deployment Guide

Covers Docker Compose setup, image builds, service configuration, and running management commands in containers.

---

## 1. Architecture

`docker-compose.yml` defines six services:

| Service | Image | Purpose |
|---|---|---|
| `dev` | `masgeek/kvuno-api` (built from `Dockerfile`) | Flask dev server on port 5000 |
| `prod` | `masgeek/kvuno-api:${PROD_TAG}` (built from `Dockerfile.prod.dockerfile`) | Gunicorn on port 5001 |
| `worker` | `masgeek/kvuno-worker` (built from `Dockerfile.worker`) | Celery ingestion worker |
| `migrate` | `masgeek/kvuno-api` | One-shot `scripts/run_migrations.py` run, then exits |
| `redis` | `redis:7-alpine` | Celery broker + job-progress store |
| `db` | `postgis/postgis:17-3.5` | PostgreSQL + PostGIS on port 5432 |

```
┌──────────────┐      port 5432     ┌──────────────┐
│   db         │◀───────────────────│   dev/prod   │
│  PostGIS 17  │   DB_HOST=db       │  Flask API   │
│              │                    │  5000 / 5001 │
└──────────────┘                    └──────┬───────┘
                                            │
       ┌────────────────────────────────────┴──────────────┐
       │                                                   │
┌──────▼───────┐                              ┌───────────▼──┐
│    redis     │◀───── broker, results ─────▶│    worker    │
│  6379        │      job progress keys      │  Celery task │
└──────────────┘                              └──────────────┘
```

The database data persists in the `db` container's own volume (no named `pgdata` volume is declared in the current compose file). `dev`, `prod`, `worker`, and `migrate` all bind-mount `${DATA_DIR:-./data}` at `/app/static/data`.

---

## 2. Environment Variables

### `docker-compose.yml` interpolation

The compose file reads these variables from the host environment (or `.env` file in the project root):

| Compose variable | Default | Used by |
|---|---|---|
| `TAG` | `latest` | `dev` / `worker` / `migrate` image tag |
| `PROD_TAG` | `production` | `prod` image tag |
| `DATA_DIR` | `./data` | Host directory mounted at `/app/static/data` |
| `DB_NAME` | *(required)* | Database name (also sets `POSTGRES_DB`) |
| `DB_USER` | *(required)* | Database user (also sets `POSTGRES_USER`) |
| `DB_PASSWORD` | *(required)* | Database password (also sets `POSTGRES_PASSWORD`) |

`DB_USER`, `DB_PASSWORD`, and `DB_NAME` use `${VAR:?message}` syntax, so Compose **fails fast** if they are missing.

### Env loading per service

| Service | How it gets config |
|---|---|
| `dev` | `env_file: .env` (the explicit `environment:` block is commented out) |
| `prod`, `worker`, `migrate` | Explicit `environment:` blocks with `${VAR:-default}` interpolation |
| All | `load_dotenv()` at import time, so a mounted/baked `.env` also applies |

### Required values for the app container

```env
# Build the DB URL from parts (app.config.build_db_url)
DB_DRIVER=postgresql
DB_HOST=db                   # ← service name, not localhost
DB_PORT=5432
DB_USER=postgres
DB_PASSWORD=postgres
DB_NAME=agwise_api

# Required — app/config.py raises at import if unset
JWT_SECRET=<random string>

# Optional: full URL override
# DB_URL=postgresql://postgres:postgres@db:5432/agwise_api
```

> **Important:** When running inside Docker Compose, `DB_HOST` must be `db` (the service name), not `127.0.0.1` or `localhost`, because containers communicate over the internal Compose network.

---

## 3. Running

### First-time startup

```bash
# Build images and start all services in the background
docker compose up --build -d

# Run database migrations (one-shot)
docker compose run --rm migrate

# Verify health
curl http://localhost:5000/health
```

### Normal start/stop

```bash
docker compose up -d          # Start services
docker compose logs -f        # Follow logs
docker compose down           # Stop and remove containers
docker compose down -v        # Stop and delete volumes (wipes DB)
```

### Rebuilding after dependency changes

```bash
docker compose build --no-cache dev
docker compose up -d
```

---

## 4. Running Commands Inside Containers

Note the service names — there is no `kvuno` service.

### Database migrations

```bash
# Recommended: the dedicated one-shot service
docker compose run --rm migrate

# Or exec into a running container
docker compose exec dev python scripts/run_migrations.py
docker compose exec dev alembic upgrade head
```

Migrations are **not** applied by the web service at startup; run them as a separate deploy step.

### Housekeeping (RDS ingestion)

Prefer the `worker` service when `HOUSEKEEPING_ENABLED=true` — ingestion is a Celery task and `dev`/`prod` just enqueue it.

```bash
# One-off run inside the API container
docker cp data/file.RDS kvuno-dev:/app/static/data/
docker compose exec dev python housekeeping.py --dry-run
docker compose exec dev python housekeeping.py

# Remote download + process
docker compose exec dev python housekeeping.py /app/static/data
```

### Interactive shell

```bash
docker compose exec dev bash
docker compose exec db psql -U postgres -d agwise_api
docker compose logs -f worker
```

---

## 5. Image Variants

All three use `python:3.14-slim` with a builder stage, and run as a non-root `app` user.

### Dev (`Dockerfile`)

- Installs **all** dependencies (including dev group)
- Runs the Flask dev server via `python run.py`
- Mounts `${DATA_DIR}` at `/app/static/data`

### Production (`Dockerfile.prod.dockerfile`)

- Installs **only** production dependencies (`poetry install --no-root --without dev`)
- Copies code into the image (no bind mount needed for deployment)
- `HEALTHCHECK` polls `http://localhost:5000/health`
- Runs Gunicorn: `gunicorn -b 0.0.0.0:5000 -w 4 --timeout 60 wsgi:app`
  (settings can be overridden via the `app/gunicorn_config.py` env vars: `bind_ip`, `bind_port`, `workers`)

### Worker (`Dockerfile.worker`)

- `celery -A app.celery_app worker --loglevel=info --concurrency=1`
- `restart: unless-stopped`

To build and run the production image standalone:

```bash
docker build -f Dockerfile.prod.dockerfile -t kvuno-api:latest .
docker run -p 5000:5000 --env-file .env kvuno-api:latest
```

---

## 6. Volumes

| Host path | Mount point | Purpose |
|---|---|---|
| `${DATA_DIR:-./data}` | `/app/static/data` | Uploaded + downloaded RDS/Parquet files (shared by `dev`, `prod`, `worker`) |

To inspect or back up the database volume, first find its name:

```bash
docker volume ls | grep db
docker run --rm -v <db-volume>:/data -v $(pwd):/backup alpine tar czf /backup/db.tar.gz -C /data .
```

---

## 7. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Compose exits immediately with `DB_USER is required` | Missing env var | Set `DB_USER`/`DB_PASSWORD`/`DB_NAME` in the shell or `.env` |
| API returns 500 on `/health` | DB connection refused | Check `DB_HOST=db` (not `localhost`); is the `db` container up? |
| `RuntimeError: JWT_SECRET environment variable is required` | `app/config.py` raised at import | Set `JWT_SECRET` — the app cannot import without it |
| `psycopg2.OperationalError` | PostgreSQL not ready yet | Wait a few seconds or add `depends_on` healthcheck |
| Migrations fail with "no such table" | Alembic not yet run | `docker compose run --rm migrate` |
| Uploads accepted but never processed | `HOUSEKEEPING_ENABLED` unset/false and no worker | Set `HOUSEKEEPING_ENABLED=true` and start the `worker` service |
| 500s with "relation does not exist" | App started without migrations | Same as above — the app does **not** migrate on startup |
| Port 5432 already in use | Local PostgreSQL running | Stop local PG or change the host-side port: `ports: - "5433:5432"` |
| Port 5000 already in use | Another service on port 5000 | Stop it or use the `prod` service (port 5001) |

### Wait for PostgreSQL to be ready

`migrate` currently waits only for `service_started`, so on a cold start it can race the database. To prevent that, add a healthcheck to `db` and switch the condition:

```yaml
  migrate:
    depends_on:
      db:
        condition: service_healthy

  db:
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres"]
      interval: 5s
      timeout: 5s
      retries: 5
```

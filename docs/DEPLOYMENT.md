# Deployment Guide

Covers Docker Compose setup, image builds, service configuration, and running management commands in containers.

---

## 1. Architecture

`docker-compose.yml` defines five services (`api-dev` only with `--profile dev`):

| Service | Image | Purpose |
|---|---|---|
| `base` | `ghcr.io/masgeek/python-3.14-poetry:2.3.2` (from `docker/Dockerfile.base`) | Build-only: Python 3.14 + pinned Poetry. Never started; nothing runs in it. |
| `api` | `ghcr.io/cgiar-agwise/kvuno-api:${TAG:-latest}` (built from `docker/Dockerfile.prod.dockerfile`) | Production API: Gunicorn, `FLASK_DEBUG` defaults to `false`, `restart: unless-stopped`. Listens on **80** in the container; compose maps host `127.0.0.1:5000`. Entrypoint runs preflight, migrates if `RUN_MIGRATIONS=true`, then serves. |
| `api-dev` | `ghcr.io/cgiar-agwise/kvuno-api:${DEV_TAG:-develop}` (built from `docker/Dockerfile`) | Profile `dev` only (`docker compose --profile dev up api-dev`): Flask dev server (`run.py`), `FLASK_DEBUG`/`DEBUG_DB` default `true`. Host port `5002`, so it can run beside `api`. |
| `web` | `ghcr.io/cgiar-agwise/kvuno-web` (built from `frontend/Dockerfile`) | nginx: serves the React SPA and reverse-proxies `/api`, `/ui`, `/health` to `api`. Listens on **80** in the container; compose maps host `127.0.0.1:5001` (hardcoded — there is no `WEB_PORT` variable). |
| `worker` | `ghcr.io/cgiar-agwise/kvuno-worker` (built from `docker/Dockerfile.worker`) | Celery ingestion worker. `restart: unless-stopped`; healthcheck is `celery -A app.celery_app inspect ping -d celery@$HOSTNAME` every 60s |

```
                       ┌──────────────────────┐
                       │  Browser → :5001     │
                       └──────────┬───────────┘
                                  │ HTTP
                       ┌──────────▼───────────┐
                       │         web          │
                       │  nginx + React SPA   │
                       └──────────┬───────────┘
                                  │ /api, /ui, /health  (same origin)
┌──────────────┐      port 5432 ┌──▼───────────┐
│  PostgreSQL  │◀───────────────│     api      │
│  + PostGIS   │       DB_HOST  │  Flask API   │
│  (external)  │                │   port 80    │
└──────────────┘                └──────┬───────┘
                                       │
┌──────────────┐                       │
│    redis     │◀───── broker, results ─┴────▶│    worker    │
│  (external)  │      job progress keys      │  Celery task │
└──────────────┘                              └──────────────┘
```

> **The browser talks to `web`, not `api`.** nginx reverse-proxies `/api`, `/ui`, `/health`, and `/api-docs` to the `api` service, so production is same-origin: no CORS preflight and the bearer token never crosses origins. Point users at `http://localhost:5001` (or whatever fronts it); the API port is for direct API calls. Every published port binds to `127.0.0.1` only, so a public deployment needs its own reverse proxy (e.g. Dokploy/Traefik on `dokploy-network`) in front of `web`. nginx must be reachable by the browser for the SPA to load at all — the Flask container alone serves no HTML.

> **Postgres and Redis are no longer compose services.** They are expected to be running elsewhere (or added back via an override file), and `DB_HOST` / `CELERY_BROKER_URL` must point at whatever is reachable. The `api` and `worker` services also no longer declare `depends_on`. If you need the full stack locally, bring your own `postgis/postgis:17-3.5` and `redis:7-alpine`, or add a `docker-compose.override.yml`. (`web` is the exception: it declares `depends_on: api` with `condition: service_healthy`, so nginx does not start until the API's `/health` check passes — which is after migrations have run, when `RUN_MIGRATIONS=true`. The API images define that HEALTHCHECK.)

`api` and `worker` both bind-mount `${DATA_DIR:-./data}` at `/app/static/data`.

> **`base` is a build-time dependency, not a running service.** Build it before the others — the application images `FROM` it and will fail otherwise: `docker compose build base`.

---

## 2. Environment Variables

### `docker-compose.yml` interpolation

The compose file reads these variables from the host environment (or `.env` file in the project root):

| Compose variable | Default | Used by |
|---|---|---|
| `TAG` | `latest` | `api` / `worker` / `web` image tag |
| `DEV_TAG` | `develop` | `api-dev` image tag |
| `DB_HOST` | *(required)* | Postgres host for `api`, `api-dev`, and `worker` — no default, since Postgres is external |
| `FLASK_DEBUG` / `DEBUG_DB` | `false` (`true` for `api-dev`) | Debug mode / SQL echo |
| `RUN_MIGRATIONS` | `false` | Whether the `api` entrypoint migrates on start (see §4) |
| `DATA_DIR` | `./data` | Host directory mounted at `/app/static/data` |
| `DB_NAME` | *(required)* | Database name |
| `DB_USER` | *(required)* | Database user |
| `DB_PASSWORD` | *(required)* | Database password |

`DB_HOST`, `DB_USER`, `DB_PASSWORD`, and `DB_NAME` use `${VAR:?message}` syntax, so Compose **fails fast** if they are missing. Values in the project `.env` count for interpolation, so e.g. `FLASK_DEBUG=true` there (as in `.env.example`) also turns debug on for `api`.

### Env loading per service

| Service | How it gets config |
|---|---|
| `api` / `api-dev` | `env_file: .env`, plus a small `environment:` block (`FLASK_DEBUG`, `DEBUG_DB`, `DB_DRIVER`, `DB_HOST`, `RUN_MIGRATIONS`, `HOUSEKEEPING_ENABLED`) that overrides `.env` |
| `worker` | Explicit `environment:` block with `${VAR:-default}` interpolation — **no `env_file`**, so a worker setting not listed there (e.g. `WORKER_MAX_MEMORY_PER_CHILD`; `CELERY_VISIBILITY_TIMEOUT`, `INGEST_LOCK_STALE_SECONDS` and `REMOTE_RDS_MAX_BYTES` are listed) keeps its code default unless you add it to that block or an override |
| `web` | None — nginx needs no application config; the proxy target is `api:80` in `frontend/nginx.conf` |
| All | `load_dotenv()` at import time, so a mounted/baked `.env` also applies |

### Required values for the app container

```env
# Build the DB URL from parts (app.config.build_db_url)
DB_DRIVER=postgresql
DB_HOST=<postgres-host>      # ← the host running PostGIS; no longer a compose service
DB_PORT=5432
DB_USER=postgres
DB_PASSWORD=postgres
DB_NAME=agwise_api

# Optional: full URL override
# DB_URL=postgresql://postgres:postgres@<postgres-host>:5432/agwise_api

# Self-registration allow-list (exact email domains, case-insensitive).
# UNSET OR EMPTY DISABLES REGISTRATION — POST /api/v1/users/register returns 403
# and nobody can sign up. Existing accounts and login are unaffected.
REGISTRATION_ALLOWED_DOMAINS=cgiar.org
```

> **Set `REGISTRATION_ALLOWED_DOMAINS` before go-live.** Registration is closed by default; matching is on the exact domain after `@`, so list subdomains (`sub.cgiar.org`) separately.

> **Important:** `DB_HOST` must be the *host* of your Postgres/PostGIS instance. Because `db` is no longer a compose service, `localhost` will not work from inside a container either — use the container name, service DNS name, or reachable IP.

---

## 3. Running

### Startup checks (preflight)

Both the API and the worker verify their dependencies before doing any work and exit
non-zero if a **required** one is unreachable, so the container runtime restarts them
instead of leaving an instance that answers every request with an error. The logic is
in `app/utils/preflight.py`; it runs from `docker/entrypoint.sh` (API),
`docker/entrypoint.worker.sh` (worker), and `run.py`.

| Dependency | API | Worker |
|---|---|---|
| Database | always required | always required |
| Redis | only when `HOUSEKEEPING_ENABLED=true` | always required |

A worker with no broker is useless under any configuration, so it always requires Redis.
The API does not: with housekeeping off, progress is stored in PostgreSQL and nothing is
enqueued. On success you will see:

```
[entrypoint] checking dependencies...
[preflight] database: connected
[preflight] all required dependencies ready (api)
```

Tune with `PREFLIGHT_RETRIES` (30), `PREFLIGHT_DELAY_SECONDS` (2) — 60s of tolerance
for a slow-starting database — and `PREFLIGHT_TIMEOUT_SECONDS` (3). Set
`PREFLIGHT_ENABLED=false` to skip entirely.

`run.py` is the deliberate exception: with `FLASK_DEBUG=true` it warns and starts
anyway, so local development is not blocked by a database or Redis that is not running.
Containers still refuse to start, because the entrypoint check runs independently.

> **The worker image now has an entrypoint.** It previously ran `celery` directly as its
> CMD, so a missing broker produced a worker that idled forever — indistinguishable from
> "no work is arriving". `docker/entrypoint.worker.sh` runs the check and then `exec`s
> Celery, so it becomes PID 1 and receives SIGTERM. The worker still never migrates; the
> API owns the schema.

---

### First-time startup

```bash
# Build the shared builder base FIRST — the app images FROM it
docker compose build base

# Build and start all services. Compose defaults RUN_MIGRATIONS to false,
# so either start with RUN_MIGRATIONS=true or migrate once by hand (§4).
docker compose up --build -d
docker compose exec api python scripts/run_migrations.py

# Verify health (through the SPA's nginx, or straight at the API)
curl http://localhost:5001/health
curl http://localhost:5000/health
```

### Running the SPA

`web` is a normal compose service; nothing extra is required.

```bash
docker compose up -d web        # nginx + the prebuilt bundle
docker compose logs -f web
```

In development you can skip the image entirely and run the Vite dev server, which
proxies the same paths to the API — no build, no nginx, hot reload:

```bash
cd frontend
pnpm install
pnpm dev              # Vite on http://localhost:5173
```

Run `python run.py` (or `docker compose up -d api`) first, since the dev server only
proxies — it does not serve the API. `CORS_ORIGINS` already allows the Vite port
(`http://localhost:5173`, `http://127.0.0.1:5173`) by default.

**The dev proxy target is configurable, and the default is easy to get wrong.** It
points at `http://127.0.0.1:5000`, which is where the compose `api` service publishes
its container port 80. Set `VITE_DEV_PROXY_TARGET` in `frontend/.env.local` if your API
listens elsewhere — for example `http://127.0.0.1:80` when you run `python run.py`
directly instead of through compose. A mismatch shows up as Vite logging
`http proxy error: /api/...` with `ECONNREFUSED`; the SPA loads but every API call
fails. See `frontend/.env.example`.

> ### Why nginx does not use an `upstream` block
>
> `frontend/nginx.conf` deliberately avoids `upstream { server api:80; }`. nginx resolves
> an `upstream` **once, when it loads its config**, and Docker assigns the API's IP at
> container start — so a restarted API (new IP) leaves nginx dialling an address where
> nothing is listening. Every request then fails with
> `connect() failed (111: Connection refused)` even though the API is perfectly healthy.
>
> Instead the hostname goes in a variable and nginx re-resolves per request:
>
> ```nginx
> resolver 127.0.0.11 valid=10s ipv6=off;   # Docker's embedded DNS
> set $api_upstream http://api:80;
> proxy_pass $api_upstream$request_uri;
> ```
>
> `$request_uri` is required: when `proxy_pass` contains a variable, nginx stops
> appending the URI automatically, and the paginated endpoints need their query string.
> `127.0.0.11` is correct inside Docker only — change it for a non-Docker deployment.
> This is the single most likely cause of a SPA that loads but whose API calls all
> return 502.

### Normal start/stop

```bash
docker compose up -d          # Start services
docker compose logs -f        # Follow logs
docker compose down           # Stop and remove containers
docker compose down -v        # Stop and delete volumes (wipes DB)
```

### Rebuilding after dependency changes

```bash
# If pyproject.toml / poetry.lock changed, rebuild the base first
docker compose build --no-cache base

docker compose build --no-cache api
docker compose build web       # or `cd frontend && pnpm run build`
docker compose up -d
```

---

## 4. Running Commands Inside Containers

Note the service names — there is no `kvuno` service.

### Database migrations

There is no separate migration service. The `api` image entrypoint (`docker/entrypoint.sh`) runs the preflight, applies pending migrations when `RUN_MIGRATIONS` is `true` — **its own default is `true`**, but `docker-compose.yml` overrides it to `${RUN_MIGRATIONS:-false}` — then execs the server:

```bash
# Manual, when you need to run them outside a container start
docker compose exec api python scripts/run_migrations.py
docker compose exec api alembic upgrade head
```

| `RUN_MIGRATIONS` | Effect |
|---|---|
| `true` | Entrypoint migrates, then starts the server |
| `false` | Skips migration entirely; schema must already be current |

> `docker-compose.yml` currently sets `RUN_MIGRATIONS: ${RUN_MIGRATIONS:-false}`, so **migrations are not applied automatically** — run them manually with `docker compose exec api python scripts/run_migrations.py`. Flip it to `true` to let the `api` container migrate on start.

> **Upgrading past revision `f3a9c2d4e5b6`** (`20261022130000_planting_unique_nulls_not_distinct.py`) changes data, not just schema. On PostgreSQL ≥ 15 it **deletes** rows of `planting_recommendations` that duplicate another row on the seven key columns (`country`, `province`, `lon`, `lat`, `variety`, `season_type`, `opt_date`) with NULLs compared as equal, keeping the lowest `id`, and prints the count; then it recreates `uq_planting_recommendation` as `UNIQUE NULLS NOT DISTINCT`, so rows with a NULL key column stop being re-inserted on every replay. The downgrade restores the plain constraint but **not** the deleted rows. On SQLite or PostgreSQL < 15 it is a no-op (logged). Because compose does not migrate by default, run it explicitly after pulling this release — and back up first if you need the duplicates.

**Set it to `false` when you run more than one API replica**, otherwise each replica races to migrate the same schema — concurrent `alembic upgrade head` calls can deadlock or fail. This is the reason startup migrations were originally removed in favour of a separate step.

The `worker` never migrates. It only receives tasks from the already-running API, and `process_pending()` is enqueued from `create_app()`. A task already sitting in the broker when the worker restarts could execute before the API migrates. Celery's `autoretry_for=(Exception,)` (10 retries at 60s) only covers that window for **transient** database errors (connection refused, dropped connection): `process_file` now records any other failure — including "relation does not exist" — as an `error` job and returns, so it is not retried. Migrate before starting the worker, and use the Jobs page Retry for anything that failed in between. `depends_on: api` would **not** help — the API container counts as started the moment its entrypoint begins, which is before migrations finish.

### Housekeeping (RDS ingestion)

Prefer the `worker` service when `HOUSEKEEPING_ENABLED=true` — ingestion is a Celery task and `api` just enqueues it.

```bash
# One-off run inside the API container
docker cp data/file.RDS kvuno-api:/app/static/data/
docker compose exec api python housekeeping.py --dry-run
docker compose exec api python housekeeping.py
```

### Interactive shell

```bash
docker compose exec api bash
docker compose logs -f worker
docker compose logs -f web
```

---

## 5. Image Variants

Four Dockerfiles live in `docker/`, and a fifth (`frontend/Dockerfile`) builds the SPA. The three Python application images share a builder base; none of them install Poetry themselves. The build context for those is still the **repo root** — only the Dockerfile paths changed — so `docker-compose.yml` and `.dockerignore` remain at the root, and `COPY` paths inside the Dockerfiles are unchanged. The SPA image is the exception: `frontend/Dockerfile` builds from the **`frontend/`** context with node and pnpm, and its runtime stage is nginx.

The base installs Poetry with the official installer
(`https://install.python-poetry.org`), pinned to `ARG POETRY_VERSION`, and
verifies the resolved version in the same layer. `curl` is installed only for
that step and purged again in the same layer.

> `.dockerignore` cannot move into `docker/`: Docker only reads it from the build-context root. Moving it would silently disable every rule, including the `!README.md` negation that `poetry install` depends on.

| Dockerfile | Output | Notes |
|---|---|---|
| `docker/Dockerfile.base` | `ghcr.io/masgeek/python-3.14-poetry:<poetry-version>` | `python:3.14-slim` + Poetry via the official installer into `POETRY_HOME=/opt/poetry`. **Not built by CI** — build locally. |
| `docker/Dockerfile` | `ghcr.io/cgiar-agwise/kvuno-api` (dev) | Installs `--only main`; runs `python3 run.py`. Compose service `api-dev` |
| `docker/Dockerfile.prod.dockerfile` | same image name, prod build | Installs `--only main`; runs Gunicorn; adds a `HEALTHCHECK`. Compose service `api` |
| `docker/Dockerfile.worker` | `ghcr.io/cgiar-agwise/kvuno-worker` | Runs the Celery worker |
| `frontend/Dockerfile` | `ghcr.io/cgiar-agwise/kvuno-web` | `node:24-alpine` build stage (pnpm + `tsc -b && vite build`) → `nginx:1.27-alpine` runtime with `frontend/nginx.conf`. Does **not** use the Python base. |

The dev and prod images share a base and differ only in dependency flags, CMD, and the `HEALTHCHECK`. Both use the same `docker/entrypoint.sh`, which applies migrations and then `exec`s the image CMD — there is no mode variable to set at build or run time.

```bash
docker build -f docker/Dockerfile              -t kvuno-api:dev .
docker build -f docker/Dockerfile.prod.dockerfile -t kvuno-api:prod .
```

Each application image starts with:

```dockerfile
ARG POETRY_VERSION=2.3.2
FROM ghcr.io/masgeek/python-3.14-poetry:${POETRY_VERSION} AS builder
```

### Building the base image first

The application images will **not** build until the base image is available — locally in the image store, or already pushed to GHCR. Otherwise Docker tries to pull the tag from the registry and fails.

```bash
# Build locally
docker compose build base

# Optional: push so other projects (and CI images) can pull it
docker push ghcr.io/masgeek/python-3.14-poetry:2.3.2
```

The tag in the `base` service of `docker-compose.yml` must exactly match the `${POETRY_VERSION}` the application Dockerfiles reference.

> The base image lives under the `masgeek` namespace, while the application images are `ghcr.io/cgiar-agwise/*`. That is intentional — the base is a general Python + Poetry image reusable outside this repo, not a kvuno artefact. Pulling it therefore depends on the `masgeek` package's visibility.

### Bumping Poetry

> ⚠️ `poetry.lock` is only valid for the Poetry release that generated it. Different 2.x releases compute a different `content-hash` for `pyproject.toml`, and `poetry install` aborts with *"pyproject.toml changed significantly since poetry.lock was last generated"*.

There is no automated check for this, so a bump means editing **all** of these in one commit:

1. `docker/Dockerfile.base` — `ARG POETRY_VERSION`
2. `docker/Dockerfile` — `ARG POETRY_VERSION`
3. `docker/Dockerfile.prod.dockerfile` — `ARG POETRY_VERSION`
4. `docker/Dockerfile.worker` — `ARG POETRY_VERSION`
5. `docker-compose.yml` — `base` service `image:` tag **and** `POETRY_VERSION:` build arg
6. `.github/workflows/pr-checks.yml` — `version:` on both `snok/install-poetry` steps (lint and test jobs)

Then regenerate the lock using that same Poetry, and rebuild the base image. Missing any one of them produces an error that looks nothing like a Poetry problem.

> Note: `pip install poetry==2` does **not** mean "latest 2.x" — it resolves to 2.0.0 and silently disagrees with a lock generated by a newer release. The base image avoids that trap by pinning the exact version and asserting it in the same layer:

```dockerfile
# pipefail: a failed curl must fail the build, not feed empty input to python.
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

RUN apt-get update     && apt-get install -y --no-install-recommends ca-certificates curl     && curl -fsSL https://install.python-poetry.org | python3 - --version "${POETRY_VERSION}"     && apt-get purge -y --auto-remove curl     && rm -rf /var/lib/apt/lists/*     && installed="$(poetry --version)"     && case "${installed}" in *"version ${POETRY_VERSION})"*) ;;          *) echo "expected poetry ${POETRY_VERSION}, got: ${installed}" >&2; exit 1 ;; esac
```

### Builder stage requirements

The builder copies `pyproject.toml`, `poetry.lock`, and `README.md`:

- **`poetry.lock`** — without it the build re-resolves dependencies on every image and drifts from CI.
- **`README.md`** — `pyproject.toml` declares `readme = "README.md"` and Poetry validates that the file exists at install time. This is why `.dockerignore` ends with `!README.md`; the negation **must** come after the `*.md` rule it overrides, or the `COPY` fails.

### Runtime

All application images use `python:3.14-slim` for the runtime stage and run as a non-root `app` user.

**Dev (`docker/Dockerfile`)**
- Installs runtime dependencies (`poetry install --only main`)
- Runs `python3 run.py` (Flask dev server)
- Mounts `${DATA_DIR}` at `/app/static/data` in compose

**Production (`docker/Dockerfile.prod.dockerfile`)**
- Installs runtime dependencies (`poetry install --only main`)
- `HEALTHCHECK` polls `http://localhost:80/health`
- Runs `gunicorn -c app/gunicorn_config.py wsgi:app`, so the config file is genuinely loaded and its env vars apply: `BIND_IP` (`0.0.0.0`), `BIND_PORT` (`80`), `WORKER_CLASS` (`gthread`), `WORKERS` (`2`), `THREADS` (`4`), `TIMEOUT` (`120`), `GRACEFUL_TIMEOUT`, `KEEPALIVE`, `MAX_REQUESTS`, `WORKER_TMP_DIR` (`/dev/shm`), `ACCESSLOG`/`ERRORLOG` (`-`, i.e. stdout/stderr)

`gthread` is the default worker class because `/ui/jobs/events` is an indefinite SSE stream: a `sync` worker handles one request at a time, so open Jobs pages would exhaust the pool and stall the API.

Gunicorn boots a fresh worker on every `MAX_REQUESTS` recycle and every timeout, and each boot runs `create_app()`'s cleanup. That cleanup only removes uploads idle longer than `UPLOAD_ABANDON_AGE` and files of jobs completed longer than `CLEANUP_AGE` ago, and it logs a warning and skips (rather than failing the boot) when it cannot read uploads or jobs — Redis down with the tables not yet migrated — it used to wipe the whole chunk directory, which is what made in-flight uploads fail at random. Upload inspection runs on a thread in the API worker, so a recycle can interrupt it; it is re-claimed by the next status poll after `UPLOAD_INSPECT_STALE_SECONDS`.

**Worker (`docker/Dockerfile.worker`)**
- Entrypoint `docker/entrypoint.worker.sh` runs the preflight (`scripts/preflight.py worker`: database + Redis) and never migrates
- `celery -A app.celery_app worker --loglevel=info --concurrency=1`
- `restart: unless-stopped`; compose healthcheck `celery inspect ping` against its own node
- `task_acks_late` + `task_reject_on_worker_lost=True`: a worker killed mid-ingest (OOM, container stop) puts the task back on the queue, and the retry resumes from the stored offset. Redis redelivers any unacked task after `CELERY_VISIBILITY_TIMEOUT` (default 21600 s) **even if it is still running**, so set it above the longest single-file ingest; the per-file `.ingest.lock` stops a redelivered copy from ingesting concurrently, but the copy is wasted work
- Celery's own SIGTERM handling (warm shutdown) is untouched — the housekeeper's graceful-shutdown handlers are installed only by `python housekeeping.py`

**Web (`frontend/Dockerfile`)**
- Build stage: `node:24-alpine` with pnpm enabled via corepack; `pnpm run build` runs `tsc -b` first, so a type error fails the image build instead of shipping a broken bundle
- Runtime stage: `nginx:1.27-alpine` serving `dist/` plus `frontend/nginx.conf` and `frontend/security-headers.conf` (copied to `/etc/nginx/snippets/`)
- SPA security headers (CSP with no CDN hosts, `X-Frame-Options`, `nosniff`, `Referrer-Policy`) come from `security-headers.conf`, **included in each SPA `location`** rather than at `server` level: a location that sets its own `add_header` (the `Cache-Control` ones do) inherits none from above, so server-level headers would silently vanish. The proxied `/api-docs` deliberately gets none — Swagger UI needs inline script, and the API sets its own headers
- Source maps are built `hidden` and `*.map` returns `404`, so the original source is never published
- `/api/` has `client_max_body_size 0` (the API enforces `MAX_FILE_SIZE_MB`); `POST /api/v1/data/upload` gets 600 s read/send timeouts because it converts RDS → Parquet inside the request. `/api-docs` is proxied (spec at `/api-docs/openapi.json`; there is no root `/openapi.json`)
- `HEALTHCHECK` spiders `http://localhost/`
- No node in the final image — only nginx and a few hundred KB of static assets
- `depends_on: api` in compose; the upstream is hardcoded to `api:80`, which is the compose service name (change it for a non-compose deploy)

To build and run the production image standalone (base image must already be built):

```bash
docker build -f docker/Dockerfile.prod.dockerfile -t kvuno-api:latest .
# --cap-add NET_BIND_SERVICE: the app runs as non-root and port 80 is privileged
docker run -p 5000:80 --cap-add NET_BIND_SERVICE --env-file .env kvuno-api:latest
```

---

## 6. Volumes

| Host path | Mount point | Purpose |
|---|---|---|
| `${DATA_DIR:-./data}` | `/app/static/data` | Uploaded + downloaded RDS/Parquet files (shared by `api` and `worker`) |

SPA uploads land in `uploads/<id>/` under that mount (`data.part` while receiving, then `<id>.rds` / `<id>.parquet`), and their state is in the `uploads` table — so the volume and the database must stay together, and the `worker` must see the same path the `api` wrote. It is a subdirectory on purpose: the startup scan ingests every file in the mount's root with the default mapping.

There is no Postgres volume to back up here — the database is external (see §1).

---

## 7. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Compose exits immediately with `DB_USER is required` | Missing env var | Set `DB_USER`/`DB_PASSWORD`/`DB_NAME` in the shell or `.env` |
| API returns 500 on `/health` | DB connection refused | Check `DB_HOST` resolves from inside the container — `db` is no longer a compose service |
| `psycopg2.OperationalError` | Postgres unreachable or not ready | Verify host/port/credentials; there is no `depends_on` to wait for |
| Migrations fail with "no such table" | Alembic has not run against this database | Check the api container logs for the `[entrypoint] applying database migrations` line |
| Uploads accepted but never processed | `HOUSEKEEPING_ENABLED` unset/false and no worker | Set `HOUSEKEEPING_ENABLED=true` and start the `worker` service |
| 500s with "relation does not exist" | `RUN_MIGRATIONS=false`, or the DB was unreachable so the entrypoint skipped | `docker compose exec api python scripts/run_migrations.py` |
| `ImportError: libpq` / `could not find libpq` at runtime | Plain `psycopg` needs system libpq, absent from `python:3.14-slim` | Depend on `psycopg[binary]`, or install libpq in the runtime image |
| Port 5000 already in use on the host | Another service on host port 5000 | Stop it, or remap the host side in an override: `ports: - "127.0.0.1:5003:80"` (5001 and 5002 are taken by `web` and `api-dev`) |
| Port 5001 already in use on the host | Another service on the `web` host port | The mapping is hardcoded (`127.0.0.1:5001:80`); change it in a `docker-compose.override.yml` |
| Sign-up fails with `403` "registration is disabled" | `REGISTRATION_ALLOWED_DOMAINS` unset or empty — registration is closed by default | Set it on the `api` (e.g. `cgiar.org,iita.org`); a different domain gets `403` "not open to this email domain" |
| Worker logs "already being ingested by another worker" | A second copy of the task (double submit, or Redis redelivery after `CELERY_VISIBILITY_TIMEOUT`) found the live `<file>.ingest.lock` | Harmless — the duplicate exits. If it happens on long files, raise `CELERY_VISIBILITY_TIMEOUT` (default 21600 s) above the longest ingest. A lock untouched for `INGEST_LOCK_STALE_SECONDS` (600) is taken over |
| API restarted but the startup scan did not run | `process_pending()` claims the Redis key `kvuno:process_pending` (`SET NX EX 300`) so only one gunicorn worker enqueues it; a restart within 5 minutes skips it | Wait 5 minutes, or delete the key, or run `python housekeeping.py` |
| `Permission denied` binding, app exits at startup | Container runs as non-root and port 80 is privileged | Add `cap_add: [NET_BIND_SERVICE]` (compose already does for `api`) |
| `denied: requested access to the resource is denied` on pull | Package is private or you are not logged in | `docker login ghcr.io` with a token that has `read:packages` |
| `manifest unknown` on pull | Tag not built yet, or wrong `TAG` | Tags come from the Docker Build workflow; check what exists under the `CGIAR-AgWise` org |
| `pull access denied` / `manifest unknown` for `python-3.14-poetry` | Base image not built locally and the tag is not in the registry (or is private) | `docker compose build base`, or `docker login ghcr.io` and retry the pull |
| `pyproject.toml changed significantly` during build | Poetry version does not match the one that generated the lock | See [Bumping Poetry](#bumping-poetry) — five places to update |
| `Declared README file does not exist` during build | `!README.md` missing from `.dockerignore` | The negation must follow the `*.md` rule |
| Upload refused with `413` before it starts | The declared `Upload-Length` exceeds `MAX_FILE_SIZE_MB` (default 20) | Raise `MAX_FILE_SIZE_MB` on the API; the SPA picks the new limit up from `/ui/columns`. nginx sets `client_max_body_size 0` on `/ui/` and does not cap it |
| Upload `PATCH` returns `423` | Another request still holds that upload's `.lock` (a retry arrived while the previous one was draining) | Nothing — the client retries; a lock with no write for 30s is taken over |
| Upload stuck on *Reading the file…* | The API worker running inspection was recycled mid-inspection | The next status poll re-claims it once the heartbeat is older than `UPLOAD_INSPECT_STALE_SECONDS` (default 180) |
| Upload submit returns `503` | `HOUSEKEEPING_ENABLED=true` but the broker is unreachable | Start Redis / check `CELERY_BROKER_URL`; the upload stays `ready` with its mapping, so submit again |
| Uploads disappear after a week | Unsubmitted uploads idle longer than `UPLOAD_ABANDON_AGE` (default `7d`) are deleted at API worker boot | Raise `UPLOAD_ABANDON_AGE`, or submit them |
| Upload's `file` is `<id>.parquet` when `.rds` went in | Inspection converts RDS to Parquet so ingestion reads the cheap format | Expected; it is the name the worker reads and the one the Jobs page shows. `/api/v1/data/upload` converts the same way |
| A job shows as *Processing* indefinitely | The worker died (OOM, restart, crash) and never wrote a terminal status | It becomes *Stalled* after `JOB_STALE_AFTER_SECONDS` (default 1800) and offers Retry. A job that stays Processing past that is still being written to |
| Worker container restarts during an ingest | Exceeded `WORKER_MEM_LIMIT` | Raise it, or check whether the file is unusually large for its format |
| Entrypoint fails with `not found` | `docker/entrypoint.sh` checked out with CRLF | `.gitattributes` pins `*.sh` to LF; re-checkout after it is committed |
| Page 404s or shows JSON at `/jobs`, `/explore`, … | You are on the API port; Flask serves no HTML | Use the `web` port (`http://localhost:5001`) — nginx serves the SPA and falls back to `index.html` for client routes |
| SPA loads but every API call fails | nginx cannot reach the upstream `api:80` | Check `docker compose logs web` and that the `api` service is on the same network; the upstream host is the compose service name |
| CORS error in the browser | SPA and API on different origins (usually Vite dev without the proxy) | Use the proxy (`pnpm dev`, or nginx in compose) or add the origin to `CORS_ORIGINS` |
| `denied: permission_denied: read_package` on push | The workflow's `GITHUB_TOKEN` cannot write the package | With a `push` trigger the token keeps `packages: write`. If it still fails, the package already exists with its own access rules — in the package settings set access to inherit from the repository, or add this repository explicitly |

---

## 8. Version Reporting

`/health` and the OpenAPI `Info` block report `APP_VERSION`, injected at build
time as a Docker build arg. Nothing in the source hardcodes a version number:

| Build | `APP_VERSION` | Example |
|---|---|---|
| Release (tag) | `${{ github.ref_name }}` | `1.4.2` |
| `main` | `${{ github.ref_name }}` | `main` |
| `develop` | `${{ github.ref_name }}-dev` | `develop-dev` |
| Local `docker compose build` | `ARG` default | `0.0.0-dev` |

```bash
curl http://localhost:5000/health   # {"status":..., "version":"1.4.2"}
```

Tags come from `masgeek/github-tag-action`, which computes the next version
from the previous tag plus conventional commit messages. It has no
`version_file` input — the git tag is the source of truth, so there is no file
to keep in sync. `app/config.py` falls back to `GITHUB_REF_NAME`, then
`0.0.0-dev`.

---

## 9. Container Registry (GHCR)

Images are published to **GitHub Container Registry**, not Docker Hub:

Every image (`kvuno-api`, `kvuno-worker`, `kvuno-web`) is built on each path, and each build also gets an immutable `:sha-<short>` tag to roll back to:

| Trigger | API Dockerfile | Tags |
|---|---|---|
| push to `develop` (after `UnitTests` passes) | `docker/Dockerfile` (dev server) | `:develop`, `:sha-<short>` |
| push to `main` (after `UnitTests` passes) | `docker/Dockerfile.prod.dockerfile` | `:production`, `:sha-<short>` |
| release tag (`version-release.yml`, or a tag pushed by hand) | `docker/Dockerfile.prod.dockerfile` | `:latest`, `:<tag>`, `:sha-<short>` |
| manual `workflow_dispatch` on a branch | dev | `:<branch-with-dashes>`, `:sha-<short>` |

`:latest` now means **latest release** — only release tags move it. The worker always uses `docker/Dockerfile.worker` and the web image `frontend/Dockerfile` (built with `frontend/` as its context). ⚠️ `:develop` runs the Flask dev server; don't deploy it anywhere shared.

### Build workflows

| Workflow | Trigger | Does |
|---|---|---|
| `pr-checks.yml` (`UnitTests`) | PRs to `develop`/`main`; pushes to `develop`/`main` | lint, frontend build, tests on 3.13 + 3.14; on PRs also builds all four Dockerfiles **without pushing** |
| `docker-build.yml` | `workflow_run` of `UnitTests`; `push: tags`; `workflow_call`; `workflow_dispatch` | picks the profile and tags, gates, calls the reusable build per image |
| `docker-build-reusable.yml` | `workflow_call` | builds and pushes one image |
| `version-release.yml` | push to `main` | creates the next semver tag + GitHub release, then **calls** `docker-build.yml` with that tag |

Rules the pipeline relies on — each was a real failure:

- **Only a successful *push* run of `UnitTests` on `develop`/`main` builds.** `docker-build` checks `workflow_run.conclusion == 'success'` (`completed` also fires on failure) and `workflow_run.event == 'push'`. PR runs are ignored: `workflow_run` executes with this repo's `packages: write`, so building a PR head (possibly a fork) would publish untrusted code, and it used to push every feature branch as `:develop`. This is why `pr-checks.yml` **must** keep its `develop`/`main` push trigger.
- **Releases are built by `version-release.yml` calling `docker-build.yml`.** A tag pushed with `GITHUB_TOKEN` never triggers another workflow, so `push: tags` alone never built an automated release. Tags pushed by a person still arrive via `push: tags`.
- **Tag builds pass a gate.** A tag push runs no tests itself, so the `gate` job uses `.github/actions/wait-for-checks` to require that `UnitTests` passed **for that exact commit** (from its branch push). Failure, cancellation, or no run within 30 min fails the gate and skips the images.
- **Tags are expanded per image** in the reusable workflow (`img:latest,img:1.2.3,…`). Passing `img:latest,1.2.3,production` made `build-push-action` treat `1.2.3` as a Docker Hub image and fail.
- **Only develop builds are cancelled by a newer push**; main and release builds always finish, so `:production`/`:latest` never point at a half-published set.
- There is no "skip if source unchanged" step any more: its cache key ignored the profile, so a develop build made the release build of the same code skip. The registry cache (below) keeps unchanged rebuilds fast instead.
> `ghcr.io/masgeek/python-3.14-poetry` is **not** in this list: no CI workflow builds or pushes it. It is a general-purpose image, so it lives in the `masgeek` namespace rather than the `cgiar-agwise` one used by the kvuno images. Build it with `docker compose build base` and push it yourself if you want it available to others.

### Authentication

CI authenticates with the workflow's built-in `GITHUB_TOKEN` (the jobs declare `permissions: packages: write`), so **no `DOCKER_USERNAME` / `DOCKER_PASSWORD` repository secrets are needed**. Those secrets can be deleted from the repo settings.

To pull the images on a server or workstation, create a classic PAT with `read:packages` scope:

```bash
echo "$GHCR_TOKEN" | docker login ghcr.io -u <github-username> --password-stdin
```

Packages are private by default. To make them public, set each package to public visibility in the GitHub org settings (or via the API); public packages can be pulled anonymously.

Build cache is stored in the same registry as `<image>:cache-<branch>` (`cache-develop`, `cache-main`; releases share `cache-main`), falling back to `cache-develop` on a first build. It used to be keyed by commit SHA, which never hit and left a cache image per commit in GHCR. PR builds use the GitHub Actions cache instead and push nothing.

### Referencing in compose

Override the image without editing the tracked file:

```bash
TAG=production docker compose up -d
```

or add a `docker-compose.override.yml` pinning the image and credentials. Note that a *private* GHCR package requires the host to be logged in (`docker login ghcr.io`) before `docker compose pull` will work.

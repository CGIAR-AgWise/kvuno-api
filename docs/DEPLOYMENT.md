# Deployment Guide

Covers Docker Compose setup, image builds, service configuration, and running management commands in containers.

---

## 1. Architecture

`docker-compose.yml` defines four services:

| Service | Image | Purpose |
|---|---|---|
| `base` | `ghcr.io/masgeek/python-3.14-poetry:2.3.2` (from `docker/Dockerfile.base`) | Build-only: Python 3.14 + pinned Poetry. Never started; nothing runs in it. |
| `api` | `ghcr.io/cgiar-agwise/kvuno-api` (built from `docker/Dockerfile`) | Flask dev server on port 5000 |
| `worker` | `ghcr.io/cgiar-agwise/kvuno-worker` (built from `docker/Dockerfile.worker`) | Celery ingestion worker |
| `migrate` | `ghcr.io/cgiar-agwise/kvuno-api` | One-shot `scripts/run_migrations.py` run, then exits |

```
┌──────────────┐      port 5432     ┌──────────────┐
│  PostgreSQL  │◀───────────────────│     api      │
│  + PostGIS   │   DB_HOST=db       │  Flask API   │
│  (external)  │                    │   port 5000  │
└──────────────┘                    └──────┬───────┘
                                             │
┌──────────────┐                              │
│    redis     │◀───── broker, results ─────▶│    worker    │
│  (external)  │      job progress keys      │  Celery task │
└──────────────┘                              └──────────────┘
```

> **Postgres and Redis are no longer compose services.** They are expected to be running elsewhere (or added back via an override file), and `DB_HOST` / `CELERY_BROKER_URL` must point at whatever is reachable. The services also no longer declare `depends_on`. If you need the full stack locally, bring your own `postgis/postgis:17-3.5` and `redis:7-alpine`, or add a `docker-compose.override.yml`.

`api`, `worker`, and `migrate` all bind-mount `${DATA_DIR:-./data}` at `/app/static/data`.

> **`base` is a build-time dependency, not a running service.** Build it before the others — the application images `FROM` it and will fail otherwise: `docker compose build base`.

---

## 2. Environment Variables

### `docker-compose.yml` interpolation

The compose file reads these variables from the host environment (or `.env` file in the project root):

| Compose variable | Default | Used by |
|---|---|---|
| `TAG` | `latest` | `api` / `worker` / `migrate` image tag |
| `DATA_DIR` | `./data` | Host directory mounted at `/app/static/data` |
| `DB_NAME` | *(required)* | Database name |
| `DB_USER` | *(required)* | Database user |
| `DB_PASSWORD` | *(required)* | Database password |

`DB_USER`, `DB_PASSWORD`, and `DB_NAME` use `${VAR:?message}` syntax, so Compose **fails fast** if they are missing.

### Env loading per service

| Service | How it gets config |
|---|---|
| `api` | `env_file: .env` (the explicit `environment:` block is commented out) |
| `worker`, `migrate` | Explicit `environment:` blocks with `${VAR:-default}` interpolation |
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
```

> **Important:** `DB_HOST` must be the *host* of your Postgres/PostGIS instance. Because `db` is no longer a compose service, `localhost` will not work from inside a container either — use the container name, service DNS name, or reachable IP.

---

## 3. Running

### First-time startup

```bash
# Build the shared builder base FIRST — the app images FROM it
docker compose build base

# Build and start all services in the background
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
# If pyproject.toml / poetry.lock changed, rebuild the base first
docker compose build --no-cache base

docker compose build --no-cache api
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
docker compose exec api python scripts/run_migrations.py
docker compose exec api alembic upgrade head
```

Migrations are **not** applied by the web service at startup; run them as a separate deploy step.

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
```

---

## 5. Image Variants

All four Dockerfiles live in `docker/`. The three application images share a builder base; none of them install Poetry themselves. The build context is still the **repo root** — only the Dockerfile paths changed — so `docker-compose.yml` and `.dockerignore` remain at the root, and `COPY` paths inside the Dockerfiles are unchanged.

The base installs Poetry with `uv tool install` (pinned by `ARG UV_VERSION`, currently 0.9.0) rather than `pip`. `UV_VERSION` is independent of `poetry.lock` and can be bumped freely; `POETRY_VERSION` cannot.

> `.dockerignore` cannot move into `docker/`: Docker only reads it from the build-context root. Moving it would silently disable every rule, including the `!README.md` negation that `poetry install` depends on.

| Dockerfile | Output | Notes |
|---|---|---|
| `docker/Dockerfile.base` | `ghcr.io/masgeek/python-3.14-poetry:<poetry-version>` | `python:3.14-slim` + Poetry installed via `uv tool install` into an isolated `/opt/uv-tools` venv. **Not built by CI** — build locally. |
| `docker/Dockerfile` | `ghcr.io/cgiar-agwise/kvuno-api` (dev) | All dependencies; runs `python run.py` |
| `docker/Dockerfile.prod.dockerfile` | same image, prod build | `--without dev`; runs Gunicorn |
| `docker/Dockerfile.worker` | `ghcr.io/cgiar-agwise/kvuno-worker` | `--without dev`; runs the Celery worker |

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

There is no automated check for this, so a bump means editing **five** places in one commit:

1. `docker/Dockerfile.base` — `ARG POETRY_VERSION`
2. `docker/Dockerfile` — `ARG POETRY_VERSION`
3. `docker/Dockerfile.prod.dockerfile` — `ARG POETRY_VERSION`
4. `docker/Dockerfile.worker` — `ARG POETRY_VERSION`
5. `docker-compose.yml` — `base` service `image:` tag **and** `POETRY_VERSION:` build arg

Then regenerate the lock using that same Poetry, and rebuild the base image. Missing any one of them produces an error that looks nothing like a Poetry problem.

> Note: `pip install poetry==2` does **not** mean "latest 2.x" — it resolves to 2.0.0 and silently disagrees with a lock generated by a newer release. The base image avoids that trap by pinning the exact version and asserting it in the same layer:

```dockerfile
RUN --mount=type=cache,target=/root/.cache/uv     uv tool install "poetry==${POETRY_VERSION}"     && installed="$(poetry --version)"     && case "${installed}" in *"version ${POETRY_VERSION})"*) ;;          *) echo "expected poetry ${POETRY_VERSION}, got: ${installed}" >&2; exit 1 ;; esac
```

### Builder stage requirements

The builder copies `pyproject.toml`, `poetry.lock`, and `README.md`:

- **`poetry.lock`** — without it the build re-resolves dependencies on every image and drifts from CI.
- **`README.md`** — `pyproject.toml` declares `readme = "README.md"` and Poetry validates that the file exists at install time. This is why `.dockerignore` ends with `!README.md`; the negation **must** come after the `*.md` rule it overrides, or the `COPY` fails.

### Runtime

All application images use `python:3.14-slim` for the runtime stage and run as a non-root `app` user.

**Dev (`docker/Dockerfile`)**
- Installs **all** dependencies (including dev group)
- Mounts `${DATA_DIR}` at `/app/static/data`

**Production (`docker/Dockerfile.prod.dockerfile`)**
- Installs **only** production dependencies (`poetry install --no-root --without dev`)
- `HEALTHCHECK` polls `http://localhost:5000/health`
- Runs Gunicorn: `gunicorn -b 0.0.0.0:5000 -w 4 --timeout 60 wsgi:app`
  (overridable via `app/gunicorn_config.py` env vars: `bind_ip`, `bind_port`, `workers`)

**Worker (`docker/Dockerfile.worker`)**
- `celery -A app.celery_app worker --loglevel=info --concurrency=1`
- `restart: unless-stopped`

To build and run the production image standalone (base image must already be built):

```bash
docker build -f docker/Dockerfile.prod.dockerfile -t kvuno-api:latest .
docker run -p 5000:5000 --env-file .env kvuno-api:latest
```

---

## 6. Volumes

| Host path | Mount point | Purpose |
|---|---|---|
| `${DATA_DIR:-./data}` | `/app/static/data` | Uploaded + downloaded RDS/Parquet files (shared by `api`, `worker`, `migrate`) |

There is no Postgres volume to back up here — the database is external (see §1).

---

## 7. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Compose exits immediately with `DB_USER is required` | Missing env var | Set `DB_USER`/`DB_PASSWORD`/`DB_NAME` in the shell or `.env` |
| API returns 500 on `/health` | DB connection refused | Check `DB_HOST` resolves from inside the container — `db` is no longer a compose service |
| `psycopg2.OperationalError` | Postgres unreachable or not ready | Verify host/port/credentials; there is no `depends_on` to wait for |
| Migrations fail with "no such table" | Alembic not yet run | `docker compose run --rm migrate` |
| Uploads accepted but never processed | `HOUSEKEEPING_ENABLED` unset/false and no worker | Set `HOUSEKEEPING_ENABLED=true` and start the `worker` service |
| 500s with "relation does not exist" | App started without migrations | Same as above — the app does **not** migrate on startup |
| Port 5000 already in use | Another service on port 5000 | Stop it, or remap with `ports: - "5001:5000"` |
| `denied: requested access to the resource is denied` on pull | Package is private or you are not logged in | `docker login ghcr.io` with a token that has `read:packages` |
| `manifest unknown` on pull | Tag not built yet, or wrong `TAG` | Tags come from the Docker Build workflow; check what exists under the `CGIAR-AgWise` org |
| `pull access denied` / `manifest unknown` for `python-3.14-poetry` | Base image not built locally and the tag is not in the registry (or is private) | `docker compose build base`, or `docker login ghcr.io` and retry the pull |
| `pyproject.toml changed significantly` during build | Poetry version does not match the one that generated the lock | See [Bumping Poetry](#bumping-poetry) — five places to update |
| `Declared README file does not exist` during build | `!README.md` missing from `.dockerignore` | The negation must follow the `*.md` rule |

---

## 8. Container Registry (GHCR)

Images are published to **GitHub Container Registry**, not Docker Hub:

| Image | Source Dockerfile | Built when |
|---|---|---|
| `ghcr.io/cgiar-agwise/kvuno-api` | `docker/Dockerfile` (dev) | `develop` pushes after PR Checks pass |
| `ghcr.io/cgiar-agwise/kvuno-api` | `docker/Dockerfile.prod.dockerfile` (prod) | `main` pushes after PR Checks pass |
| `ghcr.io/cgiar-agwise/kvuno-worker` | `docker/Dockerfile.worker` | `main`, `develop`, or `beta/*` |

> `ghcr.io/masgeek/python-3.14-poetry` is **not** in this list: no CI workflow builds or pushes it. It is a general-purpose image, so it lives in the `masgeek` namespace rather than the `cgiar-agwise` one used by the kvuno images. Build it with `docker compose build base` and push it yourself if you want it available to others.

### Authentication

CI authenticates with the workflow's built-in `GITHUB_TOKEN` (the jobs declare `permissions: packages: write`), so **no `DOCKER_USERNAME` / `DOCKER_PASSWORD` repository secrets are needed**. Those secrets can be deleted from the repo settings.

To pull the images on a server or workstation, create a classic PAT with `read:packages` scope:

```bash
echo "$GHCR_TOKEN" | docker login ghcr.io -u <github-username> --password-stdin
```

Packages are private by default. To make them public, set each package to public visibility in the GitHub org settings (or via the API); public packages can be pulled anonymously.

### Tags

Tags are generated by `.github/actions/set-docker-tags`:

| Ref | Tags pushed |
|---|---|
| `refs/tags/<vX.Y.Z>` | `:latest`, `:<vX.Y.Z>` |
| `refs/heads/main` | `:latest`, `:production` |
| `refs/heads/develop` | `:latest` only |
| any other branch | `:latest`, `:<branch-with-slashes-replaced-by-dashes>` |

Build cache is stored in the same registry under `:<image>:cache-<branch>`.

### Referencing in compose

Override the image without editing the tracked file:

```bash
TAG=production docker compose up -d
```

or add a `docker-compose.override.yml` pinning the image and credentials. Note that a *private* GHCR package requires the host to be logged in (`docker login ghcr.io`) before `docker compose pull` will work.

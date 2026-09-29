# Deployment Guide

Covers Docker Compose setup, image builds, service configuration, and running management commands in containers.

---

## 1. Architecture

`docker-compose.yml` defines three services:

| Service | Image | Purpose |
|---|---|---|
| `base` | `ghcr.io/masgeek/python-3.14-poetry:2.3.2` (from `docker/Dockerfile.base`) | Build-only: Python 3.14 + pinned Poetry. Never started; nothing runs in it. |
| `api` | `ghcr.io/cgiar-agwise/kvuno-api` (built from `docker/Dockerfile`) | Flask dev server. Listens on **80** in the container; compose maps host `5000`. Entrypoint migrates, then serves. |
| `worker` | `ghcr.io/cgiar-agwise/kvuno-worker` (built from `docker/Dockerfile.worker`) | Celery ingestion worker |

```
┌──────────────┐      port 5432     ┌──────────────┐
│  PostgreSQL  │◀───────────────────│     api      │
│  + PostGIS   │   DB_HOST=db       │  Flask API   │
│  (external)  │                    │   port 80   │
└──────────────┘                    └──────┬───────┘
                                             │
┌──────────────┐                              │
│    redis     │◀───── broker, results ─────▶│    worker    │
│  (external)  │      job progress keys      │  Celery task │
└──────────────┘                              └──────────────┘
```

> **Postgres and Redis are no longer compose services.** They are expected to be running elsewhere (or added back via an override file), and `DB_HOST` / `CELERY_BROKER_URL` must point at whatever is reachable. The services also no longer declare `depends_on`. If you need the full stack locally, bring your own `postgis/postgis:17-3.5` and `redis:7-alpine`, or add a `docker-compose.override.yml`.

`api` and `worker` both bind-mount `${DATA_DIR:-./data}` at `/app/static/data`.

> **`base` is a build-time dependency, not a running service.** Build it before the others — the application images `FROM` it and will fail otherwise: `docker compose build base`.

---

## 2. Environment Variables

### `docker-compose.yml` interpolation

The compose file reads these variables from the host environment (or `.env` file in the project root):

| Compose variable | Default | Used by |
|---|---|---|
| `TAG` | `latest` | `api` / `worker` image tag |
| `DATA_DIR` | `./data` | Host directory mounted at `/app/static/data` |
| `DB_NAME` | *(required)* | Database name |
| `DB_USER` | *(required)* | Database user |
| `DB_PASSWORD` | *(required)* | Database password |

`DB_USER`, `DB_PASSWORD`, and `DB_NAME` use `${VAR:?message}` syntax, so Compose **fails fast** if they are missing.

### Env loading per service

| Service | How it gets config |
|---|---|
| `api` | `env_file: .env` (the explicit `environment:` block is commented out) |
| `worker` | Explicit `environment:` block with `${VAR:-default}` interpolation |
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

# Build and start all services. The api container applies migrations
# before it starts serving, so there is no separate migration step.
docker compose up --build -d

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

There is no separate migration service. The `api` image entrypoint (`docker/entrypoint.sh`) applies pending migrations, then execs the server:

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

**Set it to `false` when you run more than one API replica**, otherwise each replica races to migrate the same schema — concurrent `alembic upgrade head` calls can deadlock or fail. This is the reason startup migrations were originally removed in favour of a separate step.

The `worker` never migrates. It only receives tasks from the already-running API, and `process_pending()` is enqueued from `create_app()`. A task already sitting in the broker when the worker restarts could execute before the API migrates, but Celery's `autoretry_for=(Exception,)` with 10 retries at 60s absorbs that window. `depends_on: api` would **not** help — the API container counts as started the moment its entrypoint begins, which is before migrations finish.

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

Four Dockerfiles live in `docker/`. The three application images share a builder base; none of them install Poetry themselves. The build context is still the **repo root** — only the Dockerfile paths changed — so `docker-compose.yml` and `.dockerignore` remain at the root, and `COPY` paths inside the Dockerfiles are unchanged.

The base installs Poetry with the official installer
(`https://install.python-poetry.org`), pinned to `ARG POETRY_VERSION`, and
verifies the resolved version in the same layer. `curl` is installed only for
that step and purged again in the same layer.

> `.dockerignore` cannot move into `docker/`: Docker only reads it from the build-context root. Moving it would silently disable every rule, including the `!README.md` negation that `poetry install` depends on.

| Dockerfile | Output | Notes |
|---|---|---|
| `docker/Dockerfile.base` | `ghcr.io/masgeek/python-3.14-poetry:<poetry-version>` | `python:3.14-slim` + Poetry via the official installer into `POETRY_HOME=/opt/poetry`. **Not built by CI** — build locally. |
| `docker/Dockerfile` | `ghcr.io/cgiar-agwise/kvuno-api` (dev) | Installs `--only main`; runs `python3 run.py` |
| `docker/Dockerfile.prod.dockerfile` | same image name, prod build | Installs `--only main`; runs Gunicorn; adds a `HEALTHCHECK` |
| `docker/Dockerfile.worker` | `ghcr.io/cgiar-agwise/kvuno-worker` | Runs the Celery worker |

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

There is no automated check for this, so a bump means editing **five** places in one commit:

1. `docker/Dockerfile.base` — `ARG POETRY_VERSION`
2. `docker/Dockerfile` — `ARG POETRY_VERSION`
3. `docker/Dockerfile.prod.dockerfile` — `ARG POETRY_VERSION`
4. `docker/Dockerfile.worker` — `ARG POETRY_VERSION`
5. `docker-compose.yml` — `base` service `image:` tag **and** `POETRY_VERSION:` build arg

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

**Worker (`docker/Dockerfile.worker`)**
- `celery -A app.celery_app worker --loglevel=info --concurrency=1`
- `restart: unless-stopped`

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
| Port 5000 already in use on the host | Another service on host port 5000 | Stop it, or remap the host side: `ports: - "5001:80"` |
| `Permission denied` binding, app exits at startup | Container runs as non-root and port 80 is privileged | Add `cap_add: [NET_BIND_SERVICE]` (compose already does for `api`) |
| `denied: requested access to the resource is denied` on pull | Package is private or you are not logged in | `docker login ghcr.io` with a token that has `read:packages` |
| `manifest unknown` on pull | Tag not built yet, or wrong `TAG` | Tags come from the Docker Build workflow; check what exists under the `CGIAR-AgWise` org |
| `pull access denied` / `manifest unknown` for `python-3.14-poetry` | Base image not built locally and the tag is not in the registry (or is private) | `docker compose build base`, or `docker login ghcr.io` and retry the pull |
| `pyproject.toml changed significantly` during build | Poetry version does not match the one that generated the lock | See [Bumping Poetry](#bumping-poetry) — five places to update |
| `Declared README file does not exist` during build | `!README.md` missing from `.dockerignore` | The negation must follow the `*.md` rule |
| Upload returns `413` on the chunked path | The merged file exceeded `MAX_FILE_SIZE_MB` (default 20) | The cap is now enforced server-side during the merge, not just in the browser |
| Upload returns a `.parquet` file name when `.rds` went in | Uploads are converted to Parquet on arrival so previews and ingestion read the cheap format | Use the returned `file` value from `/ui/upload/complete` or `/api/v1/data/upload`; it is the name the worker will read |
| A job shows as *Processing* indefinitely | The worker died (OOM, restart, crash) and never wrote a terminal status | It becomes *Stalled* after `JOB_STALE_AFTER_SECONDS` (default 1800) and offers Retry. A job that stays Processing past that is still being written to |
| Worker container restarts during an ingest | Exceeded `WORKER_MEM_LIMIT` | Raise it, or check whether the file is unusually large for its format |
| Entrypoint fails with `not found` | `docker/entrypoint.sh` checked out with CRLF | `.gitattributes` pins `*.sh` to LF; re-checkout after it is committed |
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

| Trigger | Image | Source Dockerfile | Tags |
|---|---|---|---|
| `develop` | `kvuno-api` | `docker/Dockerfile` (dev) | `:latest`, `:develop` |
| `develop` | `kvuno-worker` | `docker/Dockerfile.worker` | `:latest`, `:develop` |
| `main` | `kvuno-api` | `docker/Dockerfile.prod.dockerfile` (prod) | `:latest`, `:production` |
| any tag | `kvuno-api` | `docker/Dockerfile.prod.dockerfile` (prod) | `:latest`, `:<tag>`, `:production` |
| any tag | `kvuno-worker` | `docker/Dockerfile.worker` | `:latest`, `:<tag>`, `:production` |

### Build workflows

| Workflow | Trigger | Builds |
|---|---|---|
| `docker-build.yml` | `push` to `develop` or `main` | Branch images |
| `docker-release.yml` | `push` on tags (`*`) | Release images |

They are separate so neither needs compound branching. Branch builds used to be triggered by `workflow_run` of PR Checks, which had to fail: **a `workflow_run` fired from a forked pull request gets a read-only `GITHUB_TOKEN`**, so the registry rejected the push with `denied: permission_denied: read_package`. Triggering on `push` fixes that — a push to `develop` or `main` cannot originate from a fork, so `packages: write` holds.

Both workflows trigger on `push`, so they start at the same time and neither can simply `needs` the other. Each image workflow therefore runs a `gate` job first, using `.github/actions/wait-for-checks`: it polls the GitHub API until PR Checks reports a conclusion **for that exact commit**, then sets `passed`. The image jobs have `needs: [ gate ]` and `if: needs.gate.outputs.passed == 'true'`.

| Outcome | Result |
|---|---|
| PR Checks succeeds | Images build |
| PR Checks fails or is cancelled | `gate` **fails** (red run) with the conclusion and run URL; image jobs are skipped |
| No run found within `timeout` (default 1800s) | Same — `gate` fails. A missing run never counts as a pass |

The gate marks the run failed rather than silently skipping, so a red push is visible in the Actions tab instead of looking like a no-op.

The gate needs `actions: read` to read run status; the image jobs keep `packages: write`.

Each job has a single-condition `if`. `main` builds **only** the production API image; the worker is not built there, so deploy the worker from a release tag.

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

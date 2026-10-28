# Docker

IITA Internal Use. Overview, quick start and the other guides: [README.md](../README.md).

Running the stack with Docker Compose, the published images, and the shared builder base. [DEPLOYMENT.md](DEPLOYMENT.md) has the full deployment guide.

## Docker Deployment

`docker-compose.yml` defines `base` (build-only, never started), `data-init` (one-shot: gives the data volume to the containers' `app` user), `api` (production image, Gunicorn), `api-dev` (Flask dev server, only with `--profile dev`, host `127.0.0.1:5002`), `web` (nginx serving the React SPA on host `127.0.0.1:5001`), and `worker` (Celery, or housekeeping via `HOUSEKEEPING_MODE`). The app listens on port 80 inside the container and compose maps host `127.0.0.1:5000` to it. Open the UI at `http://localhost:5001`, not at the API port. All published ports bind to loopback only; there is no `WEB_PORT` variable — change the mapping in the compose file or an override.

Database migrations are applied by the `api` container's entrypoint when `RUN_MIGRATIONS=true` — the entrypoint's own default — but `docker-compose.yml` overrides it to `${RUN_MIGRATIONS:-false}`, so under compose either set it or run `docker compose exec api python scripts/run_migrations.py` after every upgrade. Keep it `false` if you run more than one API replica so they don't race to migrate the same schema. The `worker` deliberately does **not** migrate: it is only handed work by the already-running API.

> **Set `ADMIN_EMAILS`** (e.g. `you@cgiar.org`) on the `api`, or nobody can approve new accounts: anyone can register, but every new account stays `pending` until an administrator approves it. Accounts that existed before the approval migration are `approved` and unaffected. See [Accounts and approval](API.md#accounts-and-approval).

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

## Docker — Individual Services

```bash
# API only (processes SPA uploads, and runs the startup scan in one process)
docker compose up -d api

# API + worker (adds the Celery path; needs Redis)
docker compose up -d api worker

# API + web UI
docker compose up -d api web
```

## Container Images (GHCR)

Images are published to GitHub Container Registry, not Docker Hub:

| Trigger | Images | Tags pushed |
|---|---|---|
| push to `develop`, after `UnitTests` passes | only the images whose contents changed (api = dev server) | `:develop`, `:sha-<short>` |
| push to `main`, after `UnitTests` passes → automatic release tag | api (gunicorn), worker, web | `:latest`, `:<tag>`, `:production`, `:sha-<short>` |

Images: `ghcr.io/cgiar-agwise/kvuno-api`, `-worker`, `-web`. `:latest` is the latest **release**; `:sha-<short>` is immutable, for rollbacks. CI is built to save minutes: it runs only the checks a change can affect (docs-only changes run almost nothing), PRs build only the Docker images whose build inputs changed, and `main` is built once per release. Details and the rules the pipeline depends on: [docs/DEPLOYMENT.md](DEPLOYMENT.md#9-container-registry-ghcr).
CI publishes with the built-in `GITHUB_TOKEN` (`permissions: packages: write`) — no `DOCKER_USERNAME` / `DOCKER_PASSWORD` secrets are needed. To pull on a server, authenticate with a classic PAT that has `read:packages`:

```bash
echo "$GHCR_TOKEN" | docker login ghcr.io -u <github-username> --password-stdin
```

Packages are private by default; set them to public in the org settings if you want anonymous pulls.

## Base Image (GHCR)

The builder base is `ghcr.io/masgeek/python-3.14-poetry:2.3.2` — `python:3.14-slim` plus a pinned Poetry. CI does not build or push it; build it locally with `docker compose build base`, and `docker push` it if you want to share it with other projects.

The Poetry version is the **tag**, not part of the name, so a Poetry bump publishes a new tag on the same package rather than creating a new one.

## Upgrading Poetry

`poetry.lock` is only valid for the Poetry release that generated it: different 2.x releases compute a different content-hash for `pyproject.toml`, and the build fails with *"pyproject.toml changed significantly"*. There is no automated check, so a bump must touch five places in one commit — `ARG POETRY_VERSION` in `docker/Dockerfile.base` and in all three application Dockerfiles under `docker/`, plus the `base` service `image:` tag **and** `POETRY_VERSION:` build arg in `docker-compose.yml`. Then regenerate the lock with that same Poetry and rebuild the base image. See [docs/DEPLOYMENT.md](DEPLOYMENT.md#5-image-variants).

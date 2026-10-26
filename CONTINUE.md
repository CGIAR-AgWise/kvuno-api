# CONTINUE.md — handoff for the next agent

IITA Internal Use. Written 2026-10-26.

**Read `AGENTS.md` first.** It is the reference for how this service works, and it
is kept current. This file covers only the work in progress: what is done, what
is not, what still needs the user to decide, and how to check things. Delete or
rewrite it once those items are done.

## Where things stand

- Branch **`develop`**. The last seven commits are **local only**, not pushed:
  - `15fa383` fix(worker): keep reconnecting after a broker blip
  - `af7bfb9` fix(uploads): let a redelivered inspection take over a dead one
  - `57bd544` fix(uploads): finish an upload's status when its import ends
  - `f026e9a` fix(ingest): open a processing run for the fallback ingest runner
  - `e127a6c` feat(jobs): administrators delete a file with its upload and job history
  - `fa1a359` fix(worker): resume at once after a killed import, and stop the OOM kills
  - `90effc3` feat(ingest): `created_at` from a confirmed data date; stop storing NULL timestamps

  Everything older (worker offload, ingest speed fix, conflict cleanup script,
  conversion progress, awaiting-mapping list, build contexts) is on `origin/develop`.
- **Production stack** (`D:\Dev\tools\proxy-tool\stacks\kvuno`, a different repo):
  committed by the user. The worker runs Celery (`HOUSEKEEPING_MODE` empty) with
  `CELERY_CONCURRENCY=1` and 2 GB; the API has 2 GB. Whether the latest images
  are deployed is not known. Ask.
- **Checks at the last commit:** 462 passed, 1 skipped (a POSIX-only lock test,
  skipped on Windows); `ruff` clean; `gen_config_docs --check` clean; `pnpm run
  build` passes.
- **Migrations since the last release**, all applied by the API container on
  start: `b7d3f1a9c2e4` (uploads.source), `c4e8a2d6f1b3` (uploads.progress),
  `d9f1b3c5e7a2` (drop the duplicate GiST index), `e2a4c6b8d0f1`
  (uploads.file_modified_at and data_date). Head is `e2a4c6b8d0f1`.

## What changed recently, and why (one line each; details in AGENTS.md)

- **Ingest was slow and logged false conflicts.** psycopg 3 returns `rowcount = -1`,
  so every batch ran a full-table lookup and logged every inserted row as a
  conflict. Now: INSERT … RETURNING, executemany, an index-backed lookup.
  About 14k rows/s on PostGIS.
- **Imports died for 40+ hours.** They ran on threads inside gunicorn workers,
  which gunicorn recycles. Now all file work goes to the Celery worker, or to a
  detached `ingest_runner` process when there is no worker.
- **Intermittent stalls** (production log: `WorkerLostError … signal 9`). An
  OOM-killed process left its ingest lock, and the requeued task skipped the
  file. Now a lock whose holder is a dead pid on the same host is taken over at
  once. Worker concurrency is back to 1.
- **Timestamps.** Rows were stored with NULL `created_at`/`updated_at`. Now
  `created_at` is the upload's confirmed **Data date** (else the file's first
  import time) and `updated_at` is the import time. RDS and Parquet files store
  no creation date.
- **Admin file delete** (`DELETE /ui/jobs/<file>`): removes the file, its upload
  and its job history, and keeps imported rows and `file_imports`.
- **Scripts:** `scripts/convert_rds.py` (local RDS → Parquet, recursive, defaults
  to `static/`) and `scripts/cleanup_conflicts.py` (removes the old false
  conflicts; dry run unless `--apply`).
- **The last four commits closed the known gaps 5–8 below:** the fallback
  `ingest_runner` opens a processing run; an upload moves `processing → completed`
  (or `failed`) when its import ends; a redelivered worker inspection takes over
  at once instead of waiting for the Upload-page poll; and the worker's
  post-connect broker retry is explicit and verified (it reconnects, it does not
  exit).

## Open items

Waiting on the user (ask; do not do these unprompted):

1. **Push `develop`** and deploy. Never push, deploy, or change production
   resource limits without the user saying so.
2. **Run the conflict cleanup in production:**
   `docker exec kvuno python scripts/cleanup_conflicts.py`, then the same with
   `--apply`. It changes production data, so it is the user's call.
3. **Backfill NULL timestamps** on rows imported before `90effc3` (for example
   from `file_imports.processed_at` by `check_sum`). Offered, not written. If
   written, make it a script with a dry run, like `cleanup_conflicts.py`.
4. **"Undo import"**: delete a file's imported rows by `check_sum`, as an
   opt-in option on the admin delete. Offered, not built.

Known gaps 5–8 are fixed (see "What changed recently"). Older suggestions the
user has not taken up:

9. A CI job against `postgis/postgis:17-3.5`; `RATE_LIMIT_STORAGE` on Redis;
   metrics or a richer `/health`; the token in `localStorage` (consider an
   HttpOnly cookie); the unused `psycopg2-binary` dependency.

## How to check work

```bash
poetry run ruff check .
poetry run pytest -q
poetry run python scripts/gen_config_docs.py --check
```

Run from `frontend/` (it type-checks first):

```bash
pnpm run build
```

- **Ingest, spatial queries and migrations need real PostGIS.** The unit tests
  use SQLite. The local `postgis/postgis:17-3.5` image is in **WSL Docker**
  (`wsl -e docker …`). Start a throwaway container on a spare port with tmpfs
  storage, then run `alembic upgrade head` with
  `DB_URL=postgresql://postgres:bench@127.0.0.1:55432/kvuno`:
  `wsl -e docker run -d --rm --name kvuno-bench -e POSTGRES_PASSWORD=bench -e POSTGRES_DB=kvuno -p 127.0.0.1:55432:5432 --tmpfs /var/lib/postgresql/data postgis/postgis:17-3.5`.
  Stop it when done.
- ⚠️ **The user's own containers** are `postgres` on 5432, `redis-dev` on 6379
  and others. Never use, stop or modify them. For a broker, start a throwaway
  `redis:8` on another port (for example 56379). When a test needs "no Redis",
  point `CELERY_BROKER_URL` at a closed port; on Windows each refused connect
  costs about 2 s.
- **A real Celery round trip:** start a worker with the same env vars plus
  `--pool solo` (Windows), then stop it by its own PID (`taskkill /PID <pid> /T /F`),
  never by matching command lines.
- `.venv-linux` has no pytest. For Linux-only behaviour (pid probing), run a
  plain script with WSL's `python3`.

## User preferences and constraints

- **IITA rules:** no PII in logs or data (people by id only); mark written
  outputs "IITA Internal Use"; call the organisation "IITA". Do not open the real
  `.env` files or the database backup dumps.
- **GitHub Actions** stay on version tags; the user rejects SHA or digest pinning.
- **No `print()`** (ruff T20); structlog with keyword fields.
- **UI:** deliberately plain, AKILIMO branding (see AGENTS.md); every status goes
  through `StatusBadge`; the user dislikes designs that look AI-generated.
- **Commits:** Conventional Commits, lowercase, short imperative subject. Commit
  locally; push only when asked.
- The user decides anything that changes production data or resources, and for
  real choices prefers a short question with a recommended option.
- **Windows/PowerShell gotchas:** pass multi-line commit messages with
  `git commit -F <file>`; `Remove-Item` on piped paths can be blocked, so use
  `[System.IO.File]::Delete(...)`; prefer small Python patch scripts over complex
  PowerShell string quoting.

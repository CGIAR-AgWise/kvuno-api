# CONTINUE.md — handoff for the next agent

IITA Internal Use. Written 2026-10-26.

**Read `AGENTS.md` first.** It is the reference for how this service works, and it
is kept current. This file covers only the work in progress: what is done, what
is not, what still needs the user to decide, and how to check things. Delete or
rewrite it once those items are done.

## Where things stand

- Branch **`develop`**, pushed with the commit that carries this file (check
  `git status -sb`). Newest commits:
  - `4b1242b` chore(deps): drop the unused psycopg2 driver
  - `9b8d26a` feat(health): report the ingest stack behind deep=1
  - `b2e6b6e` ci: run migrations and a spatial query against PostGIS
  - `baa1526` feat(scripts): backfill planting rows imported without timestamps
  - `7e2adb2` feat(admin): let a file deletion purge the rows it imported
  - `35a9276` fix(uploads): close a processing upload whose job already finished
  - `12757a1` fix(uploads): match an upload by id when its extension changes
  - `e999cf6` style: tidy the delete-file api doc and dialog props
  - `e73bae2` fix(uploads): update a Processing card when its import ends
  - `d8d1e2c` docs: record the closed upload and worker gaps
  - `15fa383` fix(worker): keep reconnecting after a broker blip
  - `af7bfb9` fix(uploads): let a redelivered inspection take over a dead one
  - `57bd544` fix(uploads): finish an upload's status when its import ends
  - `f026e9a` fix(ingest): open a processing run for the fallback ingest runner
  - `90effc3` feat(ingest): `created_at` from a confirmed data date; stop storing NULL timestamps
- **Production stack** (`D:\Dev\tools\proxy-tool\stacks\kvuno`, a different repo):
  committed by the user. The worker runs Celery (`HOUSEKEEPING_MODE` empty) with
  `CELERY_CONCURRENCY=1` and 2 GB; the API has 2 GB. No `kvuno` containers were
  running at the time of writing, so nothing has been verified against the new
  images.
- **Checks at the last commit:** 475 passed, 1 skipped (a POSIX-only lock test,
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
  no creation date. Old NULL rows: `scripts/backfill_timestamps.py` (dry run
  unless `--apply`).
- **Admin file delete** (`DELETE /ui/jobs/<file>`): removes the file, its upload
  and its job history, and keeps imported rows and `file_imports`. With
  `{"purge_rows": true}` ("Undo import", now a checkbox in the Jobs dialog) it
  also deletes the rows by checksum, the file's `import_conflicts` and the
  `file_imports` row, so the same file can be imported afresh.
- **The last upload/worker commits closed the known gaps 5–8:** the fallback
  `ingest_runner` opens a processing run; an upload moves `processing →
  completed` (or `failed`) when its import ends; a redelivered worker inspection
  takes over at once instead of waiting for the Upload-page poll; and the
  worker's post-connect broker retry is explicit and verified (it reconnects, it
  does not exit).
- **A "Processing" card no longer lies.** The Upload page only polled the
  inspection states, so a card loaded while an import ran stayed on
  "Processing" for as long as the page was open, even after the row turned
  `completed`. It now polls `submitted`/`processing` too and drops the card on
  completion.
- **The remaining processing-upload holes are closed.** An RDS whose Parquet
  write failed keeps `stored_path = <id>.rds` while the worker imports
  `<id>.parquet`; `mark_processed`/`find_by_stored_name` now compare the id, not
  the extension. And `GET /ui/uploads` reconciles a `processing` row whose job is
  `completed`, so a failed bookkeeping write no longer leaves it stuck.
- **`/health?deep=1`** adds Redis, Celery-worker and data-directory state
  (`degraded`, still 200, when one is down). The default stays the cheap DB-only
  probe the probes rely on.
- **CI runs the real database now.** The Python job starts
  `postgis/postgis:17-3.5`, runs `alembic upgrade head` and
  `scripts/postgis_smoke.py` (inserts a row, queries it by `radius` in metres).
  Unit tests remain SQLite-only.
- **Scripts:** `scripts/convert_rds.py` (local RDS → Parquet, recursive, defaults
  to `static/`), `scripts/cleanup_conflicts.py` (removes the old false
  conflicts) and `scripts/backfill_timestamps.py` (dates pre-`90effc3` rows from
  `file_imports.processed_at`). All are dry runs unless `--apply`.

## Open items

Waiting on the user (ask; do not do these unprompted):

1. **Deploy from `develop` and verify.** Rebuild the API **and** web images,
   then check: (a) an upload card leaves "Processing" when its import ends;
   (b) `curl /health?deep=1` reports `redis`/`worker` `UP` in production. The
   production stack is `D:\Dev\tools\proxy-tool\stacks\kvuno`; no kvuno
   containers were running at the last check.
2. **Run the conflict cleanup in production:**
   `docker exec kvuno python scripts/cleanup_conflicts.py`, then the same with
   `--apply`. It changes production data, so it is the user's call.
3. **Backfill NULL timestamps** now that the script exists:
   `docker exec kvuno python scripts/backfill_timestamps.py`, then `--apply`.
   It only fills NULLs and commits in batches; dry-run first.
4. **`RATE_LIMIT_STORAGE` on Redis** in the production stack (the default
   `memory://` is per gunicorn worker, so limits are inconsistent). The stack
   already has `redis-jobs`; use a database number other than the broker's.
5. **The token in `localStorage`** (consider an HttpOnly cookie). This is an
   auth redesign with CSRF implications; not done, and no change is recommended
   without a decision on CSRF/cross-origin behaviour.

Resolved (were items 9–11 of the previous handoff): the PostGIS CI job, the
richer `/health`, the unused `psycopg2-binary`, and the two processing-upload
findings.

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
  `DB_URL=postgresql://postgres:bench@127.0.0.1:55432/kvuno`, followed by
  `python scripts/postgis_smoke.py`:
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

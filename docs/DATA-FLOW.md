# Data Flow

This document describes the two main data flows in KVuno API: **ingestion** (RDS/Parquet files into the database) and **serving** (database records through the REST API).

> All ingestion logic lives in `app/services/housekeeper.py`. The root `housekeeping.py` is a thin Typer CLI over `housekeeper.run()`.

**Where files are processed** — every path ends in `process_file()`:

| Source | Runs in | Needs Redis/Celery |
|---|---|---|
| Upload page → `POST /ui/uploads/<id>/process` | A daemon thread in the API process (`routes/uploads.py::enqueue_upload`); upload → `processing` | No |
| Files already in `HOUSEKEEPING_DATA_DIR` | `create_app()` → `housekeeper.start_startup_scan()`, once per deployment: `process_pending()` → Celery when `HOUSEKEEPING_ENABLED=true`; otherwise `run_startup_scan()` on a thread in the one API process that claims `<data dir>/.startup-scan` | No |
| `POST /api/v1/data/upload` | `process_file_task` on the Celery worker when `HOUSEKEEPING_ENABLED=true`; otherwise saved for the next scan | Yes, to run at once |
| Jobs Retry (`POST /ui/process`) | An upload: the API thread, re-using its saved mapping. A plain data-folder file: Celery when `HOUSEKEEPING_ENABLED=true` | Only for plain files |
| `python housekeeping.py`, or the worker image with `HOUSEKEEPING_MODE` | That process (`housekeeper.run()`) | No |

---

## 0. Request Path — Browser to API

The frontend is a standalone **React 19 + TypeScript SPA** in `frontend/`, built with
Vite and pnpm and served by **nginx**; Flask renders no HTML. nginx reverse-proxies
`/api`, `/ui`, and `/health` to the `api` service, so the browser talks to one origin
and the bearer token never crosses one. Everything below is what happens *after* that
proxy hop — the flows are otherwise unchanged by the move to React.

| Layer | Value |
|---|---|
| SPA | `frontend/` (React 18, react-router 6, TypeScript), routes `/login`, `/register`, `/jobs`, `/explore`, `/upload`, `/quality`, `/tokens` |
| Edge | nginx — serves the bundle, reverse-proxies `/api`, `/ui`, `/health` to `api:80` |
| API | Flask — JSON only; SSE through `/ui/jobs/events` with buffering disabled |
| Async | Celery worker, same as §1 |

---

## 1. Ingestion Flow — RDS File Processing

RDS/Parquet files containing crop planting data are either uploaded through the SPA (into `static/data/uploads/<id>/`, see §1a), uploaded through `POST /api/v1/data/upload` or dropped into the data dir (`static/data/`, or `HOUSEKEEPING_DATA_DIR` if set), or downloaded from remote sources (`REMOTE_RDS_URLS`). They are then processed either by a Celery task (`worker` service) or by the standalone `housekeeping.py` CLI.

```
┌──────────────────┐     ┌──────────────────┐     ┌──────────────────┐
│  Remote server   │────▶│ download_remote_ │────▶│  static/data/    │
│  (HTTPS)         │     │  files()         │     │  *.RDS / *.parquet│
│  REMOTE_RDS_URLS │     │  (RDSDownloader, │     │  (local +        │
│   env var        │     │   SSRF guards)   │     │   downloaded)    │
└──────────────────┘     └──────────────────┘     └────────┬─────────┘
                                                            │
                                                  ┌─────────▼─────────┐
                                                  │  process_file()   │
                                                  │   per file        │
                                                  └─────────┬─────────┘
                                                            │
                                                  ┌─────────▼─────────┐
                                                  │  checksum check   │
                                                  │  (SHA-256)        │
                                                  └─────────┬─────────┘
                                                            │
                                          ┌─────────────────▼─────────────────┐
                                          │  file_imports lookup by checksum │
                                          │  + stored `offset`              │
                                          └─────────────────┬─────────────────┘
                                              offset>0       │        offset=0
                                                   ┌────────▼───┐   ┌────────▼─────┐
                                                   │  RESUME    │   │  READ FILE   │
                                                   │  from row  │   │ pyreadr /    │
                                                   │  offset    │   │ read_parquet │
                                                   └────────────┘   └───────┬──────┘
                                                                             │
                                                              ┌──────────────▼─────────────┐
                                                              │  Drop rows without coords  │
                                                              │  Map columns via upload's  │
                                                              │ column_map / RDS_COLUMN_MAP│
                                                              └──────────────┬─────────────┘
                                                                             │
                                                              ┌──────────────▼─────────────┐
                                                              │  Batch insert in savepoint│
                                                              │  (HOUSEKEEPING_BATCH_SIZE) │
                                                              │  WKT POINT(lon lat), 4326 │
                                                              └──────────────┬─────────────┘
                                                                             │
                                                              ┌──────────────▼─────────────┐
                                                              │  Every CHECKPOINT_INTERVAL │
                                                              │  batches: commit rows AND  │
                                                              │  persist new offset        │
                                                              └──────────────┬─────────────┘
                                                                             │
                                                              ┌──────────────▼─────────────┐
                                                              │  Record in file_imports   │
                                                              │  (name + SHA + offset)    │
                                                              │  conflicts → import_      │
                                                              │  conflicts                │
                                                              └────────────────────────────┘
```

### Step-by-step

1. **(Optional) Remote download** — `download_remote_files()` (`app/services/housekeeper.py`) reads `REMOTE_RDS_URLS` (semicolon-delimited) and fetches each URL into the data dir. Supports bearer tokens (`REMOTE_RDS_TOKEN`), cookies (`REMOTE_RDS_COOKIES`), and custom headers (`REMOTE_RDS_HEADERS`). SSRF guards (`app/utils/downloader.py`): HTTPS-only unless `REMOTE_RDS_ALLOW_HTTP=true`; private/loopback/link-local/metadata IPs rejected; redirects followed **by hand** (max 5), each hop re-validated, with the bearer token (`Authorization`) dropped when a hop changes host — `REMOTE_RDS_COOKIES` are not host-scoped and are still sent; optional `REMOTE_RDS_ALLOWED_DOMAINS` allow-list (exact host or true subdomain — `example.org.evil.com` does not match); `REMOTE_RDS_MAX_BYTES` (default 1 GiB) enforced while streaming; and, after connecting, the socket's peer IP is re-checked against DNS rebinding (best effort; skipped behind an HTTP(S) proxy, whose own address is the peer). Failures are logged and skipped.
2. **Conversion + file discovery** — `load_rds_to_db()` (`app/services/housekeeper.py`) first converts every `.RDS` in the data dir root to Parquet (`batch_convert()`; the watcher does the same as files arrive), then scans the **root** (not subdirectories) for `.RDS` and `.parquet` files. Conversion is safe with several scanners at once: a per-file `<file>.convert.lock` lets one process convert it (the others skip it), the Parquet is written to `<name>.parquet.tmp` and renamed into place so no one reads a partial file, an RDS already removed by another process counts as done, and one bad file is logged and skipped. SPA uploads live in `uploads/<id>/` precisely so this scan does not ingest them with the default mapping before the user has mapped their columns
3. **Pre-flight DB health check** — `db_health_check()` runs `SELECT 1` before any processing
4. **Concurrent dispatch** — Files are processed in a `ThreadPoolExecutor` (size `HOUSEKEEPING_MAX_WORKERS`) using `submit()` + `as_completed()` so one failure does not abort the batch. The whole scan is **one** `processing_runs` row (`source='scan'`); each Celery submission gets its own run (`source='upload'`) from `process_file_async()`. Files processed on an API thread (SPA uploads) are not grouped into a run. **The startup scan runs once per deployment, not once per process.** `create_app()` runs in every gunicorn worker and again on every `MAX_REQUESTS` recycle, so `start_startup_scan()` decides: with `HOUSEKEEPING_ENABLED=true` it calls `process_pending()`, which enqueues the scan for the Celery worker only if it wins the Redis key `kvuno:process_pending` (`SET NX`, expiring after `STARTUP_SCAN_COOLDOWN_SECONDS`) — the API never imports files itself. Without a worker, `run_startup_scan()` claims `<data dir>/.startup-scan` (an O_EXCL lock whose staleness is the cooldown), refreshes it every 30 s while scanning, and leaves it in place afterwards, so sibling and recycled workers skip until the cooldown passes. `STARTUP_SCAN=false` turns it off
   - **Per-file lock** — `process_file` takes `<file>.ingest.lock` next to the data file (O_EXCL, `app/utils/file_lock.py`) and touches it every chunk. A second worker on the same file (double submit, or a Redis redelivery of a still-running task) returns **silently** — writing an error would mark the live job failed. A lock untouched for `INGEST_LOCK_STALE_SECONDS` (default 600) is taken over as dead
5. **Checksum calculation** — Each file is hashed with SHA-256 (`app/utils/__init__.py::calculate_file_checksum`)
6. **Deduplication / resume** — `FileImportRepo.get_by_checksum()` checks `file_imports`. A row with `offset IS NULL` is fully imported: the file is skipped and a `completed` progress entry ("Already ingested") is written so a submitted duplicate still shows up on the Jobs page. A non-null `offset` resumes mid-file; an `offset` at or past the row count (left by files completed before the offset was cleared on success) is treated as processed and normalised to `NULL`
7. **File parsing** — `.parquet` is **streamed**: `_iter_parquet_chunks()` reads record batches from a `pyarrow.parquet.ParquetFile` starting at the resume offset, so the whole file is never in memory. `.RDS` is read with `pyreadr.read_r()`, which has no chunked mode and deserialises the **whole** file before it is sliced — which is why SPA uploads are converted to Parquet during inspection. Either way rows are processed in chunks of `HOUSEKEEPING_CHUNK_SIZE` (default 5000), which sizes the insert batches rather than bounding memory
8. **Column mapping** — Rows without coordinates are dropped. A file submitted from the Upload page carries the user's `column_map` (passed to `process_file(column_map=...)`), which is used **as is** — not merged over the defaults, so a skipped column stays skipped. Any other file resolves its columns through `load_column_map()`: built-in defaults, overridden by `RDS_COLUMN_MAP` JSON, overridden by a per-file `.map.json` sidecar — and that map is then **intersected with the file's actual columns** (`_select_columns()`; missing ones are logged and skipped), because asking pyarrow for an absent column fails the whole read. `XY` is read only if the file has it. The aliases (`cultivar`→`variety`, `season`→`season_type`, `sowing_date`/`planting_date`/`date`→`opt_date`, `longitude`/`long`/`lon_x`→`lon`, `latitude`/`lat_y`→`lat`) live in `COLUMN_ALIASES` (`app/services/uploads.py`) and only seed the suggested mapping during upload inspection. The file checksum is attached to every row
9. **Batch insert** — Records are bulk-inserted, each batch wrapped in `begin_nested()` (savepoint) and retried with backoff. `coordinates` is built as `POINT(lon lat)` WKT, SRID 4326, whenever `lat` and `lon` are not `None` — `0` is a valid coordinate (a truthiness check used to null it and fail the whole batch). A batch that still fails is skipped and counted — unless the failure is a **transient** database error (`is_transient_db_error()`: lost connection, pool timeout, …), which aborts the file instead, since skipping would move the offset past rows that were never written
10. **Checkpointing** — Every `HOUSEKEEPING_CHECKPOINT_INTERVAL` batches (default 50) the inserted rows **and** the new `offset` (`upsert_offset(commit=False)`) are committed in **one** transaction. Committing rows first left a window where a crash kept rows past the stored offset, which a resume re-inserted. Don't split them
11. **Conflict capture** — Duplicates against the `uq_planting_recommendation` unique constraint are recorded in `import_conflicts` (country/province/lon/lat/variety/season_type/opt_date). The constraint is `NULLS NOT DISTINCT` on PostgreSQL ≥ 15 (migration `f3a9c2d4e5b6`), so a row with a NULL key column now conflicts instead of being re-inserted on every replay, and the conflict lookup matches with `IS NOT DISTINCT FROM`
12. **File tracking** — A `FileImport` record (file name, checksum, `original_filename`, `offset`) is saved. On completion `offset` is stored as `NULL`, which is what every "already processed" check tests for (it used to store the row count, so no finished file was ever recognised and re-uploads were reported as partial). A partially processed file keeps its numeric offset. Completion with skipped batches is reported as `completed` with an error note ("Done with errors")
13. **Graceful shutdown / failure** — Ingestion stops only at a **chunk boundary**: the chunk's rows and the offset after it are committed together, so a resume never re-inserts rows. In the CLI (`python housekeeping.py`), SIGTERM/SIGINT request it and a second signal force-quits; the handlers are installed by `install_signal_handlers()` from `housekeeper.run()` only — they used to be installed at import, which replaced gunicorn's and Celery's own SIGTERM handling. On the Celery worker, `worker_shutting_down` drops a stop file the pool child checks; the job is left `processing` ("Paused for a worker restart") and on the next start `worker_ready` re-enqueues every job that host left unfinished, in its own run, after `worker_init` has cleared that host's dead ingest locks. On an exception the session is rolled back and the error is reported at the last checkpoint row (where a retry will resume). A transient DB error is **re-raised**, so the Celery task's `autoretry_for` retries it from the stored offset; any other error (a bad file) is recorded as `error` and the task returns, so it is **not** retried
14. **Progress** — `save_progress()` in `app/services/progress_store.py` writes the `job:{stem}` hash to Redis and publishes on the `jobs:updates` channel, which `/ui/jobs/events` streams to browsers (the hot path, throttled by `JOB_PROGRESS_MIN_INTERVAL`). It also checkpoints the job to the `processing_jobs` table (grouped under a `processing_runs` row) every `JOB_DB_CHECKPOINT_SECONDS` (default 5s) whether or not Redis is up; terminal states always write through to both. Database progress writes use their **own short-lived `Session`** (`_db_session()`), never the scoped session holding the uncommitted ingest batch, so a progress write can no longer commit ingest rows ahead of their offset. `attempts` counts on from the file's history for each new run and increments when a run's job comes back to `processing` (a retry). Timestamps are stored naive UTC and converted with `_epoch()`, which treats them as UTC — `datetime.timestamp()` assumed local time and made jobs look stale (or fresh) on non-UTC hosts. On completion the worker also calls `invalidate_planting_data_cache()`, so new rows appear in the cached map/filter endpoints immediately
15. **Telemetry** — `emit_event()` logs structured events through the app logger (JSON lines in production) at `housekeeping.start/end`, `file.download_start/end`, `file.processing_start/end`

### Tuning knobs

| Env var | Default | Effect |
|---|---|---|
| `HOUSEKEEPING_DATA_DIR` | `static/data` | Where downloads live; SPA uploads go in its `uploads/` subdirectory |
| `HOUSEKEEPING_BATCH_SIZE` | `2000` | Rows per savepoint-wrapped insert |
| `HOUSEKEEPING_CHUNK_SIZE` | `5000` | Rows per in-memory chunk |
| `HOUSEKEEPING_CHECKPOINT_INTERVAL` | `50` | Batches between offset commits |
| `HOUSEKEEPING_MAX_WORKERS` | `1` | Files processed in parallel |
| `RDS_COLUMN_MAP` | built-in defaults | JSON map of source column → ORM field |
| `INGEST_LOCK_STALE_SECONDS` | `600` | Age at which an unrefreshed `<file>.ingest.lock` is taken over |
| `CELERY_VISIBILITY_TIMEOUT` | `21600` | Redis redelivers an unacked (late-acked) task after this; must exceed the longest single-file ingest |

### Tables affected

| Table | Action | Key columns |
|---|---|---|
| `planting_recommendations` | INSERT (bulk) | `check_sum`, `country`, `province`, `coordinates` (POINT), `lon`, `lat`, `variety`, `season_type`, `opt_date`, `planting_option` |
| `file_imports` | UPSERT | `check_sum` (unique), `file_name`, `original_filename`, `offset`, `processed_at` |
| `import_conflicts` | INSERT | `record_data` (JSON), `country`, `province`, `lon`, `lat`, `variety`, `season_type`, `opt_date`, `check_sum`, `source` |
| `processing_runs` | INSERT (one per enqueued submission/Retry, one per scan) | `id`, `label`, `source`, `status`, `total_files`, `started_at`, `finished_at` |
| `processing_jobs` | INSERT / UPDATE (checkpointed, always on terminal states) | `run_id` → `processing_runs.id`, `file_name` (not unique), `status`, `current_row`, `total_rows`, `error`, `attempts`, `updated_at` |

---

## 1a. Upload Flow — SPA to Worker

The Upload page sends files with the in-house tus client (`frontend/src/lib/tus.ts`) (5 MB `PATCH` bodies) to a tus 1.0 server (core + creation extension) implemented in Flask in `app/routes/uploads.py`, with storage, inspection and mapping rules in `app/services/uploads.py`. Every step moves one `uploads` row forward; nothing is kept in sidecar files.

```
 browser (lib/tus.ts)                        API process                                worker
 ───────────────────────                     ───────────                                ──────
 POST  /ui/uploads/tus  ───────────────▶ create row `receiving`, empty data.part
   Upload-Length, Upload-Metadata          size ≤ MAX_FILE_SIZE, .rds/.parquet
                                           (413 / 415 before any bytes)
 HEAD  /ui/uploads/tus/<id> ───────────▶ Upload-Offset = size of data.part
 PATCH /ui/uploads/tus/<id> ───────────▶ append at offset (409 mismatch, 423 locked)
                                           last byte: rename to <id>.<ext>, `received`,
                                           start inspection thread
 GET   /ui/uploads/<id>  (poll) ───────▶ `inspecting` → `ready` | `failed`
                                           restarts a dead inspection
 POST  /ui/uploads/<id>/process ───────▶ validate + save column_map, `processing`
                                           ──▶ API thread: process_file(column_map=…)
```

### Lifecycle

`receiving → received → inspecting → ready | failed → submitted → processing`

1. **Create** — `POST /ui/uploads/tus` with `Upload-Length` and `Upload-Metadata: filename <base64>`. The size cap (`MAX_FILE_SIZE_MB`) and extension are checked here, before any bytes are accepted (`413` / `415`). The server issues the id (`uuid4().hex`, validated against `^[0-9a-f]{32}$` before it ever becomes a path) and returns `201` + `Location`. `Upload-Defer-Length` is not supported
2. **Receive** — `PATCH` appends at `Upload-Offset` into `HOUSEKEEPING_DATA_DIR/uploads/<id>/data.part`. The part file's size *is* the offset, so an API restart loses nothing and `HEAD` tells the client where to resume. An offset mismatch is `409`; a second request while another holds the per-upload `.lock` file (O_EXCL, taken over after 30s without a write) is `423`, which the client retries
3. **Received** — after the last byte the part file is renamed to `<id>.rds` / `<id>.parquet` and inspection starts
4. **Inspect** — on a daemon thread in the API process, so it works with `HOUSEKEEPING_ENABLED=false`: `convert_for_ingestion()` (RDS → Parquet in a **child process**: reading an RDS is the expensive step — a 7.4 MB file of 2.1 M rows peaks at ~1.1 GB — so if it runs out of memory only the child is killed and the upload fails with "needs more memory than the server allows" instead of the API worker dying and the inspection restarting forever; an unreadable file fails with "not a readable R data file"; only a failed Parquet *write* keeps the RDS), preview rows, row count, SHA-256, duplicate lookup against `file_imports` (`processed` or `partial`), and a suggested `column_map` (exact DB column name, case-insensitive, then `COLUMN_ALIASES`). The claim is a conditional `UPDATE`, so two pollers never run it twice. A heartbeat refreshes `updated_at` every 30s; a row still `inspecting` without one after `UPLOAD_INSPECT_STALE_SECONDS` (default 180) — its gunicorn worker was recycled — is re-claimed by the next `GET /ui/uploads/<id>`
5. **Map** — the SPA shows the server's suggestion, so what is in the selects is exactly what will be sent; choosing *skip* removes the key. Validation on submit: `lat` and `lon` must be mapped, no DB column used twice, every source must exist in the file, every target must be a `planting_recommendations` column
6. **Process** — `POST /ui/uploads/<id>/process` validates and saves the mapping, sets the upload to `processing` and starts `process_file(column_map=…)` on a daemon thread in the API process — no Celery or Redis, whatever `HOUSEKEEPING_ENABLED` says. Progress goes to `progress_store` like any other ingest. A thread dies with its gunicorn worker; the file's stored offset is kept, and Retry resumes from it
7. **Retry** — the Jobs page calls `POST /ui/process` with only the file name; for an upload the server re-uses the saved `column_map` (it used to send `{}` and wipe it). Files with no `uploads` row fall back to the sidecar path, resolved with `Path.is_relative_to` against the data dir

Uploads are per user (`owner_id`); anyone else's id is reported as `404`. `GET /ui/uploads` lists the caller's unfinished uploads so a page reload does not lose files waiting to be mapped, and `DELETE /ui/uploads/<id>` discards one that is not yet `submitted` or `processing` (`409` otherwise). The tus `HEAD`/`PATCH` and the status `GET` are exempt from the default rate limit.

### Cleanup

`_cleanup_temp_files()` runs on every API worker boot and must never touch anything in flight — it used to `rmtree` the whole chunk directory, and gunicorn boots a worker on every `max_requests` recycle and timeout, which is what made uploads fail at random. It now:

- deletes unfinished uploads (including `processing` ones) idle longer than `UPLOAD_ABANDON_AGE` (default `7d`; `30m` / `12h` / `1d` / `2w`)
- deletes the upload directory, data file and any sidecars of jobs `completed` longer than `CLEANUP_AGE` ago
- removes the retired `.chunks/` directory if one is left over

---

## 2. Serving Flow — API Query

Clients retrieve planting data via `GET /api/v1/planting-data/`, which applies filters, builds a SQLAlchemy query, and returns paginated JSON. This endpoint is **public** (rate-limited); its sibling endpoints under the same prefix require a Bearer token.

```
┌──────────┐     ┌──────────────────┐     ┌──────────────────────┐
│  Client   │────▶│  flask-openapi3  │────▶│  PlantingDataFilter  │
│  HTTP GET │     │  APIBlueprint     │     │  (Pydantic validation)│
└──────────┘     └──────────────────┘     └──────────┬───────────┘
                                                      │
                                             ┌────────▼───────────┐
                                              │  PlantingRecommendationRepo.│
                                              │  get_paginated_data         │
                                             └────────┬───────────┘
                                                      │
                                             ┌────────▼───────────┐
                                             │  get_filtered_data │
                                             │  builds Query      │
                                             └────────┬───────────┘
                                                      │
                    ┌──────────────────────────────────┼──────────────────────────────┐
                    ▼                                  ▼                              ▼
          ┌─────────────────┐                ┌─────────────────┐          ┌─────────────────────┐
          │  Spatial filter  │                │  Field filters   │          │  Pagination          │
          │  ST_DWithin      │                │  country         │          │  query.paginate()    │
          │  (coordinates +  │                │  province (ILIKE)│          │  page, per_page      │
          │   radius meters) │                │  variety         │          └──────────┬──────────┘
          └─────────────────┘                │  season_type     │                     │
                                              │  opt_date        │                     │
                                              │  planting_option │                     │
                                              └─────────────────┘                     │
                                                                                      ▼
                                                                             ┌──────────────────┐
                                                                             │  JSON response   │
                                                                             │  data[]           │
                                                                             │  total            │
                                                                             │  pages            │
                                                                             │  current_page     │
                                                                             │  per_page         │
                                                                             └──────────────────┘
```

### Step-by-step

1. **HTTP request** — `GET /api/v1/planting-data/?country=Zambia&variety=Soybean&page=1&per_page=20` (`app/api/planting_data.py::get_data`)
2. **Parameter extraction** — `page` and `per_page` from `request.args` via `get_pagination()` (`app/dto/pagination.py`; defaults 1 and `DEFAULT_PER_PAGE` = 100; `per_page` is clamped to `[1, MAX_PER_PAGE]` = `[1, 500]`). All filter params are injected as a `PlantingDataFilter` Pydantic model (`app/dto/data_filters.py`)
3. **Validation** — Pydantic validators check:
   - `coordinates`: must match `lon,lat` format; lat in [-90, 90], lon in [-180, 180]
   - `opt_date`: must be `YYYY-MM-DD`
   - `sort_col` / `sort_dir`: allow-listed scalar columns (`PLANTING_SORT_COLUMNS` / `CONFLICT_SORT_COLUMNS` in `app/dto/data_filters.py`) and `asc`/`desc`; anything else is a `422` (flask-openapi3's validation status). It used to reach `getattr(Model, sort_col)` unchecked and 500 on names like `metadata`
   - Whitespace is stripped; enum values resolved
4. **Query building** — `PlantingRecommendationRepo.get_filtered_data()` (`app/repo/planting_recommendation.py`) constructs a SQLAlchemy query with optional filters:
   - **Spatial**: If `coordinates` + `radius` provided, uses `ST_DWithin(coordinates::geography, point::geography, radius)`, so `radius` is in **metres**. Without the cast, `ST_DWithin` on SRID 4326 geometries measured in degrees and `radius=50000` matched the whole planet
   - **Exact match**: `country`, `variety`, `season_type`, `opt_date`, `planting_option`
   - **Partial/ILIKE**: `province` uses `ILIKE '%search%'`
   - **Sort**: `sort_col` / `sort_dir` (defaults to `id` ascending)
5. **Pagination** — `query.paginate(page, per_page)` returns a `QueryPagination` object with items, total count, and page metadata
6. **Response mapping** — Each ORM `PlantingRecommendation` row is converted to a `PlantingRecommendationRecord` dict (`app/api/planting_data.py`)
7. **JSON response** — Returns `{ data: [...], total, pages, current_page, per_page }` with HTTP 200, or a generic 500 on error (the exception is logged with `logger.exception`, never returned to the client). The sibling endpoints `filters` (TTL 300s), `coordinates` (120s), `coordinates_bulk` (120s), and `clusters` (120s) are Redis-cached via `@api_cache`, which stores only **2xx `dict`/`list`** bodies — a cached 500 used to be replayed as a 200 for the whole TTL. `invalidate_planting_data_cache()` (`app/cache.py`) drops all four prefixes (`PLANTING_DATA_CACHE_PREFIXES`); it runs after an API upload (`app/api/upload.py`) **and** when the worker completes a file (`housekeeper.process_file`). The main data endpoint is **not** cached.

Other serving details: `/clusters` pages by cluster — `pages` is computed from `total_clusters` (also returned), not from the point `total` — and a non-numeric `zoom` or an out-of-range bound (`ne_lat`, `ne_lng`, `sw_lat`, `sw_lng`) is a `400` naming the parameter. CSV `/export` keeps `0` values (a truthiness check used to blank them).

### Response shape

```json
{
  "data": [
    {
      "id": 1,
      "country": "Zambia",
      "province": "Southern",
      "lat": -17.85,
      "lon": 25.92,
      "variety": "Soybean",
      "season_type": "Main",
      "opt_date": "2024-11-15",
      "planting_option": 1,
      "check_sum": "abc123..."
    }
  ],
  "total": 42,
  "pages": 5,
  "current_page": 1,
  "per_page": 10
}
```

---

## 3. Database Schema

All tables are defined in `app/models/kvuno.py` and created by Alembic migrations in `alembic/versions/`.

### `planting_recommendations`

| Column | Type | Description |
|---|---|---|
| `id` | BIGINT PK | Auto-increment ID |
| `check_sum` | VARCHAR(100) NOT NULL | SHA-256 of source file |
| `coordinates` | GEOMETRY(POINT, 4326) NOT NULL | Spatial point (PostGIS). `TEXT` on SQLite, where the migration skips the `postgis` extension — so spatial queries and ingestion need Postgres |
| `country` | VARCHAR(20) | Country name |
| `province` | VARCHAR(20) | Province/region name |
| `lon` | REAL | Longitude (denormalized) |
| `lat` | REAL | Latitude (denormalized) |
| `variety` | VARCHAR(20) | Crop variety |
| `season_type` | VARCHAR(20) | Season classification |
| `opt_date` | VARCHAR(8) | Optimal sowing date |
| `planting_option` | INTEGER | Planting option ID |
| `created_at` | DATETIME | Auto-set on insert |
| `updated_at` | DATETIME | Auto-set on update |

Unique constraint `uq_planting_recommendation` on `(country, province, lon, lat, variety, season_type, opt_date)` — this is what drives conflict detection. On PostgreSQL ≥ 15 it is `UNIQUE NULLS NOT DISTINCT` (migration `f3a9c2d4e5b6`, `20261022130000_planting_unique_nulls_not_distinct.py`), which first deletes existing NULL-aware duplicates keeping the lowest `id`; its downgrade does not restore them. Every key column is nullable, so under the default NULLS DISTINCT a row with any NULL key never conflicted and was inserted again on every replay. On SQLite / PostgreSQL < 15 the migration is a no-op.

Indexes: `idx_check_sum`, `idx_coordinates` (GiST), `idx_country`, `idx_province`, `idx_lon`, `idx_lat`, `idx_variety`, `idx_season_type`, `idx_opt_date`, `idx_planting_option`, plus a second GiST index `idx_planting_recommendations_coordinates`.

### `file_imports`

| Column | Type | Description |
|---|---|---|
| `id` | BIGINT PK | Auto-increment ID |
| `check_sum` | VARCHAR(100) UNIQUE NOT NULL | SHA-256 of processed file |
| `file_name` | VARCHAR(120) NOT NULL | Stored file name |
| `original_filename` | VARCHAR(255) | User-facing upload name |
| `offset` | BIGINT | Row offset reached — enables resume; `NULL` once fully processed |
| `processed_at` | DATETIME | Auto-set on insert |

### `uploads`

One row per SPA upload, from the first byte to submission (migration `20261022120000_create_uploads_table.py`, revision `7b1d0c5e2a91`). Replaces the `.meta.json` / `.map.json` / `.preview.json` sidecars for uploads. `.map.json` and `.meta.json` are still read by `process_file()` for files with no `uploads` row (dropped into the data dir, or sent to `/api/v1/data/upload`), and `/ui/process` still writes `.map.json` for them.

| Column | Type | Description |
|---|---|---|
| `id` | VARCHAR(32) PK | Server-issued uuid hex; also the directory name under `uploads/` |
| `owner_id` | BIGINT FK → `users.id` (SET NULL) | Uploader; other users get `404` |
| `original_name` | VARCHAR(255) NOT NULL | User-facing file name |
| `size` | BIGINT NOT NULL | tus `Upload-Length`, checked against `MAX_FILE_SIZE` |
| `status` | VARCHAR(20) NOT NULL | `receiving` / `received` / `inspecting` / `ready` / `failed` / `submitted` / `processing` |
| `stored_path` | VARCHAR(512) | File the worker reads (`.parquet` after conversion) |
| `sha256` | VARCHAR(64) | Checksum of the stored file |
| `preview` | JSON | Columns, sample rows, `total_rows` |
| `duplicate` | JSON | Matching `file_imports` row, if any (`processed` / `partial`) |
| `column_map` | JSON | Source → DB column; seeded by inspection, replaced on submit, re-used by Retry |
| `error` | VARCHAR(2000) | Inspection failure or why a submit did not enqueue |
| `created_at` | DATETIME | Set on insert |
| `updated_at` | DATETIME | Written explicitly on every change; the inspection heartbeat |

Indexes: `ix_uploads_owner_id`, `ix_uploads_status`.

### `import_conflicts`

| Column | Type | Description |
|---|---|---|
| `id` | BIGINT PK | Auto-increment ID |
| `record_data` | JSON NOT NULL | Full rejected row |
| `country`, `province`, `lon`, `lat`, `variety`, `season_type`, `opt_date` | matching types | Denormalized for conflict filtering |
| `check_sum` | VARCHAR(100) | Source file checksum |
| `source` | VARCHAR(50) | Originating filename |
| `created_at` | DATETIME | Auto-set on insert |

Index: `idx_conflict_created_at`.

### `users`

| Column | Type | Description |
|---|---|---|
| `id` | BIGINT PK | Auto-increment ID |
| `username` | VARCHAR(100) UNIQUE NOT NULL | Login name |
| `email` | VARCHAR(255) UNIQUE NOT NULL | Email |
| `password_hash` | VARCHAR(255) NOT NULL | bcrypt hash |
| `created_at` | DATETIME | Auto-set on insert |
| `status` | VARCHAR(20) NOT NULL | `pending` / `approved` / `rejected` / `disabled`. Server default `approved`, which backfilled existing accounts; registration writes `pending` explicitly (`approved` for an `ADMIN_EMAILS` address). Only `approved` accounts (and owners) can sign in or use a token |
| `role` | VARCHAR(20) NOT NULL | `user` (default) or `admin`. Owners in `ADMIN_EMAILS` are administrators whatever this says |
| `reviewed_at` | DATETIME | Last status/role change by an administrator (naive UTC) |
| `reviewed_by` | BIGINT | `users.id` of that administrator (no FK; INTEGER on SQLite) |

Index: `ix_users_status`. Added by migrations `a8c4e2f1d9b3` (`20261023100000_add_user_approval_status.py`: `status`, `reviewed_at`, `reviewed_by`, the index; `batch_alter_table` so it also runs on SQLite) and `e16b8fad5988` (`20261023142312_add_role_column_to_users_table.py`: `role`).

### `user_tokens`

| Column | Type | Description |
|---|---|---|
| `id` | BIGINT PK | Auto-increment ID |
| `user_id` | BIGINT FK → `users.id` | Owner |
| `token` | VARCHAR(128) NOT NULL | `{id}\|{secret}` bearer token |
| `expires_at` | DATETIME | NULL = never expires. Naive UTC, like every token timestamp (`_utcnow()` / `_as_naive_utc()` in `app/api/user.py`); mixing aware and naive values used to 500 on any expiring token |
| `last_used_at` | DATETIME | Refreshed by an authenticated request only when unset or older than 5 minutes, not on every request |
| `created_at` | DATETIME | Auto-set on insert |

Index: `idx_user_tokens_token`.

### `processing_runs`

One ingestion batch — every enqueue (upload submit, Jobs Retry, `/api/v1/data/upload`) opens one via `start_run(source='upload')`, and a startup/CLI scan opens one for all its files (`source='scan'`). A run whose task could not be enqueued is discarded. Replaced the old `job_progress` table (migration `20260930110505_replace_job_progress_with_processing_runs_and_processing_jobs.py`).

| Column | Type | Description |
|---|---|---|
| `id` | BIGINT PK | Auto-increment ID (INTEGER on SQLite) |
| `label` | VARCHAR(255) | Optional display label |
| `source` | VARCHAR(20) NOT NULL | Origin of the run: `upload` (default) or `scan` |
| `status` | VARCHAR(20) NOT NULL | Stored status, default `running` — **not** authoritative: `list_runs()` derives a run's status from its jobs |
| `total_files` | INTEGER | Files in the batch |
| `initiated_by` | VARCHAR(120) | Who started it |
| `created_at` | DATETIME | Auto-set on insert |
| `started_at` / `finished_at` | DATETIME | Run timing |

Index: `ix_processing_runs_created_at`.

### `processing_jobs`

One file's progress within a run.

| Column | Type | Description |
|---|---|---|
| `id` | BIGINT PK | Auto-increment ID (INTEGER on SQLite) |
| `run_id` | BIGINT FK → `processing_runs.id` | `ON DELETE CASCADE` |
| `file_name` | VARCHAR(255) NOT NULL | Stored file basename — **not** unique, so re-processing a file keeps its history |
| `original_filename` | VARCHAR(255) | Name as uploaded |
| `stored_path` | VARCHAR(512) | Real path on disk (differs after RDS → Parquet conversion) |
| `status` | VARCHAR(20) NOT NULL | `unknown` / `processing` / `completed` / `error`; `mark_stale()` reports an idle `processing` job as `stale` |
| `current_row` / `total_rows` | BIGINT | Rows processed / detected |
| `message` | VARCHAR(500) | Human-readable status |
| `error` | VARCHAR(2000) | Last failure detail, kept apart from `message` |
| `attempts` | INTEGER | Times this file has been processed — continues from the file's history in a new run, and increments when a run's job returns to `processing` (retry/redelivery) |
| `worker` | VARCHAR(120) | Worker that wrote the row |
| `started_at` / `finished_at` | DATETIME | Job timing |
| `updated_at` | DATETIME NOT NULL | Written explicitly on every update (not just a server default) |

Indexes: `ix_processing_jobs_run_id`, `ix_processing_jobs_status`, `ix_processing_jobs_updated_at`.

> Redis `job:{stem}` hashes are the hot path the SSE stream reads; these tables are the durable record, checkpointed every `JOB_DB_CHECKPOINT_SECONDS` and always written on terminal states, so a broker restart does not lose running jobs.

---

## 4. Key Modules Reference

| Layer | Module | Responsibility |
|---|---|---|
| ETL | `app/services/housekeeper.py` | Remote download, file discovery, parallel dispatch, chunked processing, checkpointing, `run()` (CLI entry), worker stop/resume |
| CLI wrapper | `housekeeping.py` | Typer front-end → `housekeeper.run()` (`[DATA_FOLDER] --watch/-w --dry-run --batch-size --chunk-size --checkpoint-interval`) |
| Watcher | `app/services/watch_handler.py` | watchdog-based directory monitoring for `--watch`; converts new RDS files to Parquet first |
| Progress | `app/services/progress_store.py` | Job progress: Redis hot path + `jobs:updates` pub/sub, `processing_runs` / `processing_jobs` durable record |
| Uploads | `app/services/uploads.py` | Upload storage, tus append + lock, background inspection, suggested mapping (`COLUMN_ALIASES`), mapping validation, abandoned-upload cleanup |
| Celery | `app/tasks.py` | `process_file_task` (accepts `column_map`) / `process_pending_task`; builds its own Flask app + `MyDb` |
| Download | `app/utils/downloader.py` | `RDSDownloader` — remote download with auth + SSRF guards |
| Conversion | `app/utils/rds_to_parquet.py` | `batch_convert()` — RDS → Parquet before every scan (per-file lock, atomic write); `convert_for_ingestion()` used by upload inspection and `/api/v1/data/upload` |
| Checksum | `app/utils/__init__.py` | `calculate_file_checksum()` — file hashing |
| Parsing | `pyreadr` / `pandas` (external) | RDS or Parquet → DataFrame |
| Models | `app/models/kvuno.py` | `PlantingRecommendation`, `FileImport`, `ImportConflict`, `Upload`, `User`, `UserToken`, `ProcessingRun`, `ProcessingJob` |
| DB Conn | `app/models/database_conn.py` | `MyDb` — SQLAlchemy initialization + health check |
| Repo | `app/repo/planting_recommendation.py` | `PlantingRecommendationRepo` — CRUD, batch insert, filtered queries, spatial search |
| Repo | `app/repo/file_import.py` | `FileImportRepo` — deduplication checks, offset upserts |
| Repo | `app/repo/import_conflict.py` | `ImportConflictRepo` — conflict recording and paginated queries |
| DTO | `app/dto/data_filters.py` | `PlantingDataFilter` — Pydantic query validation |
| DTO | `app/dto/planting_recommendation.py` | Response models for OpenAPI schema |
| API | `app/api/planting_data.py` | `public_api` (query) + `protected_api` (filters, coordinates, clusters, export) |
| API | `app/api/upload.py` | `POST /api/v1/data/upload` |
| API | `app/api/quality.py` | `GET /api/v1/quality/stats`, `/conflicts` — both `@require_auth` |
| API | `app/api/user.py` | Register (open to any address, new accounts `pending`; passwords ≤ 72 UTF-8 bytes), login (`403` + `status` when not active), `/me`, logout (bearer or `token` cookie), token management; `is_owner` / `is_admin` / `is_active` |
| API | `app/api/admin.py` | `/api/v1/admin/users` — list with per-status counts, approve / reject / disable (revokes tokens), role; all `@require_admin` |
| Mail | `app/services/mailer.py` | Optional SMTP (stdlib `smtplib`, daemon thread, never raises): activation, rejection, new-registration notice to `ADMIN_EMAILS`; logs `"email not sent: SMTP_HOST/MAIL_FROM unset" (logged with user ids, never addresses)` when `SMTP_HOST`/`MAIL_FROM` are unset |
| Auth | `app/auth.py` | `require_auth` — Bearer header or `token` cookie; `require_admin` — `401` unauthenticated, `403` not an administrator |
| Cache | `app/cache.py` | `@api_cache` (2xx `dict`/`list` bodies only) / `invalidate_planting_data_cache()` (Redis, no-op if unavailable) |
| Locking | `app/utils/file_lock.py` | `FileLock` — O_EXCL lock file with stale takeover; the per-file `.ingest.lock` |
| Routes | `app/routes/main.py` | `/` redirect (`SPA_ROOT_URL`), `/health`, the JSON `/ui/*` endpoints, `/ui/process` (Retry), SSE stream |
| Routes | `app/routes/uploads.py` | tus 1.0 server (`/ui/uploads/tus`) + upload lifecycle (`/ui/uploads/*`) |
| Frontend | `frontend/` | React + TypeScript SPA (Vite/pnpm), served by nginx; the UI is not in this repo's Python layer |
| App | `app/__init__.py` | `create_app()` — OpenAPI factory, CORS, rate limiter, security headers, DB init |
| Config | `app/config.py` | `Settings` (pydantic-settings): every environment variable, typed and validated; read with `get_settings()`. `build_db_url()` raises when `DB_USER`/`DB_PASSWORD`/`DB_NAME` are unset (no `DB_URL`, not sqlite) |

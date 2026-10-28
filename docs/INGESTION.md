# Ingesting Files

IITA Internal Use. Overview, quick start and the other guides: [README.md](../README.md).

How RDS and Parquet files reach the database: uploads, the data folder, the Celery worker, the housekeeping CLI and the maintenance scripts. [DATA-FLOW.md](DATA-FLOW.md) goes step by step through the pipeline.

## How files get processed

| Source | Where it is processed | Needs Redis/Celery |
|---|---|---|
| Upload page (tus) → **Process** | The Celery worker when one consumes the queue; otherwise a separate `ingest_runner` process the API starts (never a gunicorn thread). The upload goes to `processing`, then `completed` | No (a worker is used when there is one) |
| Files already in `HOUSEKEEPING_DATA_DIR` | Startup scan (`STARTUP_SCAN`, on by default), once per deployment: queued for the Celery worker when `HOUSEKEEPING_ENABLED=true`, otherwise run by **one** API process — the others, and workers recycled within `STARTUP_SCAN_COOLDOWN_SECONDS`, skip | No |
| `POST /api/v1/data/upload` | Celery worker when `HOUSEKEEPING_ENABLED=true`; otherwise saved for the next scan | Yes, to process at once |
| Jobs page **Retry** | An upload: like **Process**, re-using its saved mapping, data date and crop type. A plain data-folder file: Celery when `HOUSEKEEPING_ENABLED=true` | Only for plain files |
| `python housekeeping.py` / `HOUSEKEEPING_MODE` | The CLI process, or the worker container in housekeeping mode | No |

Every path ends in the same `process_file()`: resumable from a stored offset, deduplicated by checksum, with progress on the Jobs page. Several processes can scan the same folder safely: RDS → Parquet conversion takes a per-file lock and writes atomically, and ingestion takes a per-file lock, so a file is never converted or imported twice at once.

## File Processing (Celery Worker)

The worker is optional, but production runs one. With `HOUSEKEEPING_ENABLED=true` and a worker consuming the queue, it reads (inspects) and imports Upload-page files, converts and imports `POST /api/v1/data/upload` files, and takes retries of plain data-folder files.

Redis is **not** a compose service; run it externally and point `CELERY_BROKER_URL` / `CELERY_RESULT_BACKEND` at it.

```bash
# Start Redis (standalone Docker container, not part of docker-compose.yml)
docker run -d --name kvuno-redis -p 6379:6379 redis:7

# Start the Celery worker (Native — Linux)
celery -A app.celery_app worker --loglevel="${LOG_LEVEL:-info}"

# Start the Celery worker (Native — Windows, use solo pool)
celery -A app.celery_app worker --loglevel="${LOG_LEVEL:-info}" --pool solo

# Start the Celery worker (Docker)
docker compose up --build -d worker
```

Tasks are acked late and re-queued if the worker dies (`task_reject_on_worker_lost`), so `CELERY_VISIBILITY_TIMEOUT` (default 6 h) must exceed your longest single-file ingest or Redis hands a still-running task to a second worker. A per-file `<file>.ingest.lock` makes any such duplicate exit quietly. A database outage mid-file re-raises so Celery retries from the stored offset; a bad file is recorded as `error` at its last position and is not retried.

**Restarting the worker does not lose work.** On shutdown the running file stops at the next chunk boundary, commits its rows and position together, and shows "Paused for a worker restart"; when the worker starts again it clears its own stale locks and resumes every job it left unfinished, from where it stopped. Compose gives it `stop_grace_period: 2m` for this.

## Uploading Files (SPA)

The Upload page sends files with the in-house tus client (`frontend/src/lib/tus.ts`) to a tus 1.0 server (core + creation) in `app/routes/uploads.py`. The server issues the upload id and appends bytes at an offset, so a dropped connection, closed tab, or API restart resumes from the last byte. Each upload is a row in the `uploads` table and moves through:

```
receiving → received → inspecting → ready | failed → submitted → processing
```

1. **Receive** — bytes land in `HOUSEKEEPING_DATA_DIR/uploads/<id>/data.part`. The size cap (`MAX_FILE_SIZE_MB`) and extension are checked at creation, before any bytes arrive
2. **Inspect** — RDS → Parquet conversion, preview rows, SHA-256, duplicate lookup against `file_imports`, and a suggested column mapping. On the **Celery worker** when one consumes the `kvuno` queue (the page shows "Waiting for the worker" until it starts); otherwise on a background thread in the API
3. **Map** — the page shows the server's suggested mapping, which is exactly what will be sent. `lat` and `lon` must be mapped, and each database column at most once. The **Data date** (when the data was produced) becomes every imported row's `created_at`; it is pre-filled from the file's last-modified date, which editing or copying a file changes, so it is there to be checked. Left empty, rows take the date the file was first imported (RDS and Parquet files record no creation date of their own). The **Crop type** (e.g. `maize`, suggestions from earlier uploads) is stored in lower case on every imported row; if a file column is mapped to `crop_type` (a column named `crop` is mapped automatically), its values win and only its empty cells take the chosen crop. Leave it empty for a file that covers no single crop
4. **Process** — `POST /ui/uploads/<id>/process` (`{"column_map": {...}, "data_date": "2024-03-01", "crop_type": "maize"}`; omit a field to keep its saved value, `""` to clear it) saves the mapping, sets the upload to `processing` and imports it with that mapping as-is (not merged over `RDS_COLUMN_MAP`): on the **Celery worker** when there is one, otherwise in a separate `ingest_runner` process the API starts. Never inside a gunicorn worker — gunicorn recycles those every `MAX_REQUESTS` requests, which used to kill long imports over and over. Progress shows on the Jobs page. A submitted or processing upload can no longer be discarded (`409`)

In production run the worker as a Celery worker (`HOUSEKEEPING_MODE` unset), so the API never reads or imports files itself.

Uploads live under `uploads/` rather than the data directory root on purpose: the startup scan (`load_rds_to_db`) ingests every file in the root with the default mapping. Unfinished uploads idle longer than `UPLOAD_ABANDON_AGE` are deleted when an API process starts. The tus `HEAD`/`PATCH` and the status `GET` are exempt from the default rate limit, since a large file is many requests.

### Removing false import conflicts

Before the ingest fix, every imported row was also logged in `import_conflicts` as a duplicate of itself, which inflated the Quality page. `scripts/cleanup_conflicts.py` removes those false entries and keeps real ones (a clash with another file, or a key that really occurs twice in a file). It only reports unless given `--apply`:

```bash
python scripts/cleanup_conflicts.py            # how many would be removed
python scripts/cleanup_conflicts.py --apply    # remove them, 50,000 per transaction
```

In the production stack: `docker exec kvuno python scripts/cleanup_conflicts.py --apply`.

### Converting large RDS files locally

Reading an RDS takes about 150× its size in memory (`pyreadr` reads the whole file), so the server can refuse a large one with *"Reading this R file needs more memory than the server allows"*. Convert it on a machine with more memory and upload the Parquet instead. `scripts/convert_rds.py` uses the same converter as the server, shows the read time and the rows written, keeps the RDS, never leaves a partial `.parquet`, and exits 1 if any file failed:

```bash
python scripts/convert_rds.py                                # every .rds under static/, next to each file
python scripts/convert_rds.py data/field.RDS                 # writes data/field.parquet
python scripts/convert_rds.py data/ --out-dir converted/     # keeps data/'s folder layout under converted/
python scripts/convert_rds.py data/ --no-recursive           # only data/ itself, not its subfolders
python scripts/convert_rds.py data/field.RDS --overwrite     # replace an existing .parquet
make convert-rds ARGS="data/ -o converted/"
```

Directories are searched recursively, skipping hidden folders and `uploads/` (uploads in progress, which the app converts itself).

From R, `arrow::write_parquet(df, "field.parquet")` does the same.

## Housekeeping (on-demand import)

`housekeeping.py` imports the files in a folder directly, without Celery — for bulk loads or a manual re-scan. It is a Typer CLI over `housekeeper.run()`; `python housekeeping.py --help` lists the options.

```bash
python housekeeping.py                    # scan HOUSEKEEPING_DATA_DIR once
python housekeeping.py /path/to/data      # another folder
python housekeeping.py --dry-run          # report what would be imported, skipped or resumed
python housekeeping.py --watch            # keep running; import new files as they appear (-w)
python housekeeping.py --batch-size 5000 --chunk-size 20000 --checkpoint-interval 25
```

In Docker, run it in the worker container (same data volume, more memory than the API):

```bash
docker compose exec worker python housekeeping.py --dry-run
```

Or start the worker image **as** a housekeeping job instead of Celery by setting `HOUSEKEEPING_MODE` on it: `oneshot` (scan once and exit), `watch` (keep importing new files) or `dryrun`. Leave it unset for the normal Celery worker.

What happens during a run:
1. **Health check** — `SELECT 1` confirms the database is reachable
2. **Remote download** — Downloads files from `REMOTE_RDS_URLS` (if configured)
3. **Deduplication** — SHA-256 checksum lookup in `file_imports` table; fully imported files are skipped
4. **Resumable processing** — Files with a stored `offset` resume from that row; incremental checkpoints commit every `checkpoint_interval` batches
5. **Batch insert** — Records are inserted in savepoint-protected batches; individual batch failures are logged and skipped
6. **Graceful shutdown** — `Ctrl+C` (or `SIGTERM`) stops at the next chunk boundary, committing its rows and the offset together for later resumption; a second `Ctrl+C` force-quits immediately. These handlers are installed by `housekeeper.run()` (the CLI) only — gunicorn and Celery keep their own signal handling
7. **Telemetry** — Structured log events (`housekeeping.start/end`, `file.download_start/end`, `file.processing_start/end`) go through the app's logger, as JSON lines in production

Remote files are configured with the `REMOTE_RDS_*` settings (see [Configuration](CONFIGURATION.md)).

Downloads are SSRF-guarded (`app/utils/downloader.py`): HTTPS only unless `REMOTE_RDS_ALLOW_HTTP=true`; private, loopback, link-local and metadata addresses refused; redirects followed by hand (max 5), each hop re-checked, with the bearer token and `REMOTE_RDS_COOKIES` dropped when a redirect changes host; optional `REMOTE_RDS_ALLOWED_DOMAINS` (exact host or subdomain); `REMOTE_RDS_MAX_BYTES` (default 1 GiB) per file; and a post-connect peer-IP check against DNS rebinding (best effort, skipped behind an HTTP proxy).

`.RDS` files in the data folder are converted to `.parquet` automatically before every scan (and by the watcher as they arrive): Parquet streams in chunks, whereas an RDS must be read into memory whole.

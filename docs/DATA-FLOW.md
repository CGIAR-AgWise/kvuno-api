# Data Flow

This document describes the two main data flows in KVuno API: **ingestion** (RDS/Parquet files into the database) and **serving** (database records through the REST API).

> All ingestion logic lives in `app/services/housekeeper.py`. The root `housekeeping.py` is a thin `argparse` wrapper around `cli_run()`.

---

## 1. Ingestion Flow — RDS File Processing

RDS/Parquet files containing crop planting data are either uploaded through the UI/API (into `static/data/`, or `HOUSEKEEPING_DATA_DIR` if set) or downloaded from remote sources (`REMOTE_RDS_URLS`). They are then processed either by a Celery task (`worker` service) or by the standalone `housekeeping.py` CLI.

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
                                                              │  Map columns via           │
                                                              │  RDS_COLUMN_MAP + aliases  │
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

1. **(Optional) Remote download** — `download_remote_files()` (`app/services/housekeeper.py:190`) reads `REMOTE_RDS_URLS` (semicolon-delimited) and fetches each URL into the data dir. Supports bearer tokens (`REMOTE_RDS_TOKEN`), cookies (`REMOTE_RDS_COOKIES`), and custom headers (`REMOTE_RDS_HEADERS`). SSRF guards: HTTPS-only unless `REMOTE_RDS_ALLOW_HTTP=true`, private/loopback/link-local/metadata IPs rejected, `allow_redirects=False` unless the target validates, optional `REMOTE_RDS_ALLOWED_DOMAINS` allow-list. Failures are logged and skipped.
2. **File discovery** — `load_rds_to_db()` (`app/services/housekeeper.py:507`) scans the data dir for `.RDS` and `.parquet` files, both pre-existing and freshly downloaded
3. **Pre-flight DB health check** — `db_health_check()` runs `SELECT 1` before any processing
4. **Concurrent dispatch** — Files are processed in a `ThreadPoolExecutor` (size `HOUSEKEEPING_MAX_WORKERS`) using `submit()` + `as_completed()` so one failure does not abort the batch
5. **Checksum calculation** — Each file is hashed with SHA-256 (`app/utils/__init__.py::calculate_file_checksum`)
6. **Deduplication / resume** — `FileImportRepo.get_by_checksum()` checks `file_imports`. Fully imported files are skipped; a non-zero `offset` resumes mid-file
7. **File parsing** — `pyreadr.read_r()` for `.RDS`, `pandas.read_parquet()` for `.parquet`. The whole file is loaded, then iterated in chunks of `HOUSEKEEPING_CHUNK_SIZE` (default 5000)
8. **Column mapping** — Rows without coordinates are dropped. Column names are resolved through `load_column_map()` (`RDS_COLUMN_MAP` JSON) plus built-in aliases (`cultivar`→`variety`, `season`→`season_type`, `sowing_date`/`planting_date`/`date`→`opt_date`, `longitude`→`lon`, `latitude`/`lat_y`→`lat`, `long`/`lon_x`→`lon`). The file checksum is attached to every row
9. **Batch insert** — Records are bulk-inserted via `PlantingRecommendationRepo.batch_insert()`, each batch wrapped in `begin_nested()` (savepoint) and retried up to 3× with exponential backoff. `coordinates` is built as `POINT(lon lat)` WKT, SRID 4326
10. **Checkpointing** — Every `HOUSEKEEPING_CHECKPOINT_INTERVAL` batches (default 50) the outer transaction commits **and** the new `offset` is persisted. This ordering is what makes resumption safe — don't reorder it
11. **Conflict capture** — Duplicates against the `uq_planting_recommendation` unique constraint are recorded in `import_conflicts` (country/province/lon/lat/variety/season_type/opt_date)
12. **File tracking** — A `FileImport` record (file name, checksum, `original_filename`, final `offset`) is saved. A partially processed file is *not* marked as fully processed
13. **Graceful shutdown** — SIGTERM/SIGINT set a flag checked at each batch boundary: the current batch finishes and the offset is committed; a second signal force-quits
14. **Progress** — `save_progress()` in `app/services/progress_store.py` writes `job:{stem}` to Redis (falling back to the `job_progress` table) and publishes on the `jobs:updates` channel, which `/ui/jobs/events` streams to browsers
15. **Telemetry** — `emit_event()` writes structured JSON lines to stderr at `housekeeping.start/end`, `file.download_start/end`, `file.processing_start/end`

### Tuning knobs

| Env var | Default | Effect |
|---|---|---|
| `HOUSEKEEPING_DATA_DIR` | `static/data` | Where uploads and downloads live |
| `HOUSEKEEPING_BATCH_SIZE` | `2000` | Rows per savepoint-wrapped insert |
| `HOUSEKEEPING_CHUNK_SIZE` | `5000` | Rows per in-memory chunk |
| `HOUSEKEEPING_CHECKPOINT_INTERVAL` | `50` | Batches between offset commits |
| `HOUSEKEEPING_MAX_WORKERS` | `1` | Files processed in parallel |
| `RDS_COLUMN_MAP` | built-in defaults | JSON map of source column → ORM field |

### Tables affected

| Table | Action | Key columns |
|---|---|---|
| `planting_recommendations` | INSERT (bulk) | `check_sum`, `country`, `province`, `coordinates` (POINT), `lon`, `lat`, `variety`, `season_type`, `opt_date`, `planting_option` |
| `file_imports` | UPSERT | `check_sum` (unique), `file_name`, `original_filename`, `offset`, `processed_at` |
| `import_conflicts` | INSERT | `record_data` (JSON), `country`, `province`, `lon`, `lat`, `variety`, `season_type`, `opt_date`, `check_sum`, `source` |
| `job_progress` | UPSERT (only if Redis is down) | `file_name` (unique), `status`, `current_row`, `total_rows`, `message` |

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

1. **HTTP request** — `GET /api/v1/planting-data/?country=Zambia&variety=Soybean&page=1&per_page=20` (`app/api/planting_data.py:77`)
2. **Parameter extraction** — `page` and `per_page` from `request.args` (defaults 1 and 50; `per_page` is clamped to `[1, 500]` by `_clamp_per_page()`). All filter params are injected as a `PlantingDataFilter` Pydantic model (`app/dto/data_filters.py`)
3. **Validation** — Pydantic validators check:
   - `coordinates`: must match `lon,lat` format; lat in [-90, 90], lon in [-180, 180]
   - `opt_date`: must be `YYYY-MM-DD`
   - `sort_col` / `sort_dir`: allow-listed column names and direction
   - Whitespace is stripped; enum values resolved
4. **Query building** — `PlantingRecommendationRepo.get_filtered_data()` (`app/repo/planting_recommendation.py`) constructs a SQLAlchemy query with optional filters:
   - **Spatial**: If `coordinates` + `radius` provided, uses `ST_DWithin(geometry, point, radius)` for PostGIS radius search
   - **Exact match**: `country`, `variety`, `season_type`, `opt_date`, `planting_option`
   - **Partial/ILIKE**: `province` uses `ILIKE '%search%'`
   - **Sort**: `sort_col` / `sort_dir` (defaults to `id` ascending)
5. **Pagination** — `query.paginate(page, per_page)` returns a `QueryPagination` object with items, total count, and page metadata
6. **Response mapping** — Each ORM `PlantingRecommendation` row is converted to a `PlantingRecommendationRecord` dict (`app/api/planting_data.py`)
7. **JSON response** — Returns `{ data: [...], total, pages, current_page, per_page }` with HTTP 200, or 500 on error. The sibling endpoints `filters` (TTL 300s), `coordinates` (120s), and `clusters` (120s) are Redis-cached via `@api_cache`; new uploads call `invalidate_cache()` (`app/api/upload.py:66`) to drop those keys. The main data endpoint is **not** cached.

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

Six tables, all defined in `app/models/kvuno.py` and created by Alembic migrations in `alembic/versions/`.

### `planting_recommendations`

| Column | Type | Description |
|---|---|---|
| `id` | BIGINT PK | Auto-increment ID |
| `check_sum` | VARCHAR(100) NOT NULL | SHA-256 of source file |
| `coordinates` | GEOMETRY(POINT, 4326) NOT NULL | Spatial point (PostGIS) |
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

Unique constraint `uq_planting_recommendation` on `(country, province, lon, lat, variety, season_type, opt_date)` — this is what drives conflict detection.

Indexes: `idx_check_sum`, `idx_coordinates` (GiST), `idx_country`, `idx_province`, `idx_lon`, `idx_lat`, `idx_variety`, `idx_season_type`, `idx_opt_date`, `idx_planting_option`, plus a second GiST index `idx_planting_recommendations_coordinates`.

### `file_imports`

| Column | Type | Description |
|---|---|---|
| `id` | BIGINT PK | Auto-increment ID |
| `check_sum` | VARCHAR(100) UNIQUE NOT NULL | SHA-256 of processed file |
| `file_name` | VARCHAR(120) NOT NULL | Stored file name |
| `original_filename` | VARCHAR(255) | User-facing upload name |
| `offset` | BIGINT | Row offset reached — enables resume |
| `processed_at` | DATETIME | Auto-set on insert |

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

### `user_tokens`

| Column | Type | Description |
|---|---|---|
| `id` | BIGINT PK | Auto-increment ID |
| `user_id` | BIGINT FK → `users.id` | Owner |
| `token` | VARCHAR(128) NOT NULL | `{id}\|{secret}` bearer token |
| `expires_at` | DATETIME | NULL = never expires |
| `last_used_at` | DATETIME | Updated on each authenticated request |
| `created_at` | DATETIME | Auto-set on insert |

Index: `idx_user_tokens_token`.

### `job_progress`

| Column | Type | Description |
|---|---|---|
| `id` | BIGINT PK | Auto-increment ID |
| `file_name` | VARCHAR(255) UNIQUE NOT NULL | File stem |
| `status` | VARCHAR(20) | `unknown` / `processing` / `completed` / `error` |
| `current_row` | BIGINT | Rows processed so far |
| `total_rows` | BIGINT | Total rows detected |
| `message` | VARCHAR(500) | Human-readable status |
| `updated_at` | DATETIME | Auto-set on insert |

> `job_progress` is only the **fallback** store. When Redis is reachable, progress lives in the `job:{stem}` keys and this table stays empty.

---

## 4. Key Modules Reference

| Layer | Module | Responsibility |
|---|---|---|
| ETL | `app/services/housekeeper.py` | Remote download, file discovery, parallel dispatch, chunked processing, checkpointing, CLI |
| CLI wrapper | `housekeeping.py` | argparse front-end → `housekeeper.cli_run()` |
| Watcher | `app/services/watch_handler.py` | watchdog-based directory monitoring for `--watch` |
| Progress | `app/services/progress_store.py` | Job progress: Redis primary, `job_progress` table fallback, `jobs:updates` pub/sub |
| Celery | `app/tasks.py` | `process_file_task` / `process_pending_task`; builds its own Flask app + `MyDb` |
| Download | `app/utils/downloader.py` | `RDSDownloader` — remote download with auth + SSRF guards |
| Conversion | `app/utils/rds_to_parquet.py` | `batch_convert()` — RDS → Parquet |
| Checksum | `app/utils/__init__.py` | `calculate_file_checksum()` — file hashing |
| Parsing | `pyreadr` / `pandas` (external) | RDS or Parquet → DataFrame |
| Models | `app/models/kvuno.py` | `PlantingRecommendation`, `FileImport`, `ImportConflict`, `User`, `UserToken`, `JobProgress` |
| DB Conn | `app/models/database_conn.py` | `MyDb` — SQLAlchemy initialization + health check |
| Repo | `app/repo/planting_recommendation.py` | `PlantingRecommendationRepo` — CRUD, batch insert, filtered queries, spatial search |
| Repo | `app/repo/file_import.py` | `FileImportRepo` — deduplication checks, offset upserts |
| Repo | `app/repo/import_conflict.py` | `ImportConflictRepo` — conflict recording and paginated queries |
| DTO | `app/dto/data_filters.py` | `PlantingDataFilter` — Pydantic query validation |
| DTO | `app/dto/planting_recommendation.py` | Response models for OpenAPI schema |
| API | `app/api/planting_data.py` | `public_api` (query) + `protected_api` (filters, coordinates, clusters, export) |
| API | `app/api/upload.py` | `POST /api/v1/data/upload` |
| API | `app/api/quality.py` | `GET /api/v1/quality/stats`, `/conflicts` |
| API | `app/api/user.py` | Register, login, logout, token management |
| Auth | `app/auth.py` | `require_auth` — Bearer header or `token` cookie |
| Cache | `app/cache.py` | `@api_cache` / `invalidate_cache` (Redis, no-op if unavailable) |
| Routes | `app/routes/main.py` | `/` redirect, `/health`, all `/ui/*` pages, resumable upload endpoints, SSE stream |
| App | `app/__init__.py` | `create_app()` — OpenAPI factory, CORS, rate limiter, security headers, DB init |
| Config | `app/config.py` | Constants + `build_db_url()`; **raises at import if `DB_USER`/`DB_PASSWORD`/`DB_NAME` are unset** (no `DB_URL`) |

# API

IITA Internal Use. Overview, quick start and the other guides: [README.md](../README.md).

Authentication, accounts, every endpoint, pagination and the planting-data query. The live spec is at `/api-docs/swagger` (Swagger UI) and `/api-docs/openapi.json`.

## Authentication

All `/ui/*` JSON endpoints and most API routes require authentication. Obtain a token via:

```bash
curl -X POST http://localhost:5000/api/v1/users/login \
  -H "Content-Type: application/json" \
  -d '{"username": "johndoe", "password": "securePass123"}'
# Returns: {"msg": "login success", "access_token": "1|a1b2c3d4e5f6..."}
```

Use the token in subsequent requests:

```bash
curl http://localhost:5000/api/v1/planting-data/ \
  -H "Authorization: Bearer 1|a1b2c3d4e5f6..."
```

Or sign in through the SPA at `/login` — the token is stored as a cookie for browser navigation (`Secure` when the page is served over HTTPS). An unauthenticated browser hitting a protected endpoint is redirected to `SPA_LOGIN_PATH` (default `/login`); API clients still get a `401` JSON body. In the SPA, a `401` from any API call (an expired or revoked token) signs the user out and returns them to `/login?next=<page>`.

Registration (`POST /api/v1/users/register`) is open to any valid email address, but the new account is **pending** until an administrator approves it — see [Accounts and approval](#accounts-and-approval). Passwords longer than 72 UTF-8 bytes (bcrypt's limit) are rejected at registration (`422`); a login with one is simply `401`.

## Accounts and approval

Every new account starts `pending` (the register response is `201` with `"status": "pending"`) and can neither sign in nor use a token until an administrator approves it. A login with the right password for an account that is not active is `403` with `{"msg", "status"}` (`pending`, `rejected`, or `disabled`); a wrong password is always `401`, so an account's status is only revealed to someone who knows its password. Disabling or rejecting an account revokes all its tokens, and any token of an inactive account stops working at once (`401`).

Administrators are:

- **Owners** — the addresses in `ADMIN_EMAILS` (comma-separated, case-insensitive, read on every request). Registering with one of them skips the queue, and an owner cannot be disabled or demoted from the UI; access can always be recovered by editing that variable.
- Approved accounts that an administrator gave the **admin** role.

To bootstrap the first administrator:

1. Set `ADMIN_EMAILS=you@example.org` on the `api` and restart it.
2. Register at `/register` with that address — the account is approved immediately and you are signed in.
3. The navbar shows **Accounts** (`/admin/users`), with a badge counting pending requests. Approve, reject (with an optional reason), disable, re-activate, or make other approved accounts admins from there. You cannot change your own account, and owners show no actions.

Accounts that existed before migration `a8c4e2f1d9b3` are backfilled `approved`, so the upgrade locks nobody out.

Email is optional. With `SMTP_HOST` and `MAIL_FROM` set, the API sends an activation email on approval (linking `SPA_ROOT_URL` + `/login`), a rejection email with the optional reason, and a new-registration notice to every `ADMIN_EMAILS` address (linking `SPA_ROOT_URL` + `/admin/users`); the reviewer can untick "Email the user about my decision" to skip the user's email. Links are only included when `SPA_ROOT_URL` is an absolute `http(s)` URL. Without SMTP configured nothing is sent and the API log shows `"email not sent: SMTP_HOST/MAIL_FROM unset" (logged with user ids, never addresses)` for each message instead. Sending runs on a background thread and a failure is logged, never raised.

Error responses never carry exception text: a failing endpoint returns a generic `500` body and logs the exception server-side. Request validation errors (bad query/body parameters, including an unknown `sort_col`/`sort_dir`) are `422`, flask-openapi3's validation status.

## API Endpoints

> Rows marked *SPA route* are client-side React routes served by nginx — Flask has no HTML endpoints. In production nginx reverse-proxies `/api`, `/ui`, and `/health` to the `api` service, so the browser sees a single origin.

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET` | `/` | — | Browsers: a landing page linking the web app and API docs. Other clients: redirect to `SPA_ROOT_URL`, or a JSON pointer when it is unset |
| `GET` | `/health` | — | Health check with database status; `?deep=1` adds Redis, worker and data-directory state |
| `GET` | `/login`, `/register` | — | SPA routes — login and registration pages (nginx) |
| `GET` | `/jobs` | Required | SPA route — job list (nginx) |
| `GET` | `/explore` | Required | SPA route — map explorer (nginx) |
| `GET` | `/upload` | Required | SPA route — file upload (nginx) |
| `GET` | `/quality` | Required | SPA route — data quality dashboard (nginx) |
| `GET` | `/tokens` | Required | SPA route — token management (nginx) |
| `GET` | `/admin/users` | Admin | SPA route — account approval (nginx; non-admins are sent to `/jobs`) |
| `GET` | `/ui/jobs/data` | Required | JSON: job list |
| `GET` | `/ui/jobs/runs` | Required | Recent processing runs (files submitted together), newest first |
| `GET` | `/ui/jobs/events` | Required | SSE: live job updates (Redis pub/sub, 3s poll fallback) |
| `POST` | `/ui/uploads/tus` | Required | tus create — `Upload-Length` + `Upload-Metadata filename`; size and extension checked before any bytes (`413`/`415`) |
| `HEAD` | `/ui/uploads/tus/<id>` | Required | tus offset probe — `Upload-Offset` to resume from |
| `PATCH` | `/ui/uploads/tus/<id>` | Required | tus append at `Upload-Offset` (`409` offset mismatch, `423` another request holds the upload) |
| `GET` | `/ui/uploads` | Required | The caller's unfinished uploads (survives a page reload) |
| `GET` | `/ui/uploads/<id>` | Required | Upload status, preview, duplicate info, suggested mapping |
| `POST` | `/ui/uploads/<id>/process` | Required | Validate + save the column mapping, data date and crop type (`column_map`, `data_date`, `crop_type`), then start the import (`processing`) on the worker, or in an `ingest_runner` process without one |
| `DELETE` | `/ui/uploads/<id>` | Required | Discard an upload that is not yet submitted or processing (`409` otherwise) |
| `POST` | `/ui/process` | Required | (Re)process a stored file — Jobs page Retry; omit `column_map` to re-use the saved one |
| `GET` | `/ui/progress/<file_name>` | Required | One job's progress record (same shape as a `/ui/jobs/data` item), or `status: unknown` |
| `GET` | `/ui/columns` | Required | Mappable DB columns, aliases, `max_file_size`, and `crop_types` (crops chosen on earlier uploads, as suggestions) |
| `POST` | `/api/v1/users/register` | — | Register a new account — `201` with `status: pending` (or `approved` for an `ADMIN_EMAILS` address); `400` invalid email, `409` taken |
| `POST` | `/api/v1/users/login` | — | Authenticate and get a token (`401` wrong credentials, `403` + `status` when the account is pending/rejected/disabled) |
| `GET` | `/api/v1/users/me` | Required | The signed-in account: `id`, `username`, `email`, `status`, `role`, `is_admin` |
| `GET` | `/api/v1/admin/users` | Admin | Accounts, newest first (paginated; filter `status`, search `q` on username/email) plus `counts` per status |
| `POST` | `/api/v1/admin/users/<id>/approve` | Admin | Approve a pending, rejected, or disabled account; optional body `{"notify": true}` |
| `POST` | `/api/v1/admin/users/<id>/reject` | Admin | Reject a pending account and revoke its tokens; optional body `{"reason": "...", "notify": true}` (reason ≤ 500 chars) |
| `POST` | `/api/v1/admin/users/<id>/disable` | Admin | Disable an approved account and revoke all its tokens |
| `POST` | `/api/v1/admin/users/<id>/role` | Admin | `{"role": "admin"}` or `{"role": "user"}` — approved accounts only (`409` otherwise) |
| `POST` | `/api/v1/users/logout` | Required | Revoke the current token (bearer header or `token` cookie) |
| `POST` | `/api/v1/users/tokens` | Required | Create a new API token |
| `GET` | `/api/v1/users/tokens` | Required | List active tokens (paginated) |
| `DELETE` | `/api/v1/users/tokens/<id>` | Required | Revoke a specific token |
| `POST` | `/api/v1/data/upload` | Required | Upload RDS/Parquet files (queued for the worker when `HOUSEKEEPING_ENABLED=true`) |
| `GET` | `/api/v1/planting-data/` | **Public** | Paginated, filterable crop data. Each record has `coordinates` as a GeoJSON Point, `opt_date` as in the source (`30-Nov`, no year) plus `opt_month_day` (`--11-30`), `crop_type` (lower-case, `null` for rows imported before crop types were recorded), and ISO 8601 UTC `created_at`/`updated_at` |
| `GET` | `/api/v1/planting-data/filters` | Required | Distinct `country`, `province`, `variety`, `season_type` and `crop_type` values (paginated per column) |
| `GET` | `/api/v1/planting-data/coordinates` | Required | Map coordinates (paginated) |
| `GET` | `/api/v1/planting-data/clusters` | Required | Spatial clusters (paginated by cluster: `pages` from `total_clusters`; bad `zoom`/bounds → `400`) |
| `GET` | `/api/v1/planting-data/export` | Required | Export data (CSV/JSON, paginated; `0` values are kept). Columns: `country, province, lon, lat, variety, season_type, opt_date, planting_option, crop_type` — `crop_type` last, so existing column positions are unchanged |
| `GET` | `/api/v1/quality/stats` | Required | Quality statistics |
| `GET` | `/api/v1/quality/conflicts` | Required | Import conflicts (paginated; filters `country`, `crop_type`, `source`, `search`) |
| `GET` | `/api/v1/quality/duplicates` | Required | Conflicts grouped by country/province/variety/season/date/crop, most repeated first, with counts per source (paginated by group; same filters as `/conflicts`) |

## Pagination

Every collection endpoint is paginated server-side. `page` defaults to `1` and
`per_page` to `100`, clamped to a maximum of `500` (`MAX_PER_PAGE` in
`app/dto/pagination.py`). `per_page` is clamped rather than rejected, and `page`
is floored at `1` so a malformed value cannot produce a negative offset.

Responses carry `total`, `pages`, `current_page`, and `per_page` alongside their
rows. `page` is 1-based. `/planting-data/filters` is the one exception to a
single row axis: its columns are paginated independently, and `totals` / `pages`
are reported per column so a client can tell when each is exhausted.

`page` and `per_page` appear as query parameters on every paginated endpoint in
the OpenAPI spec and Swagger UI (`/api-docs/swagger`), from the shared
`PaginationQuery` model. Out-of-range or unparsable values are still clamped or
defaulted, never rejected.

`/quality/stats` returns aggregates rather than a record list, so it has no
pagination; `register`, `login`, `logout`, `upload`, and token creation return
single objects.

> `GET /api/v1/planting-data/` is intentionally **public** (rate-limited via `RATE_LIMIT_DATA`); the supporting endpoints under the same prefix require a token.

## Query Parameters for `/api/v1/planting-data/`

| Parameter | Type | Description |
|---|---|---|
| `page` | int | Page number (default: 1) |
| `per_page` | int | Items per page (default: 100, max 500) |
| `coordinates` | string | Center point for radius search (`lon,lat`) |
| `radius` | float | Search radius in metres (requires `coordinates`; measured on the geography, not in degrees) |
| `crop_type` | string | Crop, exact match, case-insensitive (`Maize` finds `maize`) |
| `country` | string | Country name, exact match |
| `province` | string | Province name (partial ILIKE match) |
| `variety` | string | Crop variety exact match |
| `season_type` | string | Season type exact match |
| `opt_date` | string | Optimal sowing day, which has no year: `30-Nov`, ISO month-day `--11-30`, or a full date such as `2026-11-30` whose year is ignored |
| `planting_option` | int | Planting option exact match |
| `sort_col` / `sort_dir` | string | Server-side sorting used by the explore UI. `sort_col` must be one of `id`, `check_sum`, `country`, `province`, `lon`, `lat`, `variety`, `season_type`, `opt_date`, `planting_option`, `crop_type`, `created_at`, `updated_at`; `sort_dir` `asc`/`desc`. Anything else is `422` |

Example:

```bash
curl "http://localhost:5000/api/v1/planting-data/?crop_type=maize&country=Kenya&page=1&per_page=10"
```

## Response Format

```json
{
  "data": [
    {
      "id": 1,
      "lat": -1.29,
      "lon": 36.82,
      "country": "Kenya",
      "province": "Nairobi",
      "variety": "H614",
      "season_type": "Average",
      "opt_date": "30-Nov",
      "opt_month_day": "--11-30",
      "planting_option": 1,
      "crop_type": "maize",
      "check_sum": "a1b2c3d4e5f6...",
      "coordinates": { "type": "Point", "coordinates": [36.82, -1.29] },
      "created_at": "2024-03-01T00:00:00Z",
      "updated_at": "2026-10-28T10:15:00Z"
    }
  ],
  "total": 42,
  "pages": 5,
  "current_page": 1,
  "per_page": 10
}
```

`coordinates` is a GeoJSON Point, `[lon, lat]`. `created_at` is the data date
confirmed when the file was mapped (else its first import time); `updated_at`
is when the row was imported.

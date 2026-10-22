# UI/UX Design Plan — kvuno

## Target Users

**Primary:** Data scientists at CGIAR / AgWise who ingest, validate, and serve
agricultural planting-recommendation data (RDS/Parquet files with spatial crop
information).

**Secondary:** API consumers building applications on top of the planting data.

### User goals

| Goal | Frequency | Current UX |
|------|-----------|------------|
| Upload RDS/Parquet files | Daily/weekly | ✅ Upload page (`/upload`) with tus (`frontend/src/lib/tus.ts`) drag-drop, resume, and column mapping |
| Preview data before ingestion | Every upload | ✅ Columns + sample rows + duplicate check from background inspection (`GET /ui/uploads/<id>`) |
| Monitor processing status | Per upload | ✅ Jobs page (`/jobs`) with live SSE updates |
| Explore / query ingested data | Daily | ✅ `/explore` — filters, map, paged table |
| Filter by country, variety, date | Daily | ✅ `<select>` dropdowns populated from `/planting-data/filters` |
| View data on a map | Weekly | ✅ Leaflet + markercluster + optional heat layer |
| Export data for offline analysis | Weekly | ✅ Server-streamed CSV / JSON from `/planting-data/export` |
| Understand data quality | Weekly | ✅ Quality dashboard (`/quality`) with conflicts, duplicates, coverage stats |
| Test API queries interactively | Monthly | ⚠️ Swagger at `/api-docs`, but no live query builder |

---

## Frontend Architecture

The pages are a standalone **React 18 + TypeScript SPA** in `frontend/`, built with
**Vite** and **pnpm** and served by **nginx**. Flask renders no HTML — it only exposes
JSON. `frontend/nginx.conf` serves the built bundle and reverse-proxies `/api`,
`/ui`, and `/health` to the `api` service, so production is **same-origin** and no
CORS preflight is needed; `vite dev` proxies the same paths to the API
(`VITE_DEV_PROXY_TARGET`, default `http://127.0.0.1:5000` to match the compose
`api` service), so local development matches it. Routes are code-split with `React.lazy`, which keeps
Leaflet, markercluster, and the heat layer out of the initial bundle.

Only Bootstrap's **CSS** is loaded, not its JS, so `data-bs-*` attributes do
nothing: the mobile navbar collapse is React state in `Layout.tsx`, and the Jobs
modal is a React component. Shared GETs go through `cached()`
(`frontend/src/lib/cache.ts`), where one subscriber aborting no longer cancels the
request for the others — under React StrictMode's double mount that used to leave
Explore empty. Explore's sortable headers are keyboard-operable and expose
`aria-sort`. nginx serves the SPA with its own CSP and security headers
(`frontend/security-headers.conf`; no CDN hosts) and never serves source maps.

| Layer | Value |
|---|---|
| Framework | React 18 + react-router 6 + TypeScript 5.7 |
| Build tool | Vite 6, package manager pnpm |
| Client routes | `/login`, `/register`, `/jobs`, `/explore`, `/upload`, `/quality`, `/tokens` |
| Runtime | nginx (`ghcr.io/cgiar-agwise/kvuno-web`, compose host `127.0.0.1:5001`) |
| Dev server | `pnpm dev` → Vite on `:5173` |

---

## Current UI Audit

### Page: Upload (`/upload`)

**What exists:**
- Drag-drop zone (multiple files) uploading with the in-house tus client (`frontend/src/lib/tus.ts`) in 5 MB pieces; a dropped connection, closed tab, or API restart resumes from the last byte, and re-selecting the same file after a reload continues it
- Size and type are refused up front: the SPA reads `max_file_size` from `/ui/columns` instead of hardcoding 20 MB, and the server rejects at tus creation before any bytes are sent
- Per-file status badge following the server lifecycle (`receiving` → `inspecting` → `ready` / `failed` → `submitted`), polled from `GET /ui/uploads/<id>`; inspection (RDS → Parquet, preview, checksum) runs server-side, so nothing waits on one long request
- Unfinished uploads are listed from `GET /ui/uploads` on load, so a reload never loses a file waiting to be mapped; each can be discarded
- Column mapping UI seeded with the **server's** suggestion, so the selects show exactly what will be sent; *skip* removes a column. `lat`/`lon` are required and each DB column can be used once
- Duplicate warning when the same content was already ingested (or partially ingested)
- Processing trigger (`POST /ui/uploads/<id>/process`), which enqueues a Celery task, returns `503` and leaves the upload `ready` when the broker is unreachable, or stores the mapping when `HOUSEKEEPING_ENABLED=false`

**Gaps:**
- No per-column type information
- No way to re-map or correct after submission

### Page: Jobs (`/jobs`)

**What exists:**
- SSE live-updating table with status, progress bar, timestamps
- Status filter tabs (All / Completed / Processing / Failed) and filename search
- Per-job detail modal with a Retry action, wired to `POST /ui/process` with only the file name, so the server re-uses the mapping saved with the upload (it used to send `{}` and wipe it). The modal reads `/ui/progress/<file_name>`, which returns the job record from `app/services/progress_store.py` (Redis hot path, Postgres durable record) — it used to read a `.progress.json` file nothing wrote, so it always showed `unknown` and hid Retry
- The modal is a real dialog: `role="dialog"`, focus moves into it and back on close, Escape closes it
- Rows open their detail from the keyboard (focusable, Enter/Space), and progress bars carry an accessible label and value
- Highlight scroll from upload redirect

**Gaps:**
- No per-job logs, row counts, or duration
- No history beyond the live progress store

### Page: Quality (`/quality`)

**What exists:**
- Summary stat cards (total records, conflicts, files, spatial coverage %)
- Paginated conflicts table with country/source/search filters
- Duplicates viewer grouped by unique record key
- Source breakdown with bar visualization
- Spatial coverage heatmap on the explore page

**Gaps:**
- No per-job conflict detail linking back to the source file
- No data quality trend/history over time
- No automated quality checks (null counts, outlier detection)

---

## Pagination: Paged, Not Infinite Scroll

The explore table uses **numbered pagination buttons, and this is a deliberate decision** — not an unfinished feature. Do not "fix" it by adding infinite scroll.

Rationale:

- The target users compare specific pages and share filtered URLs. A scroll position is not shareable or restorable the way `?page=3` is, and filter state is already URL-persisted via the router's search params.
- The page count communicates result-set size, which matters when inspecting a large ingestion.
- Server-side `LIMIT/OFFSET` is already indexed and fast, so infinite scroll would add client-side row-accumulation state for no query-plan gain.
- Keeping each page fetch a single request keeps the render path trivial — the table body is one state update per page.

Defaults: `perPage=200`, hard-capped at 500 server-side by `_clamp_per_page()` (`app/api/planting_data.py:70-74`).

---

## Technology Choices

| Concern | Choice | Notes |
|---------|--------|-------|
| CSS framework | Bootstrap 5.3 | npm dependency of the `frontend/` package |
| Icons | Bootstrap Icons | npm dependency |
| Map | Leaflet + `leaflet.markercluster` | `chunkedLoading: true`; optional `leaflet.heat` overlay |
| Charts | Bar rendering for the source breakdown | `/quality` |
| Tables | React components | One page of rows per request; no DataTables |
| Export | Server streaming endpoint | Client-side Blob only works below ~10k rows |
| State in URL | react-router search params | Filters, page, and sort are shareable |
| Build tool | Vite 6 + pnpm | `tsc -b && vite build`; output served by nginx |
| Job progress | Redis hash + pub/sub (`jobs:updates`) as the hot path; `processing_runs` / `processing_jobs` (Postgres) as the durable record, checkpointed every `JOB_DB_CHECKPOINT_SECONDS` | `app/services/progress_store.py` |

UX stance: data scientists are not frontend engineers, so the pages stay a thin
client over the existing JSON endpoints — no client-side data modelling, and the
filter/pagination contract is unchanged by the move to React.

---

## Routes Map (current)

```
GET  /                    → redirect → SPA_ROOT_URL (default /)
GET  /health              → JSON: app + database status

Client-side (React, served by nginx — no Flask route):
  /login                   → login page
  /register                → registration page
  /jobs                    → jobs page
  /explore                 → data explorer
  /upload                  → upload page (lib/tus.ts)
  /quality                 → data quality dashboard
  /tokens                  → token management

Flask (JSON, reverse-proxied by nginx):
GET    /ui/columns                 → JSON: mappable DB columns, aliases, max_file_size
GET    /ui/jobs/data               → JSON: job list
GET    /ui/jobs/events             → SSE: live updates (Redis pub/sub, 3s poll fallback)
POST   /ui/uploads/tus             → tus create (size/type checked before any bytes; 413/415)
HEAD   /ui/uploads/tus/<id>        → tus Upload-Offset, for resuming
PATCH  /ui/uploads/tus/<id>        → tus append at offset (409 mismatch, 423 busy)
GET    /ui/uploads                 → JSON: the caller's unfinished uploads
GET    /ui/uploads/<id>            → JSON: status, preview, duplicate, suggested mapping
POST   /ui/uploads/<id>/process    → validate + save mapping, enqueue ingestion
DELETE /ui/uploads/<id>            → discard an unsubmitted upload
POST   /ui/process                 → re-process a stored file (Jobs Retry; re-uses the saved mapping)
GET    /ui/progress/<file>         → JSON: one job record from progress_store (or status "unknown")

POST   /api/v1/users/register          → create account (REGISTRATION_ALLOWED_DOMAINS only; 403 when unset)
POST   /api/v1/users/login             → {id}|{secret} token
POST   /api/v1/users/logout            → revoke current token (bearer or token cookie)
GET    /api/v1/users/tokens            → list tokens
POST   /api/v1/users/tokens            → create token
DELETE /api/v1/users/tokens/<id>       → revoke token

GET  /api/v1/planting-data             → JSON: filtered data (public, rate-limited)
GET  /api/v1/planting-data/filters     → JSON: distinct filter values (per-column paged)
GET  /api/v1/planting-data/coordinates → JSON: lat/lon points for the map (paged)
GET  /api/v1/planting-data/clusters    → JSON: server-side spatial clusters (paged)
GET  /api/v1/planting-data/export      → CSV / JSON stream of one page of results

POST /api/v1/data/upload               → upload a single RDS/Parquet file
GET  /api/v1/quality/stats             → JSON: summary statistics (auth required)
GET  /api/v1/quality/conflicts         → JSON: paginated conflict list (auth required)
```

Every Flask `/ui/*` endpoint requires a session (`@require_auth`); they redirect browsers to the SPA login route (`SPA_LOGIN_PATH`, default `/login`) and return `401` JSON to API clients. The React routes are guarded client-side by `AuthContext` (a single provider). A `401` from **any** API call dispatches `kvuno:unauthorized` (`frontend/src/api.ts`); `AuthContext` drops the token and `RequireAuth` sends the user to `/login?next=<page>`, instead of leaving a signed-in shell whose every request fails. Logout always clears local state, even if the server call fails. The token cookie is `Secure` on HTTPS.

### API pagination

Every collection endpoint under `/api/v1/` is paginated server-side. `page`
defaults to `1`, `per_page` to `100`, clamped to `MAX_PER_PAGE` (500). Values are
clamped rather than rejected, and `page` is floored at `1` so a malformed
parameter cannot produce a negative SQL offset. All of it funnels through
`get_pagination()` in `app/dto/pagination.py`, so the default and the ceiling
exist in exactly one place.

Responses carry `total` / `pages` / `current_page` / `per_page` next to their
rows. `/planting-data/filters` is the exception: it returns a dict of four
independent lists, so `page` slices each column on its own and `totals` / `pages`
are reported per column. `/quality/stats` returns aggregates rather than a record
list and so has no pagination.

Ordering is always tie-broken on the primary key (`get_filtered_data`).
A non-unique sort column leaves row order ambiguous, and `LIMIT`/`OFFSET` over an
ambiguous order silently repeats and skips rows as a client pages.

Client-side, the Explore page maps the rows of the **current** page and fetches
`/planting-data/coordinates` and `/planting-data/clusters` for the heatmap and
cluster layers — the aggregation is server-side rather than accumulated in the
browser, so there is no page-walking cap to reason about.

---

## Design Principles

1. **Data scientists are not frontend devs** — the UI must be intuitive, self-documenting, and require zero configuration.
2. **Progressive disclosure** — show simple filters first, advanced options on expand.
3. **Feedback on every action** — loading states, progress bars, success/error messages.
4. **Shareable state** — every filter configuration should be encoded in the URL.
5. **Keyboard-friendly** — tab through filters, enter to search.
6. **Mobile-conscious** — data tables scroll horizontally, maps stack vertically.
7. **No page reload for data actions** — filters, pagination, and export are JS-driven, but pagination stays paged rather than scroll-driven (see [Pagination](#pagination-paged-not-infinite-scroll)).

# UI/UX Design Plan — kvuno

## Target Users

**Primary:** Data scientists at CGIAR / AgWise who ingest, validate, and serve
agricultural planting-recommendation data (RDS/Parquet files with spatial crop
information).

**Secondary:** API consumers building applications on top of the planting data.

### User goals

| Goal | Frequency | Current UX |
|------|-----------|------------|
| Upload RDS/Parquet files | Daily/weekly | ✅ Upload page with resumable.js drag-drop + column mapping |
| Preview data before ingestion | Every upload | ✅ Columns + sample rows returned by `/ui/upload/complete` |
| Monitor processing status | Per upload | ✅ Jobs page with live SSE updates |
| Explore / query ingested data | Daily | ✅ `/ui/explore` — filters, map, paged table |
| Filter by country, variety, date | Daily | ✅ `<select>` dropdowns populated from `/planting-data/filters` |
| View data on a map | Weekly | ✅ Leaflet + markercluster + optional heat layer |
| Export data for offline analysis | Weekly | ✅ Server-streamed CSV / JSON from `/planting-data/export` |
| Understand data quality | Weekly | ✅ Quality dashboard with conflicts, duplicates, coverage stats |
| Test API queries interactively | Monthly | ⚠️ Swagger at `/api-docs`, but no live query builder |

---

## Current UI Audit

### Page: Upload (`/ui/upload`)

**What exists:**
- Drag-drop zone with Resumable.js chunked upload
- Column mapping UI (auto-match + manual select) with sample values
- Processing trigger (`POST /ui/process`), which either enqueues a Celery task or stores the mapping when `HOUSEKEEPING_ENABLED=false`

**Gaps:**
- Uploads one file at a time (Resumable.js `maxFiles: 1`)
- No per-column type information
- No way to re-map or correct after submission

### Page: Jobs (`/ui/jobs`)

**What exists:**
- SSE live-updating table with status, progress bar, timestamps
- Status filter tabs (All / Completed / Processing / Failed) and filename search
- Per-job detail modal with a Retry action, wired to `POST /ui/process`
- Highlight scroll from upload redirect

**Known issue:** the detail modal fetches `/ui/progress/<file_name>`, which reads a `.progress.json` file that nothing writes anymore — progress moved to Redis / the `job_progress` table via `app/services/progress_store.py`. The modal therefore shows `unknown` and the Retry button stays hidden. Fix by having `/ui/progress/<file_name>` return the record from `progress_store` instead of reading a file.

**Gaps:**
- No per-job logs, row counts, or duration
- No history beyond the live progress store

### Page: Quality (`/ui/quality`)

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

- The target users compare specific pages and share filtered URLs. A scroll position is not shareable or restorable the way `?page=3` is, and filter state is already URL-persisted via `history.replaceState`.
- The page count communicates result-set size, which matters when inspecting a large ingestion.
- Server-side `LIMIT/OFFSET` is already indexed and fast, so infinite scroll would add client-side row-accumulation state for no query-plan gain.
- Keeping the table body a single `innerHTML` assignment per page keeps the render path trivial (`app/static/js/explore.js:140`, `renderPagination()` at `:155-176`).

Defaults: `perPage=200`, hard-capped at 500 server-side by `_clamp_per_page()` (`app/api/planting_data.py:70-74`).

---

## Technology Choices

| Concern | Choice | Notes |
|---------|--------|-------|
| CSS framework | Bootstrap 5.3 (jsDelivr CDN) | Also vendored under `node_modules/` for reference |
| Icons | Bootstrap Icons (CDN) | |
| Map | Leaflet + `leaflet.markercluster` (CDN) | `chunkedLoading: true`; optional `leaflet.heat` overlay |
| Charts | Chart.js-style bar rendering in vanilla JS | Source breakdown on `/ui/quality` |
| Tables | Server-rendered HTML + vanilla JS | No DataTables; body is one `innerHTML` assignment per page |
| Export | Server streaming endpoint | Client-side Blob only works below ~10k rows |
| State in URL | `URLSearchParams` + `history.replaceState` | Filters, page, and sort are shareable |
| Build tool | None | Static assets served by Flask; no JS build step |
| Job progress | Redis pub/sub (`jobs:updates`), DB fallback | `app/services/progress_store.py` |

There is **no `package.json`** — the `node_modules/` directory at the repo root is a leftover, not part of the build. All third-party JS/CSS loads from jsDelivr or unpkg, which is why the CSP in `app/__init__.py` allows those origins.

UX stance: data scientists are not frontend engineers, so the UI stays server-rendered (Jinja) with vanilla-JS progressive enhancement and no SPA framework.

---

## Routes Map (current)

```
GET  /                    → redirect → /ui/jobs
GET  /health              → JSON: app + database status

GET  /ui/login            → login page
GET  /ui/register         → registration page
GET  /ui/jobs             → jobs page
GET  /ui/jobs/data        → JSON: job list
GET  /ui/jobs/events      → SSE: live updates (Redis pub/sub, 3s poll fallback)
GET  /ui/upload           → upload page (resumable.js)
GET  /ui/upload/resumable → chunk probe (200 exists / 204 missing)
POST /ui/upload/resumable → receive one chunk
POST /ui/upload/complete  → merge chunks, return columns + sample rows
POST /ui/process          → save column mapping, enqueue ingestion
GET  /ui/progress/<file>  → JSON: per-file progress (⚠️ currently reads a stale `.progress.json` file — see Jobs audit)
GET  /ui/explore          → data explorer page
GET  /ui/quality          → data quality dashboard
GET  /ui/tokens           → token management
GET  /ui/columns          → JSON: mappable DB columns + aliases

POST   /api/v1/users/register          → create account
POST   /api/v1/users/login             → {id}|{secret} token
POST   /api/v1/users/logout            → revoke current token
GET    /api/v1/users/tokens            → list tokens
POST   /api/v1/users/tokens            → create token
DELETE /api/v1/users/tokens/<id>       → revoke token

GET  /api/v1/planting-data             → JSON: filtered data (public, rate-limited)
GET  /api/v1/planting-data/filters     → JSON: distinct filter values (per-column paged)
GET  /api/v1/planting-data/coordinates → JSON: lat/lon points for the map (paged)
GET  /api/v1/planting-data/clusters    → JSON: server-side spatial clusters (paged)
GET  /api/v1/planting-data/export      → CSV / JSON stream of one page of results

POST /api/v1/data/upload               → upload a single RDS/Parquet file
GET  /api/v1/quality/stats             → JSON: summary statistics
GET  /api/v1/quality/conflicts         → JSON: paginated conflict list
```

All `/ui/*` routes except `/ui/login` and `/ui/register` require a session (`@require_auth`); they redirect browsers to `/ui/login` and return `401` JSON to API clients.

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

Client-side, `explore.js` walks pages and accumulates (`fetchAllPages`) so the
map, heatmap, and filter dropdowns still render a complete set rather than just
the first page. It stops at `MAX_PAGES` (200) and tells the user when the result
is a sample rather than pretending it is complete.

---

## Design Principles

1. **Data scientists are not frontend devs** — the UI must be intuitive, self-documenting, and require zero configuration.
2. **Progressive disclosure** — show simple filters first, advanced options on expand.
3. **Feedback on every action** — loading states, progress bars, success/error messages.
4. **Shareable state** — every filter configuration should be encoded in the URL.
5. **Keyboard-friendly** — tab through filters, enter to search.
6. **Mobile-conscious** — data tables scroll horizontally, maps stack vertically.
7. **No page reload for data actions** — filters, pagination, and export are JS-driven, but pagination stays paged rather than scroll-driven (see [Pagination](#pagination-paged-not-infinite-scroll)).

# UI/UX Design Plan — kvuno

## Target Users

**Primary:** Data scientists at CGIAR / AgWise who ingest, validate, and serve
agricultural planting-recommendation data (RDS/Parquet files with spatial crop
information).

**Secondary:** API consumers building applications on top of the planting data.

### User goals

| Goal | Frequency | Current UX |
|------|-----------|------------|
| Upload RDS/Parquet files | Daily/weekly | ✅ Upload page (`/upload`) with tus (`frontend/src/lib/tus.ts`) drag-drop, resume, column mapping, Data date and Crop type |
| Preview data before ingestion | Every upload | ✅ Columns + sample rows + duplicate check from background inspection (`GET /ui/uploads/<id>`) |
| Monitor processing status | Per upload | ✅ Jobs page (`/jobs`) with live SSE updates |
| Explore / query ingested data | Daily | ✅ `/explore` — filters, map, paged table |
| Filter by crop, country, variety, date | Daily | ✅ `<select>` dropdowns (Crop, Country, Province, Variety, Season) populated from `/planting-data/filters` |
| View data on a map | Weekly | ✅ Leaflet + markercluster + optional heat layer |
| Export data for offline analysis | Weekly | ✅ Server-streamed CSV / JSON from `/planting-data/export` |
| Understand data quality | Weekly | ✅ Quality dashboard (`/quality`) with conflicts, duplicates, coverage stats |
| Test API queries interactively | Monthly | ⚠️ Swagger at `/api-docs` (every filter plus `page`/`per_page` on paginated endpoints), but no live query builder |

---

## Frontend Architecture

The pages are a standalone **React 19 + TypeScript SPA** in `frontend/`, built with
**Vite** and **pnpm** and served by **nginx**. Flask renders no HTML — it only exposes
JSON. `frontend/nginx.conf` serves the built bundle and reverse-proxies `/api`,
`/ui`, and `/health` to the `api` service, so production is **same-origin** and no
CORS preflight is needed; `vite dev` proxies the same paths to the API
(`VITE_DEV_PROXY_TARGET`, default `http://127.0.0.1:5000` to match the compose
`api` service), so local development matches it. Routes are code-split with `React.lazy`, which keeps
Leaflet, markercluster, and the heat layer out of the initial bundle.

Only Bootstrap's **CSS** is loaded, not its JS, so `data-bs-*` attributes do
nothing: the sidebar (a drawer on phones, collapsible to icons on desktop) is
React state in `Layout.tsx`, and the Jobs modal is a React component. Each page's
title, nav group and column width come from the `GROUPS` table in `Layout.tsx`;
the content is a centred column sized per page.

**Look.** Bootstrap re-themed in `src/lib/theme.css` with AKILIMO's brand, taken
from akilimo.org: red `#CE1A2B` (primary actions, links, the current page, the
square mark), slate `#303744` (sidebar, titles), background `#F3F5F7`,
Montserrat for the wordmark and titles and Open Sans for text (self-hosted).
Otherwise deliberately plain: hairline borders, no textures or taglines. Every
status uses `components/StatusBadge.tsx` — blue processing, green completed, red
failed, amber stalled, grey pending, each with its own icon — never Bootstrap's
`primary`, which is the brand red. Page changes run as React transitions and
page code is prefetched when the browser is idle, so moving between pages does
not flash a loading screen. Shared GETs go through `cached()`
(`frontend/src/lib/cache.ts`), where one subscriber aborting no longer cancels the
request for the others — under React StrictMode's double mount that used to leave
Explore empty. Explore's sortable headers are keyboard-operable and expose
`aria-sort`. nginx serves the SPA with its own CSP and security headers
(`frontend/security-headers.conf`; no CDN hosts) and never serves source maps.

| Layer | Value |
|---|---|
| Framework | React 19 + react-router 6 + TypeScript 5.7 |
| Build tool | Vite 8, package manager pnpm |
| Client routes | `/login`, `/register`, `/upload`, `/jobs`, `/explore`, `/quality`, `/tokens`, `/admin/users` (admins only) |
| Runtime | nginx (`ghcr.io/cgiar-agwise/kvuno-web`, compose host `127.0.0.1:5001`) |
| Dev server | `pnpm dev` → Vite on `:5173` |

---

## Current UI Audit

### Page: Upload (`/upload`)

**What exists:**
- Drag-drop zone (multiple files) uploading with the in-house tus client (`frontend/src/lib/tus.ts`) in 5 MB pieces; a dropped connection, closed tab, or API restart resumes from the last byte, and re-selecting the same file after a reload continues it
- Size and type are refused up front: the SPA reads `max_file_size` from `/ui/columns` instead of hardcoding 20 MB, and the server rejects at tus creation before any bytes are sent
- Per-file status badge following the server lifecycle (`receiving` → `inspecting` → `ready` / `failed` → `submitted` → `processing`), polled from `GET /ui/uploads/<id>`; inspection (RDS → Parquet, preview, checksum) runs server-side, so nothing waits on one long request. While it runs the card shows the stage from the upload's `progress`: "reading the R file (1m 15s)" (pyreadr gives no percentage), then a bar of rows written to Parquet, then preview and checksum
- Jobs page: data-folder RDS conversions (scan, watcher) appear as `Converting` jobs under the RDS name, indeterminate while reading and a row count while writing, counted under the Processing filter
- Unfinished uploads are listed from `GET /ui/uploads` on load, so a reload never loses a file waiting to be mapped; each can be discarded until it is submitted
- **Awaiting mapping** list: every inspected file waiting for a mapping — the user's own uploads and data-folder files the scan held because the default mapping found no lat/lon (labelled "Data folder", shared by all users, discard admin-only). Rows are collapsed with name, source and row count; a click opens the mapping, `?map=<id>` opens one directly, and a file uploaded on this visit opens by itself once read. **Process** sends only opened files, so nothing goes out with a mapping nobody looked at. The sidebar's Upload item shows the count (`GET /ui/uploads/awaiting`), refreshed on navigation and on `kvuno:uploads-changed`
- Column mapping UI seeded with the **server's** suggestion, so the selects show exactly what will be sent; *skip* removes a column. `lat`/`lon` are required and each DB column can be used once
- Duplicate warning when the same content was already ingested (or partially ingested)
- Processing trigger (`POST /ui/uploads/<id>/process`): saves the mapping and imports the file on a background thread in the API — no worker or Redis needed — with progress on the Jobs page

**Gaps:**
- No per-column type information
- No way to re-map or correct after submission

### Page: Jobs (`/jobs`)

**What exists:**
- SSE live-updating table with status, progress bar, timestamps
- Status filter tabs (All / Completed / Processing / Failed) and filename search
- Per-job detail modal with a Retry action, wired to `POST /ui/process` with only the file name, so the server re-uses the mapping saved with the upload (it used to send `{}` and wipe it). The modal reads `/ui/progress/<file_name>`, which returns the job record from `app/services/progress_store.py` (Redis hot path, Postgres durable record) — it used to read a `.progress.json` file nothing wrote, so it always showed `unknown` and hid Retry
- Administrators get **Delete file** in the modal (not while the job is processing or converting): a second, inline confirmation, then `DELETE /ui/jobs/<file>` removes the file, its upload and its job history. The confirmation says imported rows are kept, and offers an **Also delete the imported data (Undo import)** checkbox that sends `{"purge_rows": true}` to remove them and the checksum record too — irreversible, so it is never the default
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
- Duplicates viewer: conflicts grouped by country/province/variety/season/date/crop on the server (`GET /api/v1/quality/duplicates`), one page at a time
- Source breakdown with bar visualization
- Spatial coverage heatmap on the explore page

**Gaps:**
- No per-job conflict detail linking back to the source file
- No data quality trend/history over time
- No automated quality checks (null counts, outlier detection)

### Page: Accounts (`/admin/users`)

Administrators only — owners in `ADMIN_EMAILS` and accounts with the `admin` role. The route is wrapped in `RequireAdmin` (`App.tsx`), which waits for `me` and sends everyone else to `/jobs`; the server enforces the same with `@require_admin` (`403`).

**What exists:**
- Sidebar **Accounts** link, shown only to administrators, with a badge counting pending requests; the signed-in username and role are shown above the sidebar's **Sign out** button
- Status tabs Pending (default — the only one that needs action) / Approved / Rejected / Disabled / All, each with its count, plus a username/email search
- Row actions: **Approve** (pending) or **Re-activate** (rejected/disabled), **Reject** with an inline optional reason, **Disable** with an inline confirm, **Make admin** / **Remove admin** on approved accounts
- An "Email the user about my decision" switch (sent as `notify`); emails only go out when SMTP is configured
- **Owner** and **Admin** badges; owners and your own row show no actions, so nobody can lock themselves out

Around it: `/register` shows "Request received" for a pending account instead of signing in (an `ADMIN_EMAILS` address is approved and signed straight in), and `/login` shows a warning notice for a `403` (pending, rejected, or disabled) instead of "invalid credentials".

**Gaps:**
- No audit history beyond `reviewed_at` / `reviewed_by` and the API log

---

## Pagination: Paged, Not Infinite Scroll

The explore table uses **numbered pagination buttons, and this is a deliberate decision** — not an unfinished feature. Do not "fix" it by adding infinite scroll.

Rationale:

- The target users compare specific pages and share filtered URLs. A scroll position is not shareable or restorable the way `?page=3` is, and filter state is already URL-persisted via the router's search params.
- The page count communicates result-set size, which matters when inspecting a large ingestion.
- Server-side `LIMIT/OFFSET` is already indexed and fast, so infinite scroll would add client-side row-accumulation state for no query-plan gain.
- Keeping each page fetch a single request keeps the render path trivial — the table body is one state update per page.

Defaults: `perPage=200`, hard-capped at 500 server-side by `get_pagination()` (`app/dto/pagination.py`).

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
| Build tool | Vite 8 + pnpm | `tsc -b && vite build`; output served by nginx |
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
  /admin/users             → account approval (RequireAdmin; others → /jobs)

Flask (JSON, reverse-proxied by nginx):
GET    /ui/columns                 → JSON: mappable DB columns, aliases, max_file_size, crop_types (suggestions)
GET    /ui/jobs/data               → JSON: job list
GET    /ui/jobs/runs               → JSON: recent processing runs
GET    /ui/jobs/events             → SSE: live updates (Redis pub/sub, 3s poll fallback)
DELETE /ui/jobs/<file>             → admin only: delete the file, its upload and its job history (imported rows kept unless body `{purge_rows: true}`; 409 while processing)
POST   /ui/uploads/tus             → tus create (size/type checked before any bytes; 413/415)
HEAD   /ui/uploads/tus/<id>        → tus Upload-Offset, for resuming
PATCH  /ui/uploads/tus/<id>        → tus append at offset (409 mismatch, 423 busy)
GET    /ui/uploads                 → JSON: the caller's unfinished uploads + data-folder files held for mapping
GET    /ui/uploads/awaiting        → JSON: {count} of those waiting for a mapping (sidebar badge)
GET    /ui/uploads/<id>            → JSON: status, preview, duplicate, suggested mapping
POST   /ui/uploads/<id>/process    → validate + save column_map, data_date, crop_type; import on the worker (else an ingest_runner process) (`processing`)
DELETE /ui/uploads/<id>            → discard an upload not yet submitted or processing (409 otherwise; a data-folder file is admin-only, 403)
POST   /ui/process                 → re-process a stored file (Jobs Retry; re-uses the saved mapping)
GET    /ui/progress/<file>         → JSON: one job record from progress_store (or status "unknown")

POST   /api/v1/users/register          → create account, status pending (approved for an ADMIN_EMAILS address)
POST   /api/v1/users/login             → {id}|{secret} token (403 + status when not approved)
GET    /api/v1/users/me                → id, username, email, status, role, is_admin
POST   /api/v1/users/logout            → revoke current token (bearer or token cookie)
GET    /api/v1/users/tokens            → list tokens
POST   /api/v1/users/tokens            → create token
DELETE /api/v1/users/tokens/<id>       → revoke token

GET    /api/v1/admin/users                → accounts (status, q, paged) + counts per status (admin)
POST   /api/v1/admin/users/<id>/approve   → pending/rejected/disabled → approved (admin)
POST   /api/v1/admin/users/<id>/reject    → pending → rejected, tokens revoked (admin)
POST   /api/v1/admin/users/<id>/disable   → approved → disabled, tokens revoked (admin)
POST   /api/v1/admin/users/<id>/role      → {role: user|admin}, approved accounts only (admin)

GET  /api/v1/planting-data             → JSON: filtered data incl. crop_type (public, rate-limited)
GET  /api/v1/planting-data/filters     → JSON: distinct country/province/variety/season_type/crop_type values (per-column paged)
GET  /api/v1/planting-data/coordinates → JSON: lat/lon points for the map (paged)
GET  /api/v1/planting-data/clusters    → JSON: server-side spatial clusters (paged)
GET  /api/v1/planting-data/export      → CSV / JSON stream of one page of results

POST /api/v1/data/upload               → upload a single RDS/Parquet file
GET  /api/v1/quality/stats             → JSON: summary statistics (auth required)
GET  /api/v1/quality/conflicts         → JSON: paginated conflict list, filter country/crop_type/source/search (auth required)
GET  /api/v1/quality/duplicates        → JSON: conflicts grouped by key incl. crop_type, paged by group (auth required)
```

Every Flask `/ui/*` endpoint requires a session (`@require_auth`); they redirect browsers to the SPA login route (`SPA_LOGIN_PATH`, default `/login`) and return `401` JSON to API clients. The React routes are guarded client-side by `AuthContext` (a single provider). A `401` from **any** API call dispatches `kvuno:unauthorized` (`frontend/src/api.ts`); `AuthContext` drops the token and `RequireAuth` sends the user to `/login?next=<page>`, instead of leaving a signed-in shell whose every request fails. Logout always clears local state, even if the server call fails. The token cookie is `Secure` on HTTPS.

### API pagination

Every collection endpoint under `/api/v1/` is paginated server-side. `page`
defaults to `1`, `per_page` to `100`, clamped to `MAX_PER_PAGE` (500). Values are
clamped rather than rejected, and `page` is floored at `1` so a malformed
parameter cannot produce a negative SQL offset. All of it funnels through
`get_pagination()` in `app/dto/pagination.py`, so the default and the ceiling
exist in exactly one place. `PaginationQuery` in the same file puts `page` and
`per_page` in the OpenAPI spec, so Swagger UI can page every collection endpoint;
it only documents them, and the clamping stays in `get_pagination()`.

Responses carry `total` / `pages` / `current_page` / `per_page` next to their
rows. `/planting-data/filters` is the exception: it returns a dict of five
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

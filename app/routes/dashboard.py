"""
The page a browser gets at `/` on the API: name, version, and links to the web
app, the API docs, the OpenAPI spec and the health check.

Self-contained on purpose — inline CSS, no script, no template files — so it
works under the API's CSP (`default-src 'self'`) and needs nothing beyond this
module. It deliberately does not query the database: a hung connection would
hang the page, so it links to /health instead of embedding its result.
"""
from html import escape
from urllib.parse import urlparse

_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{name}</title>
<style>
  :root {{
    --bg: #f6f7f9; --card: #fff; --text: #1d2125; --muted: #5f6b76;
    --border: #e3e6ea; --accent: #0d6efd; --accent-bg: #e7f0ff;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --bg: #15181c; --card: #1e2227; --text: #e6e8eb; --muted: #9aa4ae;
      --border: #2e343b; --accent: #6ea8fe; --accent-bg: #1c2a3d;
    }}
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; min-height: 100vh; background: var(--bg); color: var(--text);
    font: 16px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
    display: flex; align-items: center; justify-content: center; padding: 16px;
  }}
  main {{ width: 100%; max-width: 40rem; }}
  header {{ margin-bottom: 1.5rem; }}
  h1 {{ margin: 0; font-size: 1.6rem; }}
  .meta {{ color: var(--muted); font-size: .9rem; margin-top: .25rem; }}
  .grid {{ display: grid; gap: .75rem; grid-template-columns: repeat(auto-fit, minmax(16rem, 1fr)); }}
  a.card, div.card {{
    display: block; padding: 1rem 1.1rem; border: 1px solid var(--border);
    border-radius: .6rem; background: var(--card); color: inherit; text-decoration: none;
  }}
  a.card:hover, a.card:focus-visible {{ border-color: var(--accent); background: var(--accent-bg); }}
  a.card:focus-visible {{ outline: 2px solid var(--accent); outline-offset: 2px; }}
  .title {{ font-weight: 600; color: var(--accent); }}
  .desc {{ color: var(--muted); font-size: .9rem; margin-top: .2rem; }}
  code {{ font-size: .85em; overflow-wrap: anywhere; }}
  footer {{ margin-top: 1.5rem; color: var(--muted); font-size: .85rem; }}
</style>
</head>
<body>
<main>
  <header>
    <h1>{name}</h1>
    <div class="meta">Version {version} &middot; IITA Internal Use</div>
  </header>
  <div class="grid">
    {frontend_card}
    <a class="card" href="/api-docs/swagger">
      <div class="title">API docs &rarr;</div>
      <div class="desc">Interactive Swagger UI. Use <em>Authorize</em> with a token from the web app&rsquo;s Tokens page.</div>
    </a>
    <a class="card" href="/api-docs/openapi.json">
      <div class="title">OpenAPI spec &rarr;</div>
      <div class="desc">Machine-readable <code>openapi.json</code> for client generators.</div>
    </a>
    <a class="card" href="/health">
      <div class="title">Health &rarr;</div>
      <div class="desc">Service and database status as JSON.</div>
    </a>
  </div>
  <footer>API base path: <code>/api/v1</code></footer>
</main>
</body>
</html>
"""

_FRONTEND_LINK = """<a class="card" href="{url}">
      <div class="title">Web app &rarr;</div>
      <div class="desc">Upload files, track ingestion jobs and explore the data. <code>{url}</code></div>
    </a>"""

_FRONTEND_MISSING = """<div class="card">
      <div class="title">Web app</div>
      <div class="desc">Not configured. Set <code>SPA_ROOT_URL</code> to the web app&rsquo;s address to link it here.</div>
    </div>"""


def _safe_url(url: str) -> str | None:
    """Only absolute http(s) URLs or same-site paths; never `javascript:` and the like."""
    url = (url or '').strip()
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.scheme in ('http', 'https') and parsed.netloc:
        return url
    if not parsed.scheme and not parsed.netloc and url.startswith('/') and not url.startswith('//'):
        return url
    return None


def render_dashboard(name: str, version: str, spa_root: str | None) -> str:
    url = _safe_url(spa_root or '')
    frontend = _FRONTEND_LINK.format(url=escape(url, quote=True)) if url else _FRONTEND_MISSING
    return _PAGE.format(name=escape(name), version=escape(version), frontend_card=frontend)

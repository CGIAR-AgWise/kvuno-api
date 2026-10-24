from flask_cors import CORS
from flask_openapi3 import OpenAPI, Server, Contact, License, Info

from alembic import command
from alembic.config import Config as AlembicConfig

from pathlib import Path
import shutil

from app.models.database_conn import MyDb
from app.routes.main import register_app_routes
from app.config import APP_NAME, Settings, build_db_url, get_settings


def _cleanup_temp_files():
    """Remove what is safely finished. Runs once per worker boot.

    It must never touch anything in flight. It used to `rmtree` the whole
    chunk directory here, and gunicorn boots a worker on every `max_requests`
    recycle and timeout — so uploads in progress lost their chunks at random.
    Uploads are now only removed once idle for UPLOAD_ABANDON_AGE.
    """
    settings = get_settings()
    data_dir = Path(settings.HOUSEKEEPING_DATA_DIR)
    if not data_dir.is_dir():
        return

    import logging
    import time
    log = logging.getLogger(__name__)

    # The retired resumable.js flow's directory. Nothing writes to it anymore.
    legacy_chunks = data_dir / '.chunks'
    if legacy_chunks.is_dir():
        shutil.rmtree(legacy_chunks, ignore_errors=True)

    from app.services import uploads as upload_service
    try:
        removed = upload_service.cleanup_abandoned(Settings.age_seconds(settings.UPLOAD_ABANDON_AGE))
        if removed:
            log.info("Removed %d abandoned upload(s)", removed)
    except Exception as e:
        # Table missing (migrations not yet run) or DB down: try next boot.
        # Roll back so the failed statement does not poison the job cleanup.
        MyDb.get_db().session.rollback()
        log.warning("Upload cleanup skipped: %s", e.__class__.__name__)

    cutoff = time.time() - Settings.age_seconds(settings.CLEANUP_AGE)

    from app.services.progress_store import load_all_jobs, delete_job
    try:
        jobs = load_all_jobs()
    except Exception as e:
        # Redis down and the jobs table missing (migrations not yet run) must
        # not stop the app from booting; there is nothing to clean up yet.
        log.warning("Job cleanup skipped: %s", e.__class__.__name__)
        jobs = []
    for job in jobs:
        if job.get('status') != 'completed':
            continue
        if job.get('mtime', 0) > cutoff:
            continue
        # job['file'] already carries its extension ('<id>.parquet'); the old
        # code appended another, so the data file itself was never removed.
        file_name = job['file']
        try:
            upload = upload_service.find_by_stored_name(file_name)
            if upload is not None:
                upload_service.discard(upload)
                log.info("Removed processed upload: %s", file_name)
        except Exception:
            log.warning("Could not remove upload for %s", file_name, exc_info=True)
        for target in [data_dir / file_name] + [
            data_dir / f"{file_name}{suffix}" for suffix in ('.meta.json', '.map.json', '.preview.json')
        ]:
            try:
                if target.is_file():
                    target.unlink()
                    log.info("Removed processed file: %s", target.name)
            except OSError:
                pass
        delete_job(file_name)


# API contact information
contact = Contact(
    name="Munywele Sammy",
    email="sammy@munywele.co.ke",
    url="https://munywele.co.ke"
)

# API license information
api_license = License(
    name="Apache 2.0",
    identifier="Apache-2.0"
)

def _info(settings: Settings) -> Info:
    return Info(
        title=APP_NAME,
        version=settings.app_version,
        contact=contact,
        license=api_license,
        termsOfService="https://agwise.org/terms-of-service"
    )


def _servers(settings: Settings) -> list[Server]:
    return [
        Server(url="http://127.0.0.1:5000"),
        Server(url=settings.SERVER_URL_PROD),
        Server(url=settings.SERVER_URL_PROD_2),
    ]


def init_db(app):
    """Initialize the database with the Flask app."""
    MyDb.init_app(app)


def _db_available() -> bool:
    """Check if the database host:port is reachable (non-blocking)."""
    from urllib.parse import urlparse
    import socket
    url = build_db_url()
    if url.startswith('sqlite'):
        return True
    parts = urlparse(url)
    host = parts.hostname or '127.0.0.1'
    port = parts.port or 5432
    try:
        s = socket.create_connection((host, port), timeout=2)
        s.close()
        return True
    except (OSError, ValueError):
        return False


def _sanitize_db_url(url: str) -> str:
    """Strip credentials from a database URL for safe logging."""
    from urllib.parse import urlparse, urlunparse
    parsed = urlparse(url)
    if parsed.password:
        netloc = f"{parsed.username or ''}:****@{parsed.hostname or ''}"
        if parsed.port:
            netloc = f"{netloc}:{parsed.port}"
        cleaned = parsed._replace(netloc=netloc)
        return str(urlunparse(cleaned))
    return url


def run_migrations():
    """Apply pending Alembic migrations.

    Called by docker/entrypoint.sh before the app starts (and by
    scripts/run_migrations.py). A no-op when the database is unreachable, so a
    database that is still booting will not block the app from starting.
    """
    if not _db_available():
        from app.utils.logging import get_logger
        _log = get_logger(__name__)
        _log.warning(
            f"Database at {_sanitize_db_url(build_db_url())} is not reachable — skipping migrations. "
            f"The app will start, but requests will fail until the database is available. "
            f"Set RUN_MIGRATIONS=false in the container environment to skip migrations entirely."
        )
        return
    alembic_cfg = AlembicConfig("alembic.ini")
    alembic_cfg.set_main_option("sqlalchemy.url", build_db_url())
    command.upgrade(alembic_cfg, "head")


def register_apis(app: OpenAPI):
    """Register all API Blueprints with the Flask app."""
    from app.api.user import api as user_api
    from app.api.planting_data import public_api as pd_public_api
    from app.api.planting_data import protected_api as pd_protected_api
    from app.api.upload import api as upload_api
    from app.api.quality import api as quality_api
    from app.api.admin import api as admin_api

    app.register_api(user_api)
    app.register_api(pd_public_api)
    app.register_api(pd_protected_api)
    app.register_api(upload_api)
    app.register_api(quality_api)
    app.register_api(admin_api)


def create_app():
    """Create and configure the Flask app."""
    # First, so everything below — Flask, SQLAlchemy, our own modules — logs
    # through the one configured handler (app/utils/logging.py).
    from app.utils.logging import setup_logging
    setup_logging()
    # Then the configuration: an invalid value stops here, every problem
    # listed, instead of surfacing later as an odd failure.
    settings = get_settings()

    app = OpenAPI(
        __name__,
        servers=_servers(settings),
        info=_info(settings),
        doc_prefix="/api-docs",
        security_schemes={
            "jwt": {
                "type": "http",
                "scheme": "bearer",
                "bearerFormat": "OpaqueToken",
                "description": "Access token returned by POST /api/v1/users/login in the format {id}|{secret}. "
                               "It is an opaque Sanctum-style token (SHA-256 hashed at rest), not a signed JWT. "
                               "Include as: Authorization: Bearer {token}"
            },
        }
    )

    # Swagger UI config — hide the Schemas section
    # app.config['SWAGGER_CONFIG'] = {"defaultModelsExpandDepth": -1}
    # app.config['OPENAPI_HTML_STRING'] = '<!DOCTYPE html><script>window.location.href="swagger"</script>'

    # Enable Cross-Origin Resource Sharing (CORS)
    # In production nginx proxies /api and /ui to this app, so the browser sees
    # one origin and CORS is unused. These defaults only matter when the SPA
    # runs on its own dev server (`vite dev`, port 5173).
    origins = settings.cors_origins
    # The tus upload protocol is driven by response headers, which a
    # cross-origin client cannot read unless they are exposed.
    CORS(app, origins=origins, supports_credentials=True,
         expose_headers=['Location', 'Upload-Offset', 'Upload-Length', 'Tus-Resumable', 'X-Request-ID'])

    # Rate limiting
    from app.rate_limit import configure_limiter, limiter
    configure_limiter(app)
    # Static assets must never be rate limited: browsers request several per
    # page view, so counting them against the default limit makes a handful of
    # page loads look like abuse.
    limiter.exempt(app.send_static_file)

    @app.errorhandler(429)
    def ratelimit_handler(e):
        return {"error": "Rate limit exceeded. Please slow down."}, 429

    @app.errorhandler(400)
    def bad_request(e):
        return {"error": "Bad request"}, 400

    @app.errorhandler(403)
    def forbidden(e):
        return {"error": "Forbidden"}, 403

    @app.errorhandler(404)
    def not_found(e):
        return {"error": "Not found"}, 404

    @app.errorhandler(500)
    def server_error(e):
        # The request_id lets a user's report be matched to the logged traceback.
        from flask import g
        return {"error": "Internal server error", "request_id": getattr(g, 'request_id', None)}, 500

    # Security headers for all responses
    # ── Request context for logs ──────────────────────────────
    # Every line logged while handling a request carries its request_id,
    # method and path (structlog contextvars). The id comes from X-Request-ID
    # when a proxy set one — kvuno-web's nginx does — so one id follows a
    # request from nginx through the API; otherwise one is generated. It is
    # returned in the response header, so a user or the SPA can quote it.
    import re
    import uuid

    import structlog
    from flask import g, request

    _request_id_ok = re.compile(r'^[A-Za-z0-9._-]{1,64}$')

    @app.before_request
    def bind_request_context():
        incoming = request.headers.get('X-Request-ID', '')
        # Only accept a sane id from outside: it lands in every log line.
        g.request_id = incoming if _request_id_ok.match(incoming) else uuid.uuid4().hex
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=g.request_id, method=request.method,
                                               path=request.path)

    @app.after_request
    def return_request_id(response):
        request_id = getattr(g, 'request_id', None)
        if request_id:
            response.headers['X-Request-ID'] = request_id
        return response

    @app.teardown_request
    def clear_request_context(_exc=None):
        structlog.contextvars.clear_contextvars()

    @app.after_request
    def add_security_headers(response):
        response.headers.setdefault('X-Content-Type-Options', 'nosniff')
        response.headers.setdefault('X-Frame-Options', 'DENY')
        response.headers.setdefault('X-XSS-Protection', '0')
        response.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
        response.headers.setdefault('Permissions-Policy', '')
        if response.content_type and 'text/html' in response.content_type:
            response.headers.setdefault(
                'Content-Security-Policy',
                # No CDN hosts: the SPA bundles Bootstrap, Leaflet and the
                # rest from npm, and nothing loads from a CDN at runtime, so
                # the old cdn.jsdelivr.net / unpkg.com allowances were only
                # widening the policy. The Swagger UI at /api-docs serves its
                # assets locally from flask-openapi3-swagger.
                "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                "style-src 'self' 'unsafe-inline'; "
                # Tiles are served from a./b./c. subdomains
                # (tileLayer('https://{s}.tile.openstreetmap.org/...')). A CSP host
                # without a wildcard matches only that exact host, so the
                # subdomain form is required — keep the bare host too, since
                # '*.host' does not match 'host' itself.
                "img-src 'self' data: https://tile.openstreetmap.org https://*.tile.openstreetmap.org; "
                "font-src 'self'; "
                "connect-src 'self' https://tile.openstreetmap.org https://*.tile.openstreetmap.org; "
                "frame-ancestors 'none';"
            )
        return response

    # Configure the database URI
    app.config['SQLALCHEMY_DATABASE_URI'] = build_db_url()
    # DEBUG_DB is handled by app/utils/logging.py (through the shared handler);
    # echo would add SQLAlchemy's own stdout handler and bypass LOG_LEVEL.
    app.config['SQLALCHEMY_ECHO'] = False
    # Dates as ISO 8601 UTC ("2026-10-22T07:59:49Z"), not Flask's default HTTP
    # date ("Thu, 22 Oct 2026 07:59:49 GMT"), so clients can parse and sort them.
    from app.utils.json_encoding import IsoJSONProvider
    app.json = IsoJSONProvider(app)
    app.json.sort_keys = settings.SORT_JSON

    # Initialize the database
    init_db(app)

    # Register APIs and other routes
    register_apis(app)
    register_app_routes(app)

    # Clean up temporary files from previous runs
    with app.app_context():
        _cleanup_temp_files()

    # Enqueue background processing of any unprocessed files via Celery
    if settings.HOUSEKEEPING_ENABLED:
        from app.services.housekeeper import process_pending
        process_pending()

    # Behind a reverse proxy (Dokploy, nginx) request.remote_addr is the
    # proxy's address, so every client shares one rate-limit bucket and the
    # first few requests exhaust everyone's budget. ProxyFix makes Werkzeug
    # read the real client IP from the forwarded headers.
    #
    # PROXY_FIX_HOPS counts trusted proxies in front of the app: 1 for the
    # single nginx that serves the SPA, 2 if there is also a CDN in front.
    #
    # This defaults to 1 because the deployment now has that nginx by default.
    # Without it, get_remote_address() returns the proxy's own IP, so every
    # user shares one rate-limit bucket — which is a denial of service on our
    # own limiter, not just inaccurate logging.
    #
    # ⚠️ The tradeoff: ProxyFix trusts the forwarded headers unconditionally, so
    # if this app is ever exposed WITHOUT a proxy in front, a client can send
    # its own X-Forwarded-For and evade every rate limit. Set PROXY_FIX_HOPS=0
    # in that case — running `python run.py` straight onto a public port is
    # the scenario to watch for.
    hops = settings.PROXY_FIX_HOPS
    if hops > 0:
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=hops, x_proto=hops, x_host=hops)
        from app.utils.logging import get_logger
        get_logger(__name__).info(f"ProxyFix enabled for {hops} proxy hop(s)")

    return app

import os

from dotenv import load_dotenv
from flask_cors import CORS
from flask_openapi3 import OpenAPI, Server, Contact, License, Info

from alembic import command
from alembic.config import Config as AlembicConfig

from pathlib import Path
import shutil

from app.models.database_conn import MyDb
from app.routes.main import register_app_routes
from app.config import build_db_url, APP_NAME, APP_VERSION, HOUSEKEEPING_DATA_DIR, HOUSEKEEPING_ENABLED

# Load environment variables from .env file
load_dotenv()


def _cleanup_temp_files():
    data_dir = Path(HOUSEKEEPING_DATA_DIR)
    if not data_dir.is_dir():
        return

    import logging
    import time
    log = logging.getLogger(__name__)

    chunks_dir = data_dir / '.chunks'
    if chunks_dir.is_dir():
        shutil.rmtree(chunks_dir)
        log.info("Cleaned up chunks directory")

    raw = os.getenv('CLEANUP_AGE', '1d')
    unit = raw[-1]
    value = int(raw[:-1])
    multipliers = {'m': 60, 'h': 3600, 'd': 86400, 'w': 604800}
    cutoff = time.time() - value * multipliers.get(unit, 86400)

    from app.services.progress_store import load_all_jobs, delete_job
    for job in load_all_jobs():
        if job.get('status') != 'completed':
            continue
        if job.get('mtime', 0) > cutoff:
            continue
        stem = job['file']
        for suffix in ('.rds', '.parquet', '.meta.json', '.map.json', '.preview.json'):
            target = data_dir / f"{stem}{suffix}"
            try:
                if target.is_file():
                    target.unlink()
                    log.info("Removed processed file: %s", target.name)
            except OSError:
                pass
        delete_job(stem)


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

# API information
info = Info(
    title=APP_NAME,
    version=APP_VERSION,
    contact=contact,
    license=api_license,
    termsOfService="https://agwise.org/terms-of-service"
)

# API servers
servers = [
    Server(url="http://127.0.0.1:5000"),
    Server(url=os.getenv("SERVER_URL_PROD", "https://kvuno.agwise.org")),
    Server(url=os.getenv("SERVER_URL_PROD_2", "https://kvuno.akilimo.org")),
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
        from app.utils.logging import SharedLogger
        _log = SharedLogger().get_logger()
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

    app.register_api(user_api)
    app.register_api(pd_public_api)
    app.register_api(pd_protected_api)
    app.register_api(upload_api)
    app.register_api(quality_api)


def create_app():
    """Create and configure the Flask app."""
    app = OpenAPI(
        __name__,
        servers=servers,
        info=info,
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
    cors_origins = os.getenv('CORS_ORIGINS', 'http://127.0.0.1:5000')
    origins = [o.strip() for o in cors_origins.split(',') if o.strip()]
    CORS(app, origins=origins, supports_credentials=True)

    # Rate limiting
    from app.rate_limit import limiter
    limiter.init_app(app)
    # Static assets must never be rate limited: browsers request several per
    # page view, so counting them against the default limit makes a handful of
    # page loads look like abuse. `storage_uri` is already applied by the
    # Limiter constructor, so RATE_LIMIT_STORAGE needs nothing here.
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
        return {"error": "Internal server error"}, 500

    # Security headers for all responses
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
                "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
                "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://unpkg.com; "
                "img-src 'self' data: https://tile.openstreetmap.org; "
                "font-src 'self' https://cdn.jsdelivr.net; "
                "connect-src 'self' https://tile.openstreetmap.org https://cdn.jsdelivr.net; "
                "frame-ancestors 'none';"
            )
        return response

    # Configure the database URI
    app.config['SQLALCHEMY_DATABASE_URI'] = build_db_url()
    app.config['SQLALCHEMY_ECHO'] = os.getenv('DEBUG_DB', 'false').lower() == 'true'
    app.json.sort_keys = os.getenv('SORT_JSON') == '1'

    # Initialize the database
    init_db(app)

    @app.template_filter('datetime')
    def datetime_filter(ts):
        from datetime import datetime
        return datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M')

    # Register APIs and other routes
    register_apis(app)
    register_app_routes(app)

    # Clean up temporary files from previous runs
    with app.app_context():
        _cleanup_temp_files()

    # Enqueue background processing of any unprocessed files via Celery
    if HOUSEKEEPING_ENABLED:
        from app.services.housekeeper import process_pending
        process_pending()

    # Behind a reverse proxy (Dokploy, nginx) request.remote_addr is the
    # proxy's address, so every client shares one rate-limit bucket and the
    # first few requests exhaust everyone's budget. ProxyFix makes Werkzeug
    # read the real client IP from the forwarded headers.
    #
    # Only enable this when a trusted proxy sets those headers — otherwise a
    # client can spoof X-Forwarded-For and bypass the limiter entirely.
    # PROXY_FIX_HOPS counts proxies in front of the app: 1 for a single nginx.
    if os.getenv('PROXY_FIX_HOPS', '0').isdigit() and int(os.getenv('PROXY_FIX_HOPS', '0')) > 0:
        from werkzeug.middleware.proxy_fix import ProxyFix
        hops = int(os.getenv('PROXY_FIX_HOPS', '0'))
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=hops, x_proto=hops, x_host=hops)
        from app.utils.logging import SharedLogger
        SharedLogger().get_logger().info(f"ProxyFix enabled for {hops} proxy hop(s)")

    return app

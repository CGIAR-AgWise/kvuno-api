import json
import os
import time

from flask import abort, redirect, jsonify, request, Response, stream_with_context

from pathlib import Path

from app.auth import require_auth
from app.config import HOUSEKEEPING_DATA_DIR, MAX_FILE_SIZE
from app.models.database_conn import MyDb
from app.routes.uploads import enqueue_upload, register_upload_routes
from app.services import uploads as upload_service
from app.services.uploads import COLUMN_ALIASES, DB_TARGET_COLUMNS, UploadError
from app.utils.logging import get_logger

logger = get_logger(__name__)

DATA_DIR = HOUSEKEEPING_DATA_DIR
ALLOWED_EXTENSIONS = {'.rds', '.parquet'}


def _load_jobs():
    from app.services.progress_store import load_all_jobs
    return load_all_jobs()


def _format_jobs(raw):
    counts = {'completed': 0, 'processing': 0, 'error': 0, 'stale': 0, 'unknown': 0}
    for j in raw:
        st = j.get('status', 'unknown')
        counts[st] = counts.get(st, 0) + 1
    return raw, counts


def _in_data_dir(file_name: str) -> Path | None:
    """Resolve `file_name` inside the data directory, or None if it escapes it.

    `is_relative_to`, not a string prefix: '/data2/x' starts with '/data'.
    """
    data_dir = Path(DATA_DIR).resolve()
    file_path = (data_dir / file_name).resolve()
    return file_path if file_path.is_relative_to(data_dir) else None


def register_app_routes(app):
    """
    Register routes for the Flask application.

    Args:
        app (Flask): The Flask application instance.
    """

    @app.route('/')
    def index():
        """
        A landing page for browsers; a pointer for everything else.

        A browser (Accept: text/html) gets a small dashboard linking the web
        app (SPA_ROOT_URL), the API docs, the OpenAPI spec and /health. The UI
        itself is a separate React app served by its own nginx container.

        API clients keep the old behaviour: a redirect to SPA_ROOT_URL when it
        is set, otherwise a JSON pointer. SPA_ROOT_URL must never default to
        '/': that pointed the redirect back at *this* service, an infinite
        loop reported as ERR_TOO_MANY_REDIRECTS.
        """
        spa_root = os.getenv('SPA_ROOT_URL', '').strip()
        # Explicit text/html only: curl, fetch and test clients send */*, and
        # must keep getting JSON or the redirect.
        if 'text/html' in request.headers.get('Accept', ''):
            from app.config import APP_NAME, APP_VERSION
            from app.routes.dashboard import render_dashboard
            return Response(render_dashboard(APP_NAME, APP_VERSION, spa_root),
                            mimetype='text/html')
        if not spa_root:
            return jsonify({
                'service': 'kvuno-api',
                'message': 'This service does not serve the web UI. '
                           'Set SPA_ROOT_URL to the SPA origin, or open the web container.',
                'api_docs': '/api-docs',
                'health': '/health',
            }), 200
        return redirect(spa_root, code=302)

    @app.route('/health', methods=['GET'])
    def health_check():
        db_connected = MyDb.check_db_connection()

        from app.config import APP_NAME, APP_VERSION

        health_status = {
            "status": "healthy" if db_connected else "unhealthy",
            "name": APP_NAME,
            "version": APP_VERSION,
            "database": "UP" if db_connected else "DOWN",
        }
        return jsonify(health_status), 200 if db_connected else 500

    @app.route('/ui/columns', methods=['GET'])
    @require_auth
    def ui_columns():
        # max_file_size lets the SPA refuse an oversized file before sending a
        # byte, from the server's own limit rather than a hardcoded copy of it.
        return jsonify(columns=sorted(DB_TARGET_COLUMNS), aliases=COLUMN_ALIASES,
                       max_file_size=MAX_FILE_SIZE)

    register_upload_routes(app)

    @app.route('/ui/process', methods=['POST'])
    @require_auth
    def upload_ui_process():
        """(Re)process a stored file — the Jobs page's Retry.

        Omitting `column_map` re-uses the mapping saved with the upload. Retry
        used to send `{}`, which overwrote the user's mapping with nothing and
        re-ingested the file with the defaults.
        """
        body = request.get_json(silent=True) or {}
        file_name = body.get('file')
        column_map = body.get('column_map') or None

        if not file_name or not isinstance(file_name, str):
            return jsonify(error="No file specified"), 400

        upload = upload_service.find_by_stored_name(file_name)
        if upload is not None:
            try:
                upload_service.submit(upload, column_map)
            except UploadError as e:
                return jsonify(error=str(e)), e.status
            return enqueue_upload(upload)

        # A file placed in the data directory by hand or by the API upload,
        # which has no upload row and keeps its mapping in a sidecar.
        ext = os.path.splitext(file_name)[1].lower()
        if ext not in ALLOWED_EXTENSIONS:
            return jsonify(error=f"Unsupported extension '{ext}'. Allowed: {sorted(ALLOWED_EXTENSIONS)}"), 400

        file_path = _in_data_dir(file_name)
        if file_path is None:
            abort(404)
        if not file_path.is_file():
            return jsonify(error=f"File not found: {file_name}"), 404

        if column_map is not None:
            if not isinstance(column_map, dict):
                return jsonify(error="column_map must be an object"), 400
            invalid = [v for v in column_map.values() if v not in DB_TARGET_COLUMNS]
            if invalid:
                return jsonify(error=f"Invalid target columns: {invalid}"), 400
            with open(str(file_path) + '.map.json', 'w') as f:
                json.dump(column_map, f)

        from app.config import HOUSEKEEPING_ENABLED
        if HOUSEKEEPING_ENABLED:
            from app.services.housekeeper import process_file_async
            if process_file_async(file_path=file_path):
                return jsonify(id=file_name, message="Processing started")
            return jsonify(error="The job queue is unreachable. Try again shortly."), 503

        return jsonify(id=file_name, message="File saved with mapping. Set HOUSEKEEPING_ENABLED=true and start a Celery worker.")

    @app.route('/ui/progress/<file_name>', methods=['GET'])
    @require_auth
    def ui_progress(file_name):
        """One job, in the same shape as an item of /ui/jobs/data.

        This used to read a `.progress.json` sidecar that nothing has written
        since progress moved to progress_store, so the Jobs detail modal (and
        its Retry) always saw 'unknown'. The lookup is now Redis/Postgres, not
        the filesystem, but a name with a path separator is still refused: job
        keys are basenames, so such a name can only be a probe.
        """
        if '/' in file_name or '\\' in file_name or file_name in ('.', '..'):
            abort(404)
        from app.services.progress_store import get_job
        job = get_job(file_name)
        if job is None:
            return jsonify(status='unknown', current=0, total=0, message='')
        return jsonify(job)

    @app.route('/ui/jobs/data', methods=['GET'])
    @require_auth
    def ui_jobs_data():
        jobs = _load_jobs()
        return jsonify(jobs=jobs)

    @app.route('/ui/jobs/runs', methods=['GET'])
    @require_auth
    def ui_jobs_runs():
        """Recent ingestion runs with aggregate progress, newest first.

        Progress is tracked per file, so without this a multi-file upload
        reported N unrelated jobs and there was no way to see how the batch as
        a whole was doing.
        """
        from app.services.progress_store import list_runs
        try:
            limit = int(request.args.get('limit', default=20))
        except (TypeError, ValueError):
            limit = 20
        try:
            return jsonify(runs=list_runs(limit=max(1, min(limit, 100))))
        except Exception:
            # Progress reporting is observability, not the product. If the
            # table is missing (migrations not yet run) or the database is
            # briefly unavailable, report no runs rather than returning 500 and
            # making the jobs page look broken.
            logger.warning("Unable to load processing runs", exc_info=True)
            return jsonify(runs=[])

    # ── Auth UI ────────────────────────────────────────────────

    @app.route('/ui/jobs/events', methods=['GET'])
    @require_auth
    def ui_jobs_events():
        # A comment line, ignored by EventSource but seen by reverse proxies.
        # Proxies commonly drop an idle connection, and a job list can be silent
        # for minutes, so this is what keeps the stream alive.
        KEEPALIVE = ': keepalive\n\n'
        POLL_SECONDS = 15

        def snapshot():
            raw, counts = _format_jobs(_load_jobs())
            return f"data: {json.dumps({'jobs': raw, 'counts': counts})}\n\n"

        def generate():
            # Full snapshot on connect, then one message per changed job.
            # The pubsub payload IS the job record, so a client no longer
            # re-reads the whole keyspace on every progress write.
            from app.services.progress_store import get_job
            yield snapshot()

            pubsub = None
            r = None
            try:
                from redis import Redis
                # No socket_timeout here. A subscription is supposed to block
                # indefinitely; a 2s read timeout made the connection raise
                # TimeoutError whenever a job list was simply quiet. The wait is
                # bounded by get_message() instead.
                r = Redis.from_url(os.getenv('CELERY_BROKER_URL', 'redis://localhost:6379/0'),
                                   socket_connect_timeout=2, socket_timeout=None,
                                   health_check_interval=30)
                r.ping()
                pubsub = r.pubsub(ignore_subscribe_messages=True)
                pubsub.subscribe('jobs:updates')
            except Exception:
                pubsub = None

            def polling():
                # Used when pubsub is unavailable or the connection drops. The
                # job list is small, so polling is acceptable as a fallback.
                while True:
                    time.sleep(3)
                    yield snapshot()

            if pubsub is not None:
                last_sent = time.time()
                try:
                    while True:
                        # get_message(timeout=...) returns None on idle rather
                        # than blocking the socket, so keepalives and error
                        # handling stay under our control.
                        message = pubsub.get_message(timeout=1.0)
                        if message is not None and message.get('type') == 'message':
                            try:
                                changed = json.loads(message['data'])
                            except (ValueError, TypeError):
                                changed = None
                            if changed and 'file' in changed:
                                # Prefer the stored record: the published message
                                # may have been in flight when a job crossed the
                                # staleness threshold.
                                job = get_job(changed['file']) or changed
                                yield f"data: {json.dumps({'job': job})}\n\n"
                                last_sent = time.time()
                        elif time.time() - last_sent >= POLL_SECONDS:
                            yield KEEPALIVE
                            last_sent = time.time()
                except GeneratorExit:
                    raise
                except Exception:
                    # A dropped or erroring subscription must not 500 the
                    # response; degrade to polling instead.
                    yield snapshot()
                    try:
                        yield from polling()
                    except GeneratorExit:
                        raise
                finally:
                    try:
                        pubsub.unsubscribe()
                        pubsub.close()
                    except Exception:
                        pass
                    if r is not None:
                        try:
                            r.close()
                        except Exception:
                            pass
            else:
                try:
                    yield from polling()
                except GeneratorExit:
                    raise

        return Response(stream_with_context(generate()), mimetype='text/event-stream')

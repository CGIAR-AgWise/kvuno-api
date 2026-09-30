import json
import os
import shutil
import time
import uuid

from flask import abort, redirect, jsonify, request, Response, stream_with_context

from pathlib import Path

from app.auth import require_auth
from app.config import HOUSEKEEPING_DATA_DIR, HOUSEKEEPING_ENABLED, MAX_FILE_SIZE
from app.models.database_conn import MyDb
from app.models.kvuno import PlantingRecommendation
from app.utils.preview import FileTooLarge, build_preview, iter_chunks
from app.utils.rds_to_parquet import convert_for_ingestion

DATA_DIR = HOUSEKEEPING_DATA_DIR
ALLOWED_EXTENSIONS = {'.rds', '.parquet'}

_EXCLUDED = {'id', 'check_sum', 'coordinates', 'created_at', 'updated_at'}
DB_TARGET_COLUMNS = {
    col.name for col in PlantingRecommendation.__table__.columns
    if col.name not in _EXCLUDED
}

COLUMN_ALIASES = {
    'cultivar': 'variety',
    'season': 'season_type',
    'sowing_date': 'opt_date',
    'planting_date': 'opt_date',
    'plantingdate': 'opt_date',
    'date': 'opt_date',
    'longitude': 'lon',
    'long': 'lon',
    'latitude': 'lat',
    'lat_y': 'lat',
    'lon_x': 'lon',
}

CHUNK_DIR = os.path.join(DATA_DIR, '.chunks')


def _chunk_path(identifier: str, number: int):
    return os.path.join(CHUNK_DIR, identifier, str(number))


def _load_jobs():
    from app.services.progress_store import load_all_jobs
    return load_all_jobs()


def _format_jobs(raw):
    counts = {'completed': 0, 'processing': 0, 'error': 0, 'stale': 0, 'unknown': 0}
    for j in raw:
        st = j.get('status', 'unknown')
        counts[st] = counts.get(st, 0) + 1
    return raw, counts


def register_app_routes(app):
    """
    Register routes for the Flask application.

    Args:
        app (Flask): The Flask application instance.
    """

    @app.route('/')
    def index():
        """
        Point browsers at the SPA.

        The UI is a separate React app served by its own nginx container; this
        service renders no HTML. Redirecting to '/' by default would point the
        browser back at *this* service, which redirects again — an infinite
        loop, reported as ERR_TOO_MANY_REDIRECTS or ERR_SOCKET_NOT_CONNECTED.

        So SPA_ROOT_URL must name the SPA's actual origin. When it is unset,
        return a small JSON pointer instead of a redirect.
        """
        spa_root = os.getenv('SPA_ROOT_URL', '').strip()
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
        return jsonify(columns=sorted(DB_TARGET_COLUMNS), aliases=COLUMN_ALIASES)

    # ── Upload UI ──────────────────────────────────────────────


    @app.route('/ui/upload/resumable', methods=['GET'])
    @require_auth
    def upload_resumable_test():
        """Resumable.js test — return 200 if chunk exists, 204 otherwise."""
        try:
            identifier = request.args['resumableIdentifier']
            number = int(request.args['resumableChunkNumber'])
            path = _chunk_path(identifier, number)
            if os.path.isfile(path) and os.path.getsize(path) > 0:
                return '', 200
        except (KeyError, ValueError, TypeError):
            pass
        return '', 204

    @app.route('/ui/upload/resumable', methods=['POST'])
    @require_auth
    def upload_resumable_chunk():
        """Receive a single chunk from resumable.js."""
        try:
            identifier = request.form['resumableIdentifier']
            number = int(request.form['resumableChunkNumber'])
        except (KeyError, ValueError):
            return jsonify(error="Missing chunk metadata"), 400

        chunk_file = request.files.get('file')
        if not chunk_file:
            return jsonify(error="No chunk data"), 400

        chunk_dir = os.path.join(CHUNK_DIR, identifier)
        os.makedirs(chunk_dir, exist_ok=True)
        chunk_file.save(_chunk_path(identifier, number))
        return jsonify(success=True)

    @app.route('/ui/upload/complete', methods=['POST'])
    @require_auth
    def upload_resumable_complete():
        """Merge chunks into final file and return column preview."""
        body = request.get_json(silent=True) or {}
        identifier = body.get('identifier')
        total_chunks = body.get('totalChunks')
        filename = body.get('filename', 'upload')

        if not identifier or not total_chunks:
            return jsonify(error="Missing identifier or totalChunks"), 400

        ext = os.path.splitext(filename)[1].lower()
        if ext not in ALLOWED_EXTENSIONS:
            return jsonify(error=f"Unsupported extension {ext}"), 400

        unique_name = f"{uuid.uuid4().hex}{ext}"
        dest = os.path.join(DATA_DIR, unique_name)

        # Bound the merge as we go. Without this the endpoint accepted chunks of
        # any size — the UI cap is client-side only — so one request could fill
        # the disk and then block on a full-file RDS parse below.
        try:
            with open(dest, 'wb') as out:
                for block in iter_chunks(identifier, _chunk_path, total_chunks, MAX_FILE_SIZE):
                    out.write(block)
        except FileTooLarge as e:
            shutil.rmtree(os.path.join(CHUNK_DIR, identifier), ignore_errors=True)
            if os.path.exists(dest):
                os.remove(dest)
            return jsonify(error=str(e)), 413
        except FileNotFoundError:
            if os.path.exists(dest):
                os.remove(dest)
            return jsonify(error="Missing chunk — upload may have failed"), 400

        # clean up chunk dir
        shutil.rmtree(os.path.join(CHUNK_DIR, identifier), ignore_errors=True)

        # Convert RDS -> Parquet once, here. An RDS is re-parsed in full by both
        # the preview below and again by the worker; a Parquet answers both from
        # the footer plus one batch. Falls back to the RDS if conversion fails.
        stored = convert_for_ingestion(dest)
        ext = os.path.splitext(stored)[1].lower()

        meta = {"original_name": filename}
        with open(stored + '.meta.json', 'w') as f:
            json.dump(meta, f)

        try:
            preview = build_preview(stored, ext)
        except Exception as e:
            for p in (stored, stored + '.meta.json'):
                if os.path.exists(p):
                    os.remove(p)
            return jsonify(error=f"Failed to read file: {e}"), 400

        return jsonify(file=os.path.basename(stored), **preview)

    @app.route('/ui/process', methods=['POST'])
    @require_auth
    def upload_ui_process():
        body = request.get_json(silent=True) or {}
        file_name = body.get('file')
        column_map = body.get('column_map', {})

        if not file_name:
            return jsonify(error="No file specified"), 400

        ext = os.path.splitext(file_name)[1].lower()
        if ext not in ALLOWED_EXTENSIONS:
            return jsonify(error=f"Unsupported extension '{ext}'. Allowed: {sorted(ALLOWED_EXTENSIONS)}"), 400

        data_dir = Path(DATA_DIR).resolve()
        file_path = (data_dir / file_name).resolve()
        if not str(file_path).startswith(str(data_dir)):
            abort(404)
        if not file_path.is_file():
            return jsonify(error=f"File not found: {file_name}"), 404

        invalid = [v for v in column_map.values() if v not in DB_TARGET_COLUMNS]
        if invalid:
            return jsonify(error=f"Invalid target columns: {invalid}"), 400

        mapping_path = str(file_path) + '.map.json'
        with open(mapping_path, 'w') as f:
            json.dump(column_map, f)

        if HOUSEKEEPING_ENABLED:
            from app.services.housekeeper import process_file_async
            process_file_async(file_path=file_path)
            return jsonify(id=file_name, message="Processing started")

        return jsonify(id=file_name, message="File saved with mapping. Set HOUSEKEEPING_ENABLED=true and start a Celery worker.")

    @app.route('/ui/progress/<file_name>', methods=['GET'])
    @require_auth
    def ui_progress(file_name):
        data_dir = Path(DATA_DIR).resolve()
        file_path = (data_dir / file_name).resolve()
        if not str(file_path).startswith(str(data_dir)):
            abort(404)
        progress_path = file_path.with_name(file_path.name + '.progress.json')
        if not progress_path.is_file():
            return jsonify(status='unknown', current=0, total=0, message='')
        try:
            with open(progress_path) as f:
                return jsonify(**json.load(f))
        except (json.JSONDecodeError, OSError) as e:
            return jsonify(status='error', current=0, total=0, message=str(e))

    @app.route('/ui/jobs/data', methods=['GET'])
    @require_auth
    def ui_jobs_data():
        jobs = _load_jobs()
        return jsonify(jobs=jobs)

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

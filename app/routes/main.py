import json
import os
import queue
import threading
import uuid

import pandas as pd
import pyreadr
from flask import abort, redirect, jsonify, render_template, request, Response, stream_with_context
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from pathlib import Path

from app.auth import require_auth
from app.config import HOUSEKEEPING_DATA_DIR, HOUSEKEEPING_ENABLED
from app.models.database_conn import MyDb
from app.models.kvuno import PlantingRecommendation

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


# ── File watcher (SSE push on changes) ─────────────────────────

class _ProgressHandler(FileSystemEventHandler):
    def on_created(self, event):
        if event.src_path.endswith('.progress.json'):
            _notify_sse_clients()
    def on_modified(self, event):
        if event.src_path.endswith('.progress.json'):
            _notify_sse_clients()


_sse_queues: list[queue.Queue] = []
_sse_lock = threading.Lock()
_MAX_SSE_CLIENTS = 50


def _notify_sse_clients():
    with _sse_lock:
        dead = []
        for q in _sse_queues:
            try:
                q.put_nowait(True)
            except queue.Full:
                dead.append(q)
        for q in dead:
            _sse_queues.remove(q)


def _start_watcher():
    data_dir = Path(DATA_DIR)
    data_dir.mkdir(parents=True, exist_ok=True)
    observer = Observer()
    observer.schedule(_ProgressHandler(), str(data_dir), recursive=False)
    observer.daemon = True
    observer.start()


_start_watcher()


def _read_columns(path: str, ext: str):
    if ext == '.parquet':
        return pd.read_parquet(path, nrows=0).columns.tolist()
    data = pyreadr.read_r(path)
    return data[None].columns.tolist()


def _read_rows(path: str, ext: str, n: int = 5):
    if ext == '.parquet':
        df = pd.read_parquet(path, nrows=n)
    else:
        data = pyreadr.read_r(path)
        df = data[None].head(n)
    return json.loads(df.to_json(orient='records'))


def _chunk_path(identifier: str, number: int):
    return os.path.join(CHUNK_DIR, identifier, str(number))


def _load_jobs():
    jobs = []
    data_dir = Path(DATA_DIR)
    for p in data_dir.glob('*.progress.json'):
        try:
            with open(p) as f:
                job = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        stem = p.stem.replace('.progress', '')
        mtime = os.path.getmtime(p)
        job['file'] = stem
        job['mtime'] = mtime

        meta_path = data_dir / f"{stem}.meta.json"
        if meta_path.is_file():
            try:
                with open(meta_path) as f:
                    meta = json.load(f)
                job['original_name'] = meta.get('original_name', stem)
            except (json.JSONDecodeError, OSError):
                job['original_name'] = stem
        else:
            job['original_name'] = stem

        jobs.append(job)
    jobs.sort(key=lambda j: j.get('mtime', 0), reverse=True)
    return jobs


def _format_jobs(raw):
    counts = {'completed': 0, 'processing': 0, 'error': 0, 'unknown': 0}
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
        Route for the root URL.

        Returns:
            Response: Redirect to the upload UI.
        """
        return redirect('/ui/jobs')

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

    @app.route('/ui/upload', methods=['GET'])
    @require_auth
    def upload_ui():
        max_size = int(os.getenv('MAX_FILE_SIZE_MB', '20'))
        return render_template('upload.html', max_file_size=max_size * 1024 * 1024, active_nav='upload')

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

        try:
            with open(dest, 'wb') as out:
                for i in range(1, total_chunks + 1):
                    p = _chunk_path(identifier, i)
                    with open(p, 'rb') as inp:
                        out.write(inp.read())
        except FileNotFoundError:
            return jsonify(error="Missing chunk — upload may have failed"), 400

        # clean up chunk dir
        import shutil
        shutil.rmtree(os.path.join(CHUNK_DIR, identifier), ignore_errors=True)

        meta = {"original_name": filename}
        with open(dest + '.meta.json', 'w') as f:
            json.dump(meta, f)

        try:
            columns = _read_columns(dest, ext)
            rows = _read_rows(dest, ext)
        except Exception as e:
            os.remove(dest)
            return jsonify(error=f"Failed to read file: {e}"), 400

        return jsonify(file=unique_name, columns=columns, rows=rows)

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

    @app.route('/ui/jobs', methods=['GET'])
    @require_auth
    def ui_jobs_html():
        return render_template('jobs.html', active_nav='jobs')

    @app.route('/ui/explore', methods=['GET'])
    @require_auth
    def ui_explore():
        return render_template('explore.html', active_nav='explore')

    @app.route('/ui/tokens', methods=['GET'])
    @require_auth
    def ui_tokens():
        return render_template('tokens.html', active_nav='tokens')

    @app.route('/ui/quality', methods=['GET'])
    @require_auth
    def ui_quality():
        return render_template('quality.html', active_nav='quality')

    @app.route('/ui/jobs/data', methods=['GET'])
    @require_auth
    def ui_jobs_data():
        jobs = _load_jobs()
        return jsonify(jobs=jobs)

    # ── Auth UI ────────────────────────────────────────────────

    @app.route('/ui/login', methods=['GET'])
    def ui_login():
        return render_template('login.html')

    @app.route('/ui/register', methods=['GET'])
    def ui_register():
        return render_template('register.html')

    @app.route('/ui/jobs/events', methods=['GET'])
    @require_auth
    def ui_jobs_events():
        with _sse_lock:
            if len(_sse_queues) >= _MAX_SSE_CLIENTS:
                return jsonify(error="Too many SSE connections. Try again later."), 503
        def generate():
            q = queue.Queue(maxsize=16)
            with _sse_lock:
                _sse_queues.append(q)
            try:
                raw, counts = _format_jobs(_load_jobs())
                yield f"data: {json.dumps({'jobs': raw, 'counts': counts})}\n\n"
                while True:
                    try:
                        q.get(timeout=60)
                    except queue.Empty:
                        # Send keepalive comment and continue waiting
                        yield ": keepalive\n\n"
                        continue
                    raw, counts = _format_jobs(_load_jobs())
                    yield f"data: {json.dumps({'jobs': raw, 'counts': counts})}\n\n"
            except GeneratorExit:
                pass
            finally:
                with _sse_lock:
                    if q in _sse_queues:
                        _sse_queues.remove(q)
        return Response(stream_with_context(generate()), mimetype='text/event-stream')

"""
Upload endpoints: a tus 1.0 server (core + creation) and the upload lifecycle.

    POST   /ui/uploads/tus            create; returns 201 + Location
    HEAD   /ui/uploads/tus/<id>       current Upload-Offset, for resuming
    PATCH  /ui/uploads/tus/<id>       append bytes at Upload-Offset
    GET    /ui/uploads                the caller's unfinished uploads
    GET    /ui/uploads/<id>           status, preview, suggested mapping
    POST   /ui/uploads/<id>/process   save the mapping and enqueue ingestion
    DELETE /ui/uploads/<id>           discard

All of these sit under /ui/, which nginx and the Vite dev server already proxy
with no body-size limit and buffering off.

HEAD, PATCH and the status GET are exempt from the default rate limit. A large
file is many PATCHes and inspection is polled, so counting them is what made
the old chunk flow trip 429s on any file over a few hundred MB.
"""
from flask import current_app, jsonify, request

from app.api.user import get_current_user
from app.auth import require_auth
from app.config import get_settings
from app.rate_limit import limiter
from app.services import uploads as service
from app.services.uploads import TUS_VERSION, UploadError
from app.utils.logging import get_logger

logger = get_logger(__name__)

TUS_PATH = '/ui/uploads/tus'


def _owner_id():
    user = get_current_user()
    return user.id if user is not None else None


def _tus_headers(extra: dict | None = None) -> dict:
    headers = {'Tus-Resumable': TUS_VERSION, 'Cache-Control': 'no-store'}
    headers.update(extra or {})
    return headers


def _tus_error(e: UploadError):
    return jsonify(error=str(e)), e.status, _tus_headers()


def _check_tus_version():
    sent = request.headers.get('Tus-Resumable')
    if sent is not None and sent != TUS_VERSION:
        raise UploadError(f'Unsupported tus version {sent}', 412)


def _int_header(name: str) -> int:
    raw = request.headers.get(name)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise UploadError(f'Missing or invalid {name} header')
    if value < 0:
        raise UploadError(f'Invalid {name} header')
    return value


def _parse_metadata(raw: str | None) -> dict[str, str]:
    """tus Upload-Metadata: comma-separated `key base64(value)` pairs."""
    import base64
    import binascii

    meta = {}
    for pair in (raw or '').split(','):
        parts = pair.strip().split(' ', 1)
        if not parts[0]:
            continue
        try:
            meta[parts[0]] = base64.b64decode(parts[1]).decode('utf-8') if len(parts) > 1 else ''
        except (binascii.Error, UnicodeDecodeError):
            raise UploadError(f'Upload-Metadata value for {parts[0]!r} is not valid base64')
    return meta


def enqueue_upload(upload):
    """Hand a submitted upload to the worker. Shared with /ui/process (Retry)."""
    if not get_settings().HOUSEKEEPING_ENABLED:
        service.mark_ready(upload, 'Mapping saved, but no worker is configured. '
                                   'Set HOUSEKEEPING_ENABLED=true and start a Celery worker.')
        return jsonify(id=upload.id, file=service.to_dict(upload)['file'],
                       message=upload.error, queued=False)

    from app.services.housekeeper import process_file_async
    queued = process_file_async(
        file_path=upload.stored_path,
        column_map=upload.column_map,
        original_filename=upload.original_name,
    )
    if not queued:
        # Leave it submittable rather than claiming it started: the old flow
        # said "Processing started" whether or not the broker took the job.
        service.mark_ready(upload, 'The job queue is unreachable. Try again shortly.')
        return jsonify(error=upload.error), 503
    return jsonify(id=upload.id, file=service.to_dict(upload)['file'],
                   message='Processing started', queued=True)


def register_upload_routes(app):

    # ── tus protocol ───────────────────────────────────────────

    @app.route(TUS_PATH, methods=['POST'])
    @require_auth
    def tus_create():
        try:
            _check_tus_version()
            if request.headers.get('Upload-Defer-Length'):
                raise UploadError('Upload-Defer-Length is not supported; send Upload-Length')
            size = _int_header('Upload-Length')
            meta = _parse_metadata(request.headers.get('Upload-Metadata'))
            upload = service.create(_owner_id(), meta.get('filename', ''), size,
                                    get_settings().max_file_size)
        except UploadError as e:
            return _tus_error(e)
        return '', 201, _tus_headers({'Location': f'{TUS_PATH}/{upload.id}', 'Upload-Offset': '0'})

    @app.route(f'{TUS_PATH}/<upload_id>', methods=['HEAD'])
    @limiter.exempt
    @require_auth
    def tus_head(upload_id):
        try:
            _check_tus_version()
            upload = service.get_owned(upload_id, _owner_id())
        except UploadError as e:
            return '', e.status, _tus_headers()
        return '', 200, _tus_headers({
            'Upload-Offset': str(service.current_offset(upload)),
            'Upload-Length': str(upload.size),
        })

    @app.route(f'{TUS_PATH}/<upload_id>', methods=['PATCH'])
    @limiter.exempt
    @require_auth
    def tus_patch(upload_id):
        try:
            _check_tus_version()
            if request.mimetype != 'application/offset+octet-stream':
                raise UploadError('Content-Type must be application/offset+octet-stream', 415)
            offset = _int_header('Upload-Offset')
            upload = service.get_owned(upload_id, _owner_id())
            new_offset = service.append(upload, offset, request.stream, request.content_length)
            if new_offset == upload.size and upload.status == 'receiving':
                service.finish_receiving(upload)
                service.ensure_inspection(current_app._get_current_object(), upload)
        except UploadError as e:
            return _tus_error(e)
        return '', 204, _tus_headers({'Upload-Offset': str(new_offset)})

    # ── Lifecycle ──────────────────────────────────────────────

    @app.route('/ui/uploads', methods=['GET'])
    @require_auth
    def uploads_list():
        """Unfinished uploads, so a page reload does not lose files awaiting mapping."""
        return jsonify(uploads=[service.to_dict(u) for u in service.list_owned(_owner_id())])

    @app.route('/ui/uploads/<upload_id>', methods=['GET'])
    @limiter.exempt
    @require_auth
    def uploads_get(upload_id):
        try:
            upload = service.get_owned(upload_id, _owner_id())
        except UploadError as e:
            return jsonify(error=str(e)), e.status
        # Also the recovery path: an inspection whose worker died is restarted
        # here once its heartbeat goes stale.
        service.ensure_inspection(current_app._get_current_object(), upload)
        return jsonify(service.to_dict(upload))

    @app.route('/ui/uploads/<upload_id>/process', methods=['POST'])
    @require_auth
    def uploads_process(upload_id):
        body = request.get_json(silent=True) or {}
        try:
            upload = service.get_owned(upload_id, _owner_id())
            service.submit(upload, body.get('column_map'))
        except UploadError as e:
            return jsonify(error=str(e)), e.status
        return enqueue_upload(upload)

    @app.route('/ui/uploads/<upload_id>', methods=['DELETE'])
    @require_auth
    def uploads_delete(upload_id):
        try:
            upload = service.get_owned(upload_id, _owner_id())
        except UploadError as e:
            return jsonify(error=str(e)), e.status
        if upload.status == 'submitted':
            return jsonify(error='Already submitted for processing'), 409
        service.discard(upload)
        return '', 204

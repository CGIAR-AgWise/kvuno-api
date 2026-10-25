"""File upload API for ingesting RDS/Parquet files into the database."""
import os
import uuid

from flask import request
from flask_openapi3 import Tag, APIBlueprint
from pydantic import BaseModel, Field

from app.auth import require_auth
from app.cache import invalidate_planting_data_cache
from app.config import API_PREFIX, API_VERSION, ALLOWED_EXTENSIONS, get_settings
from app.dto.upload import UploadResponse
from app.rate_limit import limiter
from app.utils.logging import get_logger
from app.utils.preview import read_columns
from app.utils.rds_to_parquet import ConversionError, convert_for_ingestion

logger = get_logger(__name__)

__bp__ = "/data"
url_prefix = API_PREFIX + API_VERSION + __bp__

tag = Tag(name="ingestion", description="File upload and data ingestion")
api = APIBlueprint(__bp__, __name__, url_prefix=url_prefix, abp_tags=[tag], abp_security=[{"jwt": []}])


class UploadBatchResponse(BaseModel):
    files: list[UploadResponse] = Field(..., description="List of upload results")


def _process_uploaded_file(f):
    if not f.filename:
        return {"error": "Empty filename"}, 400

    ext = str(os.path.splitext(f.filename)[1].lower())
    if ext not in ALLOWED_EXTENSIONS:
        return {"error": f"Unsupported extension {ext}. Allowed: {ALLOWED_EXTENSIONS}"}, 400

    f.seek(0, os.SEEK_END)
    size = f.tell()
    f.seek(0)
    settings = get_settings()
    if size > settings.max_file_size:
        return {
            "error": f"File too large ({size / 1024 / 1024:.1f} MB). Maximum allowed: {settings.MAX_FILE_SIZE_MB} MB"}, 413

    data_dir = settings.HOUSEKEEPING_DATA_DIR
    os.makedirs(data_dir, exist_ok=True)

    unique_name = f"{uuid.uuid4().hex}{ext}"
    dest = os.path.join(data_dir, unique_name)
    f.save(dest)

    # Convert RDS -> Parquet once (in a child process) so the worker does not
    # re-parse the whole file. Keeps the RDS if only the Parquet write fails.
    try:
        stored = convert_for_ingestion(dest)
    except ConversionError as e:
        try:
            os.remove(dest)
        except OSError:
            pass
        return {"error": str(e), "file": unique_name}, 400
    stored_ext = os.path.splitext(stored)[1].lower()

    try:
        columns = read_columns(stored, stored_ext)
    except Exception:
        for p in (stored, stored + '.preview.json'):
            if os.path.exists(p):
                os.remove(p)
        # The parser's exception text can carry server paths and library
        # internals, so it goes to the log and the client gets a stable message.
        logger.exception(f"Failed to read columns of uploaded file {unique_name}")
        return {"error": "Failed to read file columns. Is it a valid RDS or Parquet file?",
                "file": unique_name}, 400

    # Invalidate cached aggregation data since the dataset changed. Every
    # planting-data prefix, not a hand-picked list: 'coordinates' does not
    # match the 'coordinates_bulk' keys, so the map kept its stale sample.
    invalidate_planting_data_cache()

    message = "File saved. Set HOUSEKEEPING_ENABLED=true and start a Celery worker for background processing."
    if settings.HOUSEKEEPING_ENABLED:
        from app.services.housekeeper import process_file_async
        # Say so when the broker refused it, rather than "accepted" regardless.
        # The file stays in the data directory, so the next startup scan or a
        # Retry picks it up.
        message = ("File accepted for processing" if process_file_async(file_path=stored)
                   else "File saved, but the job queue is unreachable; it will be processed on the next worker start.")

    return UploadResponse(
        message=message,
        # The stored name may differ from the upload name: an RDS is converted
        # to Parquet, so return whatever the worker will actually read.
        file=os.path.basename(stored),
        columns=columns,
    )


@api.post('/upload',
          responses={202: UploadBatchResponse, 400: {"description": "Upload error"},
                     401: {"description": "Authentication required"}, 413: {"description": "File too large"}},
          summary="Upload RDS/Parquet files for ingestion",
          description="Upload one or more .rds or .parquet files. Each file is validated, saved, and processed in the background by a Celery worker.")
@require_auth
@limiter.limit(lambda: get_settings().RATE_LIMIT_UPLOAD)
def upload_file():
    """Upload one or more RDS or Parquet files for processing.

    Send a single file via the ``file`` field, or multiple files via the
    ``files`` field. Each file is saved to the housekeeping data directory
    and processed in the background.
    """
    files = request.files.getlist('files')
    if not files:
        single = request.files.get('file')
        if single:
            files = [single]

    if not files:
        return {"error": "No file provided. Use 'file' (single) or 'files' (multiple)."}, 400

    results = []
    errors = []
    for f in files:
        result = _process_uploaded_file(f)
        if isinstance(result, tuple):
            errors.append({"file": f.filename, "error": result[0]["error"]})
        else:
            results.append(result)

    status_code = 202 if results else 400
    body = UploadBatchResponse(files=results).model_dump()
    if errors:
        body["errors"] = errors
    return body, status_code

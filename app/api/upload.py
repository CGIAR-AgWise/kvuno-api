"""File upload API for ingesting RDS/Parquet files into the database."""
import os
import uuid

import pandas as pd
import pyreadr
from flask import request
from flask_openapi3 import Tag, APIBlueprint
from pydantic import BaseModel, Field

from app.auth import require_auth
from app.cache import invalidate_cache
from app.config import API_PREFIX, API_VERSION, HOUSEKEEPING_DATA_DIR, HOUSEKEEPING_ENABLED, RATE_LIMIT_UPLOAD
from app.config import MAX_FILE_SIZE, ALLOWED_EXTENSIONS
from app.dto.upload import UploadResponse
from app.rate_limit import limiter

__bp__ = "/data"
url_prefix = API_PREFIX + API_VERSION + __bp__

tag = Tag(name="ingestion", description="File upload and data ingestion")
api = APIBlueprint(__bp__, __name__, url_prefix=url_prefix, abp_tags=[tag], abp_security=[{"jwt": []}])

DATA_DIR = HOUSEKEEPING_DATA_DIR


def _read_columns(path: str, ext: str):
    if ext == '.parquet':
        return pd.read_parquet(path, nrows=0).columns.tolist()
    data = pyreadr.read_r(path)
    return data[None].columns.tolist()


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
    if size > MAX_FILE_SIZE:
        return {
            "error": f"File too large ({size / 1024 / 1024:.1f} MB). Maximum allowed: {MAX_FILE_SIZE / 1024 / 1024:.0f} MB"}, 413

    os.makedirs(DATA_DIR, exist_ok=True)

    unique_name = f"{uuid.uuid4().hex}{ext}"
    dest = os.path.join(DATA_DIR, unique_name)
    f.save(dest)

    try:
        columns = _read_columns(dest, ext)
    except Exception as e:
        return {"error": f"Failed to read file columns: {e}", "file": unique_name}, 400

    # Invalidate cached aggregation data since the dataset changed
    for prefix in ('filters', 'coordinates', 'clusters'):
        invalidate_cache(prefix)

    if HOUSEKEEPING_ENABLED:
        from app.services.housekeeper import process_file_async
        process_file_async(file_path=dest)

    return UploadResponse(
        message="File accepted for processing" if HOUSEKEEPING_ENABLED
        else "File saved. Set HOUSEKEEPING_ENABLED=true and start a Celery worker for background processing.",
        file=unique_name,
        columns=columns,
    )


@api.post('/upload',
          responses={202: UploadBatchResponse, 400: {"description": "Upload error"},
                     401: {"description": "Authentication required"}, 413: {"description": "File too large"}},
          summary="Upload RDS/Parquet files for ingestion",
          description="Upload one or more .rds or .parquet files. Each file is validated, saved, and processed in the background by a Celery worker.")
@require_auth
@limiter.limit(RATE_LIMIT_UPLOAD)
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

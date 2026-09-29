"""
Column and sample-row preview for uploaded data files.

Shared by the chunked UI upload path (`app/routes/main.py`) and the single-file
API (`app/api/upload.py`), which previously carried duplicate copies.

Why this is not trivial
-----------------------
Reading a Parquet file is cheap: the column list comes from the footer alone,
and rows are read one batch from the first row group. Reading an **RDS** file
is not — `pyreadr` deserialises the whole R object into a DataFrame, so
previewing 5 rows costs the same as loading the entire file. There is no
partial-read API for RDS.

Two consequences this module handles:

- The result is cached in a `.preview.json` sidecar, keyed on the file's size
  and mtime, so a file is only ever parsed once. The upload path names files
  with a uuid, so a cached preview cannot go stale; the key guards the case
  where it might.
- `elapsed_ms` is returned so callers can show how long a slow first parse
  actually took, instead of the request looking hung.

Callers are still responsible for bounding file size before calling in — an
RDS of arbitrary size is unbounded work, and `MAX_FILE_SIZE` is the guard.
"""
from __future__ import annotations

import json
import os
import time

import pyarrow.parquet as pq
import pyreadr

DEFAULT_ROWS = 5
PREVIEW_SUFFIX = '.preview.json'


def read_columns(path: str, ext: str) -> list[str]:
    """Column names only. Cheap for Parquet, full parse for RDS."""
    if ext == '.parquet':
        # ParquetFile.schema reads only the footer — no row data touched.
        # Note pandas' read_parquet has no `nrows`; it forwards kwargs to
        # pyarrow.read_table, which rejects it.
        return list(pq.ParquetFile(path).schema.names)
    return list(pyreadr.read_r(path)[None].columns)


def read_rows(path: str, ext: str, n: int = DEFAULT_ROWS) -> list[dict]:
    """First `n` rows as JSON-safe dicts. Full parse for RDS."""
    if ext == '.parquet':
        # Read only as many rows as asked for, from the first row group.
        batch = next(pq.ParquetFile(path).iter_batches(batch_size=n), None)
        if batch is None:
            return []
        return json.loads(batch.to_pandas().head(n).to_json(orient='records'))
    df = pyreadr.read_r(path)[None].head(n)
    return json.loads(df.to_json(orient='records'))


def _cache_path(path: str) -> str:
    return path + PREVIEW_SUFFIX


def _load_cached(path: str) -> dict | None:
    try:
        with open(_cache_path(path), encoding='utf-8') as fh:
            payload = json.load(fh)
    except (OSError, ValueError):
        return None
    stat = os.stat(path)
    if payload.get('size') != stat.st_size or payload.get('mtime') != int(stat.st_mtime):
        return None
    # Drop the cache bookkeeping keys; they are not part of the response.
    return {'columns': payload['columns'], 'rows': payload['rows'],
            'elapsed_ms': payload.get('elapsed_ms', 0)}


def build_preview(path: str, ext: str, n: int = DEFAULT_ROWS, use_cache: bool = True) -> dict:
    """Return `{"columns": [...], "rows": [...], "elapsed_ms": int, "cached": bool}`."""
    if use_cache:
        hit = _load_cached(path)
        if hit is not None:
            return {**hit, 'cached': True, 'elapsed_ms': 0}

    started = time.perf_counter()
    columns = read_columns(path, ext)
    rows = read_rows(path, ext, n)
    elapsed_ms = int((time.perf_counter() - started) * 1000)

    payload = {'columns': columns, 'rows': rows, 'elapsed_ms': elapsed_ms}
    if use_cache:
        stat = os.stat(path)
        try:
            with open(_cache_path(path), 'w', encoding='utf-8') as fh:
                json.dump({**payload, 'size': stat.st_size, 'mtime': int(stat.st_mtime)}, fh)
        except OSError:
            pass  # a missing cache is not worth failing the preview over

    return {**payload, 'cached': False}


def iter_chunks(identifier: str, chunk_path, total: int, max_bytes: int):
    """Yield chunk contents, raising FileTooLarge once `max_bytes` is passed.

    Bounding the merge is what keeps a single request from filling the disk and
    then hanging on a full RDS parse.
    """
    total_bytes = 0
    for i in range(1, total + 1):
        with open(chunk_path(identifier, i), 'rb') as fh:
            while True:
                block = fh.read(1024 * 1024)
                if not block:
                    break
                total_bytes += len(block)
                if total_bytes > max_bytes:
                    raise FileTooLarge(total_bytes, max_bytes)
                yield block


class FileTooLarge(Exception):
    def __init__(self, size: int, limit: int):
        super().__init__(f'File too large ({size / 1024 / 1024:.1f} MB). Maximum allowed: {limit / 1024 / 1024:.0f} MB')
        self.size = size
        self.limit = limit

"""Convert RDS files to Parquet on your own machine, before uploading them.

The server converts uploaded RDS files itself, but reading an RDS takes about
150x its size in memory (pyreadr reads the whole file), so a large one can
fail with "needs more memory than the server allows". Converting locally and
uploading the Parquet skips that step entirely. It uses the same converter
as the server (a child process, Parquet written in row groups), so the
result imports exactly like a server-converted file.

    python scripts/convert_rds.py data/field.RDS
    python scripts/convert_rds.py data/ --out-dir converted/
    make convert-rds ARGS="data/ --overwrite"

The RDS files are kept. Exits 1 if any file failed.
"""
import os
import sys
import time
from pathlib import Path

# Ensure the project root is on sys.path
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    # noinspection PyTypeChecker
    sys.path.insert(0, _project_root)

# The converter's own log lines would break up the progress line, and each
# outcome is printed anyway. LOG_LEVEL=INFO brings them back.
os.environ.setdefault('LOG_LEVEL', 'ERROR')

import typer  # noqa: E402

from app.utils.rds_to_parquet import ConversionError, convert  # noqa: E402

app = typer.Typer(
    name="convert-rds",
    help="Convert RDS files to Parquet locally, to upload the Parquet instead.",
    add_completion=False,
)


def _rds_files(paths: list[Path]) -> list[Path]:
    """The RDS files named, and those directly inside any directory named."""
    found = []
    for path in paths:
        if path.is_dir():
            found.extend(sorted(p for p in path.iterdir() if p.is_file() and p.suffix.lower() == '.rds'))
        elif path.suffix.lower() == '.rds':
            found.append(path)
        else:
            typer.echo(f"skipped {path}: not an .rds file or a directory", err=True)
    return found


def _duration(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 60}m {seconds % 60:02d}s" if seconds >= 60 else f"{seconds}s"


def _progress_line(name: str):
    """One status line per file, rewritten in place as the conversion moves."""
    def show(stage, done, total, elapsed):
        if stage == 'reading':
            text = f"reading the R file ({_duration(elapsed)})"
        elif total:
            text = f"writing {done:,} of {total:,} rows ({done * 100 // total}%)"
        else:
            text = "writing"
        typer.echo(f"\r  {name}: {text}".ljust(78), nl=False)
    return show


def _size(path: Path) -> str:
    size = path.stat().st_size
    return f"{size / 1024 / 1024:.1f} MB" if size >= 1024 * 1024 else f"{max(1, size // 1024)} KB"


@app.command()
def main(
    paths: list[Path] = typer.Argument(..., help="RDS files, or directories of them (not recursive)."),
    out_dir: Path | None = typer.Option(
        None, "--out-dir", "-o",
        help="Where to write the Parquet files. Defaults to next to each RDS.",
    ),
    overwrite: bool = typer.Option(
        False, "--overwrite", help="Replace a Parquet file that already exists.",
    ),
):
    files = _rds_files(paths)
    if not files:
        typer.echo("No .rds files found.", err=True)
        raise typer.Exit(1)
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)

    failed = skipped = 0
    for rds in files:
        target = (out_dir or rds.parent) / (rds.stem + '.parquet')
        if target.exists() and not overwrite:
            skipped += 1
            typer.echo(f"  {rds.name}: {target} exists, skipped (use --overwrite)")
            continue
        started = time.monotonic()
        try:
            convert(str(rds), str(target), on_progress=_progress_line(rds.name))
        except ConversionError as e:
            failed += 1
            typer.echo(f"\r  {rds.name}: FAILED — {e}".ljust(78))
            continue
        typer.echo(f"\r  {rds.name}: {_size(rds)} -> {target} ({_size(target)}) "
                   f"in {_duration(time.monotonic() - started)}".ljust(78))

    typer.echo(f"{len(files) - failed - skipped} of {len(files)} converted"
               + (f", {skipped} skipped" if skipped else "") + (f", {failed} failed" if failed else "") + ".")
    if failed:
        raise typer.Exit(1)


if __name__ == "__main__":
    app()

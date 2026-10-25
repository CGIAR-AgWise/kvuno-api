"""Convert RDS files to Parquet on your own machine, before uploading them.

The server converts uploaded RDS files itself, but reading an RDS takes about
150x its size in memory (pyreadr reads the whole file), so a large one can
fail with "needs more memory than the server allows". Converting locally and
uploading the Parquet skips that step entirely. It uses the same converter
as the server (a child process, Parquet written in row groups), so the
result imports exactly like a server-converted file.

    python scripts/convert_rds.py                       # every RDS under static/
    python scripts/convert_rds.py data/field.RDS
    python scripts/convert_rds.py data/ --out-dir converted/
    make convert-rds ARGS="data/ --overwrite"

Directories are searched recursively (--no-recursive for the top level only),
skipping hidden directories and `uploads/` — those files belong to uploads in
progress and are converted by the app. With --out-dir the layout under each
directory is kept, so two `field.RDS` in different folders do not collide.
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


# The default when no path is given: the app's static tree (static/data,
# static/data_old, ...).
STATIC_DIR = Path(_project_root) / 'static'

# Directories never searched: uploads in progress are the app's to convert.
SKIP_DIRS = {'uploads', 'node_modules', '__pycache__'}


def _is_rds(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() == '.rds'


def _rds_files(paths: list[Path], recursive: bool = True) -> list[tuple[Path, Path]]:
    """(rds file, the directory it was found under) for every RDS named or found.

    The directory is what --out-dir mirrors; a file named directly is its own
    parent's.
    """
    found = []
    for path in paths:
        if path.is_dir():
            if recursive:
                for root, dirs, files in os.walk(path):
                    dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith('.'))
                    found.extend((Path(root) / f, path) for f in sorted(files) if f.lower().endswith('.rds'))
            else:
                found.extend((p, path) for p in sorted(path.iterdir()) if _is_rds(p))
        elif _is_rds(path):
            found.append((path, path.parent))
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
    paths: list[Path] | None = typer.Argument(
        None, help="RDS files, or directories to search. Defaults to the static/ directory.",
    ),
    recursive: bool = typer.Option(
        True, "--recursive/--no-recursive", help="Search directories' subdirectories too.",
    ),
    out_dir: Path | None = typer.Option(
        None, "--out-dir", "-o",
        help="Where to write the Parquet files. Defaults to next to each RDS.",
    ),
    overwrite: bool = typer.Option(
        False, "--overwrite", help="Replace a Parquet file that already exists.",
    ),
):
    paths = paths or [STATIC_DIR]
    files = _rds_files(paths, recursive)
    if not files:
        typer.echo(f"No .rds files found in {', '.join(str(p) for p in paths)}.", err=True)
        raise typer.Exit(1)

    failed = skipped = 0
    for rds, base in files:
        name = str(rds.relative_to(base)) if rds.is_relative_to(base) else rds.name
        if out_dir is None:
            target = rds.with_suffix('.parquet')
        else:
            target = out_dir / Path(name).with_suffix('.parquet')
            target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and not overwrite:
            skipped += 1
            typer.echo(f"  {name}: {target} exists, skipped (use --overwrite)")
            continue
        started = time.monotonic()
        try:
            convert(str(rds), str(target), on_progress=_progress_line(name))
        except ConversionError as e:
            failed += 1
            typer.echo(f"\r  {name}: FAILED — {e}".ljust(78))
            continue
        typer.echo(f"\r  {name}: {_size(rds)} -> {target} ({_size(target)}) "
                   f"in {_duration(time.monotonic() - started)}".ljust(78))

    typer.echo(f"{len(files) - failed - skipped} of {len(files)} converted"
               + (f", {skipped} skipped" if skipped else "") + (f", {failed} failed" if failed else "") + ".")
    if failed:
        raise typer.Exit(1)


if __name__ == "__main__":
    app()

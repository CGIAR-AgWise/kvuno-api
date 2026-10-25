"""Housekeeping CLI for processing RDS/Parquet files into the database.

This module contains only CLI wiring. All processing logic lives in
:mod:`app.services.housekeeper`.
"""

import typer

from app.config import get_settings
from app.services.housekeeper import run as run_housekeeping


app = typer.Typer(
    name="kvuno-housekeeping",
    help="Process RDS/Parquet agricultural files into the database.",
    add_completion=False,
)


@app.command()
def main(
    data_folder: str = typer.Argument(
        default_factory=lambda: get_settings().HOUSEKEEPING_DATA_DIR,
        help="Data directory to scan.",
    ),
    watch: bool = typer.Option(
        False,
        "--watch",
        "-w",
        help="Continuously watch the directory for new files.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Scan and report what would be processed without modifying the database.",
    ),
    batch_size: int | None = typer.Option(
        None,
        "--batch-size",
        help="Batch insert size. Defaults to HOUSEKEEPING_BATCH_SIZE.",
    ),
    chunk_size: int | None = typer.Option(
        None,
        "--chunk-size",
        help="Rows per processing chunk. Defaults to HOUSEKEEPING_CHUNK_SIZE.",
    ),
    checkpoint_interval: int | None = typer.Option(
        None,
        "--checkpoint-interval",
        help=(
            "Number of batches between checkpoints. "
            "Defaults to HOUSEKEEPING_CHECKPOINT_INTERVAL."
        ),
    ),
) -> None:
    """Process RDS/Parquet files in the configured data directory."""

    run_housekeeping(
        data_folder=data_folder,
        watch=watch,
        dry_run=dry_run,
        batch_size=batch_size,
        chunk_size=chunk_size,
        checkpoint_interval=checkpoint_interval,
    )


if __name__ == "__main__":
    app()

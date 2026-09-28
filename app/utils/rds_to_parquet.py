"""Convert .RDS files to .parquet for faster, splittable downstream processing."""
import os
import pyreadr


def rds_to_parquet(rds_path: str, parquet_path: str | None = None, compression: str = "zstd") -> str:
    """Read an RDS file and write it as Parquet.

    Args:
        rds_path: Path to the .RDS file.
        parquet_path: Output path (defaults to ``rds_path`` with ``.parquet`` extension).
        compression: Parquet compression codec (default ``zstd``).

    Returns:
        Path to the written Parquet file.
    """
    if parquet_path is None:
        parquet_path = os.path.splitext(rds_path)[0] + ".parquet"

    result = pyreadr.read_r(rds_path)
    df = result[None]
    df.to_parquet(parquet_path, index=False, compression=compression)
    return parquet_path


def batch_convert(data_dir: str, pattern: str = ".RDS", delete_originals: bool = False) -> list[str]:
    """Convert all matching files in *data_dir* to Parquet.

    Args:
        data_dir: Directory to scan.
        pattern: File extension to match (default ``.RDS``).
        delete_originals: Remove the original RDS file after conversion.

    Returns:
        List of created Parquet paths.
    """
    created = []
    for fname in os.listdir(data_dir):
        if fname.upper().endswith(pattern.upper()):
            rds_path = os.path.join(data_dir, fname)
            parquet_path = rds_to_parquet(rds_path)
            created.append(parquet_path)
            if delete_originals:
                os.remove(rds_path)
    return created

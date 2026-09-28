"""
Housekeeping script — processes RDS/Parquet files into the database.

Thin CLI wrapper; all processing logic lives in app/services/housekeeper.py.
"""
import argparse
import os

from app.services.housekeeper import cli_run

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="KVuno housekeeping — process RDS/Parquet files")
    parser.add_argument('--watch', action='store_true', help="Watch directory for new files")
    parser.add_argument('--dry-run', action='store_true', help="Scan and report what would be processed without touching DB")
    parser.add_argument('--batch-size', type=int, default=None, help="Batch insert size")
    parser.add_argument('--chunk-size', type=int, default=None, help="Rows per chunk")
    parser.add_argument('--checkpoint-interval', type=int, default=None, help="Batches per checkpoint")
    parser.add_argument('data_folder', nargs='?', default=os.path.join("static/", 'data'),
                        help="Data directory (default: static/data)")

    cli_run(parser.parse_args())

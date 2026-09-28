import os
import subprocess

from dotenv import load_dotenv
from loguru import logger
from sqlalchemy import create_engine, MetaData

from app.config import build_db_url

# Load environment variables from .env file
load_dotenv()


def create_dummy_file(filepath):
    """
    Create a dummy file with placeholder content.

    Args:
        filepath (str): The path to the file to be created.
    """
    try:
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        with open(filepath, 'w', encoding='UTF-8') as f:
            f.write("# This is a dummy ORM file\n")
            f.write("# Replace this content with generated ORM code\n")
        logger.info(f"Dummy file created at {filepath}")
    except OSError as e:
        logger.error(f"Failed to create dummy file at {filepath}: {e}")
        raise


def get_tables_in_schema(db_url):
    """
    Retrieve the list of tables in the database schema.

    Args:
        db_url (str): The database connection URL.

    Returns:
        list: A list of table names in the schema.
    """
    try:
        # Create the database engine
        engine = create_engine(db_url)

        # Reflect the database schema
        metadata = MetaData()
        metadata.reflect(bind=engine)

        # Get the list of tables
        tables = list(metadata.tables.keys())
        logger.debug(f"Tables in schema: {tables}")

        return tables
    except Exception as e:
        logger.error(f"Failed to retrieve tables from schema: {e}")
        raise


def run_sqlacodegen():
    """
    Run the sqlacodegen tool to generate model classes from the database schema.

    Reads environment variables for database URL and output file path,
    executes the `sqlacodegen` command, and handles any errors that occur.

    Environment Variables:
        - DB_URL / DB_*: The database connection URL (see app.config.build_db_url).
        - OUTFILE_PATH: The path where the generated model file will be saved (default: 'app/models/kvuno.py').
        - EXCLUDED_TABLES: Comma-separated names of tables to exclude from generation.

    Raises:
        RuntimeError: If the sqlacodegen command fails.
    """
    # Read environment variables
    db_url = build_db_url()

    outfile_path = os.getenv('OUTFILE_PATH', 'app/models/kvuno.py')
    excluded_tables = os.getenv('EXCLUDED_TABLES', 'spatial_ref_sys,alembic_version').split(',')

    # Create dummy file
    create_dummy_file(outfile_path)

    try:
        # Get all tables from the database
        all_tables = get_tables_in_schema(db_url)

        # Filter out excluded tables
        included_tables = [table for table in all_tables if table not in excluded_tables]

        if not included_tables:
            logger.warning("No tables left to generate models after exclusions.")
            exit(100)

        logger.info(f"Generating models for tables: {included_tables}")

        # Construct the command to generate models for the included tables
        command = [
            'sqlacodegen',
            db_url,
            '--outfile',
            outfile_path,
            '--tables',
            ','.join(included_tables),
            '--options',
            'use_inflect'
        ]

        result = subprocess.run(command, check=True, capture_output=True, text=True)
        logger.info("Output:\n" + result.stdout)
        if result.stderr:
            logger.error("Errors:\n" + result.stderr)

    except subprocess.CalledProcessError as e:
        logger.error(f"sqlacodegen failed with error: {e}")
        raise RuntimeError(f"sqlacodegen failed: {e}") from e
    except Exception as e:
        logger.error(f"An unexpected error occurred: {e}")
        raise


if __name__ == "__main__":
    run_sqlacodegen()

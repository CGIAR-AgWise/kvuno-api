from alembic import op
from sqlalchemy import Integer, BigInteger, text


def is_specified_db(db_dialect):
    dialect = op.get_bind().dialect.name
    return dialect == db_dialect


def is_sqlite() -> bool:
    return is_specified_db('sqlite')


def get_integer_column_type():
    """
    Returns the appropriate SQLAlchemy column type for the primary key based on the database dialect.
    """
    if op.get_bind().dialect.name == 'sqlite':
        return Integer
    else:
        return BigInteger


def now_default():
    """A "current time" server default that works on every dialect we migrate.

    ``now()`` is Postgres-only. SQLite accepts it in CREATE TABLE and then
    fails every INSERT that relies on it ("no such function: now"), so the
    zero-infra SQLite setup could create tables it could not write to.
    Postgres keeps ``now()`` exactly, so its DDL is unchanged.
    """
    return text('CURRENT_TIMESTAMP') if is_sqlite() else text('now()')

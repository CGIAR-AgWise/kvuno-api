
from app.config import build_db_url


def _reset(monkeypatch):
    """Clear DB_URL and restore DB_DRIVER to postgresql for each test."""
    monkeypatch.delenv("DB_URL", raising=False)
    monkeypatch.setenv("DB_DRIVER", "postgresql")


def test_build_db_url_defaults(monkeypatch):
    _reset(monkeypatch)
    monkeypatch.delenv("DB_HOST", raising=False)
    monkeypatch.delenv("DB_PORT", raising=False)
    monkeypatch.setenv("DB_USER", "postgres")
    monkeypatch.setenv("DB_PASSWORD", "postgres")
    monkeypatch.setenv("DB_NAME", "agwise_api")
    url = build_db_url()
    assert url == "postgresql://postgres:postgres@127.0.0.1:5432/agwise_api"


def test_build_db_url_full_url_takes_priority(monkeypatch):
    _reset(monkeypatch)
    monkeypatch.setenv("DB_URL", "postgresql://custom:p@ss@remote:9999/mydb")
    url = build_db_url()
    assert url == "postgresql://custom:p@ss@remote:9999/mydb"


def test_build_db_url_sqlite(monkeypatch):
    _reset(monkeypatch)
    monkeypatch.setenv("DB_DRIVER", "sqlite")
    monkeypatch.setenv("DB_NAME", "test.db")
    url = build_db_url()
    assert url == "sqlite:///test.db"


def test_build_db_url_sqlite_default_name(monkeypatch):
    _reset(monkeypatch)
    monkeypatch.setenv("DB_DRIVER", "sqlite")
    monkeypatch.delenv("DB_NAME", raising=False)
    url = build_db_url()
    assert url == "sqlite:///kvuno.db"


def test_build_db_url_custom_parts(monkeypatch):
    _reset(monkeypatch)
    monkeypatch.setenv("DB_HOST", "db.example.com")
    monkeypatch.setenv("DB_PORT", "15432")
    monkeypatch.setenv("DB_USER", "admin")
    monkeypatch.setenv("DB_PASSWORD", "secret")
    monkeypatch.setenv("DB_NAME", "kvuno_prod")
    url = build_db_url()
    assert url == "postgresql://admin:secret@db.example.com:15432/kvuno_prod"

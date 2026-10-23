"""Regression tests for auth fixes in app/api/user.py, app/api/quality.py and
app/dto/auth.py. Unit-only: the database is a MagicMock, as in test_security.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask
from pydantic import ValidationError

from app import create_app
from app.api.user import _hash_token, get_current_user
from app.models.kvuno import User, UserToken


def _naive_utc_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.fixture
def client():
    app = create_app()
    with app.test_client() as c:
        yield c


@pytest.fixture(autouse=True)
def no_rate_limit():
    """register/login carry per-hour limits shared across the whole test run."""
    from app.rate_limit import limiter
    previous = limiter.enabled
    limiter.enabled = False
    yield
    limiter.enabled = previous


def _token_session(token_record, user=None):
    session = MagicMock()
    token_query = MagicMock()
    token_query.filter.return_value.first.return_value = token_record
    user_query = MagicMock()
    user_query.filter.return_value.first.return_value = user

    def query(cls):
        return user_query if cls is User else token_query
    session.query.side_effect = query
    return session


def _resolve(token_record, user, bearer):
    session = _token_session(token_record, user)
    with (
        Flask(__name__).test_request_context(headers={"Authorization": f"Bearer {bearer}"}),
        patch("app.api.user.MyDb.get_db", return_value=MagicMock(session=session)),
    ):
        return get_current_user(), session


# ── Quality endpoints require auth ─────────────────────────────

class TestQualityRequiresAuth:
    def test_conflicts_without_token_is_401(self, client):
        repo = MagicMock()
        with patch("app.api.quality.repo", repo):
            resp = client.get("/api/v1/quality/conflicts")
        assert resp.status_code == 401
        repo.get_paginated.assert_not_called()

    def test_stats_without_token_is_401(self, client):
        repo = MagicMock()
        with patch("app.api.quality.repo", repo):
            resp = client.get("/api/v1/quality/stats")
        assert resp.status_code == 401
        repo.get_stats.assert_not_called()

    def test_stats_error_does_not_leak_exception_text(self, client):
        repo = MagicMock()
        repo.get_stats.side_effect = RuntimeError("password=hunter2 host=db.internal")
        with (
            patch("app.api.quality.repo", repo),
            patch("app.auth.get_current_user", return_value=MagicMock(id=1)),
        ):
            resp = client.get("/api/v1/quality/stats")
        assert resp.status_code == 500
        assert "hunter2" not in resp.get_data(as_text=True)


# ── Logout ─────────────────────────────────────────────────────

class TestLogout:
    def test_logout_route_is_the_function_not_the_model(self):
        """The decorator sat on TokenInfoResponse, so that class was the view."""
        from app.api.user import TokenInfoResponse
        app = create_app()
        view = app.view_functions[
            next(r.endpoint for r in app.url_map.iter_rules() if r.rule == "/api/v1/users/logout")
        ]
        assert view is not TokenInfoResponse
        # And the model is a plain model again.
        assert TokenInfoResponse(id=1, created_at="x").id == 1

    def test_logout_revokes_the_token(self, client):
        secret = "d" * 64
        record = MagicMock(spec=UserToken, id=7, token=_hash_token(7, secret), expires_at=None)
        session = _token_session(record)
        with patch("app.api.user.MyDb.get_db", return_value=MagicMock(session=session)):
            resp = client.post("/api/v1/users/logout",
                               headers={"Authorization": f"Bearer 7|{secret}"})
        assert resp.status_code == 200
        session.delete.assert_called_once_with(record)
        session.commit.assert_called()

    def test_logout_with_wrong_secret_is_401_and_deletes_nothing(self, client):
        record = MagicMock(spec=UserToken, id=7, token=_hash_token(7, "a" * 64), expires_at=None)
        session = _token_session(record)
        with patch("app.api.user.MyDb.get_db", return_value=MagicMock(session=session)):
            resp = client.post("/api/v1/users/logout",
                               headers={"Authorization": "Bearer 7|" + "b" * 64})
        assert resp.status_code == 401
        session.delete.assert_not_called()


# ── Token expiry with naive DB timestamps ──────────────────────

class TestTokenExpiry:
    user = User(id=1, username="u", email="u@cgiar.org", password_hash="x")

    def _record(self, expires_at, last_used_at=None):
        secret = "e" * 64
        record = MagicMock(spec=UserToken, id=1, token=_hash_token(1, secret),
                           expires_at=expires_at, last_used_at=last_used_at)
        record.user_id = 1
        return record, f"1|{secret}"

    def test_expired_naive_token_is_rejected(self):
        record, bearer = self._record(_naive_utc_now() - timedelta(minutes=1))
        user, _ = _resolve(record, self.user, bearer)
        assert user is None

    def test_unexpired_naive_token_is_accepted(self):
        record, bearer = self._record(_naive_utc_now() + timedelta(days=1))
        user, _ = _resolve(record, self.user, bearer)
        assert user is not None and user.id == 1

    def test_aware_value_is_also_handled(self):
        record, bearer = self._record(datetime.now(timezone.utc) + timedelta(days=1))
        user, _ = _resolve(record, self.user, bearer)
        assert user is not None

    def test_created_tokens_get_naive_utc_expiry(self, client, monkeypatch):
        import bcrypt
        monkeypatch.setattr("app.api.user.TOKEN_TTL_DAYS", 30)
        pw_hash = bcrypt.hashpw(b"correct-horse", bcrypt.gensalt(4)).decode()
        session = MagicMock()
        session.query.return_value.filter.return_value.first.return_value = User(
            id=1, username="u", email="u@cgiar.org", password_hash=pw_hash)
        session.execute.return_value.scalar.return_value = 5
        with patch("app.api.user.MyDb.get_db", return_value=MagicMock(session=session)):
            resp = client.post("/api/v1/users/login",
                               json={"username": "u", "password": "correct-horse"})
        assert resp.status_code == 200
        exp = session.execute.call_args_list[0].args[1]["exp"]
        assert exp.tzinfo is None
        assert timedelta(days=29) < exp - _naive_utc_now() <= timedelta(days=30)


class TestLastUsedThrottle:
    user = User(id=1, username="u", email="u@cgiar.org", password_hash="x")

    def _record(self, last_used_at):
        secret = "f" * 64
        record = MagicMock(spec=UserToken, id=1, token=_hash_token(1, secret),
                           expires_at=None, last_used_at=last_used_at)
        record.user_id = 1
        return record, f"1|{secret}"

    def test_recent_use_does_not_commit(self):
        recent = _naive_utc_now() - timedelta(seconds=30)
        record, bearer = self._record(recent)
        user, session = _resolve(record, self.user, bearer)
        assert user is not None
        session.commit.assert_not_called()
        assert record.last_used_at == recent

    def test_stale_use_is_refreshed(self):
        record, bearer = self._record(_naive_utc_now() - timedelta(minutes=10))
        _, session = _resolve(record, self.user, bearer)
        session.commit.assert_called_once()
        assert _naive_utc_now() - record.last_used_at < timedelta(seconds=5)

    def test_never_used_is_set(self):
        record, bearer = self._record(None)
        _, session = _resolve(record, self.user, bearer)
        session.commit.assert_called_once()
        assert record.last_used_at is not None


# ── Registration allow-list ────────────────────────────────────

def _register(client, email, password="longenough1"):
    session = MagicMock()
    session.query.return_value.filter.return_value.first.return_value = None
    with patch("app.api.user.MyDb.get_db", return_value=MagicMock(session=session)):
        resp = client.post("/api/v1/users/register",
                           json={"username": "newuser", "email": email, "password": password})
    return resp, session


class TestRegistrationIsOpenButPending:
    # Registration used to be gated by an email-domain allow-list
    # (REGISTRATION_ALLOWED_DOMAINS). Administrator approval replaced it: any
    # address may register, and the account starts pending.
    # tests/test_user_approval.py covers the approval itself.
    def test_any_domain_registers_as_pending(self, client):
        for email in ("a@gmail.com", "Someone@CGIAR.org", "x@sub.iita.org"):
            resp, session = _register(client, email)
            assert resp.status_code == 201, email
            assert resp.get_json()["status"] == "pending"
            assert session.add.call_args.args[0].status == "pending"

    def test_old_allow_list_setting_is_ignored(self, client, monkeypatch):
        monkeypatch.setenv("REGISTRATION_ALLOWED_DOMAINS", "cgiar.org")
        resp, _ = _register(client, "a@gmail.com")
        assert resp.status_code == 201

    def test_not_an_email_address_is_400(self, client):
        for email in ("cgiar.org", "@cgiar.org"):
            resp, session = _register(client, email)
            assert resp.status_code == 400, email
            session.add.assert_not_called()

# ── bcrypt 72-byte limit ───────────────────────────────────────

class TestPasswordByteLimit:
    def test_dto_rejects_over_72_bytes(self):
        from app.dto.auth import RegisterRequest
        with pytest.raises(ValidationError, match="72 bytes"):
            RegisterRequest(username="abc", email="a@b.c", password="a" * 73)

    def test_dto_counts_bytes_not_characters(self):
        from app.dto.auth import RegisterRequest
        # 40 characters, 80 bytes: passes max_length-style checks, not bcrypt.
        with pytest.raises(ValidationError):
            RegisterRequest(username="abc", email="a@b.c", password="é" * 40)

    def test_dto_accepts_exactly_72_bytes(self):
        from app.dto.auth import RegisterRequest
        assert RegisterRequest(username="abc", email="a@b.c", password="a" * 72)

    def test_register_endpoint_rejects_long_password(self, client, monkeypatch):
        resp, session = _register(client, "a@cgiar.org", password="a" * 100)
        assert 400 <= resp.status_code < 500
        session.add.assert_not_called()

    def test_login_with_long_password_is_401_not_500(self, client):
        session = MagicMock()
        session.query.return_value.filter.return_value.first.return_value = User(
            id=1, username="u", email="u@cgiar.org", password_hash="$2b$12$" + "x" * 53)
        with patch("app.api.user.MyDb.get_db", return_value=MagicMock(session=session)):
            resp = client.post("/api/v1/users/login",
                               json={"username": "u", "password": "a" * 100})
        assert resp.status_code == 401

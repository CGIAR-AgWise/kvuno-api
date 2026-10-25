"""Account approval: register -> pending -> approved by an administrator.

Runs the real endpoints against in-memory SQLite (users + user_tokens only),
so the status checks are exercised end to end rather than through mocks.
"""
import pytest

# Captured before the fixture replaces it, for the no-PII-in-logs test.
from app.services.mailer import send_mail as _real_send_mail

ADMIN = 'boss@cgiar.org'
PASSWORD = 'correct-horse-9'


@pytest.fixture
def env(monkeypatch):
    from app import create_app
    from app.models.database_conn import MyDb
    from app.models.kvuno import Base, User, UserToken
    from app.rate_limit import limiter
    from app.services import mailer

    monkeypatch.setenv('ADMIN_EMAILS', ADMIN)
    monkeypatch.setenv('SPA_ROOT_URL', 'https://kvuno-ui.example.org')
    sent = []
    monkeypatch.setattr(mailer, 'send_mail',
                        lambda to, subject, body, **kw: sent.append((to, subject, body)) or True)
    limiter.enabled = False

    app = create_app()
    with app.app_context():
        Base.metadata.create_all(MyDb.get_db().engine, tables=[User.__table__, UserToken.__table__])
        client = app.test_client()
        yield type('Env', (), {'client': client, 'sent': sent, 'session': MyDb.get_db().session})
        MyDb.get_db().session.remove()
    limiter.enabled = True


def _register(client, name, email):
    return client.post('/api/v1/users/register',
                       json={'username': name, 'email': email, 'password': PASSWORD})


def _login(client, name):
    return client.post('/api/v1/users/login', json={'username': name, 'password': PASSWORD})


def _auth(token):
    return {'Authorization': f'Bearer {token}'}


def _admin_token(env):
    _register(env.client, 'boss', ADMIN)
    return _login(env.client, 'boss').get_json()['access_token']


def _user_id(env, name):
    from app.models.kvuno import User
    env.session.expire_all()
    return env.session.query(User).filter_by(username=name).one().id


class TestRegistration:
    def test_new_account_is_pending_and_cannot_sign_in(self, env):
        resp = _register(env.client, 'alice', 'alice@cgiar.org')
        assert resp.status_code == 201
        assert resp.get_json()['status'] == 'pending'

        login = _login(env.client, 'alice')
        assert login.status_code == 403
        assert login.get_json()['status'] == 'pending'
        assert 'awaiting approval' in login.get_json()['msg']

    def test_admins_are_notified_of_a_new_registration(self, env):
        _register(env.client, 'alice', 'alice@cgiar.org')
        to, subject, body = env.sent[-1]
        assert to == [ADMIN]
        assert 'awaiting approval' in subject
        assert 'https://kvuno-ui.example.org/admin/users' in body

    def test_admin_email_is_approved_at_once(self, env):
        assert _register(env.client, 'boss', ADMIN).get_json()['status'] == 'approved'
        me = env.client.get('/api/v1/users/me', headers=_auth(_login(env.client, 'boss').get_json()['access_token']))
        assert me.get_json()['is_admin'] is True

    def test_wrong_password_does_not_reveal_the_status(self, env):
        _register(env.client, 'alice', 'alice@cgiar.org')
        resp = env.client.post('/api/v1/users/login', json={'username': 'alice', 'password': 'nope-nope-1'})
        assert resp.status_code == 401
        assert 'status' not in resp.get_json()


class TestAdminReview:
    def test_non_admins_get_403(self, env):
        token = _admin_token(env)  # noqa: F841 (creates the admin)
        _register(env.client, 'alice', 'alice@cgiar.org')
        env.client.post(f'/api/v1/admin/users/{_user_id(env, "alice")}/approve',
                        headers=_auth(token))
        alice = _login(env.client, 'alice').get_json()['access_token']
        assert env.client.get('/api/v1/admin/users', headers=_auth(alice)).status_code == 403

    def test_list_filters_by_status_and_counts(self, env):
        token = _admin_token(env)
        _register(env.client, 'alice', 'alice@cgiar.org')
        _register(env.client, 'bob', 'bob@cgiar.org')
        body = env.client.get('/api/v1/admin/users?status=pending', headers=_auth(token)).get_json()
        assert sorted(u['username'] for u in body['users']) == ['alice', 'bob']
        assert body['counts']['pending'] == 2
        assert body['counts']['approved'] == 1

    def test_approve_activates_and_sends_the_activation_email(self, env):
        token = _admin_token(env)
        _register(env.client, 'alice', 'alice@cgiar.org')
        resp = env.client.post(f'/api/v1/admin/users/{_user_id(env, "alice")}/approve', headers=_auth(token))
        assert resp.status_code == 200
        assert resp.get_json()['user']['status'] == 'approved'
        assert resp.get_json()['emailed'] is True

        to, subject, body = env.sent[-1]
        assert to == ['alice@cgiar.org']
        assert 'is active' in subject
        assert 'https://kvuno-ui.example.org/login' in body
        assert _login(env.client, 'alice').status_code == 200

    def test_approve_without_notify_sends_nothing(self, env):
        token = _admin_token(env)
        _register(env.client, 'alice', 'alice@cgiar.org')
        before = len(env.sent)
        env.client.post(f'/api/v1/admin/users/{_user_id(env, "alice")}/approve',
                        json={'notify': False}, headers=_auth(token))
        assert len(env.sent) == before

    def test_reject_emails_the_reason(self, env):
        token = _admin_token(env)
        _register(env.client, 'alice', 'alice@cgiar.org')
        resp = env.client.post(f'/api/v1/admin/users/{_user_id(env, "alice")}/reject',
                               json={'reason': 'Not a project member'}, headers=_auth(token))
        assert resp.get_json()['user']['status'] == 'rejected'
        assert 'Not a project member' in env.sent[-1][2]
        assert _login(env.client, 'alice').status_code == 403

    def test_disable_kills_existing_tokens(self, env):
        token = _admin_token(env)
        _register(env.client, 'alice', 'alice@cgiar.org')
        uid = _user_id(env, 'alice')
        env.client.post(f'/api/v1/admin/users/{uid}/approve', headers=_auth(token))
        alice = _login(env.client, 'alice').get_json()['access_token']
        assert env.client.get('/api/v1/users/me', headers=_auth(alice)).status_code == 200

        env.client.post(f'/api/v1/admin/users/{uid}/disable', headers=_auth(token))
        assert env.client.get('/api/v1/users/me', headers=_auth(alice)).status_code == 401
        assert _login(env.client, 'alice').get_json()['status'] == 'disabled'

    def test_invalid_transition_is_409(self, env):
        token = _admin_token(env)
        _register(env.client, 'alice', 'alice@cgiar.org')
        resp = env.client.post(f'/api/v1/admin/users/{_user_id(env, "alice")}/disable', headers=_auth(token))
        assert resp.status_code == 409

    def test_admins_cannot_be_changed_here(self, env):
        token = _admin_token(env)
        resp = env.client.post(f'/api/v1/admin/users/{_user_id(env, "boss")}/disable', headers=_auth(token))
        assert resp.status_code == 400


class TestRoles:
    """role 'admin' grants the Accounts screens; owners (ADMIN_EMAILS) are fixed."""

    def _approved(self, env, token, name):
        _register(env.client, name, f'{name}@cgiar.org')
        uid = _user_id(env, name)
        env.client.post(f'/api/v1/admin/users/{uid}/approve', json={'notify': False}, headers=_auth(token))
        return uid, _login(env.client, name).get_json()['access_token']

    def test_regular_users_cannot_see_admin_endpoints(self, env):
        owner = _admin_token(env)
        _, alice = self._approved(env, owner, 'alice')
        me = env.client.get('/api/v1/users/me', headers=_auth(alice)).get_json()
        assert me['role'] == 'user' and me['is_admin'] is False
        assert env.client.get('/api/v1/admin/users', headers=_auth(alice)).status_code == 403

    def test_promoted_user_gets_admin_access_and_loses_it_on_demotion(self, env):
        owner = _admin_token(env)
        uid, alice = self._approved(env, owner, 'alice')

        resp = env.client.post(f'/api/v1/admin/users/{uid}/role', json={'role': 'admin'}, headers=_auth(owner))
        assert resp.status_code == 200 and resp.get_json()['user']['role'] == 'admin'
        assert env.client.get('/api/v1/users/me', headers=_auth(alice)).get_json()['is_admin'] is True
        assert env.client.get('/api/v1/admin/users', headers=_auth(alice)).status_code == 200

        env.client.post(f'/api/v1/admin/users/{uid}/role', json={'role': 'user'}, headers=_auth(owner))
        assert env.client.get('/api/v1/admin/users', headers=_auth(alice)).status_code == 403

    def test_a_disabled_admin_is_locked_out(self, env):
        owner = _admin_token(env)
        uid, alice = self._approved(env, owner, 'alice')
        env.client.post(f'/api/v1/admin/users/{uid}/role', json={'role': 'admin'}, headers=_auth(owner))
        env.client.post(f'/api/v1/admin/users/{uid}/disable', headers=_auth(owner))
        assert env.client.get('/api/v1/admin/users', headers=_auth(alice)).status_code == 401
        assert _login(env.client, 'alice').status_code == 403

    def test_owners_and_yourself_cannot_be_changed(self, env):
        owner = _admin_token(env)
        uid, alice = self._approved(env, owner, 'alice')
        env.client.post(f'/api/v1/admin/users/{uid}/role', json={'role': 'admin'}, headers=_auth(owner))
        boss = _user_id(env, 'boss')
        # A role-admin cannot touch an owner…
        assert env.client.post(f'/api/v1/admin/users/{boss}/role', json={'role': 'user'},
                               headers=_auth(alice)).status_code == 400
        assert env.client.post(f'/api/v1/admin/users/{boss}/disable', headers=_auth(alice)).status_code == 400
        # …nor demote or disable themselves.
        assert env.client.post(f'/api/v1/admin/users/{uid}/role', json={'role': 'user'},
                               headers=_auth(alice)).status_code == 400
        assert env.client.post(f'/api/v1/admin/users/{uid}/disable', headers=_auth(alice)).status_code == 400

    def test_only_approved_accounts_get_a_role(self, env):
        owner = _admin_token(env)
        _register(env.client, 'bob', 'bob@cgiar.org')
        resp = env.client.post(f'/api/v1/admin/users/{_user_id(env, "bob")}/role', json={'role': 'admin'},
                               headers=_auth(owner))
        assert resp.status_code == 409

    def test_unknown_role_is_rejected(self, env):
        owner = _admin_token(env)
        uid, _ = self._approved(env, owner, 'alice')
        resp = env.client.post(f'/api/v1/admin/users/{uid}/role', json={'role': 'superuser'}, headers=_auth(owner))
        assert resp.status_code == 422


class TestNoPersonalDataInLogs:
    """Account and email logs identify people by id only (IITA data rules)."""

    def test_register_review_and_email_logs_carry_ids_not_addresses(self, env, monkeypatch, caplog):
        from app.services import mailer
        monkeypatch.setattr(mailer, 'send_mail', _real_send_mail)
        monkeypatch.setenv('SMTP_HOST', '')  # logged as "not sent"

        token = _admin_token(env)
        caplog.clear()
        with caplog.at_level('INFO'):
            _register(env.client, 'alice', 'alice@cgiar.org')
            _register(env.client, 'bob', 'bob@cgiar.org')
            alice, bob = _user_id(env, 'alice'), _user_id(env, 'bob')
            env.client.post(f'/api/v1/admin/users/{alice}/approve', headers=_auth(token))
            env.client.post(f'/api/v1/admin/users/{bob}/reject', json={'reason': 'no'}, headers=_auth(token))
            env.client.post(f'/api/v1/admin/users/{alice}/role', json={'role': 'admin'}, headers=_auth(token))

        logged = '\n'.join(str(r.msg) for r in caplog.records)
        for secret in ('alice@cgiar.org', 'bob@cgiar.org', ADMIN, "'alice'", "'bob'", 'boss'):
            assert secret not in logged, f'{secret} leaked into the logs'
        # …but each event is still traceable by id.
        assert f"'user_id': {alice}" in logged
        assert f"'user_id': {bob}" in logged
        assert f"'about_user_id': {alice}" in logged
        assert "'kind': 'account_activated'" in logged

    def test_smtp_failure_logs_the_code_not_the_message(self, monkeypatch, caplog):
        import smtplib
        from app.services import mailer
        monkeypatch.setenv('SMTP_HOST', 'smtp.example.org')
        monkeypatch.setenv('MAIL_FROM', 'noreply@example.org')

        def refuse(*args, **kwargs):
            raise smtplib.SMTPRecipientsRefused({'secret@cgiar.org': (550, b'no such user secret@cgiar.org')})
        monkeypatch.setattr(mailer.smtplib, 'SMTP', refuse)

        with caplog.at_level('INFO'):
            assert mailer.send_mail(['secret@cgiar.org'], 'Hi', 'body', background=False,
                                    log_ref={'user_id': 7}) is False
        logged = '\n'.join(str(r.msg) for r in caplog.records)
        assert 'secret@cgiar.org' not in logged
        assert 'SMTPRecipientsRefused' in logged and "'user_id': 7" in logged


class TestMailer:
    def test_unconfigured_mail_is_logged_not_sent(self, monkeypatch):
        from app.services import mailer
        monkeypatch.setenv('SMTP_HOST', '')
        assert mailer.send_mail(['a@b.org'], 'Hi', 'body') is False

    def test_configured_mail_builds_a_proper_message(self, monkeypatch):
        from app.services import mailer
        monkeypatch.setenv('SMTP_HOST', 'smtp.example.org')
        monkeypatch.setenv('MAIL_FROM', 'noreply@example.org')
        captured = []
        monkeypatch.setattr(mailer, '_deliver', lambda msg, ref: captured.append(msg) or True)
        assert mailer.send_mail(['a@b.org'], 'Hi', 'body', background=False) is True
        msg = captured[0]
        assert msg['To'] == 'a@b.org'
        assert 'noreply@example.org' in msg['From']
        assert msg.get_content().strip() == 'body'

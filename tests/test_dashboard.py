"""The browser landing page at `/` on the API.

Browsers (Accept: text/html) get a dashboard linking the web app, the API docs,
the OpenAPI spec and /health; API clients keep the JSON pointer / redirect
covered in test_security.TestNoServerRenderedPages.
"""
import pytest

from app.routes.dashboard import render_dashboard

BROWSER = {'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'}


@pytest.fixture
def client(monkeypatch):
    from app import create_app
    monkeypatch.delenv('SPA_ROOT_URL', raising=False)
    return create_app().test_client()


def test_browser_gets_the_dashboard(client):
    resp = client.get('/', headers=BROWSER)
    assert resp.status_code == 200
    assert resp.mimetype == 'text/html'
    body = resp.get_data(as_text=True)
    for link in ('href="/api-docs/swagger"', 'href="/api-docs/openapi.json"', 'href="/health"'):
        assert link in body


def test_dashboard_links_the_web_app_when_configured(client, monkeypatch):
    monkeypatch.setenv('SPA_ROOT_URL', 'https://kvuno.example.org/')
    body = client.get('/', headers=BROWSER).get_data(as_text=True)
    assert 'href="https://kvuno.example.org/"' in body


def test_dashboard_says_when_the_web_app_is_not_configured(client):
    body = client.get('/', headers=BROWSER).get_data(as_text=True)
    assert 'Set <code>SPA_ROOT_URL</code>' in body


def test_browser_is_no_longer_redirected_away(client, monkeypatch):
    """With SPA_ROOT_URL set, a browser used to bounce straight to the SPA."""
    monkeypatch.setenv('SPA_ROOT_URL', 'http://127.0.0.1:5001')
    assert client.get('/', headers=BROWSER).status_code == 200


def test_api_clients_still_get_json(client):
    resp = client.get('/', headers={'Accept': '*/*'})
    assert resp.is_json
    assert resp.get_json()['service'] == 'kvuno-api'


@pytest.mark.parametrize('bad', ['javascript:alert(1)', '//evil.example', 'data:text/html,x', 'ftp://x'])
def test_unsafe_spa_url_is_not_linked(bad):
    html = render_dashboard('KVuno API', '1.0', bad)
    assert bad not in html
    assert 'Not configured' in html


def test_values_are_escaped():
    html = render_dashboard('<b>x</b>', '"1.0"<', 'https://ok.example/?a=1&b="2"')
    assert '<b>x</b>' not in html
    assert '&lt;b&gt;x&lt;/b&gt;' in html
    assert 'href="https://ok.example/?a=1&amp;b=&quot;2&quot;"' in html

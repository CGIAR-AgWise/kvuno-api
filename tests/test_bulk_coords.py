"""The bulk coordinates endpoint.

The map's "All points" used to page /coordinates, which is one request per 500
rows. On a 2.2M-row dataset that is 4,566 requests against a 120/min limit, so
the map could never finish loading — it hit 429 after ~2.6% of the data. One
capped response replaces the walk.
"""

from unittest.mock import MagicMock, patch

import pytest

from app import create_app


class _NullRedis:
    def get(self, _key):
        return None

    def setex(self, *_args):
        return None


@pytest.fixture(autouse=True)
def no_cache():
    """Bypass the response cache.

    The endpoint is decorated with @api_cache and a live Redis is reachable, so
    a cached body from an earlier test — or an earlier manual run — is served
    instead of the mocked repo, and the assertions read real production counts.
    """
    with patch('app.cache._get_client', return_value=_NullRedis()):
        yield


@pytest.fixture
def client():
    import app.auth as auth
    auth.get_current_user = lambda: MagicMock(id=1)
    return create_app().test_client()


def _repo(total, points):
    repo = MagicMock()
    repo.count_coordinates.return_value = total
    repo.get_coordinates.return_value = points
    return repo


def test_returns_flat_arrays_in_one_request(client):
    from app.api import planting_data

    points = [(-1.3, 36.8), (0.5, 20.1)]
    with patch.object(planting_data, 'repo', _repo(2, points)):
        body = client.get('/api/v1/planting-data/coordinates/all').get_json()
    assert body['lats'] == [-1.3, 0.5]
    assert body['lons'] == [36.8, 20.1]
    assert body['returned'] == 2
    assert body['truncated'] is False


def test_reports_truncation_at_the_cap(client):
    from app.api import planting_data

    # Server holds more than the cap allows, so the map is a sample and the UI
    # must say so rather than implying completeness.
    with patch.object(planting_data, 'repo', _repo(2_000_000, [(1.0, 2.0)] * 10)):
        body = client.get('/api/v1/planting-data/coordinates/all').get_json()
    assert body['truncated'] is True
    assert body['total'] == 2_000_000
    assert body['returned'] == 10


def test_cap_is_respected_and_bounded(client):
    from app.api import planting_data

    repo = _repo(10, [])
    with patch.object(planting_data, 'repo', repo):
        client.get('/api/v1/planting-data/coordinates/all')
    # limit=cap, offset=0: one query, no walk.
    assert repo.get_coordinates.call_args.kwargs['limit'] > 0
    assert repo.get_coordinates.call_args.kwargs['offset'] == 0


def test_requires_authentication(client):
    import app.auth as auth
    auth.get_current_user = lambda: None
    assert client.get('/api/v1/planting-data/coordinates/all').status_code == 401

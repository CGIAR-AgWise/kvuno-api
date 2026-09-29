"""Regression tests for server-side pagination across the API.

The important one is ``/coordinates``: it used to end in ``query.all()`` with
no limit, so a large filtered dataset was materialised whole. These assert the
limit and offset actually reach the repository, not just that the response
*looks* paginated.
"""

from unittest.mock import MagicMock, patch

import pytest

from app import create_app


@pytest.fixture(autouse=True)
def no_cache():
    """Bypass the Redis response cache.

    The cached endpoints really do hit a live Redis, and the cache key is the
    full query string, so without this a cached page from an earlier run is
    served and the repository is never called — making these assertions pass or
    fail depending on what happens to be cached.
    """
    class _NullClient:
        def get(self, key):
            return None

        def setex(self, key, ttl, value):
            return None

    with patch("app.cache._get_client", return_value=_NullClient()):
        yield


@pytest.fixture
def client():
    app = create_app()
    with app.test_client() as c:
        yield c


@pytest.fixture
def authed():
    """Patch auth so protected endpoints are reachable without a real session."""
    with patch("app.auth.get_current_user", return_value=MagicMock(id=1)):
        yield


class TestCoordinatesPagination:
    def test_limit_and_offset_reach_the_repo(self, client, authed):
        repo = MagicMock()
        repo.count_coordinates.return_value = 1000
        repo.get_coordinates.return_value = [(1.0, 2.0)]
        with patch("app.api.planting_data.repo", repo):
            resp = client.get("/api/v1/planting-data/coordinates?page=3&per_page=100")
        assert resp.status_code == 200
        # Bounded server-side: the repo must receive a real limit and offset.
        repo.get_coordinates.assert_called_once()
        args = repo.get_coordinates.call_args.kwargs
        assert args["limit"] == 100
        assert args["offset"] == 200

    def test_default_page_size_is_100(self, client, authed):
        repo = MagicMock()
        repo.count_coordinates.return_value = 5
        repo.get_coordinates.return_value = []
        with patch("app.api.planting_data.repo", repo):
            resp = client.get("/api/v1/planting-data/coordinates")
        body = resp.get_json()
        assert body["per_page"] == 100
        assert body["current_page"] == 1
        assert repo.get_coordinates.call_args.kwargs["limit"] == 100

    def test_page_size_is_capped(self, client, authed):
        from app.dto.pagination import MAX_PER_PAGE
        repo = MagicMock()
        repo.count_coordinates.return_value = 5
        repo.get_coordinates.return_value = []
        with patch("app.api.planting_data.repo", repo):
            resp = client.get("/api/v1/planting-data/coordinates?per_page=99999")
        assert resp.get_json()["per_page"] == MAX_PER_PAGE
        assert repo.get_coordinates.call_args.kwargs["limit"] == MAX_PER_PAGE

    def test_envelope_reports_real_total(self, client, authed):
        """The count is separate, so a short page never misreports the total."""
        repo = MagicMock()
        repo.count_coordinates.return_value = 4321
        repo.get_coordinates.return_value = [(1.0, 2.0)]
        with patch("app.api.planting_data.repo", repo):
            resp = client.get("/api/v1/planting-data/coordinates?per_page=100")
        body = resp.get_json()
        assert body["total"] == 4321
        assert body["pages"] == 44
        assert body["coordinates"] == [{"lat": 1.0, "lon": 2.0}]

    def test_negative_page_does_not_produce_negative_offset(self, client, authed):
        repo = MagicMock()
        repo.count_coordinates.return_value = 5
        repo.get_coordinates.return_value = []
        with patch("app.api.planting_data.repo", repo):
            resp = client.get("/api/v1/planting-data/coordinates?page=-3")
        assert resp.status_code == 200
        assert repo.get_coordinates.call_args.kwargs["offset"] == 0


class TestFiltersPagination:
    def test_reports_per_column_totals(self, client, authed):
        repo = MagicMock()
        repo.get_distinct_values.return_value = {
            "country": ["Kenya"], "province": ["Nairobi"],
            "variety": [], "season_type": [],
        }
        repo.count_distinct_values.return_value = {
            "country": 250, "province": 47, "variety": 0, "season_type": 0,
        }
        with patch("app.api.planting_data.repo", repo):
            resp = client.get("/api/v1/planting-data/filters")
        body = resp.get_json()
        assert body["per_page"] == 100
        assert body["totals"]["country"] == 250
        # Each column pages independently, so pages differ per column.
        assert body["pages"]["country"] == 3
        assert body["pages"]["province"] == 1

    def test_slice_is_passed_per_column(self, client, authed):
        repo = MagicMock()
        repo.get_distinct_values.return_value = {}
        repo.count_distinct_values.return_value = {}
        with patch("app.api.planting_data.repo", repo):
            client.get("/api/v1/planting-data/filters?page=2&per_page=50")
        assert repo.get_distinct_values.call_args.kwargs == {"limit": 50, "offset": 50}


class TestClustersPagination:
    def test_clusters_are_limited(self, client, authed):
        repo = MagicMock()
        repo.get_clusters.return_value = [{"lat": 1.0, "lon": 2.0, "count": 10}]
        with patch("app.api.planting_data.repo", repo):
            resp = client.get("/api/v1/planting-data/clusters?zoom=5&page=2&per_page=25")
        assert resp.status_code == 200
        kwargs = repo.get_clusters.call_args.kwargs
        assert kwargs["limit"] == 25
        assert kwargs["offset"] == 25
        assert resp.get_json()["per_page"] == 25


class TestExportPagination:
    def test_export_slices_rows(self, client, authed):
        """Export used to stream every matching row; it is now capped."""
        repo = MagicMock()
        query = MagicMock()
        query.limit.return_value = query
        query.offset.return_value = query
        query.yield_per.return_value = []
        repo.get_filtered_data.return_value = query
        with patch("app.api.planting_data.repo", repo):
            resp = client.get("/api/v1/planting-data/export?format=json&per_page=200")
        assert resp.status_code == 200
        query.limit.assert_called_once_with(200)


class TestListEndpointsDefaultTo100:
    def test_planting_data_defaults_to_100(self, client):
        repo = MagicMock()
        page = MagicMock(items=[], total=0, pages=0, page=1, per_page=100)
        repo.get_paginated_data.return_value = page
        with patch("app.api.planting_data.repo", repo):
            resp = client.get("/api/v1/planting-data")
        assert resp.get_json()["per_page"] == 100
        args = repo.get_paginated_data.call_args.args
        assert args[1] == 1 and args[2] == 100

    def test_conflicts_defaults_to_100(self, client):
        repo = MagicMock()
        page = MagicMock(items=[], total=0, pages=0, page=1, per_page=100)
        repo.get_paginated.return_value = page
        with patch("app.api.quality.repo", repo):
            resp = client.get("/api/v1/quality/conflicts")
        assert resp.get_json()["per_page"] == 100
        args = repo.get_paginated.call_args.args
        assert args[1] == 1 and args[2] == 100

    def test_conflicts_page_size_is_capped(self, client):
        from app.dto.pagination import MAX_PER_PAGE
        repo = MagicMock()
        page = MagicMock(items=[], total=0, pages=0, page=1, per_page=MAX_PER_PAGE)
        repo.get_paginated.return_value = page
        with patch("app.api.quality.repo", repo):
            resp = client.get("/api/v1/quality/conflicts?per_page=100000")
        assert resp.get_json()["per_page"] == MAX_PER_PAGE

    def test_tokens_are_limited_and_ordered(self, client, authed):
        with (
            patch("app.api.user.MyDb.get_db") as get_db,
            patch("app.api.user.get_current_user", return_value=MagicMock(id=1)),
        ):
            session = MagicMock()
            query = MagicMock()
            query.filter.return_value = query
            query.count.return_value = 3
            query.order_by.return_value = query
            query.limit.return_value = query
            query.offset.return_value = query
            query.all.return_value = []
            session.query.return_value = query
            get_db.return_value = MagicMock(session=session)
            resp = client.get("/api/v1/users/tokens?per_page=100")
        body = resp.get_json()
        assert body["per_page"] == 100
        assert body["total"] == 3
        query.limit.assert_called_once_with(100)
        # Ordered, so paging is stable rather than arbitrary.
        query.order_by.assert_called_once()

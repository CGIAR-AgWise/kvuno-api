"""Regression tests for the planting-data / quality data-path fixes:
sort_col whitelisting, radius in metres, cache status handling, cache
invalidation, CSV zeros, cluster paging, and error-body hygiene.
"""

import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask
from pydantic import ValidationError

from app import create_app
from app.dto.data_filters import PlantingDataFilter, QualityFilter


@pytest.fixture(autouse=True)
def no_cache():
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
    with patch("app.auth.get_current_user", return_value=MagicMock(id=1)):
        yield


# ── sort_col / sort_dir validation ─────────────────────────────

class TestSortValidation:
    @pytest.mark.parametrize("col", ["metadata", "coordinates", "registry", "__table__", "nope"])
    def test_planting_filter_rejects_non_columns(self, col):
        with pytest.raises(ValidationError, match="sort_col must be one of"):
            PlantingDataFilter(sort_col=col)

    def test_planting_filter_accepts_real_columns(self):
        f = PlantingDataFilter(sort_col="country", sort_dir="DESC")
        assert f.sort_col == "country" and f.sort_dir == "desc"

    def test_empty_sort_is_none(self):
        f = PlantingDataFilter(sort_col="", sort_dir="")
        assert f.sort_col is None and f.sort_dir is None

    def test_sort_dir_rejects_garbage(self):
        with pytest.raises(ValidationError, match="sort_dir"):
            PlantingDataFilter(sort_dir="sideways")

    def test_quality_filter_rejects_json_and_non_columns(self):
        for col in ("record_data", "metadata"):
            with pytest.raises(ValidationError):
                QualityFilter(sort_col=col)
        assert QualityFilter(sort_col="source").sort_col == "source"

    def test_endpoint_returns_client_error_not_500(self, client):
        repo = MagicMock()
        with patch("app.api.planting_data.repo", repo):
            resp = client.get("/api/v1/planting-data?sort_col=metadata")
        assert 400 <= resp.status_code < 500
        repo.get_paginated_data.assert_not_called()

    def test_quality_endpoint_returns_client_error_not_500(self, client, authed):
        repo = MagicMock()
        with patch("app.api.quality.repo", repo):
            resp = client.get("/api/v1/quality/conflicts?sort_col=metadata")
        assert 400 <= resp.status_code < 500
        repo.get_paginated.assert_not_called()


def _sqlite_session():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    return Session(create_engine("sqlite://"))


class TestRepoBackstop:
    def test_unvalidated_bad_sort_col_falls_back_to_id(self):
        from app.repo.planting_recommendation import PlantingRecommendationRepo
        repo = PlantingRecommendationRepo()
        repo._get_session = _sqlite_session
        f = PlantingDataFilter.model_construct(sort_col="metadata", sort_dir="asc",
                                               coordinates=None, radius=None, country=None,
                                               province=None, variety=None, season_type=None,
                                               opt_date=None, planting_option=None)
        sql = str(repo.get_filtered_data(f).statement)
        assert "ORDER BY planting_recommendations.id" in sql

    def test_conflict_repo_backstop(self):
        from app.repo.import_conflict import ImportConflictRepo
        repo = ImportConflictRepo()
        repo._get_session = _sqlite_session
        f = QualityFilter.model_construct(sort_col="metadata", sort_dir="asc",
                                          country=None, source=None, search=None)
        sql = str(repo.get_filtered_query(f).statement)
        assert "ORDER BY import_conflicts.created_at" in sql


class TestRadiusIsMetres:
    def test_dwithin_runs_on_geography(self):
        from sqlalchemy.dialects import postgresql
        from app.repo.planting_recommendation import PlantingRecommendationRepo
        repo = PlantingRecommendationRepo()
        repo._get_session = _sqlite_session
        f = PlantingDataFilter(coordinates="36.82,-1.29", radius=50000)
        sql = str(repo._apply_filters(f).statement.compile(dialect=postgresql.dialect()))
        dwithin = sql[sql.index("ST_DWithin"):]
        # Both operands cast, so the third argument is metres, not degrees.
        assert dwithin.count("AS geography") == 2


# ── Cache ──────────────────────────────────────────────────────

class _RecordingClient:
    def __init__(self):
        self.set_keys = []
        self.scanned = []
        self.deleted = []

    def get(self, key):
        return None

    def setex(self, key, ttl, value):
        self.set_keys.append(key)

    def scan_iter(self, match, count):
        self.scanned.append(match)
        return iter([f"{match[:-1]}x"])

    def delete(self, key):
        self.deleted.append(key)


class TestCacheOnlySuccess:
    def _run(self, result):
        from app.cache import api_cache
        rec = _RecordingClient()

        @api_cache("t")
        def view():
            return result

        with patch("app.cache._get_client", return_value=rec), \
                Flask(__name__).test_request_context("/?a=1"):
            out = view()
        return out, rec

    def test_error_tuple_is_not_cached(self):
        out, rec = self._run(({"error": "db down"}, 500))
        assert out[1] == 500
        assert rec.set_keys == []

    def test_4xx_is_not_cached(self):
        _, rec = self._run(({"error": "bad"}, 400))
        assert rec.set_keys == []

    def test_success_tuple_is_cached(self):
        _, rec = self._run(({"ok": 1}, 200))
        assert len(rec.set_keys) == 1

    def test_plain_body_is_cached(self):
        _, rec = self._run({"ok": 1})
        assert len(rec.set_keys) == 1


class TestCacheInvalidation:
    def test_invalidation_covers_bulk_coordinates(self):
        from app.cache import invalidate_planting_data_cache
        rec = _RecordingClient()
        with patch("app.cache._get_client", return_value=rec):
            invalidate_planting_data_cache()
        assert "kvuno:coordinates_bulk:*" in rec.scanned

    def test_every_planting_data_cache_prefix_is_invalidated(self):
        """A new @api_cache prefix must be added to the invalidation list."""
        from app.cache import PLANTING_DATA_CACHE_PREFIXES
        src = (Path(__file__).resolve().parents[1] / "app" / "api" / "planting_data.py").read_text()
        used = set(re.findall(r"@api_cache\('([^']+)'", src))
        assert used, "no cached endpoints found"
        assert used <= set(PLANTING_DATA_CACHE_PREFIXES)

    def test_upload_uses_the_full_invalidation(self):
        import app.api.upload as upload
        from app.cache import invalidate_planting_data_cache
        assert upload.invalidate_planting_data_cache is invalidate_planting_data_cache


# ── planting_data endpoint fixes ───────────────────────────────

class TestExportZeros:
    def test_csv_keeps_zero_values(self, client, authed):
        row = SimpleNamespace(country="Kenya", province="X", lon=0.0, lat=0.0, variety="V",
                              season_type="Average", opt_date="20240101", planting_option=0, crop_type="maize")
        query = MagicMock()
        query.limit.return_value = query
        query.offset.return_value = query
        query.yield_per.return_value = [row]
        repo = MagicMock()
        repo.get_filtered_data.return_value = query
        with patch("app.api.planting_data.repo", repo):
            resp = client.get("/api/v1/planting-data/export")
        lines = resp.get_data(as_text=True).strip().splitlines()
        assert lines[1].split(",") == ["Kenya", "X", "0.0", "0.0", "V", "Average", "20240101", "0", "maize"]

    def test_csv_none_is_blank(self, client, authed):
        row = SimpleNamespace(country=None, province=None, lon=None, lat=None, variety=None,
                              season_type=None, opt_date=None, planting_option=None, crop_type=None)
        query = MagicMock()
        query.limit.return_value = query
        query.offset.return_value = query
        query.yield_per.return_value = [row]
        repo = MagicMock()
        repo.get_filtered_data.return_value = query
        with patch("app.api.planting_data.repo", repo):
            resp = client.get("/api/v1/planting-data/export")
        assert resp.get_data(as_text=True).strip().splitlines()[1] == ",,,,,,,,"


class TestClusters:
    def test_pages_come_from_cluster_count(self, client, authed):
        repo = MagicMock()
        # Two clusters on this page holding 10,000 points, 3 clusters overall.
        repo.get_clusters.return_value = [{"lat": 1.0, "lon": 2.0, "count": 9000},
                                          {"lat": 3.0, "lon": 4.0, "count": 1000}]
        repo.count_clusters.return_value = 3
        with patch("app.api.planting_data.repo", repo):
            body = client.get("/api/v1/planting-data/clusters?per_page=2").get_json()
        assert body["pages"] == 2
        assert body["total_clusters"] == 3
        assert body["total"] == 10000

    @pytest.mark.parametrize("qs", ["zoom=abc", "ne_lat=north", "sw_lng=", "ne_lat=91",
                                    "sw_lng=-181", "ne_lng=nan"])
    def test_bad_parameters_are_400(self, client, authed, qs):
        repo = MagicMock()
        with patch("app.api.planting_data.repo", repo):
            resp = client.get(f"/api/v1/planting-data/clusters?{qs}")
        assert resp.status_code == 400
        assert resp.get_json()["error"]
        repo.get_clusters.assert_not_called()


class TestErrorBodiesDoNotLeak:
    SECRET = "postgresql://kvuno:s3cr3t@db.internal/kvuno"

    def test_public_data_endpoint(self, client):
        repo = MagicMock()
        repo.get_paginated_data.side_effect = RuntimeError(self.SECRET)
        with patch("app.api.planting_data.repo", repo):
            resp = client.get("/api/v1/planting-data")
        assert resp.status_code == 500
        assert "s3cr3t" not in resp.get_data(as_text=True)

    def test_protected_endpoints(self, client, authed):
        repo = MagicMock()
        for attr in ("get_distinct_values", "count_coordinates", "get_clusters"):
            getattr(repo, attr).side_effect = RuntimeError(self.SECRET)
        repo.get_filtered_data.side_effect = RuntimeError(self.SECRET)
        with patch("app.api.planting_data.repo", repo):
            for path in ("filters", "coordinates", "coordinates/all", "clusters", "export"):
                resp = client.get(f"/api/v1/planting-data/{path}")
                assert resp.status_code == 500, path
                assert "s3cr3t" not in resp.get_data(as_text=True), path


# ── app/__init__.py ────────────────────────────────────────────

class TestCspAndFilters:
    def test_csp_has_no_cdn_hosts(self):
        from flask import Response
        app = create_app()
        with app.test_request_context("/"):
            resp = app.process_response(Response("x", mimetype="text/html"))
        csp = resp.headers["Content-Security-Policy"]
        assert "cdn.jsdelivr.net" not in csp
        assert "unpkg.com" not in csp
        assert "https://*.tile.openstreetmap.org" in csp

    def test_unused_datetime_filter_is_gone(self):
        app = create_app()
        assert "datetime" not in app.jinja_env.filters

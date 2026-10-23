import csv
import io
import json
import math
import os

from flask import request, Response, stream_with_context
from flask_openapi3 import Tag, APIBlueprint

from app.auth import require_auth
from app.cache import api_cache
from app.config import API_PREFIX, API_VERSION, RATE_LIMIT_COORDINATES_BULK, RATE_LIMIT_DATA
from app.dto.data_filters import PlantingDataFilter
from app.dto.pagination import get_pagination
from app.dto.planting_recommendation import PlantingRecommendationRecord, PlantingRecommendationResponse, Unauthorized
from app.rate_limit import limiter
from app.repo.planting_recommendation import PlantingRecommendationRepo
from app.utils.logging import get_logger
from pydantic import BaseModel, Field

__bp__ = "/planting-data"
url_prefix = API_PREFIX + API_VERSION + __bp__

tag = Tag(name="kvuno", description="Data serving API")

#api = APIBlueprint(__bp__, __name__, url_prefix=url_prefix, abp_tags=[tag], abp_security=[{"jwt": []}])

public_api = APIBlueprint(
    __bp__,
    __name__,
    url_prefix=url_prefix,
    abp_tags=[tag]
)

protected_api = APIBlueprint(
    f"{__bp__}-secure",
    __name__,
    url_prefix=url_prefix,
    abp_tags=[tag],
    abp_security=[{"jwt": []}]
)

logger = get_logger(__name__)

repo = PlantingRecommendationRepo()

EXPORT_COLUMNS = ['country', 'province', 'lon', 'lat', 'variety', 'season_type', 'opt_date', 'planting_option']


class CoordinatesResponse(BaseModel):
    coordinates: list[dict] = Field(default=[], description="Array of {lat, lon} objects")
    total: int = Field(0, description="Total matching coordinate pairs")
    pages: int = Field(0, description="Total number of pages")
    current_page: int = Field(1, description="Current page number")
    per_page: int = Field(100, description="Points per page")


class ClusterItem(BaseModel):
    lat: float = Field(..., description="Cluster center latitude")
    lon: float = Field(..., description="Cluster center longitude")
    count: int = Field(..., description="Number of points in cluster")


class ClustersResponse(BaseModel):
    clusters: list[ClusterItem] = Field(default=[], description="Spatially aggregated clusters")
    total: int = Field(0, description="Total points represented by the clusters on this page")
    total_clusters: int = Field(0, description="Total clusters across all pages")
    pages: int = Field(0, description="Total number of cluster pages")
    current_page: int = Field(1, description="Current page number")
    per_page: int = Field(100, description="Clusters per page")


class CompactCoordinatesResponse(BaseModel):
    lats: list[float] = Field(default=[], description="Flat array of latitudes, index-aligned with `lons`")
    lons: list[float] = Field(default=[], description="Flat array of longitudes, index-aligned with `lats`")
    total: int = Field(0, description="Total matching coordinate pairs")
    pages: int = Field(0, description="Total number of pages")
    current_page: int = Field(1, description="Current page number")
    per_page: int = Field(100, description="Points per page")


class BulkCoordinatesResponse(BaseModel):
    lats: list[float] = Field(default=[], description="Flat array of latitudes, index-aligned with `lons`")
    lons: list[float] = Field(default=[], description="Flat array of longitudes, index-aligned with `lats`")
    total: int = Field(0, description="Total matching coordinate pairs")
    returned: int = Field(0, description="Pairs actually returned, capped by COORDINATES_BULK_MAX_ROWS")
    truncated: bool = Field(0, description="True when the cap was hit, so the map is a sample")
    cap: int = Field(0, description="The cap that was applied")


class FiltersResponse(BaseModel):
    country: list[str] = Field(default=[], description="Distinct country values")
    province: list[str] = Field(default=[], description="Distinct province values")
    variety: list[str] = Field(default=[], description="Distinct variety values")
    season_type: list[str] = Field(default=[], description="Distinct season_type values")
    totals: dict[str, int] = Field(default={}, description="Distinct value count per column")
    pages: dict[str, int] = Field(default={}, description="Total pages per column")
    current_page: int = Field(1, description="Current page number")
    per_page: int = Field(100, description="Distinct values per column per page")


MAX_PER_PAGE = 500

# Hard ceiling for the single-response coordinates endpoint.
#
# Deliberately small. The map is a density impression, not a data dump, and a
# dense plot of hundreds of thousands of dots reads as a solid blob — 5,000
# points is enough to show the shape of the distribution and costs ~120 KB.
# The rows returned are a random sample of the whole filtered set, not the
# first N, so the sample is representative however the data was ingested.
COORDINATES_BULK_MAX_ROWS = int(os.getenv('COORDINATES_BULK_MAX_ROWS', '5000'))


@public_api.get('',
         responses={200: PlantingRecommendationResponse, 401: Unauthorized},
         summary="Query planting recommendation data",
         description="Return paginated planting recommendation records with optional filters (country, province, variety, season_type, date range, coordinates). Defaults to 100 records per page, capped at 500.",
         security=[])
@limiter.limit(RATE_LIMIT_DATA)
def get_data(query: PlantingDataFilter):
    pagination = get_pagination()

    try:
        paginated_data = repo.get_paginated_data(query, pagination.page, pagination.per_page)

        records = [PlantingRecommendationRecord(
            id=item.id,
            country=item.country,
            province=item.province,
            lon=item.lon,
            lat=item.lat,
            variety=item.variety,
            season_type=item.season_type,
            opt_date=item.opt_date,
            planting_option=item.planting_option,
            check_sum=item.check_sum,
            coordinates=str(item.coordinates),
        ) for item in paginated_data.items]

        return PlantingRecommendationResponse(
            data=records,
            total=paginated_data.total,
            pages=paginated_data.pages,
            current_page=paginated_data.page,
            per_page=paginated_data.per_page,
        ).model_dump(), 200

    except Exception:
        logger.exception("Error retrieving planting recommendation data")
        return {'error': 'Failed to retrieve planting data'}, 500


FILTER_COLUMNS = ['country', 'province', 'variety', 'season_type']


@protected_api.get('/filters',
         responses={200: FiltersResponse, 401: Unauthorized},
         summary="Get distinct filter values",
         description="Return distinct values for country, province, variety, and season_type columns for use in filter dropdowns. Each column is paginated independently; `totals` reports the distinct count per column so a client can keep paging until it has them all.")
@require_auth
@limiter.limit(RATE_LIMIT_DATA)
@api_cache('filters', ttl=300)
def get_filter_options():
    pagination = get_pagination()
    try:
        values = repo.get_distinct_values(
            FILTER_COLUMNS, limit=pagination.per_page, offset=pagination.offset
        )
        totals = repo.count_distinct_values(FILTER_COLUMNS)
        pages = {
            col: ((totals.get(col, 0) + pagination.per_page - 1) // pagination.per_page)
            for col in FILTER_COLUMNS
        }
        return {**values, 'totals': totals, 'pages': pages,
                'current_page': pagination.page, 'per_page': pagination.per_page}, 200
    except Exception:
        logger.exception("Error fetching filter options")
        return {'error': 'Failed to fetch filter options'}, 500


@protected_api.get('/coordinates',
         responses={200: CoordinatesResponse, 401: Unauthorized},
         summary="Get map coordinates for data points",
         description="Return an array of {lat, lon} objects matching the specified filters, paginated server-side. Defaults to 100 points per page, capped at 500. "
                     "Pass format=compact for two flat arrays (`lats`/`lons`) instead, which is about 40% smaller on the wire and lets a client fill typed arrays directly.")
@require_auth
@limiter.limit(RATE_LIMIT_DATA)
@api_cache('coordinates', ttl=120)
def get_coordinates(query: PlantingDataFilter):
    pagination = get_pagination()
    compact = request.args.get('format', '').lower() == 'compact'
    try:
        total = repo.count_coordinates(query)
        points = repo.get_coordinates(query, limit=pagination.per_page, offset=pagination.offset)
        if compact:
            # Two flat lists rather than a list of objects. ~40% less JSON, and
            # no intermediate object per point for the client to allocate and
            # discard before copying into a typed array.
            return {
                "lats": [lat for lat, _ in points],
                "lons": [lon for _, lon in points],
                **pagination.envelope(total),
            }, 200
        return {
            "coordinates": [{"lat": lat, "lon": lon} for lat, lon in points],
            **pagination.envelope(total),
        }, 200
    except Exception:
        logger.exception("Error retrieving coordinates")
        return {'error': 'Failed to retrieve coordinates'}, 500


@protected_api.get('/coordinates/all',
         responses={200: BulkCoordinatesResponse, 401: Unauthorized},
         summary="Get every matching coordinate pair in one response",
         description="Return all matching coordinate pairs as two flat arrays (`lats`/`lons`) in a SINGLE request, capped at COORDINATES_BULK_MAX_ROWS. "
                     "Paging /coordinates to reach the same data costs one request per 500 rows, which at 2M+ rows means thousands of round trips and exhausts the rate limit long before the map finishes. "
                     "`truncated` reports whether the cap was reached, so the UI can say the map is a sample rather than implying completeness.")
@require_auth
@limiter.limit(RATE_LIMIT_COORDINATES_BULK)
@api_cache('coordinates_bulk', ttl=120)
def get_coordinates_all(query: PlantingDataFilter):
    cap = max(1, min(COORDINATES_BULK_MAX_ROWS, MAX_PER_PAGE * 1000))
    try:
        total = repo.count_coordinates(query)
        # sample=True: a random subset rather than the first N by id, which
        # would be geographically clumped if data arrived grouped by region.
        points = repo.get_coordinates(query, limit=cap, offset=0, sample=True)
        return {
            "lats": [lat for lat, _ in points],
            "lons": [lon for _, lon in points],
            "total": total,
            "returned": len(points),
            "truncated": len(points) < total,
            "cap": cap,
        }, 200
    except Exception:
        logger.exception("Error retrieving bulk coordinates")
        return {'error': 'Failed to retrieve coordinates'}, 500


@protected_api.get('/clusters',
         responses={200: ClustersResponse, 401: Unauthorized},
         summary="Get spatially clustered data points",
         description="Aggregate data points into spatial clusters at the given zoom level for map rendering, paginated server-side. Densest clusters are returned first.")
@require_auth
@limiter.limit(RATE_LIMIT_DATA)
@api_cache('clusters', ttl=120)
def get_clusters(query: PlantingDataFilter):
    pagination = get_pagination()
    # Parsed outside the try below: a malformed value is the client's mistake
    # and gets a 400 naming the parameter, not a 500 from int()/float().
    try:
        zoom = int(request.args.get('zoom', 5))
    except (TypeError, ValueError):
        return {'error': "zoom must be an integer"}, 400
    bounds = {}
    for name, default, limit in (('ne_lat', 90, 90), ('ne_lng', 180, 180),
                                 ('sw_lat', -90, 90), ('sw_lng', -180, 180)):
        try:
            value = float(request.args.get(name, default))
        except (TypeError, ValueError):
            return {'error': f"{name} must be a number"}, 400
        if not math.isfinite(value) or abs(value) > limit:
            return {'error': f"{name} must be between -{limit} and {limit}"}, 400
        bounds[name] = value
    try:
        clusters = repo.get_clusters(query, zoom, bounds['ne_lat'], bounds['ne_lng'],
                                     bounds['sw_lat'], bounds['sw_lng'],
                                     limit=pagination.per_page, offset=pagination.offset)
        # `total` is points represented on this page, so it keeps its existing
        # meaning. `pages` is about clusters, though: it used to be derived
        # from the point total, which over-reported pages by orders of
        # magnitude (one cluster can hold thousands of points), so a client
        # paging until `pages` walked far past the last real cluster.
        cluster_total = repo.count_clusters(query, zoom, bounds['ne_lat'], bounds['ne_lng'],
                                            bounds['sw_lat'], bounds['sw_lng'])
        total = sum(c['count'] for c in clusters)
        return {"clusters": clusters, "total": total,
                "total_clusters": cluster_total,
                "current_page": pagination.page, "per_page": pagination.per_page,
                "pages": max(1, (cluster_total + pagination.per_page - 1) // pagination.per_page)}, 200
    except Exception:
        logger.exception("Error fetching clusters")
        return {'error': 'Failed to fetch clusters'}, 500


def _row_to_dict(row):
    return {col: getattr(row, col, None) for col in EXPORT_COLUMNS}


@protected_api.get('/export',
         responses={200: {"content": {"text/csv": {}, "application/json": {}}}, 401: Unauthorized},
         summary="Export filtered data",
         description="Download filtered planting recommendation data as CSV (default) or JSON. Paginated server-side like the other data endpoints: 100 rows by default, 500 maximum. To export a full dataset, request successive pages and concatenate them.")
@require_auth
@limiter.limit(RATE_LIMIT_DATA)
def export_data(query: PlantingDataFilter):
    fmt = request.args.get('format', 'csv')
    pagination = get_pagination()

    try:
        rows = repo.get_filtered_data(query).limit(pagination.per_page).offset(pagination.offset)

        if fmt == 'json':
            def generate_json():
                yield '[\n'
                first = True
                for batch in rows.yield_per(500):
                    d = _row_to_dict(batch)
                    line = json.dumps(d, default=str)
                    if not first:
                        yield ',\n'
                    yield line
                    first = False
                yield '\n]\n'

            return Response(
                stream_with_context(generate_json()),
                mimetype='application/json',
                headers={'Content-Disposition': 'attachment; filename=kvuno-export.json'},
            )

        def generate_csv():
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(EXPORT_COLUMNS)
            yield buf.getvalue()
            buf.seek(0)
            buf.truncate(0)
            for batch in rows.yield_per(500):
                # Explicit None check: `or ''` also blanked legitimate zeros
                # (planting_option 0, a coordinate on the equator/meridian).
                w.writerow(['' if (v := getattr(batch, col, None)) is None else v
                            for col in EXPORT_COLUMNS])
                yield buf.getvalue()
                buf.seek(0)
                buf.truncate(0)

        return Response(
            stream_with_context(generate_csv()),
            mimetype='text/csv',
            headers={'Content-Disposition': 'attachment; filename=kvuno-export.csv'},
        )

    except Exception:
        logger.exception("Error exporting data")
        return {'error': 'Failed to export data'}, 500

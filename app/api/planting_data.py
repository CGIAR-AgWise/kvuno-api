import csv
import io
import json

from flask import request, Response, stream_with_context
from flask_openapi3 import Tag, APIBlueprint

from app.auth import require_auth
from app.cache import api_cache
from app.config import API_PREFIX, API_VERSION, RATE_LIMIT_DATA
from app.dto.data_filters import PlantingDataFilter
from app.dto.pagination import get_pagination
from app.dto.planting_recommendation import PlantingRecommendationRecord, PlantingRecommendationResponse, Unauthorized
from app.rate_limit import limiter
from app.repo.planting_recommendation import PlantingRecommendationRepo
from app.utils.logging import SharedLogger
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

shared_logger = SharedLogger()
logger = shared_logger.get_logger()

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
    total: int = Field(0, description="Total points represented")
    pages: int = Field(0, description="Total number of cluster pages")
    current_page: int = Field(1, description="Current page number")
    per_page: int = Field(100, description="Clusters per page")


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

    except Exception as e:
        logger.error(f"Error retrieving planting recommendation data: {e}")
        return {'error': str(e)}, 500


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
    except Exception as e:
        logger.error(f"Error fetching filter options: {e}")
        return {'error': str(e)}, 500


@protected_api.get('/coordinates',
         responses={200: CoordinatesResponse, 401: Unauthorized},
         summary="Get map coordinates for data points",
         description="Return an array of {lat, lon} objects matching the specified filters, paginated server-side. Defaults to 100 points per page, capped at 500.")
@require_auth
@limiter.limit(RATE_LIMIT_DATA)
@api_cache('coordinates', ttl=120)
def get_coordinates(query: PlantingDataFilter):
    pagination = get_pagination()
    try:
        total = repo.count_coordinates(query)
        points = repo.get_coordinates(query, limit=pagination.per_page, offset=pagination.offset)
        return {
            "coordinates": [{"lat": lat, "lon": lon} for lat, lon in points],
            **pagination.envelope(total),
        }, 200
    except Exception as e:
        logger.error(f"Error retrieving coordinates: {e}")
        return {'error': str(e)}, 500


@protected_api.get('/clusters',
         responses={200: ClustersResponse, 401: Unauthorized},
         summary="Get spatially clustered data points",
         description="Aggregate data points into spatial clusters at the given zoom level for map rendering, paginated server-side. Densest clusters are returned first.")
@require_auth
@limiter.limit(RATE_LIMIT_DATA)
@api_cache('clusters', ttl=120)
def get_clusters(query: PlantingDataFilter):
    pagination = get_pagination()
    try:
        zoom = int(request.args.get('zoom', 5))
        ne_lat = float(request.args.get('ne_lat', 90))
        ne_lng = float(request.args.get('ne_lng', 180))
        sw_lat = float(request.args.get('sw_lat', -90))
        sw_lng = float(request.args.get('sw_lng', -180))

        clusters = repo.get_clusters(query, zoom, ne_lat, ne_lng, sw_lat, sw_lng,
                                     limit=pagination.per_page, offset=pagination.offset)
        # `total` is points represented, not cluster count, so it keeps its
        # existing meaning; the cluster page count describes this page only.
        total = sum(c['count'] for c in clusters)
        return {"clusters": clusters, "total": total,
                "current_page": pagination.page, "per_page": pagination.per_page,
                "pages": max(1, (total + pagination.per_page - 1) // pagination.per_page)}, 200
    except Exception as e:
        logger.error(f"Error fetching clusters: {e}")
        return {'error': str(e)}, 500


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
                w.writerow([getattr(batch, col, None) or '' for col in EXPORT_COLUMNS])
                yield buf.getvalue()
                buf.seek(0)
                buf.truncate(0)

        return Response(
            stream_with_context(generate_csv()),
            mimetype='text/csv',
            headers={'Content-Disposition': 'attachment; filename=kvuno-export.csv'},
        )

    except Exception as e:
        logger.error(f"Error exporting data: {e}")
        return {'error': str(e)}, 500

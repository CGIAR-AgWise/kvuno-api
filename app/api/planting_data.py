import csv
import io
import json

from flask import request, Response, stream_with_context
from flask_openapi3 import Tag, APIBlueprint

from app.auth import require_auth
from app.cache import api_cache
from app.config import API_PREFIX, API_VERSION, RATE_LIMIT_DATA
from app.dto.data_filters import PlantingDataFilter
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


class ClusterItem(BaseModel):
    lat: float = Field(..., description="Cluster center latitude")
    lon: float = Field(..., description="Cluster center longitude")
    count: int = Field(..., description="Number of points in cluster")


class ClustersResponse(BaseModel):
    clusters: list[ClusterItem] = Field(default=[], description="Spatially aggregated clusters")
    total: int = Field(0, description="Total points represented")


class FiltersResponse(BaseModel):
    country: list[str] = Field(default=[], description="Distinct country values")
    province: list[str] = Field(default=[], description="Distinct province values")
    variety: list[str] = Field(default=[], description="Distinct variety values")
    season_type: list[str] = Field(default=[], description="Distinct season_type values")


MAX_PER_PAGE = 500


def _clamp_per_page(value: int) -> int:
    return max(1, min(value, MAX_PER_PAGE))


@public_api.get('',
         responses={200: PlantingRecommendationResponse, 401: Unauthorized},
         summary="Query planting recommendation data",
         description="Return paginated planting recommendation records with optional filters (country, province, variety, season_type, date range, coordinates).",
         security=[])
@limiter.limit(RATE_LIMIT_DATA)
def get_data(query: PlantingDataFilter):
    page = max(1, int(request.args.get('page', default=1, type=int)))
    per_page = _clamp_per_page(int(request.args.get('per_page', default=50, type=int)))

    try:
        paginated_data = repo.get_paginated_data(query, page, per_page)

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
         description="Return distinct values for country, province, variety, and season_type columns for use in filter dropdowns.")
@require_auth
@limiter.limit(RATE_LIMIT_DATA)
@api_cache('filters', ttl=300)
def get_filter_options():
    try:
        values = repo.get_distinct_values(FILTER_COLUMNS)
        return values, 200
    except Exception as e:
        logger.error(f"Error fetching filter options: {e}")
        return {'error': str(e)}, 500


@protected_api.get('/coordinates',
         responses={200: CoordinatesResponse, 401: Unauthorized},
         summary="Get map coordinates for data points",
         description="Return an array of {lat, lon} objects matching the specified filters.")
@require_auth
@limiter.limit(RATE_LIMIT_DATA)
@api_cache('coordinates', ttl=120)
def get_coordinates(query: PlantingDataFilter):
    try:
        points = repo.get_coordinates(query)
        return {"coordinates": [{"lat": lat, "lon": lon} for lat, lon in points]}, 200
    except Exception as e:
        logger.error(f"Error retrieving coordinates: {e}")
        return {'error': str(e)}, 500


@protected_api.get('/clusters',
         responses={200: ClustersResponse, 401: Unauthorized},
         summary="Get spatially clustered data points",
         description="Aggregate data points into spatial clusters at the given zoom level for map rendering.")
@require_auth
@limiter.limit(RATE_LIMIT_DATA)
@api_cache('clusters', ttl=120)
def get_clusters(query: PlantingDataFilter):
    try:
        zoom = int(request.args.get('zoom', 5))
        ne_lat = float(request.args.get('ne_lat', 90))
        ne_lng = float(request.args.get('ne_lng', 180))
        sw_lat = float(request.args.get('sw_lat', -90))
        sw_lng = float(request.args.get('sw_lng', -180))

        clusters = repo.get_clusters(query, zoom, ne_lat, ne_lng, sw_lat, sw_lng)
        total = sum(c['count'] for c in clusters)
        return {"clusters": clusters, "total": total}, 200
    except Exception as e:
        logger.error(f"Error fetching clusters: {e}")
        return {'error': str(e)}, 500


def _row_to_dict(row):
    return {col: getattr(row, col, None) for col in EXPORT_COLUMNS}


@protected_api.get('/export',
         responses={200: {"content": {"text/csv": {}, "application/json": {}}}, 401: Unauthorized},
         summary="Export filtered data",
         description="Download filtered planting recommendation data as CSV (default) or JSON.")
@require_auth
@limiter.limit(RATE_LIMIT_DATA)
def export_data(query: PlantingDataFilter):
    fmt = request.args.get('format', 'csv')

    try:
        rows = repo.get_filtered_data(query)

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

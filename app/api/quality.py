from flask import request
from flask_openapi3 import Tag, APIBlueprint

from app.config import API_PREFIX, API_VERSION
from app.dto.data_filters import QualityFilter
from app.dto.import_conflict import ImportConflictRecord
from app.dto.pagination import PaginatedResponse
from app.dto.planting_recommendation import Unauthorized
from app.repo.import_conflict import ImportConflictRepo
from app.utils.logging import SharedLogger
from pydantic import BaseModel, Field

__bp__ = "/quality"
url_prefix = API_PREFIX + API_VERSION + __bp__

tag = Tag(name="quality", description="Data quality endpoints")

api = APIBlueprint(__bp__, __name__, url_prefix=url_prefix, abp_tags=[tag], abp_security=[{"jwt": []}])

shared_logger = SharedLogger()
logger = shared_logger.get_logger()

repo = ImportConflictRepo()


class QualityStatsResponse(BaseModel):
    total_records: int = Field(..., description="Total planting recommendation records")
    total_conflicts: int = Field(..., description="Total import conflicts logged")
    total_files: int = Field(..., description="Total files imported")
    coverage_pct: int = Field(..., description="Percentage of records with valid coordinates")
    conflicts_by_country: list[dict] = Field(default=[], description="Conflicts grouped by country")
    conflicts_by_source: list[dict] = Field(default=[], description="Conflicts grouped by source")


class ConflictListResponse(PaginatedResponse[ImportConflictRecord]):
    pass


@api.get('/stats',
         responses={200: QualityStatsResponse, 401: Unauthorized},
         summary="Get data quality statistics",
         description="Return aggregate quality metrics including total records, conflicts, file count, coordinate coverage, and breakdowns by country and source.")
def get_quality_stats():
    try:
        stats = repo.get_stats()
        return stats, 200
    except Exception as e:
        logger.error(f"Error fetching quality stats: {e}")
        return {'error': str(e)}, 500


@api.get('/conflicts',
         responses={200: ConflictListResponse, 401: Unauthorized},
         summary="List import conflicts",
         description="Return paginated list of import conflicts with optional filtering.")
def get_conflicts(query: QualityFilter):
    page = int(request.args.get('page', default=1, type=int))
    per_page = int(request.args.get('per_page', default=50, type=int))

    try:
        paginated = repo.get_paginated(query, page, per_page)

        records = [ImportConflictRecord(
            id=item.id,
            country=item.country,
            province=item.province,
            lon=item.lon,
            lat=item.lat,
            variety=item.variety,
            season_type=item.season_type,
            opt_date=item.opt_date,
            check_sum=item.check_sum,
            source=item.source,
            record_data=item.record_data,
            created_at=item.created_at,
        ) for item in paginated.items]

        return {
            "data": [r.model_dump(mode='json') for r in records],
            "total": paginated.total,
            "pages": paginated.pages,
            "current_page": paginated.page,
            "per_page": paginated.per_page,
        }, 200

    except Exception as e:
        logger.error(f"Error fetching conflicts: {e}")
        return {'error': str(e)}, 500

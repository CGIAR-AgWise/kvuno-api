from typing import Optional

from flask_openapi3 import Tag, APIBlueprint

from app.auth import require_auth
from app.config import API_PREFIX, API_VERSION
from app.dto.data_filters import QualityFilter
from app.dto.import_conflict import ImportConflictRecord
from app.dto.pagination import PaginatedResponse, get_pagination
from app.dto.planting_recommendation import Unauthorized
from app.repo.import_conflict import ImportConflictRepo
from app.utils.logging import get_logger
from pydantic import BaseModel, Field

__bp__ = "/quality"
url_prefix = API_PREFIX + API_VERSION + __bp__

tag = Tag(name="quality", description="Data quality endpoints")

# NOTE: abp_security only labels the endpoints in the OpenAPI document; it does
# not enforce anything. Each view below carries @require_auth for that — the
# blueprint used to rely on the label alone, so these were public.
api = APIBlueprint(__bp__, __name__, url_prefix=url_prefix, abp_tags=[tag], abp_security=[{"jwt": []}])

logger = get_logger(__name__)

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


class DuplicateGroup(BaseModel):
    country: Optional[str] = None
    province: Optional[str] = None
    variety: Optional[str] = None
    season_type: Optional[str] = None
    opt_date: Optional[str] = None
    count: int = Field(..., description="Conflicts with this country/province/variety/season/date")
    sources: dict[str, int] = Field(default={}, description="Conflicts per source file")


class DuplicateListResponse(PaginatedResponse[DuplicateGroup]):
    pass


@api.get('/stats',
         responses={200: QualityStatsResponse, 401: Unauthorized},
         summary="Get data quality statistics",
         description="Return aggregate quality metrics including total records, conflicts, file count, coordinate coverage, and breakdowns by country and source.")
@require_auth
def get_quality_stats():
    try:
        stats = repo.get_stats()
        return stats, 200
    except Exception:
        # Exception text can carry SQL and connection details; log it, do not
        # return it.
        logger.exception("Error fetching quality stats")
        return {'error': 'Failed to fetch quality statistics'}, 500


@api.get('/conflicts',
         responses={200: ConflictListResponse, 401: Unauthorized},
         summary="List import conflicts",
         description="Return paginated list of import conflicts with optional filtering. Defaults to 100 records per page, capped at 500.")
@require_auth
def get_conflicts(query: QualityFilter):
    pagination = get_pagination()

    try:
        paginated = repo.get_paginated(query, pagination.page, pagination.per_page)

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

    except Exception:
        logger.exception("Error fetching conflicts")
        return {'error': 'Failed to fetch import conflicts'}, 500


@api.get('/duplicates',
         responses={200: DuplicateListResponse, 401: Unauthorized},
         summary="List import conflicts grouped as duplicates",
         description="Conflicts grouped by country, province, variety, season and date, most repeated first, "
                     "with a count per source. Paginated by group (default 100, max 500); takes the same "
                     "country/source/search filters as /conflicts.")
@require_auth
def get_duplicates(query: QualityFilter):
    pagination = get_pagination()
    try:
        total, groups = repo.get_duplicate_groups(query, pagination.offset, pagination.per_page)
        return {
            "data": [DuplicateGroup(**g).model_dump(mode='json') for g in groups],
            **pagination.envelope(total),
        }, 200
    except Exception:
        logger.exception("Error fetching duplicate groups")
        return {'error': 'Failed to fetch duplicates'}, 500

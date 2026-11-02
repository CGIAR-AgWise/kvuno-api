from typing import Generic, List, Optional, TypeVar

from flask import request
from pydantic import BaseModel, Field, field_validator

T = TypeVar("T")

DEFAULT_PER_PAGE = 100
MAX_PER_PAGE = 500


class PaginationQuery(BaseModel):
    """``page``/``per_page`` as query parameters, so the OpenAPI spec (and
    Swagger UI) shows them on every paginated endpoint.

    Documentation only: get_pagination() still reads and clamps them, which is
    why there are no ge/le bounds here — out-of-range values are clamped, not
    rejected, and an unparsable one falls back to the default as it always did.
    """

    # json_schema_extra, not examples=[...]: flask-openapi3 turns query fields
    # into Parameter objects, whose `examples` must be a dict.
    page: Optional[int] = Field(default=None, description="1-based page number; default 1, values below 1 mean 1",
                                json_schema_extra={"example": 1})
    per_page: Optional[int] = Field(
        default=None, json_schema_extra={"example": DEFAULT_PER_PAGE},
        description=f"Items per page; default {DEFAULT_PER_PAGE}, clamped to 1..{MAX_PER_PAGE}")

    @field_validator('page', 'per_page', mode='before')
    @classmethod
    def _lenient(cls, value):
        try:
            return int(value) if value not in (None, '') else None
        except (TypeError, ValueError):
            return None


class PaginatedResponse(BaseModel, Generic[T]):
    data: List[T] = Field(default=[], description="List of records")
    total: int = Field(..., description="Total number of records", examples=[150])
    pages: int = Field(..., description="Total number of pages", examples=[3])
    current_page: int = Field(..., description="Current page number", examples=[1])
    per_page: int = Field(..., description="Records per page", examples=[50])


class Pagination(BaseModel):
    """Resolved page/per_page pair, clamped to a safe range.

    ``per_page`` is clamped rather than rejected so a client asking for a huge
    page gets a bounded response instead of a 4xx it cannot act on. ``page`` is
    floored at 1 so a nonsense value cannot produce a negative OFFSET.
    """

    page: int = Field(..., ge=1, description="1-based page number")
    per_page: int = Field(..., ge=1, description="Page size, clamped to MAX_PER_PAGE")

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.per_page

    def envelope(self, total: int) -> dict:
        """Build the standard pagination metadata block for a total count."""
        pages = (total + self.per_page - 1) // self.per_page if self.per_page else 0
        return {
            "total": total,
            "pages": pages,
            "current_page": self.page,
            "per_page": self.per_page,
        }


def get_pagination() -> Pagination:
    """Read ``page``/``per_page`` from the query string, defaulting to 100.

    Every collection endpoint funnels through this so the default and the
    ceiling are defined in exactly one place.
    """
    page = max(1, request.args.get("page", default=1, type=int) or 1)
    per_page = request.args.get("per_page", default=DEFAULT_PER_PAGE, type=int)
    if per_page is None:
        per_page = DEFAULT_PER_PAGE
    per_page = max(1, min(per_page, MAX_PER_PAGE))
    return Pagination(page=page, per_page=per_page)

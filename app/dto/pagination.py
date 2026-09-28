from typing import Generic, List, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class PaginatedResponse(BaseModel, Generic[T]):
    data: List[T] = Field(default=[], description="List of records")
    total: int = Field(..., description="Total number of records", examples=[150])
    pages: int = Field(..., description="Total number of pages", examples=[3])
    current_page: int = Field(..., description="Current page number", examples=[1])
    per_page: int = Field(..., description="Records per page", examples=[50])

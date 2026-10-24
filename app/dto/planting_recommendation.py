from datetime import datetime
from typing import Literal, Optional

from pydantic import Field, BaseModel

from app.dto.pagination import PaginatedResponse


class Unauthorized(BaseModel):
    code: int = Field(-1, description="Status Code", examples=[-1])
    message: str = Field("Unauthorized!", description="Exception Information", examples=["Unauthorized!"])


class PlantingRecommendationBase(BaseModel):
    lat: Optional[float] = Field(default=None, description="Latitude", examples=[-1.29])
    lon: Optional[float] = Field(default=None, description="Longitude", examples=[36.82])
    country: Optional[str] = Field(default=None, description="Country", examples=["Kenya"])
    province: Optional[str] = Field(default=None, description="Province", examples=["Nairobi"])
    variety: Optional[str] = Field(default=None, description="Crop variety", examples=["H614"])
    season_type: Optional[str] = Field(default=None, description="Season type", examples=["Average"])
    opt_date: Optional[str] = Field(default=None, description="Optimal sowing date as day-month, no year (as in the source data)", examples=["30-Nov"])
    planting_option: Optional[int] = Field(default=None, description="Planting option", examples=[1])
    check_sum: Optional[str] = Field(default=None, description="File checksum", examples=["a1b2c3d4e5f6..."])
    coordinates: Optional[str] = Field(default=None, description="WKT coordinate string", examples=["POINT(36.82 -1.29)"])
    created_at: Optional[datetime] = Field(default=None, description="Record creation timestamp", examples=["2023-08-20T12:34:56Z"])
    updated_at: Optional[datetime] = Field(default=None, description="Record update timestamp", examples=["2023-08-20T12:34:56Z"])


class PlantingRecommendationCreate(PlantingRecommendationBase):
    pass


class GeoJSONPoint(BaseModel):
    """RFC 7946 Point. Coordinates are [longitude, latitude], in that order."""
    type: Literal['Point'] = 'Point'
    coordinates: list[float] = Field(..., description="[lon, lat]", examples=[[30.025, -14.475]])


class PlantingRecommendationRecord(PlantingRecommendationBase):
    id: int = Field(..., description="Record ID", examples=[1])
    # The response carries GeoJSON; Base keeps the WKT string ingestion uses.
    coordinates: Optional[GeoJSONPoint] = Field(
        default=None, description="Location as a GeoJSON Point (RFC 7946); same position as lon/lat")
    opt_month_day: Optional[str] = Field(
        default=None,
        description="opt_date as an ISO 8601 month-day ('--MM-DD'). The data has no year: the optimal "
                    "sowing date recurs every season.",
        examples=["--11-30"])


class PlantingRecommendationResponse(PaginatedResponse[PlantingRecommendationRecord]):
    pass

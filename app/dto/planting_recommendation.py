from typing import Optional

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
    opt_date: Optional[str] = Field(default=None, description="Optimal planting date", examples=["2024-10-15"])
    planting_option: Optional[int] = Field(default=None, description="Planting option", examples=[1])
    check_sum: Optional[str] = Field(default=None, description="File checksum", examples=["a1b2c3d4e5f6..."])
    coordinates: Optional[str] = Field(default=None, description="WKT coordinate string", examples=["POINT(36.82 -1.29)"])


class PlantingRecommendationCreate(PlantingRecommendationBase):
    pass


class PlantingRecommendationRecord(PlantingRecommendationBase):
    id: int = Field(..., description="Record ID", examples=[1])


class PlantingRecommendationResponse(PaginatedResponse[PlantingRecommendationRecord]):
    pass

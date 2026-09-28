from typing import Any, Optional, Dict
import datetime

from pydantic import BaseModel, Field


class ImportConflictBase(BaseModel):
    record_data: Dict[str, Any] = Field(..., description="Full JSON payload of the conflicting record")
    country: Optional[str] = Field(default=None, description="Country", examples=["Kenya"])
    province: Optional[str] = Field(default=None, description="Province", examples=["Nairobi"])
    lon: Optional[float] = Field(default=None, description="Longitude", examples=[36.82])
    lat: Optional[float] = Field(default=None, description="Latitude", examples=[-1.29])
    variety: Optional[str] = Field(default=None, description="Crop variety", examples=["H614"])
    season_type: Optional[str] = Field(default=None, description="Season type", examples=["Average"])
    opt_date: Optional[str] = Field(default=None, description="Optimal planting date", examples=["2024-10-15"])
    check_sum: Optional[str] = Field(default=None, description="Source file checksum", examples=["a1b2c3d4e5f6..."])
    source: Optional[str] = Field(default=None, description="Origin of the conflict", examples=["batch_insert"])
    created_at: Optional[datetime.datetime] = Field(default=None, description="When the conflict was logged")


class ImportConflictCreate(ImportConflictBase):
    pass


class ImportConflictRecord(ImportConflictBase):
    id: int = Field(..., description="Record ID", examples=[1])

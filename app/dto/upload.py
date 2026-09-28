from typing import List, Optional

from pydantic import BaseModel, Field


class UploadResponse(BaseModel):
    message: str = Field(..., description="Upload result message", examples=["File accepted for processing"])
    file: str = Field(..., description="Unique filename assigned to the uploaded file", examples=["a1b2c3d4e5f6.parquet"])
    columns: Optional[List[str]] = Field(default=None, description="Column names in the uploaded file", examples=[["country", "province", "lat", "lon"]])

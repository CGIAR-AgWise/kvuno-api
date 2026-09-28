from typing import Optional
import datetime

from pydantic import BaseModel, Field


class FileImportBase(BaseModel):
    check_sum: str = Field(..., description="File checksum", examples=["a1b2c3d4e5f6..."])
    file_name: str = Field(..., description="UUID filename on disk", examples=["abc123.parquet"])
    original_filename: Optional[str] = Field(default=None, description="Original uploaded filename", examples=["data.parquet"])
    processed_at: Optional[datetime.datetime] = Field(default=None, description="When the file was processed")
    offset: Optional[int] = Field(default=None, description="Last processed row offset")


class FileImportCreate(FileImportBase):
    pass


class FileImportRecord(FileImportBase):
    id: int = Field(..., description="Record ID", examples=[1])

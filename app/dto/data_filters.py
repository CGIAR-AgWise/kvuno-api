import re
from typing import Optional

from pydantic import BaseModel, Field, ConfigDict, field_validator


class PlantingDataFilter(BaseModel):
    coordinates: Optional[str] = Field(
        default=None,
        description='Coordinates in lon,lat format',
        json_schema_extra={"example": "36.82,-1.29"},
    )
    radius: Optional[float] = Field(
        default=None,
        description='Radius to search from defined coordinates in meters',
        json_schema_extra={"example": 50000},
    )
    country: Optional[str] = Field(
        default=None,
        description='Country where the crop is located',
        json_schema_extra={"example": "Kenya"},
    )
    province: Optional[str] = Field(
        default=None,
        description='Province where the crop is located',
        json_schema_extra={"example": "Nairobi"},
    )
    variety: Optional[str] = Field(
        default=None,
        description='Crop variety',
        json_schema_extra={"example": "H614"},
    )
    season_type: Optional[str] = Field(
        default=None,
        description='Type of season, e.g., Average, High',
        json_schema_extra={"example": "Average"},
    )
    opt_date: Optional[str] = Field(
        default=None,
        description='Optional date in YYYY-MM-DD format',
        json_schema_extra={"example": "2024-03-15"},
    )
    planting_option: Optional[int] = Field(
        default=None,
        description='Option for planting, typically an integer',
        json_schema_extra={"example": 1},
    )
    sort_col: Optional[str] = Field(
        default=None,
        description='Column to sort by',
        json_schema_extra={"example": "country"},
    )
    sort_dir: Optional[str] = Field(
        default=None,
        description='Sort direction: asc or desc',
        json_schema_extra={"example": "asc"},
    )

    model_config = ConfigDict(
        use_enum_values=True,
        str_strip_whitespace=True
    )

    @field_validator('coordinates', mode='before')
    @classmethod
    def validate_coordinates(cls, value):
        if value:
            pattern = re.compile(r"^-?\d+(\.\d+)?,-?\d+(\.\d+)?$")
            if not pattern.match(value):
                raise ValueError("Coordinates must be in 'lon,lat' format.")
            lon_str, lat_str = value.split(',')
            try:
                lon = float(lon_str)
                lat = float(lat_str)
            except ValueError:
                raise ValueError("Coordinates must contain valid float numbers.")
            if not (-90 <= lat <= 90):
                raise ValueError("Latitude must be between -90 and 90.")
            if not (-180 <= lon <= 180):
                raise ValueError("Longitude must be between -180 and 180.")
        return value

    @field_validator('opt_date', mode='before')
    @classmethod
    def validate_opt_date(cls, value):
        if value:
            pattern = re.compile(r"^\d{4}-\d{2}-\d{2}$")
            if not pattern.match(value):
                raise ValueError("Date must be in 'YYYY-MM-DD' format.")
        return value


class QualityFilter(BaseModel):
    country: Optional[str] = Field(default=None)
    source: Optional[str] = Field(default=None)
    search: Optional[str] = Field(default=None)
    sort_col: Optional[str] = Field(default=None)
    sort_dir: Optional[str] = Field(default=None)

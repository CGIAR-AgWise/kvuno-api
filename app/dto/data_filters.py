import re
from typing import Optional

from pydantic import Field, ConfigDict, field_validator

from app.dto.pagination import PaginationQuery


# Columns a client may sort by. sort_col used to go straight into
# getattr(Model, sort_col), so any model attribute was reachable: `metadata`
# (the SQLAlchemy MetaData object) or a relationship has no .asc(), and the
# request died with a 500. Only real, scalar table columns are listed — the
# geometry and JSON columns are left out because ordering by them is either
# meaningless or unsupported by the database.
PLANTING_SORT_COLUMNS = (
    'id', 'check_sum', 'country', 'province', 'lon', 'lat', 'variety',
    'season_type', 'opt_date', 'planting_option', 'crop_type', 'created_at', 'updated_at',
)
CONFLICT_SORT_COLUMNS = (
    'id', 'country', 'province', 'lon', 'lat', 'variety', 'season_type',
    'opt_date', 'crop_type', 'check_sum', 'source', 'created_at',
)
SORT_DIRECTIONS = ('asc', 'desc')


def _check_sort_col(value, allowed: tuple[str, ...]):
    if value is None or not str(value).strip():
        return None
    value = str(value).strip()
    if value not in allowed:
        raise ValueError(f"sort_col must be one of: {', '.join(allowed)}")
    return value


def _check_sort_dir(value):
    if value is None or not str(value).strip():
        return None
    value = str(value).strip().lower()
    if value not in SORT_DIRECTIONS:
        raise ValueError("sort_dir must be 'asc' or 'desc'")
    return value


class PlantingDataFilter(PaginationQuery):
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
    crop_type: Optional[str] = Field(
        default=None,
        description='Crop, case-insensitive',
        json_schema_extra={"example": "maize"},
    )
    opt_date: Optional[str] = Field(
        default=None,
        description="Optimal sowing day of the year (the data has no year): '30-Nov', ISO month-day "
                    "'--11-30', or a full date '2026-11-30' whose year is ignored",
        json_schema_extra={"example": "30-Nov"},
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

    @field_validator('crop_type', mode='before')
    @classmethod
    def validate_crop_type(cls, value):
        # Stored lower-case, so 'Maize' must still match.
        from app.utils.crop_type import normalise
        return normalise(value)

    @field_validator('opt_date', mode='before')
    @classmethod
    def validate_opt_date(cls, value):
        # Stored values are day-month ('30-Nov'). This used to demand
        # YYYY-MM-DD, which no stored value is, so the filter matched nothing
        # and the Explore page's Date dropdown got a 422.
        if value:
            from app.utils.opt_date import to_stored
            return to_stored(value)
        return value

    @field_validator('sort_col', mode='before')
    @classmethod
    def validate_sort_col(cls, value):
        return _check_sort_col(value, PLANTING_SORT_COLUMNS)

    @field_validator('sort_dir', mode='before')
    @classmethod
    def validate_sort_dir(cls, value):
        return _check_sort_dir(value)


class QualityFilter(PaginationQuery):
    country: Optional[str] = Field(default=None)
    crop_type: Optional[str] = Field(default=None, description='Crop, case-insensitive',
                                     json_schema_extra={"example": "maize"})
    source: Optional[str] = Field(default=None)
    search: Optional[str] = Field(default=None)
    sort_col: Optional[str] = Field(default=None, description='Column to sort by')
    sort_dir: Optional[str] = Field(default=None, description='Sort direction: asc or desc')

    @field_validator('crop_type', mode='before')
    @classmethod
    def validate_crop_type(cls, value):
        from app.utils.crop_type import normalise
        return normalise(value)

    @field_validator('sort_col', mode='before')
    @classmethod
    def validate_sort_col(cls, value):
        return _check_sort_col(value, CONFLICT_SORT_COLUMNS)

    @field_validator('sort_dir', mode='before')
    @classmethod
    def validate_sort_dir(cls, value):
        return _check_sort_dir(value)

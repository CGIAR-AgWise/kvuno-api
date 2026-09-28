import pytest
from pydantic import ValidationError

from app.dto.data_filters import PlantingDataFilter


class TestPlantingDataFilter:
    def test_empty_filter_is_valid(self):
        f = PlantingDataFilter()
        assert f.coordinates is None
        assert f.radius is None
        assert f.country is None

    def test_valid_coordinates(self):
        f = PlantingDataFilter(coordinates="25.92,-17.85")
        assert f.coordinates == "25.92,-17.85"

    def test_invalid_coordinates_format(self):
        with pytest.raises(ValidationError, match="lon,lat"):
            PlantingDataFilter(coordinates="invalid")

    def test_coordinates_lat_out_of_range(self):
        with pytest.raises(ValidationError, match="Latitude"):
            PlantingDataFilter(coordinates="0,100")

    def test_coordinates_lon_out_of_range(self):
        with pytest.raises(ValidationError, match="Longitude"):
            PlantingDataFilter(coordinates="200,0")

    def test_valid_opt_date(self):
        f = PlantingDataFilter(opt_date="2024-11-15")
        assert f.opt_date == "2024-11-15"

    def test_invalid_opt_date_format(self):
        with pytest.raises(ValidationError, match="Date"):
            PlantingDataFilter(opt_date="15-11-2024")

    def test_all_fields(self):
        f = PlantingDataFilter(
            coordinates="25.92,-17.85",
            radius=50000,
            country="Zambia",
            province="Southern",
            variety="Soybean",
            season_type="Main",
            opt_date="2024-11-15",
            planting_option=1,
        )
        assert f.radius == 50000
        assert f.country == "Zambia"
        assert f.province == "Southern"
        assert f.variety == "Soybean"
        assert f.season_type == "Main"
        assert f.planting_option == 1

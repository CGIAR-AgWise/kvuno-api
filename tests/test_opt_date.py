"""opt_date is a day of the year ('30-Nov'); the source data has no year."""
import pytest
from pydantic import ValidationError

from app.dto.data_filters import PlantingDataFilter
from app.utils.opt_date import month_day, to_stored


@pytest.mark.parametrize('stored,iso', [
    ('30-Nov', '--11-30'), ('02-Nov', '--11-02'), ('05-Jan', '--01-05'), ('29-Feb', '--02-29'),
])
def test_month_day_is_iso_8601(stored, iso):
    assert month_day(stored) == iso


@pytest.mark.parametrize('bad', [None, '', 'Nov-30', '32-Jan', '30-Feb', '30-Foo', '2026-11-30'])
def test_month_day_of_anything_else_is_none(bad):
    assert month_day(bad) is None


@pytest.mark.parametrize('given', ['30-Nov', '30-nov', '--11-30', '11-30', '2026-11-30'])
def test_filter_accepts_every_form_and_queries_the_stored_one(given):
    assert PlantingDataFilter(opt_date=given).opt_date == '30-Nov'


def test_single_digit_day_is_padded_like_the_data():
    assert to_stored('5-Jan') == '05-Jan'


@pytest.mark.parametrize('bad', ['15-11-2024', '31-Nov', 'soon', '2026-13-01'])
def test_filter_rejects_what_is_not_a_day_of_the_year(bad):
    with pytest.raises(ValidationError):
        PlantingDataFilter(opt_date=bad)

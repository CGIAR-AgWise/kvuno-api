"""Dates in API responses are ISO 8601 UTC, not Flask's HTTP date.

`created_at` on planting recommendations came back as
"Thu, 22 Oct 2026 07:59:49 GMT" (Flask's default for datetime), which clients
cannot sort or parse reliably.
"""
import datetime
import json

from app.utils.json_encoding import iso_utc, json_default

UTC = datetime.timezone.utc


def test_naive_datetime_is_taken_as_utc():
    assert iso_utc(datetime.datetime(2026, 10, 22, 7, 59, 49)) == '2026-10-22T07:59:49Z'


def test_aware_datetime_is_converted_to_utc():
    nairobi = datetime.timezone(datetime.timedelta(hours=3))
    value = datetime.datetime(2026, 10, 22, 10, 59, 49, tzinfo=nairobi)
    assert iso_utc(value) == '2026-10-22T07:59:49Z'


def test_fractional_seconds_are_kept():
    value = datetime.datetime(2026, 10, 22, 7, 59, 49, 125000, tzinfo=UTC)
    assert iso_utc(value) == '2026-10-22T07:59:49.125000Z'


def test_dates_and_the_cache_encoder():
    body = {'created_at': datetime.datetime(2026, 10, 22, 7, 59, 49), 'day': datetime.date(2026, 10, 22)}
    assert json.loads(json.dumps(body, default=json_default)) == {
        'created_at': '2026-10-22T07:59:49Z', 'day': '2026-10-22'}


def test_app_responses_use_iso_dates():
    from app import create_app

    app = create_app()

    @app.route('/__test_dates')
    def dates():
        return {'created_at': datetime.datetime(2026, 10, 22, 7, 59, 49)}

    body = app.test_client().get('/__test_dates').get_json()
    assert body == {'created_at': '2026-10-22T07:59:49Z'}

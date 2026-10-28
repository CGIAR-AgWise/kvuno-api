"""PostGIS smoke test, run against the CI service after `alembic upgrade head`.

The unit suite is SQLite-only, so nothing in it can catch a Postgres/PostGIS
regression: a migration that only breaks there, the geometry binding on an
INSERT, or the `radius` query that must measure metres. This inserts one row
and asks the real repo for it with `radius`, from a point a few kilometres
away, and with the `crop_type` filter. It exits non-zero on a wrong answer, so the CI step fails.

    DB_URL=postgresql://... python scripts/postgis_smoke.py
"""
import os
import sys

# Ensure the project root is on sys.path
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    # noinspection PyTypeChecker
    sys.path.insert(0, _project_root)

os.environ.setdefault('LOG_LEVEL', 'WARNING')

import typer  # noqa: E402
from geoalchemy2.elements import WKTElement  # noqa: E402
from sqlalchemy import text  # noqa: E402

CHECKSUM = 'postgis-smoke'


def main():
    from app.dto.data_filters import PlantingDataFilter
    from app.models.database_conn import MyDb
    from app.models.kvuno import PlantingRecommendation
    from app.repo.planting_recommendation import PlantingRecommendationRepo
    from app.worker_app import build_app

    with build_app().app_context():
        session = MyDb.get_db().session
        session.query(PlantingRecommendation).filter_by(check_sum=CHECKSUM).delete()
        session.commit()

        version = session.execute(text('SELECT postgis_version()')).scalar()
        typer.echo(f"PostGIS {version}")

        session.add(PlantingRecommendation(
            check_sum=CHECKSUM,
            coordinates=WKTElement('POINT(36.8 -1.3)', srid=4326),
            lon=36.8, lat=-1.3, country='Kenya', province='Nairobi', variety='H614',
            season_type='Average', opt_date='30-Nov', planting_option=1, crop_type='maize',
        ))
        session.commit()

        repo = PlantingRecommendationRepo()
        near = repo.get_coordinates(PlantingDataFilter(coordinates='36.8,-1.3', radius=1_000), limit=10)
        far = repo.get_coordinates(PlantingDataFilter(coordinates='36.9,-1.3', radius=1_000), limit=10)
        # The filter is case-insensitive: 'Maize' is stored as 'maize'.
        same_crop = repo.get_coordinates(PlantingDataFilter(
            coordinates='36.8,-1.3', radius=1_000, crop_type='Maize'), limit=10)
        other_crop = repo.get_coordinates(PlantingDataFilter(
            coordinates='36.8,-1.3', radius=1_000, crop_type='cassava'), limit=10)

        session.query(PlantingRecommendation).filter_by(check_sum=CHECKSUM).delete()
        session.commit()

        assert len(near) == 1, f'metres radius did not find the row 0 m away (got {len(near)})'
        assert len(far) == 0, f'metres radius matched a row ~11 km away (got {len(far)})'
        assert len(same_crop) == 1, f'crop_type=Maize did not find the maize row (got {len(same_crop)})'
        assert len(other_crop) == 0, f'crop_type=cassava matched the maize row (got {len(other_crop)})'
        typer.echo('PostGIS smoke test passed')


if __name__ == '__main__':
    main()

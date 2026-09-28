from typing import Optional, List, Type

from flask_sqlalchemy.pagination import QueryPagination
from geoalchemy2 import WKTElement
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Query

from app.dto.planting_recommendation import PlantingRecommendationCreate
from app.dto.data_filters import PlantingDataFilter
from app.models.database_conn import MyDb
from app.models.kvuno import PlantingRecommendation, ImportConflict
from app.utils.logging import SharedLogger

shared_logger = SharedLogger()


class PlantingRecommendationRepo:
    def __init__(self):
        self.logger = shared_logger.get_logger()

    def _get_session(self):
        self.db = MyDb.get_db()
        return self.db.session

    def add(self, record: PlantingRecommendation) -> PlantingRecommendation:
        session = self._get_session()
        try:
            session.add(record)
            session.commit()
            session.refresh(record)
            self.logger.info(f"Added PlantingRecommendation with ID: {record.id}")
            return record
        except Exception as e:
            session.rollback()
            self.logger.error(f"Failed to add PlantingRecommendation: {e}")
            raise

    def get_by_id(self, record_id: int) -> Optional[PlantingRecommendation]:
        session = self._get_session()
        try:
            record = session.query(PlantingRecommendation).filter_by(id=record_id).first()
            self.logger.info(f"Retrieved PlantingRecommendation with ID: {record_id}")
            return record
        except Exception as e:
            self.logger.error(f"Failed to retrieve PlantingRecommendation with ID {record_id}: {e}")
            raise

    def get_all(self, page, per_page) -> list[Type[PlantingRecommendation]]:
        session = self._get_session()
        offset = (page - 1) * per_page
        try:
            records = (session.query(PlantingRecommendation)
                       .offset(offset)
                       .limit(per_page)
                       .all())
            self.logger.info("Retrieved all PlantingRecommendation records")
            return records
        except Exception as e:
            self.logger.error(f"Failed to retrieve all PlantingRecommendation records: {e}")
            raise

    def get_filtered_data(self, filters: PlantingDataFilter) -> Query:
        query = self._apply_filters(filters)
        sort_col = filters.sort_col or 'id'
        sort_dir = filters.sort_dir or 'asc'
        col_attr = getattr(PlantingRecommendation, sort_col, PlantingRecommendation.id)
        query = query.order_by(col_attr.asc() if sort_dir == 'asc' else col_attr.desc())
        return query

    def _apply_filters(self, filters: PlantingDataFilter) -> Query:
        session = self._get_session()
        query = session.query(PlantingRecommendation)

        if filters.coordinates and filters.radius:
            lon, lat = map(float, filters.coordinates.split(","))
            point = WKTElement(f'POINT({lon} {lat})', srid=4326)
            query = query.filter(
                func.ST_DWithin(PlantingRecommendation.coordinates, point, filters.radius)
            )
        if filters.country:
            query = query.filter(PlantingRecommendation.country == filters.country)
        if filters.province:
            query = query.filter(PlantingRecommendation.province.ilike(f"%{filters.province}%"))
        if filters.variety:
            query = query.filter(PlantingRecommendation.variety == filters.variety)
        if filters.season_type:
            query = query.filter(PlantingRecommendation.season_type == filters.season_type)
        if filters.opt_date:
            query = query.filter(PlantingRecommendation.opt_date == filters.opt_date)
        if filters.planting_option is not None:
            query = query.filter(PlantingRecommendation.planting_option == filters.planting_option)
        return query

    def get_paginated_data(self, filters: PlantingDataFilter, page: int, per_page: int) -> QueryPagination:
        query = self.get_filtered_data(filters)
        return query.paginate(page=page, per_page=per_page, error_out=False)

    def get_coordinates(self, filters: PlantingDataFilter) -> list:
        query = self.get_filtered_data(filters)
        query = query.with_entities(
            PlantingRecommendation.lat,
            PlantingRecommendation.lon,
        ).filter(
            PlantingRecommendation.lat.isnot(None),
            PlantingRecommendation.lon.isnot(None),
        )
        return query.all()

    def get_clusters(self, filters: PlantingDataFilter, zoom: int,
                     ne_lat: float, ne_lng: float,
                     sw_lat: float, sw_lng: float) -> list[dict]:
        grid_size = max(0.0001, 360.0 / (2 ** max(zoom, 1)))
        bounds = func.ST_MakeEnvelope(sw_lng, sw_lat, ne_lng, ne_lat, 4326)

        # Base query: filters + spatial bounding box — no ORDER BY (conflicts with GROUP BY)
        query = self._apply_filters(filters)
        query = query.filter(
            PlantingRecommendation.coordinates.intersects(bounds)
        ).filter(
            PlantingRecommendation.lat.isnot(None),
            PlantingRecommendation.lon.isnot(None),
        )
        query = query.order_by(None)

        # Group by grid cell and return center + count
        grid = func.ST_SnapToGrid(PlantingRecommendation.coordinates, grid_size)
        cols = query.with_entities(
            func.avg(PlantingRecommendation.lat).label('lat'),
            func.avg(PlantingRecommendation.lon).label('lon'),
            func.count(PlantingRecommendation.id).label('count'),
        ).group_by(grid).all()

        return [
            {'lat': round(float(r.lat), 6), 'lon': round(float(r.lon), 6), 'count': r.count}
            for r in cols if r.lat is not None and r.lon is not None
        ]

    def update(self, record: PlantingRecommendation) -> PlantingRecommendation:
        session = self._get_session()
        try:
            session.commit()
            session.refresh(record)
            self.logger.info(f"Updated PlantingRecommendation with ID: {record.id}")
            return record
        except Exception as e:
            session.rollback()
            self.logger.error(f"Failed to update PlantingRecommendation with ID {record.id}: {e}")
            raise

    def delete(self, record: PlantingRecommendation) -> None:
        session = self._get_session()
        try:
            session.delete(record)
            session.commit()
            self.logger.info(f"Deleted PlantingRecommendation with ID: {record.id}")
        except Exception as e:
            session.rollback()
            self.logger.error(f"Failed to delete PlantingRecommendation with ID {record.id}: {e}")
            raise

    def find_by_checksum(self, check_sum: str) -> Optional[PlantingRecommendation]:
        session = self._get_session()
        try:
            record = (session.query(PlantingRecommendation)
                      .filter_by(check_sum=check_sum).first())
            self.logger.info(f"Retrieved PlantingRecommendation with checksum: {check_sum}")
            return record
        except Exception as e:
            self.logger.error(f"Failed to find PlantingRecommendation with checksum {check_sum}: {e}")
            raise

    def get_distinct_values(self, columns: list[str]) -> dict[str, list]:
        session = self._get_session()
        result = {}
        for col in columns:
            try:
                col_attr = getattr(PlantingRecommendation, col, None)
                if col_attr is None:
                    continue
                values = (
                    session.query(col_attr)
                    .filter(col_attr.isnot(None))
                    .distinct()
                    .order_by(col_attr)
                    .all()
                )
                result[col] = [v[0] for v in values]
            except Exception as e:
                self.logger.error(f"Failed to get distinct values for {col}: {e}")
                result[col] = []
        return result

    def _log_conflicts(self, session, mappings, inserted_count):
        if inserted_count == len(mappings):
            return
        from sqlalchemy import tuple_
        unique_cols = ['country', 'province', 'lon', 'lat', 'variety', 'season_type', 'opt_date']
        key_tuples = [
            tuple(m.get(c) for c in unique_cols)
            for m in mappings
        ]
        existing = session.query(PlantingRecommendation).filter(
            tuple_(*[getattr(PlantingRecommendation, c) for c in unique_cols]).in_(key_tuples)
        ).all()
        existing_keys = {
            tuple(getattr(e, c) for c in unique_cols): e.check_sum
            for e in existing
        }
        for m in mappings:
            key = tuple(m.get(c) for c in unique_cols)
            if key in existing_keys:
                record_data = {
                    k: str(v) if not isinstance(v, (str, int, float, bool, list, dict)) and v is not None else v
                    for k, v in m.items()
                }
                conflict = ImportConflict(
                    record_data=record_data,
                    country=m.get('country'),
                    province=m.get('province'),
                    lon=m.get('lon'),
                    lat=m.get('lat'),
                    variety=m.get('variety'),
                    season_type=m.get('season_type'),
                    opt_date=m.get('opt_date'),
                    check_sum=existing_keys[key],
                    source='batch_insert',
                )
                session.add(conflict)
        session.flush()

    def batch_insert(self, records: List[PlantingRecommendationCreate]) -> int:
        if not records:
            self.logger.warning("No records to insert.")
            return 0

        session = self._get_session()
        try:
            mappings = [
                {
                    **record.model_dump(),
                    'coordinates': WKTElement(f"POINT({record.lon} {record.lat})", srid=4326)
                    if record.lat and record.lon
                    else None
                }
                for record in records
            ]

            stmt = insert(PlantingRecommendation).values(mappings)
            stmt = stmt.on_conflict_do_nothing()
            result = session.execute(stmt)
            inserted = result.rowcount
            self._log_conflicts(session, mappings, inserted)
            session.commit()
            if inserted:
                self.logger.info(
                    f"Inserted {inserted} new PlantingRecommendation records "
                    f"({len(mappings) - inserted} duplicates logged)"
                )
            else:
                self.logger.info(f"All {len(mappings)} records were duplicates — logged")
            return inserted
        except SQLAlchemyError as e:
            session.rollback()
            self.logger.error(f"Failed to batch insert PlantingRecommendation records: {e}")
            raise
        except Exception as e:
            session.rollback()
            self.logger.error(f"An unexpected error occurred: {e}")
            raise

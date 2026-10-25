from typing import Optional, Type

from flask_sqlalchemy.pagination import QueryPagination
from geoalchemy2 import Geography, WKTElement
from sqlalchemy import cast, func
from sqlalchemy.orm import Query

from app.dto.data_filters import PLANTING_SORT_COLUMNS, PlantingDataFilter
from app.models.database_conn import MyDb
from app.models.kvuno import PlantingRecommendation
from app.utils.logging import get_logger



class PlantingRecommendationRepo:
    def __init__(self):
        self.logger = get_logger(__name__)

    def _get_session(self):
        self.db = MyDb.get_db()
        return self.db.session

    def add(self, record: PlantingRecommendation) -> PlantingRecommendation:
        session = self._get_session()
        try:
            session.add(record)
            session.commit()
            session.refresh(record)
            self.logger.debug(f"Added PlantingRecommendation with ID: {record.id}")
            return record
        except Exception as e:
            session.rollback()
            self.logger.error(f"Failed to add PlantingRecommendation: {e}")
            raise

    def get_by_id(self, record_id: int) -> Optional[PlantingRecommendation]:
        session = self._get_session()
        try:
            record = session.query(PlantingRecommendation).filter_by(id=record_id).first()
            self.logger.debug(f"Retrieved PlantingRecommendation with ID: {record_id}")
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
            self.logger.debug("Retrieved all PlantingRecommendation records")
            return records
        except Exception as e:
            self.logger.error(f"Failed to retrieve all PlantingRecommendation records: {e}")
            raise

    def get_filtered_data(self, filters: PlantingDataFilter) -> Query:
        query = self._apply_filters(filters)
        sort_col = filters.sort_col or 'id'
        sort_dir = filters.sort_dir or 'asc'
        # The DTO already rejects unknown columns with a validation error; this
        # is the backstop for callers that build a filter with model_construct
        # or bypass validation. getattr on an arbitrary name reached non-column
        # attributes like `metadata` and 500'd on .asc().
        if sort_col not in PLANTING_SORT_COLUMNS:
            sort_col = 'id'
        col_attr = getattr(PlantingRecommendation, sort_col)
        order = col_attr.asc() if sort_dir == 'asc' else col_attr.desc()
        # Tie-break on the primary key. A non-unique sort column leaves row
        # order ambiguous, and LIMIT/OFFSET over an ambiguous order silently
        # repeats and skips rows as the client pages through the result.
        if col_attr is not PlantingRecommendation.id:
            order = (order, PlantingRecommendation.id.asc())
        return query.order_by(*order) if isinstance(order, tuple) else query.order_by(order)

    def _apply_filters(self, filters: PlantingDataFilter) -> Query:
        session = self._get_session()
        query = session.query(PlantingRecommendation)

        if filters.coordinates and filters.radius:
            lon, lat = map(float, filters.coordinates.split(","))
            point = WKTElement(f'POINT({lon} {lat})', srid=4326)
            # `radius` is documented in metres, but ST_DWithin on two SRID 4326
            # geometries measures in the SRS's unit — degrees — so radius=50000
            # matched the whole planet. Casting both sides to geography makes
            # the distance metres (on the spheroid), as documented.
            query = query.filter(
                func.ST_DWithin(
                    cast(PlantingRecommendation.coordinates, Geography(srid=4326)),
                    cast(point, Geography(srid=4326)),
                    filters.radius,
                )
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

    def get_coordinates(self, filters: PlantingDataFilter, limit: int, offset: int = 0,
                        sample: bool = False) -> list:
        """Rows for the map, newest-ordered unless ``sample`` is set.

        With ``sample`` the row order is randomised before the LIMIT, so a
        capped map is a representative sample of the whole dataset. Plain
        LIMIT/OFFSET would return the *first* N by id, and since ingested data
        tends to arrive grouped by region or season, that slice is usually
        geographically clumped — which makes a sampled map look misleading
        rather than sparse. Costs a sort of the filtered set, which is why it
        is opt-in and the result is cached.
        """
        query = self.get_filtered_data(filters)
        query = query.with_entities(
            PlantingRecommendation.lat,
            PlantingRecommendation.lon,
        ).filter(
            PlantingRecommendation.lat.isnot(None),
            PlantingRecommendation.lon.isnot(None),
        )
        if sample:
            query = query.order_by(func.random())
        return query.limit(limit).offset(offset).all()

    def count_coordinates(self, filters: PlantingDataFilter) -> int:
        """Total rows the coordinates endpoint could page over.

        Counted separately from the page query so the client learns the real
        size of the set instead of inferring it from a short final page.

        Uses ``_apply_filters`` and clears any ordering rather than going
        through ``get_filtered_data``. An ORDER BY carried into a COUNT(*)
        makes Postgres reject the whole statement — "column id must appear in
        the GROUP BY clause or be used in an aggregate function" — because the
        sort column is not aggregated and there is no GROUP BY to satisfy.
        Ordering is meaningless for a count anyway.
        """
        query = self._apply_filters(filters).filter(
            PlantingRecommendation.lat.isnot(None),
            PlantingRecommendation.lon.isnot(None),
        )
        return query.order_by(None).with_entities(func.count()).scalar() or 0

    def _cluster_base(self, filters: PlantingDataFilter, zoom: int,
                      ne_lat: float, ne_lng: float, sw_lat: float, sw_lng: float):
        """Filtered, bounded query plus the grid expression clusters group by."""
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
        grid = func.ST_SnapToGrid(PlantingRecommendation.coordinates, grid_size)
        return query, grid

    def count_clusters(self, filters: PlantingDataFilter, zoom: int,
                       ne_lat: float, ne_lng: float,
                       sw_lat: float, sw_lng: float) -> int:
        """Number of grid cells (clusters) the cluster endpoint can page over."""
        query, grid = self._cluster_base(filters, zoom, ne_lat, ne_lng, sw_lat, sw_lng)
        cells = query.with_entities(grid.label('cell')).group_by(grid).subquery()
        return self._get_session().query(func.count()).select_from(cells).scalar() or 0

    def get_clusters(self, filters: PlantingDataFilter, zoom: int,
                     ne_lat: float, ne_lng: float,
                     sw_lat: float, sw_lng: float,
                     limit: int | None = None, offset: int = 0) -> list[dict]:
        query, grid = self._cluster_base(filters, zoom, ne_lat, ne_lng, sw_lat, sw_lng)

        # Group by grid cell and return center + count
        grid_query = query.with_entities(
            func.avg(PlantingRecommendation.lat).label('lat'),
            func.avg(PlantingRecommendation.lon).label('lon'),
            func.count(PlantingRecommendation.id).label('count'),
        ).group_by(grid)
        # Grouped by grid cell, so the row count is bounded by cells rather than
        # by records. Paginated anyway so a very low zoom cannot return a
        # response the client has to buffer in full. Densest cells first, so
        # the heatmap degrades gracefully when a page is dropped.
        if limit is not None:
            grid_query = grid_query.order_by(
                func.count(PlantingRecommendation.id).desc()
            ).limit(limit).offset(offset)
        cols = grid_query.all()

        return [
            {'lat': round(float(r.lat), 6), 'lon': round(float(r.lon), 6), 'count': r.count}
            for r in cols if r.lat is not None and r.lon is not None
        ]

    def update(self, record: PlantingRecommendation) -> PlantingRecommendation:
        session = self._get_session()
        try:
            session.commit()
            session.refresh(record)
            self.logger.debug(f"Updated PlantingRecommendation with ID: {record.id}")
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
            self.logger.debug(f"Deleted PlantingRecommendation with ID: {record.id}")
        except Exception as e:
            session.rollback()
            self.logger.error(f"Failed to delete PlantingRecommendation with ID {record.id}: {e}")
            raise

    def find_by_checksum(self, check_sum: str) -> Optional[PlantingRecommendation]:
        session = self._get_session()
        try:
            record = (session.query(PlantingRecommendation)
                      .filter_by(check_sum=check_sum).first())
            self.logger.debug(f"Retrieved PlantingRecommendation with checksum: {check_sum}")
            return record
        except Exception as e:
            self.logger.error(f"Failed to find PlantingRecommendation with checksum {check_sum}: {e}")
            raise

    def get_distinct_values(self, columns: list[str], limit: int | None = None,
                            offset: int = 0) -> dict[str, list]:
        """Distinct values per column, optionally sliced server-side.

        ``limit``/``offset`` apply to each column independently: the response is
        a dict of independent lists, so there is no single row axis to page
        along. The caller pages each column on its own using the per-column
        totals from :meth:`count_distinct_values`.
        """
        session = self._get_session()
        result = {}
        for col in columns:
            try:
                col_attr = getattr(PlantingRecommendation, col, None)
                if col_attr is None:
                    continue
                query = (
                    session.query(col_attr)
                    .filter(col_attr.isnot(None))
                    .distinct()
                    .order_by(col_attr)
                )
                if limit is not None:
                    query = query.limit(limit).offset(offset)
                result[col] = [v[0] for v in query.all()]
            except Exception as e:
                self.logger.exception(f"Failed to get distinct values for {col}: {e}")
                result[col] = []
        return result

    def count_distinct_values(self, columns: list[str]) -> dict[str, int]:
        """Distinct value count per column, so a client knows when to stop paging."""
        session = self._get_session()
        result = {}
        for col in columns:
            try:
                col_attr = getattr(PlantingRecommendation, col, None)
                if col_attr is None:
                    continue
                result[col] = (
                    session.query(func.count(func.distinct(col_attr)))
                    .filter(col_attr.isnot(None))
                    .scalar() or 0
                )
            except Exception as e:
                self.logger.exception(f"Failed to count distinct values for {col}: {e}")
                result[col] = 0
        return result

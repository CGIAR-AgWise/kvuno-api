
from sqlalchemy import func
from sqlalchemy.orm import Query

from app.dto.data_filters import QualityFilter
from app.models.database_conn import MyDb
from app.models.kvuno import ImportConflict
from app.utils.logging import SharedLogger

shared_logger = SharedLogger()


class ImportConflictRepo:
    def __init__(self):
        self.logger = shared_logger.get_logger()

    def _get_session(self):
        self.db = MyDb.get_db()
        return self.db.session

    def get_filtered_query(self, filters: QualityFilter) -> Query:
        session = self._get_session()
        query = session.query(ImportConflict)

        if filters.country:
            query = query.filter(ImportConflict.country.ilike(f"%{filters.country}%"))
        if filters.source:
            query = query.filter(ImportConflict.source == filters.source)
        if filters.search:
            query = query.filter(
                ImportConflict.country.ilike(f"%{filters.search}%")
                | ImportConflict.variety.ilike(f"%{filters.search}%")
                | ImportConflict.province.ilike(f"%{filters.search}%")
            )

        sort_col = filters.sort_col or 'created_at'
        sort_dir = filters.sort_dir or 'desc'
        col_attr = getattr(ImportConflict, sort_col, ImportConflict.created_at)
        query = query.order_by(col_attr.asc() if sort_dir == 'asc' else col_attr.desc())

        return query

    def get_paginated(self, filters: QualityFilter, page: int, per_page: int):
        query = self.get_filtered_query(filters)
        return query.paginate(page=page, per_page=per_page, error_out=False)

    def get_stats(self):
        session = self._get_session()
        from app.models.kvuno import PlantingRecommendation, FileImport

        total_records = session.query(func.count(PlantingRecommendation.id)).scalar() or 0
        total_conflicts = session.query(func.count(ImportConflict.id)).scalar() or 0
        total_files = session.query(func.count(FileImport.id)).scalar() or 0

        conflicts_by_country = (
            session.query(ImportConflict.country, func.count(ImportConflict.id))
            .filter(ImportConflict.country.isnot(None))
            .group_by(ImportConflict.country)
            .order_by(func.count(ImportConflict.id).desc())
            .limit(10)
            .all()
        )

        conflicts_by_source = (
            session.query(ImportConflict.source, func.count(ImportConflict.id))
            .filter(ImportConflict.source.isnot(None))
            .group_by(ImportConflict.source)
            .order_by(func.count(ImportConflict.id).desc())
            .all()
        )

        coverage = self._compute_coverage(session)

        return {
            "total_records": total_records,
            "total_conflicts": total_conflicts,
            "total_files": total_files,
            "coverage_pct": coverage,
            "conflicts_by_country": [
                {"country": c or "Unknown", "count": n}
                for c, n in conflicts_by_country
            ],
            "conflicts_by_source": [
                {"source": s or "Unknown", "count": n}
                for s, n in conflicts_by_source
            ],
        }

    def _compute_coverage(self, session):
        from app.models.kvuno import PlantingRecommendation
        try:
            total = session.query(func.count(PlantingRecommendation.id)).scalar() or 0
            if total == 0:
                return 0
            with_coords = (
                session.query(func.count(PlantingRecommendation.id))
                .filter(
                    PlantingRecommendation.lat.isnot(None),
                    PlantingRecommendation.lon.isnot(None),
                )
                .scalar() or 0
            )
            return round(with_coords / total * 100)
        except Exception:
            return 0

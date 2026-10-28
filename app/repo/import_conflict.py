
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Query

from app.dto.data_filters import CONFLICT_SORT_COLUMNS, QualityFilter
from app.models.database_conn import MyDb
from app.models.kvuno import ImportConflict
from app.utils.logging import get_logger

# What makes two conflicts "the same recommendation" on the Duplicates tab.
DUPLICATE_KEYS = ('country', 'province', 'variety', 'season_type', 'opt_date', 'crop_type')


class ImportConflictRepo:
    def __init__(self):
        self.logger = get_logger(__name__)

    def _get_session(self):
        self.db = MyDb.get_db()
        return self.db.session

    def get_filtered_query(self, filters: QualityFilter) -> Query:
        session = self._get_session()
        query = self._filtered(session.query(ImportConflict), filters)

        sort_col = filters.sort_col or 'created_at'
        sort_dir = filters.sort_dir or 'desc'
        # The DTO rejects unknown columns; this is the backstop for a filter
        # built without validation. getattr on an arbitrary name reached
        # non-column attributes like `metadata` and 500'd on .asc().
        if sort_col not in CONFLICT_SORT_COLUMNS:
            sort_col = 'created_at'
        col_attr = getattr(ImportConflict, sort_col)
        query = query.order_by(col_attr.asc() if sort_dir == 'asc' else col_attr.desc())

        return query

    def get_paginated(self, filters: QualityFilter, page: int, per_page: int):
        query = self.get_filtered_query(filters)
        return query.paginate(page=page, per_page=per_page, error_out=False)

    def _filtered(self, query, filters: QualityFilter):
        if filters.country:
            query = query.filter(ImportConflict.country.ilike(f"%{filters.country}%"))
        if filters.crop_type:
            query = query.filter(ImportConflict.crop_type == filters.crop_type)
        if filters.source:
            query = query.filter(ImportConflict.source == filters.source)
        if filters.search:
            query = query.filter(
                ImportConflict.country.ilike(f"%{filters.search}%")
                | ImportConflict.variety.ilike(f"%{filters.search}%")
                | ImportConflict.province.ilike(f"%{filters.search}%")
            )
        return query

    def get_duplicate_groups(self, filters: QualityFilter, offset: int, limit: int) -> tuple[int, list[dict]]:
        """One page of conflicts grouped by DUPLICATE_KEYS, most repeated first.

        The grouping is done here, in SQL. The Duplicates tab used to download
        every page of /conflicts and group them in the browser, which meant one
        request per page of conflicts every time the tab opened.

        Two bounded queries per page: the groups themselves, then the per-source
        counts for just those groups (portable — no string_agg/group_concat).
        Keys are matched with IS NOT DISTINCT FROM, so a NULL province groups
        with other NULLs, as it does in GROUP BY.
        """
        session = self._get_session()
        keys = [getattr(ImportConflict, k) for k in DUPLICATE_KEYS]
        count = func.count(ImportConflict.id).label('count')

        grouped = self._filtered(session.query(*keys, count), filters).group_by(*keys)
        total = session.query(func.count()).select_from(grouped.subquery()).scalar() or 0

        rows = (
            grouped.order_by(count.desc(), *[k.asc() for k in keys])
            .offset(offset)
            .limit(limit)
            .all()
        )
        if not rows:
            return total, []

        groups = [dict(zip(DUPLICATE_KEYS, row[:len(DUPLICATE_KEYS)]), count=row[-1], sources={})
                  for row in rows]

        page_keys = or_(*[
            and_(*[col.is_not_distinct_from(group[name]) for name, col in zip(DUPLICATE_KEYS, keys)])
            for group in groups
        ])
        by_source = (
            self._filtered(session.query(*keys, ImportConflict.source, func.count(ImportConflict.id)), filters)
            .filter(page_keys)
            .group_by(*keys, ImportConflict.source)
            .all()
        )
        index = {tuple(g[k] for k in DUPLICATE_KEYS): g for g in groups}
        for row in by_source:
            group = index.get(tuple(row[:len(DUPLICATE_KEYS)]))
            if group is not None:
                source = row[len(DUPLICATE_KEYS)] or 'unknown'
                group['sources'][source] = group['sources'].get(source, 0) + row[-1]
        return total, groups

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

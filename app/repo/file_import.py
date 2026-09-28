from typing import Optional, List, Type

from app.models.database_conn import MyDb
from app.models.kvuno import FileImport
from app.utils.logging import SharedLogger

shared_logger = SharedLogger()


class FileImportRepo:
    def __init__(self):
        self.logger = shared_logger.get_logger()

    def _get_session(self):
        self.db = MyDb.get_db()
        return self.db.session

    def get_all(self) -> list[Type[FileImport]]:
        session = self._get_session()
        try:
            records = session.query(FileImport).all()
            self.logger.info("Retrieved all file imports")
            return records
        except Exception as e:
            self.logger.error(f"Failed to retrieve file imports: {e}")
            raise

    def add(self, record: FileImport) -> None:
        session = self._get_session()
        try:
            session.add(record)
            session.commit()
            self.logger.info(f"Added file import with ID: {record.id}")
        except Exception as e:
            session.rollback()
            self.logger.error(f"Failed to add file import: {e}")
            raise

    def get_by_id(self, record_id: int) -> Optional[FileImport]:
        session = self._get_session()
        try:
            record = session.query(FileImport).filter_by(id=record_id).first()
            self.logger.info(f"Retrieved file import with ID: {record_id}")
            return record
        except Exception as e:
            self.logger.error(f"Failed to retrieve file import with ID {record_id}: {e}")
            raise

    def get_by_checksum(self, checksum: str) -> Optional[FileImport]:
        session = self._get_session()
        try:
            record = (session.query(FileImport)
                      .filter_by(check_sum=checksum).first())
            self.logger.info(f"Retrieved file import with checksum: {checksum}")
            return record
        except Exception as e:
            self.logger.error(f"Failed to retrieve file import with checksum {checksum}: {e}")
            raise

    def get_by_name(self, file_name: str) -> Optional[FileImport]:
        session = self._get_session()
        try:
            record = session.query(FileImport).filter_by(file_name=file_name).first()
            self.logger.info(f"Retrieved file import with name: {file_name}")
            return record
        except Exception as e:
            self.logger.error(f"Failed to retrieve file import with name {file_name}: {e}")
            raise

    def delete(self, record: FileImport) -> None:
        session = self._get_session()
        try:
            session.delete(record)
            session.commit()
            self.logger.info(f"Deleted file import with ID: {record.id}")
        except Exception as e:
            session.rollback()
            self.logger.error(f"Failed to delete file import with ID {record.id}: {e}")
            raise

    def upsert_offset(self, checksum: str, file_name: str, offset: int, original_filename: str = "") -> None:
        session = self._get_session()
        try:
            existing = session.query(FileImport).filter_by(check_sum=checksum).first()
            if existing:
                existing.offset = offset
                if original_filename:
                    existing.original_filename = original_filename
            else:
                session.add(FileImport(
                    check_sum=checksum,
                    file_name=file_name,
                    offset=offset,
                    original_filename=original_filename or None,
                ))
            session.commit()
            self.logger.info(f"Upserted offset {offset} for checksum {checksum}")
        except Exception as e:
            session.rollback()
            self.logger.error(f"Failed to upsert offset for checksum {checksum}: {e}")
            raise

    def batch_insert(self, records: List[FileImport]) -> None:
        session = self._get_session()
        try:
            session.add_all(records)
            session.commit()
            self.logger.info(f"Batch inserted {len(records)} file imports")
        except Exception as e:
            session.rollback()
            self.logger.error(f"Failed to batch insert file imports: {e}")
            raise

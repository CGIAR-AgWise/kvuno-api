import hashlib
import logging
import os
import tempfile


from app.utils import calculate_file_checksum

logger = logging.getLogger(__name__)


class TestCalculateFileChecksum:
    def test_sha256_default(self):
        content = b"hello world"
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(content)
            path = f.name
        try:
            result = calculate_file_checksum(path, logger)
            expected = hashlib.sha256(content).hexdigest()
            assert result == expected
        finally:
            os.unlink(path)

    def test_md5_algorithm(self):
        content = b"test data"
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(content)
            path = f.name
        try:
            result = calculate_file_checksum(path, logger, algorithm="md5")
            expected = hashlib.md5(content).hexdigest()
            assert result == expected
        finally:
            os.unlink(path)

    def test_file_not_found_returns_none(self):
        result = calculate_file_checksum("/nonexistent/file.RDS", logger)
        assert result is None

    def test_large_file(self):
        content = b"x" * (1024 * 1024)
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(content)
            path = f.name
        try:
            result = calculate_file_checksum(path, logger)
            expected = hashlib.sha256(content).hexdigest()
            assert result == expected
        finally:
            os.unlink(path)

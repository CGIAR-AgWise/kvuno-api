"""scripts/convert_rds.py: convert RDS locally when the server lacks the memory."""
import importlib.util
import os

import pandas as pd
import pyarrow.parquet as pq
import pyreadr
from typer.testing import CliRunner

_spec = importlib.util.spec_from_file_location(
    'convert_rds', os.path.join(os.path.dirname(__file__), '..', 'scripts', 'convert_rds.py'))
script = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(script)


def _rds(path):
    pyreadr.write_rds(str(path), pd.DataFrame({'lat': [1.0, 2.0], 'lon': [3.0, 4.0]}))


def test_converts_a_directory_keeps_the_rds_and_reports_failures(tmp_path):
    _rds(tmp_path / 'good.RDS')
    (tmp_path / 'bad.rds').write_bytes(b'not R data')
    out = tmp_path / 'out'

    result = CliRunner().invoke(script.app, [str(tmp_path), '--out-dir', str(out)])

    assert result.exit_code == 1  # one file failed
    assert pq.ParquetFile(out / 'good.parquet').metadata.num_rows == 2
    assert (tmp_path / 'good.RDS').exists(), 'the original is kept'
    assert sorted(os.listdir(out)) == ['good.parquet'], 'no partial output for the bad file'
    assert '1 of 2 converted, 1 failed.' in result.output


def test_an_existing_parquet_is_kept_unless_overwrite(tmp_path):
    _rds(tmp_path / 'a.RDS')
    (tmp_path / 'a.parquet').write_text('keep me')

    result = CliRunner().invoke(script.app, [str(tmp_path / 'a.RDS')])
    assert result.exit_code == 0 and '1 skipped' in result.output
    assert (tmp_path / 'a.parquet').read_text() == 'keep me'

    assert CliRunner().invoke(script.app, [str(tmp_path / 'a.RDS'), '--overwrite']).exit_code == 0
    assert pq.ParquetFile(tmp_path / 'a.parquet').metadata.num_rows == 2

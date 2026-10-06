"""Isolate every test (including root-level legacy tests) before collection."""
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
_previous_data_dir = os.environ.get('PIXIV_VIEWER_DATA_DIR')
_test_data = tempfile.TemporaryDirectory(prefix='pixiv-viewer-tests-')
os.environ['PIXIV_VIEWER_DATA_DIR'] = str(Path(_test_data.name) / 'data')


@pytest.fixture
def library(tmp_path):
    from utils.database import DatabaseManager
    manager = DatabaseManager(tmp_path / 'library.db')
    try:
        yield manager
    finally:
        manager.close_thread_connection()


def pytest_unconfigure(config):
    module = sys.modules.get('utils.database')
    if module is not None:
        module.db.close_thread_connection()
    _test_data.cleanup()
    if _previous_data_dir is None:
        os.environ.pop('PIXIV_VIEWER_DATA_DIR', None)
    else:
        os.environ['PIXIV_VIEWER_DATA_DIR'] = _previous_data_dir

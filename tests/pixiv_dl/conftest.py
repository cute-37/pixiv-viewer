import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """把所有路径指到临时目录，并去掉等待/节流，避免测试碰到真实数据或变慢。"""
    from pixiv_dl import interrupt
    from pixiv_dl.config import Config

    for k, v in {
        'DB_PATH': str(tmp_path / 'db' / 't.db'),
        'LOCAL_SAVE_PATH': str(tmp_path / 'save'),
        'LOCAL_TEMP_PATH': str(tmp_path / 'temp'),
        'AVATARS_PATH': str(tmp_path / 'avatars'),
        'CACHE_PATH': str(tmp_path / 'cache'),
        'SETTINGS_FILE': str(tmp_path / 'settings.json'),
        'LOG_DIR': str(tmp_path / 'logs'),
        'STORAGE_MODE': 'local',
        'DELAY_SYNC': (0, 0), 'DELAY_DOWNLOAD': (0, 0),
        'RATE_LIMIT_ENABLED': False, 'AUTO_THROTTLE_ENABLED': False,
        'MAX_RETRIES': 2, 'MAX_ATTEMPTS': 3, 'METADATA_REFRESH_LIMIT': 2,
        'DB_AUTO_BACKUP_DAYS': 0, 'DB_BACKUP_KEEP': 5, 'DB_JOURNAL': 'delete',
        'SYNC_TYPES': ['illust', 'manga'], 'SYNC_NOVELS': True,
        'MAIN_ACCOUNT_SYNC_THREADS': 1, 'BACKUP_ACCOUNT_SYNC_THREADS': 1,
        'MAIN_ACCOUNT_DOWNLOAD_THREADS': 2, 'BACKUP_ACCOUNT_DOWNLOAD_THREADS': 1,
        'TOKENS': {'main': {'token': 'tok-main', 'is_valid': True}},
        'MAIN_ACCOUNT': 'main', 'REFRESH_TOKEN': '', 'PROXIES': {},
        'PROXY_MODE': '', 'PROXY_URL': '',
    }.items():
        monkeypatch.setattr(Config, k, v)
    interrupt.clear()
    yield Config
    interrupt.clear()


@pytest.fixture
def no_sleep(monkeypatch):
    from pixiv_dl import interrupt
    monkeypatch.setattr(interrupt, 'wait', lambda s: interrupt.is_set())

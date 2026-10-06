import os
import sqlite3

import pytest

from pixiv_dl.database import Database


def _cols(db, table):
    return [r[1] for r in db.conn.execute(f"PRAGMA table_info({table})")]


def test_fresh_schema_has_everything_and_no_backup(cfg):
    db = Database(cfg.DB_PATH)
    assert {'is_deleted', 'is_private_follow', 'is_temp_name', 'profile_image_local', 'gender'} <= set(_cols(db, 'artists'))
    idx = {r[0] for r in db.conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {'idx_illust_hash', 'idx_illust_status'} <= idx
    assert not os.path.exists(os.path.join(os.path.dirname(cfg.DB_PATH), 'backup'))


def _make_old_style_db(path):
    """模拟用户现有库的前一个版本：缺少新列和 file_hash 索引，里面有数据。"""
    c = sqlite3.connect(path)
    c.executescript("""
    CREATE TABLE artists (author_id INTEGER PRIMARY KEY, author_name TEXT, last_synced_id INTEGER DEFAULT 0, last_sync_time TIMESTAMP);
    CREATE TABLE illust_metadata (illust_id INTEGER PRIMARY KEY, author_id INTEGER, title TEXT, create_date TEXT, tags TEXT,
        x_restrict INTEGER DEFAULT 0, is_r18 INTEGER DEFAULT 0, page_count INTEGER DEFAULT 1, width INTEGER, height INTEGER,
        sanity_level INTEGER, illust_type INTEGER, series_id INTEGER, series_title TEXT, tools TEXT, caption TEXT);
    CREATE TABLE illusts (task_key TEXT PRIMARY KEY, illust_id INTEGER, page_index INTEGER, url TEXT, media_type TEXT DEFAULT 'image',
        status INTEGER DEFAULT 0, updated_at TEXT, attempts INTEGER DEFAULT 0, file_hash TEXT, file_size INTEGER DEFAULT 0,
        download_date TEXT, original_filename TEXT, content_type TEXT);
    INSERT INTO artists VALUES (1, 'A', 5, '2026-01-01');
    INSERT INTO illust_metadata (illust_id, author_id, title) VALUES (10, 1, 'w');
    INSERT INTO illusts (task_key, illust_id, page_index, url, status, file_hash, file_size) VALUES ('10_0', 10, 0, 'http://old/10_p0.jpg', 1, 'abc', 123);
    """)
    c.commit()
    c.close()


def test_migration_is_additive_and_backs_up(cfg):
    os.makedirs(os.path.dirname(cfg.DB_PATH))
    _make_old_style_db(cfg.DB_PATH)
    db = Database(cfg.DB_PATH)
    # 数据原样保留
    assert db.conn.execute("SELECT author_name, last_synced_id FROM artists WHERE author_id=1").fetchone() == ('A', 5)
    assert db.conn.execute("SELECT status, file_hash, file_size, url FROM illusts").fetchone() == (1, 'abc', 123, 'http://old/10_p0.jpg')
    assert 'is_deleted' in _cols(db, 'artists') and 'ugoira_data' in _cols(db, 'illust_metadata')
    # 迁移前留了备份，且备份里是迁移前的结构与数据
    bdir = os.path.join(os.path.dirname(cfg.DB_PATH), 'backup')
    files = os.listdir(bdir)
    assert len(files) == 1
    b = sqlite3.connect(os.path.join(bdir, files[0]))
    assert 'is_deleted' not in [r[1] for r in b.execute("PRAGMA table_info(artists)")]
    assert b.execute("SELECT COUNT(*) FROM illusts").fetchone()[0] == 1
    b.close()
    # 再次打开不会重复备份
    Database(cfg.DB_PATH)
    assert len(os.listdir(bdir)) == 1


def test_legacy_illusts_with_author_id_migrates_without_dropping(cfg):
    os.makedirs(os.path.dirname(cfg.DB_PATH))
    c = sqlite3.connect(cfg.DB_PATH)
    c.executescript("""
    CREATE TABLE artists (author_id INTEGER PRIMARY KEY, author_name TEXT, last_synced_id INTEGER DEFAULT 0, last_sync_time TIMESTAMP);
    CREATE TABLE illusts (task_key TEXT PRIMARY KEY, illust_id INTEGER, page_index INTEGER, author_id INTEGER, title TEXT, url TEXT,
        media_type TEXT, status INTEGER, create_date TEXT, tags TEXT, x_restrict INTEGER, is_r18 INTEGER, page_count INTEGER,
        width INTEGER, height INTEGER, sanity_level INTEGER, illust_type INTEGER, series_id INTEGER, series_title TEXT, tools TEXT, caption TEXT);
    INSERT INTO illusts (task_key, illust_id, page_index, author_id, title, url, media_type, status) VALUES ('5_0', 5, 0, 9, 'T', 'u', 'image', 1);
    """)
    c.commit()
    c.close()
    db = Database(cfg.DB_PATH)
    assert db.conn.execute("SELECT author_id, title FROM illust_metadata WHERE illust_id=5").fetchone() == (9, 'T')
    assert db.conn.execute("SELECT status FROM illusts WHERE task_key='5_0'").fetchone() == (1,)
    # 旧表保留，不丢数据
    assert db.conn.execute("SELECT COUNT(*) FROM illusts_old").fetchone()[0] == 1


def test_upsert_artist_only_updates_given_fields(cfg):
    db = Database(cfg.DB_PATH)
    db.upsert_artist(1, 'Name', author_account='acc', is_followed=1)
    db.upsert_artist(1, None, is_deleted=1)
    a = db.get_artist_full(1)
    assert (a['author_name'], a['author_account'], a['is_followed'], a['is_deleted']) == ('Name', 'acc', 1, 1)
    with pytest.raises(TypeError):
        db.upsert_artist(1, 'x', bogus=1)


def test_save_illust_keeps_status_and_does_not_overwrite_url_with_placeholder(cfg):
    db = Database(cfg.DB_PATH)
    db.upsert_artist(1, 'A')
    base = {'task_key': '7_0', 'illust_id': 7, 'page_index': 0, 'author_id': 1, 'title': 't', 'media_type': 'ugoira'}
    db.save_illust({**base, 'url': 'https://x/7_ugoira600x600.zip'})
    db.mark_status('7_0', 1)
    db.save_illust({**base, 'url': 'ugoira://7', 'title': 'new title'})
    assert db.conn.execute("SELECT status, url FROM illusts WHERE task_key='7_0'").fetchone() == (1, 'https://x/7_ugoira600x600.zip')
    assert db.conn.execute("SELECT title FROM illust_metadata WHERE illust_id=7").fetchone() == ('new title',)


def test_update_file_info_does_not_wipe_hash(cfg):
    db = Database(cfg.DB_PATH)
    db.save_illust({'task_key': '1_0', 'illust_id': 1, 'page_index': 0, 'author_id': 1, 'url': 'u'})
    db.update_file_info('1_0', 'HASH', 100)
    db.update_file_info('1_0', None, 200)
    assert db.conn.execute("SELECT file_hash, file_size FROM illusts").fetchone() == ('HASH', 200)


def test_pending_tasks_shape_and_filters(cfg):
    db = Database(cfg.DB_PATH)
    for aid, iid in ((1, 10), (2, 20), (1, 11)):
        db.upsert_artist(aid, f'a{aid}')
        db.save_illust({'task_key': f'{iid}_0', 'illust_id': iid, 'page_index': 0, 'author_id': aid, 'url': 'u', 'title': 't'})
    rows = db.get_pending_tasks()
    assert len(rows[0]) == 10
    assert [r[1] for r in rows] == [10, 11, 20]          # 按画师、作品排序
    assert [r[1] for r in db.get_pending_tasks(author_id=2)] == [20]
    assert len(db.get_pending_tasks(limit=2)) == 2
    db.set_attempts('10_0', 3)
    assert 10 not in [r[1] for r in db.get_pending_tasks(max_attempts=3)]


def test_watermark_never_decreases_and_failed_reset(cfg):
    db = Database(cfg.DB_PATH)
    db.upsert_artist(1, 'a')
    db.mark_artist_synced(1, 100)
    db.mark_artist_synced(1, 50)
    assert db.get_artist(1)[2] == 100
    db.save_illust({'task_key': '5_0', 'illust_id': 5, 'page_index': 0, 'author_id': 1, 'url': 'u'})
    db.mark_status('5_0', -1)
    db.increment_attempts('5_0')
    assert db.reset_failed_tasks_by_filter() == 1
    assert db.conn.execute("SELECT status, attempts FROM illusts").fetchone() == (0, 0)


def test_artist_helpers_and_stats(cfg):
    db = Database(cfg.DB_PATH)
    db.upsert_artist(1, 'a', is_deleted=1)
    db.upsert_artist(2, 'b', profile_image_url='https://x/y.png')
    assert [r[0] for r in db.get_all_artists()] == [2]
    assert [r[0] for r in db.get_all_artists(include_deleted=True)] == [1, 2]
    assert [r[0] for r in db.get_deleted_artists()] == [1]
    assert [r[0] for r in db.get_artists_without_avatar()] == [2]
    assert [r[0] for r in db.get_artists_without_profile()] == [2]
    assert db.get_artist_full(2)['author_name'] == 'b'
    s = db.stats()
    assert s['artists'] == 2 and s['artists_deleted'] == 1


def test_reset_in_progress(cfg):
    db = Database(cfg.DB_PATH)
    db.save_illust({'task_key': '1_0', 'illust_id': 1, 'page_index': 0, 'author_id': 1, 'url': 'u'})
    db.mark_status('1_0', 2)
    assert db.reset_in_progress() == 1
    assert db.conn.execute("SELECT status FROM illusts").fetchone() == (0,)

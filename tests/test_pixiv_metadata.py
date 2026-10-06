import json
import sqlite3
import threading
from contextlib import closing, contextmanager

import pytest

from utils.pixiv_metadata_reader import PixivMetadataReader, get_pixiv_reader


def make_metadata(path, count=4, title='work', with_artists=True):
    with closing(sqlite3.connect(path)) as conn:
        conn.execute('CREATE TABLE illust_metadata (illust_id INTEGER PRIMARY KEY, author_id INTEGER, title TEXT, tags TEXT, tags_translated TEXT, tools TEXT, ai_type INTEGER, x_restrict INTEGER)')
        if with_artists:
            conn.execute('CREATE TABLE artists (author_id INTEGER PRIMARY KEY, name TEXT)')
            conn.execute("INSERT INTO artists VALUES (7, 'artist')")
        conn.executemany('INSERT INTO illust_metadata VALUES (?, 7, ?, ?, ?, ?, ?, ?)',
                         [(10000 + i, title, json.dumps(['猫', 'AI生成'] if i % 2 == 0 else ['maid']),
                           '[]', '[]', 2 if i % 2 == 0 else 1, i % 3) for i in range(count)])
        conn.commit()
    return path


@pytest.mark.parametrize('tag,expected', [('maid', False), ('hair', False), ('sailor', False),
    ('airplane', False), ('AI生成', True), ('AIイラスト', True), ('ai-generated', True),
    ('AI', True), ('aigc', True), ('人工智能', True)])
def test_ai_classification(tag, expected):
    assert PixivMetadataReader().is_ai_from_meta({'tags': [tag]}) is expected


def test_batch_queries_close_readonly_connection_and_limit_cache(tmp_path, monkeypatch):
    path = make_metadata(tmp_path / 'pixiv #测试.db', count=1200)
    reader = PixivMetadataReader(str(path))
    reader._cache_max_size = 20
    connections, statements = [], []
    connect = sqlite3.connect

    def tracked_connect(*args, **kwargs):
        conn = connect(*args, **kwargs)
        connections.append(conn)
        conn.set_trace_callback(statements.append)
        with pytest.raises(sqlite3.OperationalError, match='readonly'):
            conn.execute('CREATE TABLE forbidden (x)')
        return conn

    monkeypatch.setattr('utils.pixiv_metadata_reader.sqlite3.connect', tracked_connect)
    paths = [f'{10000 + i}_p0.jpg' for i in range(1200)]
    results = reader.get_metadata_batch(paths)
    assert len(results) == 1200
    assert results[paths[-1]]['artist']['name'] == 'artist'
    assert results[paths[0]]['meta']['tags'] == ['猫', 'AI生成']
    assert len(connections) == 1
    assert sum(statement.startswith('SELECT') for statement in statements) == 5
    assert len(reader._metadata_cache) == 20
    for conn in connections:
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute('SELECT 1')


def test_missing_artist_table_and_cached_miss(tmp_path, monkeypatch):
    reader = PixivMetadataReader(str(make_metadata(tmp_path / 'pixiv.db', with_artists=False)))
    assert reader.get_metadata('10000_p0.jpg')['artist'] is None
    assert reader.get_metadata('99999.jpg') is None
    def fail(_):
        raise AssertionError('cached miss must not query again')
    monkeypatch.setattr(reader, '_connect', fail)
    assert reader.get_metadata('99999.jpg') is None


def test_path_change_discards_inflight_result(tmp_path, monkeypatch):
    old = make_metadata(tmp_path / 'old.db', title='old')
    new = make_metadata(tmp_path / 'new.db', title='new')
    reader = PixivMetadataReader(str(old))
    original_connect = reader._connect
    entered, release = threading.Event(), threading.Event()

    @contextmanager
    def slow_connect(path):
        with original_connect(path) as conn:
            if path == old:
                entered.set()
                assert release.wait(5)
            yield conn

    monkeypatch.setattr(reader, '_connect', slow_connect)
    results = []
    thread = threading.Thread(target=lambda: results.append(reader.get_metadata('10000.jpg')), daemon=True)
    thread.start()
    try:
        assert entered.wait(3)
        reader.set_metadata_path(str(new))
    finally:
        release.set()
        thread.join(5)
    assert results == [None]
    assert reader.get_metadata('10000.jpg')['meta']['title'] == 'new'


def test_deleted_database_is_not_recreated(tmp_path):
    path = make_metadata(tmp_path / 'removed.db')
    reader = PixivMetadataReader(str(path))
    path.unlink()
    assert reader.get_metadata('10000.jpg') is None
    assert not path.exists()


def test_tags_handle_json_unicode_case_and_exact_matches(tmp_path):
    reader = PixivMetadataReader(str(make_metadata(tmp_path / 'pixiv.db')))
    assert reader.find_illust_ids_by_tag('猫') == {'10000', '10002'}
    assert reader.find_illust_ids_by_tag('ai生成') == {'10000', '10002'}
    assert reader.find_illust_ids_by_tag('ma') == set()
    assert set(reader.get_all_tags()) == {'猫', 'AI生成', 'maid'}


def test_shared_factory_and_same_path_does_not_reset_cache(tmp_path):
    assert get_pixiv_reader() is get_pixiv_reader()
    reader = PixivMetadataReader(str(make_metadata(tmp_path / 'pixiv.db')))
    metadata = reader.get_metadata('10000.jpg')
    reader.set_metadata_path(reader.metadata_path)
    assert reader.get_metadata('10000.jpg') is metadata

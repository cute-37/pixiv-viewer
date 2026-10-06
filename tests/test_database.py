import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from utils.database import DatabaseManager


def test_constructor_does_not_create_database(tmp_path):
    manager = DatabaseManager(tmp_path / 'unused' / 'library.db')
    assert not manager.db_path.exists()
    assert not hasattr(manager._local, 'conn')


def test_large_rating_batch_is_chunked_and_read_only(library):
    paths = [f'image-{i}.jpg' for i in range(2200)]
    library.batch_set_ratings([(path, i % 5 + 1) for i, path in enumerate(paths)])
    statements = []
    library.get_connection().set_trace_callback(statements.append)
    ratings = library.batch_get_ratings(paths + [paths[0], 'missing.jpg'])
    assert len(ratings) == 2200
    assert ratings[paths[1099]] == 5
    assert sum(statement.startswith('SELECT path, rating') for statement in statements) == 3
    assert library.get_connection().execute('SELECT COUNT(*) FROM images').fetchone()[0] == 2200


def test_independent_databases_and_connection_reopen(tmp_path):
    first = DatabaseManager(tmp_path / 'a.db')
    second = DatabaseManager(tmp_path / 'b.db')
    try:
        first.set_rating('x.jpg', 4)
        assert second.get_rating('x.jpg') == 0
        conn = first.get_connection()
        first.close_thread_connection()
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute('SELECT 1')
        assert first.get_rating('x.jpg') == 4
    finally:
        first.close_thread_connection()
        second.close_thread_connection()


def test_concurrent_ratings_and_tags(library):
    def write(i):
        try:
            path = f'{i}.jpg'
            library.set_rating(path, i % 5 + 1)
            library.add_tags([path], ['shared'])
            return library.get_rating(path)
        finally:
            library.close_thread_connection()

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(write, range(60))) == [i % 5 + 1 for i in range(60)]
    assert library.get_tag_usage() == [{'name': 'shared', 'count': 60}]


def test_batch_failure_rolls_back_and_releases_write_transaction(library):
    conn = library.get_connection()
    conn.execute("CREATE TRIGGER reject_bad BEFORE INSERT ON images WHEN NEW.path = 'bad.jpg' BEGIN SELECT RAISE(ABORT, 'rejected'); END")
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match='rejected'):
        library.add_tags(['good.jpg', 'bad.jpg'], ['partial'])
    assert not conn.in_transaction
    assert conn.execute('SELECT COUNT(*) FROM images').fetchone()[0] == 0
    assert library.get_all_tags() == []
    library.add_tags(['after.jpg'], ['after'])
    assert library.get_tags('after.jpg') == ['after']


def test_renaming_missing_tag_is_noop(library):
    library.rename_tag('missing', 'new')
    assert library.get_all_tags() == []
    assert not library.get_connection().in_transaction

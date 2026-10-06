"""数据库结构版本：新库、没有版本号的旧库、逐步升级、失败回滚、比程序新的库"""
import sqlite3

import pytest

from utils.schema import migrate, version_of
from webapp.store import SCHEMA, WebStore

V1 = "CREATE TABLE IF NOT EXISTS t (a TEXT)"
V2 = "ALTER TABLE t ADD COLUMN b TEXT; CREATE INDEX t_b ON t(b)"


def columns(conn, table="t"):
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]


def test_new_database_gets_all_steps(tmp_path):
    conn = sqlite3.connect(tmp_path / "a.db")
    assert migrate(conn, [V1, V2]) == 2
    assert version_of(conn) == 2 and columns(conn) == ["a", "b"]
    assert migrate(conn, [V1, V2]) == 2          # 再打开一次什么都不做


def test_unversioned_old_database_keeps_data(tmp_path):
    conn = sqlite3.connect(tmp_path / "a.db")
    conn.execute("CREATE TABLE t (a TEXT)")
    conn.execute("INSERT INTO t VALUES ('keep')")
    conn.commit()
    assert version_of(conn) == 0
    migrate(conn, [V1, V2])
    assert conn.execute("SELECT a, b FROM t").fetchall() == [("keep", None)]


def test_upgrade_runs_only_missing_steps(tmp_path):
    conn = sqlite3.connect(tmp_path / "a.db")
    migrate(conn, [V1])
    calls = []
    migrate(conn, [lambda c: calls.append("v1"), lambda c: calls.append("v2")])
    assert calls == ["v2"] and version_of(conn) == 2


def test_failed_step_rolls_back_and_keeps_version(tmp_path):
    conn = sqlite3.connect(tmp_path / "a.db")
    migrate(conn, [V1])

    def broken(c):
        c.execute("ALTER TABLE t ADD COLUMN b TEXT")
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        migrate(conn, [V1, broken])
    assert version_of(conn) == 1 and columns(conn) == ["a"]


def test_newer_database_is_left_alone(tmp_path):
    conn = sqlite3.connect(tmp_path / "a.db")
    migrate(conn, [V1, V2])
    assert migrate(conn, [V1]) == 2
    assert columns(conn) == ["a", "b"]


def test_webstore_stamps_version_and_upgrades_old_file(tmp_path):
    path = tmp_path / "webapp.db"
    old = sqlite3.connect(path)                    # 模拟加版本号之前的数据库：只有部分表，没有版本号
    old.execute("CREATE TABLE favorites (key TEXT PRIMARY KEY, added REAL)")
    old.execute("INSERT INTO favorites VALUES ('w1', 1.0)")
    old.commit()
    old.close()
    store = WebStore(path, tmp_path / "ui.json")
    assert store.favorites() == {"w1"}
    assert store.folders() == []
    check = sqlite3.connect(path)
    assert version_of(check) == len(SCHEMA)
    check.close()
    store.close_thread_connection()


def test_library_database_is_versioned(library):
    library.add_tags(["a.jpg"], ["x"])
    conn = sqlite3.connect(library.db_path)
    assert version_of(conn) == 1
    conn.close()

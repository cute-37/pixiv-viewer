"""备份只存在本机的数据（评分、标签、收藏、置顶、画师文件夹），并能从备份恢复"""
import json
import os
import sqlite3
import time
import zipfile
from contextlib import closing

import pytest

from webapp import backup, importer
from webapp.store import WebStore


@pytest.fixture
def data(tmp_path):
    from utils.database import DatabaseManager as Database
    store = WebStore(tmp_path / "webapp.db", tmp_path / "web_ui.json")
    store.set_favorite(["100", "200"], True)
    store.mark_viewed("100")
    store.set_pin(r"D:\Pixiv\[1] Alice", True)
    fid = store.folder_create("风景")
    store.folder_set(fid, [r"D:\Pixiv\[1] Alice", r"D:\Pixiv\[2] Bob"], True)
    store.set_dims([("x.png", 1.0, 10, 3, 4)])                            # 缓存：不该进备份
    store.save_settings({"lang": "zh", "tile": 180})
    db = Database(tmp_path / "library.db")
    db.set_rating(r"D:\Pixiv\[1] Alice\100_p0.png", 4)
    db.add_tag(r"D:\Pixiv\[1] Alice\100_p0.png", "壁纸候选")
    yield tmp_path, store, db
    store.close_thread_connection()


def test_backup_contains_user_data_but_not_caches(data):
    tmp, store, db = data
    out = backup.create(tmp / "out" / "b.zip", store.db_path, db.db_path, store.settings_path)
    assert out["counts"] == {"favorites": 2, "views": 1, "pins": 1, "folders": 1, "rated": 1, "tagged": 1}
    with zipfile.ZipFile(out["path"]) as z:
        assert sorted(z.namelist()) == ["library.db", "manifest.json", "web_ui.json", "webapp.db"]
        z.extract("webapp.db", tmp / "x")
        assert json.loads(z.read("web_ui.json"))["tile"] == 180
    with closing(sqlite3.connect(tmp / "x" / "webapp.db")) as con:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"favorites", "views", "pins", "folders", "folder_artists"} <= tables and not tables & {"dims", "scans"}
    assert backup.read_manifest(out["path"])["counts"]["favorites"] == 2
    assert store.get_dims([("x.png", 1.0, 10)]) == {"x.png": (3, 4)}     # 原来的数据库没被动


def test_restore_merges_into_a_fresh_install_without_losing_anything(data, tmp_path_factory):
    from utils.database import DatabaseManager as Database
    tmp, store, db = data
    out = backup.create(tmp / "b.zip", store.db_path, db.db_path, store.settings_path)
    fresh = tmp_path_factory.mktemp("fresh")
    new_store = WebStore(fresh / "webapp.db", fresh / "ui.json")
    new_store.set_favorite(["999"], True)                                 # 新装之后自己又收藏了一个
    fid = new_store.folder_create("风景")
    new_store.folder_set(fid, [r"D:\Pixiv\[3] Carol"], True)
    new_db = Database(fresh / "library2.db")
    items = importer.inspect(out["path"])
    kinds = {i["kind"]: i for i in items}
    assert set(kinds) == {"viewer_db", "ratings_db"} and all(i["ok"] for i in items)
    r1 = importer.merge_viewer_db(kinds["viewer_db"]["path"], new_store)
    r2 = importer.merge_ratings_db(kinds["ratings_db"]["path"], new_db)
    assert r1["ok"] and r2["ok"] and "1 个画师文件夹" in r1["message"]
    assert new_store.favorites() == {"100", "200", "999"}                 # 备份里的回来了，新收藏的还在
    folders = new_store.folders()
    assert len(folders) == 1 and set(folders[0]["artists"]) == {r"D:\Pixiv\[1] Alice", r"D:\Pixiv\[2] Bob", r"D:\Pixiv\[3] Carol"}
    assert new_db.get_rating(r"D:\Pixiv\[1] Alice\100_p0.png") == 4
    assert new_db.get_tags(r"D:\Pixiv\[1] Alice\100_p0.png") == ["壁纸候选"]
    new_store.close_thread_connection()


def test_other_zip_files_are_not_mistaken_for_backups(tmp_path):
    other = tmp_path / "photos.zip"
    with zipfile.ZipFile(other, "w") as z:
        z.writestr("a.txt", "hi")
    assert backup.read_manifest(other) is None
    items = importer.inspect(str(other))
    assert len(items) == 1 and not items[0]["ok"] and "不是这个软件做的备份" in items[0]["problem"]


def test_auto_backup_runs_weekly_and_keeps_only_recent_ones(data):
    tmp, store, db = data
    folder = tmp / "backups"
    args = (store.db_path, db.db_path, store.settings_path)
    first = backup.auto_backup(folder, *args)
    assert first and backup.auto_backup(folder, *args) is None            # 刚做过：不重复
    old = time.time() - (backup.AUTO_EVERY_DAYS + 1) * 86400
    os.utime(first["path"], (old, old))
    assert backup.auto_due(folder) and backup.auto_backup(folder, *args)  # 过了一周：再做一份
    for i in range(10):                                                   # 攒了很多份：只留最近几份
        p = folder / f"{backup.PREFIX}2020010{i}-000000.zip"
        p.write_bytes(b"x")
        os.utime(p, (old - i * 86400, old - i * 86400))
    backup.auto_backup(folder, *args, force=True)
    assert len(backup.list_auto(folder)) == backup.AUTO_KEEP

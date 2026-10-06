# -*- coding: utf-8 -*-
"""导入已有数据：按内容识别（改过名也认得）、替换时保留旧的、评分与收藏是合并"""
import json
import sqlite3
from contextlib import closing

import pytest

from webapp import importer


def _works_db(path, works=3, artists=2):
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as c:
        c.execute("CREATE TABLE artists (author_id INTEGER PRIMARY KEY, author_name TEXT)")
        c.execute("CREATE TABLE illust_metadata (illust_id INTEGER PRIMARY KEY, author_id INTEGER, title TEXT)")
        c.execute("CREATE TABLE illusts (task_key TEXT PRIMARY KEY, illust_id INTEGER, status INTEGER)")
        c.executemany("INSERT INTO artists VALUES (?, ?)", [(i, f"a{i}") for i in range(artists)])
        c.executemany("INSERT INTO illust_metadata VALUES (?, 0, 't')", [(i,) for i in range(works)])
        c.commit()
    return path


def _viewer_db(path):
    with closing(sqlite3.connect(path)) as c:
        c.executescript("CREATE TABLE favorites (key TEXT PRIMARY KEY, added REAL); CREATE TABLE views (key TEXT PRIMARY KEY, ts REAL);"
                        "CREATE TABLE pins (artist TEXT PRIMARY KEY, added REAL); CREATE TABLE dims (path TEXT PRIMARY KEY);")
        c.execute("INSERT INTO favorites VALUES ('111', 1.0)")
        c.execute("INSERT INTO views VALUES ('111', 50.0)")
        c.execute("INSERT INTO pins VALUES ('X', 1.0)")
        c.commit()
    return path


def _ratings_db(path):
    with closing(sqlite3.connect(path)) as c:
        c.executescript("CREATE TABLE images (id INTEGER PRIMARY KEY, path TEXT UNIQUE, rating INTEGER DEFAULT 0);"
                        "CREATE TABLE tags (id INTEGER PRIMARY KEY, name TEXT UNIQUE);"
                        "CREATE TABLE image_tags (image_id INTEGER, tag_id INTEGER, PRIMARY KEY (image_id, tag_id));")
        c.execute("INSERT INTO images VALUES (1, 'D:/a.png', 4)")
        c.execute("INSERT INTO images VALUES (2, 'D:/b.png', 0)")
        c.execute("INSERT INTO tags VALUES (1, '壁纸')")
        c.execute("INSERT INTO image_tags VALUES (2, 1)")
        c.commit()
    return path


def _settings(path, accounts=("main",)):
    path.write_text(json.dumps({"current": {"TOKENS": {n: {"token": "t"} for n in accounts}, "STORAGE_MODE": "smb"}}), encoding="utf-8")
    return path


def _avatars(folder, ids=(1, 2, 3)):
    folder.mkdir(parents=True, exist_ok=True)
    for i in ids:
        (folder / f"{i}.png").write_bytes(b"png")
    return folder


def test_files_are_recognised_by_content_not_by_name(tmp_path):
    kinds = {
        "备份-随便改的名字.bak": ("works_db", _works_db),
        "我的收藏.dat": ("viewer_db", _viewer_db),
        "stars.sqlite": ("ratings_db", _ratings_db),
        "账号.txt": ("dl_settings", _settings),
    }
    for name, (kind, make) in kinds.items():
        item = importer.inspect(str(make(tmp_path / name)))[0]
        assert item["ok"] and item["kind"] == kind, name
    assert importer.inspect(str(tmp_path / "备份-随便改的名字.bak"))[0]["detail"] == "2 位画师 · 3 个作品"
    assert importer.inspect(str(_avatars(tmp_path / "头像们")))[0]["kind"] == "avatars"
    # 认不出来的：说明原因，不会被导入
    (tmp_path / "note.json").write_text('{"hello": 1}', encoding="utf-8")
    (tmp_path / "photo.png").write_bytes(b"\x89PNG....")
    other = tmp_path / "other.db"
    with closing(sqlite3.connect(other)) as c:
        c.execute("CREATE TABLE something (x)")
    for name in ("note.json", "photo.png", "other.db", "不存在.db"):
        item = importer.inspect(str(tmp_path / name))[0]
        assert not item["ok"] and item["problem"], name


def test_whole_old_folder_is_expanded(tmp_path):
    old = tmp_path / "pixiv_downloader"
    _works_db(old / "db" / "pixiv_manager.db", works=9)
    _works_db(old / "db" / "small_backup.db", works=1)          # 同一种取最大的那个
    _settings(old / "settings.json")
    _avatars(old / "avatars")
    found = {i["kind"]: i for i in importer.inspect(str(old))}
    assert set(found) == {"works_db", "dl_settings", "avatars"}
    assert found["works_db"]["name"] == "pixiv_manager.db"


def test_import_replaces_but_keeps_the_old_copy(tmp_path):
    home = tmp_path / "home"
    _works_db(home / "db" / "pixiv_manager.db", works=1)
    _settings(home / "settings.json", accounts=("old",))
    _avatars(home / "avatars", ids=(9,))
    src = tmp_path / "src"
    src.mkdir()
    items = [importer.inspect(str(_works_db(src / "renamed.db", works=7)))[0],
             importer.inspect(str(_settings(src / "s.json", accounts=("a", "b"))))[0],
             importer.inspect(str(_avatars(src / "pics")))[0]]
    results = importer.import_download_data(items, home)
    assert all(r["ok"] for r in results) and all(r["kept"] for r in results)
    # 新的到位了
    assert importer.describe_home(home)["works_db"]["detail"] == "2 位画师 · 7 个作品"
    assert importer.describe_home(home)["dl_settings"]["detail"].startswith("2 个账号")
    assert sorted(p.name for p in (home / "avatars").iterdir()) == ["1.png", "2.png", "3.png"]
    # 旧的还在原地，只是改了名
    kept_db = [p for p in (home / "db").iterdir() if ".旧-" in p.name]
    assert len(kept_db) == 1 and importer.inspect(str(kept_db[0]))[0]["detail"] == "2 位画师 · 1 个作品"
    assert [p for p in home.iterdir() if p.name.startswith("settings.旧-")]
    assert [p for p in home.iterdir() if p.name.startswith("avatars.旧-") and (p / "9.png").is_file()]
    # 用户选的原文件没有被动过
    assert (src / "renamed.db").is_file() and (src / "s.json").is_file() and (src / "pics" / "1.png").is_file()


def test_viewer_data_is_merged_not_replaced(tmp_path, library):
    from webapp.store import WebStore
    store = WebStore(tmp_path / "webapp.db", tmp_path / "ui.json")
    store.set_favorite(["222"], True)
    store.mark_viewed("111")
    database = library
    database.batch_set_ratings([("D:/c.png", 2)])
    try:
        r = importer.merge_viewer_db(str(_viewer_db(tmp_path / "old-webapp.db")), store)
        assert r["ok"] and store.favorites() == {"111", "222"} and store.pins() == {"X"}
        assert store.views()["111"] > 50.0                         # 已有的更新的时间保留
        r = importer.merge_ratings_db(str(_ratings_db(tmp_path / "old-library.db")), database)
        assert r["ok"]
        assert database.batch_get_ratings(["D:/a.png", "D:/c.png"]) == {"D:/a.png": 4, "D:/c.png": 2}
        assert database.get_tags("D:/b.png") == ["壁纸"]
    finally:
        store.close_thread_connection()


@pytest.mark.parametrize("kind", importer.KINDS)
def test_every_kind_has_a_description(kind):
    info = importer.KINDS[kind]
    assert info["label"] and info["was"] and info["what"] and info["need"] in ("核心", "可选")

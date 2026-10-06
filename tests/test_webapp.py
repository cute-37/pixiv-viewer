"""新界面的 Python 后端：资料库索引、前端接口、本机图片服务（不需要打开窗口）"""
import json
import sqlite3
import urllib.error
import urllib.request
from contextlib import closing
from urllib.parse import quote

import pytest

PIL = pytest.importorskip("PIL.Image")


def _img(path, size=(60, 80), color=(200, 120, 90)):
    path.parent.mkdir(parents=True, exist_ok=True)
    PIL.new("RGB", size, color).save(path)


@pytest.fixture
def setup(tmp_path, library):
    from utils.config_manager import ConfigManager
    from utils.pixiv_metadata_reader import PixivMetadataReader
    from webapp.api import Api
    from webapp.store import WebStore

    root = tmp_path / "Pixiv"
    artist = root / "[777] あおい凪"
    _img(artist / "100001_p0.png", (60, 80))
    _img(artist / "100001_p1.png", (60, 80))
    _img(artist / "100002_p0.jpg", (120, 60))
    _img(artist / "sub" / "sketch.png", (50, 50))
    _img(root / "loose.png", (40, 40))

    meta_db = tmp_path / "pixiv.db"
    with closing(sqlite3.connect(meta_db)) as conn:
        conn.execute("CREATE TABLE illust_metadata (illust_id INTEGER PRIMARY KEY, author_id INTEGER, title TEXT, "
                     "tags TEXT, ai_type INTEGER, x_restrict INTEGER, create_date TEXT, caption TEXT)")
        conn.execute("INSERT INTO illust_metadata VALUES (100001, 777, '夏の終わり', ?, 1, 0, '2025-08-30T12:00:00+09:00', '説明<br />二行目')",
                     (json.dumps(["風景", "オリジナル"]),))
        conn.execute("INSERT INTO illust_metadata VALUES (100002, 777, '星空', ?, 2, 1, '2025-09-02', '')",
                     (json.dumps(["星空", "オリジナル"]),))
        conn.commit()

    cm = ConfigManager(tmp_path / "config.json")
    cm.config.image_paths = [str(root)]
    cm.config.pixiv_metadata_path = str(meta_db)
    reader = PixivMetadataReader(tag_index_dir=tmp_path / "tag-index")
    store = WebStore(tmp_path / "webapp.db", tmp_path / "web_ui.json")
    api = Api(cm, reader, library, store=store, token="tok")
    yield api, root, artist
    store.close_thread_connection()


def test_library_groups_pages_and_reads_metadata(setup):
    api, root, artist = setup
    lib = api.get_library()
    names = {a["name"]: a for a in lib["artists"]}
    assert "あおい凪" in names and names["あおい凪"]["id"] == 777
    assert "Pixiv" in names                                   # 根目录里直接放的图片
    res = api.list_works({"scope": "artist", "artist": str(artist), "sort": "id"})
    works = res["works"]
    assert [w["pid"] for w in works][:2] == [100002, 100001]   # 按作品 ID 从新到旧
    multi = next(w for w in works if w["pid"] == 100001)
    assert len(multi["pages"]) == 2 and multi["title"] == "夏の終わり" and multi["month"] == "2025-08"
    star = next(w for w in works if w["pid"] == 100002)
    assert star["rating"] == "r18" and star["ai"] is True and star["ar"] == 2.0
    assert any(w["title"] == "sketch" for w in works)          # 子文件夹也会扫描；非 Pixiv 文件名单独成作品
    assert dict(res["scopeTags"])["オリジナル"] == 2
    flat = api.list_works({"scope": "artist", "artist": str(artist), "mergePages": False})["works"]
    assert len(flat) == len(works) + 1 and any(w["key"] == "100001#1" for w in flat)


def test_filters_search_and_rating(setup):
    api, root, artist = setup
    q = {"scope": "all", "sort": "id"}
    assert {w["pid"] for w in api.list_works({**q, "tags": ["星空"]})["works"]} == {100002}
    assert {w["pid"] for w in api.list_works({**q, "rating": "safe"})["works"]} >= {100001}
    assert 100002 not in {w["pid"] for w in api.list_works({**q, "rating": "safe"})["works"]}
    assert {w["pid"] for w in api.list_works({**q, "q": "夏の"})["works"]} == {100001}
    assert {w["pid"] for w in api.list_works({**q, "filters": {"ai": "exclude"}})["works"]} == {100001, 0}
    assert {w["pid"] for w in api.list_works({**q, "filters": {"multiPage": True}})["works"]} == {100001}
    s = api.suggest("星")
    assert s["tags"][0][0] == "星空" and s["works"][0]["pid"] == 100002


def test_user_data_persists(setup):
    api, root, artist = setup
    api.list_works({"scope": "all"})
    api.set_favorite(["100001"], True)
    api.set_stars(["100001"], 4)
    api.add_tags(["100001"], ["壁纸候选"])
    api.mark_viewed("100002#0")
    fav = api.list_works({"scope": "fav"})["works"]
    assert [w["pid"] for w in fav] == [100001] and fav[0]["stars"] == 4
    assert [w["pid"] for w in api.list_works({"scope": "recent"})["works"]] == [100002]
    assert api.list_works({"scope": "all", "tags": ["壁纸候选"]})["works"][0]["pid"] == 100001
    details = api.get_details("100001")
    assert details["myTags"] == ["壁纸候选"] and details["caption"] == "説明\n二行目"
    api.remove_tags(["100001"], ["壁纸候选"])
    assert api.get_details("100001")["myTags"] == []
    api.save_config({"font": "wenkai", "tile": 200})
    assert api.get_config()["tile"] == 200
    api.pin_artist(str(artist), True)
    assert next(a for a in api.get_library()["artists"] if a["key"] == str(artist))["pinned"]


def test_media_server_requires_token_and_library_paths(setup, tmp_path):
    from webapp.server import MediaServer
    api, root, artist = setup
    server = MediaServer("tok", api._allowed)
    server.start()
    try:
        img = str(artist / "100001_p0.png")

        def get(url):
            with urllib.request.urlopen(server.url + url, timeout=5) as r:
                return r.status, r.headers.get("Content-Type"), r.read()

        status, ctype, data = get(f"/thumb?t=tok&path={quote(img)}")
        assert status == 200 and ctype == "image/jpeg" and data[:2] == b"\xff\xd8"
        status, ctype, _ = get(f"/image?t=tok&path={quote(img)}")
        assert ctype == "image/png"
        outside = tmp_path / "secret.png"
        _img(outside)
        for bad in (f"/thumb?t=wrong&path={quote(img)}", f"/image?t=tok&path={quote(str(outside))}"):
            with pytest.raises(urllib.error.HTTPError) as e:
                get(bad)
            assert e.value.code == 403
        status, ctype, html = get("/index.html")
        assert html.startswith(b"<!doctype html>") and b"__PV_TOKEN__" in html and "charset=utf-8" in ctype
        with pytest.raises(urllib.error.HTTPError):
            get("/../web_main.py")
    finally:
        server.stop()


def test_media_server_json_api(setup):
    import json
    from webapp.server import MediaServer
    api, *_ = setup
    server = MediaServer("tok", api._allowed, api=api)
    server.start()
    try:
        def post(name, args, token="tok"):
            req = urllib.request.Request(server.url + "/api/" + name, data=json.dumps(args).encode(), method="POST",
                                         headers={"Content-Type": "application/json", "X-PV-Token": token})
            with urllib.request.urlopen(req, timeout=5) as r:
                return json.loads(r.read().decode("utf-8"))

        res = post("list_works", [{"scope": "all"}])
        assert [w["pid"] for w in res["works"]] == [w["pid"] for w in api.list_works({"scope": "all"})["works"]]
        assert post("get_details", ["100001"])["pid"] == 100001
        # 令牌不对、或者不在只读白名单里的方法都会被拒绝
        for name, token, code in (("list_works", "wrong", 403), ("set_stars", "tok", 404), ("_store", "tok", 404)):
            with pytest.raises(urllib.error.HTTPError) as e:
                post(name, [], token)
            assert e.value.code == code
    finally:
        server.stop()


def test_api_does_not_expose_internals(setup):
    api, *_ = setup
    public = [n for n in dir(api) if not n.startswith("_")]
    # pywebview 会把公开属性递归暴露给网页：只能是给前端用的方法
    assert all(callable(getattr(api, n)) for n in public)
    assert "list_works" in public and "window" not in public


def test_indexing_can_defer_image_dimensions(setup):
    api, root, artist = setup
    lib = api._library
    a = next(x for x in lib.artists() if x.key == str(artist))
    with api._store._conn() as conn:
        conn.execute("DELETE FROM dims")
    # 后台索引时不逐个打开文件读宽高，之后由 fill_dims 补上
    works = lib.scan_artist(a, force=True, read_dims=False)
    assert works and all(p.w == 0 for w in works for p in w.pages)
    lib.fill_dims()
    assert all(p.w and p.h for w in lib.scan_artist(a) for p in w.pages)


def test_list_works_packed_format(setup):
    import os
    api, root, artist = setup
    plain = api.list_works({"scope": "all"})["works"]
    res = api.list_works({"scope": "all", "packed": 1})
    assert res["packed"] and len(res["works"]) == len(plain)
    for row, w in zip(res["works"], plain):
        key, pid, title, ai, *_rest, pages = row
        a_key, a_name, a_id = res["artists"][ai]
        assert (key or str(pid)) == w["key"] and a_key == w["artistKey"] and a_name == w["artistName"]
        assert (title or os.path.splitext(w["pages"][0]["file"])[0]) == w["title"]
        # 页面路径只给相对画师文件夹的部分
        assert [os.path.join(a_key, p[0]) for p in pages] == [p["path"] for p in w["pages"]]


def test_same_artist_in_several_folders_is_merged(setup):
    api, root, artist = setup
    # 同一位画师的另外两个文件夹：一个名字是 Unknown，一个名字里没有画师 ID（靠里面的作品认出来）
    _img(root / "[777] Unknown" / "100001_p0.png", (60, 80))      # 和主文件夹重复的文件
    _img(root / "[777] Unknown" / "100003_p0.png", (60, 80))
    _img(root / "_AOI~1" / "100002_p0.jpg", (120, 60))            # 重复
    _img(root / "_AOI~1" / "100004_p0.png", (60, 80))
    api._library.invalidate()
    artists = [a for a in api._library.artists() if not a.loose]
    assert len(artists) == 1 and artists[0].key == str(artist) and len(artists[0].extra) == 2
    works = api.list_works({"scope": "artist", "artist": str(artist), "sort": "id"})["works"]
    assert sorted(w["pid"] for w in works if w["pid"]) == [100001, 100002, 100003, 100004]
    assert len(next(w for w in works if w["pid"] == 100001)["pages"]) == 2      # 重复的那一页没有多算
    keys = [w["key"] for w in api.list_works({"scope": "all"})["works"]]
    assert len(keys) == len(set(keys))
    info = next(a for a in api.get_library()["artists"] if a["key"] == str(artist))
    assert info["folders"] == 3 and info["name"] == "あおい凪"


def test_artist_folders_group_artists(setup):
    api, root, artist = setup
    other = root / "[888] 白川ルカ"
    _img(other / "200001_p0.png", (60, 80))
    api._library.invalidate()
    fid = api.folder_create("  风景  ", [str(artist)])
    assert api.folder_create("   ") is None                        # 名字不能是空的
    lib = api.get_library()
    assert lib["folders"] == [{"id": fid, "name": "风景", "artists": [str(artist)]}]
    pids = lambda: sorted(w["pid"] for w in api.list_works({"scope": "folder", "folder": fid})["works"] if w["pid"])  # noqa: E731
    assert pids() == [100001, 100002]
    api.folder_set(fid, [str(other)], True)                        # 一位画师可以在多个文件夹里；重复加入没有影响
    api.folder_set(fid, [str(other)], True)
    assert pids() == [100001, 100002, 200001]
    api.folder_set(fid, [str(artist)], False)
    assert pids() == [200001]
    api.folder_rename(fid, "人物")
    assert api.get_library()["folders"][0]["name"] == "人物"
    api.folder_delete(fid)                                         # 删除文件夹不影响画师和图片
    assert api.get_library()["folders"] == [] and pids() == []
    assert len([a for a in api.get_library()["artists"] if a["id"]]) == 2


def test_suggestions_can_leave_out_r18(setup):
    api, root, artist = setup
    api.list_works({"scope": "all"})                               # 先让资料库扫描一遍
    assert [w["pid"] for w in api.suggest("星空")["works"]] == [100002]     # 100002 是 R18
    assert api.suggest("星空", True)["works"] == [] and api.suggest("星空", True)["tags"] == []
    assert [w["pid"] for w in api.suggest("夏", True)["works"]] == [100001]


def test_thumbnail_cache_is_pruned_and_can_be_cleared(setup, monkeypatch):
    import os
    import time
    from utils import thumbnail_cache
    from webapp import api as api_module
    api, *_ = setup
    cache = api_module.CACHE_DIR
    cache.mkdir(parents=True, exist_ok=True)
    for old in cache.glob("*.jpg"):
        old.unlink()
    for name, size, age_days in (("fresh.jpg", 600, 1), ("stale.jpg", 600, 200), ("mid.jpg", 600, 10)):
        (cache / name).write_bytes(b"x" * size)
        t = time.time() - age_days * 86400
        os.utime(cache / name, (t, t))
    info = api.cache_info()
    assert info["size"] == 1800 and info["maxAgeDays"] == 90 and info["maxBytes"] == 500 * 1024 * 1024
    assert api.get_library()["cache"]["size"] == 1800

    monkeypatch.setattr(thumbnail_cache, "DEFAULT_MAX_BYTES", 1000)       # 启动时的清理：太旧的删掉，再压到上限以内
    monkeypatch.setattr(thumbnail_cache.prune, "__defaults__", (1000, 90, None))
    api._prune_cache()
    assert sorted(p.name for p in cache.glob("*.jpg")) == ["fresh.jpg"]   # 200 天没用的删掉；超量时先删最久没用的
    assert api.cache_info()["size"] == 600

    assert api.clear_cache() == {"removed": 1, "freed": 600}
    assert api.cache_info()["size"] == 0

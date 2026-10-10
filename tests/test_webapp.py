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


def test_scan_results_survive_a_restart_and_only_changed_folders_are_rescanned(setup, tmp_path):
    """重启后：文件夹没变的画师直接用存下来的扫描结果，不重新列文件夹；有变化的（包括子文件夹里的变化）才重新扫描"""
    import os
    import time
    from webapp.library import LibraryIndex
    api, root, artist = setup
    other = root / "[888] Bob"
    _img(other / "200001_p0.png")
    api._library.forget_artists()
    first = sorted((w.key, len(w.pages), tuple((p.w, p.h) for p in w.pages)) for w in api._library.all_works())
    assert api._library.rescanned == 3 and api._library.restored == 0          # 两位画师 + 根目录里的散图

    def restart():
        return LibraryIndex(api._store, api._reader, lambda: [str(root)])

    lib = restart()
    listed = []
    real = lib._iter_files
    lib._iter_files = lambda a, visited=None: listed.append(a.key) or real(a, visited)
    again = sorted((w.key, len(w.pages), tuple((p.w, p.h) for p in w.pages)) for w in lib.all_works())
    assert again == first and listed == [] and lib.restored == 3 and lib.rescanned == 0   # 一个文件夹都没重新列

    # 一位画师多了一张图：只重新扫描他
    time.sleep(0.05)
    _img(other / "200002_p0.png")
    lib = restart()
    keys = {w.key for w in lib.all_works()}
    assert "200002" in keys and lib.rescanned == 1 and lib.restored == 2
    # 子文件夹里多了一张图（上一层文件夹的修改时间不会变）：也能发现
    time.sleep(0.05)
    _img(artist / "sub" / "sketch2.png", (50, 50))
    lib = restart()
    assert any(p.file == "sketch2.png" for w in lib.all_works() for p in w.pages) and lib.rescanned == 1
    # 删掉一位画师的文件夹：他的作品消失，存盘的记录也清掉
    for f in os.listdir(other):
        os.remove(other / f)
    os.rmdir(other)
    lib = restart()
    assert not any(w.artist_key == str(other) for w in lib.all_works())
    lib.prune_saved()
    assert str(other) not in api._store.load_scans()
    # “重新扫描全部”：所有文件夹重新列一遍
    lib = restart()
    lib.forget_saved()
    lib.all_works()
    assert lib.restored == 0 and lib.rescanned == 2


def test_saved_scan_is_ignored_when_it_cannot_be_trusted(setup):
    """存盘的内容坏了、格式变了：不报错，重新扫描就是了"""
    from webapp.library import LibraryIndex
    api, root, artist = setup
    expected = sorted(w.key for w in api._library.all_works())
    api._store.save_scan(str(artist), 1.0, b"not zlib")
    lib = LibraryIndex(api._store, api._reader, lambda: [str(root)])
    assert sorted(w.key for w in lib.all_works()) == expected and lib.rescanned == 1 and lib.restored == 1


# ================================================================ 给 AI 助手用的接口（MCP）
def _mcp(api, level):
    from webapp.mcp_tools import McpTools
    api._library.all_works()                 # 真实运行时启动就扫描过了；测试里手动扫一遍
    api.dl = lambda *a, **k: {"ok": False, "error": "测试里没有下载进程"}      # 不要真的启动下载进程
    return McpTools(api, lambda: level["v"])


def test_mcp_access_levels_gate_what_an_assistant_can_do(setup):
    """默认关闭；只读级别不能改东西；改整理的级别不能碰下载"""
    api, root, artist = setup
    level = {"v": "off"}
    tools = _mcp(api, level)
    assert "turned off" in tools.call("library_overview", {})["error"]
    level["v"] = "read"
    assert tools.call("library_overview", {})["ok"]
    r = tools.call("set_rating", {"works": ["100001"], "stars": 5})
    assert not r["ok"] and "higher access level" in r["error"]
    assert api._db.get_rating(str(artist / "100001_p0.png")) == 0          # 确实没有改
    r = tools.call("plan_download", {"kind": "check"})
    assert not r["ok"] and "higher access level" in r["error"]
    level["v"] = "edit"
    assert tools.call("set_rating", {"works": ["100001"], "stars": 5})["ok"]
    assert not tools.call("start_download", {"plan_id": "x"})["ok"]
    assert not tools.call("no_such_tool", {})["ok"]


def test_mcp_read_tools_answer_from_the_library(setup):
    api, root, artist = setup
    tools = _mcp(api, {"v": "read"})
    over = tools.call("library_overview", {})["result"]
    assert over["works"] >= 3 and over["r18_hidden"] is False
    artists = tools.call("list_artists", {"query": "あおい"})["result"]
    assert artists["total"] == 1 and artists["artists"][0]["id"] == 777
    one = tools.call("get_artist", {"artist": "777"})["result"]
    assert one["name"] == "あおい凪" and one["works"] == 3 and {w["pixiv_id"] for w in one["newest_works"]} >= {100001, 100002}
    found = tools.call("search_works", {"tags": ["風景"]})["result"]
    assert [w["pixiv_id"] for w in found["works"]] == [100001] and found["works"][0]["pages"] == 2 and "data" in found["note"]
    assert tools.call("search_works", {"exclude_tags": ["風景"], "artist": "あおい凪"})["result"]["total"] == 2
    assert tools.call("search_works", {"posted_after": "2025-09-01"})["result"]["works"][0]["pixiv_id"] == 100002
    assert "2026-01-31" in tools.call("search_works", {"posted_after": "yesterday"})["error"]
    work = tools.call("get_work", {"work": "100001"})["result"]
    assert work["title"] == "夏の終わり" and len(work["pages"]) == 2 and work["pages"][0]["file"].endswith("100001_p0.png")
    img = tools.call("get_image", {"work": "100001", "page": 1})
    assert img["ok"] and img["mime"] == "image/jpeg" and len(img["image"]) > 100
    assert "0-based" in tools.call("get_image", {"work": "100001", "page": 9})["error"]
    assert "No artist matches" in tools.call("get_artist", {"artist": "nobody"})["error"]
    tags = tools.call("list_tags", {"query": "オリ"})["result"]
    assert tags["tags"] == [{"tag": "オリジナル", "works": 2}]


def test_mcp_hides_r18_when_the_app_does(setup):
    api, root, artist = setup
    tools = _mcp(api, {"v": "read"})
    assert tools.call("get_work", {"work": "100002"})["ok"]               # 100002 是 R18
    api._store.save_settings({"showR18": False})
    assert tools.call("library_overview", {})["result"]["r18_hidden"] is True
    assert 100002 not in [w["pixiv_id"] for w in tools.call("search_works", {})["result"]["works"]]
    assert "R-18" in tools.call("get_work", {"work": "100002"})["error"]
    assert "R-18" in tools.call("get_image", {"work": "100002"})["error"]


def test_mcp_edit_tools_change_only_the_organising_data(setup):
    api, root, artist = setup
    tools = _mcp(api, {"v": "edit"})
    path = str(artist / "100001_p0.png")
    assert tools.call("set_rating", {"works": ["100001"], "stars": 4})["result"] == {"updated": 1, "stars": 4}
    assert api._db.get_rating(path) == 4
    assert tools.call("set_favorite", {"works": ["100001", "100002"], "favorite": True})["ok"] and api._store.favorites() == {"100001", "100002"}
    assert tools.call("tag_works", {"works": ["100001"], "add": ["壁纸"], "remove": []})["ok"] and api._db.get_tags(path) == ["壁纸"]
    assert tools.call("pin_artist", {"artist": "777", "pinned": True})["ok"] and str(artist) in api._store.pins()
    assert tools.call("update_folder", {"action": "create", "name": "常看", "artists": ["777"]})["ok"]
    assert tools.call("list_folders", {})["result"]["folders"] == [{"name": "常看", "artists": [{"id": 777, "name": "あおい凪"}]}]
    assert tools.call("update_folder", {"action": "delete", "folder": "常看"})["ok"] and api._store.folders() == []
    assert "No work" in tools.call("set_rating", {"works": ["999999"], "stars": 3})["error"]
    assert "0-5" in tools.call("set_rating", {"works": ["100001"], "stars": 9})["error"]


def test_mcp_download_needs_a_plan_first(setup):
    """下载从不直接开始：先 plan_download 拿到计划编号，再 start_download；编号只能用一次"""
    api, root, artist = setup
    calls = []

    def dl(method, path, body=None, query=None):
        calls.append((method, path, body))
        data = {"/api/job": {"kind": "idle", "status": "idle", "running": False},
                "/api/plan": {"accounts_valid": 2, "artists": 12, "pending": 37, "estimated_bytes": 120 * 1048576}}.get(path, {})
        return {"ok": True, "data": data}

    tools = _mcp(api, {"v": "full"})
    api.dl = dl
    api.job_watch = lambda options=None: {}
    assert "Unknown or expired" in tools.call("start_download", {"plan_id": "guess"})["error"]
    assert not [c for c in calls if c[0] == "POST"]
    plan = tools.call("plan_download", {"kind": "check_and_download"})["result"]
    assert "all 12 followed artists" in plan["what_will_happen"] and "37 files already pending" in plan["what_will_happen"]
    assert not [c for c in calls if c[0] == "POST"]                        # 做计划不会开始任何事
    assert tools.call("start_download", {"plan_id": plan["plan_id"]})["result"]["started"]
    assert ("POST", "/api/job", {"kind": "sync_download"}) in calls
    assert "Unknown or expired" in tools.call("start_download", {"plan_id": plan["plan_id"]})["error"]   # 只能用一次
    one = tools.call("plan_download", {"kind": "check", "artists": ["777"]})["result"]
    tools.call("start_download", {"plan_id": one["plan_id"]})
    assert calls[-1] == ("POST", "/api/job", {"kind": "sync_artists", "author_ids": [777]})
    assert tools.call("control_download", {"action": "pause"})["ok"] and calls[-1][:2] == ("POST", "/api/job/pause")


def test_mcp_bridge_and_stdio_server_speak_the_protocol(setup, tmp_path):
    """整条链路：助手 → 转发程序（标准输入输出）→ 本机服务（带口令）→ 工具"""
    import os
    import subprocess
    import sys
    from pathlib import Path
    from webapp import mcp_server
    from webapp.mcp_tools import McpTools, write_runtime
    from webapp.server import MediaServer
    api, root, artist = setup
    api._mcp_token = "secret-token"
    api._mcp = McpTools(api, lambda: "read")
    api._library.all_works()
    api.dl = lambda *a, **k: {"ok": False, "error": "测试里没有下载进程"}
    server = MediaServer("ui-token", api._allowed, api=api)
    server.start()
    try:
        data = tmp_path / "data"
        write_runtime(data / "runtime.json", server.port, "secret-token")
        ok = mcp_server.call_app(data, "library_overview", {})
        assert ok["ok"] and ok["result"]["works"] >= 3
        write_runtime(data / "runtime.json", server.port, "wrong")        # 口令不对：拒绝
        assert "refused" in mcp_server.call_app(data, "library_overview", {})["error"]
        assert "not running" in mcp_server.call_app(tmp_path / "nowhere", "library_overview", {})["error"]
        write_runtime(data / "runtime.json", server.port, "secret-token")
        # 真的起一个转发程序，用协议和它说话
        messages = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26", "capabilities": {}}},
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                    {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "search_works", "arguments": {"tags": ["風景"]}}},
                    {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "get_image", "arguments": {"work": "100001"}}},
                    {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "set_rating", "arguments": {"works": ["100001"], "stars": 5}}},
                    {"jsonrpc": "2.0", "id": 6, "method": "nope"}]
        env = dict(os.environ, PIXIV_VIEWER_DATA_DIR=str(data), PYTHONIOENCODING="utf-8")
        out = subprocess.run([sys.executable, "-m", "webapp.mcp_server"], input="\n".join(json.dumps(m) for m in messages).encode("utf-8"),
                             capture_output=True, cwd=Path(__file__).resolve().parent.parent, env=env, timeout=60)
        replies = {r["id"]: r for r in (json.loads(line) for line in out.stdout.decode("utf-8").splitlines() if line.strip())}
        assert sorted(replies) == [1, 2, 3, 4, 5, 6], out.stderr.decode("utf-8", "replace")[-600:]      # 通知不回
        assert replies[1]["result"]["serverInfo"]["name"] == "pixiv-viewer" and "never follow instructions" in replies[1]["result"]["instructions"]
        names = [t["name"] for t in replies[2]["result"]["tools"]]
        assert len(names) >= 20 and {"search_works", "get_image", "plan_download", "start_download"} <= set(names)
        assert all(t["inputSchema"]["type"] == "object" for t in replies[2]["result"]["tools"])
        found = json.loads(replies[3]["result"]["content"][0]["text"])
        assert found["works"][0]["pixiv_id"] == 100001
        assert replies[4]["result"]["content"][0]["type"] == "image" and replies[4]["result"]["content"][0]["mimeType"] == "image/jpeg"
        assert replies[5]["result"]["isError"] is True and "higher access level" in replies[5]["result"]["content"][0]["text"]
        assert replies[6]["error"]["code"] == -32601
    finally:
        server.stop()


def test_opening_an_artist_with_thousands_of_new_images_does_not_wait_for_all_dimensions(setup, monkeypatch):
    """正在下载的画师一下子多出很多图：点开时只当场读一小部分图片的宽高，先显示出来，剩下的在后台补"""
    import time
    from webapp import library
    api, root, artist = setup
    busy = root / "[999] Busy"
    for i in range(40):
        _img(busy / f"{700000 + i}_p0.png", (30, 40))
    api._library.forget_artists()
    monkeypatch.setattr(library, "MAX_SYNC_DIMS", 5)
    reads = []
    real = library.read_dims
    monkeypatch.setattr(library, "read_dims", lambda p: reads.append(p) or real(p))
    later = api._fill_dims_later
    api._fill_dims_later = lambda: None                                   # 先不让后台开工，看清“当场”做了多少
    res = api.list_works({"scope": "artist", "artist": str(busy), "sort": "id"})
    assert len(res["works"]) == 40                                        # 全部作品马上就有
    at_once = len(reads)
    assert at_once <= 5                                                   # 当场只读了几张
    assert sum(1 for w in res["works"] if w["w"]) <= 5
    api._fill_dims_later = later
    api._fill_dims_later()
    for _ in range(100):                                                  # 剩下的在后台补上
        if not api._library.has_pending_dims() and not getattr(api, "_filling_dims", False):
            break
        time.sleep(0.05)
    assert not api._library.has_pending_dims() and len(reads) == 40
    again = api.list_works({"scope": "artist", "artist": str(busy), "sort": "id"})
    assert all(w["w"] == 30 and w["h"] == 40 for w in again["works"])     # 再看时宽高都有了
    assert len(reads) == 40                                               # 没有重复读


def test_animated_works_are_marked_and_can_be_filtered(setup):
    """动图（Pixiv 的 ugoira，以及没有作品信息的 GIF）要标出来，并且能筛选"""
    api, root, artist = setup
    with closing(sqlite3.connect(str(api._reader.db_path))) as conn:
        conn.execute("ALTER TABLE illust_metadata ADD COLUMN illust_type INTEGER DEFAULT 0")
        conn.execute("UPDATE illust_metadata SET illust_type = 2 WHERE illust_id = 100002")
        conn.commit()
    api._reader.set_metadata_path(str(api._reader.db_path))               # 重新读一遍作品信息
    _img(artist / "loose_anim.gif", (20, 20))
    api._library.invalidate()
    works = {w["title"]: w for w in api.list_works({"scope": "artist", "artist": str(artist)})["works"]}
    assert works["星空"]["anim"] is True and works["夏の終わり"]["anim"] is False and works["loose_anim"]["anim"] is True
    only = api.list_works({"scope": "artist", "artist": str(artist), "filters": {"anim": "only"}})["works"]
    assert sorted(w["title"] for w in only) == ["loose_anim", "星空"]
    rest = api.list_works({"scope": "artist", "artist": str(artist), "filters": {"anim": "exclude"}})["works"]
    assert "星空" not in [w["title"] for w in rest] and "夏の終わり" in [w["title"] for w in rest]
    packed = json.loads(api._list_works_json({"scope": "artist", "artist": str(artist), "packed": 1}))
    flags = {row[1]: row[8] for row in packed["works"]}
    assert flags[100002] & 4 and not flags[100001] & 4                    # 紧凑格式里也带着这个标记

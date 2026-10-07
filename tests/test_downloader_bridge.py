# -*- coding: utf-8 -*-
"""内置下载功能的进程通信：用一个假的 pixiv_dl 测试工作进程和管道协议（不访问网络，不碰真实数据）"""
import textwrap

import pytest

FAKE_SERVER = textwrap.dedent('''
    import re
    import logging
    logger = logging.getLogger("fake")
    VERSION = "9.9"
    ROUTES = []

    class HttpError(Exception):
        def __init__(self, status, message):
            super().__init__(message)
            self.status, self.message = status, message

    class Raw:
        def __init__(self, data):
            self.data = data

    class Request:
        def __init__(self, app, match, query, body):
            self.app, self.m, self.q, self.body = app, match, query, body or {}

    class _Runner:
        _thread = None

    class App:
        def __init__(self):
            self.runner = _Runner()
            self.calls = 0

    def route(method, pattern):
        def deco(fn):
            ROUTES.append((method, re.compile("^" + pattern + "$"), fn))
            return fn
        return deco

    @route("GET", r"/api/echo/(?P<name>\\w+)")
    def echo(r):
        r.app.calls += 1
        print("这行输出不能混进协议里")          # 下载器里的 print 要被改道
        return {"name": r.m["name"], "q": r.q, "calls": r.app.calls, "text": "中文"}

    @route("POST", r"/api/sum")
    def total(r):
        return {"sum": sum(r.body.get("nums", []))}

    @route("GET", r"/api/boom")
    def boom(r):
        raise HttpError(409, "已有任务在运行")

    @route("GET", r"/api/crash")
    def crash(r):
        raise RuntimeError("坏了")

    @route("GET", r"/api/file")
    def file(r):
        return Raw(b"x")
''')


@pytest.fixture
def fake_downloader(tmp_path):
    root = tmp_path / "pixiv_downloader"
    (root / "pixiv_dl" / "web").mkdir(parents=True)
    (root / "pixiv_dl" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pixiv_dl" / "web" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pixiv_dl" / "web" / "server.py").write_text(FAKE_SERVER, encoding="utf-8")
    (root / "pixiv_dl" / "interrupt.py").write_text("def set():\n    pass\n", encoding="utf-8")
    (root / "pixiv_dl" / "applog.py").write_text("def setup_logging(console=True, to_file=True):\n    pass\n",
                                                  encoding="utf-8")
    (root / "avatars").mkdir()
    (root / "avatars" / "123.png").write_bytes(b"png")
    (root / "avatars" / "readme.txt").write_text("x", encoding="utf-8")
    return root


def test_resolve_home(tmp_path):
    from webapp.downloader import resolve_home
    own = tmp_path / "data" / "pixiv"
    assert resolve_home(str(tmp_path / "picked"), "", own) == tmp_path / "picked"      # 设置里指定的优先
    # 以前单独使用下载器留下的数据（settings.json + db/）：沿用
    old = tmp_path / "old"
    (old / "db").mkdir(parents=True)
    (old / "settings.json").write_text("{}", encoding="utf-8")
    assert resolve_home("", str(old / "db" / "pixiv_manager.db"), own) == old
    # 元数据库旁边没有下载数据：用查看器自己的目录
    assert resolve_home("", str(tmp_path / "elsewhere" / "db" / "x.db"), own) == own
    assert resolve_home("", "", own) == own


def test_bridge_calls_downloader_in_worker_process(fake_downloader):
    from webapp.downloader import DownloaderBridge, DownloaderError
    bridge = DownloaderBridge(fake_downloader, code_root=fake_downloader)
    try:
        assert not bridge.running                       # 第一次调用时才启动
        r = bridge.call("GET", "/api/echo/abc", query={"limit": 5})
        assert r == {"name": "abc", "q": {"limit": ["5"]}, "calls": 1, "text": "中文"}
        assert bridge.running and bridge.version == "9.9"
        assert bridge.call("POST", "/api/sum", {"nums": [1, 2, 3]}) == {"sum": 6}
        assert bridge.call("GET", "/api/echo/x")["calls"] == 2      # 同一个进程，状态保留
        for path, status in (("/api/boom", 409), ("/api/crash", 500), ("/api/nope", 404), ("/api/file", 415)):
            with pytest.raises(DownloaderError) as e:
                bridge.call("GET", path)
            assert e.value.status == status
        assert "已有任务在运行" in str(_error(bridge, "/api/boom"))
    finally:
        bridge.stop()
    assert not bridge.running


def _error(bridge, path):
    from webapp.downloader import DownloaderError
    try:
        bridge.call("GET", path)
    except DownloaderError as e:
        return e


def test_bridge_restarts_after_worker_exit(fake_downloader):
    from webapp.downloader import DownloaderBridge
    bridge = DownloaderBridge(fake_downloader, code_root=fake_downloader)
    try:
        assert bridge.call("GET", "/api/echo/a")["calls"] == 1
        bridge._proc.kill()
        bridge._proc.wait(timeout=10)
        assert bridge.call("GET", "/api/echo/b")["calls"] == 1      # 新进程，计数从头开始
    finally:
        bridge.stop()


def test_downloader_data_reads_avatars(fake_downloader):
    from webapp.downloader import DownloaderData
    data = DownloaderData(fake_downloader)
    assert data.avatars() == {123: "123.png"}
    assert data.avatar_path(123) == fake_downloader / "avatars" / "123.png"
    assert data.avatar_path(456) is None
    (fake_downloader / "avatars" / "456.jpg").write_bytes(b"jpg")
    assert data.avatar_path(456) is None            # 有缓存
    data.refresh()
    assert data.avatar_path(456) is not None
    assert data.author_names() == {}                # 没有数据库时不报错


def test_api_dl_passthrough_and_blocked_paths(fake_downloader, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from webapp.api import Api
    from webapp.store import WebStore

    class Reader:
        db_path = None

        def set_metadata_path(self, p):
            pass

    store = WebStore(tmp_path / "w.db", tmp_path / "ui.json")
    store.save_settings({"downloaderHome": str(fake_downloader)})
    cm = SimpleNamespace(config=SimpleNamespace(image_paths=[], pixiv_metadata_path=""))
    api = Api(cm, Reader(), None, store=store, token="tok")
    api._dl_code_root = fake_downloader
    try:
        info = api.dl_info()
        assert info["home"] == str(fake_downloader) and info["external"] and not info["started"]
        assert api.dl("POST", "/api/sum", {"nums": [2, 5]}) == {"ok": True, "data": {"sum": 7}}
        assert api.dl("GET", "/api/boom") == {"ok": False, "status": 409, "error": "已有任务在运行"}
        # 返回文件的接口、不是 /api/ 开头的路径都不转发
        for path in ("/api/file/abc", "/api/thumb/abc", "/api/avatar/1", "/api/tasks/export.csv", "/static/x.js"):
            assert api.dl("GET", path)["status"] == 400
        assert api.dl_info()["started"]
        assert api._avatar_path(123) is not None
    finally:
        api._shutdown()
        store.close_thread_connection()


def test_data_locations_follow_one_rule(tmp_path, monkeypatch):
    """保存位置自动属于资料库；元数据用下载数据里的数据库——不用分别设置"""
    import json
    from types import SimpleNamespace
    from webapp.api import Api
    from webapp.store import WebStore

    class Reader:
        db_path = None

        def set_metadata_path(self, p):
            self.db_path = p or None

    class Cm:
        def __init__(self):
            self.config = SimpleNamespace(image_paths=[], pixiv_metadata_path="")

        def update(self, **kw):
            for k, v in kw.items():
                setattr(self.config, k, v)

    home = tmp_path / "pixiv"
    home.mkdir()
    store = WebStore(tmp_path / "w.db", tmp_path / "ui.json")
    store.save_settings({"downloaderHome": str(home)})
    api = Api(Cm(), Reader(), None, store=store, token="tok")

    def configure(**current):
        (home / "settings.json").write_text(json.dumps({"current": current}), encoding="utf-8")
        api._dl_data._target = None

    try:
        # 没有任何设置：默认存到数据目录下的 downloads；文件夹还不存在时不算进资料库
        link = api.dl_storage_link()
        assert link["mode"] == "local" and link["path"] == str(home / "downloads") and link["readable"]
        assert api._library.roots() == []
        (home / "downloads").mkdir()
        assert api._library.roots() == [str(home / "downloads")] and api._allowed(str(home / "downloads" / "a.png"))
        lib_root = api.get_library()["roots"][0]
        assert lib_root["auto"] and lib_root["path"] == str(home / "downloads")
        # 用户自己加的文件夹照常保留；已经包含保存位置（或它的上级）时不重复
        api._cm.config.image_paths = [str(tmp_path / "mine"), str(home)]
        assert api._library.roots() == [str(tmp_path / "mine"), str(home)]
        assert not api.dl_storage_link()["auto"]
        # SMB：换算成网络路径
        api._cm.config.image_paths = []
        configure(STORAGE_MODE="smb", NAS_IP="10.0.0.2", NAS_SHARE="media", NAS_BASE_PATH="图片/PIXIV")
        unc = chr(92) * 2 + chr(92).join(["10.0.0.2", "media", "图片", "PIXIV"])
        assert api.dl_storage_link()["path"] == unc and api._library.roots() == [unc]
        # 共享没连上（比如电脑刚重启，Windows 不记得上次的登录）：用下载设置里的账号登录一次再读
        assert api._dl_data.smb_login() is None                      # 没填账号：不去登录
        configure(STORAGE_MODE="smb", NAS_IP="10.0.0.2", NAS_SHARE="media", NAS_BASE_PATH="图片/PIXIV",
                  NAS_USER="me", NAS_PASS="secret")
        share = chr(92) * 2 + chr(92).join(["10.0.0.2", "media"])
        assert api._dl_data.smb_login() == {"share": share, "user": "me", "password": "secret"}
        import utils.network_mount as nm
        state = {"up": False, "calls": []}

        class Dir:
            def close(self):
                pass

        def fake_scandir(path):
            if not state["up"]:
                raise OSError(1326, "用户名或密码不正确。")
            return Dir()

        def fake_connect(share_, user, password):
            state["calls"].append((share_, user, password))
            state["up"] = state.get("accept", True)
            return (True, "") if state["up"] else (False, "找不到网络路径。")

        monkeypatch.setattr("webapp.api.os.scandir", fake_scandir)
        monkeypatch.setattr(nm, "connect_share", fake_connect)
        api._connect_save_share()
        assert state["calls"] == [(share, "me", "secret")] and api._share_error == ""
        api._connect_save_share()
        assert len(state["calls"]) == 1                              # 已经能读：不再登录
        state.update(up=False, accept=False)
        api._connect_save_share()
        assert api._share_error == "找不到网络路径。" and api._root_error(unc) == "找不到网络路径。"
        monkeypatch.undo()
        # 查看器读不了的保存方式：不加进资料库
        configure(STORAGE_MODE="s3", S3_BUCKET="b")
        link = api.dl_storage_link()
        assert not link["readable"] and not link["inLibrary"] and api._library.roots() == []
        # 下载数据里有数据库时，作品信息自动读它
        (home / "db").mkdir()
        (home / "db" / "pixiv_manager.db").write_bytes(b"")
        api._adopt_download_metadata()
        assert api._cm.config.pixiv_metadata_path == str(home / "db" / "pixiv_manager.db")
    finally:
        store.close_thread_connection()

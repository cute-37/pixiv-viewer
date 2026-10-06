"""软件更新：版本比较、检查、下载校验、替换与回滚。全部用假的网络，不访问 GitHub。"""
import hashlib
import io
import json
import shutil
import time
import zipfile

import pytest

from webapp import updater
from webapp.updater import UpdateApiMixin, UpdateError, Updater, is_newer, parse_version, replace_install

REPO = "someone/app"
APP = "PixivViewer"


# ---------------------------------------------------------------- 假的 GitHub
class FakeResponse:
    def __init__(self, status=200, data=None, body=b""):
        self.status_code = status
        self._data = data
        self._body = body
        self.text = json.dumps(data) if data is not None else ""

    def json(self):
        if self._data is None:
            raise ValueError("no json")
        return self._data

    def iter_content(self, size):
        for i in range(0, len(self._body), size):
            yield self._body[i:i + size]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def make_zip(entries=None):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        for name, data in (entries or {f"{APP}/{APP}.exe": b"new exe", f"{APP}/_internal/lib.dll": b"new lib",
                                       f"{APP}/使用说明.txt": "新说明".encode()}).items():
            z.writestr(name, data)
    return buffer.getvalue()


def release(version="9.9.9", body=None, digest=True, size=None, name=None, url=None):
    body = make_zip() if body is None else body
    asset = {"name": name or f"{APP}-{version}-win64.zip", "size": len(body) if size is None else size,
             "browser_download_url": url or f"https://github.com/{REPO}/releases/download/v{version}/{APP}-{version}-win64.zip"}
    if digest:
        asset["digest"] = "sha256:" + (hashlib.sha256(body).hexdigest() if digest is True else digest)
    return {"tag_name": f"v{version}", "body": "改了这些", "html_url": f"https://github.com/{REPO}/releases/tag/v{version}",
            "published_at": "2026-10-06T00:00:00Z", "assets": [asset]}, body


@pytest.fixture
def install(tmp_path):
    """一个假的已安装程序：exe、_internal、说明、用户数据"""
    root = tmp_path / "PixivViewer"
    (root / "_internal").mkdir(parents=True)
    (root / f"{APP}.exe").write_bytes(b"old exe")
    (root / "_internal" / "lib.dll").write_bytes(b"old lib")
    (root / "_internal" / "gone-in-new-version.dll").write_bytes(b"x")
    (root / "data" / "config").mkdir(parents=True)
    (root / "data" / "config" / "settings.json").write_text("mine", encoding="utf-8")
    return root


def make_updater(install, monkeypatch, rel=None, body=b"", version="2.5.0", status=200):
    up = Updater(APP, version, REPO, app_dir=install, frozen=True)
    monkeypatch.setattr(up, "can_apply", lambda: (True, ""))

    def fake_get(url, **kwargs):
        if "api.github.com" in url:
            return FakeResponse(status, rel)
        return FakeResponse(200, None, body)

    monkeypatch.setattr(up, "_get", fake_get)
    return up


def wait_done(up, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = up.status()
        if state["state"] in ("ready", "error", "idle"):
            return state
        time.sleep(0.02)
    raise AssertionError("下载一直没有结束")


# ---------------------------------------------------------------- 版本号
def test_version_compare():
    assert parse_version("v2.6.0") == (2, 6, 0) and parse_version("2.6") == (2, 6, 0)
    assert parse_version("nightly") == ()
    assert is_newer("v2.10.0", "2.9.9") and is_newer("2.5.1", "2.5.0")
    assert not is_newer("2.5.0", "2.5.0") and not is_newer("2.4.9", "2.5.0")
    assert not is_newer("nightly", "2.5.0")


# ---------------------------------------------------------------- 检查
def test_check_finds_newer_version(install, monkeypatch):
    rel, body = release()
    info = make_updater(install, monkeypatch, rel, body).check()
    assert info["newer"] and info["latest"] == "9.9.9" and info["current"] == "2.5.0"
    assert info["size"] == len(body) and info["notes"] == "改了这些" and info["canApply"]


def test_check_same_version_is_not_newer(install, monkeypatch):
    rel, body = release("2.5.0")
    up = make_updater(install, monkeypatch, rel, body)
    assert not up.check()["newer"]
    with pytest.raises(UpdateError):
        up.start_download()                         # 没有新版本就不能下载


def test_check_no_release_yet(install, monkeypatch):
    info = make_updater(install, monkeypatch, None, status=404).check()
    assert not info["newer"] and "还没有发布" in info["notes"]


def test_check_release_without_matching_package(install, monkeypatch):
    rel, body = release(name="PixivDownloader-9.9.9-win64.zip")
    info = make_updater(install, monkeypatch, rel, body).check()
    assert info["newer"] and not info["canApply"] and "安装包" in info["reason"]


def test_check_server_error(install, monkeypatch):
    with pytest.raises(UpdateError):
        make_updater(install, monkeypatch, {"message": "boom"}, status=500).check()


def test_source_checkout_cannot_apply(tmp_path):
    ok, reason = Updater(APP, "2.5.0", REPO, app_dir=tmp_path, frozen=False).can_apply()
    assert not ok and "源码" in reason


# ---------------------------------------------------------------- 下载
def test_download_verifies_and_unpacks(install, monkeypatch):
    rel, body = release()
    up = make_updater(install, monkeypatch, rel, body)
    up.check()
    up.start_download()
    assert wait_done(up)["state"] == "ready"
    assert up.staged_exe.read_bytes() == b"new exe"
    assert not list(up.work.glob("*.zip")) and not list(up.work.glob("*.part"))


@pytest.mark.parametrize("kwargs, expect", [
    ({"digest": "0" * 64}, "校验"),                                       # 内容和校验值对不上
    ({"size": 5}, "不完整"),                                              # 大小对不上
    ({"body": make_zip({"Other/readme.txt": b"x"})}, "内容不对"),          # 不是这个程序的安装包
    ({"body": make_zip({f"{APP}/{APP}.exe": b"x", "../evil.txt": b"x"})}, "不正常的路径"),
    ({"url": "https://example.com/evil.zip"}, "GitHub 地址"),
])
def test_download_rejects_bad_packages(install, monkeypatch, kwargs, expect):
    rel, body = release(**kwargs)
    up = make_updater(install, monkeypatch, rel, body)
    up.check()
    up.start_download()
    state = wait_done(up)
    assert state["state"] == "error" and expect in state["error"]
    assert not up.work.exists()                      # 失败后不留半截文件
    with pytest.raises(UpdateError):
        up.launch_apply()


def test_download_can_be_cancelled(install, monkeypatch):
    rel, body = release()
    up = make_updater(install, monkeypatch, rel, body)
    up.check()
    up._cancel.set()
    up._set(state="downloading")
    up._download(rel["assets"][0])
    assert up.status() ["state"] == "idle" and up.status()["error"] == ""


# ---------------------------------------------------------------- 替换
def new_version(tmp_path):
    root = tmp_path / "staged" / APP
    (root / "_internal").mkdir(parents=True)
    (root / f"{APP}.exe").write_bytes(b"new exe")
    (root / "_internal" / "lib.dll").write_bytes(b"new lib")
    (root / "使用说明.txt").write_text("新说明", encoding="utf-8")
    return root


def snapshot(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def test_replace_swaps_program_and_keeps_user_data(install, tmp_path):
    replace_install(new_version(tmp_path), install)
    files = snapshot(install)
    assert files == {f"{APP}.exe": b"new exe", "_internal/lib.dll": b"new lib", "使用说明.txt": "新说明".encode(),
                     "data/config/settings.json": b"mine"}          # 旧版本独有的文件没了，留底也清掉了


def test_replace_failure_restores_everything(install, tmp_path, monkeypatch):
    before = snapshot(install)
    new_root = new_version(tmp_path)
    real_copy = shutil.copy2

    def failing_copy(src, dst, *args, **kwargs):
        if str(src).endswith(".txt"):
            raise OSError("磁盘满了")
        return real_copy(src, dst, *args, **kwargs)

    monkeypatch.setattr(updater.shutil, "copy2", failing_copy)
    with pytest.raises(UpdateError):
        replace_install(new_root, install)
    assert snapshot(install) == before


def test_replace_gives_up_when_files_stay_locked(install, tmp_path, monkeypatch):
    before = snapshot(install)
    real_replace = updater.os.replace

    def locked(src, dst):
        if str(src).endswith("_internal"):
            raise PermissionError(13, "正被另一个进程使用")
        return real_replace(src, dst)

    monkeypatch.setattr(updater.os, "replace", locked)
    with pytest.raises(UpdateError, match="占用"):
        replace_install(new_version(tmp_path), install, retry_secs=0.2)
    monkeypatch.setattr(updater.os, "replace", real_replace)
    assert snapshot(install) == before


def test_replace_never_touches_data_even_if_package_contains_it(install, tmp_path):
    new_root = new_version(tmp_path)
    (new_root / "data").mkdir()
    (new_root / "data" / "settings.json").write_text("theirs", encoding="utf-8")
    replace_install(new_root, install)
    assert (install / "data" / "config" / "settings.json").read_text(encoding="utf-8") == "mine"
    assert not (install / "data" / "settings.json").exists()


def test_apply_refuses_to_run_from_outside_the_install(install, tmp_path, monkeypatch):
    before = snapshot(install)
    elsewhere = new_version(tmp_path)
    monkeypatch.setattr(updater.sys, "executable", str(elsewhere / f"{APP}.exe"))
    monkeypatch.setattr(updater, "_message", lambda *a, **k: None)
    monkeypatch.setattr(updater.subprocess, "Popen", lambda *a, **k: None)
    assert updater.apply_main([str(install), "0", "2.5.0"]) == 1
    assert {k: v for k, v in snapshot(install).items() if not k.startswith("data/logs/")} == before


def test_apply_replaces_and_leaves_marker(install, monkeypatch):
    staged = install / "data" / "update" / "staged" / APP
    (staged / "_internal").mkdir(parents=True)
    (staged / f"{APP}.exe").write_bytes(b"new exe")
    (staged / "_internal" / "lib.dll").write_bytes(b"new lib")
    launched = []
    monkeypatch.setattr(updater.sys, "executable", str(staged / f"{APP}.exe"))
    monkeypatch.setattr(updater.subprocess, "Popen", lambda cmd, **k: launched.append(cmd))
    assert updater.apply_main([str(install), "0", "2.5.0"]) == 0
    assert (install / f"{APP}.exe").read_bytes() == b"new exe"
    assert launched == [[str((install / f"{APP}.exe").resolve())]]
    # 新版本启动后：读到“刚更新过”，并把临时文件清掉
    up = Updater(APP, "9.9.9", REPO, app_dir=install, frozen=True)
    assert up.finish()["from"] == "2.5.0"
    updater.cleanup(install, tries=1)
    assert not (install / "data" / "update").exists()
    assert up.finish() is None


# ---------------------------------------------------------------- 给界面的接口
def test_api_reports_errors_instead_of_raising(install, monkeypatch):
    class Api(UpdateApiMixin):
        pass

    api = Api()
    assert api.update_check() == {"ok": False, "error": "这个程序没有启用更新功能"}
    api._updater = make_updater(install, monkeypatch, {"message": "boom"}, status=500)
    result = api.update_check()
    assert result["ok"] is False and "500" in result["error"]
    assert api.update_start()["ok"] is False
    assert api.update_status()["state"] == "idle"
    api._update_done = {"from": "2.4.0"}
    assert api.update_done() == {"from": "2.4.0"} and api.update_done() is None

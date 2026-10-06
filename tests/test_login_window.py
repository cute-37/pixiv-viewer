"""登录窗口：从网络请求里认出授权码、完成添加、取消、出错。窗口本身用假的代替。"""
import sys
import time
import types

import pytest

from webapp.login_window import LoginMixin, apply_browser_proxy, browser_proxy_arguments, find_code

CALLBACK = "https://app-api.pixiv.net/web/v1/users/auth/pixiv/callback?state=abc&code=THE-CODE"


# ---------------------------------------------------------------- 认出授权码
@pytest.mark.parametrize("url, headers, expect", [
    (CALLBACK, None, "THE-CODE"),
    ("pixiv://account/login?code=THE-CODE&via=login", None, "THE-CODE"),
    ("https://app-api.pixiv.net/whatever", {"Location": "pixiv://account/login?code=FROM-HEADER&via=login"}, "FROM-HEADER"),
    ("https://app-api.pixiv.net/web/v1/users/auth/pixiv/callback?state=abc", None, ""),        # 没有 code
    ("https://accounts.pixiv.net/login?code=not-this-one", None, ""),                          # 别的页面上的 code 参数不算
    ("https://example.com/?next=" + CALLBACK.replace("?", "%3F").replace("&", "%26"), None, ""),
    ("", None, ""),
])
def test_find_code(url, headers, expect):
    assert find_code(url, headers) == expect


# ---------------------------------------------------------------- 假窗口
class Event:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def fire(self, *args):
        for handler in list(self.handlers):
            handler(*args)


class FakeWindow:
    def __init__(self):
        self.events = types.SimpleNamespace(loaded=Event(), request_sent=Event(), response_received=Event(), closed=Event())
        self.loaded_urls, self.cookies_cleared, self.destroyed, self.shown = [], 0, False, 0

    def clear_cookies(self):
        self.cookies_cleared += 1

    def load_url(self, url):
        self.loaded_urls.append(url)

    def destroy(self):
        self.destroyed = True
        self.events.closed.fire()

    def show(self):
        self.shown += 1


class Api(LoginMixin):
    def __init__(self, finish=None, start=None):
        self.calls = []
        self._finish = finish or {"ok": True, "data": {"ok": True, "name": "新账号"}}
        self._start = start or {"ok": True, "data": {"state": "st-1", "url": "https://app-api.pixiv.net/web/v1/login?x=1"}}

    def dl(self, method, path, body=None):
        self.calls.append((method, path, body))
        return self._start if path.endswith("/start") else self._finish


@pytest.fixture
def window(monkeypatch):
    win = FakeWindow()
    fake = types.SimpleNamespace(create_window=lambda *a, **k: win)
    monkeypatch.setitem(sys.modules, "webview", fake)
    return win


def wait_for(api, statuses, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        if api.login_status()["status"] in statuses:
            return api.login_status()
        time.sleep(0.01)
    raise AssertionError(api.login_status())


def request(url):
    return types.SimpleNamespace(url=url, headers={})


# ---------------------------------------------------------------- 流程
def test_login_captures_code_closes_window_and_adds_account(window):
    api = Api()
    assert api.login_start("")["status"] == "waiting"
    window.events.loaded.fire()                                   # 占位页载入：清登录状态，转去登录页
    window.events.loaded.fire()                                   # 登录页自己载入时不再重复
    assert window.cookies_cleared == 1 and window.loaded_urls == ["https://app-api.pixiv.net/web/v1/login?x=1"]
    window.events.request_sent.fire(request("https://accounts.pixiv.net/login"))
    assert api.login_status()["status"] == "waiting"
    window.events.request_sent.fire(request(CALLBACK))
    window.events.request_sent.fire(request(CALLBACK))            # 同一个地址再来一次也只处理一次
    done = wait_for(api, {"done"})
    assert done["name"] == "新账号" and window.destroyed
    finishes = [c for c in api.calls if c[1].endswith("/finish")]
    assert finishes == [("POST", "/api/accounts/oauth/finish", {"state": "st-1", "callback": "THE-CODE", "name": ""})]


def test_closing_the_window_cancels(window):
    api = Api()
    api.login_start("")
    window.events.closed.fire()
    assert api.login_status()["status"] == "cancelled"
    assert not [c for c in api.calls if c[1].endswith("/finish")]


def test_cancel_button_closes_window(window):
    api = Api()
    api.login_start("")
    assert api.login_cancel()["status"] == "cancelled" and window.destroyed


def test_failed_exchange_is_reported(window):
    api = Api(finish={"ok": False, "status": 400, "error": "换取 Token 失败: invalid_grant"})
    api.login_start("")
    window.events.request_sent.fire(request(CALLBACK))
    failed = wait_for(api, {"error"})
    assert "invalid_grant" in failed["message"] and window.destroyed


def test_start_failure_and_second_click_reuses_window(window):
    broken = Api(start={"ok": False, "error": "下载模块没有启动"})
    assert broken.login_start("") == {"ok": False, "error": "下载模块没有启动"}
    api = Api()
    api.login_start("")
    again = api.login_start("")                                   # 窗口还开着时再点一次：不重新开始，只把窗口叫到前面
    assert again["status"] == "waiting" and window.shown == 1
    assert len([c for c in api.calls if c[1].endswith("/start")]) == 1


def test_account_name_is_passed_through(window):
    api = Api()
    api.login_start("  小号  ")
    window.events.request_sent.fire(request(CALLBACK))
    wait_for(api, {"done"})
    assert api.calls[-1][2]["name"] == "小号"



# ---------------------------------------------------------------- 登录窗口用的代理
@pytest.mark.parametrize("mode, url, expect", [
    ("system", "", ""),
    ("none", "", "--no-proxy-server"),
    ("custom", "http://127.0.0.1:7890", "--proxy-server=http://127.0.0.1:7890"),
    ("custom", "socks5h://127.0.0.1:1080", "--proxy-server=socks5://127.0.0.1:1080"),
    ("custom", "http://user:pw@10.0.0.2:8080", "--proxy-server=http://10.0.0.2:8080"),     # 启动参数里不带用户名密码
    ("custom", "", ""),
])
def test_browser_proxy_arguments(mode, url, expect):
    assert browser_proxy_arguments(mode, url) == expect


def test_apply_browser_proxy_reads_settings(tmp_path, monkeypatch):
    import json
    key = "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"
    monkeypatch.delenv(key, raising=False)
    f = tmp_path / "settings.json"
    assert apply_browser_proxy(f) == ""                                   # 还没有设置文件
    f.write_text(json.dumps({"current": {"PROXY_MODE": "system"}}), encoding="utf-8")
    assert apply_browser_proxy(f) == "" and key not in __import__("os").environ
    f.write_text(json.dumps({"current": {"PROXIES": {"https": "http://127.0.0.1:7890"}}}), encoding="utf-8")   # 旧写法
    assert apply_browser_proxy(f) == "--proxy-server=http://127.0.0.1:7890"
    assert __import__("os").environ[key] == "--proxy-server=http://127.0.0.1:7890"
    f.write_text(json.dumps({"current": {"PROXY_MODE": "none"}}), encoding="utf-8")
    apply_browser_proxy(f)
    assert __import__("os").environ[key] == "--proxy-server=http://127.0.0.1:7890"   # 已经定过的不再改（一次启动只定一次）


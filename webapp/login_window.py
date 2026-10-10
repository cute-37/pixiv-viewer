#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
在软件里登录 Pixiv 账号

Pixiv 的登录授权是这样的：打开官方登录页 → 用户登录 → 页面跳到
https://app-api.pixiv.net/web/v1/users/auth/pixiv/callback?state=…&code=… → 那个地址再转去 pixiv://account/login?code=…
（给手机 App 用的地址，电脑上打不开，所以浏览器里最后是一片空白）。我们要的就是其中的 code。

在普通浏览器里，这个 code 只能打开开发者工具从网络记录里抄出来，对一般用户太难。这里改成由程序自己开一个
小窗口显示登录页，并留意窗口里的网络请求：一看到上面那个回调地址，就把 code 取下来、关掉窗口、完成添加。
账号和密码只输入在 Pixiv 自己的页面里，程序不接触它们，拿到的只有 code。

给界面用的接口（LoginMixin）：login_start() 开窗口，login_status() 看进行到哪了，login_cancel() 取消。
使用它的类要有 dl(method, path, body) 方法（转给下载模块的接口）。
"""
from __future__ import annotations

import json
import os
import threading
from typing import Optional
from urllib.parse import parse_qs, urlsplit

from utils.lang import pick

CALLBACK_MARK = "/auth/pixiv/callback"

WAITING_HTML = """<!doctype html><meta charset="utf-8">
<body style="margin:0;height:100vh;display:flex;align-items:center;justify-content:center;
font:14px 'Microsoft YaHei UI',sans-serif;color:#52525b;background:#fff">正在打开 Pixiv 登录页…</body>"""


def browser_proxy_arguments(mode: str, url: str) -> str:
    """把软件里的代理设置（见 pixiv_dl/proxy.py）换成内置浏览器的启动参数。跟随系统时不需要任何参数。"""
    if mode == "none":
        return "--no-proxy-server"
    if mode == "custom" and url:
        parts = urlsplit(url)
        if parts.hostname and parts.port:
            scheme = {"socks5h": "socks5", "socks4a": "socks4"}.get(parts.scheme, parts.scheme)   # 浏览器的写法里没有 h / a
            host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
            return f"--proxy-server={scheme}://{host}:{parts.port}"
    return ""


def apply_browser_proxy(settings_file) -> str:
    """程序启动、还没开任何窗口时调用：让登录窗口用和下载一样的代理。

    内置浏览器的代理只能在启动时定下来，所以在设置里改了代理之后，登录窗口要到下次启动才跟着变。
    访问本机地址（界面自己的文件）不受影响：浏览器对本机地址从不走代理。返回实际加上的参数（没加就是空字符串）。
    """
    try:
        current = json.loads(open(settings_file, encoding="utf-8").read()).get("current") or {}
    except (OSError, ValueError):
        return ""
    mode, url = str(current.get("PROXY_MODE") or ""), str(current.get("PROXY_URL") or "")
    if not mode:                                    # 以前手工写在 PROXIES 里的
        legacy = current.get("PROXIES") if isinstance(current.get("PROXIES"), dict) else {}
        url = str(legacy.get("https") or legacy.get("http") or "")
        mode = "custom" if url else "system"
    args = browser_proxy_arguments(mode, url)
    if args:
        key = "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"
        existing = os.environ.get(key, "")
        if "--proxy-server" not in existing and "--no-proxy-server" not in existing:
            os.environ[key] = (existing + " " + args).strip()
    return args


def find_code(url: str, headers: Optional[dict] = None) -> str:
    """从一次网络回应里找授权码：回调地址本身，或者它要转去的 pixiv:// 地址。找不到返回空字符串。"""
    candidates = [str(url or "")]
    for key, value in (headers or {}).items():
        if str(key).lower() == "location":
            candidates.append(str(value or ""))
    for text in candidates:
        if CALLBACK_MARK in text or text.startswith("pixiv://"):
            code = parse_qs(urlsplit(text).query).get("code", [""])[0]
            if code:
                return code
    return ""


class LoginMixin:
    _login_lock = threading.Lock()
    _login: Optional[dict] = None           # {"status", "message", "name", "window", "state"}

    def _login_state(self) -> dict:
        login = self._login or {}
        return {"status": login.get("status", "idle"), "message": login.get("message", ""), "name": login.get("name", "")}

    def login_start(self, name=""):
        """开一个登录窗口。返回 {ok} 或 {ok: False, error}；之后用 login_status() 轮询。"""
        import webview
        with self._login_lock:
            if self._login and self._login.get("status") in ("waiting", "finishing"):
                window = self._login.get("window")
                if window is not None:
                    try:
                        window.show()
                    except Exception:
                        pass
                return {"ok": True, **self._login_state()}
            started = self.dl("POST", "/api/accounts/oauth/start")
            if not started or not started.get("ok"):
                return {"ok": False, "error": (started or {}).get("error") or "没能开始登录"}
            data = started["data"]
            login = {"status": "waiting", "message": "", "name": "", "state": data["state"],
                     "wanted_name": str(name or "").strip(), "window": None, "navigated": False}
            self._login = login
        try:
            window = webview.create_window(pick("登录 Pixiv", "Sign in to Pixiv", "Pixiv にログイン"), html=WAITING_HTML, width=520, height=780, min_size=(420, 560),
                                           background_color="#FFFFFF", text_select=True)
        except Exception as error:
            login["status"], login["message"] = "error", f"登录窗口打不开：{error}"
            return {"ok": False, "error": login["message"]}
        login["window"] = window

        def on_loaded():
            # 第一次载入的是“正在打开”的占位页：清掉上一个账号留下的登录状态，再去真正的登录页
            if login["navigated"]:
                return
            login["navigated"] = True
            try:
                window.clear_cookies()
            except Exception:
                pass
            window.load_url(data["url"])

        def on_response(response):
            if login["status"] != "waiting":
                return
            code = find_code(getattr(response, "url", ""), getattr(response, "headers", None))
            if code:
                login["status"] = "finishing"
                threading.Thread(target=self._login_finish, args=(login, code), name="pixiv-login", daemon=True).start()

        def on_closed():
            if login["status"] == "waiting":
                login["status"] = "cancelled"

        window.events.loaded += on_loaded
        # 回调地址的回应是一次“转去 pixiv://”的跳转，WebView2 不会为它报告“收到回应”，但会报告“发出请求”，
        # 而请求的地址里就带着 code。两个事件都听，哪个先看到都行。
        window.events.request_sent += on_response
        window.events.response_received += on_response
        window.events.closed += on_closed
        return {"ok": True, **self._login_state()}

    def _login_finish(self, login: dict, code: str) -> None:
        window = login.get("window")
        try:
            if window is not None:
                window.destroy()
        except Exception:
            pass
        try:
            result = self.dl("POST", "/api/accounts/oauth/finish",
                             {"state": login["state"], "callback": code, "name": login["wanted_name"]})
        except Exception as error:
            result = {"ok": False, "error": f"{type(error).__name__}: {error}"}
        if result and result.get("ok"):
            login["name"] = (result.get("data") or {}).get("name", "")
            login["status"] = "done"
        else:
            login["message"] = (result or {}).get("error") or "没能完成登录"
            login["status"] = "error"

    def login_status(self):
        return {"ok": True, **self._login_state()}

    def login_cancel(self):
        login = self._login
        if login and login.get("status") == "waiting":
            login["status"] = "cancelled"
            window = login.get("window")
            try:
                if window is not None:
                    window.destroy()
            except Exception:
                pass
        return {"ok": True, **self._login_state()}

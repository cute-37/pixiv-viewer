#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Pixiv 下载器 —— 独立的桌面程序

界面就是查看器里的“下载与更新”面板（webui/downloader.html），铺满整个窗口；后端是内置的 pixiv_dl 模块。
不依赖查看器的资料库，也不启动下载模块自带的网页服务。

    python dl_main.py

数据（账号、设置、数据库、头像、下载的文件）放在程序旁边的 data 文件夹里；
也可以用环境变量 PIXIV_DL_HOME 指定别的位置。打包见 scripts/build_downloader.py。
"""
from __future__ import annotations

import json
import mimetypes
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

FROZEN = getattr(sys, "frozen", False)
APP_DIR = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR))          # 打包后界面文件所在的位置
WEBUI_DIR = BUNDLE_DIR / "webui"
HOME = Path(os.environ.get("PIXIV_DL_HOME") or (APP_DIR / "data" / ("" if FROZEN else "pixiv_standalone")))
os.environ["PIXIV_DL_HOME"] = str(HOME)                       # 必须在导入 pixiv_dl 之前
os.environ.setdefault("PIXIV_VIEWER_DATA_DIR", str(HOME))     # 不让公共模块把日志写到程序文件夹里

from webapp.login_window import LoginMixin  # noqa: E402  （只用标准库，不受上面环境变量影响）

PENDING = HOME / "import-pending.json"                        # 上次没能立即导入、留到这次启动时做的文件

BOOT = b'<script>window.__PV_DESKTOP__=true;window.__PV_TOKEN__="";</script>\n<script type="module"'


class StaticHandler(BaseHTTPRequestHandler):
    """只负责把界面文件发给窗口（只监听本机的随机端口）"""

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        rel = self.path.split("?", 1)[0].lstrip("/") or "downloader.html"
        target = (WEBUI_DIR / rel).resolve()
        if not str(target).startswith(str(WEBUI_DIR.resolve())) or not target.is_file():
            return self.send_error(404)
        data = target.read_bytes()
        if target.suffix == ".html":
            # 页面文件本身不含 doctype（同一份文件也用于浏览器预览），这里补上，并告诉前端是桌面版
            data = b"<!doctype html>\n" + data.replace(b'<script type="module"', BOOT, 1)
        ctype = {".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
                 ".html": "text/html; charset=utf-8"}.get(target.suffix.lower()) \
            or mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        try:
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(data)
        except (ConnectionError, BrokenPipeError):
            pass


class DlApi(LoginMixin):
    """给界面用的接口。pywebview 会把不以下划线开头的方法暴露给网页。"""

    def __init__(self) -> None:
        self._window = None
        self._settings_file = HOME / "ui.json"
        from pixiv_dl.applog import setup_logging
        from pixiv_dl.web import server as srv
        setup_logging(console=False, to_file=True)
        self._srv = srv
        self._app = srv.App()

    # ---- 界面设置（主题、字体等）
    def get_config(self):
        try:
            return json.loads(self._settings_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def save_config(self, data):
        if isinstance(data, dict):
            HOME.mkdir(parents=True, exist_ok=True)
            self._settings_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return True

    # ---- 下载模块
    def dl(self, method, path, body=None, query=None):
        """直接调用下载模块的处理函数；返回 {ok, data} 或 {ok: False, status, error}"""
        srv, path = self._srv, str(path or "")
        if not path.startswith("/api/"):
            return {"ok": False, "status": 400, "error": "不支持的接口"}
        try:
            q = {k: [str(v)] for k, v in (query or {}).items() if v is not None}
            for m, rx, fn in srv.ROUTES:
                match = rx.match(path)
                if match and m == str(method or "GET").upper():
                    result = fn(srv.Request(self._app, match, q, body if isinstance(body, dict) else {}))
                    if isinstance(result, srv.Raw):
                        return {"ok": False, "status": 415, "error": "这个接口返回的是文件，不能这样调用"}
                    return {"ok": True, "data": json.loads(json.dumps(result, default=str))}
            return {"ok": False, "status": 404, "error": "接口不存在"}
        except srv.HttpError as e:
            return {"ok": False, "status": e.status, "error": e.message}
        except Exception as e:
            srv.logger.error(f"处理 {path} 失败: {type(e).__name__}: {e}")
            return {"ok": False, "status": 500, "error": f"{type(e).__name__}: {e}"}

    def dl_info(self):
        from webapp import importer
        return {"available": True, "home": str(HOME), "builtinHome": str(HOME), "external": False,
                "files": importer.describe_home(HOME), "viewerKinds": False,
                "kinds": {k: v for k, v in importer.KINDS.items() if k in importer.DOWNLOAD_KINDS},
                "hasData": (HOME / "settings.json").is_file() or (HOME / "db" / "pixiv_manager.db").is_file(),
                "started": True, "version": getattr(self._srv, "VERSION", "")}

    # ---- 导入已有的数据（只有下载数据；评分、收藏属于查看器）
    def import_pick(self, what=None):
        import webview
        if not self._window:
            return []
        if what == "folder":
            picked = self._window.create_file_dialog(webview.FOLDER_DIALOG)
        else:
            picked = self._window.create_file_dialog(webview.OPEN_DIALOG, allow_multiple=True)
        return self.import_inspect(list(picked or []))

    def import_inspect(self, paths):
        from webapp import importer
        found, unknown = {}, []
        home = importer.describe_home(HOME)
        for p in paths or []:
            for item in importer.inspect(str(p)):
                if item["ok"] and item["kind"] in importer.DOWNLOAD_KINDS:
                    item["replaces"] = bool(home.get(item["kind"], {}).get("exists"))
                    item["merge"] = False
                    found[item["kind"]] = item
                elif item["ok"]:
                    unknown.append(dict(item, ok=False, problem="这是看图软件的数据，下载器用不上"))
                else:
                    unknown.append(item)
        return list(found.values()) + unknown

    def import_apply(self, paths):
        from webapp import importer
        items = [i for i in self.import_inspect(paths) if i["ok"]]
        if not items:
            return {"ok": False, "error": "没有可以导入的内容"}
        if self._app.runner.running:
            return {"ok": False, "error": "有下载任务正在运行，结束后再导入"}
        import gc
        gc.collect()                                   # 尽量释放已经不用的数据库连接
        results = importer.import_download_data(items, HOME)
        restart = False
        for r, item in zip(results, items):
            if r["kind"] == "works_db" and not r["ok"]:
                # 本程序自己正开着数据库，换不掉：记下来，下次启动、打开数据库之前再导入
                PENDING.write_text(json.dumps([item["path"]], ensure_ascii=False), encoding="utf-8")
                r.update(ok=True, message="数据库正在使用中，已安排在下次启动时导入。请关闭程序后重新打开。")
                restart = True
        from pixiv_dl.config import Config
        try:
            Config.load_settings()
        except Exception:
            pass
        self._app.pro.clients.clear()
        self._app.reset_storage()
        self._app.invalidate()
        return {"ok": all(r["ok"] for r in results), "results": results, "restart": restart}

    def open_home(self):
        HOME.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            os.startfile(str(HOME))
        return True

    # ---- 系统
    def pick_directory(self, kind=None):
        import webview
        result = self._window.create_file_dialog(webview.FOLDER_DIALOG) if self._window else None
        if not result:
            return None
        return result[0] if isinstance(result, (list, tuple)) else result

    def save_text(self, name, text):
        """让用户选个地方，把一段文字存成文件（导出清单用）。返回保存的路径；取消了返回空字符串。"""
        import webview
        if not self._window:
            return ""
        picked = self._window.create_file_dialog(webview.SAVE_DIALOG, save_filename=str(name or "导出.txt"))
        if not picked:
            return ""
        path = picked if isinstance(picked, str) else picked[0]
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            f.write(str(text or ""))
        return path

    def open_url(self, url):
        if not str(url).startswith(("https://", "http://")):
            return False
        import webbrowser
        webbrowser.open(str(url))
        return True

    def window_action(self, action):
        from webapp import chrome
        return chrome.window_action(self._window, str(action))

    def _shutdown(self) -> None:
        """关闭窗口时：让正在跑的任务处理完当前文件再停"""
        from pixiv_dl import interrupt
        interrupt.set()
        runner = getattr(self._app.runner, "_thread", None)
        if runner is not None:
            runner.join(timeout=20)


def main() -> int:
    try:
        import webview
    except ImportError:
        print("缺少 pywebview：请先运行  pip install pywebview", file=sys.stderr)
        return 1
    HOME.mkdir(parents=True, exist_ok=True)
    if PENDING.is_file():
        # 在打开数据库之前，把上次安排好的导入做完
        try:
            from webapp import importer
            paths = json.loads(PENDING.read_text(encoding="utf-8"))
            items = [i for p in paths for i in importer.inspect(p) if i["ok"] and i["kind"] in importer.DOWNLOAD_KINDS]
            importer.import_download_data(items, HOME)
        except Exception as e:
            print(f"导入失败: {e}", file=sys.stderr)
        finally:
            PENDING.unlink(missing_ok=True)
    api = DlApi()
    from webapp.login_window import apply_browser_proxy
    apply_browser_proxy(HOME / "settings.json")      # 登录窗口和下载用同一个代理
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), StaticHandler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, name="ui-files", daemon=True).start()

    icon = WEBUI_DIR / "app.ico"
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("PixivDownloader.App")
        except Exception:
            pass
    window = webview.create_window(
        "Pixiv 下载器", f"http://127.0.0.1:{httpd.server_address[1]}/downloader.html", js_api=api,
        width=1040, height=720, min_size=(760, 520), background_color="#FFFFFF", text_select=False,
        frameless=True, easy_drag=False,
    )
    api._window = window

    def on_shown():
        from webapp import chrome
        chrome.install(window)

    window.events.shown += on_shown
    try:
        # 界面的本地存储放在数据目录里，不写到用户的漫游配置里
        webview.start(private_mode=False, storage_path=str(HOME / "webview"), icon=str(icon) if icon.is_file() else None)
    finally:
        api._shutdown()
        httpd.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())

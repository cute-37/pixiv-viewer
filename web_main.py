#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Pixiv 图片查看器 —— 新界面（网页前端 + Python 后端）

界面在 webui/，由 pywebview 用系统的 WebView2 显示；图片与缩略图由本机 HTTP 服务提供，
资料库、评分、标签等通过 webapp.api.Api 提供给前端。

    python web_main.py
"""
from __future__ import annotations

import logging
import os
import secrets
import sys
from pathlib import Path

FROZEN = getattr(sys, "frozen", False)
if FROZEN and "--apply-update" not in sys.argv[1:2]:
    # 打包成程序后：数据（配置、缩略图缓存、日志、下载数据）放在程序旁边的 data 文件夹里，而不是程序内部
    os.environ.setdefault("PIXIV_VIEWER_DATA_DIR", str(Path(sys.executable).resolve().parent / "data"))


def main() -> int:
    if len(sys.argv) >= 3 and sys.argv[1] == "--apply-update":
        # 更新：这是刚下载好的新版本，被旧版本启动来替换它的文件（见 webapp/updater.py）
        from webapp.updater import apply_main
        return apply_main(sys.argv[2:])
    if len(sys.argv) >= 3 and sys.argv[1] == "--dl-worker":
        # 打包后的程序用自己充当下载功能的工作进程（见 webapp/downloader.py）
        from webapp.dl_worker import main as worker_main
        return worker_main(*sys.argv[2:4])

    from utils.logger import setup_logging, get_logger
    setup_logging(level=logging.INFO, log_to_file=True, log_to_console=True)
    logger = get_logger("WebMain")

    try:
        import webview
    except ImportError:
        print("缺少 pywebview：请先运行  pip install pywebview", file=sys.stderr)
        return 1

    from utils.config_manager import get_config_manager
    from utils.constants import APP_NAME, APP_VERSION
    from utils.database import db
    from utils.pixiv_metadata_reader import get_pixiv_reader
    from webapp.api import Api
    from webapp.server import MediaServer

    cm = get_config_manager()
    cm.load()
    token = secrets.token_urlsafe(24)
    api = Api(cm, get_pixiv_reader(), db, token=token)
    server = MediaServer(token, api._allowed, api=api, avatar=api._avatar_path)
    server.start()

    # 登录 Pixiv 的窗口要和下载用同一个代理；内置浏览器的代理只能在开第一个窗口之前定下来
    from webapp.login_window import apply_browser_proxy
    apply_browser_proxy(api._downloader_home() / "settings.json")

    window = webview.create_window(
        APP_NAME, server.url + "/index.html", js_api=api,
        width=1440, height=900, min_size=(900, 600), background_color="#FFFFFF", text_select=False,
        frameless=True, easy_drag=False,   # 标题栏由界面自己画（webui 里的 .winctl），见 webapp/chrome.py
    )
    api._window = window

    def on_shown():
        from webapp import chrome
        chrome.install(window)

    window.events.shown += on_shown

    def refresh_ui(done: bool = False):
        # 扫描过程中只刷新侧栏计数；扫描结束才重新载入作品列表，避免网格反复重建
        try:
            window.evaluate_js(f"window.__pvLibraryUpdated && window.__pvLibraryUpdated({'true' if done else 'false'})")
        except Exception:
            pass

    def on_started():
        api._start_indexing(on_progress=refresh_ui, on_done=lambda: refresh_ui(True))

    # 调试用：设置环境变量 PV_DEBUG_PORT=端口号 后，可以用浏览器开发者工具连接到界面
    if os.environ.get("PV_DEBUG_PORT", "").isdigit():
        webview.settings["REMOTE_DEBUGGING_PORT"] = int(os.environ["PV_DEBUG_PORT"])

    # 窗口和任务栏用自己的图标，而不是 Python 的（图标由 scripts/make_icon.py 生成）
    icon = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / "webui" / "app.ico"
    if sys.platform == "win32":
        try:
            import ctypes
            # 任务栏按这个标识分组和取图标；不设的话会和别的 Python 程序算作一个
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("PixivViewer.App")
        except Exception:
            pass

    logger.info(f"启动 {APP_NAME} v{APP_VERSION}")
    try:
        extra = {}
        if FROZEN:
            # 打包后的程序把界面的浏览器数据也放在自己的 data 文件夹里，不写到系统的用户目录
            extra["storage_path"] = str(Path(os.environ["PIXIV_VIEWER_DATA_DIR"]) / "webview")
        webview.start(on_started, private_mode=False, icon=str(icon) if icon.is_file() else None, **extra)
    finally:
        api._shutdown()      # 让下载器工作进程收尾退出
        server.stop()
        db.close_thread_connection()
    return 0


if __name__ == "__main__":
    sys.exit(main())

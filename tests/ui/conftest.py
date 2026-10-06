"""前端测试：用真实浏览器打开 webui（模拟数据），像用户一样点击、拖动、按键。

需要 playwright（pip install playwright）；浏览器优先用系统自带的 Edge，没有再用 playwright 自己下载的 Chromium。
缺少其中任何一样时，这一组测试自动跳过。
"""
import functools
import http.server
import threading
from pathlib import Path

import pytest

WEBUI = Path(__file__).resolve().parents[2] / "webui"


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


@pytest.fixture(scope="session")
def base_url():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(_Quiet, directory=str(WEBUI)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture(scope="session")
def browser():
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as p:
        instance = None
        for options in ({"channel": "msedge"}, {}):
            try:
                instance = p.chromium.launch(headless=True, **options)
                break
            except Exception:
                continue
        if instance is None:
            pytest.skip("没有可用的浏览器（Edge 或 playwright 的 Chromium）")
        yield instance
        instance.close()


@pytest.fixture
def page(browser, base_url):
    """每个测试一个全新的页面（设置、文件夹都回到模拟数据的初始状态）"""
    context = browser.new_context(viewport={"width": 1280, "height": 800})
    # 测试不依赖外网：在线字体直接放弃，用系统字体
    context.route("**/fonts.googleapis.com/**", lambda route: route.abort())
    context.route("**/fonts.gstatic.com/**", lambda route: route.abort())
    pg = context.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.goto(f"{base_url}/index.html?works=200")
    pg.wait_for_selector("#grid-root .tile")
    pg.wait_for_selector("#artists [data-folder]")
    yield pg
    context.close()
    assert not errors, f"页面脚本报错: {errors}"

#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""生成 README 用的截图（docs/screenshots/）。

用浏览器打开界面的示例数据版（图片是程序现场画的，画师和作品都是虚构的），所以截图里没有任何真实作品或个人数据。
以两倍像素密度截取，放大看也清楚。界面改了以后重新跑一遍：

    python scripts/make_screenshots.py

需要 playwright（pip install playwright）和系统自带的 Edge。
"""
import functools
import http.server
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "screenshots"
SIZE = {"width": 1440, "height": 900}


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(ROOT / "webui")))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/index.html?works=400"
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)

        def open_page(settings=None):
            context = browser.new_context(viewport=SIZE, device_scale_factor=2)
            page = context.new_page()
            # 示例提示条不进截图；需要的设置在页面脚本运行前写好
            page.add_init_script("try { sessionStorage.setItem('pv-demo-hint', '1'); } catch (e) {}")
            if settings:
                page.add_init_script(f"try {{ localStorage.setItem('pv-web-settings', JSON.stringify({settings})); }} catch (e) {{}}")
            page.goto(url)
            page.wait_for_selector("#grid-root .tile img.ok")
            page.evaluate("document.fonts.ready")
            page.wait_for_timeout(1500)
            return page

        def shot(page, name):
            page.mouse.move(700, 5)
            page.wait_for_timeout(400)
            page.screenshot(path=str(OUT / f"{name}.png"))
            print("saved", name)

        # 1. 主界面
        page = open_page()
        shot(page, "grid")

        # 2. 画师页：展开一个多页作品
        page.locator("#artists [data-artist]:not(.sub)").nth(1).click()
        page.wait_for_selector("#grid-root .tile img.ok")
        expand = page.locator("#grid-root .tile [data-expand]").first
        if expand.count():
            expand.click()
            page.wait_for_selector("#grid-root .tile.sub img.ok")
        page.evaluate("document.querySelector('#content').scrollTop = 0")
        page.wait_for_timeout(800)
        shot(page, "artist")

        # 3. 看图页（带信息栏）
        page.locator("#nav .row-btn").first.click()
        page.wait_for_selector("#grid-root .tile img.ok")
        page.locator("#grid-root .tile").nth(3).dblclick()
        page.wait_for_selector("#viewer:not([hidden])")
        page.wait_for_timeout(2000)
        page.mouse.move(600, 450)
        page.wait_for_timeout(300)
        page.screenshot(path=str(OUT / "viewer.png"))
        print("saved viewer")
        page.keyboard.press("Escape")

        # 4. 下载与更新
        page.click("#btn-dl")
        page.wait_for_selector(".dlc")
        page.wait_for_timeout(1200)
        shot(page, "download")
        page.keyboard.press("Escape")

        # 5. 设置：外观
        page.click("#btn-settings")
        page.wait_for_selector(".dnav [data-page]")
        page.locator(".dnav [data-page]").first.click()
        page.wait_for_timeout(600)
        shot(page, "settings")
        page.context.close()

        # 6. 深色
        page = open_page('{"mode": "dark"}')
        shot(page, "dark")
        page.context.close()
        browser.close()
    server.shutdown()


if __name__ == "__main__":
    main()

#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""发版前的冒烟检查：在打包好的程序（真实窗口）里把主要功能走一遍。

    python scripts/build_viewer.py        # 先打包
    python scripts/smoke_packaged.py      # 再检查；有一项不过就以非零状态退出

检查的内容：首次打开的语言选择、三种语言、设置各页、备份、开发者模式、AI 助手接口（PixivViewer.exe --mcp）、托盘、
亮暗切换、关闭提示、设置存盘、第二次启动时用上存下来的扫描结果、日志里没有 ERROR。
用的是全新的临时数据目录和一个临时小图库，不碰任何真实数据，也不访问 Pixiv。会真的弹出程序窗口，跑一分钟左右。
浏览器预览和单元测试覆盖不到“打包之后、在真实窗口里”才会出的问题，所以单独有这一步。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright

EXE = Path(__file__).resolve().parents[1] / "dist" / "PixivViewer" / "PixivViewer.exe"
if not EXE.is_file():
    sys.exit(f"没有找到 {EXE}，先运行 python scripts/build_viewer.py")
PORT = 9341
POLL = 2.0            # 托盘多久刷新一次状态（webapp/tray.py 的 POLL_SECS）
tmp = Path(tempfile.mkdtemp(prefix="pv_smoke_"))
data, lib = tmp / "data", tmp / "library"
for artist, ids in (("[101] あおい凪", (500001, 500002, 500003)), ("[202] mizuna", (600001,))):
    for i in ids:
        for page in range(2 if i == 500001 else 1):
            f = lib / artist / f"{i}_p{page}.png"
            f.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (300, 420), (40 + i % 200, 120, 160)).save(f)
(data / "config").mkdir(parents=True)
(data / "config" / "config.json").write_text(json.dumps({"image_paths": [str(lib)]}), encoding="utf-8")
env = {**os.environ, "PIXIV_VIEWER_DATA_DIR": str(data), "PV_DEBUG_PORT": str(PORT)}
report, problems = {}, []


def check(name, ok, detail=""):
    report[name] = "OK" if ok else f"FAIL {detail}"
    if not ok:
        problems.append(name)


def start():
    proc = subprocess.Popen([str(EXE)], env=env, cwd=str(EXE.parent))
    for _ in range(90):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version", timeout=1)
            return proc
        except Exception:
            time.sleep(1)
    raise RuntimeError("窗口没有起来")


def stop(proc):
    subprocess.run(f"taskkill /F /T /PID {proc.pid}", shell=True, capture_output=True)
    time.sleep(2)


def mcp(messages):
    out = subprocess.run([str(EXE), "--mcp"], input="\n".join(json.dumps(m) for m in messages).encode("utf-8"),
                         capture_output=True, env=env, cwd=str(EXE.parent), timeout=90)
    return {r["id"]: r for r in (json.loads(x) for x in out.stdout.decode("utf-8", "replace").splitlines() if x.strip())}, out.stderr.decode("utf-8", "replace")[-300:]


proc = start()
try:
    with sync_playwright() as p:
        b = p.chromium.connect_over_cdp(f"http://127.0.0.1:{PORT}")
        pg = b.contexts[0].pages[0]
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        # ---- 第一次打开：语言选择
        pg.wait_for_selector(".lang-ask", timeout=60000)
        check("首次打开弹出语言选择", pg.locator("[data-pick-lang]").count() == 3)
        pg.locator("[data-pick-lang=ja]").click()
        pg.wait_for_selector("#grid-root .tile", timeout=60000)
        pg.wait_for_timeout(1500)
        check("日语界面", pg.locator("#nav").inner_text().split("\n")[0] == "すべての画像", pg.locator("#nav").inner_text()[:40])
        check("图库扫描到 4 个作品", pg.locator("#grid-root .tile").count() == 4, str(pg.locator("#grid-root .tile").count()))
        check("画师名不翻译", "あおい凪" in pg.locator("#artists").inner_text())
        # ---- 设置各页
        pg.click("#btn-settings")
        pages = [b_.get_attribute("data-page") for b_ in pg.locator(".dnav [data-page]").all()]
        for key in pages:
            pg.click(f".dnav [data-page={key}]")
            pg.wait_for_timeout(700)
        check("设置各页都能打开", pages == ["look", "grid", "viewer", "library", "dl-link", "dl-accounts", "dl-storage", "dl-content", "dl-speed", "general", "keys", "about"], str(pages))
        left = [t for t, w in pg.evaluate("window.__i18nMissed()") if not w.startswith(("button.tag", "button.row-btn", "b")) and "あおい" not in t and "mizuna" not in t]
        check("日语下设置里没有残留中文", len(left) <= 3, str(left[:8]))
        # ---- 备份
        pg.click(".dnav [data-page=library]")
        pg.wait_for_selector("#backup-box")
        target = tmp / "manual-backup.zip"
        r = pg.evaluate("t => window.pywebview.api.backup_now(t)", str(target))
        ok = bool(r and r.get("ok")) and target.is_file() and "manifest.json" in zipfile.ZipFile(target).namelist()
        check("立即备份写出了压缩包", ok, str(r))
        autos = list((data / "backups").glob("PixivViewer-backup-*.zip"))
        check("启动时做了第一份自动备份", len(autos) == 1, str(autos))
        info = pg.evaluate("window.pywebview.api.backup_info()")
        check("备份信息能读到", len(info["items"]) == 1 and info["everyDays"] == 7)
        # ---- 开发者模式 + 接口体检（没有账号：应当明确说没有账号，而不是出错）
        pg.click(".dnav [data-page=about]")
        pg.locator("[data-toggle-dev]").click()
        pg.wait_for_selector(".dnav [data-page=dev].on")
        pg.locator("[data-dev=apicheck]").click()
        pg.wait_for_function("document.querySelector('#dpage small.bad')", timeout=60000)
        check("接口体检在没有账号时给出明确提示", "アカウント" in pg.locator("#dpage small.bad").first.inner_text(), pg.locator("#dpage small.bad").first.inner_text())
        # ---- AI 助手接口
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26", "capabilities": {}}}
        call = lambda i, name, args=None: {"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {"name": name, "arguments": args or {}}}
        replies, err = mcp([init, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, call(3, "library_overview")])
        check("--mcp 能启动并回应", 1 in replies and replies[1]["result"]["serverInfo"]["name"] == "pixiv-viewer", err)
        check("--mcp 列出工具", len(replies.get(2, {}).get("result", {}).get("tools", [])) >= 20)
        check("默认关闭时拒绝访问", replies[3]["result"].get("isError") and "turned off" in replies[3]["result"]["content"][0]["text"], str(replies.get(3))[:200])
        pg.click(".dnav [data-page=general]")
        pg.wait_for_selector("#mcp-box [data-set=mcp]")
        pg.locator("#mcp-box [data-v=read]").click()
        pg.wait_for_selector(".mcp-config")
        cfg = json.loads(pg.locator(".mcp-config").inner_text())
        check("设置页给出的配置指向这个程序", cfg["mcpServers"]["pixiv-viewer"] == {"command": str(EXE), "args": ["--mcp"]}, str(cfg))
        pg.wait_for_timeout(900)                         # 等设置存盘
        replies, err = mcp([init, call(3, "library_overview"), call(4, "search_works", {"artist": "101"}), call(5, "get_image", {"work": "500001", "page": 1}),
                            call(6, "set_rating", {"works": ["500001"], "stars": 5}), call(7, "download_status")])
        over = json.loads(replies[3]["result"]["content"][0]["text"])
        check("只读级别：图库概况", over["works"] == 4 and over["images"] == 5, str(over))
        found = json.loads(replies[4]["result"]["content"][0]["text"])
        check("只读级别：按画师搜作品", found["total"] == 3 and found["works"][0]["artist"] == "あおい凪", str(found)[:200])
        check("只读级别：看图返回图片", replies[5]["result"]["content"][0]["type"] == "image" and len(replies[5]["result"]["content"][0]["data"]) > 500)
        check("只读级别：不能改评分", replies[6]["result"].get("isError") is True)
        check("只读级别：下载状态（会拉起下载进程）", not replies[7]["result"].get("isError"), replies[7]["result"]["content"][0]["text"][:200])
        # ---- 托盘：放进去之后图标在、悬浮提示和右键菜单是对的，再叫回来
        pg.keyboard.press("Escape")
        check("放到托盘", pg.evaluate("window.pywebview.api.window_action('tray')") is True)
        time.sleep(POLL + 1.5)
        info = pg.evaluate("window.pywebview.api.tray_info()")
        native = info["native"]
        check("托盘图标真的出现了", info["hidden"] is True and native.get("icon") is True, str(native))
        check("托盘悬浮提示显示任务状态", native.get("tip", "").startswith("Pixiv Viewer\n") and "実行中のタスクはありません" in native.get("tip", ""), repr(native.get("tip")))
        check("托盘右键菜单的各项", native.get("menu") == ["Pixiv Viewer を開く", "実行中のタスクはありません", "ダウンロードと更新…", "設定…", "終了"], str(native.get("menu")))
        pg.evaluate("window.pywebview.api.window_action('restore')")
        time.sleep(1)
        check("从托盘叫回来", pg.evaluate("window.pywebview.api.tray_info()")["hidden"] is False)
        # ---- 切到英文（整页重新载入），再切回中文
        pg.click("#btn-settings")
        pg.click(".dnav [data-page=look]")
        pg.locator("[data-lang=en]").click()
        pg.wait_for_function("document.documentElement.lang === 'en'", timeout=30000)
        pg.wait_for_selector("#grid-root .tile")
        check("切到英文", pg.locator("#nav").inner_text().split("\n")[0] == "All images")
        pg.click("#btn-settings")
        pg.click(".dnav [data-page=look]")
        pg.locator("[data-lang=zh]").click()
        pg.wait_for_function("document.documentElement.lang === 'zh-CN'", timeout=30000)
        pg.wait_for_selector("#grid-root .tile")
        check("切回中文", pg.locator("#nav").inner_text().split("\n")[0] == "全部图片")
        # ---- 亮暗切换、关闭提示
        before = pg.evaluate("document.documentElement.dataset.theme")
        pg.click("#btn-mode")
        pg.wait_for_function("t => document.documentElement.dataset.theme !== t", arg=before)
        check("亮暗切换", True)
        pg.click("#winctl [data-win=close]")
        pg.wait_for_selector(".closeask")
        check("关闭窗口时询问", "托盘" in pg.locator(".closeask").inner_text())
        pg.keyboard.press("Escape")
        check("页面脚本没有报错", not errors, str(errors[:3]))
        saved = json.loads((data / "config" / "web_ui.json").read_text(encoding="utf-8"))
        check("设置存下来了", saved.get("lang") == "zh" and saved.get("mcp") == "read" and saved.get("devMode") is True, str({k: saved.get(k) for k in ("lang", "mcp", "devMode")}))
    check("运行时写了连接信息", (data / "runtime.json").is_file())
finally:
    stop(proc)

# ---- 第二次启动：不再问语言；文件夹没变，直接用上次的扫描结果
proc = start()
try:
    with sync_playwright() as p:
        b = p.chromium.connect_over_cdp(f"http://127.0.0.1:{PORT}")
        pg = b.contexts[0].pages[0]
        pg.wait_for_selector("#grid-root .tile", timeout=60000)
        check("第二次启动不再问语言", pg.locator(".lang-ask").count() == 0)
        check("第二次启动图库还在", pg.locator("#grid-root .tile").count() == 4)
    time.sleep(2)
    logs = "".join(f.read_text(encoding="utf-8", errors="replace") for f in (data / "logs").glob("pixiv_viewer_*.log"))
    last = [line for line in logs.splitlines() if "资料库索引完成" in line][-1]
    check("第二次启动用了存下来的扫描结果", "0 位重新扫描" in last and "直接用了上次的结果" in last, last[-120:])
    bad = [line for line in logs.splitlines() if "| ERROR" in line]
    check("日志里没有 ERROR", not bad, str(bad[:3])[:400])
    check("只有一份自动备份（一周内不重复做）", len(list((data / "backups").glob("*.zip"))) == 1)
finally:
    stop(proc)

for k, v in report.items():
    print(("  " if v == "OK" else "X ") + k + ("" if v == "OK" else "  -> " + v))
print(f"{len(report) - len(problems)} / {len(report)} 通过")
shutil.rmtree(tmp, ignore_errors=True)
sys.exit(1 if problems else 0)

#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把默认字体下载到 webui/fonts/，并生成 webui/css/fonts.css。

软件默认用的两款字体（正文 霞鹜文楷 TC、标题 M PLUS Rounded 1c）随程序一起发布，断网时外观也不变。
这个脚本只在想更新字体文件时才需要跑（会访问 Google Fonts）：

    python scripts/fetch_fonts.py

字体按 Google Fonts 的方式切成很多小片（每片只含一部分字），浏览器用到哪些字才读哪几片。
两款字体都以 SIL Open Font License 1.1 发布，许可说明见 webui/fonts/LICENSE.txt。
"""
import concurrent.futures
import re
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
FONTS = ROOT / "webui" / "fonts"
CSS = ROOT / "webui" / "css" / "fonts.css"
SOURCE = ("https://fonts.googleapis.com/css2?family=M+PLUS+Rounded+1c:wght@400;500;700"
          "&family=LXGW+WenKai+TC:wght@400;700&display=swap")
# 要用新浏览器的标识去取，才会拿到 woff2 + 分片的写法
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
SHORT = {"LXGW WenKai TC": "wenkai", "M PLUS Rounded 1c": "mplus"}


def main():
    session = requests.Session()
    session.headers["User-Agent"] = UA
    css = session.get(SOURCE, timeout=60).text
    blocks = re.findall(r"@font-face\s*\{.*?\}", css, re.S)
    if not blocks:
        sys.exit("没有取到字体样式，检查网络")
    FONTS.mkdir(parents=True, exist_ok=True)
    jobs, out, counters = [], [], {}
    for block in blocks:
        family = re.search(r"font-family:\s*'([^']+)'", block).group(1)
        weight = re.search(r"font-weight:\s*(\d+)", block).group(1)
        url = re.search(r"url\((https://[^)]+)\)", block).group(1)
        key = f"{SHORT[family]}-{weight}"
        counters[key] = counters.get(key, 0) + 1
        name = f"{key}-{counters[key]:03d}.woff2"
        jobs.append((url, FONTS / name))
        out.append(re.sub(r"\s+", " ", block.replace(url, f"../fonts/{name}")).replace("{ ", "{").replace("; }", "}"))

    def fetch(job):
        url, path = job
        if path.exists() and path.stat().st_size:
            return 0
        data = session.get(url, timeout=60).content
        path.write_bytes(data)
        return len(data)

    with concurrent.futures.ThreadPoolExecutor(12) as pool:
        fetched = sum(pool.map(fetch, jobs))
    keep = {path.name for _, path in jobs}
    for stale in FONTS.glob("*.woff2"):
        if stale.name not in keep:
            stale.unlink()
    CSS.write_text("/* 随程序发布的默认字体（由 scripts/fetch_fonts.py 生成，不要手改） */\n" + "\n".join(out) + "\n",
                   encoding="utf-8", newline="\n")
    total = sum(path.stat().st_size for _, path in jobs)
    print(f"{len(jobs)} 个字体分片，共 {total / 1048576:.1f} MB（本次下载 {fetched / 1048576:.1f} MB）")


if __name__ == "__main__":
    main()

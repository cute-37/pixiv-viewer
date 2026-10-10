#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Pixiv 接口体检：软件依赖的接口和字段还在不在（清单见 pixiv_dl/apicheck.py）。发版前跑一次。

    python scripts/check_api.py                # 用查看器当前的数据文件夹
    python scripts/check_api.py --home <数据文件夹> --account <账号名>

只读：每个接口问一次 Pixiv（一共不到十次请求），不下载、不改任何设置、不写数据库。
有接口变了时以非零状态退出。变了之后的处理流程见 docs/DEVELOPMENT.md 的“重要更新提醒”。
"""
import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))


def main():
    from check_probes import default_home
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--home", help="下载功能的数据文件夹（里面有 settings.json）")
    parser.add_argument("--account", help="用哪个账号检查，默认主账号")
    args = parser.parse_args()
    home = Path(args.home) if args.home else default_home()
    if not (home / "settings.json").is_file():
        sys.exit(f"{home} 里没有 settings.json，用 --home 指定数据文件夹")
    os.environ["PIXIV_DL_HOME"] = str(home)

    import sqlite3
    from types import SimpleNamespace

    import requests

    from pixiv_dl import apicheck, proxy
    from pixiv_dl.config import Config
    from pixiv_dl.pixiv_client import PixivClient

    Config.load_settings()
    accounts = Config.get_accounts()
    name = args.account or Config.MAIN_ACCOUNT or next(iter(accounts), "")
    if name not in accounts:
        sys.exit("没有可用的账号")
    client = PixivClient(accounts[name]["token"], name)
    if not client.auth():
        sys.exit(f"账号 {name} 登录失败：{client.last_error}")
    db = None
    try:                                         # 只读地打开数据库找例子，不经过会建表、升级结构的那套代码
        db = SimpleNamespace(conn=sqlite3.connect(f"file:{Path(Config.DB_PATH).as_posix()}?mode=ro", uri=True, timeout=5))
    except sqlite3.Error:
        pass
    session = requests.Session()
    proxy.configure_session(session)
    session.headers.update({"Referer": "https://www.pixiv.net/", "User-Agent": Config.USER_AGENT})

    report = apicheck.run(client, db, session)
    marks = {"ok": "正常  ", "changed": "变了  ", "error": "出错  ", "skipped": "跳过  "}
    print(f"账号 {report['account']} · pixivpy {report['library']} · {report['time']}")
    for item in report["items"]:
        print(f"  {marks[item['status']]}{item['label']}（{item['name']}）" + (f"  {item['ms']} ms" if item["ms"] else "")
              + (f"  — {item['note']}" if item["note"] else ""))
        for p in item["problems"]:
            print(f"        {p}")
    print("全部正常" if report["ok"] else "有接口和预期的不一样，见上面")
    sys.exit(0 if report["ok"] else 1)


if __name__ == "__main__":
    main()

#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""检查内置的 R-18 / R-18G 可见性测试作品（pixiv_dl/visibility.py）是否还有效。

这些作品写在程序里，时间长了个别会被作者删除或改分级。失效几张不影响功能（检测时会跳过），
但每一档有效的画师数太少时，就该换一批了。建议发版前跑一次：

    python scripts/check_probes.py                # 用查看器当前的数据文件夹
    python scripts/check_probes.py --home <数据文件夹>

只读：对每个测试作品问一次 Pixiv（共三十来次请求），不下载、不改任何设置。
需要一个 R-18 和 R-18G 都看得到的账号（默认用主账号）。
"""
import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
MIN_ARTISTS = 6                 # 每一档至少要有这么多位画师的测试作品还有效


def default_home():
    """和查看器用同一套规则找数据文件夹（见 webapp/api.py 的 _downloader_home）"""
    from utils.config_manager import get_config_manager
    from utils.constants import DATA_DIR
    from utils.pixiv_metadata_reader import PixivMetadataReader
    from webapp.downloader import resolve_home
    from webapp.store import WebStore
    settings = WebStore().load_settings() or {}
    manager = get_config_manager()
    manager.load()
    reader = PixivMetadataReader()
    reader.set_metadata_path(manager.config.pixiv_metadata_path or "")
    return resolve_home(str(settings.get("downloaderHome") or ""), str(reader.db_path or ""), DATA_DIR / "pixiv")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--home", help="下载功能的数据文件夹（里面有 settings.json）")
    parser.add_argument("--account", help="用哪个账号检查，默认主账号")
    args = parser.parse_args()
    home = Path(args.home) if args.home else default_home()
    if not (home / "settings.json").is_file():
        sys.exit(f"{home} 里没有 settings.json，用 --home 指定数据文件夹")
    os.environ["PIXIV_DL_HOME"] = str(home)

    from pixivpy3 import AppPixivAPI

    from pixiv_dl import visibility
    from pixiv_dl.config import Config
    from pixiv_dl.extract import _g

    Config.load_settings()
    accounts = Config.get_accounts()
    name = args.account or Config.MAIN_ACCOUNT or next(iter(accounts), "")
    if name not in accounts:
        sys.exit("没有可用的账号")
    api = AppPixivAPI(proxies=Config.PROXIES or {})
    api.auth(refresh_token=accounts[name]["token"])

    failed = False
    for level, label in ((1, "R-18"), (2, "R-18G")):
        good_artists, problems = set(), []
        for artist, illust in visibility.PROBES[level]:
            try:
                ill = _g(api.illust_detail(illust), "illust")
            except Exception as error:
                problems.append((illust, f"请求失败: {error}"))
                continue
            finally:
                time.sleep(0.5)
            if not ill:
                problems.append((illust, "已删除或不存在"))
            elif _g(ill, "visible", True) is False:
                problems.append((illust, "这个账号看不到（换一个开了显示的账号再查）"))
            elif _g(ill, "x_restrict") != level:
                problems.append((illust, f"分级变了（现在是 {_g(ill, 'x_restrict')}）"))
            elif _g(_g(ill, "user"), "id") != artist:
                problems.append((illust, "画师对不上"))
            else:
                good_artists.add(artist)
        total = len(visibility.PROBES[level])
        print(f"{label}: {total - len(problems)} / {total} 个作品有效，来自 {len(good_artists)} 位画师")
        for illust, why in problems:
            print(f"    {illust}: {why}")
        if len(good_artists) < MIN_ARTISTS:
            failed = True
            print(f"    有效的画师不足 {MIN_ARTISTS} 位，请更新 pixiv_dl/visibility.py 里的这一档")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()

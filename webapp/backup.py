#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
备份只存在本机的东西：评分、自定义标签、收藏、最近查看、置顶、画师文件夹、界面设置。

这些 Pixiv 上没有，丢了就补不回来（图片和作品信息可以重新下载，它们不行）。

一个备份就是一个压缩包：
    webapp.db    收藏、最近查看、置顶、画师文件夹（去掉了可以重新生成的缓存表，所以很小）
    library.db   评分和自定义标签
    web_ui.json  界面设置和快捷键
    manifest.json  什么时候、哪个版本备份的、里面有多少条

自动备份放在 <数据目录>/backups/ 里，只留最近几份。手动备份可以存到任何地方（比如网盘、别的硬盘）。
恢复：在“设置 → 数据导入”里选备份文件，按“合并”处理——现有的记录不会被覆盖或删除。
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
import zipfile
from contextlib import closing
from pathlib import Path
from typing import List, Optional

from utils.constants import APP_VERSION
from utils.logger import get_logger

logger = get_logger("Backup")

AUTO_EVERY_DAYS = 7      # 自动备份的间隔
AUTO_KEEP = 6            # 自动备份留几份
PREFIX = "PixivViewer-backup-"
CACHE_TABLES = ("dims", "scans")      # webapp.db 里可以重新生成的缓存，不放进备份


def _copy_db(src: Path, dst: Path, drop: tuple = ()) -> None:
    """一致地复制一个正在使用的数据库（用 SQLite 自己的备份接口，不怕别的线程正在写）"""
    with closing(sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True, timeout=10)) as a, closing(sqlite3.connect(dst)) as b:
        a.backup(b)
        for table in drop:
            b.execute(f"DROP TABLE IF EXISTS {table}")
        b.commit()
        b.execute("VACUUM")


def _counts(webapp_db: Optional[Path], library_db: Optional[Path]) -> dict:
    out = {}

    def one(path, name, sql):
        try:
            with closing(sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5)) as con:
                out[name] = int(con.execute(sql).fetchone()[0] or 0)
        except (sqlite3.Error, TypeError, AttributeError):
            pass

    if webapp_db:
        for name, sql in (("favorites", "SELECT COUNT(*) FROM favorites"), ("views", "SELECT COUNT(*) FROM views"),
                          ("pins", "SELECT COUNT(*) FROM pins"), ("folders", "SELECT COUNT(*) FROM folders")):
            one(webapp_db, name, sql)
    if library_db:
        one(library_db, "rated", "SELECT COUNT(*) FROM images WHERE rating > 0")
        one(library_db, "tagged", "SELECT COUNT(DISTINCT image_id) FROM image_tags")
    return out


def create(target: Path, webapp_db: Path, library_db: Path, settings_file: Path) -> dict:
    """写出一个备份。返回 {path, bytes, counts}。"""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="pv_backup_") as tmp:
        tmp = Path(tmp)
        files = []
        if Path(webapp_db).is_file():
            _copy_db(Path(webapp_db), tmp / "webapp.db", CACHE_TABLES)
            files.append("webapp.db")
        if Path(library_db).is_file():
            _copy_db(Path(library_db), tmp / "library.db")
            files.append("library.db")
        counts = _counts(tmp / "webapp.db" if "webapp.db" in files else None, tmp / "library.db" if "library.db" in files else None)
        manifest = {"app": "PixivViewer", "kind": "backup", "version": APP_VERSION, "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "files": files, "counts": counts}
        part = target.with_suffix(".part")
        with zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            for name in files:
                z.write(tmp / name, name)
            try:
                if Path(settings_file).is_file():
                    z.write(settings_file, "web_ui.json")
            except OSError:
                pass
        os.replace(part, target)
    return {"path": str(target), "bytes": target.stat().st_size, "counts": counts, "time": manifest["time"]}


def read_manifest(path: Path) -> Optional[dict]:
    """是这个软件的备份就返回它的说明，否则 None"""
    try:
        with zipfile.ZipFile(path) as z:
            data = json.loads(z.read("manifest.json"))
        return data if isinstance(data, dict) and data.get("kind") == "backup" and data.get("app") == "PixivViewer" else None
    except (OSError, KeyError, ValueError, zipfile.BadZipFile):
        return None


def extract(path: Path, into: Path) -> List[Path]:
    """把备份里的数据库解到一个文件夹里，返回解出来的文件（只取认识的那几个名字，不照压缩包里的路径写）"""
    into.mkdir(parents=True, exist_ok=True)
    out = []
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        for name in ("webapp.db", "library.db"):
            if name in names:
                (into / name).write_bytes(z.read(name))
                out.append(into / name)
    return out


def list_auto(folder: Path) -> List[dict]:
    """自动备份文件夹里的备份，新的在前"""
    items = []
    try:
        for p in sorted(Path(folder).glob(PREFIX + "*.zip"), key=lambda x: x.stat().st_mtime, reverse=True):
            items.append({"path": str(p), "name": p.name, "bytes": p.stat().st_size, "time": p.stat().st_mtime})
    except OSError:
        pass
    return items


def auto_due(folder: Path, now: Optional[float] = None) -> bool:
    items = list_auto(folder)
    return not items or (now or time.time()) - items[0]["time"] >= AUTO_EVERY_DAYS * 86400


def auto_backup(folder: Path, webapp_db: Path, library_db: Path, settings_file: Path, force: bool = False) -> Optional[dict]:
    """到时间了就在 folder 里做一份，并删掉太旧的。没到时间返回 None。"""
    folder = Path(folder)
    if not force and not auto_due(folder):
        return None
    result = create(folder / f"{PREFIX}{time.strftime('%Y%m%d-%H%M%S')}.zip", webapp_db, library_db, settings_file)
    for old in list_auto(folder)[AUTO_KEEP:]:
        try:
            os.remove(old["path"])
        except OSError:
            pass
    logger.info(f"已自动备份评分、收藏等本机数据: {result['path']}")
    return result

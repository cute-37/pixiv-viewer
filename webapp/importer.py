# -*- coding: utf-8 -*-
"""
导入已有的数据

用户选一些文件或文件夹，这里按**内容**判断每一个是什么（不看文件名，改过名也认得），再放到该放的位置：

    works_db     作品数据库（原名 pixiv_manager.db）      -> <数据文件夹>/db/pixiv_manager.db
    dl_settings  账号与下载设置（原名 settings.json）      -> <数据文件夹>/settings.json
    avatars      画师头像文件夹                           -> <数据文件夹>/avatars/
    viewer_db    收藏、最近查看、置顶（原名 webapp.db）    -> 合并进当前的记录
    ratings_db   评分与自定义标签（原名 library.db）       -> 合并进当前的记录

前三种是“换成新的”：原来的那份改名留在原地（文件名里带“旧”和时间），不删除。
后两种是“合并”：只添加，不清掉现有的记录。所有操作都是复制，用户选的原文件不动。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from utils.logger import get_logger

logger = get_logger("Importer")

AVATAR_RE = re.compile(r"^\d+\.(png|jpe?g|gif|webp)$", re.IGNORECASE)
KINDS: Dict[str, dict] = {
    "works_db": {"label": "作品数据库", "was": "pixiv_manager.db", "need": "核心",
                 "what": "画师、作品、标题、标签、分级和下载记录。没有它，图片只能按文件名显示。"},
    "dl_settings": {"label": "账号与下载设置", "was": "settings.json", "need": "可选",
                    "what": "Pixiv 账号的登录凭证、保存位置、下载选项。不导入的话重新登录、重新设置即可。"},
    "avatars": {"label": "画师头像", "was": "avatars 文件夹", "need": "可选",
                "what": "侧栏里的画师头像。不导入的话可以之后用“补全头像”从 Pixiv 下载。"},
    "viewer_db": {"label": "收藏与最近查看", "was": "webapp.db", "need": "可选",
                  "what": "收藏、最近查看、置顶的画师。只存在本机，不导入就没有。"},
    "ratings_db": {"label": "评分与自定义标签", "was": "library.db", "need": "可选",
                   "what": "你给图片打的星和加的标签。只存在本机，不导入就没有。"},
}
DOWNLOAD_KINDS = ("works_db", "dl_settings", "avatars")
VIEWER_KINDS = ("viewer_db", "ratings_db")


# ====================================================================== 识别
def _tables(path: Path) -> Optional[set]:
    """是 SQLite 数据库就返回里面的表名，否则 None"""
    try:
        with open(path, "rb") as f:
            if f.read(16) != b"SQLite format 3\x00":
                return None
        with closing(sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=3)) as con:
            return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    except (OSError, sqlite3.Error):
        return None


def _count(path: Path, sql: str) -> int:
    try:
        with closing(sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=3)) as con:
            return int(con.execute(sql).fetchone()[0] or 0)
    except (sqlite3.Error, TypeError):
        return 0


def _item(path: Path, kind: str = "", detail: str = "", problem: str = "") -> dict:
    info = KINDS.get(kind, {})
    return {"path": str(path), "name": path.name, "kind": kind, "label": info.get("label", "认不出来"),
            "detail": detail, "problem": problem, "ok": bool(kind) and not problem}


def _inspect_file(path: Path) -> dict:
    tables = _tables(path)
    if tables is not None:
        if {"illust_metadata", "artists", "illusts"} <= tables:
            works = _count(path, "SELECT COUNT(*) FROM illust_metadata")
            artists = _count(path, "SELECT COUNT(*) FROM artists")
            return _item(path, "works_db", f"{artists} 位画师 · {works} 个作品")
        if {"favorites", "views", "pins"} <= tables:
            return _item(path, "viewer_db", f"{_count(path, 'SELECT COUNT(*) FROM favorites')} 个收藏 · "
                                             f"{_count(path, 'SELECT COUNT(*) FROM views')} 条最近查看")
        if {"images", "tags", "image_tags"} <= tables:
            rated = _count(path, "SELECT COUNT(*) FROM images WHERE rating > 0")
            tagged = _count(path, "SELECT COUNT(DISTINCT image_id) FROM image_tags")
            return _item(path, "ratings_db", f"{rated} 张有评分 · {tagged} 张有标签")
        return _item(path, problem="这是一个数据库，但不是这里用得上的那几种")
    try:
        if path.stat().st_size > 20 * 1024 * 1024:
            return _item(path, problem="不是数据库，也不像设置文件")
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return _item(path, problem="既不是数据库，也不是设置文件")
    current = data.get("current") if isinstance(data, dict) else None
    if isinstance(current, dict) and {"TOKENS", "STORAGE_MODE", "MAIN_ACCOUNT", "LOCAL_SAVE_PATH"} & set(current):
        accounts = len(current.get("TOKENS") or {})
        return _item(path, "dl_settings", f"{accounts} 个账号 · 保存方式 {current.get('STORAGE_MODE') or 'local'}")
    return _item(path, problem="是 JSON 文件，但不是下载设置")


def _is_avatar_dir(path: Path) -> int:
    """像头像文件夹就返回里面的头像数量（文件名是 画师ID.图片后缀），否则 0"""
    try:
        names = [e.name for e in os.scandir(path) if e.is_file()]
    except OSError:
        return 0
    hits = sum(1 for n in names if AVATAR_RE.match(n))
    return hits if hits and hits >= len(names) * 0.5 else 0


def inspect(path_str: str) -> List[dict]:
    """识别一个路径。文件夹可能是头像文件夹，也可能是整个旧的数据文件夹（那就把里面认得的都找出来）。"""
    path = Path(path_str)
    try:
        if path.is_file():
            return [_inspect_file(path)]
        if not path.is_dir():
            return [_item(path, problem="找不到这个文件")]
    except OSError:
        return [_item(path, problem="无法读取")]
    n = _is_avatar_dir(path)
    if n:
        return [_item(path, "avatars", f"{n} 个头像")]
    found: List[dict] = []
    # 整个数据文件夹：往下找两层（settings.json、db/xxx.db、avatars/、webapp.db、library.db）
    candidates = []
    try:
        for e in os.scandir(path):
            if e.is_dir():
                candidates.append(Path(e.path))
                if e.name.lower() in ("db", "data", "pixiv"):
                    candidates += [Path(s.path) for s in os.scandir(e.path)]
            elif e.name.lower().endswith((".db", ".json", ".sqlite", ".sqlite3", ".bak")):
                candidates.append(Path(e.path))
    except OSError:
        pass
    for c in candidates:
        try:
            if c.is_dir():
                count = _is_avatar_dir(c)
                if count:
                    found.append(_item(c, "avatars", f"{count} 个头像"))
            elif c.suffix.lower() in (".db", ".json", ".sqlite", ".sqlite3", ".bak"):
                item = _inspect_file(c)
                if item["ok"]:
                    found.append(item)
        except OSError:
            continue
    # 同一种只留一个（数据库优先留最大的那个，避开备份）
    best: Dict[str, dict] = {}
    for item in found:
        old = best.get(item["kind"])
        if old is None or _size(item["path"]) > _size(old["path"]):
            best[item["kind"]] = item
    return list(best.values()) or [_item(path, problem="这个文件夹里没有找到可以导入的数据")]


def _size(p: str) -> int:
    try:
        return os.path.getsize(p)
    except OSError:
        return 0


# ====================================================================== 数据文件夹里现在有什么
def describe_home(home: Path) -> dict:
    home = Path(home)
    db = home / "db" / "pixiv_manager.db"
    settings = home / "settings.json"
    avatars = home / "avatars"
    out = {
        "works_db": {"path": str(db), "exists": db.is_file(), "detail": ""},
        "dl_settings": {"path": str(settings), "exists": settings.is_file(), "detail": ""},
        "avatars": {"path": str(avatars), "exists": avatars.is_dir(), "detail": ""},
    }
    if db.is_file():
        out["works_db"]["detail"] = _inspect_file(db)["detail"]
    if settings.is_file():
        out["dl_settings"]["detail"] = _inspect_file(settings)["detail"]
    if avatars.is_dir():
        try:
            out["avatars"]["detail"] = f"{sum(1 for e in os.scandir(avatars) if AVATAR_RE.match(e.name))} 个头像"
        except OSError:
            pass
    return out


# ====================================================================== 导入
def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _set_aside(target: Path, stamp: str) -> Optional[Path]:
    """把现有的那份改名留在原地（不删除）；没有就什么都不做。返回旧的那份现在的位置。"""
    if not target.exists():
        return None
    kept = target.with_name(f"{target.stem}.旧-{stamp}{target.suffix}")
    if target.is_file() and target.suffix == ".db":
        # 数据库可能还有没写进主文件的日志：先并进去，旧的那份才是完整的
        try:
            with closing(sqlite3.connect(str(target), timeout=5)) as con:
                con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass
        for extra in ("-wal", "-shm"):
            side = Path(str(target) + extra)
            if side.exists():
                try:
                    side.unlink()
                except OSError:
                    pass
    target.rename(kept)
    return kept


def import_download_data(items: List[dict], home: Path) -> List[dict]:
    """导入作品数据库、账号与下载设置、头像。调用前要确保下载进程已经停了。"""
    home, stamp, results = Path(home), _stamp(), []
    for item in items:
        kind, src = item["kind"], Path(item["path"])
        res = {"kind": kind, "label": KINDS[kind]["label"], "ok": False, "message": "", "kept": ""}
        try:
            if kind == "works_db":
                target = home / "db" / "pixiv_manager.db"
                target.parent.mkdir(parents=True, exist_ok=True)
                tmp = target.with_name(f"import-{stamp}.tmp")
                # 用数据库自己的备份接口复制：源文件正在被别的程序使用时也能得到完整的一份
                with closing(sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True)) as a, \
                        closing(sqlite3.connect(str(tmp))) as b:
                    a.backup(b)
                kept = _set_aside(target, stamp)
                tmp.rename(target)
            elif kind == "dl_settings":
                target = home / "settings.json"
                home.mkdir(parents=True, exist_ok=True)
                tmp = target.with_name(f"import-{stamp}.tmp")
                shutil.copy2(src, tmp)
                kept = _set_aside(target, stamp)
                tmp.rename(target)
            elif kind == "avatars":
                target = home / "avatars"
                kept = _set_aside(target, stamp) if os.path.normcase(str(src)) != os.path.normcase(str(target)) else None
                target.mkdir(parents=True, exist_ok=True)
                n = 0
                for e in os.scandir(src):
                    if e.is_file() and AVATAR_RE.match(e.name):
                        shutil.copy2(e.path, target / e.name)
                        n += 1
                item = dict(item, detail=f"{n} 个头像")
            else:
                raise ValueError("不是下载数据")
            res.update(ok=True, message=item.get("detail", ""), kept=str(kept) if kept else "")
        except Exception as e:
            logger.error(f"导入 {src} 失败: {e}")
            res["message"] = f"没有成功：{e}"
        results.append(res)
    return results


def merge_viewer_db(src: str, store) -> dict:
    """把另一份 webapp.db 里的收藏、最近查看、置顶合并进当前的记录（已有的保留，时间取较新的）"""
    res = {"kind": "viewer_db", "label": KINDS["viewer_db"]["label"], "ok": False, "message": "", "kept": ""}
    try:
        with closing(sqlite3.connect(f"file:{Path(src).as_posix()}?mode=ro", uri=True, timeout=5)) as con:
            favs = con.execute("SELECT key, added FROM favorites").fetchall()
            views = con.execute("SELECT key, ts FROM views").fetchall()
            pins = con.execute("SELECT artist, added FROM pins").fetchall()
        dst = store._conn()
        with dst:
            dst.executemany("INSERT OR IGNORE INTO favorites (key, added) VALUES (?, ?)", favs)
            dst.executemany("INSERT INTO views (key, ts) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET ts = MAX(ts, excluded.ts)", views)
            dst.executemany("INSERT OR IGNORE INTO pins (artist, added) VALUES (?, ?)", pins)
        res.update(ok=True, message=f"合并了 {len(favs)} 个收藏、{len(views)} 条最近查看、{len(pins)} 个置顶")
    except Exception as e:
        logger.error(f"合并 {src} 失败: {e}")
        res["message"] = f"没有成功：{e}"
    return res


def merge_ratings_db(src: str, database) -> dict:
    """把另一份 library.db 里的评分和自定义标签合并进当前的记录（按图片的完整路径对应）"""
    res = {"kind": "ratings_db", "label": KINDS["ratings_db"]["label"], "ok": False, "message": "", "kept": ""}
    try:
        with closing(sqlite3.connect(f"file:{Path(src).as_posix()}?mode=ro", uri=True, timeout=5)) as con:
            ratings = [(p, int(r)) for p, r in con.execute("SELECT path, rating FROM images WHERE rating > 0") if p]
            tagged = con.execute("SELECT i.path, t.name FROM image_tags it JOIN images i ON i.id = it.image_id "
                                 "JOIN tags t ON t.id = it.tag_id").fetchall()
        if ratings:
            database.batch_set_ratings(ratings)
        by_tag: Dict[str, List[str]] = {}
        for path, name in tagged:
            if path and name:
                by_tag.setdefault(name, []).append(path)
        for name, paths in by_tag.items():
            database.add_tags(paths, [name])
        res.update(ok=True, message=f"合并了 {len(ratings)} 张图的评分、{len(tagged)} 条标签记录（按文件路径对应）")
    except Exception as e:
        logger.error(f"合并 {src} 失败: {e}")
        res["message"] = f"没有成功：{e}"
    return res

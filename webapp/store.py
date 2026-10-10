#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
网页前端用到的本地数据：收藏、浏览记录、置顶画师、图片尺寸缓存、界面设置

单独存放在 DATA_DIR/webapp.db 与 CONFIG_DIR/web_ui.json，不改动原有 library.db 的表结构。
评分与自定义标签在 utils.database（library.db）里。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from utils.constants import CONFIG_DIR, DATA_DIR
from utils.logger import get_logger
from utils.schema import migrate

logger = get_logger("WebStore")

# webapp.db 的结构。要改表结构时在末尾追加一步，不要改已有的（见 utils/schema.py）。
SCHEMA = [
    # 1：最初的结构（之前没有版本号的数据库也从这里开始，所以都带 IF NOT EXISTS）
    """
    CREATE TABLE IF NOT EXISTS favorites (key TEXT PRIMARY KEY, added REAL);
    CREATE TABLE IF NOT EXISTS views (key TEXT PRIMARY KEY, ts REAL);
    CREATE TABLE IF NOT EXISTS pins (artist TEXT PRIMARY KEY, added REAL);
    CREATE TABLE IF NOT EXISTS dims (path TEXT PRIMARY KEY, mtime REAL, size INTEGER, w INTEGER, h INTEGER);
    CREATE TABLE IF NOT EXISTS folders (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, created REAL);
    CREATE TABLE IF NOT EXISTS folder_artists (folder_id INTEGER, artist TEXT, added REAL, PRIMARY KEY (folder_id, artist));
    """,
    # 2：每位画师文件夹的扫描结果（文件清单）。下次启动时文件夹没变就直接用，不用把整个图库重新列一遍
    """
    CREATE TABLE IF NOT EXISTS scans (artist TEXT PRIMARY KEY, signature REAL, saved REAL, data BLOB);
    """,
]


class WebStore:
    def __init__(self, db_path: Optional[Path] = None, settings_path: Optional[Path] = None) -> None:
        self.db_path = Path(db_path or DATA_DIR / "webapp.db")
        self.settings_path = Path(settings_path or CONFIG_DIR / "web_ui.json")
        self._lock = threading.RLock()
        self._local = threading.local()

    # ---------- 连接 ----------
    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.db_path, timeout=10)
            migrate(conn, SCHEMA, "webapp.db")
            self._local.conn = conn
        return conn

    def close_thread_connection(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # ---------- 收藏 ----------
    def favorites(self) -> set:
        with self._lock:
            return {row[0] for row in self._conn().execute("SELECT key FROM favorites")}

    def set_favorite(self, keys: Iterable[str], on: bool) -> None:
        with self._lock:
            conn = self._conn()
            if on:
                conn.executemany("INSERT OR IGNORE INTO favorites VALUES (?, ?)", [(k, time.time()) for k in keys])
            else:
                conn.executemany("DELETE FROM favorites WHERE key = ?", [(k,) for k in keys])
            conn.commit()

    # ---------- 浏览记录 ----------
    def views(self) -> Dict[str, float]:
        with self._lock:
            return dict(self._conn().execute("SELECT key, ts FROM views"))

    def mark_viewed(self, key: str) -> None:
        with self._lock:
            conn = self._conn()
            conn.execute("INSERT OR REPLACE INTO views VALUES (?, ?)", (key, time.time()))
            # 只保留最近 500 条
            conn.execute("DELETE FROM views WHERE key NOT IN (SELECT key FROM views ORDER BY ts DESC LIMIT 500)")
            conn.commit()

    # ---------- 置顶画师 ----------
    def pins(self) -> set:
        with self._lock:
            return {row[0] for row in self._conn().execute("SELECT artist FROM pins")}

    def set_pin(self, artist: str, on: bool) -> None:
        with self._lock:
            conn = self._conn()
            if on:
                conn.execute("INSERT OR IGNORE INTO pins VALUES (?, ?)", (artist, time.time()))
            else:
                conn.execute("DELETE FROM pins WHERE artist = ?", (artist,))
            conn.commit()

    # ---------- 画师文件夹（给画师分类；一位画师可以放进多个文件夹） ----------
    def folders(self) -> List[dict]:
        with self._lock:
            conn = self._conn()
            members: Dict[int, List[str]] = {}
            for fid, artist in conn.execute("SELECT folder_id, artist FROM folder_artists ORDER BY added"):
                members.setdefault(fid, []).append(artist)
            return [{"id": fid, "name": name, "artists": members.get(fid, [])}
                    for fid, name in conn.execute("SELECT id, name FROM folders ORDER BY created")]

    def folder_create(self, name: str) -> int:
        with self._lock:
            conn = self._conn()
            cur = conn.execute("INSERT INTO folders (name, created) VALUES (?, ?)", (name, time.time()))
            conn.commit()
            return int(cur.lastrowid)

    def folder_rename(self, folder_id: int, name: str) -> None:
        with self._lock:
            conn = self._conn()
            conn.execute("UPDATE folders SET name = ? WHERE id = ?", (name, folder_id))
            conn.commit()

    def folder_delete(self, folder_id: int) -> None:
        with self._lock:
            conn = self._conn()
            conn.execute("DELETE FROM folder_artists WHERE folder_id = ?", (folder_id,))
            conn.execute("DELETE FROM folders WHERE id = ?", (folder_id,))
            conn.commit()

    def folder_set(self, folder_id: int, artists: Iterable[str], on: bool) -> None:
        with self._lock:
            conn = self._conn()
            if on:
                conn.executemany("INSERT OR IGNORE INTO folder_artists VALUES (?, ?, ?)",
                                 [(folder_id, a, time.time()) for a in artists])
            else:
                conn.executemany("DELETE FROM folder_artists WHERE folder_id = ? AND artist = ?",
                                 [(folder_id, a) for a in artists])
            conn.commit()

    # ---------- 图片尺寸缓存 ----------
    def get_dims(self, items: List[Tuple[str, float, int]]) -> Dict[str, Tuple[int, int]]:
        """items: (路径, mtime, 大小)；只返回与当前文件一致的缓存"""
        found: Dict[str, Tuple[int, int]] = {}
        if not items:
            return found
        wanted = {p: (m, s) for p, m, s in items}
        with self._lock:
            conn = self._conn()
            paths = list(wanted)
            for i in range(0, len(paths), 900):
                chunk = paths[i:i + 900]
                q = f"SELECT path, mtime, size, w, h FROM dims WHERE path IN ({','.join('?' * len(chunk))})"
                for path, mtime, size, w, h in conn.execute(q, chunk):
                    m, s = wanted[path]
                    if abs(mtime - m) < 1e-3 and size == s:
                        found[path] = (w, h)
        return found

    def set_dims(self, rows: List[Tuple[str, float, int, int, int]]) -> None:
        if not rows:
            return
        with self._lock:
            conn = self._conn()
            conn.executemany("INSERT OR REPLACE INTO dims VALUES (?, ?, ?, ?, ?)", rows)
            conn.commit()

    # ---------- 文件夹扫描结果 ----------
    def load_scans(self) -> Dict[str, Tuple[float, bytes]]:
        """全部存下来的扫描结果：{画师文件夹: (文件夹签名, 压缩过的文件清单)}"""
        with self._lock:
            return {a: (sig, data) for a, sig, data in self._conn().execute("SELECT artist, signature, data FROM scans")}

    def save_scan(self, artist: str, signature: float, data: bytes) -> None:
        with self._lock:
            conn = self._conn()
            conn.execute("INSERT OR REPLACE INTO scans VALUES (?, ?, ?, ?)", (artist, signature, time.time(), data))
            conn.commit()

    def prune_scans(self, keep: Iterable[str]) -> int:
        """删掉已经不存在的画师文件夹的记录，返回删了几条"""
        keep = set(keep)
        with self._lock:
            conn = self._conn()
            gone = [(a,) for (a,) in conn.execute("SELECT artist FROM scans") if a not in keep]
            conn.executemany("DELETE FROM scans WHERE artist = ?", gone)
            conn.commit()
        return len(gone)

    def clear_scans(self) -> None:
        with self._lock:
            conn = self._conn()
            conn.execute("DELETE FROM scans")
            conn.commit()

    # ---------- 界面设置 ----------
    def load_settings(self) -> Optional[dict]:
        try:
            if self.settings_path.exists():
                return json.loads(self.settings_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            logger.warning(f"读取界面设置失败: {e}")
        return None

    def save_settings(self, data: dict) -> None:
        try:
            self.settings_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.settings_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.settings_path)
        except OSError as e:
            logger.error(f"保存界面设置失败: {e}")

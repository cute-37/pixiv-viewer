#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Pixiv 标签倒排索引

Pixiv 元数据库里标签以 JSON 数组存在每个作品一行里，按标签搜索只能全表扫描并逐行解析 JSON，
库越大越慢，补全列表（所有标签）也一样。这里把 (标签 -> 作品 ID) 建成本地 SQLite 倒排索引：
- 首次使用或源库变化（大小/修改时间）时重建一次（在调用线程里，请从后台线程调用）
- 之后按标签查询是一次带索引的 SELECT，毫秒级
- 索引文件放在本程序的 data 目录，不会写入源库（源库可能在 NAS 上，且以只读方式访问）
"""
from __future__ import annotations

import hashlib
import sqlite3
import threading
from contextlib import closing
from pathlib import Path
from typing import Callable, Iterable, List, Set, Tuple

from .constants import DATA_DIR
from .logger import get_logger

logger = get_logger("PixivTagIndex")

SCHEMA_VERSION = "1"
BATCH = 5000

# 同一个索引文件的所有 PixivTagIndex 实例共用一把锁（每次查询都会新建实例，锁必须放在模块级）
_locks: dict = {}
_locks_guard = threading.Lock()


def _lock_for(index_file: Path) -> threading.Lock:
    key = str(Path(index_file).resolve())
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def index_file_for(source_db: Path, base_dir: Path | None = None) -> Path:
    """每个源库对应一个索引文件（按源库路径区分）"""
    digest = hashlib.md5(str(Path(source_db).resolve()).encode("utf-8")).hexdigest()
    return Path(base_dir or DATA_DIR / "pixiv_tag_index") / f"{digest}.db"


def stat_key(source_db: Path) -> str:
    """源库的"版本"：大小 + 修改时间（纳秒）。变化即视为索引过期"""
    st = Path(source_db).stat()
    return f"{SCHEMA_VERSION}:{st.st_size}:{st.st_mtime_ns}"


class PixivTagIndex:
    def __init__(self, index_file: Path) -> None:
        self.index_file = Path(index_file)
        self._lock = _lock_for(self.index_file)  # 避免多个线程同时重建

    # ---------------- 读取 ----------------

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.index_file)

    def is_fresh(self, key: str) -> bool:
        if not self.index_file.exists():
            return False
        try:
            with closing(self._connect()) as conn:
                row = conn.execute("SELECT value FROM meta WHERE key = 'source'").fetchone()
        except sqlite3.Error:
            return False
        return bool(row) and row[0] == key

    def lookup(self, tag: str) -> Set[str]:
        """某标签（不区分大小写）对应的全部作品 ID"""
        with closing(self._connect()) as conn:
            return {row[0] for row in conn.execute(
                "SELECT illust_id FROM tag_index WHERE tag_key = ?", (tag.casefold(),))}

    def all_tags(self) -> List[str]:
        """所有出现过的标签（原文，去重，排序）"""
        with closing(self._connect()) as conn:
            return [row[0] for row in conn.execute("SELECT DISTINCT tag FROM tag_index ORDER BY tag")]

    # ---------------- 构建 ----------------

    def ensure_built(self, key: str,
                     rows: Callable[[], Iterable[Tuple[str, Iterable[str]]]]) -> bool:
        """
        索引过期则重建。rows() 返回 (illust_id, 标签列表) 的可迭代对象。
        返回本次是否重建了。线程安全：并发调用时只有一个线程重建，其余等待后直接复用。
        """
        if self.is_fresh(key):
            return False
        with self._lock:
            if self.is_fresh(key):
                return False
            self._build(key, rows())
            return True

    def _build(self, key: str, rows: Iterable[Tuple[str, Iterable[str]]]) -> None:
        self.index_file.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            conn.execute("PRAGMA synchronous = OFF")
            conn.execute("DROP INDEX IF EXISTS idx_tag_key")
            conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
            conn.execute("CREATE TABLE IF NOT EXISTS tag_index "
                         "(tag_key TEXT NOT NULL, tag TEXT NOT NULL, illust_id TEXT NOT NULL)")
            conn.execute("DELETE FROM tag_index")
            conn.execute("DELETE FROM meta")

            batch: List[Tuple[str, str, str]] = []
            total = 0
            for illust_id, tags in rows:
                for tag in tags:
                    if isinstance(tag, str) and tag:
                        batch.append((tag.casefold(), tag, str(illust_id)))
                if len(batch) >= BATCH:
                    conn.executemany("INSERT INTO tag_index VALUES (?, ?, ?)", batch)
                    total += len(batch)
                    batch = []
            if batch:
                conn.executemany("INSERT INTO tag_index VALUES (?, ?, ?)", batch)
                total += len(batch)

            conn.execute("CREATE INDEX idx_tag_key ON tag_index(tag_key)")
            conn.execute("INSERT INTO meta VALUES ('source', ?)", (key,))
            conn.commit()
        logger.info(f"Pixiv 标签索引已重建: {total} 条 ({self.index_file.name})")

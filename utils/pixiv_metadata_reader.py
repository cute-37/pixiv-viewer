#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Pixiv 元数据读取器
从 Pixiv Downloader 的 SQLite 数据库读取作品与画师信息
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
from collections import OrderedDict
from contextlib import closing
from pathlib import Path
from typing import Optional, Dict, Any

from utils.logger import get_logger
from utils.pixiv_tag_index import PixivTagIndex, index_file_for, stat_key

logger = get_logger("PixivMetadataReader")


class PixivMetadataReader:
    """读取 Pixiv Downloader 数据库中的元数据"""

    def __init__(self, metadata_path: str = "", tag_index_dir: Optional[Path] = None) -> None:
        self._tag_index_dir = tag_index_dir  # None 表示使用 data/pixiv_tag_index/（测试可指定临时目录）
        self._metadata_path: str = ""
        self._db_path: Optional[Path] = None
        self._is_valid: bool = False
        # 添加缓存
        self._metadata_cache = OrderedDict()
        self._cache_max_size: int = 2000
        self._cache_lock = threading.RLock()  # 缓存会被 UI 线程与后台线程同时访问
        self._generation = 0
        self.set_metadata_path(metadata_path)

    @property
    def metadata_path(self) -> str:
        return self._metadata_path

    @property
    def db_path(self) -> Optional[Path]:
        return self._db_path

    def set_metadata_path(self, path: str) -> None:
        path = path or ""
        with self._cache_lock:
            if path == self._metadata_path:
                return
            self._generation += 1
            generation = self._generation
            self._metadata_path = path
            self._db_path = None
            self._is_valid = False
            self._metadata_cache.clear()
        db_path = self._resolve_db_path(path)
        valid = self._validate_db(db_path) if db_path else False
        with self._cache_lock:
            if generation == self._generation:
                self._db_path = db_path
                self._is_valid = valid

    @staticmethod
    def _connect(db_path: Path):
        # Read only: a disconnected/deleted NAS database must never be recreated.
        return closing(sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True))

    def _resolve_db_path(self, path: str) -> Optional[Path]:
        if not path:
            return None

        p = Path(path)
        if p.is_file() and p.suffix.lower() == ".db":
            return p

        if p.is_dir():
            candidates = [p / "pixiv_manager.db", p / "pixiv.db"]
            for c in candidates:
                if c.exists() and c.is_file():
                    return c

        return None

    def _validate_db(self, db_path: Optional[Path]) -> bool:
        if not db_path or not db_path.exists():
            return False
        try:
            with self._connect(db_path) as conn:
                cur = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='illust_metadata'"
                )
                return cur.fetchone() is not None
        except (sqlite3.Error, OSError) as e:
            logger.warning(f"Pixiv 元数据数据库不可用: {e}")
            return False

    def _parse_illust_id(self, image_path: str) -> Optional[str]:
        # 安全地获取文件名，使用 rfind 更高效
        sep_idx = image_path.rfind('\\')
        if sep_idx == -1:
            sep_idx = image_path.rfind('/')
        filename = image_path[sep_idx + 1:] if sep_idx != -1 else image_path
            
        match = re.search(r"(\d{5,})(?:_p\d+)?", filename)
        return match.group(1) if match else None

    def _parse_json_list(self, value: Any) -> list:
        if value is None:
            return []
        if isinstance(value, list):
            return value
        if isinstance(value, str) and value.strip():
            try:
                data = json.loads(value)
                return data if isinstance(data, list) else []
            except (json.JSONDecodeError, TypeError):
                return []
        return []

    def is_ai_from_meta(self, meta: Dict[str, Any]) -> bool:
        """根据元数据判断是否为 AI 生成"""
        if not meta:
            return False

        ai_type = meta.get("ai_type", None)
        if ai_type is not None:
            try:
                ai_val = int(ai_type)
                if ai_val == 2:
                    return True
                if ai_val == 1:
                    return self._has_ai_tag(meta)
            except Exception:
                pass

        return self._has_ai_tag(meta)

    def is_ai(self, image_path: str) -> bool:
        info = self.get_metadata(image_path)
        if not info or not isinstance(info, dict):
            return False
        meta = info.get("meta", {}) or {}
        return self.is_ai_from_meta(meta)

    def get_rating_level(self, meta: Dict[str, Any]) -> str:
        """返回分级: all / r18 / r18g / unknown"""
        if not meta:
            return "unknown"

        x_restrict = meta.get("x_restrict", None)
        if x_restrict is not None:
            try:
                xr = int(x_restrict)
                if xr == 2:
                    return "r18g"
                if xr == 1:
                    return "r18"
                if xr == 0:
                    return "all"
            except Exception:
                pass

        is_r18 = meta.get("is_r18", None)
        if is_r18 is not None:
            try:
                return "r18" if int(is_r18) == 1 else "all"
            except Exception:
                pass

        return "unknown"

    def get_rating_level_by_path(self, image_path: str) -> str:
        info = self.get_metadata(image_path)
        if not info or not isinstance(info, dict):
            return "unknown"
        meta = info.get("meta", {}) or {}
        return self.get_rating_level(meta)

    # 以 "ai" 开头且后面不是英文字母的标签（AI / AI生成 / AIイラスト / ai-generated），排除 airplane、aiko 等
    _AI_TAG_RE = re.compile(r"^ai(?![a-z])")

    def _has_ai_tag(self, meta: Dict[str, Any]) -> bool:
        tags = self._parse_json_list(meta.get("tags")) + self._parse_json_list(meta.get("tags_translated"))
        for tag in tags:
            t = str(tag).strip().lower()
            if self._AI_TAG_RE.match(t) or t == "aigc" or "人工智能" in t:
                return True
        return False

    def get_metadata(self, image_path: str) -> Optional[Dict[str, Any]]:
        """Read one item through the shared batch/cache implementation."""
        return self.get_metadata_batch([image_path]).get(image_path)

    def get_metadata_batch(self, image_paths) -> Dict[str, Optional[Dict[str, Any]]]:
        """Read works and artists in bounded queries, using one closed connection."""
        path_ids = {path: self._parse_illust_id(path) for path in image_paths}
        result_by_id = {}
        with self._cache_lock:
            db_path, generation = self._db_path, self._generation
            if not self._is_valid or not db_path:
                return {path: None for path in path_ids}
            missing = []
            for illust_id in dict.fromkeys(path_ids.values()):
                if not illust_id:
                    continue
                if illust_id in self._metadata_cache:
                    result_by_id[illust_id] = self._metadata_cache[illust_id]
                    self._metadata_cache.move_to_end(illust_id)
                else:
                    missing.append(illust_id)
        if missing:
            try:
                with self._connect(db_path) as conn:
                    conn.row_factory = sqlite3.Row
                    has_artists = conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='artists'"
                    ).fetchone() is not None
                    for offset in range(0, len(missing), 900):
                        chunk = missing[offset:offset + 900]
                        placeholders = ','.join('?' for _ in chunk)
                        rows = conn.execute(
                            f"SELECT * FROM illust_metadata WHERE illust_id IN ({placeholders})", chunk
                        ).fetchall()
                        artists = {}
                        author_ids = list({row['author_id'] for row in rows
                                           if 'author_id' in row.keys() and row['author_id'] is not None})
                        if has_artists and author_ids:
                            params = ','.join('?' for _ in author_ids)
                            artists = {str(row['author_id']): dict(row) for row in conn.execute(
                                f"SELECT * FROM artists WHERE author_id IN ({params})", author_ids
                            )}
                        result_by_id.update(dict.fromkeys(chunk))
                        for row in rows:
                            meta = dict(row)
                            illust_id = str(meta['illust_id'])
                            meta['illust_id'] = illust_id
                            for field in ('tags', 'tags_translated', 'tools'):
                                meta[field] = self._parse_json_list(meta.get(field))
                            result_by_id[illust_id] = {
                                'source': str(db_path), 'meta': meta,
                                'artist': artists.get(str(meta.get('author_id'))),
                            }
            except (sqlite3.Error, OSError) as e:
                logger.error(f"读取 Pixiv 元数据失败: {e}")
                # Do not cache transient NAS/SQLite failures as permanently missing.
                return {path: result_by_id.get(illust_id) for path, illust_id in path_ids.items()}
        with self._cache_lock:
            if generation != self._generation:
                return {path: None for path in path_ids}
            for illust_id, value in result_by_id.items():
                self._metadata_cache[illust_id] = value
                self._metadata_cache.move_to_end(illust_id)
            while len(self._metadata_cache) > self._cache_max_size:
                self._metadata_cache.popitem(last=False)
        return {path: result_by_id.get(illust_id) for path, illust_id in path_ids.items()}

    def get_artist_rating(self, author_id: str) -> str:
        """
        获取画师的最高 R18 评级 (all / r18 / r18g)
        通过查询画师的所有作品的 x_restrict 字段
        """
        with self._cache_lock:
            db_path = self._db_path if self._is_valid else None
        if db_path is None:
            return "all"
            
        try:
            with self._connect(db_path) as conn:
                # 0: all, 1: r18, 2: r18g
                # 我们取最大值
                cur = conn.execute(
                    "SELECT MAX(x_restrict) FROM illust_metadata WHERE author_id = ?",
                    (author_id,)
                )
                row = cur.fetchone()
                if not row or row[0] is None:
                    return "all"
                    
                max_restrict = int(row[0])
                if max_restrict == 2:
                    return "r18g"
                elif max_restrict == 1:
                    return "r18"
                else:
                    return "all"
                    
        except (sqlite3.Error, OSError, ValueError, TypeError) as e:
            logger.error(f"获取画师评级失败 {author_id}: {e}")
            return "all"

    # ---------------- 标签倒排索引 ----------------
    # 标签搜索 / 补全需要"所有标签"和"某标签的作品"，直接查源库只能全表扫描并逐行解析 JSON。
    # 这里优先使用本地倒排索引（见 utils/pixiv_tag_index.py），首次使用或源库变化时重建一次；
    # 索引不可用时退回全表扫描。这些方法会阻塞（首次需要建索引），请在后台线程调用。

    def _iter_tag_rows(self, db_path: Path):
        with self._connect(db_path) as conn:
            for illust_id, tags in conn.execute("SELECT illust_id, tags FROM illust_metadata"):
                yield str(illust_id), self._parse_json_list(tags)

    def _fresh_tag_index(self, db_path: Path) -> Optional[PixivTagIndex]:
        """返回已是最新的索引（必要时重建）；索引不可用时返回 None"""
        try:
            index = PixivTagIndex(index_file_for(db_path, self._tag_index_dir))
            index.ensure_built(stat_key(db_path), lambda: self._iter_tag_rows(db_path))
            return index
        except (sqlite3.Error, OSError) as e:
            logger.warning(f"标签索引不可用，退回全表扫描: {e}")
            return None

    def get_all_tags(self) -> list:
        with self._cache_lock:
            db_path = self._db_path if self._is_valid else None
        if db_path is None:
            return []
        index = self._fresh_tag_index(db_path)
        if index is not None:
            try:
                return index.all_tags()
            except sqlite3.Error as e:
                logger.warning(f"读取标签索引失败，退回全表扫描: {e}")
        with self._connect(db_path) as conn:
            tags = set()
            for row in conn.execute("SELECT tags FROM illust_metadata WHERE tags IS NOT NULL"):
                tags.update(tag for tag in self._parse_json_list(row[0]) if isinstance(tag, str))
            return sorted(tags)

    def find_illust_ids_by_tag(self, tag: str) -> set:
        with self._cache_lock:
            db_path, generation = self._db_path, self._generation
            if not self._is_valid or db_path is None:
                return set()
        index = self._fresh_tag_index(db_path)
        if index is not None:
            try:
                result = index.lookup(tag)
                with self._cache_lock:
                    return result if generation == self._generation else set()
            except sqlite3.Error as e:
                logger.warning(f"查询标签索引失败，退回全表扫描: {e}")
        # JSON may contain escaped Unicode, so SQL LIKE on its raw text is unsafe.
        tag = tag.casefold()
        with self._connect(db_path) as conn:
            result = {str(row[0]) for row in conn.execute("SELECT illust_id, tags FROM illust_metadata")
                      if any(isinstance(value, str) and value.casefold() == tag
                             for value in self._parse_json_list(row[1]))}
        with self._cache_lock:
            return result if generation == self._generation else set()


_shared_reader = None
_shared_reader_lock = threading.Lock()


def get_pixiv_reader() -> PixivMetadataReader:
    """One application reader; explicit instances remain available for isolated use."""
    global _shared_reader
    with _shared_reader_lock:
        if _shared_reader is None:
            _shared_reader = PixivMetadataReader()
        return _shared_reader

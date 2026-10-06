#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
缩略图磁盘缓存
- 缓存键包含 路径 + 修改时间 + 文件大小 + 渲染尺寸：图片被替换或缩略图尺寸变化后不会误用旧缓存
- 命中时刷新文件 mtime，作为"最近使用时间"
- prune() 按最近使用时间淘汰：超过保留天数的删除；总容量超限时从最旧的开始删除
"""
from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path
from typing import Dict, Optional

from .constants import CACHE_DIR
from .logger import get_logger

logger = get_logger("ThumbnailCache")

CACHE_SUFFIX = ".jpg"
DEFAULT_MAX_BYTES = 500 * 1024 * 1024  # 500MB
DEFAULT_MAX_AGE_DAYS = 90


# 缓存只存固定几档渲染尺寸：用 Ctrl+滚轮调整缩略图大小时（每次 20px）不会每个尺寸都重新生成并各占一份磁盘，
# 显示时再从相近的一档缩放即可
RENDER_TIERS = (160, 256, 384, 512, 768)


def render_tier(size: int) -> int:
    """把需要的渲染尺寸向上取整到最近的一档（超过最大一档则取最大档）"""
    for tier in RENDER_TIERS:
        if size <= tier:
            return tier
    return RENDER_TIERS[-1]


def cache_path(image_path: str, mtime: float, size: int, render_size: int,
               cache_dir: Optional[Path] = None) -> Path:
    """根据图片身份与渲染尺寸计算缓存文件路径"""
    key = f"{image_path}|{int(mtime * 1000)}|{size}|{render_size}"
    name = hashlib.md5(key.encode("utf-8")).hexdigest() + CACHE_SUFFIX
    return Path(cache_dir or CACHE_DIR) / name


def touch(path: Path) -> None:
    """刷新最近使用时间（失败忽略）"""
    try:
        os.utime(path, None)
    except OSError:
        pass


def prune(max_bytes: int = DEFAULT_MAX_BYTES,
          max_age_days: int = DEFAULT_MAX_AGE_DAYS,
          cache_dir: Optional[Path] = None) -> Dict[str, int]:
    """
    清理缓存目录，返回 {"removed": 删除文件数, "freed": 释放字节数, "kept_bytes": 剩余字节数}。
    可在后台线程调用。
    """
    directory = Path(cache_dir or CACHE_DIR)
    if not directory.is_dir():
        return {"removed": 0, "freed": 0, "kept_bytes": 0}
    removed = freed = 0
    files = []
    try:
        with os.scandir(directory) as it:
            for entry in it:
                if not entry.is_file():
                    continue
                try:
                    st = entry.stat()
                except OSError:
                    continue
                files.append((st.st_mtime, st.st_size, entry.path))
    except OSError as e:
        logger.warning(f"无法扫描缓存目录: {e}")
        return {"removed": 0, "freed": 0, "kept_bytes": 0}

    def _remove(path: str, size: int) -> bool:
        nonlocal removed, freed
        try:
            os.remove(path)
        except OSError:
            return False
        removed += 1
        freed += size
        return True

    cutoff = time.time() - max_age_days * 86400
    survivors = []
    for mtime, size, path in files:
        if mtime < cutoff and _remove(path, size):
            continue
        survivors.append((mtime, size, path))

    total = sum(size for _, size, _ in survivors)
    if total > max_bytes:
        survivors.sort()  # 最久未使用的在前
        kept = []
        for mtime, size, path in survivors:
            if total > max_bytes and _remove(path, size):
                total -= size
            else:
                kept.append((mtime, size, path))
        survivors = kept

    if removed:
        logger.info(f"缩略图缓存清理: 删除 {removed} 个文件，释放 {freed / 1024 / 1024:.1f}MB")
    return {"removed": removed, "freed": freed, "kept_bytes": total}

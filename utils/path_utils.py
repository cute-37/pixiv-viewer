#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
路径工具

主线程里不应对"可能是网络路径"的位置调用 os.path.exists / isdir / stat：
NAS 休眠或断网时，单次调用可能阻塞数十秒。用 is_network_path() 先判断，
网络路径交给后台线程（或惰性扫描时自带的重试逻辑）去探测。
"""
from __future__ import annotations

import os
import sys
from typing import Dict

_DRIVE_REMOTE = 4  # GetDriveTypeW 返回值：网络驱动器
_drive_type_cache: Dict[str, bool] = {}


def _is_remote_drive(letter: str) -> bool:
    """盘符是否映射到网络（只读本机的驱动器类型表，不访问网络，不会阻塞）"""
    key = letter.upper()
    cached = _drive_type_cache.get(key)
    if cached is not None:
        return cached
    remote = False
    if sys.platform == "win32":
        try:
            import ctypes
            remote = ctypes.windll.kernel32.GetDriveTypeW(f"{key}:\\") == _DRIVE_REMOTE
        except Exception:
            remote = False
    _drive_type_cache[key] = remote
    return remote


def is_network_path(path: str) -> bool:
    """UNC 路径（\\\\host\\share 或 //host/share）或映射到网络的盘符路径"""
    if not path:
        return False
    if path.startswith("\\\\") or path.startswith("//"):
        return True
    if len(path) >= 2 and path[1] == ":" and path[0].isalpha():
        return _is_remote_drive(path[0])
    return False


def forget_drive_cache() -> None:
    """盘符映射可能在运行期变化（挂载/卸载后调用）"""
    _drive_type_cache.clear()


def safe_exists(path: str) -> bool:
    """
    仅对本地路径做同步存在性检查；网络路径一律返回 True（交给后续的异步扫描和重试判断）。
    用于必须在主线程里立刻决定"要不要添加这个路径"的场景。
    """
    if is_network_path(path):
        return True
    try:
        return os.path.exists(path)
    except OSError:
        return False

#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
数据库结构的版本管理

每个数据库文件在 SQLite 的 user_version 里记着自己的结构版本。要改表结构时，不要去改已有的步骤，
而是在对应的步骤列表末尾追加一步（一段 SQL，或一个接收连接的函数）；程序打开数据库时会把还没执行过的步骤
按顺序补上。这样旧版本留下的数据库能一步步升到最新，新建的数据库也走同一条路。
"""
from __future__ import annotations

import sqlite3
from typing import Callable, Sequence, Union

from .logger import get_logger

logger = get_logger("Schema")

Step = Union[str, Callable[[sqlite3.Connection], None]]


def version_of(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def migrate(conn: sqlite3.Connection, steps: Sequence[Step], name: str = "数据库") -> int:
    """把数据库升到 len(steps) 这个版本，返回升级后的版本号。

    steps[i] 负责把结构从版本 i 升到 i + 1。每一步连同版本号在同一个事务里提交：中途失败就停在上一个版本，
    下次启动接着来。数据库比程序新（用更新的版本打开过）时不动它，只记一条警告。
    """
    current = version_of(conn)
    if current > len(steps):
        logger.warning(f"{name} 的结构版本是 {current}，比这个程序认识的 {len(steps)} 新；按现有结构继续使用")
        return current
    for index in range(current, len(steps)):
        step = steps[index]
        try:
            if conn.in_transaction:
                conn.commit()
            conn.execute("BEGIN IMMEDIATE")          # 先拿到写锁，再确认这一步确实还没人做过
            if version_of(conn) > index:             # 另一个线程 / 进程刚刚升过了
                conn.rollback()
                continue
            if callable(step):
                step(conn)
            else:
                for statement in filter(None, (s.strip() for s in step.split(";"))):
                    conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {index + 1}")
            conn.commit()
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            logger.error(f"{name} 从版本 {index} 升到 {index + 1} 失败", exc_info=True)
            raise
        if index:
            logger.info(f"{name} 的结构已升级到版本 {index + 1}")
    return len(steps)

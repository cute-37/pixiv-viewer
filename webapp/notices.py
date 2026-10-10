#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
重要更新提醒：让已经装在别人电脑上的旧版本，能知道“这个版本有严重问题，该更新了”。

为什么需要：Pixiv 的接口哪天变了，旧版本的下载会全部失败，而用户只看到一堆失败，不知道是该更新了。
平时软件不会自己联网检查新版本（要手动点“检查更新”），所以需要一条单独的、很轻的通道。

做法：仓库里放一个很小的通知文件（notices.json）。软件启动时、之后每天一次，把它读下来，
挑出“针对当前这个版本”的通知交给界面。只读这一个文件：不下载别的，不上传任何信息；连不上就用上次读到的。
可以在“设置 → 常规”里关掉。

通知文件的格式（开发者手工维护，见 docs/DEVELOPMENT.md）：
    {"notices": [{
        "id": "2026-11-api",            # 唯一的名字
        "below": "1.2.0",               # 低于这个版本的才提醒（必填）
        "from": "1.0.0",                # 可选：不低于这个版本的才提醒
        "level": "important",           # important：打开软件时弹出提示；download：只在下载相关的地方提示
        "title": {"zh": "…", "en": "…", "ja": "…"},      # ja 可以不写，日语用户会看到英文
        "text":  {"zh": "…", "en": "…", "ja": "…"}
    }]}
通知里的文字只当纯文本显示；不接受链接、不接受任何会让软件“去做什么”的内容——它只能让软件提示用户去更新。
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Callable, List, Optional

from utils.logger import get_logger
from webapp.updater import parse_version

logger = get_logger("Notices")

URL_ENV = "PIXIV_VIEWER_NOTICES_URL"       # 测试用：改从别的地址读
CHECK_EVERY = 24 * 3600                    # 多久读一次
LEVELS = ("important", "download")
MAX_TEXT = 600


def notices_url(repo: str) -> str:
    return os.environ.get(URL_ENV) or f"https://raw.githubusercontent.com/{repo}/main/notices.json"


def _texts(value) -> dict:
    """{"zh": …, "en": …, "ja": …}；只写了一个字符串时各种语言都用它"""
    if isinstance(value, str):
        value = {"zh": value, "en": value}
    if not isinstance(value, dict):
        return {}
    return {k: str(v)[:MAX_TEXT] for k, v in value.items() if k in ("zh", "en", "ja") and isinstance(v, str) and v.strip()}


def applicable(data, version: str) -> List[dict]:
    """从通知文件的内容里挑出针对这个版本的。格式不对的条目直接跳过（不能因为一条写错了就让软件出错）。"""
    items = data.get("notices") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    mine, out = parse_version(version), []
    for it in items:
        try:
            if not isinstance(it, dict) or it.get("level") not in LEVELS or not it.get("id") or not it.get("below"):
                continue
            if not mine < parse_version(str(it["below"])):
                continue
            if it.get("from") and mine < parse_version(str(it["from"])):
                continue
            title, text = _texts(it.get("title")), _texts(it.get("text"))
            if not title:
                continue
            out.append({"id": str(it["id"])[:80], "level": it["level"], "below": str(it["below"])[:20], "title": title, "text": text})
        except Exception:
            continue
    return out[:5]


class Notices:
    def __init__(self, version: str, url: str, cache: Path, fetch: Callable[[str], str],
                 enabled: Callable[[], bool] = lambda: True) -> None:
        self.version, self.url, self.cache = version, url, Path(cache)
        self._fetch, self._enabled = fetch, enabled
        self._lock = threading.Lock()
        self._busy = False

    def _load(self) -> dict:
        try:
            return json.loads(self.cache.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def current(self) -> List[dict]:
        """现在该提醒的（来自上次读到的内容）。关掉了就一条都没有。"""
        if not self._enabled():
            return []
        return applicable(self._load().get("data"), self.version)

    def due(self, now: Optional[float] = None) -> bool:
        return self._enabled() and (now or time.time()) - float(self._load().get("checked") or 0) >= CHECK_EVERY

    def refresh(self, force: bool = False) -> bool:
        """到时间了（或 force）就去读一次。读到了返回 True。失败只记一笔，不抛异常。"""
        if not self._enabled() or not (force or self.due()):
            return False
        with self._lock:
            if self._busy:
                return False
            self._busy = True
        try:
            data = json.loads(self._fetch(self.url))
            if not isinstance(data, dict):
                raise ValueError("内容不是预期的格式")
            self.cache.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache.with_suffix(".tmp")
            tmp.write_text(json.dumps({"checked": time.time(), "data": data}, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.cache)
            return True
        except Exception as e:
            logger.info(f"没有读到更新提醒（不影响使用）: {type(e).__name__}: {e}")
            return False
        finally:
            self._busy = False


class NoticeApiMixin:
    """给界面用的接口。使用它的类要有 _notices（Notices 对象）。"""

    def notices(self, force=False):
        """现在该提醒的通知。到时间了会先去读一次（最多等十来秒；界面是在后台调用的，不会卡住）。"""
        box = getattr(self, "_notices", None)
        if box is None:
            return {"ok": True, "items": []}
        box.refresh(bool(force))
        return {"ok": True, "items": box.current()}

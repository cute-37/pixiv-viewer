#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
界面语言（Python 这一侧）。

绝大部分界面文字在网页那一侧翻译（见 webui/js/i18n.js）。只有少数文字是系统原生显示的、网页管不到：
托盘菜单、系统通知、登录窗口的标题。这些地方用 pick("中文", "English", "日本語") 按当前语言取一个。
日志始终是中文，不翻译。
"""
from __future__ import annotations

_lang = "zh"


def set_lang(code) -> None:
    global _lang
    code = str(code or "").lower()
    _lang = "en" if code.startswith("en") else "ja" if code.startswith("ja") else "zh"


def current() -> str:
    return _lang


def pick(zh: str, en: str, ja: str = "") -> str:
    """按当前语言取一个；没给日语时，日语用户看到英文"""
    if _lang == "zh":
        return zh
    return ja if _lang == "ja" and ja else en

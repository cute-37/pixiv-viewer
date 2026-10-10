#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
导出诊断信息：把最近的日志和一份“当前是什么情况”的摘要打成一个压缩包，遇到问题时发给开发者。

放进去的：
- 查看器、下载模块、软件更新最近的日志；
- summary.txt：版本、系统、图库有多少画师 / 作品、保存方式、下载设置（不含任何凭证）。

不放进去的：登录凭证、各种密码、数据库、图片。日志里出现的凭证（万一有）也会被替换成 ***。
日志里会有账号名、保存位置的路径、画师名——界面上导出前会说明这一点。
"""
from __future__ import annotations

import json
import platform
import sys
import time
import zipfile
from pathlib import Path
from typing import Iterable, List

from utils.constants import APP_VERSION
from utils.logger import get_logger

logger = get_logger("Diagnostics")

SECRET_KEYS = ("NAS_PASS", "WEBDAV_PASS", "FTP_PASS", "SFTP_PASS", "S3_SECRET_KEY", "S3_ACCESS_KEY", "REFRESH_TOKEN", "TOKENS")
URL_KEYS = ("WEBDAV_URL", "FTP_URL", "SFTP_URL", "PROXY_URL")      # 地址里可能带着 用户名:密码@
MAX_LOG_BYTES = 3 * 1024 * 1024      # 每个日志最多放这么多（取末尾）
VIEWER_LOGS, DL_LOGS = 4, 8          # 各放最近几个


def _secrets(settings: dict) -> List[str]:
    """设置里所有不能外传的值（用来把日志里万一出现的也抹掉）"""
    found: List[str] = []

    def walk(v):
        if isinstance(v, str) and len(v) >= 6:
            found.append(v)
        elif isinstance(v, dict):
            for k, x in v.items():
                if k not in ("is_valid", "last_tested", "username", "user_id", "remark", "r18", "r18g", "is_premium"):
                    walk(x)
        elif isinstance(v, (list, tuple)):
            for x in v:
                walk(x)

    for k in SECRET_KEYS:
        walk(settings.get(k))
    return sorted(set(found), key=len, reverse=True)


def _strip_userinfo(url: str) -> str:
    if "://" in url and "@" in url.split("://", 1)[1].split("/", 1)[0]:
        scheme, rest = url.split("://", 1)
        return f"{scheme}://***@{rest.split('@', 1)[1]}"
    return url


def public_settings(settings: dict) -> dict:
    """下载设置里可以给别人看的部分：去掉凭证，账号只留名字和状态"""
    out = {k: v for k, v in settings.items() if k not in SECRET_KEYS and k != "PROXIES"}
    for k in URL_KEYS:
        if isinstance(out.get(k), str):
            out[k] = _strip_userinfo(out[k])
    tokens = settings.get("TOKENS")
    if isinstance(tokens, dict):
        out["ACCOUNTS"] = {name: {k: v for k, v in info.items() if k != "token"} if isinstance(info, dict) else {}
                           for name, info in tokens.items()}
    return out


def _tail(path: Path, secrets: Iterable[str]) -> bytes:
    data = path.read_bytes()
    if len(data) > MAX_LOG_BYTES:
        data = b"...(only the last part is included)...\n" + data[-MAX_LOG_BYTES:]
    text = data.decode("utf-8", errors="replace")
    for s in secrets:
        text = text.replace(s, "***")
    return text.encode("utf-8")


def _recent(folder: Path, pattern: str, count: int) -> List[Path]:
    try:
        return sorted(folder.glob(pattern), key=lambda p: p.stat().st_mtime)[-count:]
    except OSError:
        return []


def build(target: Path, log_dir: Path, dl_home: Path, facts: dict) -> dict:
    """写出压缩包。facts：调用方给的摘要（图库数量等）。返回 {path, files, bytes}。"""
    settings: dict = {}
    try:
        settings = json.loads((dl_home / "settings.json").read_text(encoding="utf-8")).get("current") or {}
    except (OSError, ValueError, AttributeError):
        pass
    secrets = _secrets(settings)
    summary = {
        "app": APP_VERSION, "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "system": f"{platform.system()} {platform.release()} ({platform.version()})", "machine": platform.machine(),
        "python": sys.version.split()[0], "frozen": bool(getattr(sys, "frozen", False)),
        **facts, "download_settings": public_settings(settings),
    }
    text = json.dumps(summary, ensure_ascii=False, indent=2, default=str)
    for s in secrets:
        text = text.replace(s, "***")
    files = [(log_dir / "update.log", "viewer/update.log"), (dl_home / "logs" / "viewer_worker.log", "downloader/viewer_worker.log")]
    files += [(p, f"viewer/{p.name}") for p in _recent(log_dir, "pixiv_viewer_*.log", VIEWER_LOGS)]
    files += [(p, f"downloader/{p.name}") for p in _recent(dl_home / "logs", "2*.log", DL_LOGS)]
    count = 0
    target = Path(target)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("summary.txt", text)
        for path, name in files:
            try:
                if path.is_file() and path.stat().st_size:
                    z.writestr(name, _tail(path, secrets))
                    count += 1
            except OSError as e:
                logger.debug(f"读不到 {path}: {e}")
    return {"path": str(target), "files": count, "bytes": target.stat().st_size}

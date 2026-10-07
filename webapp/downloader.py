# -*- coding: utf-8 -*-
"""
下载功能（项目内置的 pixiv_dl 模块）在查看器这边的入口

- resolve_home    ：下载数据放在哪个目录
- DownloaderBridge：按需启动工作进程（webapp.dl_worker），通过管道调用下载模块的接口
- DownloaderData  ：只读地直接读下载数据（画师头像、画师名），这类小查询不必经过工作进程
"""
from __future__ import annotations

import itertools
import json
import os
import sqlite3
import subprocess
import sys
import threading
from concurrent.futures import Future
from pathlib import Path
from typing import Dict, Optional

from utils.logger import get_logger

logger = get_logger("Downloader")

CALL_TIMEOUT = 120.0     # 单次调用最长等待；测试存储连接、验证账号这类操作要访问网络
START_TIMEOUT = 40.0


def resolve_home(configured: str = "", metadata_db: str = "", default: Optional[Path] = None) -> Path:
    """下载功能的数据目录（settings.json、db/、avatars/ 都在里面）。

    1. 设置里指定了就用指定的；
    2. 否则，如果“Pixiv 元数据库”所在的项目目录里已经有下载数据（以前单独使用下载器留下的），沿用它；
    3. 都没有就用查看器自己的数据目录。
    """
    if configured:
        return Path(configured)
    if metadata_db:
        old = Path(metadata_db).resolve().parent.parent        # 数据库在 <目录>/db/pixiv_manager.db
        try:
            if (old / "settings.json").is_file() and (old / "db").is_dir():
                return old
        except OSError:
            pass
    return Path(default) if default else Path.cwd() / "data" / "pixiv"


class DownloaderError(Exception):
    def __init__(self, message: str, status: int = 500) -> None:
        super().__init__(message)
        self.status = status


class DownloaderBridge:
    """下载器工作进程的客户端。第一次调用时才启动进程；进程意外退出后，下一次调用会重新启动。"""

    def __init__(self, root: Path, code_root: Optional[Path] = None) -> None:
        self.root = Path(root)             # 数据目录
        self.code_root = code_root         # 只在测试里用：从这里加载 pixiv_dl
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()          # 启动 / 写入
        self._pending: Dict[int, Future] = {}
        self._ids = itertools.count(1)
        self._ready = threading.Event()
        self._fatal = ""
        self.version = ""

    # ---------- 进程 ----------
    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _python(self) -> str:
        # 用带控制台的解释器：pythonw 没有标准输入输出。窗口用启动参数隐藏。
        exe = Path(sys.executable)
        if exe.name.lower() == "pythonw.exe" and (exe.parent / "python.exe").is_file():
            return str(exe.parent / "python.exe")
        return str(exe)

    def _start(self) -> None:
        if self.running:
            return
        self._ready.clear()
        self._fatal = ""
        log_dir = self.root / "logs"
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            err = open(log_dir / "viewer_worker.log", "ab")
        except OSError:
            err = subprocess.DEVNULL
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1", PIXIV_DL_HOME=str(self.root))
        if getattr(sys, "frozen", False):
            # 打包后的程序里没有单独的 Python：让程序自己以工作进程的身份再启动一次（见 web_main.py）
            args = [sys.executable, "--dl-worker", str(self.root)]
        else:
            args = [self._python(), "-m", "webapp.dl_worker", str(self.root)]
        if self.code_root:
            args.append(str(self.code_root))
        self._proc = subprocess.Popen(
            args,
            cwd=str(Path(__file__).resolve().parent.parent), env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err, creationflags=flags,
        )
        threading.Thread(target=self._read_loop, args=(self._proc,), name="dl-bridge", daemon=True).start()
        logger.info(f"下载器工作进程已启动 (pid {self._proc.pid})，目录 {self.root}")

    def _read_loop(self, proc: subprocess.Popen) -> None:
        try:
            for line in proc.stdout:
                try:
                    msg = json.loads(line.decode("utf-8"))
                except ValueError:
                    continue
                if msg.get("event") == "ready":
                    self.version = msg.get("version", "")
                    self._ready.set()
                elif msg.get("event") == "fatal":
                    self._fatal = msg.get("error", "下载器加载失败")
                    logger.error(f"下载器加载失败: {self._fatal}\n{msg.get('trace', '')}")
                    self._ready.set()
                else:
                    fut = self._pending.pop(msg.get("id"), None)
                    if fut is not None:
                        fut.set_result(msg)
        finally:
            # 进程结束：让还在等的调用立即返回，而不是等到超时
            self._ready.set()
            for fut in list(self._pending.values()):
                if not fut.done():
                    fut.set_result({"ok": False, "status": 503, "error": "下载器进程已退出"})
            self._pending.clear()

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.stdin.write(b'{"op":"quit"}\n')
            proc.stdin.flush()
            proc.stdin.close()
            proc.wait(timeout=25)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    # ---------- 调用 ----------
    def call(self, method: str, path: str, body: Optional[dict] = None, query: Optional[dict] = None,
             timeout: float = CALL_TIMEOUT) -> dict:
        """调用下载器接口，返回它的结果；出错抛 DownloaderError。"""
        with self._lock:
            self._start()
        if not self._ready.wait(START_TIMEOUT):
            raise DownloaderError("下载器启动超时", 503)
        if self._fatal:
            raise DownloaderError(f"下载器加载失败：{self._fatal}", 503)
        rid = next(self._ids)
        fut: Future = Future()
        self._pending[rid] = fut
        data = json.dumps({"id": rid, "method": method, "path": path, "body": body or {}, "query": query or {}},
                          ensure_ascii=False).encode("utf-8") + b"\n"
        try:
            with self._lock:
                self._proc.stdin.write(data)
                self._proc.stdin.flush()
        except (OSError, AttributeError, ValueError):
            self._pending.pop(rid, None)
            raise DownloaderError("下载器进程已退出，请重试", 503) from None
        try:
            msg = fut.result(timeout=timeout)
        except Exception:
            self._pending.pop(rid, None)
            raise DownloaderError("下载器没有在规定时间内响应", 504) from None
        if not msg.get("ok"):
            raise DownloaderError(msg.get("error") or "下载器返回了错误", int(msg.get("status") or 500))
        return msg.get("data")


class DownloaderData:
    """只读访问下载器的数据：画师头像文件、画师名字"""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.avatars_dir = self.root / "avatars"
        self._avatars: Optional[Dict[int, str]] = None
        self._target: Optional[tuple] = None
        self._lock = threading.Lock()

    def avatars(self) -> Dict[int, str]:
        """画师 ID -> 头像文件名（列一次目录后缓存；下载了新头像后调用 refresh）"""
        with self._lock:
            if self._avatars is None:
                found: Dict[int, str] = {}
                try:
                    for e in os.scandir(self.avatars_dir):
                        stem, ext = os.path.splitext(e.name)
                        if stem.isdigit() and ext.lower() in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
                            found[int(stem)] = e.name
                except OSError:
                    pass
                self._avatars = found
            return self._avatars

    def refresh(self) -> None:
        with self._lock:
            self._avatars = None

    def avatar_path(self, author_id: int) -> Optional[Path]:
        name = self.avatars().get(int(author_id))
        return self.avatars_dir / name if name else None

    def save_target(self) -> dict:
        """下载的文件存到哪里，换算成查看器能直接打开的路径：{mode, path, readable}

        直接读数据目录里的 settings.json（没有这个文件时就是默认值：数据目录下的 downloads），
        不需要启动工作进程。本地目录和 SMB 共享查看器能直接读；其余方式 path 为空。
        """
        settings = self.root / "settings.json"
        try:
            stamp = settings.stat().st_mtime_ns
        except OSError:
            stamp = 0
        if self._target and self._target[0] == stamp:      # 这个函数调用很频繁，文件没变就用上次的结果
            return dict(self._target[1])
        cfg: dict = {}
        try:
            cfg = json.loads(settings.read_text(encoding="utf-8")).get("current") or {}
        except (OSError, ValueError, AttributeError):
            pass
        mode = cfg.get("STORAGE_MODE") or "local"
        path = ""
        if mode == "local":
            path = str(cfg.get("LOCAL_SAVE_PATH") or (self.root / "downloads"))
            if not os.path.isabs(path):
                path = str(self.root / path)
        elif mode == "smb" and cfg.get("NAS_IP") and cfg.get("NAS_SHARE"):
            sub = str(cfg.get("NAS_BASE_PATH") or "").replace("/", "\\").strip("\\")
            path = f"\\\\{cfg['NAS_IP']}\\{cfg['NAS_SHARE']}" + (f"\\{sub}" if sub else "")
        result = {"mode": mode, "path": os.path.normpath(path) if path else "", "readable": bool(path)}
        self._target = (stamp, result)
        return dict(result)

    def smb_login(self) -> Optional[dict]:
        """保存位置是 SMB 共享并且填了账号时：{share, user, password}，供查看器自己去登录这个共享"""
        try:
            cfg = json.loads((self.root / "settings.json").read_text(encoding="utf-8")).get("current") or {}
        except (OSError, ValueError, AttributeError):
            return None
        if (cfg.get("STORAGE_MODE") or "local") != "smb" or not cfg.get("NAS_IP") or not cfg.get("NAS_SHARE"):
            return None
        if not cfg.get("NAS_USER"):
            return None
        return {"share": f"\\\\{cfg['NAS_IP']}\\{cfg['NAS_SHARE']}", "user": str(cfg["NAS_USER"]),
                "password": str(cfg.get("NAS_PASS") or "")}

    def author_names(self) -> Dict[int, str]:
        """画师 ID -> 最新的昵称（文件夹名是乱码短名或 Unknown 时用它）"""
        db = self.root / "db" / "pixiv_manager.db"
        try:
            con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=3)
            try:
                return {int(a): n for a, n in con.execute("SELECT author_id, author_name FROM artists") if n}
            finally:
                con.close()
        except (sqlite3.Error, OSError, ValueError):
            return {}

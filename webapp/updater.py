#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
软件自身的检查更新、下载、替换

新版本发布在 GitHub Releases（仓库见 utils.constants.UPDATE_REPO），每个版本带两个压缩包：
PixivViewer-<版本>-win64.zip 和 PixivDownloader-<版本>-win64.zip。

整个过程分三步，每一步都要用户点了才做，不会自己联网：

1. 检查：问 GitHub 最新版本号，和自己比。
2. 下载：把压缩包下到 <程序文件夹>/data/update/，核对大小和校验值，解压到 staged/。
3. 替换：正在运行的程序没法覆盖自己，所以启动 staged/ 里那份新程序，带上 --apply-update 参数，然后自己退出。
   新程序等旧程序退出后，把程序文件夹里的旧文件改名留底、把新文件复制进去（data 文件夹不碰），
   成功就删掉留底并启动新版本；任何一步失败都改回原样，重新打开旧版本并说明原因。

只有打包后的程序能替换自己；从源码运行时只能检查。

这个模块只用标准库（requests 在用到时才导入），因为替换那一步是在一个什么都还没初始化的进程里跑的。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

API_ENV = "PIXIV_VIEWER_UPDATE_API"        # 测试用：改用别的地址取“最新版本”的信息
OLD_SUFFIX = ".old-update"                 # 替换时旧文件临时改成这个名字
KEEP = ("data",)                           # 程序文件夹里属于用户、绝不能动的东西
WAIT_EXIT_SECS = 90                        # 等旧程序（和它的下载子进程）退出的最长时间


class UpdateError(Exception):
    pass


# ---------------------------------------------------------------- 版本号
def parse_version(text: str) -> Tuple[int, ...]:
    """'v2.6.0' / '2.6' -> (2, 6, 0)；认不出来返回空元组"""
    m = re.match(r"\s*v?(\d+(?:\.\d+)*)", str(text or ""))
    if not m:
        return ()
    parts = [int(x) for x in m.group(1).split(".")]
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def is_newer(latest: str, current: str) -> bool:
    a, b = parse_version(latest), parse_version(current)
    return bool(a) and bool(b) and a > b


# ---------------------------------------------------------------- 检查、下载
class Updater:
    """运行中的程序用的那一半：检查、下载、交给新程序去替换。"""

    def __init__(self, app: str, version: str, repo: str, app_dir: Optional[Path] = None,
                 frozen: Optional[bool] = None, proxies: Optional[Callable[[], Dict[str, str]]] = None) -> None:
        self.app = app                                   # "PixivViewer" / "PixivDownloader"：压缩包和 exe 的名字
        self.version = version
        self.repo = repo
        self.frozen = getattr(sys, "frozen", False) if frozen is None else frozen
        self.app_dir = Path(app_dir) if app_dir else Path(sys.executable).resolve().parent
        self.work = self.app_dir / "data" / "update"
        self._proxies = proxies or (lambda: {})
        self._lock = threading.Lock()
        self._release: Optional[dict] = None
        self._state = {"state": "idle", "done": 0, "total": 0, "error": "", "version": ""}
        self._thread: Optional[threading.Thread] = None
        self._cancel = threading.Event()

    # ---- 能不能自己替换
    def can_apply(self) -> Tuple[bool, str]:
        if not self.frozen:
            return False, "现在是从源码运行的，不能自动替换；请用 git 更新代码"
        if os.name != "nt":
            return False, "自动替换只支持 Windows"
        try:
            self.work.mkdir(parents=True, exist_ok=True)
            probe = self.app_dir / f".write-test-{os.getpid()}"
            probe.write_bytes(b"")
            probe.unlink()
        except OSError:
            return False, "程序所在的文件夹不能写入（例如放在了 Program Files 里），请手动下载新版本"
        return True, ""

    # ---- 联网
    def _get(self, url: str, **kwargs):
        import requests
        headers = {"User-Agent": f"{self.app}/{self.version}", "Accept": "application/vnd.github+json"}
        headers.update(kwargs.pop("headers", {}))
        attempts: List[Optional[Dict[str, str]]] = [None]             # 先按系统的代理设置
        extra = {k: v for k, v in (self._proxies() or {}).items() if v}
        if extra:
            attempts.append(extra)                                    # 不行再用下载设置里填的代理
        last: Optional[Exception] = None
        for proxies in attempts:
            try:
                return requests.get(url, headers=headers, timeout=(10, 60), proxies=proxies, **kwargs)
            except requests.RequestException as error:
                last = error
        raise UpdateError(f"连不上 GitHub：{type(last).__name__}（检查网络或代理）")

    def check(self) -> dict:
        """问一次 GitHub。返回 {current, latest, newer, notes, size, page, canApply, reason}"""
        ok, reason = self.can_apply()
        out = {"current": self.version, "latest": "", "newer": False, "notes": "", "size": 0, "page": "",
               "published": "", "canApply": ok, "reason": reason}
        url = os.environ.get(API_ENV) or f"https://api.github.com/repos/{self.repo}/releases/latest"
        r = self._get(url)
        if r.status_code == 404:
            out["notes"] = "还没有发布过版本"
            return out
        if r.status_code == 403 and "rate limit" in r.text.lower():
            raise UpdateError("GitHub 暂时限制了访问次数，过一会儿再试")
        if r.status_code != 200:
            raise UpdateError(f"GitHub 返回了 {r.status_code}")
        try:
            release = r.json()
        except ValueError:
            raise UpdateError("GitHub 返回的内容看不懂") from None
        latest = str(release.get("tag_name") or "")
        asset = self._pick_asset(release)
        out.update(latest=latest.lstrip("v"), notes=str(release.get("body") or "").strip(),
                   page=str(release.get("html_url") or ""), published=str(release.get("published_at") or ""),
                   newer=is_newer(latest, self.version), size=int(asset["size"]) if asset else 0)
        if out["newer"] and not asset:
            out.update(canApply=False, reason="这个版本没有附带安装包，请到发布页面查看")
        with self._lock:
            self._release = release if out["newer"] and asset else None
        return out

    def _pick_asset(self, release: dict) -> Optional[dict]:
        for asset in release.get("assets") or []:
            name = str(asset.get("name") or "")
            if name.startswith(self.app + "-") and name.endswith("-win64.zip"):
                return asset
        return None

    def status(self) -> dict:
        with self._lock:
            return dict(self._state)

    def _set(self, **kwargs) -> None:
        with self._lock:
            self._state.update(kwargs)

    def start_download(self) -> dict:
        """开始在后台下载刚才检查到的新版本；进度用 status() 看"""
        ok, reason = self.can_apply()
        if not ok:
            raise UpdateError(reason)
        with self._lock:
            release = self._release
            if self._thread is not None and self._thread.is_alive():
                return dict(self._state)
        if not release:
            raise UpdateError("请先检查更新")
        asset = self._pick_asset(release)
        self._cancel.clear()
        self._set(state="downloading", done=0, total=int(asset["size"]), error="",
                  version=str(release.get("tag_name") or "").lstrip("v"))
        self._thread = threading.Thread(target=self._download, args=(asset,), name="update-download", daemon=True)
        self._thread.start()
        return self.status()

    def cancel(self) -> dict:
        self._cancel.set()
        return self.status()

    def _download(self, asset: dict) -> None:
        try:
            url = str(asset.get("browser_download_url") or "")
            if not os.environ.get(API_ENV) and not url.startswith(f"https://github.com/{self.repo}/releases/download/"):
                raise UpdateError("安装包的下载地址不是预期的 GitHub 地址，已放弃")
            shutil.rmtree(self.work, ignore_errors=True)
            self.work.mkdir(parents=True, exist_ok=True)
            target = self.work / str(asset["name"])
            part = target.with_suffix(".part")
            digest = hashlib.sha256()
            done = 0
            with self._get(url, stream=True, headers={"Accept": "application/octet-stream"}) as r:
                if r.status_code != 200:
                    raise UpdateError(f"下载失败：GitHub 返回了 {r.status_code}")
                with open(part, "wb") as f:
                    for chunk in r.iter_content(256 * 1024):
                        if self._cancel.is_set():
                            raise UpdateError("已取消")
                        f.write(chunk)
                        digest.update(chunk)
                        done += len(chunk)
                        self._set(done=done)
            if done != int(asset["size"]):
                raise UpdateError("下载不完整，请重试")
            expected = str(asset.get("digest") or "")
            if expected.startswith("sha256:") and expected[7:].lower() != digest.hexdigest():
                raise UpdateError("下载的文件校验不通过，已放弃")
            part.replace(target)
            self._set(state="unpacking")
            self._unpack(target)
            target.unlink(missing_ok=True)
            self._set(state="ready")
        except Exception as error:
            shutil.rmtree(self.work, ignore_errors=True)
            message = str(error) if isinstance(error, UpdateError) else f"{type(error).__name__}: {error}"
            self._set(state="idle" if message == "已取消" else "error", error="" if message == "已取消" else message)

    def _unpack(self, archive: Path) -> None:
        staged = self.work / "staged"
        with zipfile.ZipFile(archive) as z:
            names = z.namelist()
            if f"{self.app}/{self.app}.exe" not in names:
                raise UpdateError("安装包里的内容不对，已放弃")
            root = staged.resolve()
            for name in names:
                if not str((staged / name).resolve()).startswith(str(root)):
                    raise UpdateError("安装包里有不正常的路径，已放弃")
            z.extractall(staged)

    @property
    def staged_exe(self) -> Path:
        return self.work / "staged" / self.app / f"{self.app}.exe"

    def launch_apply(self) -> bool:
        """启动新程序去做替换。调用之后，这个程序应该尽快退出。"""
        if self.status()["state"] != "ready" or not self.staged_exe.is_file():
            raise UpdateError("新版本还没有下载好")
        ok, reason = self.can_apply()
        if not ok:
            raise UpdateError(reason)
        env = {k: v for k, v in os.environ.items() if not k.startswith(("PIXIV_", "_MEI", "_PYI"))}
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"            # 新程序是另一份独立的程序，不沿用这一份的运行环境
        subprocess.Popen([str(self.staged_exe), "--apply-update", str(self.app_dir), str(os.getpid()), self.version],
                         cwd=str(self.staged_exe.parent), env=env, close_fds=True, creationflags=flags)
        self._set(state="applying")
        return True

    # ---- 更新之后
    def finish(self) -> Optional[dict]:
        """程序启动时调用：清掉上次更新留下的临时文件；如果刚刚更新过，返回 {from, to}（只返回一次）"""
        result = None
        marker = self.work / "done.json"
        try:
            if marker.is_file():
                result = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            result = None
        if self.work.exists() or any(self.app_dir.glob("*" + OLD_SUFFIX)):
            threading.Thread(target=cleanup, args=(self.app_dir,), name="update-cleanup", daemon=True).start()
        return result


class UpdateApiMixin:
    """给界面用的几个接口。使用它的类要有 self._updater（Updater）和 self._window（pywebview 窗口）。

    出错时不抛异常，返回 {"ok": False, "error": 原因}，界面直接把原因显示出来。
    """
    _updater: Optional[Updater] = None
    _window = None
    _update_done: Optional[dict] = None

    def _update_call(self, fn) -> dict:
        if self._updater is None:
            return {"ok": False, "error": "这个程序没有启用更新功能"}
        try:
            return {"ok": True, **(fn() or {})}
        except UpdateError as error:
            return {"ok": False, "error": str(error)}
        except Exception as error:                              # 界面上总要有个说法
            return {"ok": False, "error": f"{type(error).__name__}: {error}"}

    def update_check(self):
        return self._update_call(lambda: self._updater.check())

    def update_start(self):
        return self._update_call(lambda: self._updater.start_download())

    def update_status(self):
        return self._update_call(lambda: self._updater.status())

    def update_cancel(self):
        return self._update_call(lambda: self._updater.cancel())

    def update_apply(self):
        """启动新程序去替换，然后关掉自己的窗口（程序随之退出，下载任务会处理完当前文件再停）"""
        def run():
            self._updater.launch_apply()
            if self._window is not None:
                threading.Timer(0.8, self._window.destroy).start()
            return {}
        return self._update_call(run)

    def update_done(self):
        """刚刚更新过的话返回 {from}，只返回一次；否则返回 None"""
        done, self._update_done = self._update_done, None
        return done


def cleanup(app_dir: Path, tries: int = 8) -> None:
    """删掉 data/update 和替换时留底的旧文件。负责替换的那个进程可能还没完全退出，所以多试几次。"""
    targets = [app_dir / "data" / "update", *app_dir.glob("*" + OLD_SUFFIX)]
    for _ in range(tries):
        for path in targets:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
        if not any(p.exists() for p in targets):
            return
        time.sleep(1.5)


# ---------------------------------------------------------------- 替换（在新程序里跑）
def replace_install(new_root: Path, target: Path, retry_secs: float = 30, log: Callable[[str], None] = lambda s: None) -> None:
    """把 target 里的程序文件换成 new_root 里的。data 等用户的东西不动。

    做法：把要换掉的每一项先改名留底，再把新的复制进去。中途出任何错，就把已经复制的删掉、把留底的改回原名，
    然后抛出 UpdateError——也就是说，要么全换成新的，要么保持原样。
    """
    names = sorted(p.name for p in new_root.iterdir() if p.name not in KEEP)
    if not names:
        raise UpdateError("新版本的文件夹是空的")
    moved: List[str] = []
    copied: List[str] = []
    try:
        for name in names:
            old = target / name
            backup = target / (name + OLD_SUFFIX)
            if backup.exists():
                shutil.rmtree(backup, ignore_errors=True) if backup.is_dir() else backup.unlink()
            if old.exists():
                _rename_when_free(old, backup, retry_secs)
                moved.append(name)
                log(f"留底 {name}")
        for name in names:
            src, dst = new_root / name, target / name
            copied.append(name)
            if src.is_dir():
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)
            log(f"写入 {name}")
    except Exception as error:
        log(f"失败，恢复原样: {type(error).__name__}: {error}")
        for name in copied:
            path = target / name
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
        for name in moved:
            try:
                os.replace(target / (name + OLD_SUFFIX), target / name)
            except OSError as undo_error:
                log(f"恢复 {name} 失败: {undo_error}")
        if isinstance(error, UpdateError):
            raise
        raise UpdateError(f"{type(error).__name__}: {error}") from error
    for name in moved:
        backup = target / (name + OLD_SUFFIX)
        if backup.is_dir():
            shutil.rmtree(backup, ignore_errors=True)
        else:
            try:
                backup.unlink(missing_ok=True)
            except OSError:
                pass


def _rename_when_free(src: Path, dst: Path, retry_secs: float) -> None:
    """改名；文件还被占用（旧程序或它的子进程还没退干净）时等一等再试"""
    deadline = time.monotonic() + retry_secs
    while True:
        try:
            os.replace(src, dst)
            return
        except OSError as error:
            if time.monotonic() >= deadline:
                raise UpdateError(f"{src.name} 还在被占用，没法替换（{error.strerror or error}）") from error
            time.sleep(0.5)


def wait_for_exit(pid: int, timeout: float = WAIT_EXIT_SECS) -> bool:
    """等一个进程退出（Windows）。等到了返回 True。"""
    if os.name != "nt":
        return True
    import ctypes
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x00100000, False, int(pid))      # SYNCHRONIZE
    if not handle:
        return True                                                  # 已经不在了
    try:
        return kernel32.WaitForSingleObject(handle, int(timeout * 1000)) == 0
    finally:
        kernel32.CloseHandle(handle)


def _message(text: str, title: str = "更新") -> None:
    if os.name == "nt":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, title, 0x30)


def apply_main(args: List[str]) -> int:
    """`<新程序> --apply-update <程序文件夹> <旧进程号> <旧版本号>`：等旧程序退出、替换文件、启动新版本。"""
    target = Path(args[0]).resolve()
    pid = int(args[1]) if len(args) > 1 and args[1].isdigit() else 0
    old_version = args[2] if len(args) > 2 else ""
    exe = Path(sys.executable).resolve()
    new_root = exe.parent
    log_file = target / "data" / "logs" / "update.log"

    def log(text: str) -> None:
        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {text}\n")
        except OSError:
            pass

    log(f"开始更新 {target}（旧版本 {old_version}，新程序 {new_root}）")
    error = ""
    if new_root == target or target not in new_root.parents:
        error = "更新程序的位置不对"
    elif pid and not wait_for_exit(pid):
        error = "旧版本一直没有退出"
    if not error:
        try:
            replace_install(new_root, target, log=log)
        except UpdateError as e:
            error = str(e)
    launch = target / exe.name
    if error:
        log(f"更新失败: {error}")
        _message(f"更新没有完成，程序保持原来的版本。\n\n原因：{error}\n\n可以稍后重试，或到发布页面手动下载。")
    else:
        try:
            marker = target / "data" / "update" / "done.json"
            marker.write_text(json.dumps({"from": old_version, "at": time.time()}), encoding="utf-8")
        except OSError:
            pass
        log("更新完成")
    if launch.is_file():
        env = {k: v for k, v in os.environ.items() if not k.startswith(("PIXIV_", "_MEI", "_PYI"))}
        env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        subprocess.Popen([str(launch)], cwd=str(target), env=env, close_fds=True, creationflags=flags)
    return 1 if error else 0

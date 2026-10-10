#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
关闭窗口时放到系统托盘，以及“同一份数据只开一个程序”

- Tray：把窗口藏起来、在托盘区放一个图标；点图标回来。藏起来期间程序照常运行，下载不会中断。
  鼠标停在图标上会显示现在在做什么（下载到多少、速度、是否暂停）；右键菜单里可以暂停 / 继续 / 停止任务、
  直接打开“下载与更新”或“设置”、退出。托盘图标用的是 WinForms 的 NotifyIcon（pywebview 在 Windows 上本来就用 WinForms）。
- CloseMixin：给界面用的接口。关闭时怎么办（每次询问 / 放到托盘 / 直接退出）由界面按设置决定，
  这里只负责执行；从任务栏、Alt+F4 关窗口时不直接关，而是转给界面去问。
- SingleInstance：放到托盘后人很容易忘了它还开着，再点一次图标就会开出第二个——两个程序同时下载会抢同一个
  数据库。所以按数据文件夹加一把锁：第二个启动时只是把第一个的窗口叫出来，然后自己退出。

只在 Windows 上起作用；别的系统上“放到托盘”退化成最小化。
"""
from __future__ import annotations

import ctypes
import hashlib
import sys
import threading
from pathlib import Path
from typing import Callable, Optional

from utils.lang import pick
from utils.logger import get_logger

logger = get_logger("Tray")


TIP_MAX = 63          # 托盘图标的悬浮提示最多这么长（系统的限制，超了会报错）
POLL_SECS = 2.0       # 窗口在托盘里时，多久看一次任务进行到哪了


def _size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return ""


def describe_job(job: Optional[dict], speed: float = 0.0) -> dict:
    """把任务的状态写成托盘上用的几句话：{state: idle|running|paused, line, detail}

    line：一行说清现在在做什么（菜单第一行、悬浮提示都用它）；detail：再补一句（速度、失败数）。
    """
    if not job or not (job.get("running") or job.get("status") == "running"):
        return {"state": "idle", "line": pick("没有任务在运行", "No job running", "実行中のタスクはありません"), "detail": ""}
    kind = str(job.get("kind") or "")
    checking = job.get("phase") == "同步" or (kind.startswith("sync") and job.get("phase") != "下载")
    done, total = int(job.get("done") or 0), int(job.get("total") or 0)
    what = pick("检查中", "Checking", "確認中") if checking else pick("下载中", "Downloading", "ダウンロード中")
    paused = bool(job.get("paused"))
    if paused:
        what = pick("已暂停", "Paused", "一時停止中")
    progress = f"{done:,} / {total:,}" + (f" ({done * 100 // total}%)" if total else "") if total else f"{done:,}"
    bits = []
    if speed > 1024 and not paused:
        bits.append(f"{_size(speed)}/s")
    if job.get("failed"):
        bits.append(pick(f"失败 {job['failed']}", f"{job['failed']} failed", f"失敗 {job['failed']}"))
    idle = int(job.get("idle") or 0)
    if idle >= 120 and not paused:
        bits.append(pick(f"{idle // 60} 分钟没有进展", f"no progress for {idle // 60} min", f"{idle // 60} 分間進捗なし"))
    return {"state": "paused" if paused else "running", "line": f"{what} {progress}", "detail": " · ".join(bits)}


def tooltip(title: str, status: dict) -> str:
    text = title + "\n" + status["line"] + ("\n" + status["detail"] if status["detail"] else "")
    return text[:TIP_MAX]


class Tray:
    def __init__(self, window, title: str, icon_path, on_quit: Callable[[], None],
                 job: Optional[Callable[[], Optional[dict]]] = None, actions: Optional[dict] = None) -> None:
        """job()：现在的任务状态（没有下载功能、或下载进程还没启动时返回 None）；
        actions：菜单里能做的事 {pause, resume, stop, downloader, settings}，没给的那一项不出现。"""
        self.window = window
        self.title = title
        self.icon_path = str(icon_path or "")
        self.on_quit = on_quit
        self._job = job
        self._actions = actions or {}
        self._icon = None
        self._items = {}                # 菜单项：名字 -> ToolStripMenuItem
        self._keep = []                 # 事件处理函数要一直被引用着
        self.hidden = False
        self.status = describe_job(None)
        self.tip = title                # 现在的悬浮提示（测试和诊断用）
        self._last = (0.0, 0)           # 上一次看的时间和已收到的字节数（算速度）
        self._poller = None

    # ---- 任务状态：窗口在托盘里时每隔一会儿看一眼，更新悬浮提示
    def refresh(self, now: Optional[float] = None) -> dict:
        """看一眼任务进行到哪了，更新 status / tip。不动界面，可以在任何线程里调用。"""
        import time
        job = None
        try:
            job = self._job() if self._job else None
        except Exception as e:
            logger.debug(f"托盘读取任务状态失败: {e}")
        now = now or time.time()
        speed = 0.0
        if job:
            got = int(job.get("transferred") or job.get("bytes") or 0)
            t0, b0 = self._last
            if t0 and now > t0 and got >= b0:
                speed = (got - b0) / (now - t0)
            self._last = (now, got)
        else:
            self._last = (0.0, 0)
        self.status = describe_job(job, speed)
        self.tip = tooltip(self.title, self.status)
        return self.status

    def _poll(self) -> None:
        import time
        while self.hidden and self._icon is not None:
            self.refresh()
            tip = self.tip

            def apply(tip=tip):
                if self._icon is not None:
                    self._icon.Text = tip
            try:
                self._ui(apply)
            except Exception:
                return                      # 窗口没了
            time.sleep(POLL_SECS)

    def menu_state(self) -> list:
        """右键菜单现在该是什么样：[(名字, 文字, 是否显示, 是否可点)]。根据最近一次看到的任务状态。"""
        st, a = self.status, self._actions
        running, paused = st["state"] != "idle", st["state"] == "paused"
        return [
            ("open", pick(f"打开 {self.title}", f"Open {self.title}", f"{self.title} を開く"), True, True),
            ("status", st["line"] + (f"  ·  {st['detail']}" if st["detail"] else ""), self._job is not None, False),
            ("pause", pick("暂停任务", "Pause job", "タスクを一時停止"), running and not paused and "pause" in a, True),
            ("resume", pick("继续任务", "Resume job", "タスクを再開"), paused and "resume" in a, True),
            ("stop", pick("停止任务", "Stop job", "タスクを停止"), running and "stop" in a, True),
            ("downloader", pick("下载与更新…", "Download && update…", "ダウンロードと更新…"), "downloader" in a, True),
            ("settings", pick("设置…", "Settings…", "設定…"), "settings" in a, True),
            ("quit", pick("退出", "Quit", "終了"), True, True),
        ]

    def _sync_menu(self) -> list:
        """把真正的菜单项摆成 menu_state() 说的样子（在界面线程里）。返回摆好之后实际显示的各项文字。"""
        shown = []
        for name, text, visible, enabled in self.menu_state():
            item = self._items.get(name)
            if item is not None:
                item.Text, item.Visible, item.Enabled = text, visible, enabled
                if visible:
                    shown.append(str(item.Text))
        return shown

    def native_state(self) -> dict:
        """真正的托盘图标现在是什么样（诊断和测试用）：图标在不在、悬浮提示、菜单里实际显示的各项"""
        out = {"icon": False, "tip": "", "menu": []}
        if self._icon is None:
            return out

        def read():
            out.update(icon=bool(self._icon.Visible), tip=str(self._icon.Text), menu=self._sync_menu())
        try:
            self._ui(read)
        except Exception as e:
            out["error"] = str(e)
        return out

    def _act(self, name: str) -> None:
        """菜单里点了一项（在界面线程里）"""
        if name == "open":
            return self._restore_now()
        if name == "quit":
            return self._quit_from_menu()
        fn = self._actions.get(name)
        if fn is None:
            return
        if name in ("downloader", "settings"):
            self._restore_now()             # 先把窗口叫出来，再让界面打开对应的面板
        threading.Thread(target=self._run_action, args=(name, fn), name=f"tray-{name}", daemon=True).start()

    def _run_action(self, name: str, fn) -> None:
        try:
            fn()
        except Exception as e:
            logger.warning(f"托盘菜单“{name}”没有成功: {e}")
        self.refresh()

    def _ui(self, fn) -> None:
        """在界面线程里执行（托盘图标和窗口都属于它）"""
        from System import Action   # pythonnet，随 pywebview 安装
        self.window.native.Invoke(Action(fn))

    def _ensure_icon(self) -> None:
        if self._icon is not None:
            return
        import clr
        clr.AddReference("System.Windows.Forms")
        clr.AddReference("System.Drawing")
        from System import EventHandler
        from System.Drawing import Icon, SystemIcons
        from System.ComponentModel import CancelEventHandler
        from System.Drawing import FontStyle, Font
        from System.Windows.Forms import (ContextMenuStrip, MouseButtons, MouseEventHandler, NotifyIcon, ToolStripMenuItem,
                                          ToolStripSeparator)

        icon = NotifyIcon()
        try:
            icon.Icon = Icon(self.icon_path) if self.icon_path and Path(self.icon_path).is_file() else SystemIcons.Application
        except Exception:
            icon.Icon = SystemIcons.Application
        icon.Text = self.title[:60]

        def on_click(sender, args):
            if args.Button == MouseButtons.Left:
                self._restore_now()

        click_handler = MouseEventHandler(on_click)
        menu = ContextMenuStrip()
        for name, text, _visible, _enabled in self.menu_state():
            item = ToolStripMenuItem(text)
            handler = EventHandler(lambda s, a, n=name: self._act(n))
            item.Click += handler
            if name == "open":
                item.Font = Font(item.Font, FontStyle.Bold)        # 左键单击的默认动作
            if name in ("pause", "downloader", "quit"):
                sep = ToolStripSeparator()
                menu.Items.Add(sep)
                self._keep.append(sep)
            menu.Items.Add(item)
            self._items[name] = item
            self._keep += [item, handler]

        # 每次打开菜单时按最近看到的任务状态摆一遍（哪些项出现、写什么）。这里不去问下载进程，免得菜单卡一下
        opening_handler = CancelEventHandler(lambda s, a: self._sync_menu())
        menu.Opening += opening_handler
        icon.ContextMenuStrip = menu
        icon.MouseClick += click_handler
        balloon_handler = EventHandler(lambda s, a: self._restore_now())      # 点通知：把窗口叫出来
        icon.BalloonTipClicked += balloon_handler
        self._keep += [click_handler, balloon_handler, opening_handler, menu]
        self._icon = icon

    def notify(self, title: str, text: str = "") -> bool:
        """弹一条系统通知（用托盘图标发）。窗口没有放在托盘里时，图标只在通知期间短暂出现。"""
        if sys.platform != "win32":
            return False

        def run():
            self._ensure_icon()
            from System.Windows.Forms import ToolTipIcon
            self._icon.Visible = True
            self._icon.ShowBalloonTip(8000, str(title)[:60] or self.title, str(text or " ")[:240], ToolTipIcon.Info)

        def tidy():
            def hide():
                if self._icon is not None and not self.hidden:
                    self._icon.Visible = False
            try:
                self._ui(hide)
            except Exception:
                pass

        try:
            self._ui(run)
            if not self.hidden:
                threading.Timer(12, tidy).start()
            return True
        except Exception as e:
            logger.debug(f"系统通知没有发出去: {e}")
            return False

    def hide(self) -> bool:
        """把窗口放到托盘。成功返回 True。"""
        if sys.platform != "win32":
            self.window.minimize()
            return False
        try:
            def run():
                self._ensure_icon()
                self._icon.Visible = True
                self.window.native.Hide()
            self._ui(run)
            self.hidden = True
            logger.info("窗口已放到托盘")
            if self._poller is None or not self._poller.is_alive():
                self._poller = threading.Thread(target=self._poll, name="tray-status", daemon=True)
                self._poller.start()
            return True
        except Exception as e:
            logger.warning(f"放到托盘失败，改为最小化: {e}")
            try:
                self.window.minimize()
            except Exception:
                pass
            return False

    def _restore_now(self) -> None:
        """已经在界面线程里：把窗口叫回来"""
        try:
            from System.Windows.Forms import FormWindowState
            form = self.window.native
            form.Show()
            if form.WindowState == FormWindowState.Minimized:
                form.WindowState = FormWindowState.Normal
            form.Activate()
            if self._icon is not None:
                self._icon.Visible = False
            self.hidden = False
        except Exception as e:
            logger.warning(f"从托盘恢复窗口失败: {e}")

    def restore(self) -> None:
        if sys.platform != "win32":
            return
        try:
            self._ui(self._restore_now)
        except Exception as e:
            logger.warning(f"从托盘恢复窗口失败: {e}")

    def _quit_from_menu(self) -> None:
        self.dispose_now()
        threading.Thread(target=self.on_quit, name="tray-quit", daemon=True).start()

    def dispose_now(self) -> None:
        if self._icon is not None:
            try:
                self._icon.Visible = False
                self._icon.Dispose()
            except Exception:
                pass
            self._icon = None

    def dispose(self) -> None:
        if self._icon is None:
            return
        try:
            self._ui(self.dispose_now)
        except Exception:
            self.dispose_now()


class CloseMixin:
    """关闭与托盘。使用它的类要有 self._window；在窗口建好之后调用一次 _init_close(标题, 图标)。"""
    _tray: Optional[Tray] = None
    _quitting = False

    def _tray_job(self) -> Optional[dict]:
        """托盘上显示的任务状态。有下载功能的类覆盖它；默认没有。"""
        return None

    def _tray_actions(self) -> dict:
        """托盘右键菜单里除了“打开”“退出”还能做的事。有下载功能的类覆盖它。"""
        return {}

    def tray_info(self):
        """托盘现在的样子（悬浮提示、菜单里有哪些项），诊断和测试用"""
        t = self._tray
        if t is None:
            return {"hidden": False, "tip": "", "menu": []}
        t.refresh()
        return {"hidden": t.hidden, "tip": t.tip, "state": t.status["state"],
                "menu": [{"name": n, "text": text, "enabled": enabled} for n, text, visible, enabled in t.menu_state() if visible],
                "native": t.native_state()}

    def _init_close(self, title: str, icon_path) -> None:
        window = self._window
        self._tray = Tray(window, title, icon_path, self._quit, job=self._tray_job, actions=self._tray_actions())

        def on_closing():
            # 从任务栏、Alt+F4 关窗口：不直接关，转给界面按设置处理（询问 / 托盘 / 退出）。真要退出时放行。
            if self._quitting:
                return True
            threading.Thread(target=self._ask_page_to_close, name="close-ask", daemon=True).start()
            return False

        window.events.closing += on_closing

    def _ask_page_to_close(self) -> None:
        try:
            handled = self._window.evaluate_js("window.__pvRequestClose ? (window.__pvRequestClose(), true) : false")
        except Exception:
            handled = False
        if not handled:                       # 界面还没准备好：直接退出，不要让窗口关不掉
            self._quit()

    def _quit(self) -> None:
        self._quitting = True
        if self._tray is not None:
            self._tray.dispose()
        try:
            self._window.destroy()
        except Exception:
            pass

    def _window_close_action(self, action: str):
        """处理 close / tray / restore；别的动作返回 None，交给 chrome.window_action。"""
        if action == "close":
            self._quit()
            return False
        if action == "tray":
            return bool(self._tray and self._tray.hide())
        if action == "restore":
            if self._tray:
                self._tray.restore()
            return False
        return None


class SingleInstance:
    """同一个数据文件夹只让一个程序在跑。第二个启动时通知第一个把窗口显示出来。"""

    def __init__(self, name: str, data_dir) -> None:
        key = hashlib.md5(str(Path(data_dir).resolve()).lower().encode("utf-8")).hexdigest()[:16]
        self.mutex_name = f"Local\\{name}.{key}.lock"
        self.event_name = f"Local\\{name}.{key}.show"
        self._mutex = None
        self._event = None
        self._stop = False

    def acquire(self) -> bool:
        """拿到锁返回 True。已经有一个在跑了就通知它显示窗口，并返回 False。"""
        if sys.platform != "win32":
            return True
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateMutexW.restype = ctypes.c_void_p
        k.CreateEventW.restype = ctypes.c_void_p
        k.OpenEventW.restype = ctypes.c_void_p
        ctypes.set_last_error(0)
        self._mutex = k.CreateMutexW(None, False, self.mutex_name)
        if ctypes.get_last_error() == 183:                                  # ERROR_ALREADY_EXISTS
            handle = k.OpenEventW(0x0002, False, self.event_name)           # EVENT_MODIFY_STATE
            if handle:
                k.SetEvent(ctypes.c_void_p(handle))
                k.CloseHandle(ctypes.c_void_p(handle))
            return False
        self._event = k.CreateEventW(None, False, False, self.event_name)
        return True

    def watch(self, on_show: Callable[[], None]) -> None:
        """有第二个程序想启动时调用 on_show（在后台线程里）"""
        if sys.platform != "win32" or not self._event:
            return
        k = ctypes.windll.kernel32

        def loop():
            while not self._stop:
                if k.WaitForSingleObject(ctypes.c_void_p(self._event), 500) == 0:
                    try:
                        on_show()
                    except Exception as e:
                        logger.warning(f"显示窗口失败: {e}")

        threading.Thread(target=loop, name="single-instance", daemon=True).start()

    def release(self) -> None:
        self._stop = True

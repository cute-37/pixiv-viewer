#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
关闭窗口时放到系统托盘，以及“同一份数据只开一个程序”

- Tray：把窗口藏起来、在托盘区放一个图标；点图标回来，右键菜单里可以退出。藏起来期间程序照常运行，
  下载不会中断。托盘图标用的是 WinForms 的 NotifyIcon（pywebview 在 Windows 上本来就用 WinForms）。
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

from utils.logger import get_logger

logger = get_logger("Tray")


class Tray:
    def __init__(self, window, title: str, icon_path, on_quit: Callable[[], None]) -> None:
        self.window = window
        self.title = title
        self.icon_path = str(icon_path or "")
        self.on_quit = on_quit
        self._icon = None
        self._keep = []                 # 事件处理函数要一直被引用着
        self.hidden = False

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
        from System.Windows.Forms import ContextMenuStrip, MouseButtons, MouseEventHandler, NotifyIcon, ToolStripMenuItem

        icon = NotifyIcon()
        try:
            icon.Icon = Icon(self.icon_path) if self.icon_path and Path(self.icon_path).is_file() else SystemIcons.Application
        except Exception:
            icon.Icon = SystemIcons.Application
        icon.Text = self.title[:60]

        def on_click(sender, args):
            if args.Button == MouseButtons.Left:
                self._restore_now()

        open_handler = EventHandler(lambda s, a: self._restore_now())
        quit_handler = EventHandler(lambda s, a: self._quit_from_menu())
        click_handler = MouseEventHandler(on_click)
        menu = ContextMenuStrip()
        show_item = ToolStripMenuItem(f"打开 {self.title}")
        show_item.Click += open_handler
        quit_item = ToolStripMenuItem("退出")
        quit_item.Click += quit_handler
        menu.Items.Add(show_item)
        menu.Items.Add(quit_item)
        icon.ContextMenuStrip = menu
        icon.MouseClick += click_handler
        balloon_handler = EventHandler(lambda s, a: self._restore_now())      # 点通知：把窗口叫出来
        icon.BalloonTipClicked += balloon_handler
        self._keep += [open_handler, quit_handler, click_handler, balloon_handler, menu, show_item, quit_item]
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

    def _init_close(self, title: str, icon_path) -> None:
        window = self._window
        self._tray = Tray(window, title, icon_path, self._quit)

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

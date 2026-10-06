# -*- coding: utf-8 -*-
"""
无边框窗口的原生行为（仅 Windows）

界面自己画标题栏，所以窗口不要系统标题栏；但缩放边框、贴边分屏、最大化不遮任务栏这些
系统行为要保留。做法：窗口以 pywebview 的 frameless 方式创建，再把可缩放边框的窗口样式加回来，
并接管 WM_NCCALCSIZE 去掉顶部那条多出来的边。任何一步失败都只是退回普通的无边框窗口。
"""
from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

from utils.logger import get_logger

logger = get_logger("WebChrome")

GWL_STYLE, GWLP_WNDPROC = -16, -4
WS_THICKFRAME, WS_SYSMENU, WS_MINIMIZEBOX, WS_MAXIMIZEBOX = 0x00040000, 0x00080000, 0x00020000, 0x00010000
WM_NCCALCSIZE, WM_NCLBUTTONDOWN, WM_SYSCOMMAND, WM_NCDESTROY = 0x0083, 0x00A1, 0x0112, 0x0082
WM_SIZE, WM_MOVE, WM_APP_FIXSIZE = 0x0005, 0x0003, 0x8000 + 0x51
SC_MINIMIZE, SC_MAXIMIZE, SC_RESTORE = 0xF020, 0xF030, 0xF120
HTCAPTION = 2
SWP_FRAME = 0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0020   # NOSIZE | NOMOVE | NOZORDER | NOACTIVATE | FRAMECHANGED

_hooks = {}   # hwnd -> (回调对象, 原窗口过程)；回调对象必须一直被引用，否则会被回收


def _user32():
    u = ctypes.windll.user32
    u.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    u.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
    u.SetWindowLongPtrW.restype = ctypes.c_ssize_t
    u.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
    u.CallWindowProcW.restype = ctypes.c_ssize_t
    u.CallWindowProcW.argtypes = [ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    u.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    u.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    u.IsZoomed.argtypes = [wintypes.HWND]
    u.IsIconic.argtypes = [wintypes.HWND]
    u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                               wintypes.UINT]
    return u


def _hwnd(window) -> int:
    native = getattr(window, "native", None)
    return int(native.Handle.ToInt64()) if native is not None else 0


def install(window) -> bool:
    """窗口显示后调用：恢复缩放边框并去掉顶部多余的边"""
    if sys.platform != "win32":
        return False
    try:
        hwnd = _hwnd(window)
        if not hwnd or hwnd in _hooks:
            return False
        u = _user32()
        proc_type = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

        # 窗口类库以为这是没有边框的窗口，从最大化 / 最小化还原时会按“没有边框”算大小，
        # 每还原一次窗口就小一圈。这里记住普通状态下的位置，还原后放回去。
        state = {"normal": None, "prev": 0, "fixing": False}

        def remember(h):
            # prev 不为 0 表示正从最大化 / 最小化还原，此时的位置是被算小了的，不能记
            if state["fixing"] or state["prev"] or u.IsZoomed(h) or u.IsIconic(h):
                return
            rect = wintypes.RECT()
            if u.GetWindowRect(h, ctypes.byref(rect)):
                state["normal"] = (rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top)

        def wndproc(h, msg, wparam, lparam):
            old = _hooks[hwnd][1]
            if msg == WM_SIZE:
                result = u.CallWindowProcW(old, h, msg, wparam, lparam)
                if wparam in (1, 2):                      # 最小化 / 最大化
                    state["prev"] = wparam
                elif wparam == 0:
                    if state["prev"] and state["normal"]:
                        state["prev"] = 0
                        state["fixing"] = True
                        u.PostMessageW(h, WM_APP_FIXSIZE, 0, 0)   # 等类库自己的调整做完再放回去
                    else:
                        remember(h)
                return result
            if msg == WM_MOVE:
                result = u.CallWindowProcW(old, h, msg, wparam, lparam)
                remember(h)
                return result
            if msg == WM_APP_FIXSIZE:
                if state["normal"] and not u.IsZoomed(h) and not u.IsIconic(h):
                    x, y, w, hh = state["normal"]
                    u.SetWindowPos(h, None, x, y, w, hh, 0x0004 | 0x0010)
                state["fixing"] = False
                return 0
            if msg == WM_NCCALCSIZE and wparam:
                # 默认处理会在四周都留出缩放边框；左右下三边本来就是透明的，只有顶部那条看得见。
                # 普通状态下把顶部还给内容区；最大化时窗口本身比屏幕大一圈，保留默认值才不会被裁掉。
                rect = ctypes.cast(lparam, ctypes.POINTER(wintypes.RECT))
                top = rect[0].top
                result = u.CallWindowProcW(old, h, msg, wparam, lparam)
                if not u.IsZoomed(h):
                    rect[0].top = top
                return result
            if msg == WM_NCDESTROY:
                u.SetWindowLongPtrW(h, GWLP_WNDPROC, old)
                result = u.CallWindowProcW(old, h, msg, wparam, lparam)
                _hooks.pop(hwnd, None)
                return result
            return u.CallWindowProcW(old, h, msg, wparam, lparam)

        callback = proc_type(wndproc)
        _hooks[hwnd] = (callback, u.GetWindowLongPtrW(hwnd, GWLP_WNDPROC))
        u.SetWindowLongPtrW(hwnd, GWLP_WNDPROC, ctypes.cast(callback, ctypes.c_void_p).value)
        style = u.GetWindowLongPtrW(hwnd, GWL_STYLE)
        u.SetWindowLongPtrW(hwnd, GWL_STYLE, style | WS_THICKFRAME | WS_SYSMENU | WS_MINIMIZEBOX | WS_MAXIMIZEBOX)
        u.SetWindowPos(hwnd, None, 0, 0, 0, 0, SWP_FRAME)
        remember(hwnd)
        return True
    except Exception as e:
        logger.warning(f"窗口边框设置失败（仍可使用，只是不能拖边缩放）: {e}")
        return False


def _start_drag(window, hwnd: int, hit: int = HTCAPTION) -> None:
    """交给系统的“拖动标题栏 / 拖动边框”流程：这样才有贴边分屏、拖到顶部最大化等行为。必须在界面线程里做。"""
    from System import Func, Type   # pythonnet，随 pywebview 安装

    def run():
        u = _user32()
        ctypes.windll.user32.ReleaseCapture()
        u.SendMessageW(hwnd, WM_NCLBUTTONDOWN, hit, 0)

    window.native.BeginInvoke(Func[Type](run))


def _follow_cursor(hwnd: int) -> None:
    """备用的拖动方式：按住左键期间让窗口跟着鼠标走（没有贴边分屏，但一定能拖动）"""
    import time
    u = _user32()
    start, rect, pt = wintypes.POINT(), wintypes.RECT(), wintypes.POINT()
    u.GetCursorPos(ctypes.byref(start))
    u.GetWindowRect(hwnd, ctypes.byref(rect))
    deadline = time.time() + 60
    while u.GetAsyncKeyState(0x01) & 0x8000 and time.time() < deadline:
        u.GetCursorPos(ctypes.byref(pt))
        u.SetWindowPos(hwnd, None, rect.left + pt.x - start.x, rect.top + pt.y - start.y, 0, 0, 0x0001 | 0x0004 | 0x0010)
        time.sleep(0.008)


def window_action(window, action: str) -> bool:
    """标题栏按钮：minimize / toggle / close / drag / follow / state。返回窗口当前是否最大化。"""
    if window is None:
        return False
    if action == "close":
        window.destroy()
        return False
    if sys.platform != "win32":
        if action == "minimize":
            window.minimize()
        elif action == "toggle":
            window.toggle_fullscreen()
        return False
    hwnd = _hwnd(window)
    if not hwnd:
        return False
    u = _user32()
    zoomed = bool(u.IsZoomed(hwnd))
    if action == "minimize":
        u.PostMessageW(hwnd, WM_SYSCOMMAND, SC_MINIMIZE, 0)
    elif action == "toggle":
        u.PostMessageW(hwnd, WM_SYSCOMMAND, SC_RESTORE if zoomed else SC_MAXIMIZE, 0)
        return not zoomed
    elif action == "drag":
        _start_drag(window, hwnd)
    elif action.startswith("resize:") and not zoomed:
        # 顶部边框被去掉了（见 install），上边缘的缩放由界面上的细条触发
        hit = {"top": 12, "topleft": 13, "topright": 14}.get(action[7:])
        if hit:
            _start_drag(window, hwnd, hit)
    elif action == "follow" and not zoomed:
        _follow_cursor(hwnd)
    return zoomed

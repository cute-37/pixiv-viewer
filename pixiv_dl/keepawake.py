"""任务进行时不让电脑自动睡眠（Windows）。

锁屏、关屏幕都不影响下载；会让下载停下来的是系统空闲一段时间后自动睡眠。这里在有任务运行时告诉系统
“现在有事在做，先别睡”，任务结束、暂停或停止后撤回，电脑恢复正常的睡眠规则。屏幕该关还是会关。
挡不住的：合上笔记本盖子、手动点睡眠、系统更新重启。

系统的这个请求是跟着线程走的（哪个线程提的，就得由哪个线程撤回，线程没了请求也就没了），
所以专门开一个小线程来管：它每秒看一眼“现在该不该保持唤醒”，状态变了才去告诉系统。
"""
import ctypes
import logging
import sys
import threading

logger = logging.getLogger("PixivDownloader")

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def _set_state(awake):
    """告诉系统要不要保持唤醒。成功返回 True。"""
    if sys.platform != "win32":
        return False
    flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if awake else 0)
    return bool(ctypes.windll.kernel32.SetThreadExecutionState(ctypes.c_uint(flags)))


class KeepAwake:
    def __init__(self, wanted):
        """wanted()：现在该不该保持唤醒（有任务在跑、没有暂停、设置里开着）"""
        self._wanted = wanted
        self._thread = None
        self._stop = threading.Event()
        self.active = False

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="keep-awake", daemon=True)
        self._thread.start()

    def _apply(self, want):
        if want == self.active:
            return
        try:
            if _set_state(want) or not want:
                self.active = want
                logger.info("任务进行中：已请求系统不要自动睡眠" if want else "已恢复系统正常的睡眠规则")
        except Exception as e:
            logger.debug(f"设置防睡眠状态失败: {e}")

    def _loop(self):
        try:
            while not self._stop.wait(1.0):
                try:
                    want = bool(self._wanted())
                except Exception:
                    want = False
                self._apply(want)
        finally:
            self._apply(False)

    def stop(self):
        self._stop.set()

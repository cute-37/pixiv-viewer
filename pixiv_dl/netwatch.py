"""网络断了的时候等它回来，而不是让文件一个接一个地失败。

典型情况：电脑从睡眠里醒来的头几秒、路由器重启、代理软件没开。这时每个请求都会报网络错误；
以前的做法是照常重试、记失败、占用文件的重试次数，几分钟就能把一大批文件打成“失败”。

现在：遇到网络类的错误时先看一眼网络到底通不通（探一下 Pixiv 的接口地址，走和下载一样的代理）。
不通，就让所有线程一起等，每隔一会儿再探一次，通了接着做；这段时间里的请求不算失败、不占重试次数。
等得太久（默认半小时）就不再等了，按普通的失败处理，免得任务永远挂着。

每个任务开始时 reset(job)。
"""
import threading
import time

from pixiv_dl import interrupt

PROBE_URL = "https://app-api.pixiv.net/"
CACHE_SECS = 8              # 刚探过就不重复探
RETRY_SECS = 15             # 断着的时候每隔这么久再探一次
MAX_WAIT = 1800             # 最多等这么久
GIVE_UP_SECS = 900          # 等到头还没好：这段时间内不再因为网络问题停下来等


def probe():
    """网络现在通不通：能从 Pixiv 的接口地址收到任何回应（哪怕是 4xx）就算通。"""
    import requests

    from pixiv_dl.config import Config
    try:
        requests.get(PROBE_URL, proxies=Config.PROXIES or None, timeout=6, allow_redirects=False,
                     headers={"User-Agent": "PixivViewer/net-check"})
        return True
    except requests.RequestException:
        return False


class NetWatch:
    def __init__(self, job=None):
        self._job = job
        self._lock = threading.Lock()
        self._checked = 0.0
        self._up = True
        self._waiting = False
        self._gave_up_until = 0.0
        self.waited = 0.0
        self.outages = 0

    def is_up(self):
        """网络通不通（几秒内的结果会复用）。已经决定不再等的那段时间里一律当作通。"""
        now = time.time()
        with self._lock:
            if now < self._gave_up_until:
                return True
            if now - self._checked < CACHE_SECS:
                return self._up
        up = probe()
        with self._lock:
            self._checked, self._up = time.time(), up
        return up

    def wait_until_up(self):
        """等网络恢复。返回 "up" / "interrupted" / "timeout"。多个线程同时来等时，只有一个负责探，其余跟着等。"""
        with self._lock:
            leader = not self._waiting
            if leader:
                self._waiting = True
                self.outages += 1
        if leader and self._job:
            self._job.log("网络连不上，先停下来等它恢复（不算失败）")
        started = time.time()
        try:
            while True:
                waited = time.time() - started
                if self._job:
                    self._job.set(message=f"网络连不上，等待恢复…（已等 {int(waited // 60)} 分 {int(waited % 60)} 秒）")
                if interrupt.wait(RETRY_SECS if leader else 2.0):
                    return "interrupted"
                if leader:
                    up = probe()
                    with self._lock:
                        self._checked, self._up = time.time(), up
                else:
                    with self._lock:
                        up = self._up or not self._waiting
                if up:
                    if leader and self._job:
                        self._job.log(f"网络恢复了，继续（等了 {int((time.time() - started) // 60)} 分 {int((time.time() - started) % 60)} 秒）")
                        self._job.set(message="")
                    return "up"
                if time.time() - started >= MAX_WAIT:
                    if leader:
                        with self._lock:
                            self._gave_up_until = time.time() + GIVE_UP_SECS
                        if self._job:
                            self._job.log(f"网络 {MAX_WAIT // 60} 分钟都没有恢复，不再等了，之后的错误按失败处理")
                            self._job.set(message="")
                    return "timeout"
        finally:
            if leader:
                with self._lock:
                    self._waiting = False
                    self.waited += time.time() - started

    def summary(self):
        with self._lock:
            return {"outages": self.outages, "waited": int(self.waited)} if self.outages else {}


watch = NetWatch()


def reset(job=None):
    global watch
    watch = NetWatch(job)
    return watch

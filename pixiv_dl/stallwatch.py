"""任务长时间没有任何进展时，记下来。

“进展”指：做完了一个文件 / 一位画师，或者正在下载的文件又收到了数据。下大文件时字节一直在涨，不算停。
真的停了（超过 STALL_AFTER 秒什么都没动）就往日志里写一条：停了多久、每个账号当时在干什么（限速中 / 休息 /
等网络 / 卡在哪个作品上）、各个线程停在代码的哪一行。之后每隔 REPEAT_EVERY 秒再写一次；恢复时写一条“恢复了”。
这样事后能从日志里直接看出那段时间是在等什么，而不是只能看到一段空白。

只记录，不干预：不会重启线程，也不会让任务失败。界面上的提示用的是任务状态里的 idle（见 progress.JobState）。
"""
import logging
import sys
import threading
import time

from pixiv_dl import interrupt

logger = logging.getLogger("PixivDownloader")

STALL_AFTER = 120       # 这么久没有任何进展算“停住了”
REPEAT_EVERY = 300      # 停住期间每隔这么久再记一次
POLL = 5.0


def describe(job):
    """各账号当时在干什么，写成一行"""
    parts = []
    for w in job.snapshot(with_logs=False).get("workers", []):
        what = w.get("note") or "；".join(w.get("items") or []) or w.get("text") or w.get("state") or ""
        parts.append(f"{w.get('name')}: {what}")
    return " | ".join(parts) or "（没有账号在工作）"


def where_threads_are(limit=10):
    """各线程停在本项目代码的哪一行（最里面的那一层）。用来分辨是卡在网络请求、写文件还是在等锁。"""
    names = {t.ident: t.name for t in threading.enumerate()}
    out = []
    for ident, frame in sys._current_frames().items():
        name = names.get(ident, "")
        if ident == threading.get_ident() or name in ("keep-awake", "MainThread"):
            continue
        spot, f = None, frame
        while f is not None:                     # 从最里层往外找第一处本项目的代码
            path = f.f_code.co_filename.replace("\\", "/")
            if "/pixiv_dl/" in path:
                spot = f"{path.rsplit('/', 1)[-1]}:{f.f_lineno} {f.f_code.co_name}"
                break
            f = f.f_back
        if spot:
            out.append(f"{name} @ {spot}（最里层 {frame.f_code.co_name}）")
    return out[:limit]


class StallWatch:
    def __init__(self, get_job):
        self._get_job = get_job
        self._thread = None
        self._job_id = None
        self._next = 0.0
        self._stalled = False

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="stall-watch", daemon=True)
        self._thread.start()

    def check(self, now=None):
        """看一眼；该记日志时返回记下的那句话（测试用），否则返回 None。"""
        now = now or time.time()
        job = self._get_job()
        if job is None or not job.running or interrupt.is_paused():
            self._stalled = False
            self._next = 0.0
            return None
        if job.id != self._job_id:
            self._job_id, self._stalled, self._next = job.id, False, 0.0
        idle = job.idle(now)
        if idle < STALL_AFTER:
            if self._stalled:
                self._stalled = False
                self._next = 0.0
                msg = "恢复了：又有进展了"
                logger.info(msg)
                return msg
            return None
        if now < self._next:
            return None
        self._stalled = True
        self._next = now + REPEAT_EVERY
        msg = f"已经 {int(idle // 60)} 分 {int(idle % 60)} 秒没有任何进展（阶段：{job.phase or '-'}）。各账号：{describe(job)}"
        logger.warning(msg)
        for line in where_threads_are():
            logger.warning(f"  线程 {line}")
        job.log(f"已经 {int(idle // 60)} 分钟没有进展，仍在等待（详情见日志）")
        return msg

    def _loop(self):
        while True:
            time.sleep(POLL)
            try:
                self.check()
            except Exception as e:           # 只是旁观者：出什么错都不能影响任务
                logger.debug(f"检查任务进展时出错: {e}")

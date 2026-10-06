"""被 Pixiv 限速时怎么办：每个账号一道“闸”。

以前的做法是哪个请求被限速，哪个线程自己等一会儿再试，试几次就放弃。问题是同一个账号的其他线程照常发请求，
限速期被越拉越长；放弃之后立刻去处理下一位画师，下一位多半也失败，于是一次限速变成一长串失败。

现在：一个账号被限速，这个账号的所有线程都在闸前等；每连续被限速一次，等的时间加长一档；
恢复正常一阵子后回到最短的一档。等待不算失败、不算重试次数。一个任务里某个账号累计等待超过预算，
才认为“这个账号这次没法用了”，由调用方决定怎么收尾（检查：剩下的画师留到下次；下载：换别的账号）。

用法：PixivClient.call 在每次请求前 gate.wait(name)，收到限速回应时 gate.trip(name)，成功时 gate.ok(name)。
每个任务开始时 reset()。
"""
import threading
import time

from pixiv_dl import interrupt

STEPS = (60, 120, 300, 600, 900)      # 连续被限速时，每次等待的秒数
BUDGET = 1800                         # 一个任务里，单个账号最多一共等这么久
CALM_AFTER = 8                        # 连续成功这么多次请求后，回到最短的一档


class RateGate:
    def __init__(self, job=None, steps=None, budget=None):
        self._lock = threading.Lock()
        self._job = job
        self._steps = tuple(steps if steps is not None else STEPS)
        self._budget = BUDGET if budget is None else budget
        self._state = {}              # 账号 -> {until, level, waited, ok, trips, exhausted}

    def _get(self, name):
        return self._state.setdefault(name, {"until": 0.0, "level": 0, "waited": 0.0, "ok": 0, "trips": 0,
                                             "exhausted": False})

    def trip(self, name):
        """这个账号刚被限速。安排一次等待；预算用完时返回 False（不再等了）。"""
        now = time.time()
        with self._lock:
            st = self._get(name)
            if st["exhausted"]:
                return False
            if st["until"] > now + 0.5:           # 别的线程刚安排过等待，不重复加码
                return True
            step = self._steps[min(st["level"], len(self._steps) - 1)]
            if st["waited"] + step > self._budget:
                st["exhausted"] = True
                if self._job:
                    self._job.log(f"账号 {name} 被限速的时间太长（已累计等待 {int(st['waited'] // 60)} 分钟），这次不再使用它")
                return False
            st["until"] = now + step
            st["waited"] += step
            st["level"] += 1
            st["trips"] += 1
            st["ok"] = 0
        if self._job:
            self._job.log(f"账号 {name} 被 Pixiv 限速，暂停 {self._fmt(step)}后继续（不算失败）")
        return True

    def ok(self, name):
        with self._lock:
            st = self._state.get(name)
            if st and st["level"]:
                st["ok"] += 1
                if st["ok"] >= CALM_AFTER:
                    st["level"], st["ok"] = 0, 0

    def wait(self, name):
        """在闸前等到可以继续。被停止时返回 True。"""
        while True:
            with self._lock:
                st = self._state.get(name)
                remain = (st["until"] - time.time()) if st else 0
            if remain <= 0:
                return interrupt.is_set()
            if self._job:
                self._job.set(message=f"账号 {name} 限速中，约 {self._fmt(remain)}后继续")
                self._job.worker_set(name, state="resting", text=f"限速中，约 {self._fmt(remain)}后继续")
            if interrupt.wait(min(remain, 1.0)):
                return True
            with self._lock:
                done = self._state[name]["until"] - time.time() <= 0
            if done and self._job:
                self._job.set(message="")

    def exhausted(self, name):
        with self._lock:
            return bool(self._state.get(name, {}).get("exhausted"))

    def limited(self, name):
        """这个账号现在是不是正在限速等待中"""
        with self._lock:
            st = self._state.get(name)
            return bool(st) and st["until"] > time.time()

    def summary(self):
        with self._lock:
            return {n: {"trips": s["trips"], "waited": int(s["waited"]), "exhausted": s["exhausted"]}
                    for n, s in self._state.items() if s["trips"]}

    @staticmethod
    def _fmt(seconds):
        seconds = int(max(1, round(seconds)))
        return f"{seconds // 60} 分 {seconds % 60} 秒" if seconds >= 60 and seconds % 60 else (
            f"{seconds // 60} 分钟" if seconds >= 60 else f"{seconds} 秒")


gate = RateGate()


def reset(job=None):
    """每个任务开始时调用：换一道新的闸（上一个任务的限速记录不带过来）。"""
    global gate
    gate = RateGate(job)
    return gate

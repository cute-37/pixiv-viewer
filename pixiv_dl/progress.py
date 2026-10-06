import functools
import threading
import time
import uuid
from collections import deque

from pixiv_dl import interrupt


class JobState:
    """一次后台任务（同步/下载/核查…）的可观察状态，线程安全。CLI 和 Web 前端都读它。"""

    def __init__(self, kind="idle", params=None):
        self.id = uuid.uuid4().hex[:8]
        self.kind = kind
        self.params = params or {}
        self.status = "idle" if kind == "idle" else "running"   # idle/running/done/cancelled/error
        self.phase = ""
        self.total = 0
        self.done = 0
        self.success = 0
        self.failed = 0
        self.skipped = 0
        self.bytes = 0
        self.message = ""
        self.error = None
        self.result = {}
        self.started = time.time() if kind != "idle" else None
        self.finished = None
        self.current = {}
        self.workers = {}     # 各账号的工作状态（整个阶段持续显示，不会因为一个作品处理完就消失）
        self.stopping = False
        self.run_id = None    # 写入 runs 表后的记录 ID
        self.detail = {}      # 结果明细：{分组: {键: {name, 计数...}}}，如 new / downloaded / fail_kinds / failed
        self._logs = deque(maxlen=300)
        self._lock = threading.Lock()

    # ---- 写
    def add(self, **inc):
        with self._lock:
            for k, v in inc.items():
                setattr(self, k, getattr(self, k) + v)

    def set(self, **kw):
        with self._lock:
            for k, v in kw.items():
                setattr(self, k, v)

    def tally(self, group, key, name=None, note=None, kind=None, **inc):
        """累计明细：detail[group][key] = {name, note, kind, 计数...}。线程安全。"""
        with self._lock:
            entry = self.detail.setdefault(group, {}).setdefault(str(key), {})
            if name is not None:
                entry['name'] = name
            if note is not None:
                entry['note'] = note
            if kind is not None:
                entry['kind'] = kind
            for k, v in inc.items():
                entry[k] = entry.get(k, 0) + v

    def set_current(self, key, text):
        with self._lock:
            if text is None:
                self.current.pop(key, None)
            else:
                self.current[key] = text

    # ---- 账号工作状态：state = working 处理中 / waiting 作品间隔 / resting 风控休息 / queue 等待任务 / stopped 已停用 / done 已完成
    def workers_reset(self):
        with self._lock:
            self.workers = {}

    def worker_register(self, name, threads=1, role=''):
        with self._lock:
            self.workers[name] = {'name': name, 'role': role, 'threads': int(threads), 'alive': int(threads), 'active': 0,
                                  'state': 'queue', 'text': '', 'note': '', 'success': 0, 'failed': 0, 'bytes': 0}

    def worker_begin(self, name, text):
        with self._lock:
            w = self.workers.get(name)
            if w:
                w['active'] += 1
                w['state'] = 'working'
                w['text'] = text

    def worker_end(self, name, state='queue'):
        with self._lock:
            w = self.workers.get(name)
            if w:
                w['active'] = max(0, w['active'] - 1)
                if w['state'] != 'stopped' and w['active'] == 0:
                    w['state'] = state

    def worker_set(self, name, **kw):
        with self._lock:
            w = self.workers.get(name)
            if w and not (w['state'] == 'stopped' and kw.get('state') not in (None, 'stopped')):
                w.update(kw)

    def worker_add(self, name, **inc):
        with self._lock:
            w = self.workers.get(name)
            if w:
                for k, v in inc.items():
                    w[k] += v

    def worker_exit(self, name):
        """一个工作线程结束；账号的所有线程都结束后标记为已完成（认证失效的账号保持「已停用」）。"""
        with self._lock:
            w = self.workers.get(name)
            if w:
                w['alive'] = max(0, w['alive'] - 1)
                if w['alive'] == 0 and w['state'] != 'stopped':
                    w['state'] = 'done'
                    w['text'] = ''

    def log(self, msg):
        with self._lock:
            self._logs.append((time.time(), str(msg)))

    def finish(self, status, error=None):
        with self._lock:
            self.status = status
            self.error = error
            self.finished = time.time()
            self.current = {}

    # ---- 读
    @property
    def running(self):
        return self.status == "running"

    def snapshot(self, with_logs=True):
        with self._lock:
            end = self.finished or time.time()
            elapsed = (end - self.started) if self.started else 0
            snap = {
                "id": self.id, "kind": self.kind, "params": self.params, "status": self.status,
                "phase": self.phase, "total": self.total, "done": self.done, "success": self.success,
                "failed": self.failed, "skipped": self.skipped, "bytes": self.bytes,
                "message": self.message, "error": self.error, "result": self.result,
                "started": self.started, "finished": self.finished, "elapsed": round(elapsed, 1),
                "current": dict(self.current), "workers": [dict(w) for w in self.workers.values()], "stopping": self.stopping, "run_id": self.run_id,
                "paused": self.status == "running" and interrupt.is_paused(),
                "detail": {g: {k: dict(v) for k, v in d.items()} for g, d in self.detail.items()},
            }
            if with_logs:
                snap["logs"] = [{"t": t, "msg": m} for t, m in self._logs]
            return snap


def _plain_params(kwargs):
    """任务参数里能原样存进记录的部分（长列表只留个数，免得一条记录存几千个编号）"""
    out = {}
    for k, v in kwargs.items():
        if isinstance(v, (int, float, str, bool, type(None))):
            out[k] = v
        elif isinstance(v, (list, tuple)):
            out[k] = list(v) if len(v) <= 20 else {"count": len(v)}
        elif isinstance(v, dict):
            out[k] = _plain_params(v)
    return out


def job_op(kind):
    """给 Processor 的顶层操作加上任务生命周期管理：

    - 最外层调用创建新的 JobState，并清除上一次遗留的中断标志；
    - 嵌套调用（例如 同步+下载）只更新 phase；
    - Ctrl+C / 前端「停止」都会让任务以 cancelled 结束，而不是把整个会话弄坏。
    """
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(self, *args, **kwargs):
            outer = self._job_depth == 0
            if outer:
                interrupt.clear()
                self.job = JobState(kind, _plain_params(kwargs))
                from pixiv_dl import netwatch, ratelimit
                ratelimit.reset(self.job)
                netwatch.reset(self.job)
            if outer:
                hook = getattr(self, '_on_job_start', None)
                if hook is not None:
                    try:
                        hook(self.job)
                    except Exception:
                        pass
            self._job_depth += 1
            self.job.set(phase=kind)
            try:
                result = fn(self, *args, **kwargs)
                if outer:
                    self.job.finish("cancelled" if interrupt.is_set() else "done")
                return result
            except KeyboardInterrupt:
                interrupt.set()
                if outer:
                    self.job.finish("cancelled")
                    return None
                raise
            except InterruptedError:
                if outer:
                    self.job.finish("cancelled")
                    return None
                raise
            except Exception as e:
                if outer:
                    self.job.finish("error", f"{type(e).__name__}: {e}")
                raise
            finally:
                self._job_depth -= 1
                if outer:
                    hook = getattr(self, '_on_job_finished', None)
                    if hook is not None:
                        try:
                            hook(self.job)
                        except Exception:
                            pass
                    interrupt.clear()
        return wrapper
    return deco

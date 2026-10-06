#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
任务结束之后：弹系统通知，以及用户选的“完成后做什么”（睡眠 / 休眠 / 关机 / 退出软件 / 运行命令）

为什么放在后端做：窗口可能在托盘里、屏幕可能锁着，界面上的定时器这时候不可靠；
所以由这里的一个线程盯着下载进程里的任务，结束了就处理。

规则：
- “完成后做什么”是每次任务单独选的，不会记成默认值——免得哪天忘了它还开着“完成后关机”。
- 只有任务自己正常做完才执行。被手动停止、出错、或者停下来等用户确认（新发现的文件太多）时都不执行。
- 执行前有倒计时（关机、睡眠、休眠 60 秒，退出软件 15 秒），期间可以取消。

使用它的类要有：dl(method, path, body) 调下载进程的接口；_tray（webapp/tray.py 的 Tray，可以没有）；_quit() 退出程序。
"""
from __future__ import annotations

import ctypes
import subprocess
import sys
import threading
import time
from typing import Optional

from utils.logger import get_logger

logger = get_logger("AfterJob")

ACTIONS = {"none": "什么都不做", "sleep": "让电脑睡眠", "hibernate": "让电脑休眠", "shutdown": "关机",
           "exit": "退出软件", "command": "运行命令"}
COUNTDOWN = {"sleep": 60, "hibernate": 60, "shutdown": 60, "exit": 15, "command": 0}
POLL_SECS = 2.0


def run_action(action: str, command: str = "", quit_app=None) -> str:
    """真正去做。返回一句给日志 / 通知用的话。"""
    if action == "command":
        if not command.strip():
            return "没有填要运行的命令"
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
        subprocess.Popen(command, shell=True, creationflags=flags, close_fds=True)
        return "已运行命令"
    if action == "exit":
        if quit_app:
            quit_app()
        return "已退出"
    if sys.platform != "win32":
        return "这个系统上不支持"
    if action == "shutdown":
        subprocess.Popen(["shutdown", "/s", "/t", "0"], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return "正在关机"
    if action in ("sleep", "hibernate"):
        # SetSuspendState(是否休眠, 强制, 禁用唤醒事件)
        ok = ctypes.windll.powrprof.SetSuspendState(action == "hibernate", False, False)
        return ("已休眠" if action == "hibernate" else "已睡眠") if ok else "系统拒绝了这个请求（可能没有开启休眠）"
    return ""


def describe(job: dict) -> tuple:
    """把任务的结果写成通知的标题和内容"""
    status, result, kind = job.get("status"), job.get("result") or {}, str(job.get("kind") or "")
    if status == "error":
        return "任务出错了", str(job.get("error") or "")[:120]
    if status == "cancelled":
        return "任务已停止", ""
    if result.get("needs_review"):
        return "检查完成，等你确认", f"新发现 {result.get('new_files', 0)} 个文件，还没有开始下载"
    parts = []
    if kind == "download_avatars":
        parts.append(f"下载了 {job.get('success', 0)} 个头像")
    else:
        if "artists" in result:
            parts.append(f"检查了 {result.get('artists_ok', 0)} 位画师")
        if "tasks" in result or not kind.startswith("sync"):
            parts.append(f"下载 {job.get('success', 0)} 个文件")
    if job.get("failed"):
        parts.append(f"失败 {job.get('failed')}")
    if result.get("unchecked"):
        parts.append(f"{result['unchecked']} 位没查到")
    return "任务完成", "，".join(parts)


class AfterJobMixin:
    _after_lock = threading.Lock()
    _after: Optional[dict] = None           # {job_id, action, command, notify, state, deadline, message, thread}
    _after_handled = None                   # 已经盯过的任务（同一个任务不处理两次）

    def _after_state(self) -> dict:
        a = self._after or {}
        left = max(0, int(round((a.get("deadline") or 0) - time.time()))) if a.get("state") == "countdown" else 0
        return {"ok": True, "action": a.get("action", "none"), "state": a.get("state", "idle"), "seconds": left,
                "message": a.get("message", ""), "label": ACTIONS.get(a.get("action", "none"), "")}

    def job_watch(self, options=None):
        """界面在任务开始后调用：盯着这个任务，结束时通知，并执行选好的“完成后做什么”。"""
        opts = options if isinstance(options, dict) else {}
        job = self._after_job()
        # 很短的任务可能在界面来得及说“盯着它”之前就做完了：刚刚结束、还没处理过的任务也算数
        just_done = bool(job) and not job.get("running") and job.get("finished") and time.time() - job["finished"] < 20             and job.get("id") != self._after_handled
        if not job or not (job.get("running") or just_done):
            return self._after_state()
        action = opts.get("action") if opts.get("action") in ACTIONS else "none"
        with self._after_lock:
            old = self._after
            if old and old.get("job_id") == job.get("id") and old.get("state") in ("watching", "countdown"):
                old.update(notify=bool(opts.get("notify", old.get("notify", True))))
                return self._after_state()
            watch = {"job_id": job.get("id"), "action": action, "command": str(opts.get("command") or ""),
                     "notify": bool(opts.get("notify", True)), "state": "watching", "deadline": 0, "message": ""}
            self._after = watch
            self._after_handled = job.get("id")
        threading.Thread(target=self._after_run, args=(watch,), name="after-job", daemon=True).start()
        return self._after_state()

    def after_job_set(self, action, command=""):
        """任务进行中改主意：换一个“完成后做什么”"""
        with self._after_lock:
            a = self._after
            if not a or a.get("state") != "watching":
                return self._after_state()
            a["action"] = action if action in ACTIONS else "none"
            a["command"] = str(command or "")
        return self._after_state()

    def after_job_state(self):
        return self._after_state()

    def after_job_cancel(self):
        """倒计时期间取消；或者任务还在跑时改回“什么都不做”"""
        with self._after_lock:
            a = self._after
            if a and a.get("state") in ("watching", "countdown"):
                if a["state"] == "countdown":
                    a["state"], a["message"] = "idle", "已取消"
                a["action"] = "none"
        return self._after_state()

    # ---- 内部
    def _after_job(self) -> Optional[dict]:
        try:
            r = self.dl("GET", "/api/job")
            return r.get("data") if r and r.get("ok") else None
        except Exception:
            return None

    def _after_notify(self, title: str, text: str) -> None:
        tray = getattr(self, "_tray", None)
        if tray is not None:
            try:
                tray.notify(title, text)
            except Exception as e:
                logger.debug(f"通知没有发出去: {e}")

    def _after_run(self, watch: dict) -> None:
        job, misses = None, 0
        while watch.get("state") == "watching":
            time.sleep(POLL_SECS)
            job = self._after_job()
            if job is None:
                misses += 1
                if misses > 30:                 # 下载进程一直没有回应：放弃
                    watch["state"] = "idle"
                    return
                continue
            misses = 0
            if job.get("id") != watch["job_id"] or not job.get("running"):
                break
        if watch.get("state") != "watching":
            return
        if not job or job.get("id") != watch["job_id"]:
            watch["state"] = "idle"             # 换了别的任务（界面会为新任务再调一次 job_watch）
            return
        title, text = describe(job)
        action = watch["action"]
        completed = job.get("status") == "done" and not (job.get("result") or {}).get("needs_review")
        if action == "none" or not completed:
            if action != "none":
                text = (text + "。" if text else "") + f"任务没有正常做完，所以没有执行“完成后{ACTIONS[action]}”"
                watch["message"] = "任务没有正常做完，没有执行"
            watch["state"] = "idle"
            if watch["notify"] or action != "none":
                self._after_notify(title, text)
            return
        seconds = COUNTDOWN.get(action, 0)
        watch["deadline"] = time.time() + seconds
        watch["state"] = "countdown"
        if seconds:
            self._after_notify(title, f"{text}。{seconds} 秒后{ACTIONS[action]}，打开窗口可以取消")
            tray = getattr(self, "_tray", None)
            if tray is not None and getattr(tray, "hidden", False):
                tray.restore()                  # 把窗口叫出来，让“取消”看得到
        elif watch["notify"]:
            self._after_notify(title, text)
        while time.time() < watch["deadline"]:
            if watch.get("state") != "countdown":
                logger.info("“完成后”的动作被取消了")
                return
            time.sleep(0.25)
        if watch.get("state") != "countdown":
            return
        watch["state"] = "running"
        logger.info(f"任务完成，执行：{ACTIONS[action]}")
        try:
            watch["message"] = run_action(action, watch.get("command", ""), getattr(self, "_quit", None))
        except Exception as e:
            watch["message"] = f"没有成功：{type(e).__name__}: {e}"
            logger.warning(f"“完成后{ACTIONS[action]}”失败: {e}")
        watch["state"] = "idle"

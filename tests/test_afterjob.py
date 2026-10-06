"""任务结束之后：系统通知、完成后睡眠 / 关机 / 退出 / 运行命令。真正的关机和睡眠用假的代替，不会碰这台电脑。"""
import sys
import time

import pytest

from webapp import afterjob
from webapp.afterjob import AfterJobMixin, describe


class FakeTray:
    def __init__(self):
        self.notes, self.hidden, self.restored = [], False, 0

    def notify(self, title, text=""):
        self.notes.append((title, text))
        return True

    def restore(self):
        self.restored += 1
        self.hidden = False


class Api(AfterJobMixin):
    def __init__(self):
        self.job = {"id": "j1", "kind": "download", "running": True, "status": "running", "success": 0, "failed": 0, "result": {}}
        self._tray = FakeTray()
        self.quit_called = 0

    def dl(self, method, path, body=None):
        return {"ok": True, "data": dict(self.job)}

    def _quit(self):
        self.quit_called += 1

    def finish(self, status="done", **extra):
        self.job.update(running=False, status=status, **extra)


@pytest.fixture
def api(monkeypatch):
    done = []
    monkeypatch.setattr(afterjob, "POLL_SECS", 0.02)
    monkeypatch.setattr(afterjob, "COUNTDOWN", {"sleep": 0.3, "hibernate": 0.3, "shutdown": 0.3, "exit": 0.3, "command": 0})
    monkeypatch.setattr(afterjob, "run_action", lambda action, command="", quit_app=None: done.append((action, command)) or "做完了")
    a = Api()
    a.done = done
    return a


def wait_state(api, states, timeout=3):
    end = time.time() + timeout
    while time.time() < end:
        st = api.after_job_state()
        if st["state"] in states:
            return st
        time.sleep(0.01)
    raise AssertionError(api.after_job_state())


def test_shutdown_runs_after_countdown_when_job_completes(api):
    assert api.job_watch({"action": "shutdown", "notify": True})["state"] == "watching"
    time.sleep(0.15)
    assert api.done == []                                    # 任务还在跑：什么都不做
    api.finish(success=12, result={"tasks": 12})
    st = wait_state(api, {"countdown"})
    assert st["action"] == "shutdown" and st["label"] == "关机" and st["seconds"] >= 0
    wait_state(api, {"idle"})
    assert api.done == [("shutdown", "")]
    title, text = api._tray.notes[0]
    assert title == "任务完成" and "下载 12 个文件" in text and "后关机" in text and "取消" in text


def test_countdown_can_be_cancelled(api):
    api.job_watch({"action": "sleep"})
    api.finish(result={"tasks": 1})
    wait_state(api, {"countdown"})
    assert api.after_job_cancel()["state"] == "idle"
    time.sleep(0.5)
    assert api.done == []


def test_window_is_brought_back_from_tray_for_the_countdown(api):
    api._tray.hidden = True
    api.job_watch({"action": "shutdown"})
    api.finish(result={"tasks": 1})
    wait_state(api, {"countdown"})
    assert api._tray.restored == 1                           # 把窗口叫出来，让“取消”看得到
    api.after_job_cancel()


@pytest.mark.parametrize("status, result", [
    ("cancelled", {}),                                       # 手动停止
    ("error", {}),                                           # 出错
    ("done", {"needs_review": True, "new_files": 5000}),     # 停下来等用户确认：并没有真的下完
])
def test_action_is_not_run_unless_the_job_really_completed(api, status, result):
    api.job_watch({"action": "shutdown"})
    api.finish(status=status, result=result)
    wait_state(api, {"idle"})
    time.sleep(0.4)
    assert api.done == []
    assert "没有执行" in api._tray.notes[-1][1]


def test_notification_only(api):
    api.job_watch({"action": "none", "notify": True})
    api.finish(success=3, failed=1, result={"tasks": 4})
    wait_state(api, {"idle"})
    assert api._tray.notes == [("任务完成", "下载 3 个文件，失败 1")] and api.done == []


def test_no_notification_when_turned_off(api):
    api.job_watch({"action": "none", "notify": False})
    api.finish(result={"tasks": 1})
    wait_state(api, {"idle"})
    time.sleep(0.1)
    assert api._tray.notes == []


def test_action_can_be_changed_while_the_job_runs(api):
    api.job_watch({"action": "none"})
    assert api.after_job_set("exit")["action"] == "exit"
    assert api.after_job_set("nonsense")["action"] == "none"
    api.after_job_set("command", "echo hi")
    api.finish(result={"tasks": 1})
    wait_state(api, {"idle"})
    time.sleep(0.1)
    assert api.done == [("command", "echo hi")]              # 运行命令没有倒计时


def test_watch_is_ignored_when_nothing_is_running(api):
    api.finish()
    assert api.job_watch({"action": "shutdown"})["state"] == "idle"
    time.sleep(0.2)
    assert api.done == []


def test_describe():
    assert describe({"status": "error", "error": "没有可用的账号"}) == ("任务出错了", "没有可用的账号")
    assert describe({"status": "cancelled"}) == ("任务已停止", "")
    assert describe({"status": "done", "kind": "sync", "result": {"artists": 5, "artists_ok": 4, "unchecked": 2}, "failed": 1}) == \
        ("任务完成", "检查了 4 位画师，失败 1，2 位没查到")
    assert describe({"status": "done", "kind": "sync_download", "result": {"needs_review": True, "new_files": 9}})[0] == "检查完成，等你确认"
    assert describe({"status": "done", "kind": "download_avatars", "success": 7, "result": {}}) == ("任务完成", "下载了 7 个头像")


def test_real_command_and_exit_actions(tmp_path):
    marker = tmp_path / "ran.txt"
    command = f'"{sys.executable}" -c "open(r\'{marker}\', \'w\').write(\'ok\')"'
    assert afterjob.run_action("command", command) == "已运行命令"
    for _ in range(100):
        if marker.exists():
            break
        time.sleep(0.05)
    assert marker.read_text() == "ok"
    assert afterjob.run_action("command", "   ") == "没有填要运行的命令"
    quits = []
    assert afterjob.run_action("exit", quit_app=lambda: quits.append(1)) == "已退出" and quits == [1]


def test_job_that_finished_before_the_watch_started_still_counts(api):
    api.finish(success=1, result={"tasks": 1}, finished=time.time())     # 很短的任务：界面还没来得及调用就做完了
    assert api.job_watch({"action": "exit"})["state"] in ("watching", "countdown")
    wait_state(api, {"idle"})
    time.sleep(0.1)
    assert api.done == [("exit", "")]
    assert api.job_watch({"action": "exit"})["state"] == "idle"          # 同一个任务不会处理第二次
    api.job = dict(api.job, id="old", finished=time.time() - 300)
    assert api.job_watch({"action": "exit"})["state"] == "idle"          # 很久以前结束的任务不算


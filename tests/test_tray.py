"""托盘：鼠标停在图标上显示的任务状态，以及右键菜单里该有哪些项。不创建真的托盘图标。"""
import pytest

from utils import lang
from webapp import tray
from webapp.tray import Tray, describe_job, tooltip

RUNNING = {"kind": "download", "status": "running", "running": True, "phase": "下载", "done": 1234, "total": 5000, "failed": 3, "transferred": 0}


@pytest.fixture(autouse=True)
def chinese():
    lang.set_lang("zh")
    yield
    lang.set_lang("zh")


def test_status_line_says_what_is_going_on():
    assert describe_job(None) == {"state": "idle", "line": "没有任务在运行", "detail": ""}
    assert describe_job({"status": "done", "running": False})["state"] == "idle"
    st = describe_job(RUNNING, speed=6.2 * 1048576)
    assert st == {"state": "running", "line": "下载中 1,234 / 5,000 (24%)", "detail": "6.2 MB/s · 失败 3"}
    assert describe_job({**RUNNING, "paused": True}, speed=9e6) == {"state": "paused", "line": "已暂停 1,234 / 5,000 (24%)", "detail": "失败 3"}
    checking = describe_job({"kind": "sync_download", "status": "running", "phase": "同步", "done": 40, "total": 560})
    assert checking["line"] == "检查中 40 / 560 (7%)"
    stuck = describe_job({**RUNNING, "failed": 0, "idle": 400})
    assert stuck["detail"] == "6 分钟没有进展"


def test_tooltip_fits_the_system_limit():
    tip = tooltip("Pixiv Viewer", describe_job(RUNNING, speed=6.2 * 1048576))
    assert tip == "Pixiv Viewer\n下载中 1,234 / 5,000 (24%)\n6.2 MB/s · 失败 3"
    long = tooltip("Pixiv Viewer", {"state": "running", "line": "x" * 80, "detail": "y" * 80})
    assert len(long) == tray.TIP_MAX                                     # 超过系统的上限会报错，所以截断


def test_menu_follows_the_job_state():
    job = {"v": None}
    done = []
    actions = {k: (lambda k=k: done.append(k)) for k in ("pause", "resume", "stop", "downloader", "settings")}
    t = Tray(None, "Pixiv Viewer", "", lambda: None, job=lambda: job["v"], actions=actions)

    def shown():
        t.refresh()
        return [name for name, text, visible, enabled in t.menu_state() if visible]

    assert shown() == ["open", "status", "downloader", "settings", "quit"]            # 空闲：没有暂停 / 停止
    job["v"] = dict(RUNNING)
    assert shown() == ["open", "status", "pause", "stop", "downloader", "settings", "quit"]
    status = next(x for x in t.menu_state() if x[0] == "status")
    assert status[1].startswith("下载中 1,234 / 5,000") and status[3] is False       # 状态那一行只是看的，点不了
    job["v"] = {**RUNNING, "paused": True}
    assert shown() == ["open", "status", "resume", "stop", "downloader", "settings", "quit"]
    # 没有下载功能的程序：只有“打开”和“退出”
    plain = Tray(None, "X", "", lambda: None)
    assert [n for n, _, visible, _ in plain.menu_state() if visible] == ["open", "quit"]


def test_speed_comes_from_bytes_received_between_looks():
    job = {"v": {**RUNNING, "failed": 0, "transferred": 10 * 1048576}}
    t = Tray(None, "Pixiv Viewer", "", lambda: None, job=lambda: job["v"])
    assert t.refresh(now=100.0)["detail"] == ""                          # 第一次看：还算不出速度
    job["v"] = {**job["v"], "transferred": 30 * 1048576}
    assert t.refresh(now=102.0)["detail"] == "10.0 MB/s"                 # 2 秒收到 20 MB
    assert "10.0 MB/s" in t.tip
    job["v"] = None
    assert t.refresh(now=104.0)["state"] == "idle" and t.tip == "Pixiv Viewer\n没有任务在运行"


def test_a_failing_status_source_does_not_break_the_tray():
    def boom():
        raise RuntimeError("下载进程没了")
    t = Tray(None, "Pixiv Viewer", "", lambda: None, job=boom)
    assert t.refresh()["state"] == "idle"


def test_menu_text_follows_the_interface_language():
    t = Tray(None, "Pixiv Viewer", "", lambda: None, job=lambda: dict(RUNNING), actions={"pause": None, "stop": None, "downloader": None, "settings": None})
    lang.set_lang("en")
    t.refresh()
    texts = {n: text for n, text, visible, _ in t.menu_state() if visible}
    assert texts["open"] == "Open Pixiv Viewer" and texts["pause"] == "Pause job" and texts["quit"] == "Quit"
    assert texts["status"].startswith("Downloading 1,234 / 5,000")
    lang.set_lang("ja")
    t.refresh()
    texts = {n: text for n, text, visible, _ in t.menu_state() if visible}
    assert texts["pause"] == "タスクを一時停止" and texts["status"].startswith("ダウンロード中")

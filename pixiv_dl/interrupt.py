from threading import Event

# 全局中断事件，按下 Ctrl+C / 点“停止”时由主流程设置
stop_event = Event()
# 暂停：设置后，各工作线程在开始下一件事之前停下来等，直到继续或停止
pause_event = Event()


def wait(seconds: float) -> bool:
    """Wait for given seconds or until stop_event is set.
    Returns True if stopped (stop_event set) during wait, False otherwise.
    """
    return stop_event.wait(seconds)


def is_set() -> bool:
    return stop_event.is_set()


def set() -> None:
    stop_event.set()


def clear() -> None:
    stop_event.clear()
    pause_event.clear()


def pause() -> None:
    pause_event.set()


def resume() -> None:
    pause_event.clear()


def is_paused() -> bool:
    return pause_event.is_set()


def wait_if_paused() -> bool:
    """暂停期间在这里等。被停止时返回 True（调用方应当收工），否则在继续之后返回 False。"""
    while pause_event.is_set():
        if stop_event.wait(0.3):
            return True
    return stop_event.is_set()

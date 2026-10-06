from threading import Event

# 全局中断事件，按下 Ctrl+C 时由主流程设置
stop_event = Event()

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

import json
import logging
import os
import threading
import time
from collections import deque
from datetime import datetime

from pixiv_dl.config import Config

_LOCK = threading.Lock()
_configured = False


class MemoryLogHandler(logging.Handler):
    """保留最近的日志，供 Web 前端「日志」页读取。"""

    def __init__(self, capacity=800):
        super().__init__(level=logging.INFO)
        self.records = deque(maxlen=capacity)
        self._seq = 0
        self._seq_lock = threading.Lock()

    def emit(self, record):
        try:
            msg = record.getMessage()
            if record.exc_info:
                msg += "\n" + (self.formatter.formatException(record.exc_info) if self.formatter else "")
            with self._seq_lock:
                self._seq += 1
                self.records.append({"id": self._seq, "t": record.created, "level": record.levelname, "msg": msg})
        except Exception:
            pass

    def since(self, last_id=0, limit=500):
        with self._seq_lock:
            items = [r for r in self.records if r["id"] > last_id]
        return items[-limit:]


MEMORY_HANDLER = MemoryLogHandler()


class _JSONFormatter(logging.Formatter):
    def format(self, record):
        rec = {"ts": datetime.fromtimestamp(record.created).strftime("%Y-%m-%dT%H:%M:%S"),
               "level": record.levelname, "msg": record.getMessage()}
        if record.exc_info:
            rec["exc"] = self.formatException(record.exc_info)
        return json.dumps(rec, ensure_ascii=False)


def setup_logging(console=True, to_file=True):
    """配置 PixivDownloader 日志（幂等）：文件 + 控制台 + 内存环形缓冲。"""
    global _configured
    logger = logging.getLogger("PixivDownloader")
    with _LOCK:
        if _configured:
            return logger
        logger.setLevel(logging.INFO)
        json_mode = getattr(Config, 'LOG_JSON', False)
        file_fmt = _JSONFormatter() if json_mode else logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')
        con_fmt = _JSONFormatter() if json_mode else logging.Formatter('%(message)s')
        if to_file:
            os.makedirs(Config.LOG_DIR, exist_ok=True)
            fh = logging.FileHandler(os.path.join(Config.LOG_DIR, f"{time.strftime('%Y%m%d_%H%M%S')}.log"),
                                     encoding="utf-8", delay=True)  # 有日志才创建文件，不再产生一堆空文件
            fh.setFormatter(file_fmt)
            logger.addHandler(fh)
        if console:
            sh = logging.StreamHandler()
            sh.setFormatter(con_fmt)
            logger.addHandler(sh)
        MEMORY_HANDLER.setFormatter(logging.Formatter())
        logger.addHandler(MEMORY_HANDLER)
        _configured = True
    return logger

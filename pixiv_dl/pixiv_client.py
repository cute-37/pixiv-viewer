import logging
import re
import threading
import time

from pixiv_dl.config import Config
from pixiv_dl import interrupt

logger = logging.getLogger("PixivDownloader")

# 错误类别
NOT_FOUND = "not_found"      # 作品/用户不存在、已删除、已设为不可见 —— 重试没有意义
RATE_LIMIT = "rate_limit"    # 触发风控限速 —— 需要等待
AUTH = "auth"                # access token 失效 —— 重新认证后重试
OTHER = "other"              # 网络波动等 —— 退避重试
INTERRUPTED = "interrupted"  # 用户中断


class ApiError:
    __slots__ = ("kind", "message")

    def __init__(self, kind, message=""):
        self.kind = kind
        self.message = message

    def __str__(self):
        return f"[{self.kind}] {self.message}"

    def __repr__(self):
        return f"ApiError({self.kind!r}, {self.message!r})"


_NOT_FOUND_HINTS = ("not found", "deleted", "does not exist", "no such", "doesn't exist",
                    "limited who can view", "has been removed", "削除", "存在しない", "非公開", "公開されていません")
_AUTH_HINTS = ("oauth", "access token", "invalid_grant", "invalid_token", "unauthorized")
_NOT_FOUND_RE = re.compile(r"\b404\b")
_AUTH_RE = re.compile(r"\b401\b")


def classify_error(res):
    """把 pixivpy 返回的 {"error": {...}} 归类。"""
    err = res.get("error") if isinstance(res, dict) else None
    if isinstance(err, dict):
        text = " ".join(str(err.get(k) or "") for k in ("message", "user_message", "reason"))
    else:
        text = str(err if err is not None else "empty response")
    low = text.lower()
    if "rate limit" in low:
        return ApiError(RATE_LIMIT, text.strip())
    if any(h in low for h in _AUTH_HINTS) or _AUTH_RE.search(low):
        return ApiError(AUTH, text.strip())
    if any(h in low for h in _NOT_FOUND_HINTS) or _NOT_FOUND_RE.search(low):
        return ApiError(NOT_FOUND, text.strip())
    return ApiError(OTHER, text.strip() or "unknown api error")


class PixivClient:
    """单个账号的 Pixiv API 客户端。每个账号一个实例，各自持有自己的 token 与认证状态。"""

    def __init__(self, token=None, name=None):
        from pixivpy3 import AppPixivAPI
        self.name = name or "default"
        self.token = token if token is not None else Config.get_main_token()
        self.api = AppPixivAPI(proxies=Config.PROXIES or {})
        self.user_id = None
        self.last_error = ""
        self._auth_lock = threading.Lock()
        self._auth_ts = 0.0
        self._authed = False

    # ------------------------------------------------------------------ 认证
    def auth(self, force=False):
        """认证；成功返回 api，失败返回 None（原因在 self.last_error）。线程安全。"""
        with self._auth_lock:
            if self._authed and not force:
                return self.api
            # 刚被别的线程重新认证过，直接复用
            if force and self._authed and time.time() - self._auth_ts < 3:
                return self.api
            try:
                res = self.api.auth(refresh_token=self.token)
                if res and "error" not in res:
                    self._authed = True
                    self._auth_ts = time.time()
                    self.user_id = getattr(self.api, "user_id", None)
                    self.last_error = ""
                    return self.api
                self.last_error = str(res.get("error") if isinstance(res, dict) else res)
            except Exception as e:
                self.last_error = str(e)
            self._authed = False
            logger.warning(f"账号 '{self.name}' 认证失败: {self.last_error}")
            return None

    @property
    def authed(self):
        return self._authed

    # ------------------------------------------------------------------ 调用
    def call(self, func, *args, **kwargs):
        """调用 pixivpy 方法，返回 (result, ApiError|None)。

        - NOT_FOUND 立即返回，不重试
        - AUTH 重新认证后重试
        - RATE_LIMIT 长等待后重试
        - 其他错误/异常按指数退避重试，次数 Config.MAX_RETRIES
        """
        last = None
        retries = max(1, int(getattr(Config, 'MAX_RETRIES', 3)))
        for attempt in range(retries):
            if interrupt.is_set():
                return None, ApiError(INTERRUPTED, "interrupted")
            try:
                res = func(*args, **kwargs)
            except Exception as e:
                last = ApiError(OTHER, str(e))
                if interrupt.wait(min(5 * (attempt + 1), 30)):
                    return None, ApiError(INTERRUPTED, "interrupted")
                continue
            if res and "error" not in res:
                return res, None
            last = classify_error(res)
            if last.kind == NOT_FOUND:
                return None, last
            if last.kind == AUTH:
                if self.auth(force=True) is None:
                    return None, last
                continue
            wait = 30 * (attempt + 1) if last.kind == RATE_LIMIT else 2 * (attempt + 1)
            if interrupt.wait(wait):
                return None, ApiError(INTERRUPTED, "interrupted")
        return None, last

    def wrap_with_error(self, func, *args, **kwargs):
        """兼容旧接口：返回 (result, 错误文本|None)。"""
        res, err = self.call(func, *args, **kwargs)
        return res, (err.message or err.kind) if err else None

    def wrap(self, func, *args, **kwargs):
        res, _ = self.call(func, *args, **kwargs)
        return res

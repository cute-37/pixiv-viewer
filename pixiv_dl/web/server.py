"""本地 Web 前端的后端：标准库 http.server，无额外依赖。

安全边界：
- 默认只监听 127.0.0.1；
- 校验 Host 头（防 DNS rebinding），非 GET 请求必须带自定义头 X-Pixiv-UI（跨站请求带不了，也不处理预检）；
- 任何接口都不会返回 token / NAS 密码。
"""
import csv
import io
import json
import logging
import mimetypes
import os
import re
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from pixiv_dl import interrupt
from pixiv_dl.applog import MEMORY_HANDLER, setup_logging
from pixiv_dl.config import Config, PASSWORD_KEYS, VERSION, mask_token
from pixiv_dl.database import Database
from pixiv_dl.extract import url_ext
from pixiv_dl.processor import Processor

logger = logging.getLogger("PixivDownloader")
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
mimetypes.add_type("image/webp", ".webp")
mimetypes.add_type("text/javascript", ".js")


class HttpError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


# --------------------------------------------------------------------------- 设置校验
def _bool(v):
    if isinstance(v, bool):
        return v
    raise ValueError("需要布尔值")


def _int_range(lo, hi):
    def f(v):
        v = int(v)
        if not lo <= v <= hi:
            raise ValueError(f"需要在 {lo}~{hi} 之间")
        return v
    return f


def _float_range(lo, hi):
    def f(v):
        v = float(v)
        if not lo <= v <= hi:
            raise ValueError(f"需要在 {lo}~{hi} 之间")
        return v
    return f


def _str(v):
    if not isinstance(v, str):
        raise ValueError("需要字符串")
    return v.strip()


def _choice(*opts):
    def f(v):
        if v not in opts:
            raise ValueError(f"可选值: {opts}")
        return v
    return f


def _pair(v):
    if not (isinstance(v, (list, tuple)) and len(v) == 2):
        raise ValueError("需要 [最小, 最大]")
    a, b = float(v[0]), float(v[1])
    if not 0 <= a <= b <= 120:
        raise ValueError("需要 0 ≤ 最小 ≤ 最大 ≤ 120")
    return (a, b)


def _types(v):
    if not isinstance(v, list) or not v or any(x not in ('illust', 'manga') for x in v):
        raise ValueError("可选 illust / manga，至少一项")
    return list(dict.fromkeys(v))


def _proxy_url(v):
    from pixiv_dl import proxy
    return proxy.normalize(_str(v))


SETTINGS_SPEC = {
    'PROXY_MODE': _choice('system', 'none', 'custom'), 'PROXY_URL': _proxy_url,
    'STORAGE_MODE': _choice('local', 'smb', 'webdav', 'ftp', 'sftp', 's3'), 'LOCAL_SAVE_PATH': _str,
    'S3_ENDPOINT': _str, 'S3_REGION': _str, 'S3_BUCKET': _str, 'S3_PREFIX': _str, 'S3_ACCESS_KEY': _str,
    'S3_SECRET_KEY': _str, 'S3_PATH_STYLE': _bool, 'S3_VERIFY_TLS': _bool,
    'WEBDAV_URL': _str, 'WEBDAV_USER': _str, 'WEBDAV_PASS': _str, 'WEBDAV_VERIFY_TLS': _bool,
    'FTP_URL': _str, 'FTP_USER': _str, 'FTP_PASS': _str,
    'SFTP_URL': _str, 'SFTP_USER': _str, 'SFTP_PASS': _str, 'SFTP_KEY_FILE': _str,
    'NAS_IP': _str, 'NAS_USER': _str, 'NAS_SHARE': _str, 'NAS_BASE_PATH': _str, 'NAS_REMOTE_NAME': _str,
    'NAS_PASS': _str,
    'MAIN_ACCOUNT_SYNC_THREADS': _int_range(1, 8), 'BACKUP_ACCOUNT_SYNC_THREADS': _int_range(1, 8),
    'MAIN_ACCOUNT_DOWNLOAD_THREADS': _int_range(1, 8), 'BACKUP_ACCOUNT_DOWNLOAD_THREADS': _int_range(1, 8),
    'METADATA_REFRESH_LIMIT': _int_range(0, 1000), 'FAILURE_RATE_THRESHOLD': _float_range(0.1, 0.9),
    'REVIEW_THRESHOLD': _int_range(0, 1000000), 'KEEP_AWAKE': _bool,
    'REST_EVERY': _int_range(10, 1000000), 'REST_SECONDS': _int_range(1, 3600),
    'RATE_LIMIT_ENABLED': _bool, 'SYNC_NOVELS': _bool, 'UGOIRA_PREFER_HQ': _bool, 'UGOIRA_WEBP_LOSSLESS': _bool,
    'SYNC_TYPES': _types, 'DELAY_SYNC': _pair, 'DELAY_DOWNLOAD': _pair, 'MAX_RETRIES': _int_range(1, 10),
    'DB_AUTO_BACKUP_DAYS': _int_range(0, 365), 'DB_BACKUP_KEEP': _int_range(1, 50), 'DB_JOURNAL': _choice('delete', 'wal'),
}


# --------------------------------------------------------------------------- 后台任务
class JobRunner:
    """同一时间只运行一个后台任务（同步/下载/核查...）。"""

    def __init__(self, processor):
        self.pro = processor
        self._lock = threading.Lock()
        self._thread = None

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def _build(self, kind, p):
        pro = self.pro

        def aid():
            try:
                return int(p['author_id'])
            except (KeyError, TypeError, ValueError):
                raise HttpError(400, "缺少 author_id")

        def ids():
            raw = p.get('author_ids')
            if not isinstance(raw, list) or not raw:
                raise HttpError(400, "请先选择画师")
            try:
                return [int(x) for x in raw][:500]
            except (TypeError, ValueError):
                raise HttpError(400, "author_ids 必须是数字列表")

        def limit():
            v = p.get('limit')
            return int(v) if str(v or '').isdigit() and int(v) > 0 else None

        def opt_ids(key='author_ids'):
            raw = p.get(key)
            if not raw:
                return None
            if not isinstance(raw, list):
                raise HttpError(400, f"{key} 必须是列表")
            try:
                return [int(x) for x in raw][:5000]
            except (TypeError, ValueError):
                raise HttpError(400, f"{key} 必须是数字列表")

        def names(key):
            raw = p.get(key)
            if not raw:
                return None
            if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
                raise HttpError(400, f"{key} 必须是字符串列表")
            return raw

        def sync_options():
            scope = p.get('scope') or 'all'
            if scope not in ('all', 'failed', 'never', 'stale', 'gone'):
                raise HttpError(400, "不认识的检查范围")
            out = {'scope': scope}
            if scope == 'stale':
                try:
                    out['stale_days'] = max(0.0, float(p.get('stale_days') or 7))
                except (TypeError, ValueError):
                    raise HttpError(400, "stale_days 必须是数字")
            if p.get('resume_since'):
                out['resume_since'] = str(p['resume_since'])[:19]
            if p.get('backfill'):
                out['backfill'] = [t for t in (names('backfill') or []) if t in ('illust', 'manga')]
            return out

        def filters():
            f = {}
            types = [t for t in (names('types') or []) if t in ('illust', 'manga', 'ugoira', 'novel')]
            if types and len(types) < 4:
                f['types'] = types
            for key in ('date_from', 'date_to'):
                v = str(p.get(key) or '').strip()
                if v:
                    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
                        raise HttpError(400, "日期要写成 2026-01-31 这样")
                    f[key] = v
            if p.get('exclude_r18'):
                f['exclude_r18'] = True
            if p.get('origin') in ('new', 'old'):
                f['origin'] = p['origin']
            ex = opt_ids('exclude_author_ids')
            if ex:
                f['exclude_author_ids'] = ex
            per = p.get('max_per_artist')
            if str(per or '').isdigit() and int(per) > 0:
                f['max_per_artist'] = int(per)
            return f or None

        def review_over():
            if p.get('review') == 'never':
                return None
            if p.get('review') == 'always':
                return -1                     # 任何数量都先看
            return int(getattr(Config, 'REVIEW_THRESHOLD', 0) or 0) or None

        def retry_args():
            keys, kinds = p.get('keys'), p.get('kinds')
            if not keys and not kinds and not p.get('author_id'):
                raise HttpError(400, "请选择要重试的文件")
            aid_ = p.get('author_id')
            return {'keys': keys or None, 'kinds': kinds or None,
                    'author_id': int(aid_) if str(aid_ or '').isdigit() else None}

        table = {
            'sync': lambda: pro.sync(deep=bool(p.get('deep')), aids=opt_ids(), accounts=names('accounts'), **sync_options()),
            'sync_download': lambda: pro.sync_and_download(
                deep=bool(p.get('deep')), limit=limit(), aids=opt_ids(), sync_options=sync_options(), filters=filters(),
                accounts=names('accounts'), review_over=review_over()),
            'download': lambda: pro.download(limit=limit(), aids=opt_ids(), filters=filters(), accounts=names('accounts')),
            'retry_now': lambda: pro.retry_now(**retry_args()),
            'recheck_gone': lambda: pro.sync(scope='gone'),
            'sync_artist': lambda: pro.sync(aid=aid(), deep=True),
            'download_artist': lambda: pro.download(aid=aid()),
            'sync_download_artist': lambda: pro.sync_and_download_artist(aid()),
            'sync_artists': lambda: pro.sync(aids=ids(), deep=bool(p.get('deep'))),
            'download_artists': lambda: pro.download(aids=ids()),
            'sync_download_artists': lambda: pro.sync_and_download_artists(ids()),
            'verify': lambda: pro.verify_storage(apply=bool(p.get('apply'))),
            'fill_sizes': lambda: pro.verify_storage(apply=False, sizes=True),
            'refresh_profiles': lambda: pro.refresh_all_artist_profiles(only_missing=not p.get('all'), limit=limit()),
            'download_avatars': lambda: pro.download_missing_avatars(limit=limit(), force=bool(p.get('all'))),
            'db_vacuum': lambda: pro.db_vacuum(),
        }
        if kind not in table:
            raise HttpError(400, f"未知任务类型: {kind}")
        if kind.endswith('_artist'):
            aid()  # 提前校验参数
        if kind.endswith('_artists'):
            ids()
        if kind in ('sync', 'sync_download', 'download'):      # 提前校验参数，错了直接告诉界面，而不是等任务跑起来才失败
            opt_ids(), names('accounts'), sync_options(), filters()
        if kind == 'retry_now':
            retry_args()
        return table[kind]

    def start(self, kind, params):
        with self._lock:
            if self.running:
                raise HttpError(409, "已有任务在运行，请先等待完成或点击「停止」")
            fn = self._build(kind, params or {})
            self._thread = threading.Thread(target=self._run, args=(fn, kind), name=f"job-{kind}", daemon=True)
            self._thread.start()

    @staticmethod
    def _run(fn, kind):
        try:
            fn()
        except Exception as e:
            logger.error(f"任务 {kind} 失败: {type(e).__name__}: {e}")

    def stop(self):
        if self.running:
            interrupt.set()
            self.pro.job.set(stopping=True, message="正在停止，等待当前文件处理完…")
            return True
        return False

    def pause(self):
        """暂停：正在处理的文件做完后，各线程停下来等，进度保留；继续时接着做。"""
        if self.running and not interrupt.is_paused():
            interrupt.pause()
            self.pro.job.log("已暂停（正在处理的文件做完后停下）")
            self.pro.job.set(message="已暂停")
            return True
        return False

    def resume(self):
        if self.running and interrupt.is_paused():
            interrupt.resume()
            self.pro.job.log("继续")
            self.pro.job.set(message="")
            return True
        return False


# --------------------------------------------------------------------------- 应用
class App:
    def __init__(self, processor=None):
        self.pro = processor or Processor()
        self.runner = JobRunner(self.pro)
        self._storage = None
        self._storage_lock = threading.Lock()
        self._summary = (0.0, None)
        self._summary_lock = threading.Lock()
        self._plan_cache = (0.0, None)
        self._plan_lock = threading.Lock()
        self.oauth = {}
        self._thumb_sem = threading.Semaphore(3)

    @property
    def db(self):
        return Database.local(Config.DB_PATH)

    def storage(self):
        """前端专用的存储连接（与下载任务的连接分开，互不阻塞）。"""
        from pixiv_dl.storage.adapter import StorageAdapter
        with self._storage_lock:
            if self._storage is None or self._storage.mode != Config.STORAGE_MODE:
                try:
                    self._storage = StorageAdapter()
                except Exception as e:
                    raise HttpError(503, f"存储不可用: {e}")
            return self._storage

    def reset_storage(self):
        with self._storage_lock:
            self._storage = None
        self.pro.reset_storage()

    def artist_summaries(self):
        """画师统计较重（扫描任务表），缓存几秒。任务运行时缓存更短。"""
        ttl = 3 if self.runner.running else 15
        with self._summary_lock:
            ts, data = self._summary
            if data is None or time.time() - ts > ttl:
                data = self.db.get_artist_summaries()
                self._summary = (time.time(), data)
            return data

    def invalidate(self):
        with self._summary_lock:
            self._summary = (0.0, None)
        with self._plan_lock:
            self._plan_cache = (0.0, None)


# --------------------------------------------------------------------------- 路由
ROUTES = []


def route(method, pattern):
    rx = re.compile("^" + pattern + "$")

    def deco(fn):
        ROUTES.append((method, rx, fn))
        return fn
    return deco


class Request:
    def __init__(self, app, match, query, body):
        self.app = app
        self.m = match
        self.q = query
        self.body = body or {}

    def qs(self, key, default=None):
        v = self.q.get(key)
        return v[0] if v else default

    def qint(self, key, default, lo=None, hi=None):
        try:
            v = int(self.qs(key, default))
        except (TypeError, ValueError):
            v = default
        if lo is not None:
            v = max(lo, v)
        if hi is not None:
            v = min(hi, v)
        return v


class Raw:
    """非 JSON 响应（图片/CSV）。"""

    def __init__(self, data, content_type, filename=None, cache=None):
        self.data, self.content_type, self.filename, self.cache = data, content_type, filename, cache


def _json_default(o):
    if isinstance(o, (set, tuple)):
        return list(o)
    return str(o)


@route("GET", r"/api/overview")
def api_overview(r):
    app = r.app
    db = app.db
    accounts = Config.get_accounts()
    return {
        "version": VERSION,
        "stats": db.stats(),
        "job": app.pro.job.snapshot(with_logs=False),
        "running": app.runner.running,
        "accounts": {"total": len(accounts),
                     "valid": sum(1 for a in accounts.values() if a.get("is_valid", True)),
                     "main": Config.MAIN_ACCOUNT},
        "storage_mode": Config.STORAGE_MODE,
        "storage_target": Config.storage_target(),
        "config": {"sync_types": Config.SYNC_TYPES, "sync_novels": Config.SYNC_NOVELS},
    }


@route("GET", r"/api/storage/status")
def api_storage_status(r):
    try:
        ok, msg = r.app.storage().ping()
    except HttpError as e:
        ok, msg = False, e.message
    return {"ok": ok, "message": msg, "mode": Config.STORAGE_MODE}


@route("GET", r"/api/job")
def api_job(r):
    snap = r.app.pro.job.snapshot()
    snap["running"] = r.app.runner.running
    return snap


@route("POST", r"/api/job")
def api_job_start(r):
    kind = r.body.get("kind")
    r.app.runner.start(kind, r.body)
    r.app.invalidate()
    return {"ok": True}


@route("POST", r"/api/job/stop")
def api_job_stop(r):
    return {"ok": True, "stopped": r.app.runner.stop()}


@route("POST", r"/api/job/pause")
def api_job_pause(r):
    return {"ok": True, "paused": r.app.runner.pause()}


@route("POST", r"/api/job/resume")
def api_job_resume(r):
    return {"ok": True, "resumed": r.app.runner.resume()}


# ---- 待下载：先看再下 / 不下载
def _pending_filters(body):
    f = body.get("filters") if isinstance(body.get("filters"), dict) else {}
    out = {}
    for key in ("author_ids", "exclude_author_ids"):
        if f.get(key):
            try:
                out[key] = [int(x) for x in f[key]][:5000]
            except (TypeError, ValueError):
                raise HttpError(400, f"{key} 必须是数字列表")
    types = [t for t in (f.get("types") or []) if t in ("illust", "manga", "ugoira", "novel")]
    if types:
        out["types"] = types
    for key in ("date_from", "date_to"):
        if f.get(key):
            out[key] = str(f[key])[:10]
    if f.get("exclude_r18"):
        out["exclude_r18"] = True
    if f.get("origin") in ("new", "old"):
        out["origin"] = f["origin"]
    return out


@route("POST", r"/api/pending/summary")
def api_pending_summary(r):
    """待下载的东西按画师汇总（文件数、作品数、类型、估算大小），可以带筛选条件。skipped=true 看“不下载”的那些。"""
    status = (-3,) if r.body.get("skipped") else (0, -1)
    data = r.app.db.pending_summary(_pending_filters(r.body), Config.MAX_ATTEMPTS if not r.body.get("skipped") else None, status)
    data["skipped_total"] = r.app.db.stats()["skipped"]
    return data


@route("POST", r"/api/avatars/check")
def api_avatars_check(r):
    """核对头像：头像文件夹里实际有哪些、还缺谁的，并把数据库里的记录改成和实际一致。不访问 Pixiv。"""
    if r.body.get("dry"):                       # 只看不改：任务进行中也可以
        return r.app.pro.check_avatars(fix=False)
    if r.app.runner.running:
        raise HttpError(409, "任务运行中，结束后再核对")
    return r.app.pro.check_avatars()


@route("POST", r"/api/pending/skip")
def api_pending_skip(r):
    """把符合条件的待下载标成“不下载”（restore=true 是恢复）。必须给出条件，不能一次全标。"""
    f = _pending_filters(r.body)
    restore = bool(r.body.get("restore"))
    if not f and not (restore and r.body.get("all")):
        raise HttpError(400, "请先选择画师或条件")
    if r.app.runner.running:
        raise HttpError(409, "任务运行中，先暂停或等它结束再调整")
    n = r.app.db.skip_tasks(f, restore=restore)
    r.app.invalidate()
    return {"ok": True, "count": n}


# ---- 检查失败的画师
_SYNC_FAIL_ORDER = ("rate_limit", "network", "auth", "other", "gone")


@route("GET", r"/api/sync/failures")
def api_sync_failures(r):
    db = r.app.db
    groups = {}
    for item in db.sync_failures():
        kind = item["kind"] if item["kind"] in _SYNC_FAIL_ORDER else "other"
        groups.setdefault(kind, []).append(item)
    return {"groups": [{"kind": k, "count": len(groups[k]), "items": groups[k]} for k in _SYNC_FAIL_ORDER if k in groups],
            "total": sum(len(v) for v in groups.values()),
            "skipped": db.sync_skipped()}


@route("POST", r"/api/sync/skip")
def api_sync_skip(r):
    """“不再检查”这些画师（restore=true 恢复）。只是以后检查时跳过，不删任何东西。"""
    raw = r.body.get("author_ids")
    if not isinstance(raw, list) or not raw:
        raise HttpError(400, "请先选择画师")
    try:
        ids = [int(x) for x in raw]
    except (TypeError, ValueError):
        raise HttpError(400, "author_ids 必须是数字列表")
    n = r.app.db.set_sync_skip(ids, skip=not r.body.get("restore"))
    r.app.invalidate()
    return {"ok": True, "count": n}


@route("GET", r"/api/logs")
def api_logs(r):
    return {"items": MEMORY_HANDLER.since(r.qint("since", 0, lo=0))}


# ---- 画师
@route("GET", r"/api/artists")
def api_artists(r):
    items = r.app.artist_summaries()
    q = (r.qs("q") or "").strip().lower()
    flt = r.qs("filter", "all")
    if q:
        items = [a for a in items if q in str(a["author_id"]) or q in (a["author_name"] or "").lower()
                 or q in (a["account"] or "").lower()]
    pred = {
        "followed": lambda a: a["is_followed"], "deleted": lambda a: a["is_deleted"],
        "pending": lambda a: a["pending"] > 0, "failed": lambda a: a["failed"] > 0,
        "nosync": lambda a: not a["last_sync_time"], "private": lambda a: a["is_private_follow"],
        "pinned": lambda a: a["pinned"], "recent": lambda a: a["recent"] > 0,
    }.get(flt)
    if pred:
        items = [a for a in items if pred(a)]
    key = r.qs("sort", "name")
    keyfn = {
        "name": lambda a: (a["author_name"] or "").lower(), "id": lambda a: a["author_id"],
        "done": lambda a: a["done"], "pending": lambda a: a["pending"] + a["failed"],
        "tasks": lambda a: a["tasks"], "synced": lambda a: a["last_sync_time"] or "",
    }.get(key, lambda a: (a["author_name"] or "").lower())
    items = sorted(items, key=keyfn, reverse=(r.qs("order") == "desc"))
    items.sort(key=lambda a: -a["pinned"])           # 置顶的画师始终排在前面（稳定排序，保留上面的次序）
    if r.qs("ids"):
        return {"ids": [a["author_id"] for a in items][:2000]}
    per = r.qint("per", 30, 1, 200)
    page = r.qint("page", 1, 1)
    total = len(items)
    return {"items": items[(page - 1) * per: page * per], "total": total, "page": page, "per": per}


@route("GET", r"/api/artists/(?P<aid>\d+)")
def api_artist(r):
    aid = int(r.m["aid"])
    a = r.app.db.get_artist_full(aid)
    if not a:
        raise HttpError(404, "没有这个画师")
    counts = next((s for s in r.app.artist_summaries() if s["author_id"] == aid), {})
    a["counts"] = {k: counts.get(k, 0) for k in ("tasks", "done", "failed", "pending")}
    a["recent"] = counts.get("recent", 0)
    a["label"] = r.app.db.get_label(aid)
    return a


@route("GET", r"/api/artists/(?P<aid>\d+)/works")
def api_artist_works(r):
    aid = int(r.m["aid"])
    per = r.qint("per", 24, 1, 100)
    page = r.qint("page", 1, 1)
    items, total = r.app.db.list_works(aid, r.qs("q"), per, (page - 1) * per)
    return {"items": items, "total": total, "page": page, "per": per}


@route("GET", r"/api/works/(?P<iid>\d+)/pages")
def api_work_pages(r):
    rows = r.app.db.conn.execute(
        "SELECT task_key, page_index, media_type, status FROM illusts WHERE illust_id = ? AND media_type != 'novel' "
        "ORDER BY page_index", (int(r.m["iid"]),)).fetchall()
    return {"items": [{"task_key": k, "page": p, "media_type": mt, "status": s} for k, p, mt, s in rows]}


@route("POST", r"/api/artists/(?P<aid>\d+)/action")
def api_artist_action(r):
    aid = int(r.m["aid"])
    action = r.body.get("action")
    db = r.app.db
    if action in ("sync", "download", "sync_download"):
        r.app.runner.start({"sync": "sync_artist", "download": "download_artist",
                            "sync_download": "sync_download_artist"}[action], {"author_id": aid})
        return {"ok": True}
    if action == "refresh":
        ok = r.app.pro.refresh_artist_profile(aid)
        r.app.invalidate()
        return {"ok": bool(ok)}
    if action == "retry_failed":
        n = db.reset_failed_tasks_by_filter(aid)
        r.app.invalidate()
        return {"ok": True, "count": n}
    if action == "clear_deleted":
        db.upsert_artist(aid, None, is_deleted=0)
        r.app.invalidate()
        return {"ok": True}
    raise HttpError(400, f"未知操作: {action}")


@route("POST", r"/api/artists/clear-deleted")
def api_clear_deleted(r):
    """把所有「已注销」标记清掉，让下次同步重新判断（此前的版本会把同步失败误标为注销）。"""
    db = r.app.db
    with db.tx() as c:
        n = c.execute("UPDATE artists SET is_deleted = 0 WHERE COALESCE(is_deleted,0) = 1").rowcount
    r.app.invalidate()
    return {"ok": True, "count": n}


@route("POST", r"/api/illust")
def api_add_illust(r):
    iid = str(r.body.get("illust_id") or "").strip()
    if not iid.isdigit():
        raise HttpError(400, "请输入数字作品 ID")
    try:
        n = r.app.pro.add_illust(int(iid))
    except RuntimeError as e:
        raise HttpError(400, str(e))
    r.app.invalidate()
    return {"ok": True, "tasks": n}


# ---- 任务
@route("GET", r"/api/tasks")
def api_tasks(r):
    status = r.qs("status")
    st = {"pending": 0, "done": 1, "running": 2, "failed": -1, "ignored": -2, "skipped": -3}.get(status)
    per = r.qint("per", 50, 1, 200)
    page = r.qint("page", 1, 1)
    author = r.qs("author")
    items, total = r.app.db.list_tasks(st, r.qs("q"), int(author) if author and author.isdigit() else None,
                                       per, (page - 1) * per, kind=r.qs("kind"))
    return {"items": items, "total": total, "page": page, "per": per}


@route("POST", r"/api/tasks/retry-failed")
def api_retry_failed(r):
    aid = r.body.get("author_id")
    n = r.app.db.reset_failed_tasks_by_filter(int(aid) if str(aid or "").isdigit() else None)
    r.app.invalidate()
    return {"ok": True, "count": n}


@route("POST", r"/api/tasks/reclaim")
def api_reclaim(r):
    return {"ok": True, **r.app.pro.reclaim_stuck_tasks()}


@route("POST", r"/api/tasks/reset-running")
def api_reset_running(r):
    return {"ok": True, "count": r.app.db.reset_in_progress()}


@route("GET", r"/api/tasks/export.csv")
def api_export(r):
    rows = r.app.db.get_failed_tasks()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(['task_key', 'illust_id', 'page_index', 'author_id', 'title', 'url', 'media_type',
                'create_date', 'tags', 'page_count', 'attempts', 'updated_at'])
    w.writerows(rows)
    return Raw(("﻿" + buf.getvalue()).encode("utf-8"), "text/csv; charset=utf-8", "failed_tasks.csv")


@route("GET", r"/api/alerts")
def api_alerts(r):
    return {"items": r.app.db.recent_alerts(r.qint("limit", 50, 1, 200))}


@route("POST", r"/api/maintenance/clean-temp")
def api_clean_temp(r):
    return {"ok": True, "removed": r.app.pro.clean_temp(r.body.get("days"))}


# ---- 下载计划 / 运行历史 / 通知
@route("GET", r"/api/plan")
def api_plan(r):
    """「检查更新并下载」的预览数据：上次同步、待下载数量与预估大小、失败情况。"""
    app = r.app
    with app._plan_lock:
        ts, cached = app._plan_cache
        if cached is None or time.time() - ts > 20:
            cached = app.db.plan_numbers(Config.MAX_ATTEMPTS)
            app._plan_cache = (time.time(), cached)
    plan = dict(cached)
    accounts = Config.get_accounts()
    plan["accounts_valid"] = sum(1 for a in accounts.values() if a.get("is_valid", True))
    plan["main_account"] = Config.MAIN_ACCOUNT
    plan["failed_total"] = app.db.stats()["failed"]
    last = app.db.last_run(["sync", "sync_download", "sync_artists", "sync_download_artists"])
    plan["last_sync_run"] = ({"id": last["id"], "status": last["status"], "finished": last["finished"]}
                             if last else None)
    plan["running"] = app.runner.running
    stats = app.db.stats()
    plan["skipped_total"] = stats["skipped"]
    plan["review_threshold"] = int(getattr(Config, "REVIEW_THRESHOLD", 0) or 0)
    plan["sync_failed"] = len([x for x in app.db.sync_failures() if not x["gone"]])
    plan["resume"] = _resume_info(app.db)
    return plan


def _resume_info(db):
    """上一次“检查全部”要是没查完（被停止、或者账号被限速太久），告诉界面还能接着查：{since, remaining}"""
    last = db.last_run(["sync", "sync_download"])
    if not last:
        return None
    run = db.get_run(last["id"]) or {}
    params = run.get("params") or {}
    result = (run.get("summary") or {}).get("result") or {}
    scope = params.get("scope") or (params.get("sync_options") or {}).get("scope")
    if scope not in (None, "all") or params.get("aid") or params.get("aids"):
        return None
    if last["status"] not in ("cancelled", "error") and not result.get("unchecked"):
        return None
    since = datetime.fromtimestamp(last["started"]).strftime("%Y-%m-%d %H:%M:%S")
    remaining = sum(1 for s in db.artist_sync_states().values()
                    if s["followed"] and not s["gone"] and not s["skip"] and (s["last"] or "") < since)
    return {"since": since, "remaining": remaining} if remaining else None


@route("GET", r"/api/runs")
def api_runs(r):
    return {"items": r.app.db.recent_runs(r.qint("limit", 10, 1, 50))}


@route("GET", r"/api/runs/(?P<rid>\d+)")
def api_run(r):
    run = r.app.db.get_run(int(r.m["rid"]))
    if not run:
        raise HttpError(404, "没有这条记录")
    return run


@route("GET", r"/api/notifications")
def api_notifications(r):
    """通知中心：账号失效、任务失败、告警、需要处理的失败文件。"""
    app = r.app
    db = app.db
    items = []
    accounts = Config.get_accounts()
    valid = [n for n, a in accounts.items() if a.get("is_valid", True)]
    if not accounts or not valid:
        items.append({"id": "no-account", "level": "error", "title": "还没有可用的账号",
                      "detail": "添加并验证 Pixiv 账号后才能同步和下载。",
                      "action": {"label": "去添加账号", "route": "#/accounts"}})
    else:
        if Config.MAIN_ACCOUNT and Config.MAIN_ACCOUNT not in valid:
            items.append({"id": "main-invalid", "level": "error",
                          "title": f"主账号「{Config.MAIN_ACCOUNT}」登录已失效",
                          "detail": "主账号用于获取关注列表，失效后无法同步关注的画师。",
                          "action": {"label": "去处理", "route": "#/accounts"}})
        for n, a in accounts.items():
            if not a.get("is_valid", True) and n != Config.MAIN_ACCOUNT:
                items.append({"id": f"acc-{n}", "level": "warn", "title": f"账号「{n}」登录已失效",
                              "detail": "该账号不会参与并发同步/下载。",
                              "action": {"label": "去处理", "route": "#/accounts"}})
    last = db.recent_runs(1)
    if last and last[0]["status"] == "error":
        items.append({"id": f"run-{last[0]['id']}", "level": "error", "time": last[0]["finished"],
                      "title": "上一次任务失败了", "detail": (last[0]["error"] or "")[:200],
                      "action": {"label": "查看日志", "route": "#/logs"}})
    failed = db.stats()["failed"]
    if failed:
        groups = db.failure_groups(Config.MAX_ATTEMPTS)
        retryable = sum(g["count"] for g in groups if g["kind"] != "deleted")
        items.append({"id": f"failed-{failed}", "level": "info", "title": f"有 {failed} 个文件下载失败",
                      "detail": f"其中 {retryable} 个可以重试，其余多半是作品已被删除。",
                      "action": {"label": "去处理", "route": "#/tasks?tab=failed"}})
    shown = 0
    for a in db.recent_alerts(12):
        if shown >= 4:
            break
        if a["level"] in ("ERROR", "WARN"):
            shown += 1
            items.append({"id": f"alert-{a['id']}", "level": "warn" if a["level"] == "WARN" else "error",
                          "title": a["message"][:80], "detail": a["task_key"] or "", "time": a["created_at"]})
    return {"items": items}


# ---- 失败任务
@route("GET", r"/api/failures")
def api_failures(r):
    db = r.app.db
    return {"groups": db.failure_groups(Config.MAX_ATTEMPTS), "ignored": db.stats()["ignored"]}


@route("GET", r"/api/failures/export")
def api_failures_export(r):
    """失败清单（CSV 文本）。界面拿到后让用户选地方保存。"""
    rows = r.app.db.get_failed_tasks()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(['task_key', 'illust_id', 'page_index', 'author_id', 'title', 'url', 'media_type',
                'create_date', 'tags', 'page_count', 'attempts', 'updated_at'])
    w.writerows(rows)
    return {"csv": buf.getvalue(), "count": len(rows)}


def _keys(body):
    keys = body.get("keys")
    if keys is not None and (not isinstance(keys, list) or not all(isinstance(k, str) for k in keys)):
        raise HttpError(400, "keys 必须是字符串列表")
    kinds = body.get("kinds")
    if kinds is not None and (not isinstance(kinds, list) or not all(isinstance(k, str) for k in kinds)):
        raise HttpError(400, "kinds 必须是字符串列表")
    if not keys and not kinds:
        raise HttpError(400, "请选择要处理的任务或失败原因")
    return keys, kinds


@route("POST", r"/api/tasks/retry")
def api_tasks_retry(r):
    keys, kinds = _keys(r.body)
    n = r.app.db.retry_tasks(keys=keys, kinds=kinds)
    r.app.invalidate()
    return {"ok": True, "count": n}


@route("POST", r"/api/tasks/ignore")
def api_tasks_ignore(r):
    keys, kinds = _keys(r.body)
    n = r.app.db.ignore_tasks(keys=keys, kinds=kinds, restore=bool(r.body.get("restore")))
    r.app.invalidate()
    return {"ok": True, "count": n}


# ---- 作品库
@route("GET", r"/api/library")
def api_library(r):
    per = r.qint("per", 36, 1, 100)
    page = r.qint("page", 1, 1)
    author = r.qs("author")
    items, total = r.app.db.library(
        q=r.qs("q"), kind=r.qs("kind"), r18=r.qs("r18"), ai=r.qs("ai"),
        author_id=int(author) if author and author.isdigit() else None,
        sort=r.qs("sort", "recent"), limit=per, offset=(page - 1) * per)
    return {"items": items, "total": total, "page": page, "per": per}


@route("GET", r"/api/updates")
def api_updates(r):
    return {"items": r.app.db.recent_updates(r.qint("limit", 60, 1, 200))}


@route("GET", r"/api/works/(?P<iid>\d+)")
def api_work(r):
    w = r.app.db.work_detail(int(r.m["iid"]))
    if not w:
        raise HttpError(404, "没有这个作品")
    w["pixiv_url"] = f"https://www.pixiv.net/artworks/{w['illust_id']}"
    w["path"] = None
    first = next((p for p in w["pages"] if p["status"] == 1), None)
    if first:
        try:
            rel, _ = _task_file(r.app, first["task_key"])
            if Config.STORAGE_MODE == "smb":
                w["path"] = f"\\\\{Config.NAS_IP}\\{Config.NAS_SHARE}\\{Config.NAS_BASE_PATH}\\" + rel.replace("/", "\\")
            else:
                w["path"] = os.path.join(Config.LOCAL_SAVE_PATH, *rel.split("/"))
        except HttpError:
            pass
    return w


# ---- 画师：批量 / 置顶 / 备注
@route("POST", r"/api/artists/batch")
def api_artists_batch(r):
    ids = r.body.get("ids")
    if not isinstance(ids, list) or not ids:
        raise HttpError(400, "请先选择画师")
    try:
        ids = [int(x) for x in ids][:500]
    except (TypeError, ValueError):
        raise HttpError(400, "ids 必须是数字列表")
    action = r.body.get("action")
    db = r.app.db
    count = 0
    if action in ("pin", "unpin"):
        for a in ids:
            db.set_label(a, pinned=(action == "pin"))
        count = len(ids)
    elif action == "retry_failed":
        for a in ids:
            count += db.reset_failed_tasks_by_filter(a)
    elif action == "clear_deleted":
        with db.tx() as c:
            for a in ids:
                count += c.execute("UPDATE artists SET is_deleted = 0 WHERE author_id = ? AND COALESCE(is_deleted,0) = 1",
                                   (a,)).rowcount
    else:
        raise HttpError(400, f"未知操作: {action}")
    r.app.invalidate()
    return {"ok": True, "count": count}


@route("POST", r"/api/artists/(?P<aid>\d+)/label")
def api_artist_label(r):
    aid = int(r.m["aid"])
    note = r.body.get("note")
    if note is not None and not isinstance(note, str):
        raise HttpError(400, "note 必须是字符串")
    r.app.db.set_label(aid, pinned=r.body.get("pinned"), note=note)
    r.app.invalidate()
    return {"ok": True, **r.app.db.get_label(aid)}


# ---- 数据库维护
def _db_files(app):
    from pixiv_dl import dbtools
    return dbtools.list_backups(Config.DB_PATH)


@route("GET", r"/api/db/info")
def api_db_info(r):
    from pixiv_dl import dbtools
    app = r.app
    info = dbtools.info(Config.DB_PATH)
    stats = app.db.stats()
    return {"info": info, "stats": {k: stats[k] for k in ("artists", "works", "tasks", "done", "pending", "failed", "ignored")},
            "backups": dbtools.list_backups(Config.DB_PATH), "running": app.runner.running,
            "settings": {"DB_AUTO_BACKUP_DAYS": Config.DB_AUTO_BACKUP_DAYS, "DB_BACKUP_KEEP": Config.DB_BACKUP_KEEP,
                         "DB_JOURNAL": Config.DB_JOURNAL}}


@route("POST", r"/api/db/backup")
def api_db_backup(r):
    from pixiv_dl import dbtools
    try:
        path = dbtools.create_backup(Config.DB_PATH, 'manual', conn=r.app.db.conn)
    except Exception as e:
        raise HttpError(500, f"备份失败：{e}")
    return {"ok": True, "name": os.path.basename(path), "size": os.path.getsize(path)}


@route("POST", r"/api/db/backup/delete")
def api_db_backup_delete(r):
    from pixiv_dl import dbtools
    name = r.body.get("name")
    if not isinstance(name, str):
        raise HttpError(400, "缺少备份文件名")
    try:
        removed = dbtools.delete_backup(Config.DB_PATH, name)
    except ValueError as e:
        raise HttpError(400, str(e))
    if not removed:
        raise HttpError(404, "备份文件不存在")
    return {"ok": True}


@route("POST", r"/api/db/check")
def api_db_check(r):
    from pixiv_dl import dbtools
    ok, msg = dbtools.integrity_check(Config.DB_PATH)
    return {"ok": ok, "message": msg}


@route("POST", r"/api/db/optimize")
def api_db_optimize(r):
    from pixiv_dl import dbtools
    if r.app.runner.running:
        raise HttpError(409, "任务运行中，请稍后再优化")
    dbtools.optimize(Config.DB_PATH)
    return {"ok": True}


@route("POST", r"/api/db/clear")
def api_db_clear(r):
    """清空运行历史 / 告警记录（这两类都只是日志性质的数据，不影响作品和下载状态）。"""
    what = r.body.get("what")
    table = {"runs": "runs", "alerts": "alerts"}.get(what)
    if not table:
        raise HttpError(400, "只能清空 runs 或 alerts")
    with r.app.db.tx() as c:
        n = c.execute(f"DELETE FROM {table}").rowcount
    return {"ok": True, "count": n}


# ---- 图片
def _task_file(app, key):
    """task_key → (存储相对路径, content_type)。"""
    db = app.db
    t = db.get_task(key)
    if not t:
        raise HttpError(404, "任务不存在")
    if t["status"] != 1:
        raise HttpError(404, "文件尚未下载")
    aid = t["author_id"]
    storage = app.storage()
    folder = storage.find_artist_folder(aid)
    if not folder:
        raise HttpError(404, "画师目录不存在")
    iid, idx, mt = t["illust_id"], t["page_index"], t["media_type"]
    if mt == "ugoira":
        rel = f"{folder}/{iid}_p0.webp"
    elif mt == "novel":
        rel = f"{folder}/{iid}_p0.txt"
    else:
        rel = f"{folder}/{iid}_p{idx}.{url_ext(t['url'])}"
    ctype = mimetypes.guess_type(rel)[0] or "application/octet-stream"
    return rel, ctype


@route("GET", r"/api/file/(?P<key>[\w\-]+)")
def api_file(r):
    rel, ctype = _task_file(r.app, r.m["key"])
    try:
        data = r.app.storage().read(rel)
    except HttpError:
        raise
    except Exception as e:
        raise HttpError(404, f"读取文件失败: {e}")
    return Raw(data, ctype, cache="private, max-age=3600")


THUMB_WIDTHS = (320, 480, 640, 800, 960, 1280, 1600)      # 缩略图的宽度档位：前端按「列宽 × 屏幕缩放」选最接近的一档，避免被浏览器二次缩小而发糊


def _thumb_width(raw):
    try:
        want = int(raw)
    except (TypeError, ValueError):
        want = 480
    return next((w for w in THUMB_WIDTHS if w >= want), THUMB_WIDTHS[-1])


@route("GET", r"/api/thumb/(?P<key>[\w\-]+)")
def api_thumb(r):
    key = r.m["key"]
    width = _thumb_width(r.qs("w"))
    cache_dir = os.path.join(Config.CACHE_PATH, "thumbs")
    cache_file = os.path.join(cache_dir, f"{key}_w{width}.jpg")      # 文件名带宽度：不同清晰度各自缓存
    hdr = "private, max-age=86400"
    if os.path.exists(cache_file):
        with open(cache_file, "rb") as f:
            return Raw(f.read(), "image/jpeg", cache=hdr)
    rel, ctype = _task_file(r.app, key)
    if not ctype.startswith("image/"):
        raise HttpError(404, "没有缩略图")
    with r.app._thumb_sem:
        if os.path.exists(cache_file):
            with open(cache_file, "rb") as f:
                return Raw(f.read(), "image/jpeg", cache=hdr)
        try:
            from PIL import Image
            data = r.app.storage().read(rel)
            with Image.open(io.BytesIO(data)) as im:
                im.seek(0)
                im = im.convert("RGB")
                if im.width > width:                                  # 只缩小不放大；按宽度缩放，超长图限制高度
                    im.thumbnail((width, width * 4), Image.LANCZOS)
                out = io.BytesIO()
                im.save(out, "JPEG", quality=90, optimize=True)
        except HttpError:
            raise
        except Exception as e:
            raise HttpError(404, f"生成缩略图失败: {e}")
    os.makedirs(cache_dir, exist_ok=True)
    tmp = cache_file + ".tmp"
    with open(tmp, "wb") as f:
        f.write(out.getvalue())
    os.replace(tmp, cache_file)
    return Raw(out.getvalue(), "image/jpeg", cache=hdr)


@route("GET", r"/api/avatar/(?P<aid>\d+)")
def api_avatar(r):
    a = r.app.db.get_artist_full(int(r.m["aid"]))
    local = (a or {}).get("profile_image_local")
    if local:
        path = os.path.join(Config.AVATARS_PATH, os.path.basename(local))
        if os.path.isfile(path):
            with open(path, "rb") as f:
                return Raw(f.read(), mimetypes.guess_type(path)[0] or "image/jpeg", cache="private, max-age=86400")
    raise HttpError(404, "没有头像")


# ---- 账号
@route("GET", r"/api/accounts")
def api_accounts(r):
    items = []
    for name, info in Config.get_accounts().items():
        items.append({"name": name, "username": info.get("username", ""), "user_id": info.get("user_id", ""),
                      "remark": info.get("remark", ""), "is_valid": info.get("is_valid", True),
                      "last_tested": info.get("last_tested", ""), "is_main": name == Config.MAIN_ACCOUNT,
                      "r18": info.get("r18"), "r18g": info.get("r18g"),
                      "visibility_tested": info.get("visibility_tested", ""),
                      "token_hint": mask_token(info.get("token", ""))})
    return {"items": items}


@route("POST", r"/api/accounts/test")
def api_accounts_test(r):
    from pixiv_dl.token_manager import test_all_tokens
    results = test_all_tokens(verbose=False)
    r.app.pro.clients.clear()
    return {"items": results}


@route("POST", r"/api/accounts/main")
def api_accounts_main(r):
    name = r.body.get("name")
    if name not in Config.TOKENS:
        raise HttpError(404, "账号不存在")
    Config.set_main_account(name)
    return {"ok": True}


@route("DELETE", r"/api/accounts/(?P<name>.+)")
def api_accounts_delete(r):
    name = unquote(r.m["name"])
    if name not in Config.TOKENS:
        raise HttpError(404, "账号不存在")
    Config.remove_token(name)
    r.app.pro.clients.pop(name, None)
    return {"ok": True}


@route("POST", r"/api/accounts")
def api_accounts_add(r):
    from pixiv_dl.token_manager import test_token_validity
    name = _str(r.body.get("name") or "")
    token = _str(r.body.get("token") or "")
    if not name or not token:
        raise HttpError(400, "账号名和 refresh token 都不能为空")
    ok, info = test_token_validity(token, visibility=True)
    username = info.get("username", "") if ok and isinstance(info, dict) else ""
    user_id = info.get("user_id", "") if ok and isinstance(info, dict) else ""
    if not ok:
        raise HttpError(400, f"Token 无效: {info or '认证失败'}")
    Config.add_token(name, token, username, user_id, True, _str(r.body.get("remark") or ""))
    from pixiv_dl.token_manager import apply_visibility
    apply_visibility(Config.TOKENS[name], info)
    Config.save_settings()
    r.app.pro.clients.pop(name, None)
    return {"ok": True, "username": username}


@route("POST", r"/api/accounts/oauth/start")
def api_oauth_start(r):
    from pixiv_dl.token_manager import PixivTokenManager
    mgr = PixivTokenManager()
    verifier, challenge = mgr.generate_pkce_challenge()
    state = os.urandom(8).hex()
    r.app.oauth[state] = verifier
    if len(r.app.oauth) > 20:
        r.app.oauth.pop(next(iter(r.app.oauth)))
    return {"state": state, "url": mgr.get_auth_url(challenge)}


@route("POST", r"/api/accounts/oauth/finish")
def api_oauth_finish(r):
    from pixiv_dl.token_manager import finish_oauth
    verifier = r.app.oauth.pop(r.body.get("state"), None)
    if not verifier:
        raise HttpError(400, "授权会话已过期，请重新开始")
    ok, msg, name = finish_oauth(r.body.get("callback") or "", verifier,
                                 _str(r.body.get("name") or ""), _str(r.body.get("remark") or ""))
    if not ok:
        raise HttpError(400, msg)
    r.app.pro.clients.pop(name, None)
    return {"ok": True, "message": msg, "name": name}


# ---- 设置
@route("GET", r"/api/settings")
def api_settings(r):
    return {"settings": Config.public_view(), "presets": Config.list_presets()}


@route("POST", r"/api/settings")
def api_settings_save(r):
    if r.app.runner.running:
        raise HttpError(409, "任务运行中，暂不能修改设置")
    errors, applied = {}, {}
    for k, v in r.body.items():
        spec = SETTINGS_SPEC.get(k)
        if spec is None:
            errors[k] = "不支持修改"
            continue
        if k in PASSWORD_KEYS and not v:
            continue  # 密码留空 = 不修改
        try:
            applied[k] = spec(v)
        except (ValueError, TypeError) as e:
            errors[k] = str(e)
    if errors:
        raise HttpError(400, "; ".join(f"{k}: {m}" for k, m in errors.items()))
    if applied.get("PROXY_MODE", Config.PROXY_MODE) == "custom" and not applied.get("PROXY_URL", Config.PROXY_URL):
        raise HttpError(400, "PROXY_URL: 选了自定义代理，但没有填地址")
    journal_warning = None
    old_journal = Config.DB_JOURNAL
    for k, v in applied.items():
        setattr(Config, k, v)
    if "PROXY_MODE" in applied or "PROXY_URL" in applied:
        from pixiv_dl import proxy
        Config.apply_proxy()
        # 已经建好的连接还带着旧的代理设置：图片下载的会话直接改，各账号的接口连接丢掉重建
        proxy.configure_session(r.app.pro.session)
        r.app.pro.clients.clear()
        logger.info(f"代理设置已改为: {proxy.describe(Config.PROXY_MODE, Config.PROXY_URL)}")
    if applied.get("DB_JOURNAL") and applied["DB_JOURNAL"] != old_journal:
        from pixiv_dl import dbtools
        try:
            actual = dbtools.set_journal_mode(Config.DB_PATH, applied["DB_JOURNAL"])
            if actual != applied["DB_JOURNAL"]:
                journal_warning = f"日志模式暂时没能切换（当前仍是 {actual}），重启程序后会按设置生效。"
        except Exception as e:
            journal_warning = f"切换日志模式失败：{e}。重启程序后会按设置重试。"
    Config.save_settings()
    r.app.reset_storage()
    r.app.invalidate()
    return {"ok": True, "applied": [k for k in applied if k not in PASSWORD_KEYS], "warning": journal_warning}


@route("POST", r"/api/settings/test-proxy")
def api_test_proxy(r):
    """按表单里（还没保存）的代理设置试着连一下 Pixiv。只读，不登录、不下载。"""
    from pixiv_dl import proxy
    mode = r.body.get("PROXY_MODE") or Config.PROXY_MODE or "system"
    if mode not in proxy.MODES:
        raise HttpError(400, "代理方式不对")
    try:
        url = proxy.normalize(r.body.get("PROXY_URL") if r.body.get("PROXY_URL") is not None else Config.PROXY_URL)
    except ValueError as e:
        raise HttpError(400, str(e))
    if mode == "custom" and not url:
        raise HttpError(400, "请先填写代理地址")
    result = proxy.test(mode, url)
    if mode == "system":
        found = proxy.effective_system_proxy()
        result["using"] = f"跟随系统设置（{'系统代理 ' + found if found else '系统没有设置代理，直接连接'}）"
    return result


def _nas_info(b):
    """前端传来的表单值；密码留空表示沿用已保存的密码。"""
    return {
        "ip": (b.get("NAS_IP") or Config.NAS_IP or "").strip(), "user": (b.get("NAS_USER") or Config.NAS_USER or "").strip(),
        "pass": b.get("NAS_PASS") or Config.NAS_PASS, "share": (b.get("NAS_SHARE") or Config.NAS_SHARE or "").strip(),
        "base_path": b.get("NAS_BASE_PATH") if b.get("NAS_BASE_PATH") is not None else Config.NAS_BASE_PATH,
        "remote_name": b.get("NAS_REMOTE_NAME") or Config.NAS_REMOTE_NAME}


@route("POST", r"/api/settings/test-storage")
def api_test_storage(r):
    b = r.body
    mode = b.get("STORAGE_MODE") or Config.STORAGE_MODE
    if mode == "smb":
        from pixiv_dl.storage import smbtools
        return smbtools.diagnose(_nas_info(b))
    import pixiv_dl.storage.diagnose as storagetest
    return storagetest.diagnose(mode, b)


@route("POST", r"/api/storage/browse")
def api_storage_browse(r):
    """浏览 NAS 的共享 / 文件夹（设置页「浏览…」）。body: NAS_IP/NAS_USER/NAS_PASS/NAS_REMOTE_NAME + share + path"""
    mode = r.body.get("mode") or "smb"
    if mode == "smb":
        from pixiv_dl.storage import smbtools
        info = _nas_info(r.body)
        info["share"] = (r.body.get("share") or "").strip()
        info["path"] = r.body.get("path") or ""
        return smbtools.browse(info)
    if mode not in ("webdav", "ftp", "sftp", "s3"):
        raise HttpError(400, "这种存储方式不支持浏览")
    import pixiv_dl.storage.browse as storagebrowse
    return storagebrowse.browse(mode, r.body, r.body.get("path") or "")


# --------------------------------------------------------------------------- HTTP 处理
class Handler(BaseHTTPRequestHandler):
    server_version = "PixivUI/" + VERSION
    protocol_version = "HTTP/1.1"
    app = None  # 由 serve() 注入

    def log_message(self, fmt, *args):  # 不把每个请求都打到控制台
        logger.debug("web: " + fmt % args)

    def handle(self):
        # 浏览器/客户端关闭 keep-alive 连接时，Windows 上会表现为连接被重置，属于正常情况
        try:
            super().handle()
        except (ConnectionError, TimeoutError):
            pass

    # -- 安全检查
    def _host_ok(self):
        host = (self.headers.get("Host") or "").split(":")[0].strip("[]").lower()
        allowed = {"localhost", "127.0.0.1", "::1", Config.WEB_HOST.lower()}
        return host in allowed or Config.WEB_HOST in ("0.0.0.0", "::")

    def _csrf_ok(self):
        if self.headers.get("X-Pixiv-UI") != "1":
            return False
        origin = self.headers.get("Origin")
        if origin:
            host = (urlparse(origin).hostname or "").lower()
            return host in {"localhost", "127.0.0.1", "::1", Config.WEB_HOST.lower()} or Config.WEB_HOST in ("0.0.0.0", "::")
        return True

    # -- 输出
    def _send(self, status, data, content_type, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _json(self, obj, status=200):
        self._send(status, json.dumps(obj, ensure_ascii=False, default=_json_default).encode("utf-8"),
                   "application/json; charset=utf-8", {"Cache-Control": "no-store"})

    def _static(self, path):
        rel = "index.html" if path in ("", "/") else path.lstrip("/")
        full = os.path.normpath(os.path.join(STATIC_DIR, rel))
        if not full.startswith(STATIC_DIR) or not os.path.isfile(full):
            return self._json({"error": "Not found"}, 404)
        with open(full, "rb") as f:
            data = f.read()
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith("javascript"):
            ctype += "; charset=utf-8"
        self._send(200, data, ctype, {"Cache-Control": "no-cache",
                                      "Content-Security-Policy": "default-src 'self'; img-src 'self' data: blob:; "
                                                                 "style-src 'self' 'unsafe-inline'; frame-ancestors 'none'"})

    def _drain_body(self):
        """先把请求体读完。keep-alive 连接上如果带着没读的请求体就返回，剩余字节会被当成下一个请求。"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > 1_000_000:
            self.close_connection = True
            raise HttpError(413, "请求体过大")
        return self.rfile.read(length) if length > 0 else b""

    @staticmethod
    def _parse_body(raw):
        if not raw:
            return {}
        try:
            body = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise HttpError(400, "请求体不是合法 JSON")
        if not isinstance(body, dict):
            raise HttpError(400, "请求体必须是 JSON 对象")
        return body

    def _handle(self):
        try:
            raw_body = self._drain_body()
            if not self._host_ok():
                return self._json({"error": "Host 不被允许"}, 403)
            parsed = urlparse(self.path)
            path = parsed.path
            if not path.startswith("/api/"):
                if self.command not in ("GET", "HEAD"):
                    return self._json({"error": "Method Not Allowed"}, 405)
                return self._static(path)
            if self.command not in ("GET", "HEAD") and not self._csrf_ok():
                return self._json({"error": "缺少 X-Pixiv-UI 头或来源不被允许"}, 403)
            body = self._parse_body(raw_body) if self.command in ("POST", "DELETE") else {}
            query = parse_qs(parsed.query)
            for method, rx, fn in ROUTES:
                m = rx.match(path)
                if m and (method == self.command or (self.command == "HEAD" and method == "GET")):
                    result = fn(Request(self.app, m, query, body))
                    if isinstance(result, Raw):
                        extra = {}
                        if result.filename:
                            extra["Content-Disposition"] = f'attachment; filename="{result.filename}"'
                        if result.cache:
                            extra["Cache-Control"] = result.cache
                        return self._send(200, result.data, result.content_type, extra)
                    return self._json(result)
            return self._json({"error": "接口不存在"}, 404)
        except HttpError as e:
            return self._json({"error": e.message}, e.status)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as e:
            logger.exception(f"web 请求出错: {self.command} {self.path}")
            try:
                return self._json({"error": f"{type(e).__name__}: {e}"}, 500)
            except Exception:
                return

    do_GET = do_POST = do_DELETE = do_HEAD = lambda self: self._handle()


def make_server(host=None, port=None, processor=None):
    setup_logging(console=True, to_file=True)
    Handler.app = App(processor)
    host = host or Config.WEB_HOST
    port = int(port if port is not None else Config.WEB_PORT)
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    return httpd


def serve(host=None, port=None, processor=None, open_browser=False):
    httpd = make_server(host, port, processor)
    h, p = httpd.server_address[:2]
    url = f"http://{'localhost' if h in ('0.0.0.0', '::') else h}:{p}/"
    if h not in ("127.0.0.1", "localhost", "::1"):
        logger.warning(f"⚠ Web 前端监听在 {h}，局域网内任何人都能操作你的下载器（没有登录验证）。建议只监听 127.0.0.1。")
    logger.info(f"Web 前端已启动: {url}  (Ctrl+C 退出)")
    if open_browser:
        import webbrowser
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("正在关闭 Web 前端...")
    finally:
        interrupt.set()
        httpd.server_close()

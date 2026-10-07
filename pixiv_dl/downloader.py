import hashlib
import logging
import os
import queue
import random
import shutil
import tempfile
import threading
import time
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor, wait as futures_wait
from urllib.parse import urlparse

import requests

from pixiv_dl import interrupt, netwatch, ratelimit, visibility
from pixiv_dl.config import Config
from pixiv_dl.database import Database, ST_DONE, ST_FAILED
from pixiv_dl.extract import _g, extract_pages, is_visible, ugoira_info, ugoira_zip_candidates, url_ext
from pixiv_dl.pixiv_client import AUTH, INTERRUPTED, NOT_FOUND, RATE_LIMIT
from pixiv_dl.progress import job_op
from pixiv_dl.ugoira import convert_ugoira

logger = logging.getLogger("PixivDownloader")

# 这些状态码重试没有意义：地址已失效/无权限
_NO_RETRY_STATUS = (400, 401, 403, 404, 410)


class NetError(Exception):
    """下载过程中的网络类问题（内容不完整等），可重试。"""


class ContentError(NetError):
    """下载到的内容不对（过小/被拦截），也会重试，但失败原因归为「内容异常」。"""


class HttpStatus(Exception):
    def __init__(self, code):
        super().__init__(f"HTTP {code}")
        self.code = code


def kind_of(e):
    """把异常归类为失败原因（error_kind），前端按它分组并给出对应的处理建议。"""
    if isinstance(e, HttpStatus):
        if e.code in (404, 410):
            return 'deleted'
        if e.code == 429:
            return 'rate_limit'
        if e.code in (400, 401, 403):
            return 'http'
        return 'network'
    if isinstance(e, (ValueError, ContentError)):
        return 'content'
    if isinstance(e, (NetError, requests.RequestException, TimeoutError)):
        return 'network'
    if isinstance(e, (OSError, RuntimeError)):
        return 'storage'
    return 'other'


class AuthBroken(Exception):
    """某个账号的 token 失效，该 worker 应退出并把手头的任务放回队列。"""


class Throttle:
    """下载的节奏控制。

    - 周期性休息：按账号各算各的——某个账号每下完 REST_EVERY 个文件，这个账号休息 REST_SECONDS 秒，别的账号照常下。
      以前是所有账号合在一起数、一起停，账号越多停得越频繁，多账号的提速大半被吃掉了。
    - 失败率过高、图片服务器限速：所有账号一起停一会儿（这两种情况和哪个账号无关）。

    各工作线程在开始处理新作品前调用 wait(账号)，该休息时在这里等。
    """

    def __init__(self, job=None):
        self._lock = threading.Lock()
        self._resume_at = 0.0           # 所有账号一起停到什么时候
        self._account_until = {}        # 账号 -> 这个账号休息到什么时候
        self._counts = {}               # 账号 -> 已下载的文件数
        self._window = deque(maxlen=max(5, int(getattr(Config, 'FAILURE_RATE_WINDOW', 20))))
        self._job = job

    def _pause(self, seconds, reason, account=None):
        until = time.time() + seconds
        if account is None:
            if until > self._resume_at:
                self._resume_at = until
            text = f"{reason}，所有账号暂停 {seconds} 秒"
        else:
            if until > self._account_until.get(account, 0.0):
                self._account_until[account] = until
            text = f"账号 {account} {reason}，休息 {seconds} 秒（其他账号继续）"
        logger.info(text)
        if self._job:
            self._job.log(text)

    def record(self, ok, account=None):
        with self._lock:
            self._window.append(1 if ok else 0)
            if getattr(Config, 'AUTO_THROTTLE_ENABLED', True) and len(self._window) >= 5:
                fail_rate = 1.0 - sum(self._window) / len(self._window)
                if fail_rate >= getattr(Config, 'FAILURE_RATE_THRESHOLD', 0.5):
                    self._window.clear()
                    self._pause(getattr(Config, 'FAILURE_PAUSE_SECONDS', 10), f"失败率过高({fail_rate:.0%})")
                    return
            if ok:
                count = self._counts[account] = self._counts.get(account, 0) + 1
                every = int(getattr(Config, 'REST_EVERY', 0) or 0)
                seconds = int(getattr(Config, 'REST_SECONDS', 0) or 0)
                if getattr(Config, 'RATE_LIMIT_ENABLED', False) and every > 0 and seconds > 0 and count % every == 0:
                    self._pause(seconds, f"已下载 {count} 个文件", account=account)

    def hold(self, seconds, reason):
        """所有下载线程一起停一会儿"""
        with self._lock:
            self._window.clear()
            if self._resume_at - time.time() < seconds * 0.5:
                self._pause(seconds, reason)

    def remaining(self, account=None):
        with self._lock:
            until = max(self._resume_at, self._account_until.get(account, 0.0))
        return max(0.0, until - time.time())

    def wait(self, account=None):
        """该休息时在这里等；被中断时返回 True。"""
        while True:
            with self._lock:
                everyone = self._resume_at - time.time()
                remain = max(everyone, self._account_until.get(account, 0.0) - time.time())
            if remain <= 0:
                return False
            if self._job and everyone > 0:
                self._job.set(message=f"所有账号暂停中，剩余 {int(everyone) + 1} 秒")
            if interrupt.wait(min(remain, 1.0)):
                return True
            if self._job and everyone > 0 and self._resume_at - time.time() <= 0:
                self._job.set(message="")


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


class DownloadMixin:
    # ------------------------------------------------------------------ HTTP
    def _get_host_semaphore(self, url):
        host = urlparse(url or '').netloc
        if not host:
            return None
        with self._sem_lock:
            if host not in self.host_semaphores:
                limit = (getattr(Config, 'HOST_CONCURRENCY', {}) or {}).get(
                    host, getattr(Config, 'DEFAULT_MAX_PER_HOST', 3))
                self.host_semaphores[host] = threading.Semaphore(max(1, int(limit)))
            return self.host_semaphores[host]

    def _http_get(self, url):
        """下载一个 URL，返回 bytes。403/404 等不重试，5xx/429/网络错误指数退避重试。"""
        sem = self._get_host_semaphore(url)
        retries = max(1, int(getattr(Config, 'MAX_RETRIES', 3)))
        last = None
        for attempt in range(retries):
            if interrupt.is_set():
                raise InterruptedError("下载被中断")
            if sem and not sem.acquire(timeout=60):
                raise TimeoutError("等待主机并发令牌超时")
            try:
                resp = self.session.get(url, timeout=(10, 30), stream=True)
                try:
                    if resp.status_code != 200:
                        raise HttpStatus(resp.status_code)
                    total = int(resp.headers.get('content-length') or 0)
                    chunks = []
                    for chunk in resp.iter_content(chunk_size=65536):
                        if interrupt.is_set():
                            raise InterruptedError("下载被中断")
                        if chunk:
                            chunks.append(chunk)
                            self.job.add_transfer(len(chunk))
                    data = b''.join(chunks)
                finally:
                    resp.close()
                if total and not resp.headers.get('content-encoding') and len(data) != total:
                    raise NetError(f"下载不完整 ({len(data)}/{total} bytes)")
                if len(data) < 100:
                    raise ContentError("下载内容过小（可能被拦截）")
                return data
            except HttpStatus as e:
                if e.code in _NO_RETRY_STATUS:
                    raise
                last = e
            except InterruptedError:
                raise
            except (requests.RequestException, NetError, OSError) as e:
                last = e
            finally:
                if sem:
                    sem.release()
            if attempt < retries - 1 and interrupt.wait(min(8, 2 ** attempt)):
                raise InterruptedError("下载被中断")
        raise last

    # ------------------------------------------------------------------ 失败记录
    def _fail_task(self, db, task_key, msg, permanent=False, kind='other'):
        # 被限速不是这个文件的问题：只记原因，不占它的重试次数
        n = db.mark_failed(task_key, kind, msg, permanent=permanent, max_attempts=Config.MAX_ATTEMPTS,
                           count_attempt=kind != 'rate_limit')
        self.job.tally('fail_kinds', kind, count=1)
        if permanent:
            if kind not in ('deleted', 'restricted'):   # 已删除 / 无权查看是常见情况，只记失败原因，不刷告警
                db.insert_alert(task_key, 'ERROR', msg)
            logger.warning(f"任务 {task_key} 永久失败: {msg}")
        else:
            if n is not None and n >= Config.MAX_ATTEMPTS:
                db.insert_alert(task_key, 'ERROR', f"达到最大重试次数({n}): {msg}")
            logger.warning(f"任务 {task_key} 失败(第{n}次): {msg}")

    def _finish_ok(self, db, task_key, size, file_hash=None):
        db.mark_status(task_key, ST_DONE)
        db.update_file_info(task_key, file_hash, size)

    # ------------------------------------------------------------------ 单个作品（一组页面任务）
    def _artist_folder(self, db, aid):
        name = db.get_artist(aid)[1] if aid else "Unknown"
        if name == "Unknown" and aid:
            name = f"User_{aid}"
        return self.storage.get_artist_folder(aid, self._safe(name), rename=False), name

    def _existing_size(self, rel_paths):
        for p in rel_paths:
            size = self.storage.get_file_size(p)
            if size >= 100:
                return size
        return 0

    def _process_group(self, client, db, iid, media_type, tasks, throttle):
        """处理同一个作品的所有页面。下载地址一律通过 API 重新获取，不使用库里保存的旧地址。"""
        job = self.job
        aid = tasks[0][3] or 0
        keys = [t[0] for t in tasks]
        if not aid:
            for k in keys:
                self._fail_task(db, k, "缺少作者信息（元数据缺失）", kind='other')
            job.add(done=len(keys), failed=len(keys))
            return False
        try:
            folder, name = self._artist_folder(db, aid)
        except InterruptedError:
            raise
        except Exception as e:
            for k in keys:
                self._fail_task(db, k, f"存储不可用: {e}", kind='storage')
            job.add(done=len(keys), failed=len(keys))
            throttle.record(False)
            return False
        job.set_current(client.name, f"[{aid}] {name} · {iid}")
        job.worker_begin(client.name, f"[{aid}] {name} · {iid}")

        handler = {'ugoira': self._group_ugoira, 'novel': self._group_novel}.get(media_type, self._group_image)
        used_api = True
        try:
            results, used_api = handler(client, db, iid, tasks, folder)
        except InterruptedError:
            raise
        except AuthBroken:
            raise
        except _NotVisible:
            raise                       # 交给 download：换一个账号再试
        except _RateLimited:
            raise                       # 交给 download：这个作品放回队列，不算失败
        except _GroupError as e:
            results = {k: ('fail', e.message, e.permanent, e.kind) for k in keys}
        except Exception as e:
            logger.exception(f"处理作品 {iid} 出错")
            results = {k: ('fail', f"{type(e).__name__}: {e}", False, kind_of(e)) for k in keys}

        # 网络类的失败：先看是不是网络断了。断了的话这些文件不记失败，放回队列，等网络回来再下
        down = [k for k in keys if results.get(k, ('',))[0] == 'fail' and len(results[k]) > 3
                and results[k][3] == 'network' and not results[k][2]]
        if down and netwatch.watch.is_up():
            down = []
        for k in keys:
            if k in down:
                continue
            kind, *rest = results.get(k, ('fail', '未处理', False, 'other'))
            if kind == 'ok':
                job.add(done=1, success=1, bytes=rest[0])
                job.worker_add(client.name, success=1, bytes=rest[0])
                job.tally('downloaded', aid, name=name, files=1, bytes=rest[0])
                throttle.record(True, client.name)
            elif kind == 'skip':
                job.add(done=1, success=1, skipped=1, bytes=rest[0])
                job.worker_add(client.name, success=1)
            else:
                self._fail_task(db, k, rest[0], permanent=rest[1], kind=rest[2] if len(rest) > 2 else 'other')
                job.add(done=1, failed=1)
                job.worker_add(client.name, failed=1)
                if len(rest) > 2 and rest[2] == 'rate_limit':
                    throttle.hold(60, "图片服务器限速")
                elif not rest[1]:  # 作品已删除/地址失效是确定性结果，不代表被限速，不计入失败率
                    throttle.record(False)
        if down:
            raise _NetworkDown([t for t in tasks if t[0] in down])
        return used_api

    # -- 图片
    def _group_image(self, client, db, iid, tasks, folder):
        # 文件已经在存储里（例如核查后重置过状态）就不必再请求 API
        pre = {}
        for task in tasks:
            ext = url_ext(task[5])
            size = self._existing_size([f"{folder}/{iid}_p{task[2]}.{ext}"])
            if size:
                db.mark_status(task[0], ST_DONE)
                db.update_file_info(task[0], None, size)
                pre[task[0]] = ('skip', size)
        if len(pre) == len(tasks):
            return pre, False
        res, err = client.call(client.api.illust_detail, iid)
        if err:
            self._raise_api_error(err, f"获取作品 {iid} 详情失败")
        ill = _g(res, 'illust')
        if not ill:
            raise _NotVisible("作品不存在", restricted=False)
        if not is_visible(ill):
            # API 给的是“无权查看”的占位项：作品还在，只是这个账号看不了（例如没开 R-18 显示、作者限制了范围）
            raise _NotVisible("这个账号无权查看", restricted=True)
        if _g(ill, 'type') == 'ugoira':
            # 库里把它记成了普通图片（旧数据），实际是动图：改过来，按动图下载。
            # 以前会拿 ugoira:// 这个占位地址去当图片下，报 “No connection adapters were found”。
            with db.tx() as c:
                c.execute("UPDATE illusts SET media_type = 'ugoira' WHERE illust_id = ?", (iid,))
            return self._group_ugoira(client, db, iid, tasks[:1], folder)
        fresh = {idx: url for idx, url, _ in extract_pages(ill)}
        results = dict(pre)
        for task in tasks:
            key, _, idx = task[0], task[1], task[2]
            if key in pre:
                continue
            url = fresh.get(idx)
            if not url:
                results[key] = ('fail', f"作品当前不存在第 {idx} 页", True, 'deleted')
                continue
            if interrupt.wait_if_paused() or interrupt.is_set():       # 暂停在页与页之间就生效，不用等整个作品下完
                raise InterruptedError()
            results[key] = self._download_image_page(db, key, iid, idx, url, folder, task[5])
        return results, True

    def _download_image_page(self, db, key, iid, idx, url, folder, old_url):
        ext = url_ext(url)
        rel = f"{folder}/{iid}_p{idx}.{ext}"
        candidates = [rel]
        old_ext = url_ext(old_url, default=ext)  # 仅用于识别历史上已保存的文件名，不用于下载
        if old_ext != ext:
            candidates.append(f"{folder}/{iid}_p{idx}.{old_ext}")
        size = self._existing_size(candidates)
        if size:
            db.mark_status(key, ST_DONE)
            db.update_file_info(key, None, size)
            return ('skip', size)
        db.update_task_url(key, url)  # 刷新为最新地址，仅供参考
        try:
            data = self._http_get(url)
        except InterruptedError:
            raise
        except HttpStatus as e:
            return ('fail', f"下载失败 {e}", e.code in (404, 410), kind_of(e))
        except Exception as e:
            return ('fail', f"{type(e).__name__}: {e}", False, kind_of(e))
        try:
            self.storage.put_bytes(rel, data)
        except InterruptedError:
            raise
        except ValueError as e:
            return ('fail', f"内容异常: {e}", False, 'content')
        except Exception as e:
            return ('fail', f"写入存储失败: {type(e).__name__}: {e}", False, 'storage')
        self._finish_ok(db, key, len(data), _sha256(data))
        return ('ok', len(data))

    # -- 动图
    def _group_ugoira(self, client, db, iid, tasks, folder):
        key = tasks[0][0]
        zip_candidates_rel = [f"{folder}/动图zip/{iid}_p0.zip", f"{folder}/动图zip/{iid}_p0.bin"]
        webp_rel = f"{folder}/{iid}_p0.webp"
        zsize = self._existing_size(zip_candidates_rel)
        wsize = self._existing_size([webp_rel])
        if zsize and wsize:
            db.mark_status(key, ST_DONE)
            db.update_file_info(key, None, zsize + wsize)
            return {key: ('skip', zsize + wsize)}, False

        res, err = client.call(client.api.ugoira_metadata, iid)
        if err:
            self._raise_api_error(err, f"获取动图 {iid} 元数据失败")
        meta = _g(res, 'ugoira_metadata')
        urls = ugoira_zip_candidates(meta, getattr(Config, 'UGOIRA_PREFER_HQ', True))
        if not urls:
            raise _GroupError("动图元数据中没有 zip 地址", permanent=True, kind='deleted')
        info = ugoira_info(meta)
        if not db.get_ugoira_data(iid):
            import json
            db.set_ugoira_data(iid, json.dumps(info, ensure_ascii=False))

        data, used, last = None, None, None
        for u in urls:
            try:
                data = self._http_get(u)
                used = u
                break
            except HttpStatus as e:
                last = e
                continue
        if data is None:
            raise _GroupError(f"动图 zip 下载失败: {last}", permanent=isinstance(last, HttpStatus) and last.code in (404, 410),
                              kind=kind_of(last) if last else 'network')

        os.makedirs(Config.LOCAL_TEMP_PATH, exist_ok=True)
        tmp = tempfile.mkdtemp(prefix="ugoira_", dir=Config.LOCAL_TEMP_PATH)
        try:
            zip_local = os.path.join(tmp, f"{iid}_p0.zip")
            with open(zip_local, "wb") as f:
                f.write(data)
            # 先保存 zip 原件（主数据），再尝试生成 webp
            zip_rel = zip_candidates_rel[0]
            if not zsize:
                self.storage.put_file(zip_rel, zip_local)
            total = len(data)
            try:
                webp_local = convert_ugoira(zip_local, info['frames'], out_dir=os.path.join(tmp, "out"))
                if not wsize:
                    total += self.storage.put_file(webp_rel, webp_local)
            except InterruptedError:
                raise
            except Exception as e:
                logger.warning(f"动图 {iid} 已保存 zip，但 WebP 转换/保存失败: {e}")
                db.insert_alert(key, 'WARN', f"zip 已保存，WebP 转换/保存失败: {e}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self._finish_ok(db, key, total, _sha256(data))
        return {key: ('ok', total)}, True

    # -- 小说
    def _group_novel(self, client, db, iid, tasks, folder):
        key = tasks[0][0]
        rel = f"{folder}/{iid}_p0.txt"
        size = self.storage.get_file_size(rel)
        if size >= 1:
            db.mark_status(key, ST_DONE)
            db.update_file_info(key, None, size)
            return {key: ('skip', size)}, False
        text = self._fetch_novel_text(client, iid)
        data = text.encode('utf-8')
        self.storage.put_bytes(rel, data, min_size=1)
        self._finish_ok(db, key, len(data), _sha256(data))
        return {key: ('ok', len(data))}, True

    def _fetch_novel_text(self, client, iid):
        api = client.api
        errors = []
        for method, fields in (('webview_novel', ('text',)), ('novel_text', ('novel_text', 'text'))):
            fn = getattr(api, method, None)
            if fn is None:
                continue
            res, err = client.call(fn, iid)
            if err:
                if err.kind in (NOT_FOUND, AUTH, INTERRUPTED):
                    self._raise_api_error(err, f"获取小说 {iid} 失败")
                errors.append(str(err))
                continue
            for f in fields:
                v = _g(res, f)
                if isinstance(v, str) and v.strip():
                    return v
        raise _GroupError("未能获取小说正文: " + "; ".join(errors) if errors else "未能获取小说正文")

    def _raise_api_error(self, err, prefix):
        if err.kind == INTERRUPTED:
            raise InterruptedError()
        if err.kind == AUTH:
            raise AuthBroken(err.message)
        if err.kind == NOT_FOUND:
            # “找不到”可能是真的删了，也可能只是这个账号看不到（看不到的作品，动图接口就返回 Page not found）。
            # 先换别的账号试，都不行才下结论（见 download 里的处理）
            raise _NotVisible(f"{prefix}: {err.message}", restricted=False)
        if err.kind == RATE_LIMIT:
            raise _RateLimited(f"{prefix}: {err.message}")
        raise _GroupError(f"{prefix}: {err.message}", permanent=False, kind='api')

    # ------------------------------------------------------------------ 主流程
    @job_op('download')
    def download(self, aid=None, limit=None, aids=None, filters=None, accounts=None):
        """阶段 B：下载。多账号并发，每个账号按配置的线程数工作。

        filters：只下载符合条件的（类型、日期、画师、每位画师的数量……见 Database.get_pending_tasks）。
        accounts：只用这几个账号。"""
        db = Database.local(Config.DB_PATH)
        job = self.job
        tasks = db.get_pending_tasks(aid, limit, max_attempts=Config.MAX_ATTEMPTS, author_ids=aids, filters=filters)
        if not tasks:
            logger.info("没有待处理任务")
            job.log("没有待处理任务")
            return {'tasks': 0}

        job.set(phase="认证账号")
        clients = self.ensure_clients()
        if accounts:
            clients = {n: c for n, c in clients.items() if n in set(accounts)}
        if not clients:
            raise RuntimeError("没有可用的账号（请先在「账号」页添加并确认 Token 有效）")
        _ = self.storage  # 尽早暴露存储连接问题
        job.log(f"使用 {len(clients)} 个账号并发下载: {list(clients)}")

        groups = OrderedDict()
        for t in tasks:
            groups.setdefault((t[1], t[6]), []).append(t)
        # 队列里每一项带着“哪些账号已经试过、看不到”。一个账号看不到的作品换别的账号再试，
        # 所有账号都看不到才记为失败——备用账号常常看不了主账号能看的作品（没开 R-18 显示等）。
        # 按账号验证时测得的可见性分配：R-18 / R-18G 作品只交给看得到的账号，不让看不到的账号白试。
        # 没测过的账号当作看得到（真看不到时会像上面说的那样换账号）。
        accounts = Config.get_accounts()
        levels = db.restrict_levels([iid for iid, _ in groups])
        blocked = {lv: {n for n in clients if not visibility.can_view(accounts.get(n), lv)} for lv in (1, 2)}
        for lv, names in blocked.items():
            if names:
                job.log(f"{'R-18' if lv == 1 else 'R-18G'} 作品不分配给: {sorted(names)}（验证账号时测得看不到）")
        q = queue.Queue()
        for (iid, mt), ts in groups.items():
            lv = levels.get(iid, 0)
            q.put((iid, mt, ts, {n: True for n in blocked.get(lv, ())}))
        state_lock = threading.Lock()
        alive = {}                      # 账号 -> 还在工作的线程数
        inflight = [0]                  # 正在处理中的作品数（它们可能被放回队列）
        blind = {}                      # (账号, 画师) -> 连续看不到的作品数
        BLIND_AFTER = 3                 # 一个账号连续这么多次看不到某位画师的作品，就不再拿这位画师的作品去试它

        def give_up(wdb, iid, ts, tried):
            """所有账号都试过了：有账号拿到过“无权查看”的占位说明作品还在，否则才当作已删除"""
            restricted = any(tried.values())
            lv = levels.get(iid, 0)
            if lv and len(blocked.get(lv, ())) == len(clients):
                msg = (f"没有账号能看 {'R-18' if lv == 1 else 'R-18G'} 作品：请在 Pixiv 网页的设置里打开显示，"
                       "然后在「账号」页重新验证")
            elif restricted:
                msg = "所有账号都无权查看这个作品（作者限制了可见范围，或账号没有开启 R-18 / 敏感作品的显示）"
            else:
                msg = "作品已删除（所有账号都找不到）"
            for t in ts:
                self._fail_task(wdb, t[0], msg, permanent=True, kind='restricted' if restricted else 'deleted')
            job.add(done=len(ts), failed=len(ts))

        # 组合任务（同步+下载）里下载阶段单独计数，不和同步阶段的画师计数混在一起
        job.set(phase="下载", total=len(tasks), done=0, success=0, failed=0, skipped=0, bytes=0)
        throttle = Throttle(job)
        main = Config.MAIN_ACCOUNT

        def worker(client):
            with state_lock:
                alive[client.name] = alive.get(client.name, 0) + 1
            try:
                _worker(client)
            finally:
                with state_lock:
                    alive[client.name] -= 1
                job.worker_exit(client.name)

        def _worker(client):
            wdb = Database.local(Config.DB_PATH)
            me = client.name
            while not interrupt.is_set():
                if interrupt.is_paused():
                    job.worker_set(me, state='paused', text='已暂停')
                if interrupt.wait_if_paused() or ratelimit.gate.wait(me):
                    return
                if ratelimit.gate.exhausted(me):
                    job.worker_set(me, state='stopped', text='', note='被限速太久，这次不再使用')
                    return
                # 取任务和“手里有任务”的计数要一起做：否则别的线程会在这一瞬间以为没活了而退出，
                # 之后被放回队列的作品就没有账号可以接手
                with state_lock:
                    try:
                        item = q.get_nowait()
                        inflight[0] += 1
                    except queue.Empty:
                        item = None
                        busy = inflight[0] > 0
                if item is None:
                    if not busy:
                        return
                    if interrupt.wait(0.3):      # 别的线程手里的作品可能会被放回来
                        return
                    continue
                iid, mt, ts, tried = item
                aid = ts[0][3] or 0
                with state_lock:
                    if me not in tried and blind.get((me, aid), 0) >= BLIND_AFTER:
                        tried[me] = True         # 这个账号看不了这位画师：不再为它花请求
                    others = [n for n, c in alive.items() if c > 0 and n not in tried]
                    mine = me not in tried
                if not mine:
                    try:
                        if others:
                            q.put((iid, mt, ts, tried))      # 留给还没试过的账号
                        else:
                            give_up(wdb, iid, ts, tried)
                    finally:
                        with state_lock:
                            inflight[0] -= 1
                    if others and interrupt.wait(0.2):
                        return
                    continue
                used_api = False
                try:
                    if throttle.remaining(me) > 0:
                        job.worker_set(me, state='resting', text='休息中')
                    if throttle.wait(me):
                        q.put((iid, mt, ts, tried))
                        return
                    used_api = self._process_group(client, wdb, iid, mt, ts, throttle)
                    with state_lock:
                        blind.pop((me, aid), None)
                except _NotVisible as e:
                    used_api = True
                    with state_lock:
                        tried[me] = e.restricted
                        blind[(me, aid)] = blind.get((me, aid), 0) + 1
                    q.put((iid, mt, ts, tried))               # 换别的账号；都试过之后由 give_up 收尾
                except _NetworkDown as e:
                    q.put((iid, mt, e.tasks, tried))          # 只把没下成的那几页放回去；已经下好的不重来
                    job.worker_set(me, state='resting', text='网络连不上，等待恢复')
                    if netwatch.watch.wait_until_up() == "interrupted":
                        return
                except _RateLimited:
                    used_api = True
                    q.put((iid, mt, ts, tried))               # 不算失败：等限速过去，或者留给别的账号 / 下次
                    job.log(f"账号 {me} 被限速太久，停用；没下完的留在待下载里")
                    job.worker_set(me, state='stopped', text='', note='被限速太久，这次不再使用')
                    return
                except AuthBroken as e:
                    logger.error(f"账号 '{client.name}' 认证失效，停止该账号的下载线程: {e}")
                    job.log(f"账号 {client.name} 认证失效，已停用")
                    job.worker_set(client.name, state='stopped', text='', note='认证失效，已停用')
                    q.put((iid, mt, ts, tried))
                    return
                except InterruptedError:
                    return
                except Exception as e:
                    logger.exception(f"worker 异常: {e}")
                    for t in ts:
                        self._fail_task(wdb, t[0], f"内部错误: {e}", kind='other')
                    job.add(done=len(ts), failed=len(ts))
                finally:
                    with state_lock:
                        inflight[0] -= 1
                    job.set_current(client.name, None)
                    job.worker_end(client.name, 'waiting' if used_api else 'queue')
                # 作品之间的礼貌间隔（只在真正请求过 API 之后）
                if used_api and interrupt.wait(random.uniform(*Config.DELAY_DOWNLOAD)):
                    return

        pools, futures = [], []
        job.workers_reset()
        for name, client in clients.items():
            n = (Config.MAIN_ACCOUNT_DOWNLOAD_THREADS if name == main else Config.BACKUP_ACCOUNT_DOWNLOAD_THREADS)
            n = max(1, int(n or 1))
            job.worker_register(name, n, 'main' if name == main else 'backup')
            pool = ThreadPoolExecutor(max_workers=n, thread_name_prefix=f"dl-{name}")
            pools.append(pool)
            futures += [pool.submit(worker, client) for _ in range(n)]

        try:
            while True:
                done, not_done = futures_wait(futures, timeout=0.5)
                if not not_done:
                    break
        except KeyboardInterrupt:
            interrupt.set()
            futures_wait(futures)
            raise
        finally:
            for p in pools:
                p.shutdown(wait=True)

        left = q.qsize()
        summary = {'tasks': len(tasks), 'success': job.success, 'failed': job.failed,
                   'skipped': job.skipped, 'bytes': job.bytes, 'unprocessed_groups': left,
                   'network': netwatch.watch.summary()}
        job.set(result={**job.result, **summary})
        logger.info(f"下载阶段结束: 成功 {job.success}（其中已存在跳过 {job.skipped}），失败 {job.failed}，"
                    f"数据量 {self._format_size(job.bytes)}")
        if getattr(Config, 'AUTO_CLEAN_TEMP_AFTER_DOWNLOAD', False):
            self.clean_temp()
        return summary


class _NotVisible(Exception):
    """当前账号看不到这个作品。restricted=True 表示拿到了“无权查看”的占位（作品肯定还在）。"""

    def __init__(self, message, restricted=False):
        super().__init__(message)
        self.message = message
        self.restricted = restricted


class _NetworkDown(Exception):
    """网络断了。tasks 是这个作品里还没下成的那几页，放回队列等网络恢复。"""

    def __init__(self, tasks):
        super().__init__("network down")
        self.tasks = tasks


class _RateLimited(Exception):
    """这个账号被限速到没法继续了（已经在闸前等过，预算用完）。作品放回队列，不算失败。"""


class _GroupError(Exception):
    def __init__(self, message, permanent=False, kind='other'):
        super().__init__(message)
        self.message = message
        self.permanent = permanent
        self.kind = kind

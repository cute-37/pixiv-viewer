import csv
import logging
import os
import re
import threading
import time
from datetime import datetime

import requests
from requests.adapters import HTTPAdapter

from pixiv_dl import interrupt, proxy
from pixiv_dl.artists import ArtistMixin
from pixiv_dl.config import Config
from pixiv_dl.database import Database, ST_DONE
from pixiv_dl.downloader import DownloadMixin
from pixiv_dl.extract import _g, is_visible, url_ext
from pixiv_dl.pixiv_client import PixivClient
from pixiv_dl.progress import JobState, job_op
from pixiv_dl.storage.adapter import StorageAdapter
from pixiv_dl.syncer import SyncMixin

logger = logging.getLogger("PixivDownloader")

_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


class Processor(SyncMixin, DownloadMixin, ArtistMixin):
    """业务入口。构造时不访问网络；账号认证与存储连接都是用到时才建立。"""

    def __init__(self):
        self.job = JobState("idle")
        self._job_depth = 0
        self.clients = {}
        self._clients_lock = threading.RLock()
        self._storage = None
        self._storage_lock = threading.Lock()
        self.host_semaphores = {}
        self._sem_lock = threading.Lock()

        self.session = requests.Session()
        proxy.configure_session(self.session)
        self.session.headers.update({
            'Referer': 'https://www.pixiv.net/',
            'User-Agent': Config.USER_AGENT,
            'Connection': 'close',
        })
        adapter = HTTPAdapter(pool_connections=16, pool_maxsize=32)
        self.session.mount('https://', adapter)
        self.session.mount('http://', adapter)

        # 启动时把遗留的"进行中"任务放回待处理，避免永久卡住
        try:
            n = self.db.reset_in_progress()
            if n:
                logger.info(f"已将 {n} 个遗留的进行中任务重置为待处理")
        except Exception as e:
            logger.warning(f"重置遗留任务失败: {e}")

    # ------------------------------------------------------------------ 基础设施
    @property
    def db(self):
        """当前线程专属的数据库连接。"""
        return Database.local(Config.DB_PATH)

    @property
    def storage(self):
        with self._storage_lock:
            if self._storage is None or self._storage.mode != Config.STORAGE_MODE:
                self._storage = StorageAdapter()
            return self._storage

    def reset_storage(self):
        """配置变更后丢弃旧连接，下次使用时按新配置重连。"""
        with self._storage_lock:
            self._storage = None

    @staticmethod
    def _safe(s):
        s = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', str(s)).strip().rstrip('. ')
        if s.upper() in _RESERVED:
            s = f"_{s}"
        return s or "_"

    @staticmethod
    def _format_size(size_bytes):
        size = float(size_bytes or 0)
        for unit in ('B', 'KB', 'MB', 'GB'):
            if size < 1024:
                return f"{size:.1f}{unit}"
            size /= 1024
        return f"{size:.1f}TB"

    def _sleep(self, seconds):
        """可中断的睡眠；被中断返回 True。"""
        return interrupt.wait(seconds)

    # ------------------------------------------------------------------ 账号客户端
    def ensure_clients(self):
        """为每个有效账号建立并认证客户端，返回 {账号名: 已认证的 PixivClient}。"""
        with self._clients_lock:
            accounts = Config.get_accounts()
            for name in [n for n in self.clients if n not in accounts]:
                del self.clients[name]
            for name, info in accounts.items():
                if not info.get("is_valid", True):
                    continue
                cli = self.clients.get(name)
                if cli is None or cli.token != info["token"]:
                    cli = self.clients[name] = PixivClient(info["token"], name)
                if not cli.authed and cli.auth() is None:
                    logger.warning(f"账号 '{name}' 不可用: {cli.last_error}")
            return {n: c for n, c in self.clients.items() if c.authed}

    def get_main_client(self):
        """主账号客户端。显式设置了主账号但它不可用时返回 None（不会悄悄换成别的账号）；
        没有设置主账号时使用第一个可用账号。"""
        if Config.MAIN_ACCOUNT:
            c = self.clients.get(Config.MAIN_ACCOUNT)
            return c if c is not None and c.authed else None
        return self.get_any_client()

    def get_any_client(self):
        return next((c for c in self.clients.values() if c.authed), None)

    @property
    def api(self):
        """兼容旧脚本：主账号（或任一可用账号）的 pixivpy API 对象。"""
        self.ensure_clients()
        c = self.get_main_client() or self.get_any_client()
        return c.api if c else None

    # ------------------------------------------------------------------ 数据库维护
    _AUTO_BACKUP_KINDS = ('sync', 'download', 'sync_download', 'sync_artists', 'download_artists',
                          'sync_download_artists', 'sync_artist', 'download_artist', 'sync_download_artist')

    def _on_job_start(self, job):
        """同步/下载开始前，如果距离上次备份已超过设定天数，先自动备份一次数据库。"""
        if job.kind not in self._AUTO_BACKUP_KINDS:
            return
        from pixiv_dl import dbtools
        try:
            path = dbtools.auto_backup_if_due(Config.DB_PATH, getattr(Config, 'DB_AUTO_BACKUP_DAYS', 0),
                                              getattr(Config, 'DB_BACKUP_KEEP', 5), conn=self.db.conn)
        except Exception as e:
            logger.warning(f"自动备份数据库失败（不影响本次任务）: {e}")
            job.log(f"自动备份数据库失败: {e}")
            return
        if path:
            logger.info(f"已自动备份数据库: {path}")
            job.log(f"已自动备份数据库 → {os.path.basename(path)}")

    @job_op('db_vacuum')
    def db_vacuum(self):
        """压缩数据库：先做一份备份，再 VACUUM 回收空间。"""
        from pixiv_dl import dbtools
        job = self.job
        job.set(phase="备份数据库")
        backup = dbtools.create_backup(Config.DB_PATH, 'pre_vacuum', conn=self.db.conn)
        job.log(f"压缩前备份: {os.path.basename(backup)}")
        job.set(phase="压缩数据库（期间页面可能短暂无响应）")
        before, after = dbtools.vacuum(Config.DB_PATH)
        job.set(result={'size_before': before, 'size_after': after, 'saved': before - after, 'backup': os.path.basename(backup)})
        job.log(f"压缩完成：{self._format_size(before)} → {self._format_size(after)}")
        return before, after

    # ------------------------------------------------------------------ 运行历史
    def _on_job_finished(self, job):
        """顶层任务结束时把结果存进 runs 表（前端「最近任务」和结果卡片读它）。"""
        if job.kind == "idle":
            return
        job.run_id = self.db.record_run(job.snapshot(with_logs=False))

    # ------------------------------------------------------------------ 组合操作
    @job_op('sync_download')
    def sync_and_download(self, deep=False, limit=None):
        self.sync(deep=deep)
        if interrupt.is_set():
            return
        self.download(limit=limit)

    @job_op('sync_artist_download')
    def sync_and_download_artist(self, aid):
        self.sync(aid=aid, deep=True)
        if interrupt.is_set():
            return
        self.download(aid=aid)

    @job_op('sync_download_artists')
    def sync_and_download_artists(self, aids):
        self.sync(aids=aids, deep=False)
        if interrupt.is_set():
            return
        self.download(aids=aids)

    # ------------------------------------------------------------------ 手动添加作品
    def add_illust(self, illust_id):
        """按作品 ID 加入下载队列（作品所属画师会一并登记）。返回新增页面数。"""
        self.ensure_clients()
        client = self.get_main_client() or self.get_any_client()
        if client is None:
            raise RuntimeError("没有可用的账号")
        res, err = client.call(client.api.illust_detail, illust_id)
        if err:
            raise RuntimeError(f"获取作品 {illust_id} 失败: {err.message}")
        ill = _g(res, 'illust')
        if not ill or not is_visible(ill):
            raise RuntimeError(f"作品 {illust_id} 不存在或不可见")
        user = _g(ill, 'user')
        aid = int(_g(user, 'id'))
        self.db.upsert_artist(aid, _g(user, 'name'), is_deleted=0)
        before = self.db.conn.execute("SELECT COUNT(*) FROM illusts WHERE illust_id=?", (illust_id,)).fetchone()[0]
        self._save_illust(client, self.db, aid, ill)
        after = self.db.conn.execute("SELECT COUNT(*) FROM illusts WHERE illust_id=?", (illust_id,)).fetchone()[0]
        return after - before if after > before else after

    # ------------------------------------------------------------------ 维护
    def clean_temp(self, older_than_days=None):
        """清理本地临时目录里过期的文件，返回删除数量。"""
        days = older_than_days if older_than_days is not None else getattr(Config, 'TEMP_CLEAN_DAYS', 7)
        cutoff = time.time() - days * 86400
        base = Config.LOCAL_TEMP_PATH
        removed = 0
        if not os.path.exists(base):
            return removed
        for root, dirs, files in os.walk(base, topdown=False):
            for f in files:
                fp = os.path.join(root, f)
                try:
                    if os.path.getmtime(fp) < cutoff:
                        os.remove(fp)
                        removed += 1
                except OSError:
                    continue
            for d in dirs:
                dp = os.path.join(root, d)
                try:
                    if not os.listdir(dp):
                        os.rmdir(dp)
                except OSError:
                    continue
        logger.info(f"已清理临时文件: {removed} 个")
        return removed

    def reclaim_stuck_tasks(self):
        try:
            hours = getattr(Config, 'IN_PROGRESS_TIMEOUT_HOURS', 6)
            res = self.db.reset_stuck_tasks(hours)
            logger.info(f"回收卡住任务: 已恢复 {res['reclaimed']} 个, 永久失败 {res['permanent_failed']} 个 (阈值 {hours} 小时)")
            return res
        except Exception as e:
            logger.error(f"回收卡住任务失败: {e}")
            return {'reclaimed': 0, 'permanent_failed': 0}

    def list_failed_tasks(self, author_id=None, attempts_lt=None, limit=None):
        return self.db.get_failed_tasks(author_id, attempts_lt, limit)

    def export_failed_tasks(self, out_path=None):
        out = out_path or os.path.abspath(f"failures_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
        return self.db.export_failed_tasks_csv(out)

    def retry_failed_tasks(self, author_id=None):
        return self.db.reset_failed_tasks_by_filter(author_id)

    def preview_pending(self, limit=20, author_id=None):
        """预览待处理任务（dry-run）。"""
        out = []
        for t in self.db.get_pending_tasks(author_id=author_id, limit=limit, max_attempts=Config.MAX_ATTEMPTS):
            out.append({'task_key': t[0], 'illust_id': t[1], 'page': t[2], 'author_id': t[3],
                        'title': t[4], 'media_type': t[6]})
        return out

    def export_preview_csv(self, out_path, limit=None):
        rows = self.db.get_pending_tasks(limit=limit, max_attempts=Config.MAX_ATTEMPTS)
        with open(out_path, 'w', newline='', encoding='utf-8-sig') as f:
            w = csv.writer(f)
            w.writerow(['task_key', 'illust_id', 'page_index', 'author_id', 'title', 'media_type',
                        'create_date', 'tags', 'page_count'])
            for r in rows:
                w.writerow([r[0], r[1], r[2], r[3], r[4], r[6], r[7], r[8], r[9]])
        return out_path

    # ------------------------------------------------------------------ 存储一致性核查
    @job_op('verify')
    def verify_storage(self, apply=False, author_id=None):
        """对比数据库与存储里的实际文件。

        - 库里 "已下载" 但文件缺失/损坏 → 重置为待下载
        - 库里非 "已下载" 但文件完好 → 标记为已下载
        按画师目录整体列目录（而不是逐文件查询），NAS 上快得多。
        apply=False 只统计不修改（预览）。存储读取失败的画师会被跳过，绝不当作"文件缺失"处理。
        返回统计字典。
        """
        db = self.db
        job = self.job
        storage = self.storage
        storage._list_base_dirs(force=True)  # 存储不可用时直接抛错，避免误判

        tasks = db.get_storage_check_list(author_id)
        by_artist = {}
        for t in tasks:
            by_artist.setdefault(t[3] or 0, []).append(t)
        job.set(total=len(by_artist), phase="核查" if apply else "核查（预览）")

        stats = {'checked': 0, 'ok': 0, 'missing': 0, 'restored': 0, 'skipped_artists': 0}
        to_reset, to_restore, samples = [], [], []
        for aid, ts in by_artist.items():
            if interrupt.is_set():
                break
            job.add(done=1)
            folder = storage.find_artist_folder(aid) if aid else None
            listing, sub = {}, {}
            if folder:
                listing = storage.list_dir(folder)
                if listing is None:
                    stats['skipped_artists'] += 1
                    continue
                if any(t[6] == 'ugoira' for t in ts):
                    sub = storage.list_dir(f"{folder}/动图zip") or {}
            job.set_current("verify", folder or f"[{aid}]")

            def size_of(d, name):
                v = d.get(name)
                return 0 if v is None or v[0] else v[1]

            for key, iid, idx, _aid, _title, url, mt, status in ts:
                stats['checked'] += 1
                if mt == 'ugoira':
                    zsize = max(size_of(sub, f"{iid}_p0.zip"), size_of(sub, f"{iid}_p0.bin"))
                    fsize = zsize if zsize >= 100 else 0
                elif mt == 'novel':
                    fsize = size_of(listing, f"{iid}_p0.txt")
                else:
                    fsize = size_of(listing, f"{iid}_p{idx}.{url_ext(url)}")
                    if fsize < 100:
                        fsize = 0
                exists = fsize > 0 if mt == 'novel' else fsize >= 100
                if exists and status != ST_DONE:
                    stats['restored'] += 1
                    to_restore.append((key, fsize))
                elif not exists and status == ST_DONE:
                    stats['missing'] += 1
                    to_reset.append(key)
                    if len(samples) < 30:
                        samples.append(key)
                else:
                    stats['ok'] += 1
        stats['samples_missing'] = samples
        if apply and not interrupt.is_set():
            db.bulk_set_status(to_reset, 0)
            db.bulk_mark_done(to_restore)
        stats['applied'] = bool(apply and not interrupt.is_set())
        job.set(result=stats)
        summary = (f"核查{'完成' if apply else '预览'}：检查 {stats['checked']}，正常 {stats['ok']}，"
                   f"缺失 {stats['missing']}，找回 {stats['restored']}，跳过画师 {stats['skipped_artists']}")
        logger.info(summary)
        job.log(summary)
        return stats

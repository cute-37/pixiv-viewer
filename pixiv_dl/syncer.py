import json
import logging
import random
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, wait as futures_wait
from types import SimpleNamespace

from pixiv_dl import interrupt
from pixiv_dl.config import Config
from pixiv_dl.database import Database
from pixiv_dl.extract import _g, extract_metadata, extract_pages, is_visible, ugoira_info
from pixiv_dl.pixiv_client import INTERRUPTED, NOT_FOUND
from pixiv_dl.progress import job_op

logger = logging.getLogger("PixivDownloader")


class ArtistGone(Exception):
    """画师账号已注销/不存在（API 明确返回 not found）。"""


class SyncFailed(Exception):
    """同步过程中 API 出错（网络/限速等）。不应该把画师标成注销。"""


def _ref(user, private=False):
    """把 API 的 user 对象（或 DB 行）统一成轻量引用。"""
    return SimpleNamespace(
        id=int(_g(user, 'id')), name=_g(user, 'name') or "",
        account=_g(user, 'account'), comment=_g(user, 'comment'),
        profile_image=_g(_g(user, 'profile_image_urls'), 'medium'),
        is_followed=1 if _g(user, 'is_followed') else 0,
        private=1 if private else 0,
    )


class SyncMixin:
    # ------------------------------------------------------------------ 画师列表
    def _fetch_following(self, client):
        """主账号的关注列表（公开 + 私密），返回 [ref]；第一页就失败时抛 SyncFailed。"""
        artists = []
        for restrict in ("public", "private"):
            qs = {"user_id": client.user_id, "restrict": restrict}
            pages = 0
            while qs:
                res, err = client.call(client.api.user_following, **qs)
                if err:
                    if pages == 0 and restrict == "public":
                        raise SyncFailed(f"获取关注列表失败: {err.message}")
                    logger.warning(f"获取 {restrict} 关注第 {pages + 1} 页失败: {err.message}，使用已获取的部分")
                    break
                pages += 1
                for preview in (_g(res, 'user_previews') or []):
                    artists.append(_ref(_g(preview, 'user'), private=(restrict == "private")))
                qs = client.api.parse_qs(_g(res, 'next_url')) if _g(res, 'next_url') else None
            self.job.log(f"{restrict} 关注获取完成，累计 {len(artists)} 位")
        return artists

    def _db_artist_refs(self, db, deleted=False):
        rows = db.get_deleted_artists() if deleted else db.get_all_artists()
        return [SimpleNamespace(id=r[0], name=r[1] or "", account=None, comment=None, profile_image=None,
                                is_followed=0, private=0) for r in rows]

    def _collect_artists(self, db, clients, aid, aids=None):
        """决定本次要同步的画师。返回 (artists, 主要客户端)。"""
        main = self.get_main_client()
        if aids:
            client = main or next(iter(clients.values()))
            refs = []
            for a in dict.fromkeys(int(x) for x in aids):
                row = db.get_artist_full(a)
                if row:
                    refs.append(SimpleNamespace(id=a, name=row.get('author_name') or "", account=None, comment=None,
                                                profile_image=None, is_followed=0, private=0))
                    continue
                res, err = client.call(client.api.user_detail, a)
                if res:
                    refs.append(_ref(_g(res, 'user')))
                else:
                    self.job.log(f"画师 {a} 获取失败: {err.message if err else '未知'}")
            return refs, client
        if aid:
            client = main or next(iter(clients.values()))
            res, err = client.call(client.api.user_detail, aid)
            if err:
                if err.kind == NOT_FOUND:
                    db.upsert_artist(aid, None, is_deleted=1)
                    raise ArtistGone(f"画师 {aid} 不存在或已注销")
                raise SyncFailed(f"获取画师 {aid} 资料失败: {err.message}")
            return [_ref(_g(res, 'user'), private=False)], client

        if main is None:
            fallback = next(iter(clients.values()))
            logger.warning("主账号不可用，改为同步数据库中已知的画师（使用其余可用账号）")
            self.job.log("主账号不可用，改为同步数据库中已知画师")
            return self._db_artist_refs(db), fallback

        try:
            artists = self._fetch_following(main)
        except SyncFailed as e:
            logger.warning(f"{e}，改为同步数据库中已知的画师")
            self.job.log(f"{e}；改为同步数据库中已知画师")
            return self._db_artist_refs(db), main

        if not artists:
            logger.info("主账号没有关注任何画师，改为同步数据库中已知的画师")
            return self._db_artist_refs(db), main

        # 之前同步失败/被标记注销的画师再给一次机会（成功后标记会被清除）
        seen = {a.id for a in artists}
        retry = [a for a in self._db_artist_refs(db, deleted=True) if a.id not in seen]
        if retry:
            logger.info(f"追加 {len(retry)} 位此前同步失败的画师重试")
            self.job.log(f"追加 {len(retry)} 位此前同步失败的画师重试")
            artists += retry
        return artists, main

    # ------------------------------------------------------------------ 主流程
    @job_op('sync')
    def sync(self, aid=None, deep=False, aids=None):
        """阶段 A：同步画师作品索引（元数据 + 待下载任务）。"""
        db = Database.local(Config.DB_PATH)
        job = self.job
        job.set(phase="认证账号")
        clients = self.ensure_clients()
        if not clients:
            raise RuntimeError("没有可用的账号（请先在「账号」页添加并确认 Token 有效）")
        job.set(phase="获取画师列表")
        artists, main_client = self._collect_artists(db, clients, aid, aids)
        if not artists:
            job.log("没有需要同步的画师")
            return {'artists': 0}

        # 画师在各账号间轮流分配；指定单个画师时只用一个账号
        workers = [(main_client.name, main_client)] if (aid or aids) else list(clients.items())
        buckets = defaultdict(list)
        for i, a in enumerate(artists):
            buckets[workers[i % len(workers)][0]].append(a)
        job.set(phase="同步", total=len(artists))
        job.workers_reset()
        for n, _c in workers:
            if buckets.get(n):
                job.worker_register(n, (Config.MAIN_ACCOUNT_SYNC_THREADS if n == Config.MAIN_ACCOUNT else Config.BACKUP_ACCOUNT_SYNC_THREADS) or 1,
                                    'main' if n == Config.MAIN_ACCOUNT else 'backup')
        job.log(f"共 {len(artists)} 位画师，使用账号: {[n for n, _ in workers]}"
                f"（{'全量' if deep else '增量'}）")

        stats = {'new_tasks_artists': 0, 'gone': 0}

        def run_artist(client, ref):
            wdb = Database.local(Config.DB_PATH)
            job.set_current(client.name, f"[{ref.id}] {ref.name}")
            job.worker_begin(client.name, f"[{ref.id}] {ref.name}")
            try:
                found = self._sync_one(client, wdb, ref, deep)
                job.add(done=1, success=1)
                job.worker_add(client.name, success=1)
                if found and (found['works'] or found['files']):
                    job.tally('new', ref.id, name=found.get('name') or ref.name, works=found['works'],
                              files=found['files'], is_new_artist=1 if found.get('is_new_artist') else 0)
            except ArtistGone:
                wdb.upsert_artist(ref.id, None, is_deleted=1)
                stats['gone'] += 1
                job.add(done=1, failed=1)
                job.worker_add(client.name, failed=1)
                job.tally('failed', ref.id, name=ref.name, note='账号已注销或不存在')
                job.log(f"画师 {ref.id} {ref.name} 已注销/不存在，已标记")
            except InterruptedError:
                raise
            except Exception as e:
                logger.warning(f"同步 {ref.name} (ID:{ref.id}) 失败（账号 {client.name}）: {e}")
                job.add(done=1, failed=1)
                job.worker_add(client.name, failed=1)
                job.tally('failed', ref.id, name=ref.name, note=str(e)[:160])
                job.log(f"同步 {ref.name}({ref.id}) 失败: {e}")
            finally:
                job.worker_end(client.name, 'waiting')

        def account_worker(name, client):
            n = Config.MAIN_ACCOUNT_SYNC_THREADS if name == Config.MAIN_ACCOUNT else Config.BACKUP_ACCOUNT_SYNC_THREADS
            with ThreadPoolExecutor(max_workers=max(1, int(n or 1)), thread_name_prefix=f"sync-{name}") as pool:
                futs = [pool.submit(run_artist, client, a) for a in buckets.get(name, [])]
                futures_wait(futs)
            job.set_current(name, None)
            job.worker_set(name, state='done', text='')

        pool = ThreadPoolExecutor(max_workers=len(workers))
        futs = [pool.submit(account_worker, n, c) for n, c in workers if buckets.get(n)]
        try:
            while True:
                _, not_done = futures_wait(futs, timeout=0.5)
                if not not_done:
                    break
        except KeyboardInterrupt:
            interrupt.set()
            futures_wait(futs)
            raise
        finally:
            pool.shutdown(wait=True)

        new = job.detail.get('new', {})
        summary = {'artists': len(artists), 'artists_ok': job.success, 'artists_failed': job.failed, 'gone': stats['gone'],
                   'new_works': sum(v.get('works', 0) for v in new.values()),
                   'new_files': sum(v.get('files', 0) for v in new.values()),
                   'artists_with_new': len(new)}
        job.set(result={**job.result, **summary})
        s = db.stats()
        logger.info(f"索引同步完成：处理 {len(artists)} 位画师（成功 {job.success}，失败 {job.failed}）。"
                    f"库内作品 {s['works']}，任务 {s['tasks']}，待下载 {s['pending'] + s['failed']}")
        return summary

    # ------------------------------------------------------------------ 单个画师
    def _sync_one(self, client, db, ref, deep):
        api = client.api
        aid, name = ref.id, (ref.name or "").strip()
        is_temp = 0
        existing = db.get_artist_full(aid)
        existing_name = (existing or {}).get('author_name')
        found = {'works': 0, 'files': 0, 'is_new_artist': existing is None}

        if not name:
            res, err = client.call(api.user_detail, aid)
            if err:
                if err.kind == NOT_FOUND:
                    raise ArtistGone(err.message)
                if err.kind == INTERRUPTED:
                    raise InterruptedError()
            else:
                user = _g(res, 'user')
                ref = _ref(user, private=bool(ref.private))
                name = (ref.name or "").strip()
        if not name:
            if existing_name and not (existing or {}).get('is_temp_name'):
                name = existing_name           # 保留库里已有的真实名字，不用临时名覆盖
            else:
                name, is_temp = f"User_{aid}", 1
                logger.warning(f"无法获取画师 {aid} 的名字，使用临时名 {name}")

        db.upsert_artist(
            aid, name, profile_image_url=ref.profile_image, author_account=ref.account,
            author_comment=ref.comment, is_followed=ref.is_followed or None,
            is_private_follow=ref.private if ref.is_followed or ref.private else None,
            is_temp_name=is_temp, is_deleted=0)
        try:
            # 画师改名时同步重命名存储目录；临时名绝不触发重命名
            self.storage.get_artist_folder(aid, self._safe(name), rename=not is_temp)
        except Exception as e:
            logger.debug(f"处理画师目录失败（不影响元数据同步）: {e}")

        watermark = db.get_artist(aid)[2]
        scan_wm = 0 if deep else watermark
        newest = watermark
        first_call = True
        for typ in (Config.SYNC_TYPES or ['illust']):
            top = self._scan_type(client, db, aid, typ, scan_wm, first_call, found)
            first_call = False
            newest = max(newest, top)
        db.mark_artist_synced(aid, newest)

        if getattr(Config, 'SYNC_NOVELS', True):
            found['files'] += self._sync_novels(client, db, aid)
        found['name'] = name
        return found

    def _scan_type(self, client, db, aid, typ, watermark, first_call, found=None):
        """扫描画师某一类作品，返回遇到的最大新作品 ID。API 出错抛 SyncFailed/ArtistGone。"""
        api = client.api
        refresh_limit = int(getattr(Config, 'METADATA_REFRESH_LIMIT', 20))
        old_seen = 0
        newest = 0
        qs = {"user_id": aid, "type": typ}
        while qs:
            if not first_call and self._sleep(random.uniform(*Config.DELAY_SYNC)):
                raise InterruptedError()
            first_call = False
            res, err = client.call(api.user_illusts, **qs)
            if err:
                if err.kind == INTERRUPTED:
                    raise InterruptedError()
                if err.kind == NOT_FOUND and qs.get("user_id") == aid and "offset" not in qs:
                    raise ArtistGone(err.message)
                raise SyncFailed(f"获取 {typ} 作品列表失败: {err.message}")
            stop = False
            for ill in (_g(res, 'illusts') or []):
                if not is_visible(ill):
                    continue
                iid = ill['id'] if isinstance(ill, dict) else ill.id
                if iid <= watermark:
                    old_seen += 1
                    if old_seen > refresh_limit:
                        stop = True
                        break
                else:
                    newest = max(newest, iid)
                n_files, n_work = self._save_illust(client, db, aid, ill)
                if found is not None:
                    found['files'] += n_files
                    found['works'] += 1 if n_work else 0
            next_url = _g(res, 'next_url')
            if stop or not next_url:
                break
            qs = api.parse_qs(next_url)
        return newest

    def _save_illust(self, client, db, aid, ill):
        """保存一个作品，返回 (新增的页面任务数, 是否新作品)。"""
        iid = _g(ill, 'id')
        pages = extract_pages(ill)
        if not pages:
            logger.debug(f"作品 {iid} 没有可下载页面，已跳过")
            return 0, False
        meta = extract_metadata(ill)
        if _g(ill, 'type') == 'ugoira' and not db.get_ugoira_data(iid):
            res, err = client.call(client.api.ugoira_metadata, iid)
            if res and _g(res, 'ugoira_metadata'):
                meta['ugoira_data'] = json.dumps(ugoira_info(_g(res, 'ugoira_metadata')), ensure_ascii=False)
        new_files, new_work = 0, False
        for idx, url, media_type in pages:
            nt, nw = db.save_illust({
                **meta, 'task_key': f"{iid}_{idx}", 'illust_id': iid, 'page_index': idx,
                'author_id': aid, 'title': _g(ill, 'title'), 'url': url, 'media_type': media_type,
            })
            new_files += 1 if nt else 0
            new_work = new_work or nw
        return new_files, new_work

    def _sync_novels(self, client, db, aid):
        """同步小说，返回新增的小说任务数。"""
        api = client.api
        added = 0
        qs = {"user_id": aid}
        while qs:
            res, err = client.call(api.user_novels, **qs)
            if err:
                if err.kind == INTERRUPTED:
                    raise InterruptedError()
                logger.debug(f"小说同步跳过 {aid}: {err.message}")
                return added
            for novel in (_g(res, 'novels') or []):
                if db.save_novel(_g(novel, 'id'), aid, _g(novel, 'title') or ''):
                    added += 1
            next_url = _g(res, 'next_url')
            qs = api.parse_qs(next_url) if next_url else None
            if qs and self._sleep(random.uniform(*Config.DELAY_SYNC)):
                raise InterruptedError()
        return added

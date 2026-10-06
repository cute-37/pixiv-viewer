import json
import logging
import queue
import random
import threading
from concurrent.futures import ThreadPoolExecutor, wait as futures_wait
from datetime import datetime, timedelta
from types import SimpleNamespace

from pixiv_dl import interrupt, ratelimit
from pixiv_dl.config import Config
from pixiv_dl.database import Database
from pixiv_dl.extract import _g, extract_metadata, extract_pages, is_visible, ugoira_info
from pixiv_dl.pixiv_client import AUTH, INTERRUPTED, NOT_FOUND, RATE_LIMIT
from pixiv_dl.progress import job_op

logger = logging.getLogger("PixivDownloader")

GONE_RECHECK_DAYS = 30          # 已注销的画师隔这么久才顺带再确认一次，不是每次检查都重试
SYNC_SCOPES = ('all', 'failed', 'never', 'stale', 'gone')


class ArtistGone(Exception):
    """画师账号已注销/不存在（API 明确返回 not found）。"""


class SyncFailed(Exception):
    """同步过程中 API 出错（网络/限速等）。不应该把画师标成注销。kind 是失败原因的类别。"""

    def __init__(self, message, kind='other'):
        super().__init__(message)
        self.kind = kind


def _fail_kind(err):
    """把接口错误归成界面上分组用的类别"""
    kind = getattr(err, 'kind', None)
    return {RATE_LIMIT: 'rate_limit', AUTH: 'auth', NOT_FOUND: 'gone'}.get(kind, 'network')


def _ref(user, private=False):
    """把 API 的 user 对象（或 DB 行）统一成轻量引用。"""
    return SimpleNamespace(
        id=int(_g(user, 'id')), name=_g(user, 'name') or "",
        account=_g(user, 'account'), comment=_g(user, 'comment'),
        profile_image=_g(_g(user, 'profile_image_urls'), 'medium'),
        is_followed=1 if _g(user, 'is_followed') else 0,
        private=1 if private else 0,
    )


def _db_ref(aid, name):
    return SimpleNamespace(id=int(aid), name=name or "", account=None, comment=None, profile_image=None,
                           is_followed=0, private=0)


class SyncMixin:
    # ------------------------------------------------------------------ 画师列表
    def _fetch_following(self, client):
        """主账号的关注列表（公开 + 私密）。返回 ([ref], 没读全的原因或 None)；第一页就失败时抛 SyncFailed。"""
        artists, incomplete = [], None
        for restrict in ("public", "private"):
            qs = {"user_id": client.user_id, "restrict": restrict}
            pages = 0
            while qs:
                res, err = client.call(client.api.user_following, **qs)
                if err:
                    if err.kind == INTERRUPTED:
                        raise InterruptedError()
                    if pages == 0 and restrict == "public":
                        raise SyncFailed(f"获取关注列表失败: {err.message}", _fail_kind(err))
                    which = "公开" if restrict == "public" else "私密"
                    incomplete = f"{which}关注列表读到第 {pages} 页后失败（{err.message}）"
                    logger.warning(f"获取 {restrict} 关注第 {pages + 1} 页失败: {err.message}，使用已获取的部分")
                    self.job.log(f"关注列表没有读全：{incomplete}。没读到的画师这次按数据库里已知的补上")
                    break
                pages += 1
                for preview in (_g(res, 'user_previews') or []):
                    artists.append(_ref(_g(preview, 'user'), private=(restrict == "private")))
                qs = client.api.parse_qs(_g(res, 'next_url')) if _g(res, 'next_url') else None
            self.job.log(f"{restrict} 关注获取完成，累计 {len(artists)} 位")
        return artists, incomplete

    def _db_artist_refs(self, db, deleted=False):
        rows = db.get_deleted_artists() if deleted else db.get_all_artists()
        return [_db_ref(r[0], r[1]) for r in rows]

    def _collect_artists(self, db, clients, aid, aids=None, scope='all', stale_days=None):
        """决定本次要检查的画师。返回 (artists, 主要客户端, 关注列表没读全的原因或 None)。"""
        main = self.get_main_client()
        any_client = main or next(iter(clients.values()))
        states = db.artist_sync_states()
        if aids:
            refs = []
            for a in dict.fromkeys(int(x) for x in aids):
                row = db.get_artist_full(a)
                if row:
                    refs.append(_db_ref(a, row.get('author_name')))
                    continue
                res, err = any_client.call(any_client.api.user_detail, a)
                if res:
                    refs.append(_ref(_g(res, 'user')))
                else:
                    self.job.log(f"画师 {a} 获取失败: {err.message if err else '未知'}")
            return refs, any_client, None
        if aid:
            res, err = any_client.call(any_client.api.user_detail, aid)
            if err:
                if err.kind == NOT_FOUND:
                    db.upsert_artist(aid, None, is_deleted=1)
                    raise ArtistGone(f"画师 {aid} 不存在或已注销")
                raise SyncFailed(f"获取画师 {aid} 资料失败: {err.message}", _fail_kind(err))
            return [_ref(_g(res, 'user'), private=False)], any_client, None

        # 只查一部分画师：直接从数据库里挑，不去读关注列表
        if scope in ('failed', 'never', 'stale', 'gone'):
            cutoff = (datetime.now() - timedelta(days=float(stale_days or 7))).strftime("%Y-%m-%d %H:%M:%S")
            pick = {
                'failed': lambda s: s['status'] == 'failed' and not s['gone'],
                'never': lambda s: not s['last'] and not s['gone'],
                'stale': lambda s: (not s['last'] or s['last'] < cutoff) and not s['gone'],
                'gone': lambda s: s['gone'],
            }[scope]
            refs = [_db_ref(a, s['name']) for a, s in states.items() if not s['skip'] and pick(s)]
            return refs, any_client, None

        incomplete = None
        if main is None:
            logger.warning("主账号不可用，改为检查数据库中已知的画师（使用其余可用账号）")
            self.job.log("主账号不可用，改为检查数据库中已知画师")
            artists = self._db_artist_refs(db)
        else:
            try:
                artists, incomplete = self._fetch_following(main)
            except SyncFailed as e:
                logger.warning(f"{e}，改为检查数据库中已知的画师")
                self.job.log(f"{e}；改为检查数据库中已知画师")
                artists, incomplete = [], str(e)
            if incomplete or not artists:
                # 没读到的那部分用数据库里已知的关注补上，不让他们悄悄漏掉
                seen = {a.id for a in artists}
                artists += [a for a in self._db_artist_refs(db) if a.id not in seen]

        seen = {a.id for a in artists}
        # 被标记“已注销”的画师不在关注列表里：隔一段时间才顺带再确认一次（成功后标记会被清除）
        cutoff = (datetime.now() - timedelta(days=GONE_RECHECK_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
        retry = [a for a in self._db_artist_refs(db, deleted=True)
                 if a.id not in seen and (states.get(a.id, {}).get('error_time') or '') < cutoff]
        if retry:
            self.job.log(f"顺带再确认 {len(retry)} 位此前被标记为已注销的画师")
            artists += retry
        artists = [a for a in artists if not states.get(a.id, {}).get('skip')]
        return artists, any_client, incomplete

    # ------------------------------------------------------------------ 主流程
    @job_op('sync')
    def sync(self, aid=None, deep=False, aids=None, scope='all', stale_days=None, resume_since=None,
             accounts=None, backfill=None):
        """阶段 A：检查画师有没有新作品（记下作品信息 + 待下载的文件）。

        scope：all 关注的全部 / failed 上次失败的 / never 从未查成功的 / stale 超过 stale_days 天没查的 / gone 复核已注销的。
        resume_since：只查上次检查时间早于它的（接着上次没查完的继续）。accounts：只用这几个账号。
        backfill：这些类型（illust / manga）从头扫一遍，把以前没记录的旧作品补上。
        """
        db = Database.local(Config.DB_PATH)
        job = self.job
        job.set(phase="认证账号")
        clients = self.ensure_clients()
        if accounts:
            clients = {n: c for n, c in clients.items() if n in set(accounts)}
        if not clients:
            raise RuntimeError("没有可用的账号（请先在「账号」页添加并确认 Token 有效）")
        self._avatar_have = None              # 头像文件夹的情况每次检查重新看一遍
        job.set(phase="获取画师列表")
        artists, main_client, incomplete = self._collect_artists(db, clients, aid, aids, scope, stale_days)
        states = db.artist_sync_states()
        if resume_since and not (aid or aids):
            before = len(artists)
            artists = [a for a in artists if (states.get(a.id, {}).get('last') or '') < str(resume_since)]
            job.log(f"接着上次没查完的继续：还有 {len(artists)} 位（另外 {before - len(artists)} 位上次已经查过）")
        if not artists:
            job.log("没有需要检查的画师")
            job.set(result={**job.result, 'artists': 0})
            return {'artists': 0}
        # 最久没查的排在前面：中途停下或被限速时，下次不会总是同一批画师轮不到
        if not (aid or aids):
            artists.sort(key=lambda a: states.get(a.id, {}).get('last') or '')

        # 指定单个 / 一批画师时只用一个账号；否则各账号一起从同一个队列里取画师
        workers = [(main_client.name, main_client)] if (aid or aids) else list(clients.items())
        todo = queue.Queue()
        for a in artists:
            todo.put(a)
        job.set(phase="同步", total=len(artists))
        job.workers_reset()
        threads = {}
        for n, _c in workers:
            threads[n] = max(1, int((Config.MAIN_ACCOUNT_SYNC_THREADS if n == Config.MAIN_ACCOUNT
                                     else Config.BACKUP_ACCOUNT_SYNC_THREADS) or 1))
            job.worker_register(n, threads[n], 'main' if n == Config.MAIN_ACCOUNT else 'backup')
        job.log(f"共 {len(artists)} 位画师，使用账号: {[n for n, _ in workers]}（{'全量' if deep else '增量'}）")
        if incomplete:
            job.tally('notes', 'following', name='关注列表没有读全', note=incomplete)

        stats = {'gone': 0, 'unchecked': 0}
        stats_lock = threading.Lock()
        backfill = [t for t in (backfill or []) if t in ('illust', 'manga')]

        def run_artist(client, ref):
            """查一位画师。返回 False 表示这个账号这次没法再用了（被限速太久），画师已经放回队列。"""
            wdb = Database.local(Config.DB_PATH)
            label = f"[{ref.id}] {ref.name}"
            job.set_current(client.name, label)
            job.worker_begin(client.name, label)
            try:
                found = self._sync_one(client, wdb, ref, deep, backfill)
                job.add(done=1, success=1)
                job.worker_add(client.name, success=1)
                if found and (found['works'] or found['files']):
                    job.tally('new', ref.id, name=found.get('name') or ref.name, works=found['works'],
                              files=found['files'], old=found.get('old', 0),
                              is_new_artist=1 if found.get('is_new_artist') else 0)
                for note in found.get('notes') or []:
                    job.tally('partial', ref.id, name=found.get('name') or ref.name, note=note)
            except ArtistGone:
                wdb.upsert_artist(ref.id, None, is_deleted=1)
                wdb.mark_artist_sync_failed(ref.id, 'gone', '账号已注销或不存在')
                with stats_lock:
                    stats['gone'] += 1
                job.add(done=1, failed=1)
                job.worker_add(client.name, failed=1)
                job.tally('failed', ref.id, name=ref.name, note='账号已注销或不存在', kind='gone')
                job.log(f"画师 {ref.id} {ref.name} 已注销/不存在，已标记")
            except InterruptedError:
                raise
            except SyncFailed as e:
                if e.kind == 'rate_limit' and ratelimit.gate.exhausted(client.name):
                    todo.put(ref)                 # 不算失败：留给别的账号，或者留到下次
                    return False
                self._sync_failed(wdb, client, ref, e.kind, str(e))
            except Exception as e:
                self._sync_failed(wdb, client, ref, 'other', f"{type(e).__name__}: {e}")
            finally:
                job.set_current(client.name, None)
                job.worker_end(client.name, 'waiting')
            return True

        def thread_main(name, client):
            try:
                while not interrupt.is_set():
                    if interrupt.wait_if_paused() or ratelimit.gate.wait(name):
                        return
                    if ratelimit.gate.exhausted(name):
                        job.worker_set(name, state='stopped', text='', note='被限速太久，这次不再使用')
                        return
                    try:
                        ref = todo.get_nowait()
                    except queue.Empty:
                        return
                    if not run_artist(client, ref):
                        job.worker_set(name, state='stopped', text='', note='被限速太久，这次不再使用')
                        return
                    # 画师之间也留间隔（以前只在同一位画师翻页时才等）
                    if self._sleep(random.uniform(*Config.DELAY_SYNC)):
                        return
            except InterruptedError:
                return
            finally:
                job.worker_exit(name)

        pool = ThreadPoolExecutor(max_workers=sum(threads.values()), thread_name_prefix="sync")
        futs = [pool.submit(thread_main, n, c) for n, c in workers for _ in range(threads[n])]
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

        stats['unchecked'] = todo.qsize()
        if stats['unchecked'] and not interrupt.is_set():
            job.log(f"还有 {stats['unchecked']} 位画师这次没有查到（账号被限速太久）。可以稍后用“接着上次没查完的”继续")
        new = job.detail.get('new', {})
        summary = {'artists': len(artists), 'artists_ok': job.success, 'artists_failed': job.failed, 'gone': stats['gone'],
                   'unchecked': stats['unchecked'], 'following_incomplete': incomplete or '',
                   'new_works': sum(v.get('works', 0) for v in new.values()),
                   'new_files': sum(v.get('files', 0) for v in new.values()),
                   'old_files': sum(v.get('old', 0) for v in new.values()),
                   'artists_with_new': len(new), 'rate_limit': ratelimit.gate.summary()}
        job.set(result={**job.result, **summary})
        s = db.stats()
        logger.info(f"检查完成：处理 {len(artists)} 位画师（成功 {job.success}，失败 {job.failed}，没查到 {stats['unchecked']}）。"
                    f"库内作品 {s['works']}，任务 {s['tasks']}，待下载 {s['pending'] + s['failed']}")
        return summary

    def _sync_failed(self, db, client, ref, kind, message):
        logger.warning(f"检查 {ref.name} (ID:{ref.id}) 失败（账号 {client.name}）: {message}")
        db.upsert_artist(ref.id, ref.name or None)
        db.mark_artist_sync_failed(ref.id, kind, message)
        self.job.add(done=1, failed=1)
        self.job.worker_add(client.name, failed=1)
        self.job.tally('failed', ref.id, name=ref.name, note=str(message)[:160], kind=kind)
        self.job.log(f"检查 {ref.name}({ref.id}) 失败: {message}")

    # ------------------------------------------------------------------ 单个画师
    def _sync_one(self, client, db, ref, deep, backfill=()):
        api = client.api
        aid, name = ref.id, (ref.name or "").strip()
        is_temp = 0
        existing = db.get_artist_full(aid)
        existing_name = (existing or {}).get('author_name')
        found = {'works': 0, 'files': 0, 'old': 0, 'notes': [], 'is_new_artist': existing is None}

        if not name:
            res, err = client.call(api.user_detail, aid)
            if err:
                if err.kind == NOT_FOUND:
                    raise ArtistGone(err.message)
                if err.kind == INTERRUPTED:
                    raise InterruptedError()
                if err.kind == RATE_LIMIT:
                    raise SyncFailed(f"获取画师资料失败: {err.message}", 'rate_limit')
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
        self._sync_avatar(db, aid, ref.profile_image, (existing or {}).get('profile_image_url'))
        try:
            # 画师改名时同步重命名存储目录；临时名绝不触发重命名
            self.storage.get_artist_folder(aid, self._safe(name), rename=not is_temp)
        except Exception as e:
            logger.debug(f"处理画师目录失败（不影响元数据同步）: {e}")

        # 每种类型各记各的进度。以前插画和漫画共用一个进度，后加的类型只能补到最近几个旧作品。
        marks = db.get_artist_marks(aid)
        new_marks, newest = {}, max(marks.values() or [0])
        first_call = True
        for typ in (Config.SYNC_TYPES or ['illust']):
            mark = marks.get(typ, 0)
            scan_from = 0 if (deep or typ in backfill) else mark
            top = self._scan_type(client, db, aid, typ, scan_from, first_call, found, mark)
            first_call = False
            new_marks[typ] = max(mark, top)
            newest = max(newest, top)
        db.mark_artist_synced(aid, newest, marks=new_marks)

        if getattr(Config, 'SYNC_NOVELS', True):
            found['files'] += self._sync_novels(client, db, aid, found)
        found['name'] = name
        return found

    def _sync_avatar(self, db, aid, url, old_url):
        """检查时顺带保持头像是新的：本地没有，或者画师换了头像，就下载。失败不影响检查。"""
        if not url:
            return
        try:
            have = getattr(self, '_avatar_have', None)
            if have is None:
                have = self._avatar_have = self.avatar_ids()
            changed = bool(old_url) and old_url != url
            if aid in have and not changed:
                return
            path = self.download_artist_avatar(aid, url, force=changed)
            if path:
                have.add(aid)
                db.upsert_artist(aid, None, profile_image_local=path)
        except Exception as e:
            logger.debug(f"下载画师 {aid} 头像失败（不影响检查）: {e}")

    def _scan_type(self, client, db, aid, typ, watermark, first_call, found=None, mark=None):
        """扫描画师某一类作品，返回遇到的最大新作品 ID。API 出错抛 SyncFailed/ArtistGone。

        watermark 是这次扫到哪为止（0 = 从头扫）；mark 是这种类型真正的检查进度，用来区分“新发的”和“补进来的旧作品”。
        """
        api = client.api
        mark = watermark if mark is None else mark
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
                raise SyncFailed(f"获取 {typ} 作品列表失败: {err.message}", _fail_kind(err))
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
                origin = 'old' if (mark and iid <= mark) else 'new'
                n_files, n_work = self._save_illust(client, db, aid, ill, origin)
                if found is not None:
                    found['files'] += n_files
                    found['works'] += 1 if n_work else 0
                    if origin == 'old':
                        found['old'] = found.get('old', 0) + n_files
            next_url = _g(res, 'next_url')
            if stop or not next_url:
                break
            qs = api.parse_qs(next_url)
        return newest

    def _save_illust(self, client, db, aid, ill, origin='new'):
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
            elif err and err.kind == INTERRUPTED:
                raise InterruptedError()
        new_files, new_work = 0, False
        for idx, url, media_type in pages:
            nt, nw = db.save_illust({
                **meta, 'task_key': f"{iid}_{idx}", 'illust_id': iid, 'page_index': idx,
                'author_id': aid, 'title': _g(ill, 'title'), 'url': url, 'media_type': media_type,
                'origin': origin,
            })
            new_files += 1 if nt else 0
            new_work = new_work or nw
        return new_files, new_work

    def _sync_novels(self, client, db, aid, found=None):
        """同步小说，返回新增的小说任务数。限速时算这位画师检查失败（下次重查）；别的错误记一笔“没读全”。"""
        api = client.api
        added = 0
        qs = {"user_id": aid}
        while qs:
            res, err = client.call(api.user_novels, **qs)
            if err:
                if err.kind == INTERRUPTED:
                    raise InterruptedError()
                if err.kind == RATE_LIMIT:
                    raise SyncFailed(f"获取小说列表失败: {err.message}", 'rate_limit')
                if err.kind != NOT_FOUND:
                    logger.warning(f"画师 {aid} 的小说列表没有读到: {err.message}")
                    if found is not None:
                        found.setdefault('notes', []).append(f"小说列表没有读到（{err.message[:80]}）")
                return added
            for novel in (_g(res, 'novels') or []):
                if db.save_novel(_g(novel, 'id'), aid, _g(novel, 'title') or ''):
                    added += 1
            next_url = _g(res, 'next_url')
            qs = api.parse_qs(next_url) if next_url else None
            if qs and self._sleep(random.uniform(*Config.DELAY_SYNC)):
                raise InterruptedError()
        return added

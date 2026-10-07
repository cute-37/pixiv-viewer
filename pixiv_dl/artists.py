import logging
import os
import random
import tempfile
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor

from pixiv_dl import interrupt
from pixiv_dl.config import Config
from pixiv_dl.database import Database
from pixiv_dl.extract import _g
from pixiv_dl.pixiv_client import INTERRUPTED, NOT_FOUND
from pixiv_dl.progress import job_op

logger = logging.getLogger("PixivDownloader")

_AVATAR_EXTS = ('jpg', 'jpeg', 'png', 'gif', 'webp')
AVATAR_THREADS = 4            # 直接按地址下载头像时同时下几个（图片服务器，不占账号的接口请求）
_tls = threading.local()      # 每个线程最近一次读画师资料失败的原因


class ArtistMixin:
    """画师资料 / 头像。"""
    _last_detail_error = ''

    def fetch_artist_detail(self, aid, client=None):
        """调用 user_detail，返回规整后的 dict；失败返回 None。画师不存在时返回 {'_gone': True}。

        client：用哪个账号去问；不给就用主账号（没有主账号时用任意一个可用的）。
        """
        _tls.error = ''
        client = client or self.get_main_client() or self.get_any_client()
        if client is None:
            logger.error("无法获取画师详情：没有可用账号")
            return None
        res, err = client.call(client.api.user_detail, aid)
        if err:
            if err.kind == NOT_FOUND:
                return {'_gone': True}
            if err.kind == INTERRUPTED:
                raise InterruptedError()
            logger.warning(f"获取画师 {aid} 详情失败: {err.message}")
            self._last_detail_error = _tls.error = err.message
            return None
        user, profile = _g(res, 'user'), _g(res, 'profile')
        if not user:
            return None
        urls = _g(user, 'profile_image_urls') or {}
        data = {
            'author_id': _g(user, 'id'), 'author_name': _g(user, 'name'),
            'author_account': _g(user, 'account'), 'author_comment': _g(user, 'comment'),
            'is_followed': 1 if _g(user, 'is_followed') else 0,
            'profile_image_url': _g(urls, 'medium') or _g(urls, 'large') or (list(urls.values())[0] if urls else None),
        }
        if profile:
            for k in ('webpage', 'twitter_account', 'gender', 'birth', 'region', 'job', 'pawoo_url',
                      'total_illusts', 'background_image_url'):
                data[k] = _g(profile, k)
            data['total_bookmarks'] = _g(profile, 'total_illust_bookmarks_public')
        return data

    def download_artist_avatar(self, aid, profile_image_url, force=False):
        """下载头像到 avatars/，返回库里使用的相对路径 `avatars/<id>.<ext>`；失败返回 None。"""
        if not profile_image_url:
            return None
        os.makedirs(Config.AVATARS_PATH, exist_ok=True)
        ext = profile_image_url.split('?')[0].rsplit('.', 1)[-1].lower()
        if ext not in _AVATAR_EXTS:
            ext = 'jpg'
        filename = f"{aid}.{ext}"
        filepath = os.path.join(Config.AVATARS_PATH, filename)
        if not force and os.path.exists(filepath) and os.path.getsize(filepath) > 100:
            return f"avatars/{filename}"
        try:
            resp = self.session.get(profile_image_url, timeout=30)
            if resp.status_code != 200 or len(resp.content) < 100:
                logger.warning(f"下载画师 {aid} 头像失败: HTTP {resp.status_code}")
                return None
            fd, tmp = tempfile.mkstemp(dir=Config.AVATARS_PATH, suffix=".tmp")
            with os.fdopen(fd, 'wb') as f:
                f.write(resp.content)
            os.replace(tmp, filepath)
            return f"avatars/{filename}"
        except Exception as e:
            logger.warning(f"下载画师 {aid} 头像出错: {e}")
            return None

    def refresh_artist_profile(self, aid, client=None):
        """刷新单个画师的资料并下载头像。成功返回 True。"""
        db = Database.local(Config.DB_PATH)
        detail = self.fetch_artist_detail(aid, client)
        if not detail:
            return False
        if detail.get('_gone'):
            db.upsert_artist(aid, None, is_deleted=1)
            return False
        avatar = self.download_artist_avatar(aid, detail.get('profile_image_url'))
        fields = {k: v for k, v in detail.items()
                  if k not in ('author_id', 'author_name', '_gone') and v is not None}
        if avatar:
            fields['profile_image_local'] = avatar
        fields['is_deleted'] = 0
        db.upsert_artist(aid, detail.get('author_name'), **fields)
        return True

    @job_op('refresh_profiles')
    def refresh_all_artist_profiles(self, only_missing=True, limit=None):
        db = Database.local(Config.DB_PATH)
        job = self.job
        if only_missing:
            artists = db.get_artists_without_profile(limit=limit)
        else:
            artists = db.get_all_artists()
            if limit:
                artists = artists[:int(limit)]
        if not artists:
            job.log("没有需要更新的画师")
            return 0, 0
        self.ensure_clients()
        job.set(total=len(artists), phase="刷新画师资料")
        for a in artists:
            if interrupt.is_set():
                break
            aid, name = a[0], a[1]
            job.set_current("profile", f"[{aid}] {name}")
            try:
                ok = self.refresh_artist_profile(aid)
            except InterruptedError:
                break
            except Exception as e:
                logger.warning(f"刷新画师 {aid} 资料失败: {e}")
                ok = False
            job.add(done=1, **({'success': 1} if ok else {'failed': 1}))
            if self._sleep(random.uniform(0.5, 1.5)):
                break
        job.set_current("profile", None)
        return job.success, job.failed

    def avatar_files(self):
        """头像文件夹里实际有的头像：{画师编号: 文件名}（看的是文件，不是数据库里的记录）"""
        have = {}
        try:
            for entry in os.scandir(Config.AVATARS_PATH):
                stem, _, ext = entry.name.rpartition('.')
                if stem.isdigit() and ext.lower() in _AVATAR_EXTS and entry.is_file() and entry.stat().st_size > 100:
                    have[int(stem)] = entry.name
        except OSError:
            pass
        return have

    def avatar_ids(self):
        """头像文件夹里已经有头像的画师编号"""
        return set(self.avatar_files())

    def check_avatars(self):
        """核对头像：逐个画师看头像文件夹里是不是真的有文件，并把数据库里的记录改成和实际一致。

        不访问 Pixiv。数据库是从别处导入的话，记录里常常写着“有”，文件却不在这里——核对之后就准了。
        返回 {artists, have, missing, fixed, items: [{id, name}]}（已注销的画师不算在内）。
        """
        db = Database.local(Config.DB_PATH)
        files = self.avatar_files()
        rows = db.conn.execute(
            "SELECT author_id, author_name, profile_image_local FROM artists WHERE COALESCE(is_deleted,0) = 0 "
            "ORDER BY author_id").fetchall()
        missing, fixes = [], []
        for aid, name, local in rows:
            actual = f"avatars/{files[aid]}" if aid in files else ''
            if not actual:
                missing.append({'id': aid, 'name': name or f"画师 {aid}"})
            if (local or '') != actual:
                fixes.append((actual, aid))
        if fixes:
            with db.tx() as c:
                c.executemany("UPDATE artists SET profile_image_local = ? WHERE author_id = ?", fixes)
        return {'artists': len(rows), 'have': len(rows) - len(missing), 'missing': len(missing),
                'fixed': len(fixes), 'items': missing[:500]}

    @job_op('download_avatars')
    def download_missing_avatars(self, limit=None, force=False):
        """补全头像。缺不缺看头像文件夹里实际有没有文件——数据库是从别处导入的话，记录里写着“有”，文件却不在这里。

        分两步：
        1. 数据库里记着头像地址的，直接按地址下载。这一步访问的是图片服务器，不占任何账号的接口请求，几个一起下。
        2. 地址没有或已经失效的，要向 Pixiv 问一次最新的资料。这一步由所有可用的账号分着做（每个账号一个线程）；
           哪个账号被限速太久就先退出，剩下的由别的账号接着做。
        force=True：不管有没有，全部重新下载一遍（画师换了头像时用）。
        """
        from pixiv_dl import ratelimit
        db = Database.local(Config.DB_PATH)
        job = self.job
        have = set() if force else self.avatar_ids()
        artists = [tuple(r) for r in db.conn.execute(
            "SELECT author_id, author_name, profile_image_url FROM artists WHERE COALESCE(is_deleted,0) = 0 "
            "ORDER BY author_id") if r[0] not in have]
        if limit:
            artists = artists[:int(limit)]
        if not artists:
            job.log("所有画师都已有头像")
            job.set(result={'avatars': 0, 'missing_before': 0})
            return 0, 0
        self.ensure_clients()
        main = self.get_main_client()
        clients = ([main] if main is not None else []) + [c for c in self.clients.values() if c.authed and c is not main]
        job.set(total=len(artists), phase="下载头像")
        job.log(f"{'重新下载' if force else '缺少'} {len(artists)} 位画师的头像")
        lock = threading.Lock()
        ask = deque()                           # 第一步没下成的：要向 Pixiv 问最新的地址

        def stopped():
            return interrupt.wait_if_paused() or interrupt.is_set()

        def fail(aid, name, reason):
            job.add(done=1, failed=1)
            job.tally('failed', aid, name=name or f"画师 {aid}", note=str(reason)[:160], kind='avatar')

        def direct(row):
            aid, name, url = row
            if stopped():
                return
            path = None
            try:
                path = self.download_artist_avatar(aid, url, force=force) if url else None
                if path:
                    Database.local(Config.DB_PATH).upsert_artist(aid, None, profile_image_local=path)
            except Exception as e:
                logger.warning(f"下载画师 {aid} 头像失败: {e}")
            if path:
                job.set_current("avatar", f"[{aid}] {name}")
                job.add(done=1, success=1)
            else:
                with lock:
                    ask.append(row)
            self._sleep(random.uniform(0.05, 0.2))

        with ThreadPoolExecutor(max_workers=AVATAR_THREADS, thread_name_prefix="avatar") as pool:
            list(pool.map(direct, artists))

        def by_account(client):
            while not stopped():
                if ratelimit.gate.exhausted(client.name):
                    job.log(f"账号 {client.name} 被限速太久，先不用它补头像")
                    return
                with lock:
                    if not ask:
                        return
                    aid, name, _ = ask.popleft()
                job.set_current("avatar", f"[{aid}] {name}")
                try:
                    if self.refresh_artist_profile(aid, client):
                        ok, reason = aid in self.avatar_ids_of([aid]), '头像文件没有下载下来'
                    else:
                        ok, reason = False, getattr(_tls, 'error', '') or '读不到这位画师的资料'
                except InterruptedError:
                    return
                except Exception as e:
                    logger.warning(f"下载画师 {aid} 头像失败: {e}")
                    ok, reason = False, f"{type(e).__name__}: {e}"
                if ok:
                    job.add(done=1, success=1)
                else:
                    fail(aid, name, reason)
                if self._sleep(random.uniform(0.5, 1.2)):
                    return

        if ask and not interrupt.is_set():
            if not clients:
                for aid, name, _ in list(ask):
                    fail(aid, name, '没有可用的账号，没法向 Pixiv 要最新的头像地址')
                ask.clear()
            else:
                if len(clients) > 1:
                    job.log(f"{len(ask)} 个头像要向 Pixiv 问最新的地址，由 {len(clients)} 个账号分着做")
                workers = [threading.Thread(target=by_account, args=(c,), name=f"avatar-{c.name}", daemon=True)
                           for c in clients]
                for w in workers:
                    w.start()
                for w in workers:
                    w.join()
                if ask and not interrupt.is_set():
                    job.log("账号被限速太久，剩下的头像留到下次再补")
        job.set_current("avatar", None)
        job.set(result={'avatars': job.success, 'avatars_failed': job.failed, 'not_done': max(0, len(artists) - job.done)})
        return job.success, job.failed

    def avatar_ids_of(self, ids):
        have = self.avatar_ids()
        return {a for a in ids if a in have}

    def view_artist_profile(self, aid):
        """CLI：打印画师资料。"""
        db = Database.local(Config.DB_PATH)
        a = db.get_artist_full(aid)
        if not a:
            print(f"未找到画师 {aid} 的信息")
            return None
        g = lambda k, d='未知': a.get(k) or d
        print(f"\n{'=' * 50}\n 画师资料 - ID: {a['author_id']}\n{'=' * 50}")
        print(f" 昵称: {g('author_name')}\n 账号: {g('author_account')}")
        print(f" 关注: {'是' if a.get('is_followed') else '否'} {'(悄悄关注)' if a.get('is_private_follow') else ''}")
        print(f" 状态: {'已注销/同步失败' if a.get('is_deleted') else '正常'}")
        print(f" 作品数: {g('total_illusts')}  收藏数: {g('total_bookmarks')}")
        comment = a.get('author_comment') or '(无)'
        print(f" 简介: {comment[:200]}{'...' if len(comment) > 200 else ''}")
        print(f" 性别: {g('gender')}  生日: {g('birth')}  地区: {g('region')}  职业: {g('job')}")
        print(f" Twitter: {g('twitter_account', '无')}  主页: {g('webpage', '无')}  Pawoo: {g('pawoo_url', '无')}")
        print(f" 本地头像: {g('profile_image_local', '未下载')}\n{'=' * 50}")
        return a

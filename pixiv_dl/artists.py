import logging
import os
import random
import tempfile

from pixiv_dl import interrupt
from pixiv_dl.config import Config
from pixiv_dl.database import Database
from pixiv_dl.extract import _g
from pixiv_dl.pixiv_client import INTERRUPTED, NOT_FOUND
from pixiv_dl.progress import job_op

logger = logging.getLogger("PixivDownloader")

_AVATAR_EXTS = ('jpg', 'jpeg', 'png', 'gif', 'webp')


class ArtistMixin:
    """画师资料 / 头像。头像地址一律用 user_detail 现取，不使用库里保存的旧地址。"""

    def fetch_artist_detail(self, aid):
        """调用 user_detail，返回规整后的 dict；失败返回 None。画师不存在时返回 {'_gone': True}。"""
        client = self.get_main_client() or self.get_any_client()
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

    def refresh_artist_profile(self, aid):
        """刷新单个画师的资料并下载头像。成功返回 True。"""
        db = Database.local(Config.DB_PATH)
        detail = self.fetch_artist_detail(aid)
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

    @job_op('download_avatars')
    def download_missing_avatars(self, limit=None):
        """补全缺失的本地头像（会向 API 现取最新头像地址）。"""
        db = Database.local(Config.DB_PATH)
        job = self.job
        artists = db.get_artists_without_avatar(limit=limit)
        # 库里没有头像地址的画师也需要补
        have = {a[0] for a in artists}
        for row in db.conn.execute(
                "SELECT author_id, author_name, profile_image_url FROM artists WHERE "
                "COALESCE(is_deleted,0)=0 AND (profile_image_local IS NULL OR profile_image_local='')"):
            if row[0] not in have:
                artists.append(row)
        if limit:
            artists = artists[:int(limit)]
        if not artists:
            job.log("所有画师都已有头像")
            return 0, 0
        self.ensure_clients()
        job.set(total=len(artists), phase="下载头像")
        for aid, name, _old_url in artists:
            if interrupt.is_set():
                break
            job.set_current("avatar", f"[{aid}] {name}")
            try:
                ok = self.refresh_artist_profile(aid)
            except InterruptedError:
                break
            except Exception as e:
                logger.warning(f"下载画师 {aid} 头像失败: {e}")
                ok = False
            job.add(done=1, **({'success': 1} if ok else {'failed': 1}))
            if self._sleep(random.uniform(0.3, 0.8)):
                break
        job.set_current("avatar", None)
        return job.success, job.failed

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

"""测试用的假 Pixiv API / 假 HTTP 会话。不访问网络。"""
import threading
from urllib.parse import parse_qs, urlparse

from pixiv_dl.pixiv_client import PixivClient


class PJ(dict):
    """模仿 pixivpy 的 ParsedJson：既能 ['k'] 也能 .k，缺失属性返回 None。"""
    __getattr__ = dict.get


def pj(o):
    if isinstance(o, dict):
        return PJ({k: pj(v) for k, v in o.items()})
    if isinstance(o, list):
        return [pj(x) for x in o]
    return o


def err(message, user_message=""):
    return PJ({'error': PJ({'message': message, 'user_message': user_message, 'reason': ''})})


def make_illust(iid, title='t', type='illust', pages=1, visible=True, base='https://new.example/img'):
    ill = {
        'id': iid, 'title': title, 'type': type, 'page_count': pages, 'width': 100, 'height': 100,
        'visible': visible, 'x_restrict': 0, 'sanity_level': 2, 'total_view': 5, 'total_bookmarks': 3,
        'is_bookmarked': False, 'illust_ai_type': 1, 'create_date': '2026-01-01T00:00:00+09:00',
        'tags': [{'name': 'tag1', 'translated_name': 'T1'}], 'caption': 'c', 'tools': [], 'series': None,
        'image_urls': {'square_medium': 'x', 'medium': 'x', 'large': 'x'},
        'meta_single_page': {}, 'meta_pages': [],
    }
    if pages == 1:
        ill['meta_single_page'] = {'original_image_url': f'{base}/{iid}_p0.png'}
    else:
        ill['meta_pages'] = [{'image_urls': {'original': f'{base}/{iid}_p{i}.png'}} for i in range(pages)]
    return ill


class FakeAPI:
    page_size = 2

    def __init__(self):
        self.user_id = 1
        self.users = {}       # aid -> {'name': ..}
        self.following = {'public': [], 'private': []}
        self.illusts = {}     # (aid, type) -> [illust...] 新→旧
        self.details = {}     # iid -> illust dict 或 err(...)
        self.ugoira = {}      # iid -> ugoira_metadata dict 或 err(...)
        self.novels = {}      # aid -> [{'id','title'}]
        self.novel_text = {}  # nid -> text
        self.errors = {}      # (method, key) -> err(...)
        self.calls = []
        self.lock = threading.Lock()

    parse_qs = staticmethod(lambda u: {k: v[-1] for k, v in parse_qs(urlparse(u).query).items()} if u else None)

    def _log(self, name, *a):
        with self.lock:
            self.calls.append((name,) + a)

    def auth(self, refresh_token=None):
        return PJ({'ok': 1})

    def user_detail(self, user_id):
        self._log('user_detail', user_id)
        if ('user_detail', user_id) in self.errors:
            return self.errors[('user_detail', user_id)]
        u = self.users.get(user_id)
        if not u:
            return err('Not Found')
        return pj({'user': {'id': user_id, 'name': u['name'], 'account': u.get('account', 'acc'),
                            'profile_image_urls': {'medium': f'https://fresh.example/avatar/{user_id}.png'},
                            'comment': 'hi', 'is_followed': True},
                   'profile': {'gender': 'male', 'total_illusts': 3, 'total_illust_bookmarks_public': 1}})

    def user_following(self, user_id, restrict='public', offset=None):
        self._log('user_following', restrict)
        if ('user_following', restrict) in self.errors:
            return self.errors[('user_following', restrict)]
        users = self.following[restrict]
        return pj({'user_previews': [{'user': {'id': a, 'name': n, 'is_followed': True}} for a, n in users],
                   'next_url': None})

    def user_illusts(self, user_id, type='illust', offset=None, **kw):
        user_id = int(user_id)  # 真实 API 的 next_url 解析出来的是字符串
        self._log('user_illusts', user_id, type, offset)
        if ('user_illusts', user_id) in self.errors:
            return self.errors[('user_illusts', user_id)]
        items = self.illusts.get((user_id, type), [])
        off = int(offset or 0)
        chunk = items[off:off + self.page_size]
        nxt = None
        if off + self.page_size < len(items):
            nxt = f"https://app-api.pixiv.net/v1/user/illusts?user_id={user_id}&type={type}&offset={off + self.page_size}"
        return pj({'illusts': chunk, 'next_url': nxt})

    def illust_detail(self, illust_id):
        self._log('illust_detail', illust_id)
        d = self.details.get(illust_id)
        if d is None:
            return err('Not Found', '該当作品は削除されたか、存在しない作品IDです。')
        if 'error' in d:
            return d
        return pj({'illust': d})

    def ugoira_metadata(self, illust_id):
        self._log('ugoira_metadata', illust_id)
        d = self.ugoira.get(illust_id)
        if d is None:
            return err('Not Found')
        if 'error' in d:
            return d
        return pj({'ugoira_metadata': d})

    def user_novels(self, user_id, **kw):
        self._log('user_novels', user_id)
        return pj({'novels': self.novels.get(user_id, []), 'next_url': None})

    def webview_novel(self, novel_id, **kw):
        self._log('webview_novel', novel_id)
        if novel_id not in self.novel_text:
            return err('Not Found')
        return pj({'text': self.novel_text[novel_id]})


def make_client(api, name='main', token='tok-main'):
    c = PixivClient.__new__(PixivClient)
    c.name, c.token, c.api = name, token, api
    c.user_id, c.last_error = 1, ''
    c._auth_lock = threading.Lock()
    c._auth_ts = 0.0
    c._authed = True
    return c


class FakeResp:
    def __init__(self, status, data):
        self.status_code = status
        self._data = data
        self.headers = {'content-length': str(len(data))}
        self.content = data

    def iter_content(self, chunk_size=8192):
        for i in range(0, len(self._data), chunk_size):
            yield self._data[i:i + chunk_size]

    def close(self):
        pass


class FakeSession:
    """routes: url -> bytes | int(状态码) | Exception。未登记的 URL 返回 404。"""

    def __init__(self, routes=None):
        self.routes = routes if routes is not None else {}
        self.requested = []
        self.lock = threading.Lock()

    def get(self, url, timeout=None, stream=False):
        with self.lock:
            self.requested.append(url)
        r = self.routes.get(url, 404)
        if isinstance(r, Exception):
            raise r
        if isinstance(r, int):
            return FakeResp(r, b'')
        return FakeResp(200, r)


def make_processor(api=None, routes=None):
    from pixiv_dl.processor import Processor
    pro = Processor()
    api = api or FakeAPI()
    pro.clients = {'main': make_client(api)}
    pro.session = FakeSession(routes)
    return pro, api

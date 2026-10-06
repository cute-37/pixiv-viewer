"""从 Pixiv API 返回对象中提取元数据 / 下载页面。全部是纯函数，不做任何网络请求，便于测试。"""
import json
import re

_TYPE_MAP = {'illust': 0, 'manga': 1, 'ugoira': 2}
_UGOIRA_SIZE_RE = re.compile(r"_ugoira\d+x\d+\.zip")


def _g(obj, key, default=None):
    """同时支持 dict 和属性访问（pixivpy 返回的 ParsedJson 两种都行）。"""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def is_visible(ill):
    """被限制不可见的作品，API 会返回 visible=False 的占位项，没有真实图片。"""
    return _g(ill, 'visible', True) is not False


def extract_metadata(ill):
    """作品级元数据（对应 illust_metadata 表）。不含 ugoira_data。"""
    m = {}
    try:
        for src, dst in (('total_view', 'total_view'), ('total_bookmarks', 'total_bookmarks'),
                         ('illust_ai_type', 'ai_type'), ('page_count', 'page_count'),
                         ('width', 'width'), ('height', 'height'), ('sanity_level', 'sanity_level')):
            v = _g(ill, src)
            if v is not None:
                m[dst] = v
        if _g(ill, 'is_bookmarked') is not None:
            m['is_bookmarked'] = 1 if _g(ill, 'is_bookmarked') else 0

        tags = _g(ill, 'tags')
        if tags is not None:
            names, translated = [], []
            for t in tags:
                if _g(t, 'name'):
                    names.append(_g(t, 'name'))
                if _g(t, 'translated_name'):
                    translated.append(_g(t, 'translated_name'))
            m['tags'] = json.dumps(names, ensure_ascii=False)
            if translated:
                m['tags_translated'] = json.dumps(translated, ensure_ascii=False)

        if _g(ill, 'create_date') is not None:
            m['create_date'] = str(_g(ill, 'create_date'))

        xr = _g(ill, 'x_restrict')
        if xr is not None:
            m['x_restrict'] = xr
            m['is_r18'] = 1 if xr >= 1 else 0
        elif _g(ill, 'sanity_level') is not None:
            m['is_r18'] = 1 if _g(ill, 'sanity_level') >= 6 else 0

        t = _g(ill, 'type')
        if t is not None:
            m['illust_type'] = _TYPE_MAP.get(t, 0)

        series = _g(ill, 'series')
        if series:
            if _g(series, 'id') is not None:
                m['series_id'] = _g(series, 'id')
            if _g(series, 'title') is not None:
                m['series_title'] = _g(series, 'title')

        tools = _g(ill, 'tools')
        if tools:
            m['tools'] = json.dumps(list(tools), ensure_ascii=False)
        if _g(ill, 'caption') is not None:
            m['caption'] = _g(ill, 'caption')
    except Exception:
        # 个别字段异常不应让整个作品丢失，已提取到的字段照常返回
        pass
    return m


def extract_pages(ill):
    """作品的可下载页面：[(page_index, url, media_type)]。

    动图返回占位 `ugoira://<id>`（真实 zip 地址在下载时通过 ugoira_metadata 获取）；
    图片地址是同步时 API 返回的原图地址，仅供参考 —— 下载阶段会重新向 API 获取。
    """
    iid = _g(ill, 'id')
    if _g(ill, 'type') == 'ugoira':
        return [(0, f"ugoira://{iid}", 'ugoira')]

    pages = []
    meta_pages = _g(ill, 'meta_pages') or []
    page_count = _g(ill, 'page_count') or 1
    if page_count > 1 or len(meta_pages) > 1:
        for i, p in enumerate(meta_pages):
            url = _g(_g(p, 'image_urls'), 'original')
            if url:
                pages.append((i, url, 'image'))
        if pages:
            return pages

    url = _g(_g(ill, 'meta_single_page'), 'original_image_url')
    if url:
        return [(0, url, 'image')]
    if meta_pages:
        url = _g(_g(meta_pages[0], 'image_urls'), 'original')
        if url:
            return [(0, url, 'image')]
    return []


def ugoira_info(meta):
    """把 ugoira_metadata 规整成可存库/可转换的 dict：{'zip_url', 'frames': [{'file','delay'}]}"""
    zip_urls = _g(meta, 'zip_urls')
    zip_url = _g(zip_urls, 'original') or _g(zip_urls, 'medium')
    frames = []
    for f in (_g(meta, 'frames') or []):
        frames.append({'file': _g(f, 'file', ''), 'delay': _g(f, 'delay', 100)})
    return {'zip_url': zip_url, 'frames': frames}


def ugoira_zip_candidates(meta, prefer_hq=True):
    """动图 zip 下载候选地址（按优先级）。API 通常只给 600x600，把尺寸改成 1920x1080 可拿到高清版。"""
    base = ugoira_info(meta)['zip_url']
    if not base:
        return []
    candidates = []
    if prefer_hq and _UGOIRA_SIZE_RE.search(base):
        hq = _UGOIRA_SIZE_RE.sub("_ugoira1920x1080.zip", base)
        if hq != base:
            candidates.append(hq)
    candidates.append(base)
    return candidates


def url_ext(url, default='jpg'):
    """取 URL 路径的扩展名（只用于命名文件）。"""
    path = (url or '').split('?')[0]
    name = path.rsplit('/', 1)[-1]
    if '.' not in name:
        return default
    ext = name.rsplit('.', 1)[-1].lower()
    return ext if 1 <= len(ext) <= 5 and ext.isalnum() else default

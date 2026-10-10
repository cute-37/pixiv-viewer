"""Pixiv 接口体检（开发者用）。

软件依赖 Pixiv 的十来个接口，以及每个接口返回内容里的一些字段。Pixiv 哪天改了，下载就会出问题。
这里把这些依赖集中写成一张清单，体检时对每个接口各发一次**只读**请求，对照清单检查：
我们用到的字段还在不在、类型对不对。不下载文件、不改任何设置、不写数据库。

用法：
- 开发者模式下，“设置 → 开发者 → Pixiv 接口体检”；
- 或命令行：python scripts/check_api.py（发版前跑一次）。

体检要用真实的画师 / 作品来问。尽量取账号自己关注的第一位画师和他最新的作品；动图、小说要从数据库里
找一个已经下载过的，找不到就跳过那一项（标成“跳过”，不算失败）。
"""
import logging
import time

from pixiv_dl.extract import _g

logger = logging.getLogger("PixivDownloader")

# 每个接口依赖的字段：(路径, 类型)。路径用 . 分隔；[] 表示“列表里的每一项”（只查第一项）。
# 类型：str / int / list / dict / bool / any（只要有这个键）
CONTRACT = {
    "user_following": [("user_previews", "list"), ("user_previews[].user.id", "int"), ("user_previews[].user.name", "str"),
                       ("user_previews[].user.account", "str"), ("user_previews[].user.profile_image_urls.medium", "str"), ("next_url", "any")],
    "user_detail": [("user.id", "int"), ("user.name", "str"), ("user.account", "str"), ("user.profile_image_urls.medium", "str"),
                    ("user.is_followed", "bool"), ("profile.total_illusts", "int"), ("profile.total_illust_bookmarks_public", "int")],
    "user_illusts": [("illusts", "list"), ("illusts[].id", "int"), ("illusts[].title", "str"), ("illusts[].type", "str"),
                     ("illusts[].create_date", "str"), ("illusts[].page_count", "int"), ("illusts[].tags", "list"),
                     ("illusts[].tags[].name", "str"), ("illusts[].x_restrict", "int"), ("illusts[].sanity_level", "int"),
                     ("illusts[].total_bookmarks", "int"), ("illusts[].total_view", "int"), ("illusts[].width", "int"),
                     ("illusts[].height", "int"), ("illusts[].image_urls", "dict"), ("illusts[].meta_single_page", "dict"),
                     ("illusts[].meta_pages", "list"), ("illusts[].user.id", "int"), ("illusts[].illust_ai_type", "int"),
                     ("illusts[].caption", "str"), ("next_url", "any")],
    "illust_detail": [("illust.id", "int"), ("illust.title", "str"), ("illust.type", "str"), ("illust.page_count", "int"),
                      ("illust.meta_single_page", "dict"), ("illust.meta_pages", "list"), ("illust.visible", "bool"),
                      ("illust.tags", "list"), ("illust.create_date", "str"), ("illust.user.id", "int")],
    "ugoira_metadata": [("ugoira_metadata.zip_urls.medium", "str"), ("ugoira_metadata.frames", "list"),
                        ("ugoira_metadata.frames[].file", "str"), ("ugoira_metadata.frames[].delay", "int")],
    "user_novels": [("novels", "list"), ("novels[].id", "int"), ("novels[].title", "str"), ("next_url", "any")],
    "webview_novel": [("text", "str")],
}
TYPES = {"str": str, "int": int, "list": (list, tuple), "dict": dict, "bool": bool}


def _dig(obj, path):
    """按路径取值。返回 (找到了没有, 值)。[] 取列表的第一项；列表是空的算“没法查”，返回 (None, None)。"""
    cur = obj
    for part in path.split("."):
        first = part.endswith("[]")
        key = part[:-2] if first else part
        if cur is None:
            return False, None
        has = (key in cur) if isinstance(cur, dict) else hasattr(cur, key)
        if not has:
            return False, None
        cur = _g(cur, key)
        if first:
            if not isinstance(cur, (list, tuple)):
                return False, None
            if not cur:
                return None, None
            cur = cur[0]
    return True, cur


def check_fields(name, res):
    """对照清单检查一次返回。返回问题列表（空 = 都在）。"""
    problems = []
    for path, kind in CONTRACT[name]:
        found, value = _dig(res, path)
        if found is None:
            continue                               # 列表是空的，这次查不了这一项
        if not found:
            problems.append(f"没有 {path}")
        elif kind != "any" and value is not None and not isinstance(value, TYPES[kind]):
            problems.append(f"{path} 的类型变了：现在是 {type(value).__name__}，原来是 {kind}")
        elif kind == "int" and isinstance(value, bool):
            problems.append(f"{path} 的类型变了：现在是 bool，原来是 int")
    return problems


def _samples(db):
    """从数据库里找体检要用的例子：一位有作品的画师、一个动图、一篇小说和它的作者"""
    out = {"artist": None, "illust": None, "ugoira": None, "novel": None, "novel_author": None}
    if db is None:
        return out
    try:
        c = db.conn
        row = c.execute("SELECT m.author_id, i.illust_id FROM illusts i JOIN illust_metadata m ON m.illust_id = i.illust_id "
                        "WHERE i.status = 1 AND i.media_type = 'image' ORDER BY i.illust_id DESC LIMIT 1").fetchone()
        if row:
            out["artist"], out["illust"] = row
        row = c.execute("SELECT illust_id FROM illusts WHERE status = 1 AND media_type = 'ugoira' ORDER BY illust_id DESC LIMIT 1").fetchone()
        out["ugoira"] = row[0] if row else None
        row = c.execute("SELECT i.illust_id, m.author_id FROM illusts i LEFT JOIN illust_metadata m ON m.illust_id = i.illust_id "
                        "WHERE i.status = 1 AND i.media_type = 'novel' ORDER BY i.illust_id DESC LIMIT 1").fetchone()
        if row:
            out["novel"], out["novel_author"] = row
    except Exception as e:
        logger.debug(f"体检：从数据库找例子失败: {e}")
    return out


def run(client, db=None, session=None):
    """做一次体检。client：已经登录的账号；session：下载图片用的连接（查图片服务器）。

    返回 {ok, time, account, library, items: [{name, label, status: ok|changed|error|skipped, problems, note, ms}]}
    """
    api = client.api
    sample = _samples(db)
    items = []

    def step(name, label, call, note=""):
        """call() 返回接口的结果；没法做这一项时返回 None"""
        t0 = time.time()
        item = {"name": name, "label": label, "status": "skipped", "problems": [], "note": note, "ms": 0}
        items.append(item)
        try:
            got = call()
        except Exception as e:                    # 体检本身不能因为一项出错就中断
            item.update(status="error", problems=[f"{type(e).__name__}: {e}"[:200]])
            return None
        item["ms"] = int((time.time() - t0) * 1000)
        if got is None:
            return None
        res, err = got
        if err:
            item.update(status="error", problems=[f"接口返回了错误：[{err.kind}] {err.message}"[:200]])
            return None
        problems = check_fields(name, res)
        item.update(status="changed" if problems else "ok", problems=problems)
        return res

    following = step("user_following", "关注列表",
                     lambda: client.call(api.user_following, user_id=client.user_id, restrict="public") if client.user_id else None)
    first = _g((_g(following, "user_previews") or [None])[0], "user") if following else None
    aid = _g(first, "id") or sample["artist"]
    step("user_detail", "画师资料", lambda: client.call(api.user_detail, aid) if aid else None,
         note="" if aid else "账号没有关注任何人，数据库里也没有画师")
    works = step("user_illusts", "画师的作品列表", lambda: client.call(api.user_illusts, user_id=aid, type="illust") if aid else None)
    newest = (_g(works, "illusts") or [None])[0] if works else None
    iid = _g(newest, "id") or sample["illust"]
    step("illust_detail", "作品详情", lambda: client.call(api.illust_detail, iid) if iid else None,
         note="" if iid else "没有可以用来查的作品")
    step("ugoira_metadata", "动图信息", lambda: client.call(api.ugoira_metadata, sample["ugoira"]) if sample["ugoira"] else None,
         note="" if sample["ugoira"] else "数据库里没有下载过的动图，这一项跳过")
    step("user_novels", "画师的小说列表",
         lambda: client.call(api.user_novels, user_id=sample["novel_author"]) if sample["novel_author"] else None,
         note="" if sample["novel_author"] else "数据库里没有下载过的小说，这一项跳过")
    step("webview_novel", "小说正文", lambda: client.call(api.webview_novel, sample["novel"]) if sample["novel"] else None,
         note="" if sample["novel"] else "数据库里没有下载过的小说，这一项跳过")

    # 图片服务器：拿一张小图试试（只读几 KB）
    url = _g(_g(newest, "image_urls"), "square_medium") if newest else None
    img = {"name": "image", "label": "图片服务器", "status": "skipped", "problems": [], "note": "" if url and session else "没有可以用来试的图片地址", "ms": 0}
    items.append(img)
    if url and session is not None:
        t0 = time.time()
        try:
            r = session.get(url, timeout=(10, 20), stream=True)
            ok = r.status_code == 200 and str(r.headers.get("content-type", "")).startswith("image/")
            r.close()
            img.update(status="ok" if ok else "changed", ms=int((time.time() - t0) * 1000),
                       problems=[] if ok else [f"图片服务器返回了 {r.status_code}（{r.headers.get('content-type')}）"])
        except Exception as e:
            img.update(status="error", problems=[f"{type(e).__name__}: {e}"[:200]])

    try:
        from importlib.metadata import version
        library = version("pixivpy3")
    except Exception:
        library = ""
    bad = [i for i in items if i["status"] in ("changed", "error")]
    return {"ok": not bad, "time": time.strftime("%Y-%m-%d %H:%M:%S"), "account": client.name, "library": library,
            "checked": sum(1 for i in items if i["status"] != "skipped"), "items": items}

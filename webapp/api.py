#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
提供给网页前端调用的接口（pywebview 的 js_api）

方法名与 webui/js/api.js 中的 desktopApi 一一对应；参数和返回值都是可 JSON 序列化的数据。
系统操作（选择文件夹、打开资源管理器等）需要窗口对象，由 web_main.py 注入。
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import threading
import time
import webbrowser
from datetime import datetime
from typing import Dict, List, Optional
from urllib.parse import quote

from contextlib import closing
from pathlib import Path

from utils.constants import APP_VERSION, CACHE_DIR, DATA_DIR, UPDATE_REPO
from utils.logger import get_logger

from . import importer
from .downloader import DownloaderBridge, DownloaderData, DownloaderError, resolve_home
from .library import DEFAULT_AR, Artist, LibraryIndex, Work
from .login_window import LoginMixin
from .store import WebStore
from .tray import CloseMixin
from .updater import UpdateApiMixin, Updater

logger = get_logger("WebApi")

ARTIST_COLORS = ["#e5989b", "#457b9d", "#6d6875", "#2a9d8f", "#c77dff", "#e76f51", "#0077b6", "#d4a373", "#4a4e69", "#ef233c"]


def _color_for(key: str) -> str:
    return ARTIST_COLORS[sum(map(ord, key)) % len(ARTIST_COLORS)]


def _pack(res: dict) -> dict:
    """把作品列表压成紧凑格式（前端在 webui/js/api.js 的 unpackWorks 里还原）。

    资料库有近十万个作品时，普通格式超过 60 MB：每张图的完整路径、每个作品重复的画师文件夹和
    Pixiv 标签占了大头。这里画师单独列一张表，页面只给相对画师文件夹的路径，标签在详情里再取。
    """
    artists, index = [], {}
    rows = []
    for x in res["works"]:
        ak = x["artistKey"]
        ai = index.get(ak)
        if ai is None:
            ai = index[ak] = len(artists)
            artists.append([ak, x["artistName"], x["artistId"]])
        base = ak if ak.endswith(os.sep) else ak + os.sep
        pages = [[p["path"][len(base):] if p["path"].startswith(base) else p["path"], p["w"], p["h"], p["size"]]
                 for p in x["pages"]]
        default_title = os.path.splitext(x["pages"][0]["file"])[0]
        rows.append([
            0 if x["key"] == str(x["pid"]) else x["key"], x["pid"], "" if x["title"] == default_title else x["title"],
            ai, int(x["posted"] or 0), int(x["mtime"] or 0), x["month"], x["rating"],
            (1 if x["ai"] else 0) | (2 if x["fav"] else 0), x["stars"], pages,
        ])
    return {**res, "works": rows, "artists": artists, "packed": 1, "sep": os.sep, "defaultAr": DEFAULT_AR}


def _month(ts: float) -> str:
    d = datetime.fromtimestamp(ts or 0)
    return f"{d.year}-{d.month:02d}"


class Api(UpdateApiMixin, LoginMixin, CloseMixin):
    def __init__(self, config_manager, reader, database, store: Optional[WebStore] = None, token: str = "") -> None:
        self._cm = config_manager
        self._reader = reader
        self._db = database
        self._store = store or WebStore()
        self._token = token
        self._window = None          # pywebview 窗口，由 web_main 设置
        self._updater = Updater("PixivViewer", APP_VERSION, UPDATE_REPO, proxies=self._download_proxies)
        self._update_done = self._updater.finish() if self._updater.frozen else None
        # 注意：pywebview 会把所有不以下划线开头的属性和方法暴露给网页，内部对象一律用私有名
        self._library = LibraryIndex(self._store, reader, lambda: self._library_roots())
        self._indexing = False
        self._rev = 0                # 评分、收藏、标签等每改一次加一
        self._views_rev = 0          # “最近查看”每变一次加一
        self._list_cache: Dict[str, tuple] = {}
        reader.set_metadata_path(getattr(self._cm.config, "pixiv_metadata_path", "") or "")
        # Pixiv 下载器：工作进程在第一次用到时才启动；头像、画师名直接从它的目录里读
        self._dl: Optional[DownloaderBridge] = None
        self._dl_data: Optional[DownloaderData] = None
        self._dl_lock = threading.Lock()
        self._dl_code_root = None       # 只在测试里用
        self._library.author_names = lambda: self._downloader_data().author_names()
        self._adopt_download_metadata()

    # ================= 工具 =================
    def _media(self, kind: str, path: str) -> str:
        return f"/{kind}?t={self._token}&path={quote(path)}"

    def _artist_info(self, a: Artist, pins: set) -> dict:
        works = self._library.cached_works(a.key)
        data = self._downloader_data()
        has_avatar = bool(a.id and a.id in data.avatars())
        return {
            "key": a.key, "id": a.id or "", "name": a.name, "folder": a.key, "color": _color_for(a.key),
            "avatar": f"/avatar?t={self._token}&id={a.id}" if has_avatar else None, "pinned": a.key in pins,
            "folders": len(a.folders),
            "count": sum(len(w.pages) for w in works) if works is not None else 0,
            "updated": max((w.mtime for w in works), default=0) if works else 0,
        }

    def _keys_to_works(self, keys: List[str]) -> List[Work]:
        works = []
        for k in keys:
            w = self._library.work(k)
            if w:
                works.append(w)
        return works

    def _paths(self, keys: List[str]) -> List[str]:
        out = []
        for k in keys:
            w = self._library.work(k)
            if not w:
                continue
            if "#" in k:
                idx = int(k.split("#", 1)[1] or 0)
                out.extend(p.path for p in w.pages[idx:idx + 1])
            else:
                out.extend(p.path for p in w.pages)
        return out

    # ================= 设置 =================
    def get_config(self):
        return self._store.load_settings()

    def save_config(self, data):
        if isinstance(data, dict):
            self._store.save_settings(data)
        return True

    # ================= 资料库 =================
    def get_library(self):
        pins = self._store.pins()
        artists = self._library.artists()
        roots = []
        added = {os.path.normcase(os.path.normpath(p)) for p in (self._cm.config.image_paths or []) if p}
        for r in self._library.roots():
            count = sum(self._artist_info(a, pins)["count"] for a in artists if a.root == r)
            roots.append({"path": r, "name": os.path.basename(r.rstrip("\\/")) or r, "count": count,
                          "offline": not any(a.root == r for a in artists) and not os.path.isdir(r),
                          # 下载的保存位置：自动包含，不能从资料库里移除
                          "auto": os.path.normcase(r) not in added})
        infos = [self._artist_info(a, pins) for a in artists]
        works = self._library.all_works(scan=False)
        favs = self._store.favorites()
        views = self._store.views()
        return {
            "version": APP_VERSION,
            "roots": roots,
            "artists": infos,
            "totals": {
                "images": sum(len(w.pages) for w in works), "works": len(works),
                "fav": sum(1 for w in works if w.key in favs), "recent": sum(1 for w in works if w.key in views),
            },
            "metadata": {"path": str(self._reader.db_path or ""), "ok": bool(self._reader.db_path)},
            "dataHome": str(self._downloader_home()),
            "folders": self._store.folders(),
            "cache": self.cache_info(),
            "indexing": self._indexing,
        }

    def _cache_size(self) -> int:
        """缩略图缓存占用；统计要遍历上万个文件，所以结果保留一分钟"""
        now = time.monotonic()
        cached = getattr(self, "_cache_stat", None)
        if cached and now - cached[0] < 60:
            return cached[1]
        size = 0
        try:
            size = sum(f.stat().st_size for f in CACHE_DIR.glob("*.jpg"))
        except OSError:
            pass
        self._cache_stat = (now, size)
        return size

    def _mount_network(self) -> None:
        """连接配置里启用的网络共享（SMB 等，凭据来自系统凭据库）；已连接时会直接复用"""
        mounts = getattr(self._cm.config, "network_mounts", None) or []
        if not mounts:
            return
        from utils.config_manager import NetworkMount
        from utils.network_mount import get_mount_manager
        manager = get_mount_manager()
        for m in mounts:
            try:
                if isinstance(m, dict):
                    m = NetworkMount(**m)
                if getattr(m, "enabled", True):
                    ok = manager.mount(m)
                    logger.info(f"网络共享 {m.protocol}://{m.host}{m.path}: {'已连接' if ok else '连接失败'}")
            except Exception as e:
                logger.warning(f"连接网络共享失败: {e}")

    def _start_indexing(self, on_progress=None, on_done=None) -> None:
        """后台连接网络共享并扫描全部画师文件夹；扫描期间定期通知前端刷新"""
        if self._indexing:
            return
        self._indexing = True   # 先置位：前端此时请求“全部图片”会拿到已扫描的部分而不是同步等待

        def run():
            t0 = last = time.time()
            try:
                self._mount_network()
                for a in self._library.artists():
                    self._library.scan_artist(a, read_dims=False)
                    if on_progress and time.time() - last > 2.5:
                        last = time.time()
                        on_progress()
                self._library.metadata(w.pid for w in self._library.all_works(scan=False))
            except Exception as e:
                logger.error(f"后台索引失败: {e}")
            finally:
                self._indexing = False
                self._store.close_thread_connection()
                logger.info(f"资料库索引完成，用时 {time.time() - t0:.1f}s")
                if on_done:
                    on_done()
            # 扫描时跳过的图片宽高在空闲时慢慢补上（只影响齐行/瀑布流的比例，不阻塞浏览）
            try:
                n = self._library.fill_dims()
                if n:
                    logger.info(f"已补全 {n} 张图片的宽高")
            except Exception as e:
                logger.warning(f"补全图片宽高失败: {e}")
            finally:
                self._store.close_thread_connection()

        threading.Thread(target=run, name="library-index", daemon=True).start()

    # ================= 作品列表 =================
    def _scope_works(self, q: dict) -> List[Work]:
        scope = q.get("scope") or "all"
        if scope == "artist":
            a = self._library.artist_by_key(q.get("artist") or "")
            return self._library.scan_artist(a) if a else []
        if scope == "folder":
            # 画师文件夹：里面所有画师的作品合在一起
            folder = next((f for f in self._store.folders() if str(f["id"]) == str(q.get("folder"))), None)
            keys = set(folder["artists"]) if folder else set()
            out: List[Work] = []
            for a in self._library.artists():
                if a.key in keys:
                    out.extend(self._library.scan_artist(a))
            return out
        # 后台索引进行中时不在这里同步扫描整个资料库（网络路径可能要几分钟），先返回已扫描的部分
        works = self._library.all_works(scan=not self._indexing)
        if scope == "fav":
            favs = self._store.favorites()
            works = [w for w in works if w.key in favs]
        elif scope == "recent":
            views = self._store.views()
            works = [w for w in works if w.key in views]
        return works

    def _list_works_json(self, q) -> bytes:
        """list_works 的 JSON 结果，数据没变时直接复用上一次的。

        近十万个作品时整理一遍要 2 秒左右；在“全部图片”和画师页之间来回切换不应该每次都等。
        """
        q = q or {}
        scope = q.get("scope") or "all"
        state = (self._library.version, self._rev, self._views_rev if scope == "recent" else 0, self._indexing,
                 str(self._reader.db_path or ""))
        key = json.dumps(q, sort_keys=True, ensure_ascii=False)
        hit = self._list_cache.get(key)
        if hit and hit[0] == state:
            return hit[1]
        data = json.dumps(self.list_works(q), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        # 扫描过程中数据一直在变，不值得存；状态以整理之后的为准（整理时可能顺带重扫了文件夹）
        state = (self._library.version, self._rev, self._views_rev if scope == "recent" else 0, self._indexing,
                 str(self._reader.db_path or ""))
        if not self._indexing:
            if len(self._list_cache) >= 6:
                self._list_cache.pop(next(iter(self._list_cache)))
            self._list_cache[key] = (state, data)
        return data

    def list_works(self, q):
        q = q or {}
        works = self._scope_works(q)
        meta = self._library.metadata(w.pid for w in works)
        pins_names = {a.key: a for a in self._library.artists()}
        favs = self._store.favorites()
        first_paths = [w.pages[0].path for w in works]
        stars = self._db.batch_get_ratings(first_paths) if first_paths else {}
        my_tags = self._db.batch_get_tags(first_paths) if first_paths else {}

        scope_tags: Dict[str, int] = {}
        for w in works:
            m = meta.get(w.pid)
            for t in (m["tags"] if m else []):
                scope_tags[t] = scope_tags.get(t, 0) + 1

        text = (q.get("q") or "").strip().lower()
        tags = q.get("tags") or []
        f = q.get("filters") or {}
        rating = q.get("rating") or "all"
        out = []
        for w in works:
            m = meta.get(w.pid) or {}
            a = pins_names.get(w.artist_key)
            artist_name = a.name if a else ""
            artist_id = a.id if a and a.id else (m.get("author_id") or "")
            p0 = w.pages[0]
            r = m.get("rating", "safe")
            mine = my_tags.get(p0.path, [])
            all_tags = (m.get("tags") or []) + mine
            ar = (p0.w / p0.h) if p0.w and p0.h else DEFAULT_AR
            st = stars.get(p0.path, 0) or 0
            if rating == "safe" and r != "safe":
                continue
            if rating == "r18" and r == "safe":
                continue
            if tags and not all(t in all_tags for t in tags):
                continue
            title = m.get("title") or os.path.splitext(p0.file)[0]
            if text and not (text in title.lower() or text in artist_name.lower() or text in str(w.pid)
                             or text in str(artist_id) or text in p0.file.lower()
                             or any(text in t.lower() for t in all_tags)):
                continue
            if f.get("ai") == "exclude" and m.get("ai"):
                continue
            if f.get("ai") == "only" and not m.get("ai"):
                continue
            ori = f.get("orientation")
            if ori == "portrait" and ar >= 1 or ori == "landscape" and ar <= 1 or ori == "square" and abs(ar - 1) > 0.05:
                continue
            if f.get("minStars") and st < int(f["minStars"]):
                continue
            if f.get("multiPage") and len(w.pages) < 2:
                continue
            posted = m.get("posted") or w.mtime
            out.append({
                "key": w.key, "pid": w.pid, "title": title, "artistKey": w.artist_key, "artistName": artist_name,
                "artistId": artist_id, "w": p0.w, "h": p0.h, "ar": round(ar, 4), "posted": posted, "mtime": w.mtime,
                "month": _month(posted), "rating": r, "ai": bool(m.get("ai")), "tags": m.get("tags") or [],
                "fav": w.key in favs, "stars": st,
                # 缩略图地址由前端按 path 拼出（见 webui/js/api.js），不在这里重复上万次
                "pages": [{"path": p.path, "file": p.file, "w": p.w, "h": p.h, "size": p.size} for p in w.pages],
            })

        sort = q.get("sort") or "id"
        if q.get("scope") == "recent":
            views = self._store.views()
            out.sort(key=lambda x: views.get(x["key"], 0), reverse=True)
        elif sort == "time":
            out.sort(key=lambda x: x["mtime"], reverse=True)
        elif sort == "name":
            out.sort(key=lambda x: x["pages"][0]["file"].lower())
        else:
            out.sort(key=lambda x: (x["pid"], x["mtime"]), reverse=True)

        if not q.get("mergePages", True):
            flat = []
            for x in out:
                for i, p in enumerate(x["pages"]):
                    flat.append({**x, "key": f"{x['key']}#{i}", "pages": [p],
                                 "w": p["w"], "h": p["h"], "ar": round(p["w"] / p["h"], 4) if p["w"] and p["h"] else DEFAULT_AR})
            out = flat
        tags_sorted = sorted(scope_tags.items(), key=lambda kv: (-kv[1], kv[0]))[:600]
        res = {"works": out, "scopeTags": [[t, n] for t, n in tags_sorted], "indexing": self._indexing}
        return _pack(res) if q.get("packed") else res

    def suggest(self, text, safe_only=False):
        t = (text or "").strip().lower()
        if not t:
            return {"artists": [], "tags": [], "works": []}
        pins = self._store.pins()
        artists = [self._artist_info(a, pins) for a in self._library.artists()
                   if t in a.name.lower() or (a.id and t in str(a.id))][:5]
        works = self._library.all_works(scan=False)
        meta = self._library.metadata(w.pid for w in works)
        counts: Dict[str, int] = {}
        hits = []
        for w in works:
            m = meta.get(w.pid) or {}
            if safe_only and m.get("rating", "safe") != "safe":
                continue                     # 设置里关掉了 R18：搜索建议里也不出现
            for tag in m.get("tags", []):
                if t in tag.lower():
                    counts[tag] = counts.get(tag, 0) + 1
            if len(hits) < 5 and (t in str(w.pid) or t in (m.get("title") or "").lower()):
                hits.append(w)
        tags = sorted(counts.items(), key=lambda kv: -kv[1])[:6]
        names = {a.key: a.name for a in self._library.artists()}
        work_items = [{"key": w.key, "pid": w.pid, "title": (meta.get(w.pid) or {}).get("title") or w.pages[0].file,
                       "artistKey": w.artist_key, "artistName": names.get(w.artist_key, ""),
                       "pages": [{"thumb": self._media("thumb", w.pages[0].path)}]} for w in hits]
        return {"artists": artists, "tags": [[k, v] for k, v in tags], "works": work_items}

    def get_details(self, key):
        w = self._library.work(key)
        if not w:
            return None
        m = self._library.metadata([w.pid]).get(w.pid) or {}
        mine = self._db.get_tags(w.pages[0].path)
        return {
            "title": m.get("title") or os.path.splitext(w.pages[0].file)[0], "caption": m.get("caption", ""),
            "tags": m.get("tags", []), "myTags": mine, "pid": w.pid,
            "url": f"https://www.pixiv.net/artworks/{w.pid}" if w.pid else "",
            "posted": m.get("posted") or w.mtime, "bookmarks": m.get("bookmarks") or 0, "views": m.get("views") or 0,
        }

    # ================= 修改 =================
    def set_stars(self, keys, n):
        self._db.batch_set_ratings([(p, int(n)) for p in self._paths(keys)])
        self._rev += 1
        return True

    def set_favorite(self, keys, on):
        self._store.set_favorite([k.split("#", 1)[0] for k in keys], bool(on))
        self._rev += 1
        return True

    def add_tags(self, keys, tags):
        self._db.add_tags(self._paths(keys), [t for t in tags if t])
        self._rev += 1
        return True

    def remove_tags(self, keys, tags):
        self._db.remove_tags(self._paths(keys), list(tags))
        self._rev += 1
        return True

    def mark_viewed(self, key):
        self._store.mark_viewed(key.split("#", 1)[0])
        self._views_rev += 1
        return True

    # ================= Pixiv 下载器 =================
    def _downloader_home(self) -> Path:
        settings = self._store.load_settings() or {}
        return resolve_home(str(settings.get("downloaderHome") or ""), str(self._reader.db_path or ""), DATA_DIR / "pixiv")

    def _downloader_data(self) -> Optional[DownloaderData]:
        if self._dl_data is None:
            self._dl_data = DownloaderData(self._downloader_home())
        return self._dl_data

    def _bridge(self) -> DownloaderBridge:
        with self._dl_lock:
            if self._dl is None:
                self._dl = DownloaderBridge(self._downloader_home(), self._dl_code_root)
            return self._dl

    def _avatar_path(self, author_id: int):
        data = self._downloader_data()
        return data.avatar_path(author_id) if data else None

    def _download_proxies(self) -> dict:
        """下载设置里填的代理（检查软件更新时，系统代理连不上才用它）"""
        try:
            data = json.loads((self._downloader_home() / "settings.json").read_text(encoding="utf-8"))
            proxies = (data.get("current") or {}).get("PROXIES") or {}
            return proxies if isinstance(proxies, dict) else {}
        except (OSError, ValueError):
            return {}

    def _shutdown(self) -> None:
        if self._dl is not None:
            self._dl.stop()

    def _reset_downloader(self) -> None:
        with self._dl_lock:
            if self._dl is not None:
                self._dl.stop()
            self._dl, self._dl_data = None, None

    def dl_info(self):
        """下载数据放在哪里；不会启动工作进程"""
        home = self._downloader_home()
        own = DATA_DIR / "pixiv"
        return {"available": True, "home": str(home), "builtinHome": str(own),
                "files": importer.describe_home(home), "kinds": importer.KINDS, "viewerKinds": True,
                "external": os.path.normcase(str(home)) != os.path.normcase(str(own)),
                "hasData": (home / "settings.json").is_file() or (home / "db" / "pixiv_manager.db").is_file(),
                "started": bool(self._dl and self._dl.running), "version": self._dl.version if self._dl else ""}

    def dl_set_home(self, path=None):
        """换一个数据目录（不传则弹出选择框）。只是改用那个目录，不移动任何文件。"""
        path = path or self._pick_folder()
        if not path:
            return self.dl_info()
        settings = self._store.load_settings() or {}
        settings["downloaderHome"] = os.path.normpath(str(path))
        self._store.save_settings(settings)
        self._reset_downloader()
        self._use_metadata_from(Path(settings["downloaderHome"]))
        return self.dl_info()

    def _adopt_download_metadata(self) -> None:
        """还没指定元数据库时，直接用内置下载功能的数据库（下载过之后它就在数据目录里）"""
        if not self._reader.db_path:
            try:
                self._use_metadata_from(self._downloader_home())
            except Exception as e:
                logger.debug(f"没有可用的下载数据库: {e}")

    def _use_metadata_from(self, home: Path) -> None:
        """看图用的元数据（标题、标签、分级）就读下载数据里的数据库"""
        db_file = home / "db" / "pixiv_manager.db"
        if db_file.is_file():
            self._cm.update(pixiv_metadata_path=str(db_file))
            self._reader.set_metadata_path(str(db_file))
            self._library.invalidate()

    def dl_migrate(self):
        """把现有的下载数据（账号与设置、数据库、头像）复制到查看器自己的数据目录，之后改用这份。
        原来的文件不动；有任务在运行时不做。"""
        src, dst = self._downloader_home(), DATA_DIR / "pixiv"
        if os.path.normcase(str(src)) == os.path.normcase(str(dst)):
            return {"ok": True, "info": self.dl_info()}
        if self._dl and self._dl.running:
            try:
                if (self._dl.call("GET", "/api/job") or {}).get("running"):
                    return {"ok": False, "error": "有任务正在运行，结束后再搬"}
            except DownloaderError:
                pass
        if (dst / "settings.json").exists() or (dst / "db" / "pixiv_manager.db").exists():
            return {"ok": False, "error": f"{dst} 里已经有数据了，为避免覆盖没有继续。"}
        self._reset_downloader()
        try:
            (dst / "db").mkdir(parents=True, exist_ok=True)
            if (src / "settings.json").is_file():
                shutil.copy2(src / "settings.json", dst / "settings.json")
            src_db = src / "db" / "pixiv_manager.db"
            if src_db.is_file():
                # 用数据库自己的备份接口复制：即使有没写完的日志也能得到完整的一份
                with closing(sqlite3.connect(f"file:{src_db.as_posix()}?mode=ro", uri=True)) as a, \
                        closing(sqlite3.connect(str(dst / "db" / "pixiv_manager.db"))) as b:
                    a.backup(b)
            if (src / "avatars").is_dir():
                shutil.copytree(src / "avatars", dst / "avatars", dirs_exist_ok=True)
        except Exception as e:
            logger.error(f"搬迁下载数据失败: {e}")
            return {"ok": False, "error": f"复制失败：{e}"}
        settings = self._store.load_settings() or {}
        settings["downloaderHome"] = str(dst)
        self._store.save_settings(settings)
        self._dl_data = None
        self._use_metadata_from(dst)
        return {"ok": True, "info": self.dl_info()}

    # ---- 导入已有的数据
    def import_pick(self, what=None):
        """弹出选择框（what="folder" 选文件夹，否则可以一次选多个文件），返回识别结果"""
        if not self._window:
            return []
        import webview
        if what == "folder":
            picked = self._window.create_file_dialog(webview.FOLDER_DIALOG)
        else:
            picked = self._window.create_file_dialog(webview.OPEN_DIALOG, allow_multiple=True)
        return self.import_inspect(list(picked or []))

    def import_inspect(self, paths):
        """按内容识别每个路径是什么数据；同一种只保留最后选的那个"""
        found = {}
        unknown = []
        for p in paths or []:
            for item in importer.inspect(str(p)):
                if item["ok"]:
                    found[item["kind"]] = item
                else:
                    unknown.append(item)
        home = importer.describe_home(self._downloader_home())
        for item in found.values():
            # 会不会替换掉现有的：前三种是替换（旧的改名留下），后两种是合并
            item["replaces"] = bool(home.get(item["kind"], {}).get("exists"))
            item["merge"] = item["kind"] in importer.VIEWER_KINDS
        return list(found.values()) + unknown

    def import_apply(self, paths):
        """导入。返回 {ok, results:[{label, ok, message, kept}]}"""
        items = [i for i in self.import_inspect(paths) if i["ok"]]
        if not items:
            return {"ok": False, "error": "没有可以导入的内容"}
        if self._dl and self._dl.running:
            try:
                if (self._dl.call("GET", "/api/job") or {}).get("running"):
                    return {"ok": False, "error": "有下载任务正在运行，结束后再导入"}
            except DownloaderError:
                pass
        home = self._downloader_home()
        results = []
        download_items = [i for i in items if i["kind"] in importer.DOWNLOAD_KINDS]
        if download_items:
            self._reset_downloader()                      # 下载进程会占着数据库，先让它退出
            self._reader.set_metadata_path("")            # 看图这边读作品信息的连接也先放开
            results += importer.import_download_data(download_items, home)
            self._dl_data = None
            self._use_metadata_from(home)
        for i in items:
            if i["kind"] == "viewer_db":
                results.append(importer.merge_viewer_db(i["path"], self._store))
            elif i["kind"] == "ratings_db":
                results.append(importer.merge_ratings_db(i["path"], self._db))
        self._library.invalidate()
        self._rev += 1
        self._views_rev += 1
        return {"ok": all(r["ok"] for r in results), "results": results}

    # ---- 数据位置的统一规则
    # 资料库 = 用户添加的文件夹 + 下载的保存位置（自动包含，不用再手动加一次）
    # 元数据（标题、标签、分级）、画师头像 = 下载数据目录里的数据库和 avatars 文件夹
    def _download_target(self) -> dict:
        return self._downloader_data().save_target()

    def _library_roots(self) -> list:
        roots = [p for p in (self._cm.config.image_paths or []) if p]
        target = self._download_target()
        path = target["path"]
        if not path or (target["mode"] == "local" and not os.path.isdir(path)):
            return roots                      # 本地的保存目录要等下载过、文件夹存在了才算
        norm = os.path.normcase(path)
        covered = any(norm == r or norm.startswith(r.rstrip("\\/") + os.sep)
                      for r in (os.path.normcase(os.path.normpath(x)) for x in roots))
        return roots if covered else roots + [path]

    def dl_storage_link(self):
        """保存位置和资料库的关系：{mode, path, readable, inLibrary, auto}"""
        target = self._download_target()
        norm = os.path.normcase(target["path"]) if target["path"] else ""
        added = [os.path.normcase(os.path.normpath(r)) for r in (self._cm.config.image_paths or [])]
        explicit = bool(norm) and any(norm == r or norm.startswith(r.rstrip("\\/") + os.sep) for r in added)
        target["inLibrary"] = target["readable"]          # 能直接读的位置都会自动出现在资料库里
        target["auto"] = target["readable"] and not explicit
        return target

    def dl_link_library(self):
        """保留给旧界面：保存位置现在自动属于资料库，这里只返回当前状态"""
        link = self.dl_storage_link()
        return {"ok": link["readable"], **link, **({} if link["readable"] else {"error": "这种保存方式查看器不能直接读取"})}

    def dl(self, method, path, body=None, query=None):
        """调用下载器的接口。返回 {ok, data} 或 {ok: False, status, error}，不抛异常（前端好处理）。"""
        path = str(path or "")
        # 返回文件的接口不走这里：头像由本机服务直接读文件，原图、缩略图用查看器自己的
        if not path.startswith("/api/") or path.startswith(("/api/file/", "/api/thumb/", "/api/avatar/")) \
                or path.endswith(".csv"):
            return {"ok": False, "status": 400, "error": "不支持的接口"}
        try:
            data = self._bridge().call(str(method or "GET").upper(), path, body if isinstance(body, dict) else None,
                                       query if isinstance(query, dict) else None)
            return {"ok": True, "data": data}
        except DownloaderError as e:
            return {"ok": False, "status": e.status, "error": str(e)}
        except Exception as e:
            logger.error(f"调用下载器 {path} 失败: {e}")
            return {"ok": False, "status": 500, "error": str(e)}

    def dl_refresh_library(self, author_ids=None):
        """下载任务结束后调用：重新扫描有变化的画师文件夹（没给 ID 就全部重新检查），并刷新头像列表"""
        if self._dl_data is not None:
            self._dl_data.refresh()
        self._adopt_download_metadata()
        ids = {int(x) for x in (author_ids or []) if str(x).isdigit()}
        self._library.forget_artists()                     # 可能多了新画师的文件夹
        artists = self._library.artists()
        targets = [a for a in artists if a.id in ids] if ids else artists
        known = {a.id for a in artists}
        if ids - known:                                    # 有对不上文件夹的画师：稳妥起见全部检查一遍
            targets = artists
        for a in targets:
            self._library.scan_artist(a, force=bool(ids) and a.id in ids, read_dims=False, recheck=True)
        self._rev += 1
        return {"scanned": len(targets)}

    # ================= 窗口（无边框窗口的标题栏按钮） =================
    def save_text(self, name, text):
        """让用户选个地方，把一段文字存成文件（导出清单用）。返回保存的路径；取消了返回空字符串。"""
        import webview
        if not self._window:
            return ""
        picked = self._window.create_file_dialog(webview.SAVE_DIALOG, save_filename=str(name or "导出.txt"))
        if not picked:
            return ""
        path = picked if isinstance(picked, str) else picked[0]
        with open(path, "w", encoding="utf-8-sig", newline="") as f:      # 带 BOM：Excel 直接打开不乱码
            f.write(str(text or ""))
        return path

    # ---- 缩略图缓存
    def _prune_cache(self) -> None:
        """启动时在后台调用：删掉太久没用到的缩略图，并把总大小压回上限以内（规则见 utils/thumbnail_cache.py）"""
        from utils import thumbnail_cache
        try:
            result = thumbnail_cache.prune()
            if result["removed"]:
                logger.info(f"清理缩略图缓存：删除 {result['removed']} 个，释放 {result['freed'] / 1048576:.0f} MB")
            self._cache_stat = None
        except Exception as e:
            logger.warning(f"清理缩略图缓存失败: {e}")

    def cache_info(self):
        from utils import thumbnail_cache
        return {"size": self._cache_size(), "maxBytes": thumbnail_cache.DEFAULT_MAX_BYTES,
                "maxAgeDays": thumbnail_cache.DEFAULT_MAX_AGE_DAYS}

    def clear_cache(self):
        """清空缩略图缓存（只是缓存，不动任何图片）。返回 {removed, freed}"""
        from utils import thumbnail_cache
        result = thumbnail_cache.clear()
        self._cache_stat = None
        return result

    def window_action(self, action):
        """minimize / toggle（最大化与还原）/ close / drag / state；返回窗口当前是否最大化"""
        from . import chrome
        handled = self._window_close_action(str(action))      # close / tray / restore（见 webapp/tray.py）
        return handled if handled is not None else chrome.window_action(self._window, str(action))

    def my_tag_list(self):
        return self._db.get_all_tags()

    def pin_artist(self, key, on):
        self._store.set_pin(key, bool(on))
        return True

    # ---- 画师文件夹
    def folder_create(self, name, artists=None):
        name = str(name or "").strip()[:60]
        if not name:
            return None
        fid = self._store.folder_create(name)
        if artists:
            self._store.folder_set(fid, [str(a) for a in artists], True)
        self._rev += 1
        return fid

    def folder_rename(self, folder_id, name):
        name = str(name or "").strip()[:60]
        if not name:
            return False
        self._store.folder_rename(int(folder_id), name)
        return True

    def folder_delete(self, folder_id):
        """删除文件夹（只是取消分类，画师和图片都不受影响）"""
        self._store.folder_delete(int(folder_id))
        self._rev += 1
        return True

    def folder_set(self, folder_id, artists, on):
        self._store.folder_set(int(folder_id), [str(a) for a in (artists or [])], bool(on))
        self._rev += 1
        return True

    # ================= 文件夹 =================
    def _pick_folder(self) -> Optional[str]:
        if not self._window:
            return None
        import webview
        result = self._window.create_file_dialog(webview.FOLDER_DIALOG)
        if not result:
            return None
        return result[0] if isinstance(result, (list, tuple)) else result

    def add_folder(self, path=None):
        path = path or self._pick_folder()
        if not path:
            return None
        path = os.path.normpath(path)
        paths = list(self._cm.config.image_paths or [])
        if path not in paths:
            paths.append(path)
            self._cm.update(image_paths=paths)
        self._library.invalidate()
        return path

    def remove_folder(self, path):
        paths = [p for p in (self._cm.config.image_paths or []) if os.path.normpath(p) != os.path.normpath(path)]
        self._cm.update(image_paths=paths)
        self._library.invalidate()
        return True

    def pick_directory(self, kind):
        path = self._pick_folder()
        if not path:
            return None
        if kind == "metadata":
            self._cm.update(pixiv_metadata_path=path)
            self._reader.set_metadata_path(path)
        elif kind == "avatar":
            self._cm.update(avatar_path=path)
        return path

    # ================= 系统操作 =================
    def _allowed(self, path: str) -> bool:
        """只允许操作资料库根目录下的文件"""
        norm = os.path.normcase(os.path.normpath(path))
        return any(norm.startswith(os.path.normcase(r).rstrip("\\/") + os.sep) or norm == os.path.normcase(r)
                   for r in self._library.roots())

    def reveal(self, paths):
        paths = [p for p in paths if self._allowed(p)]
        if not paths:
            return False
        target = os.path.normpath(paths[0])
        if os.name == "nt":
            if os.path.isdir(target):
                subprocess.Popen(["explorer", target])
            else:
                subprocess.Popen(["explorer", "/select,", target])
        return True

    def open_external(self, path):
        if not self._allowed(path):
            return False
        if os.name == "nt":
            os.startfile(path)
        return True

    def open_url(self, url):
        if not str(url).startswith(("https://", "http://")):
            return False
        browser = getattr(self._cm.config, "browser_path", "") or ""
        if browser and os.path.exists(browser):
            subprocess.Popen([browser, url])
        else:
            webbrowser.open(url)
        return True

    def set_wallpaper(self, path):
        if not self._allowed(path):
            return False
        from webapp.wallpaper import set_wallpaper
        return set_wallpaper(path)

    def export_works(self, paths):
        paths = [p for p in paths if self._allowed(p)]
        target = self._pick_folder()
        if not target or not paths:
            return False
        for p in paths:
            dest = os.path.join(target, os.path.basename(p))
            base, ext = os.path.splitext(dest)
            n = 1
            while os.path.exists(dest):
                dest = f"{base} ({n}){ext}"
                n += 1
            shutil.copy2(p, dest)
        return True

    def copy_text(self, text):
        if os.name != "nt":
            return False
        subprocess.run(["clip"], input=str(text).encode("utf-16le"), check=False)
        return True

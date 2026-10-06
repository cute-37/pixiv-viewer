#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
资料库索引：根目录 → 画师文件夹 → 作品（同一 Pixiv 作品的 p0/p1… 合并为一个作品）

- 画师 = 根目录下的一级子文件夹；根目录里直接放的图片归到以根目录命名的“画师”
- 文件夹扫描结果按目录修改时间与 TTL 缓存；图片宽高读文件头，结果持久化在 WebStore
- Pixiv 元数据（标题、标签、分级、AI、投稿日期、说明）通过 PixivMetadataReader 的数据库只读查询
"""
from __future__ import annotations

import os
import re
import threading
import time
import zlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from utils.constants import IMAGE_EXTENSIONS
from utils.logger import get_logger
from utils.path_utils import is_network_path, safe_exists

logger = get_logger("WebLibrary")

PIXIV_RE = re.compile(r"(\d{5,})_p(\d+)", re.IGNORECASE)
ARTIST_ID_RE = re.compile(r"[\[(（【]\s*(\d{2,})\s*[\])）】]")
TAG_STRIP_RE = re.compile(r"<[^>]+>")
SCAN_TTL = 120.0          # 文件夹扫描结果的有效期（秒）
ARTISTS_TTL = 30.0        # 画师列表（根目录下的子文件夹）的有效期（秒）
RECHECK_SECS = 60.0       # 这段时间内不重复检查文件夹修改时间（网络盘上 500 个文件夹要将近 1 秒）
MAX_FILES_PER_FOLDER = 20000
DEFAULT_AR = 0.75


@dataclass
class Page:
    path: str
    file: str
    mtime: float
    size: int
    page: int = 0
    w: int = 0
    h: int = 0


@dataclass
class Work:
    key: str
    pid: int
    artist_key: str
    pages: List[Page] = field(default_factory=list)
    mtime: float = 0.0   # 各页中最新的修改时间，扫描时算好


@dataclass
class Artist:
    key: str            # 文件夹的完整路径
    name: str
    id: Optional[int]
    root: str
    loose: bool = False  # 根目录里直接存放的图片
    extra: List[str] = field(default_factory=list)   # 同一位画师的其他文件夹（按画师 ID 合并进来的）

    @property
    def folders(self) -> List[str]:
        return [self.key, *self.extra]


@dataclass
class _FolderScan:
    signature: float
    scanned_at: float
    works: List[Work]
    checked_at: float = 0.0


def parse_artist(folder: str) -> Tuple[str, Optional[int]]:
    name = os.path.basename(folder.rstrip("\\/")) or folder
    m = ARTIST_ID_RE.search(name)
    artist_id = int(m.group(1)) if m else None
    clean = ARTIST_ID_RE.sub("", name).strip(" _-") or name
    return clean, artist_id


def is_image(name: str) -> bool:
    return os.path.splitext(name)[1].lower() in IMAGE_EXTENSIONS


def read_dims(path: str) -> Tuple[int, int]:
    """只读文件头取得宽高；失败返回 (0, 0)"""
    try:
        from PIL import Image
        with Image.open(path) as im:
            return im.size
    except Exception:
        return (0, 0)


class LibraryIndex:
    def __init__(self, store, reader, roots_provider: Callable[[], List[str]]) -> None:
        self.store = store
        self.reader = reader
        self._roots_provider = roots_provider
        self._lock = threading.RLock()
        self._scans: Dict[str, _FolderScan] = {}
        self._meta: Dict[int, Optional[dict]] = {}
        self._meta_source: Optional[str] = None
        self._works: Dict[str, Work] = {}
        self.version = 0     # 作品数据每变一次加一，供上层判断缓存是否还能用
        # 画师昵称表（画师 ID -> 名字），由上层提供；文件夹名认不出画师时用它
        self.author_names: Callable[[], Dict[int, str]] = dict
        # 画师列表缓存：根目录在网络盘上时每次 scandir 都很慢，界面刷新又很频繁
        self._artists: Optional[Tuple[float, Tuple[str, ...], List[Artist]]] = None

    # ================= 根目录与画师 =================
    def roots(self) -> List[str]:
        return [os.path.normpath(r) for r in self._roots_provider() if r]

    def artists(self) -> List[Artist]:
        roots = tuple(self.roots())
        with self._lock:
            cached = self._artists
        if cached and cached[1] == roots and time.monotonic() - cached[0] < ARTISTS_TTL:
            return list(cached[2])
        result = self._list_artists(roots)
        with self._lock:
            self._artists = (time.monotonic(), roots, result)
        return list(result)

    def _list_artists(self, roots: Iterable[str]) -> List[Artist]:
        result: List[Artist] = []
        for root in roots:
            if not is_network_path(root) and not safe_exists(root):
                continue
            try:
                entries = list(os.scandir(root))
            except OSError as e:
                logger.warning(f"无法读取根目录 {root}: {e}")
                continue
            has_loose = False
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        name, aid = parse_artist(entry.path)
                        result.append(Artist(os.path.normpath(entry.path), name, aid, root))
                    elif not has_loose and is_image(entry.name):
                        has_loose = True
                except OSError:
                    continue
            if has_loose:
                result.append(Artist(root, os.path.basename(root.rstrip("\\/")) or root, None, root, loose=True))
        return self._merge_same_artist(result)

    def _guess_artist_id(self, folder: str) -> Optional[int]:
        """文件夹名里没有画师 ID 时（例如名字以句号结尾，在 Windows 上显示成乱码短名），
        看里面头几个作品在元数据库里属于谁。"""
        pids: List[int] = []
        try:
            with os.scandir(folder) as it:
                for i, e in enumerate(it):
                    m = PIXIV_RE.search(e.name)
                    if m:
                        pids.append(int(m.group(1)))
                    if len(pids) >= 4 or i > 60:
                        break
        except OSError:
            return None
        owners = {m.get("author_id") for m in self.metadata(pids).values() if m and m.get("author_id")}
        return int(next(iter(owners))) if len(owners) == 1 else None

    def _merge_same_artist(self, artists: List[Artist]) -> List[Artist]:
        """同一位画师有多个文件夹时（改名、名字里有 Windows 不支持的字符等）合并成一位：
        保留名字正常的那个作为主文件夹，其余的记在 extra 里，扫描时一起读、重复的作品只算一份。"""
        names = self.author_names() or {}
        guessed = set()
        for a in artists:
            if a.id is None and not a.loose:
                a.id = self._guess_artist_id(a.key)
                if a.id is not None:
                    guessed.add(a.key)
        groups: Dict[tuple, List[Artist]] = {}
        out: List[Artist] = []
        for a in artists:
            if a.id is None:
                out.append(a)
            else:
                groups.setdefault((a.root, a.id), []).append(a)
        for (_root, aid), group in groups.items():
            # 主文件夹：优先名字里带 ID 的，其次不是 Unknown 的
            group.sort(key=lambda a: (a.key in guessed, a.name.lower() == "unknown", a.key))
            main = group[0]
            main.extra = [a.key for a in group[1:]]
            if (main.key in guessed or main.name.lower() == "unknown") and names.get(aid):
                main.name = names[aid]
            out.append(main)
        return out

    def artist_by_key(self, key: str) -> Optional[Artist]:
        for a in self.artists():
            if a.key == key:
                return a
        return None

    # ================= 扫描 =================
    def _signature(self, artist: Artist) -> float:
        """这位画师所有文件夹的修改时间之和：任何一个有增删都会变"""
        total = 0.0
        for folder in artist.folders:
            try:
                total += os.stat(folder).st_mtime
            except OSError:
                total -= 1.0
        return total

    def forget_artists(self) -> None:
        """丢掉画师列表缓存（文件夹可能有增减），已扫描的作品保留"""
        with self._lock:
            self._artists = None

    def scan_artist(self, artist: Artist, force: bool = False, read_dims: bool = True,
                    recheck: bool = False) -> List[Work]:
        """recheck=True：不管多久前检查过，都重新看一眼文件夹的修改时间"""
        with self._lock:
            cached = self._scans.get(artist.key)
        now = time.time()
        if cached and not force and not recheck and now - cached.checked_at < RECHECK_SECS:
            return cached.works
        # 文件夹修改时间没变就继续用缓存，不因为“过了几分钟”而把整个资料库重扫一遍
        sig = self._signature(artist)
        if cached and not force and cached.signature == sig:
            cached.checked_at = now
            return cached.works
        works = self._scan_folder(artist, read_dims)
        with self._lock:
            self._scans[artist.key] = _FolderScan(sig, time.time(), works, time.time())
            for w in works:
                self._register(w)
            self.version += 1
        return works

    def _register(self, w: Work) -> None:
        """登记作品；同一个 Pixiv 作品出现在两个画师文件夹里时，给其中一份换一个不重复的 key。

        规则与扫描顺序无关：文件夹路径较小的那份保留原 key，其余的变成 “作品ID@文件夹校验码”。
        否则前端按 key 定位时会打开另一位画师的那一份。
        """
        base = w.key.split("@", 1)[0] if w.pid else w.key
        other = self._works.get(base)
        if not w.pid or other is None or other is w or other.artist_key == w.artist_key:
            w.key = base
            self._works[base] = w
            return
        loser = w if w.artist_key > other.artist_key else other
        winner = other if loser is w else w
        loser.key = f"{base}@{zlib.crc32(loser.artist_key.encode('utf-8')):08x}"
        winner.key = base
        self._works[base] = winner
        self._works[loser.key] = loser

    def cached_works(self, artist_key: str) -> Optional[List[Work]]:
        with self._lock:
            scan = self._scans.get(artist_key)
            return scan.works if scan else None

    def _iter_files(self, artist: Artist):
        if artist.loose:
            try:
                for e in os.scandir(artist.key):
                    if e.is_file() and is_image(e.name):
                        yield e
            except OSError:
                return
            return
        # 先主文件夹后其余文件夹：同一个文件在两处都有时，保留主文件夹里的那份
        stack = list(reversed(artist.folders))
        count = 0
        while stack:
            folder = stack.pop()
            try:
                entries = list(os.scandir(folder))
            except OSError:
                continue
            for e in entries:
                try:
                    if e.is_dir(follow_symlinks=False):
                        stack.append(e.path)
                    elif is_image(e.name):
                        count += 1
                        if count > MAX_FILES_PER_FOLDER:
                            logger.warning(f"{artist.key} 中的图片超过 {MAX_FILES_PER_FOLDER} 张，其余未显示")
                            return
                        yield e
                except OSError:
                    continue

    def _scan_folder(self, artist: Artist, read_missing: bool = True) -> List[Work]:
        pages: List[Page] = []
        for e in self._iter_files(artist):
            try:
                st = e.stat()
            except OSError:
                continue
            pages.append(Page(os.path.normpath(e.path), e.name, st.st_mtime, st.st_size))
        # 宽高：先查缓存，缺的读文件头并写回缓存
        # 首次扫描网络盘时逐个打开文件很慢（每张几十毫秒），read_missing=False 时先留空，之后由 fill_dims 补
        known = self.store.get_dims([(p.path, p.mtime, p.size) for p in pages])
        new_rows = []
        for p in pages:
            if p.path in known:
                p.w, p.h = known[p.path]
            elif read_missing:
                p.w, p.h = read_dims(p.path)
                new_rows.append((p.path, p.mtime, p.size, p.w, p.h))
        self.store.set_dims(new_rows)
        # 分组为作品
        works: Dict[str, Work] = {}
        for p in pages:
            m = PIXIV_RE.search(p.file)
            if m:
                pid, p.page = int(m.group(1)), int(m.group(2))
                key = str(pid)
            else:
                pid, key = 0, p.path
            w = works.get(key)
            if w is None:
                w = works[key] = Work(key, pid, artist.key)
            elif artist.extra and any(q.file == p.file for q in w.pages):
                continue      # 另一个文件夹里的同一个文件
            w.pages.append(p)
        for w in works.values():
            w.pages.sort(key=lambda p: (p.page, p.file))
            w.mtime = max(p.mtime for p in w.pages)
        return list(works.values())

    def fill_dims(self, should_stop: Callable[[], bool] = lambda: False) -> int:
        """给扫描时跳过的图片补上宽高（直接写回缓存的作品对象），返回补了多少张"""
        with self._lock:
            pending = [p for scan in self._scans.values() for w in scan.works for p in w.pages if not p.w]
        rows, done = [], 0
        for p in pending:
            if should_stop():
                break
            p.w, p.h = read_dims(p.path)
            rows.append((p.path, p.mtime, p.size, p.w, p.h))
            if len(rows) >= 200:
                self.store.set_dims(rows)
                done += len(rows)
                rows = []
        self.store.set_dims(rows)
        if done or rows:
            self.version += 1
        return done + len(rows)

    def all_works(self, scan: bool = True) -> List[Work]:
        out: List[Work] = []
        for a in self.artists():
            works = self.scan_artist(a) if scan else (self.cached_works(a.key) or [])
            out.extend(works)
        return out

    def work(self, key: str) -> Optional[Work]:
        with self._lock:
            return self._works.get(key.split("#", 1)[0])

    def invalidate(self) -> None:
        with self._lock:
            self._scans.clear()
            self._works.clear()
            self._artists = None
            self.version += 1

    # ================= Pixiv 元数据 =================
    def _ensure_meta_source(self) -> None:
        src = str(self.reader.db_path) if self.reader.db_path else None
        if src != self._meta_source:
            self._meta.clear()
            self._meta_source = src

    def metadata(self, pids: Iterable[int]) -> Dict[int, Optional[dict]]:
        self._ensure_meta_source()
        pids = [p for p in dict.fromkeys(pids) if p]
        missing = [p for p in pids if p not in self._meta]
        if missing and self.reader.db_path:
            # 借用读取器的批量查询（只读连接 + 缓存）；用虚拟文件名让它按作品 ID 查询
            batch = self.reader.get_metadata_batch([f"{p}_p0.jpg" for p in missing])
            for p in missing:
                info = batch.get(f"{p}_p0.jpg")
                self._meta[p] = self._compact(info) if info else None
        return {p: self._meta.get(p) for p in pids}

    def _compact(self, info: dict) -> dict:
        meta = info.get("meta") or {}
        artist = info.get("artist") or {}
        level = self.reader.get_rating_level(meta)
        posted = 0.0
        raw = meta.get("create_date") or meta.get("upload_date") or ""
        if raw:
            try:
                posted = datetime.fromisoformat(str(raw).replace("Z", "+00:00")[:25]).timestamp()
            except ValueError:
                try:
                    posted = datetime.strptime(str(raw)[:10], "%Y-%m-%d").timestamp()
                except ValueError:
                    posted = 0.0
        caption = str(meta.get("caption") or meta.get("description") or "")
        caption = TAG_STRIP_RE.sub("", caption.replace("<br />", "\n").replace("<br/>", "\n").replace("<br>", "\n")).strip()
        return {
            "title": str(meta.get("title") or ""),
            "tags": [str(t) for t in (meta.get("tags") or []) if str(t).strip()],
            "rating": {"r18": "r18", "r18g": "r18g"}.get(level, "safe"),
            "ai": bool(self.reader.is_ai_from_meta(meta)),
            "posted": posted,
            "caption": caption,
            "author_id": meta.get("author_id"),
            "author_name": artist.get("name") or artist.get("author_name") or "",
            "bookmarks": meta.get("bookmark_count") or meta.get("total_bookmarks") or 0,
            "views": meta.get("view_count") or meta.get("total_view") or 0,
        }

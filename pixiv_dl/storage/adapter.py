import io
import logging
import os
import threading
import time
from pathlib import Path

from pixiv_dl.config import Config
import pixiv_dl.storage.backends as sb
from pixiv_dl.storage.backends import Backend  # noqa: F401  (兼容旧的导入)

logger = logging.getLogger("PixivDownloader")

_FOLDER_CACHE_TTL = 60  # 秒：画师目录列表缓存时间


class StorageAdapter:
    """统一存储接口：本地 / SMB(NAS) / WebDAV / FTP(FTPS) / SFTP。

    所有路径都是相对「保存根目录」的相对路径，用 / 分隔。协议相关的操作交给 storage_backends 里的后端；
    这里只放与协议无关的逻辑（画师目录查找 / 重命名策略 / 缓存、最小文件大小校验）。
    """

    def __init__(self, backend=None):
        self.mode = Config.STORAGE_MODE
        self._folders = {}                 # author_id -> 已确定的目录名
        self._base_dirs = None             # (时间戳, [目录名]) 缓存
        self._cache_lock = threading.Lock()
        self.backend = backend or sb.make_backend(self.mode)

    # ------------------------------------------------------------------ 查询
    def exists(self, rel_path):
        return self.backend.stat(rel_path) is not None

    def get_file_size(self, rel_path):
        """文件大小；不存在（或是目录）返回 0。"""
        st = self.backend.stat(rel_path)
        return 0 if st is None or st[0] else st[1]

    def list_dir(self, rel_path=""):
        """列目录，返回 {名称: (是否目录, 大小)}；目录不存在返回 None。"""
        return self.backend.listdir(rel_path)

    def read(self, rel_path):
        """读取整个文件内容（前端浏览用）。"""
        return self.backend.read(rel_path)

    # ------------------------------------------------------------------ 写入
    def makedirs(self, rel_path):
        self.backend.mkdirs(rel_path)

    def put_bytes(self, rel_path, data, min_size=100):
        """原子写入一个文件，返回写入字节数。中途失败不会在最终路径留下半个文件。"""
        if len(data) < min_size:
            raise ValueError(f"文件过小 ({len(data)} bytes)，可能被拦截")
        self.backend.write(rel_path, lambda: io.BytesIO(data), len(data))
        return len(data)

    def put_file(self, rel_path, local_path, min_size=100):
        """把本地文件原子地放到存储里（动图 webp / zip）。返回字节数。"""
        size = os.path.getsize(local_path)
        if size < min_size:
            raise ValueError(f"文件过小 ({size} bytes)")
        self.backend.write(rel_path, lambda: open(local_path, "rb"), size)
        return size

    # 兼容旧接口
    def save_atomic(self, rel_path, data_bytes):
        self.put_bytes(rel_path, data_bytes)
        return True, rel_path

    # ------------------------------------------------------------------ 画师目录
    def _list_base_dirs(self, force=False):
        with self._cache_lock:
            if not force and self._base_dirs and time.time() - self._base_dirs[0] < _FOLDER_CACHE_TTL:
                return self._base_dirs[1]
        listing = self.list_dir("")
        if listing is None:
            self.backend.mkdirs("")
            listing = {}
        dirs = [n for n, (is_dir, _) in listing.items() if is_dir]
        with self._cache_lock:
            self._base_dirs = (time.time(), dirs)
        return dirs

    def find_artist_folder(self, aid):
        """按 `[aid]` 前缀查找已存在的画师目录，找不到返回 None。"""
        prefix = f"[{aid}]"
        for name in self._list_base_dirs():
            if name.startswith(prefix):
                return name
        return None

    def get_artist_folder(self, aid, name, rename=True):
        """返回画师目录名。

        rename=True（同步阶段，名字是最新的）：已有目录名与新名不同则重命名；
        rename=False（下载/核查阶段）：沿用已有目录，绝不重命名。
        """
        new_name = Config.FOLDER_FORMAT.format(author_id=aid, author_name=name)
        cached = self._folders.get(aid)
        if cached == new_name or (cached and not rename):
            return cached
        existing = self.find_artist_folder(aid)
        if existing is None:
            self._folders[aid] = new_name
            return new_name
        if existing != new_name and rename:
            try:
                self.backend.rename(existing, new_name)
                with self._cache_lock:
                    self._base_dirs = None
                existing = new_name
            except Exception as e:
                logger.warning(f"重命名画师目录失败 {existing} -> {new_name}: {e}（沿用旧目录）")
        self._folders[aid] = existing
        return existing

    # ------------------------------------------------------------------ 状态
    def ping(self):
        """返回 (ok, message)，用于前端展示存储是否可用。"""
        try:
            return self.backend.ping()
        except Exception as e:
            return False, str(e)

    def describe(self):
        return self.backend.describe()

    def close(self):
        self.backend.close()

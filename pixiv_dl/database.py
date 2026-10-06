import os
import sqlite3
import logging
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta

from pixiv_dl.db_queries import QueryMixin

logger = logging.getLogger("PixivDownloader")

# 任务状态
ST_PENDING = 0
ST_DONE = 1
ST_RUNNING = 2
ST_FAILED = -1
ST_IGNORED = -2   # 用户选择忽略的失败任务（不再自动重试，也不计入失败）

# artists 表允许通过 upsert_artist 写入的字段
ARTIST_FIELDS = (
    'profile_image_url', 'profile_image_local', 'author_account', 'author_comment',
    'total_illusts', 'total_bookmarks', 'is_followed', 'is_private_follow', 'is_deleted',
    'is_temp_name', 'twitter_account', 'webpage', 'gender', 'birth', 'region', 'job',
    'pawoo_url', 'background_image_url',
)

# 新版本需要、旧库可能缺少的列：只做 ADD COLUMN，绝不修改/删除已有数据
ARTIST_COLUMNS = [
    ('profile_image_url', 'TEXT'), ('author_account', 'TEXT'), ('author_comment', 'TEXT'),
    ('total_illusts', 'INTEGER'), ('total_bookmarks', 'INTEGER'),
    ('is_followed', 'INTEGER DEFAULT 0'), ('is_private_follow', 'INTEGER DEFAULT 0'),
    ('is_deleted', 'INTEGER DEFAULT 0'), ('twitter_account', 'TEXT'), ('webpage', 'TEXT'),
    ('is_temp_name', 'INTEGER DEFAULT 0'), ('profile_image_local', 'TEXT'),
    ('gender', 'TEXT'), ('birth', 'TEXT'), ('region', 'TEXT'), ('job', 'TEXT'),
    ('pawoo_url', 'TEXT'), ('background_image_url', 'TEXT'),
]
METADATA_COLUMNS = [
    ('total_view', 'INTEGER DEFAULT 0'), ('total_bookmarks', 'INTEGER DEFAULT 0'),
    ('is_bookmarked', 'INTEGER DEFAULT 0'), ('ai_type', 'INTEGER DEFAULT 1'),
    ('ugoira_data', 'TEXT'), ('tags_translated', 'TEXT'),
]
METADATA_FIELDS = (
    'illust_id', 'author_id', 'title', 'create_date', 'tags', 'x_restrict', 'is_r18',
    'page_count', 'width', 'height', 'sanity_level', 'illust_type', 'series_id',
    'series_title', 'tools', 'caption', 'total_view', 'total_bookmarks', 'is_bookmarked',
    'ai_type', 'ugoira_data', 'tags_translated',
)
ILLUST_COLUMNS = [('last_error', 'TEXT'), ('error_kind', 'TEXT')]

# 失败原因分类（error_kind）
ERROR_KINDS = ('deleted', 'restricted', 'network', 'http', 'storage', 'content', 'api', 'auth', 'other')

INDEXES = {
    'idx_illust_unique': "CREATE UNIQUE INDEX IF NOT EXISTS idx_illust_unique ON illusts (illust_id, page_index)",
    'idx_illust_id': "CREATE INDEX IF NOT EXISTS idx_illust_id ON illusts (illust_id)",
    'idx_illust_status': "CREATE INDEX IF NOT EXISTS idx_illust_status ON illusts (status)",
    'idx_metadata_author': "CREATE INDEX IF NOT EXISTS idx_metadata_author ON illust_metadata (author_id)",
    'idx_illust_hash': "CREATE INDEX IF NOT EXISTS idx_illust_hash ON illusts (file_hash)",
    'idx_illust_dldate': "CREATE INDEX IF NOT EXISTS idx_illust_dldate ON illusts (download_date)",
}

_PATH_LOCKS = {}
_PATH_LOCKS_GUARD = threading.Lock()
_SCHEMA_DONE = set()
_SCHEMA_GUARD = threading.Lock()
_tls = threading.local()


def _lock_for(path):
    key = os.path.abspath(path)
    with _PATH_LOCKS_GUARD:
        if key not in _PATH_LOCKS:
            _PATH_LOCKS[key] = threading.RLock()
        return _PATH_LOCKS[key]


class Database(QueryMixin):
    """SQLite 数据库访问层。

    表：
    - artists: 画师信息
    - illust_metadata: 作品级元数据（每个作品一条）
    - illusts: 下载任务（每页一条）
    - alerts: 需要人工介入的告警

    线程模型：同一进程内对同一个库文件的写操作由一把进程内锁串行化，
    每个线程请使用自己的连接（见 Database.local）。连接带 30 秒 busy_timeout。
    """

    def __init__(self, db_path):
        self.db_path = db_path
        db_dir = os.path.dirname(db_path)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)
        self.conn = sqlite3.connect(db_path, timeout=30, check_same_thread=False)
        self.conn.execute("PRAGMA busy_timeout = 30000")
        self._apply_journal_mode()
        self._wlock = _lock_for(db_path)
        key = os.path.abspath(db_path)
        with _SCHEMA_GUARD:
            if key not in _SCHEMA_DONE:
                self._ensure_schema()
                _SCHEMA_DONE.add(key)

    def _apply_journal_mode(self):
        """按配置（DB_JOURNAL）设置日志模式；与当前一致时什么都不做，所以默认配置下不会改动已有数据库。"""
        try:
            from pixiv_dl.config import Config
            want = str(getattr(Config, 'DB_JOURNAL', 'delete')).lower()
            if want not in ('delete', 'wal'):
                return
            cur = str(self.conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
            if cur != want:
                self.conn.execute(f"PRAGMA journal_mode = {want}")
        except Exception as e:
            logger.warning(f"设置数据库日志模式失败（已忽略）: {e}")

    @classmethod
    def local(cls, db_path):
        """返回当前线程专属的连接（按路径缓存）。"""
        cache = getattr(_tls, 'dbs', None)
        if cache is None:
            cache = _tls.dbs = {}
        key = os.path.abspath(db_path)
        db = cache.get(key)
        if db is None:
            db = cache[key] = cls(db_path)
        return db

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass

    @contextmanager
    def tx(self):
        """写事务：进程内串行 + 自动提交/回滚。"""
        with self._wlock:
            with self.conn:
                yield self.conn

    # ------------------------------------------------------------------ 结构与迁移
    def _table_exists(self, name):
        return self.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None

    def _columns(self, table):
        return [r[1] for r in self.conn.execute(f"PRAGMA table_info({table})").fetchall()]

    def _backup_before_migration(self, reason):
        """迁移会修改已有数据库结构时，先用 SQLite 在线备份 API 留一份副本。"""
        try:
            if not os.path.exists(self.db_path) or os.path.getsize(self.db_path) < 4096:
                return None
            from pixiv_dl import dbtools
            dest = dbtools.create_backup(self.db_path, 'pre_v2', conn=self.conn)   # 原子写入，中断不会留下残缺文件
            logger.info(f"数据库结构需要升级（{reason}），已备份到 {dest}")
            return dest
        except Exception as e:
            logger.warning(f"迁移前备份失败: {e}")
            raise

    def _ensure_schema(self):
        existing = self._table_exists('illusts')
        legacy = False
        if existing:
            legacy = 'author_id' in self._columns('illusts')

        # 先判断是否有缺失项；有缺失才备份（全新库、已是最新结构的库不备份）
        needs = []
        if existing:
            if legacy:
                needs.append("旧版 illusts 表")
            if self._table_exists('artists'):
                have = set(self._columns('artists'))
                miss = [c for c, _ in ARTIST_COLUMNS if c not in have]
                if miss:
                    needs.append(f"artists 缺少列 {','.join(miss)}")
            if self._table_exists('illust_metadata'):
                have = set(self._columns('illust_metadata'))
                miss = [c for c, _ in METADATA_COLUMNS if c not in have]
                if miss:
                    needs.append(f"illust_metadata 缺少列 {','.join(miss)}")
            have = set(self._columns('illusts'))
            miss = [c for c, _ in ILLUST_COLUMNS if c not in have]
            if miss and not legacy:
                needs.append(f"illusts 缺少列 {','.join(miss)}")
        if needs:
            self._backup_before_migration("; ".join(needs))

        with self._wlock, self.conn:
            self.conn.execute('''CREATE TABLE IF NOT EXISTS artists (
                author_id INTEGER PRIMARY KEY,
                author_name TEXT,
                last_synced_id INTEGER DEFAULT 0,
                last_sync_time TIMESTAMP,
                profile_image_url TEXT,
                author_account TEXT,
                author_comment TEXT,
                total_illusts INTEGER,
                total_bookmarks INTEGER,
                is_followed INTEGER DEFAULT 0,
                is_private_follow INTEGER DEFAULT 0,
                is_deleted INTEGER DEFAULT 0,
                twitter_account TEXT,
                webpage TEXT,
                is_temp_name INTEGER DEFAULT 0,
                profile_image_local TEXT,
                gender TEXT, birth TEXT, region TEXT, job TEXT,
                pawoo_url TEXT, background_image_url TEXT
            )''')
            self.conn.execute('''CREATE TABLE IF NOT EXISTS illust_metadata (
                illust_id INTEGER PRIMARY KEY,
                author_id INTEGER,
                title TEXT,
                create_date TEXT,
                tags TEXT,
                x_restrict INTEGER DEFAULT 0,
                is_r18 INTEGER DEFAULT 0,
                page_count INTEGER DEFAULT 1,
                width INTEGER,
                height INTEGER,
                sanity_level INTEGER,
                illust_type INTEGER,
                series_id INTEGER,
                series_title TEXT,
                tools TEXT,
                caption TEXT,
                total_view INTEGER DEFAULT 0,
                total_bookmarks INTEGER DEFAULT 0,
                is_bookmarked INTEGER DEFAULT 0,
                ai_type INTEGER DEFAULT 1,
                ugoira_data TEXT,
                tags_translated TEXT,
                FOREIGN KEY (author_id) REFERENCES artists(author_id)
            )''')
            self.conn.execute('''CREATE TABLE IF NOT EXISTS illusts (
                task_key TEXT PRIMARY KEY,
                illust_id INTEGER,
                page_index INTEGER,
                url TEXT,
                media_type TEXT DEFAULT 'image',
                status INTEGER DEFAULT 0,
                updated_at TEXT,
                attempts INTEGER DEFAULT 0,
                file_hash TEXT,
                file_size INTEGER DEFAULT 0,
                download_date TEXT,
                original_filename TEXT,
                content_type TEXT,
                last_error TEXT,
                error_kind TEXT,
                FOREIGN KEY (illust_id) REFERENCES illust_metadata(illust_id)
            )''')
            self.conn.execute('''CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT, params TEXT, status TEXT,
                started REAL, finished REAL,
                total INTEGER, success INTEGER, failed INTEGER, skipped INTEGER, bytes INTEGER,
                error TEXT, summary TEXT
            )''')
            self.conn.execute('''CREATE TABLE IF NOT EXISTS artist_labels (
                author_id INTEGER PRIMARY KEY,
                pinned INTEGER DEFAULT 0,
                note TEXT,
                updated_at TEXT
            )''')
            self.conn.execute('''CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_key TEXT,
                level TEXT,
                message TEXT,
                created_at TEXT
            )''')

            if legacy:
                self._migrate_legacy_illusts()
            for table, cols in (('artists', ARTIST_COLUMNS), ('illust_metadata', METADATA_COLUMNS),
                                ('illusts', ILLUST_COLUMNS)):
                have = set(self._columns(table))
                for col, typ in cols:
                    if col not in have:
                        self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
            for sql in INDEXES.values():
                try:
                    self.conn.execute(sql)
                except sqlite3.Error as e:
                    logger.warning(f"创建索引失败（已忽略）: {e}")

    def _migrate_legacy_illusts(self):
        """旧版（illusts 表里带 author_id/title 等元数据）→ 元数据/任务分表。在 _ensure_schema 的事务内执行。
        旧表缺少的列按默认值处理；旧表本身保留为 illusts_old，不删除。"""
        logger.info("检测到旧版数据库，开始迁移...")
        old = set(self._columns('illusts'))

        def pick(col, default='NULL'):
            return col if col in old else default

        meta_cols = ['author_id', 'title', 'create_date', 'tags', 'x_restrict', 'is_r18', 'page_count', 'width',
                     'height', 'sanity_level', 'illust_type', 'series_id', 'series_title', 'tools', 'caption']
        defaults = {'x_restrict': '0', 'is_r18': '0', 'page_count': '1'}
        select = ", ".join(
            f"COALESCE({c}, {defaults[c]})" if c in defaults and c in old else pick(c, defaults.get(c, 'NULL'))
            for c in meta_cols)
        self.conn.execute(
            f"INSERT OR IGNORE INTO illust_metadata (illust_id, {', '.join(meta_cols)}) "
            f"SELECT DISTINCT illust_id, {select} FROM illusts WHERE illust_id IS NOT NULL")
        self.conn.execute("ALTER TABLE illusts RENAME TO illusts_old")
        self.conn.execute('''CREATE TABLE illusts (
            task_key TEXT PRIMARY KEY, illust_id INTEGER, page_index INTEGER, url TEXT,
            media_type TEXT DEFAULT 'image', status INTEGER DEFAULT 0, updated_at TEXT,
            attempts INTEGER DEFAULT 0, file_hash TEXT, file_size INTEGER DEFAULT 0,
            download_date TEXT, original_filename TEXT, content_type TEXT,
            FOREIGN KEY (illust_id) REFERENCES illust_metadata(illust_id))''')
        task_cols = ['task_key', 'illust_id', 'page_index', 'url', 'media_type', 'status', 'updated_at', 'attempts',
                     'file_hash', 'file_size', 'download_date', 'original_filename', 'content_type']
        tdef = {'media_type': "'image'", 'status': '0', 'attempts': '0', 'file_size': '0'}
        tselect = ", ".join(
            f"COALESCE({c}, {tdef[c]})" if c in tdef and c in old else pick(c, tdef.get(c, 'NULL'))
            for c in task_cols)
        self.conn.execute(f"INSERT INTO illusts ({', '.join(task_cols)}) SELECT {tselect} FROM illusts_old")
        logger.info("数据库迁移完成（旧表已保留为 illusts_old）")

    # ------------------------------------------------------------------ 画师
    def upsert_artist(self, aid, name, **fields):
        """插入或更新画师；只更新非 None 的字段。"""
        unknown = set(fields) - set(ARTIST_FIELDS)
        if unknown:
            raise TypeError(f"upsert_artist 不支持的字段: {sorted(unknown)}")
        updates, params = [], []
        if name is not None:
            updates.append("author_name = ?")
            params.append(name)
        for k in ARTIST_FIELDS:
            if fields.get(k) is not None:
                updates.append(f"{k} = ?")
                params.append(fields[k])
        with self.tx() as c:
            c.execute("INSERT OR IGNORE INTO artists (author_id, author_name) VALUES (?, ?)", (aid, name))
            if updates:
                c.execute(f"UPDATE artists SET {', '.join(updates)} WHERE author_id = ?", params + [aid])

    def get_artist(self, aid):
        """返回 (author_id, author_name, last_synced_id)；不存在时返回 (aid, 'Unknown', 0)。"""
        try:
            aid = int(aid) if aid is not None else 0
            res = self.conn.execute(
                "SELECT author_id, author_name, last_synced_id FROM artists WHERE author_id = ?", (aid,)).fetchone()
            if res:
                return (res[0], res[1] or f"User_{aid}", res[2] or 0)
            return (aid, "Unknown", 0)
        except Exception as e:
            logger.warning(f"数据库查询失败 get_artist({aid}): {e}")
            return (aid if isinstance(aid, int) else 0, "Unknown", 0)

    def artist_exists(self, aid):
        return self.conn.execute("SELECT 1 FROM artists WHERE author_id = ?", (aid,)).fetchone() is not None

    def get_artist_full(self, aid):
        cur = self.conn.execute("SELECT * FROM artists WHERE author_id = ?", (aid,))
        row = cur.fetchone()
        if not row:
            return None
        return dict(zip([d[0] for d in cur.description], row))

    def get_all_artists(self, include_deleted=False):
        """[(author_id, author_name, last_synced_id, is_deleted)]；默认不含已标记注销的画师。"""
        sql = "SELECT author_id, author_name, COALESCE(last_synced_id,0), COALESCE(is_deleted,0) FROM artists"
        if not include_deleted:
            sql += " WHERE COALESCE(is_deleted,0) = 0"
        return self.conn.execute(sql + " ORDER BY author_id").fetchall()

    def get_deleted_artists(self):
        return self.conn.execute(
            "SELECT author_id, author_name, COALESCE(last_synced_id,0), 1 FROM artists "
            "WHERE COALESCE(is_deleted,0) = 1 ORDER BY author_id").fetchall()

    def get_artists_without_profile(self, limit=None):
        """缺少账号名/头像地址等基础资料的画师 [(author_id, author_name)]。"""
        sql = ("SELECT author_id, author_name FROM artists WHERE COALESCE(is_deleted,0) = 0 AND "
               "(author_account IS NULL OR author_account = '' OR profile_image_url IS NULL) ORDER BY author_id")
        params = []
        if limit:
            sql += " LIMIT ?"
            params.append(int(limit))
        return self.conn.execute(sql, params).fetchall()

    def get_artists_without_avatar(self, limit=None):
        """已知头像地址但本地没有头像的画师 [(author_id, author_name, profile_image_url)]。"""
        sql = ("SELECT author_id, author_name, profile_image_url FROM artists "
               "WHERE profile_image_url IS NOT NULL AND profile_image_url != '' "
               "AND (profile_image_local IS NULL OR profile_image_local = '') ORDER BY author_id")
        params = []
        if limit:
            sql += " LIMIT ?"
            params.append(int(limit))
        return self.conn.execute(sql, params).fetchall()

    def mark_artist_synced(self, aid, last_synced_id):
        """水位线只增不减。"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self.tx() as c:
            c.execute(
                "UPDATE artists SET last_synced_id = MAX(COALESCE(last_synced_id,0), ?), last_sync_time = ? "
                "WHERE author_id = ?", (int(last_synced_id or 0), now, aid))

    def get_artist_summaries(self):
        """所有画师 + 任务统计 + 最近新增 + 置顶/备注（给前端/报表用），返回 dict 列表。"""
        counts = {}
        for aid, tasks, done, failed, pending in self.conn.execute(
                "SELECT m.author_id, COUNT(*), SUM(i.status = 1), SUM(i.status = -1), SUM(i.status IN (0,2)) "
                "FROM illusts i JOIN illust_metadata m ON m.illust_id = i.illust_id GROUP BY m.author_id"):
            counts[aid] = (tasks or 0, done or 0, failed or 0, pending or 0)
        recent = self.recent_counts_by_artist(7)
        labels = {r[0]: (r[1] or 0, r[2] or '') for r in
                  self.conn.execute("SELECT author_id, pinned, note FROM artist_labels")}
        out = []
        cur = self.conn.execute(
            "SELECT author_id, author_name, is_followed, is_private_follow, COALESCE(is_deleted,0), "
            "COALESCE(is_temp_name,0), last_sync_time, profile_image_local, total_illusts, author_account "
            "FROM artists")
        for r in cur.fetchall():
            t, d, f, p = counts.get(r[0], (0, 0, 0, 0))
            pinned, note = labels.get(r[0], (0, ''))
            out.append({
                'author_id': r[0], 'author_name': r[1], 'is_followed': r[2] or 0,
                'is_private_follow': r[3] or 0, 'is_deleted': r[4], 'is_temp_name': r[5],
                'last_sync_time': r[6], 'avatar': r[7], 'total_illusts': r[8], 'account': r[9],
                'tasks': t, 'done': d, 'failed': f, 'pending': p,
                'recent': recent.get(r[0], 0), 'pinned': pinned, 'note': note,
            })
        return out

    # ------------------------------------------------------------------ 作品 / 任务
    def get_ugoira_data(self, illust_id):
        res = self.conn.execute("SELECT ugoira_data FROM illust_metadata WHERE illust_id = ?", (illust_id,)).fetchone()
        return res[0] if res and res[0] else None

    def save_illust(self, data):
        """保存作品元数据 + 一个页面任务。返回 (新增了任务, 新增了作品)。

        data(dict) 必含 task_key/illust_id/page_index/url/media_type，以及 author_id 和任意元数据字段。
        已存在的任务不会被改状态；下载地址由下载阶段通过 API 重新获取，这里只刷新 url 作参考。
        """
        meta = {k: data[k] for k in METADATA_FIELDS if k in data}
        iid = meta.get('illust_id')
        new_task = new_work = False
        with self.tx() as c:
            if iid is not None:
                new_work = c.execute("SELECT 1 FROM illust_metadata WHERE illust_id = ?", (iid,)).fetchone() is None
                cols = list(meta.keys())
                placeholders = ','.join('?' for _ in cols)
                update_clause = ', '.join(f"{k}=excluded.{k}" for k in cols if k != 'illust_id')
                if update_clause:
                    c.execute(
                        f"INSERT INTO illust_metadata ({','.join(cols)}) VALUES ({placeholders}) "
                        f"ON CONFLICT(illust_id) DO UPDATE SET {update_clause}", [meta[k] for k in cols])
                else:
                    c.execute("INSERT OR IGNORE INTO illust_metadata (illust_id) VALUES (?)", (iid,))
            task_key = data.get('task_key')
            if task_key:
                cur = c.execute(
                    "INSERT OR IGNORE INTO illusts (task_key, illust_id, page_index, url, media_type, status) "
                    "VALUES (?,?,?,?,?,0)",
                    (task_key, iid, data.get('page_index', 0), data.get('url'), data.get('media_type', 'image')))
                new_task = cur.rowcount > 0
                url = data.get('url')
                if url and '://' in url and not url.startswith(('ugoira://', 'novel://')):
                    c.execute("UPDATE illusts SET url = ? WHERE task_key = ? AND COALESCE(url,'') != ?",
                              (url, task_key, url))
        return new_task, new_work

    def save_novel(self, novel_id, author_id, title, meta_json=None):
        """小说任务：沿用 novel_{id}_0 的任务键。

        小说与插画的 ID 空间是分开的，但本库的元数据表以 ID 为主键、任务表对 (illust_id, page_index) 唯一。
        如果小说 ID 恰好与已有插画相同，就无法存放（否则会覆盖插画的元数据或把小说归到错误的画师），
        此时跳过并返回 False。元数据只在不存在时插入。"""
        with self.tx() as c:
            c.execute("INSERT OR IGNORE INTO illust_metadata (illust_id, author_id, title) VALUES (?,?,?)",
                      (novel_id, author_id, title))
            row = c.execute("SELECT author_id FROM illust_metadata WHERE illust_id = ?", (novel_id,)).fetchone()
            if row and row[0] != author_id:
                logger.warning(f"小说 {novel_id} 的 ID 与其他画师的作品冲突，已跳过")
                return False
            cur = c.execute(
                "INSERT OR IGNORE INTO illusts (task_key, illust_id, page_index, url, media_type, status) "
                "VALUES (?,?,?,?,?,0)", (f"novel_{novel_id}_0", novel_id, 0, f"novel://{novel_id}", 'novel'))
            if cur.rowcount == 0:
                exists = c.execute("SELECT media_type FROM illusts WHERE task_key = ?",
                                   (f"novel_{novel_id}_0",)).fetchone()
                if not exists:
                    logger.warning(f"小说 {novel_id} 的 ID 与已有插画冲突，已跳过")
                return False   # 已存在或冲突：都不算新增
            return True

    def set_ugoira_data(self, illust_id, data_json):
        with self.tx() as c:
            c.execute("UPDATE illust_metadata SET ugoira_data = ? WHERE illust_id = ?", (data_json, illust_id))

    _TASK_COLS = ("i.task_key, i.illust_id, i.page_index, m.author_id, m.title, i.url, i.media_type, "
                  "m.create_date, m.tags, m.page_count")

    def restrict_levels(self, illust_ids):
        """{作品 ID: x_restrict}，只包含 R-18(1) / R-18G(2) 的作品"""
        ids, out = list(illust_ids), {}
        for i in range(0, len(ids), 800):
            chunk = ids[i:i + 800]
            cur = self.conn.execute(f"SELECT illust_id, x_restrict FROM illust_metadata "
                                    f"WHERE x_restrict > 0 AND illust_id IN ({','.join('?' * len(chunk))})", chunk)
            out.update({iid: int(lv) for iid, lv in cur.fetchall()})
        return out

    def get_pending_tasks(self, author_id=None, limit=None, max_attempts=None, author_ids=None):
        """待处理任务（status 0 / -1）。返回 10 列：
        task_key, illust_id, page_index, author_id, title, url, media_type, create_date, tags, page_count"""
        query = (f"SELECT {self._TASK_COLS} FROM illusts i "
                 "LEFT JOIN illust_metadata m ON i.illust_id = m.illust_id WHERE i.status IN (0, -1)")
        params = []
        if max_attempts is not None:
            query += " AND (i.attempts IS NULL OR i.attempts < ?)"
            params.append(max_attempts)
        if author_id:
            query += " AND m.author_id = ?"
            params.append(author_id)
        if author_ids:
            query += f" AND m.author_id IN ({','.join('?' * len(author_ids))})"
            params += [int(a) for a in author_ids]
        query += " ORDER BY m.author_id, i.illust_id, i.page_index"
        if limit:
            query += " LIMIT ?"
            params.append(int(limit))
        return self.conn.execute(query, params).fetchall()

    def count_pending(self, author_id=None, max_attempts=None):
        q = ("SELECT COUNT(*) FROM illusts i LEFT JOIN illust_metadata m ON i.illust_id = m.illust_id "
             "WHERE i.status IN (0, -1)")
        params = []
        if max_attempts is not None:
            q += " AND (i.attempts IS NULL OR i.attempts < ?)"
            params.append(max_attempts)
        if author_id:
            q += " AND m.author_id = ?"
            params.append(author_id)
        return self.conn.execute(q, params).fetchone()[0]

    def get_all_downloaded(self):
        return self.conn.execute(
            f"SELECT {self._TASK_COLS} FROM illusts i "
            "LEFT JOIN illust_metadata m ON i.illust_id = m.illust_id WHERE i.status = 1").fetchall()

    def get_storage_check_list(self, author_id=None):
        """核查用：task_key, illust_id, page_index, author_id, title, url, media_type, status"""
        q = ("SELECT i.task_key, i.illust_id, i.page_index, m.author_id, m.title, i.url, i.media_type, i.status "
             "FROM illusts i LEFT JOIN illust_metadata m ON i.illust_id = m.illust_id")
        params = []
        if author_id:
            q += " WHERE m.author_id = ?"
            params.append(author_id)
        return self.conn.execute(q + " ORDER BY m.author_id, i.illust_id, i.page_index", params).fetchall()

    def get_failed_tasks(self, author_id=None, attempts_lt=None, limit=None):
        query = (f"SELECT {self._TASK_COLS}, i.attempts, i.updated_at FROM illusts i "
                 "LEFT JOIN illust_metadata m ON i.illust_id = m.illust_id WHERE i.status = -1")
        params = []
        if author_id:
            query += " AND m.author_id = ?"
            params.append(author_id)
        if attempts_lt is not None:
            query += " AND i.attempts < ?"
            params.append(attempts_lt)
        query += " ORDER BY i.updated_at ASC"
        if limit:
            query += " LIMIT ?"
            params.append(int(limit))
        return self.conn.execute(query, params).fetchall()

    def get_task(self, task_key):
        cur = self.conn.execute(
            "SELECT i.task_key, i.illust_id, i.page_index, i.url, i.media_type, i.status, i.attempts, "
            "i.file_size, i.file_hash, i.download_date, m.author_id, m.title "
            "FROM illusts i LEFT JOIN illust_metadata m ON i.illust_id = m.illust_id WHERE i.task_key = ?",
            (task_key,))
        row = cur.fetchone()
        return dict(zip([d[0] for d in cur.description], row)) if row else None

    def list_tasks(self, status=None, q=None, author_id=None, limit=50, offset=0, kind=None):
        where, params = [], []
        if status is not None:
            where.append("i.status = ?")
            params.append(status)
        if author_id:
            where.append("m.author_id = ?")
            params.append(author_id)
        if kind:
            if kind == 'unknown':
                where.append("i.error_kind IS NULL")
            else:
                where.append("i.error_kind = ?")
                params.append(kind)
        if q:
            where.append("(m.title LIKE ? OR i.task_key LIKE ?)")
            params += [f"%{q}%", f"%{q}%"]
        w = (" WHERE " + " AND ".join(where)) if where else ""
        base = " FROM illusts i LEFT JOIN illust_metadata m ON i.illust_id = m.illust_id" + w
        total = self.conn.execute("SELECT COUNT(*)" + base, params).fetchone()[0]
        cur = self.conn.execute(
            "SELECT i.task_key, i.illust_id, i.page_index, i.media_type, i.status, i.attempts, i.file_size, "
            "i.updated_at, i.last_error, i.error_kind, m.author_id, m.title" + base +
            " ORDER BY i.updated_at DESC, i.task_key LIMIT ? OFFSET ?", params + [int(limit), int(offset)])
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()], total

    def list_works(self, author_id, q=None, limit=24, offset=0):
        where = "m.author_id = ? AND i.media_type != 'novel'"
        params = [author_id]
        if q:
            where += " AND m.title LIKE ?"
            params.append(f"%{q}%")
        total = self.conn.execute(
            f"SELECT COUNT(DISTINCT m.illust_id) FROM illust_metadata m JOIN illusts i ON i.illust_id = m.illust_id "
            f"WHERE {where}", params).fetchone()[0]
        cur = self.conn.execute(
            "SELECT m.illust_id, m.title, m.page_count, m.illust_type, m.total_bookmarks, m.is_r18, m.ai_type, "
            "m.create_date, COUNT(i.task_key) AS tasks, SUM(i.status = 1) AS done, "
            "MAX(CASE WHEN i.page_index = 0 THEN i.task_key END) AS first_key, "
            "MAX(CASE WHEN i.page_index = 0 THEN i.status END) AS first_status, MAX(i.media_type) AS media_type "
            f"FROM illust_metadata m JOIN illusts i ON i.illust_id = m.illust_id WHERE {where} "
            "GROUP BY m.illust_id ORDER BY m.illust_id DESC LIMIT ? OFFSET ?", params + [int(limit), int(offset)])
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()], total

    def export_failed_tasks_csv(self, out_path):
        import csv
        rows = self.get_failed_tasks()
        with open(out_path, 'w', newline='', encoding='utf-8-sig') as f:
            w = csv.writer(f)
            w.writerow(['task_key', 'illust_id', 'page_index', 'author_id', 'title', 'url', 'media_type',
                        'create_date', 'tags', 'page_count', 'attempts', 'updated_at'])
            w.writerows(rows)
        return out_path

    def reset_failed_tasks_by_filter(self, author_id=None):
        """失败任务 → 待处理，attempts 归零；返回受影响条数。"""
        with self.tx() as c:
            if author_id:
                cur = c.execute(
                    "UPDATE illusts SET status = 0, attempts = 0 WHERE status = -1 AND illust_id IN "
                    "(SELECT illust_id FROM illust_metadata WHERE author_id = ?)", (author_id,))
            else:
                cur = c.execute("UPDATE illusts SET status = 0, attempts = 0 WHERE status = -1")
            return cur.rowcount

    def mark_status(self, task_key, status):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self.tx() as c:
            if status == ST_DONE:
                c.execute("UPDATE illusts SET status = ?, updated_at = ?, attempts = 0, download_date = ?, "
                          "last_error = NULL, error_kind = NULL WHERE task_key = ?", (status, now, now, task_key))
            else:
                c.execute("UPDATE illusts SET status = ?, updated_at = ? WHERE task_key = ?", (status, now, task_key))

    def mark_failed(self, task_key, kind, message, permanent=False, max_attempts=3):
        """记录一次失败：状态置为失败，保存原因；permanent 时直接用满重试次数。返回新的 attempts。"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        kind = kind if kind in ERROR_KINDS else 'other'
        with self.tx() as c:
            if permanent:
                c.execute("UPDATE illusts SET status = -1, attempts = ?, updated_at = ?, last_error = ?, "
                          "error_kind = ? WHERE task_key = ?", (max_attempts, now, (message or '')[:500], kind, task_key))
            else:
                c.execute("UPDATE illusts SET status = -1, attempts = COALESCE(attempts,0) + 1, updated_at = ?, "
                          "last_error = ?, error_kind = ? WHERE task_key = ?",
                          (now, (message or '')[:500], kind, task_key))
            row = c.execute("SELECT attempts FROM illusts WHERE task_key = ?", (task_key,)).fetchone()
            return row[0] if row else None

    def bulk_set_status(self, task_keys, status, chunk=500):
        """批量改状态（核查用）。返回条数。"""
        if not task_keys:
            return 0
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self.tx() as c:
            for i in range(0, len(task_keys), chunk):
                part = task_keys[i:i + chunk]
                c.execute(f"UPDATE illusts SET status = ?, updated_at = ? WHERE task_key IN ({','.join('?' * len(part))})",
                          [status, now] + part)
        return len(task_keys)

    def bulk_mark_done(self, items):
        """items: [(task_key, file_size)] → 标记为已下载并补全文件大小。"""
        if not items:
            return 0
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self.tx() as c:
            c.executemany("UPDATE illusts SET status = 1, attempts = 0, updated_at = ?, "
                          "download_date = COALESCE(download_date, ?), file_size = ? WHERE task_key = ?",
                          [(now, now, size, key) for key, size in items])
        return len(items)

    def update_file_info(self, task_key, file_hash, file_size, original_filename=None, content_type=None):
        """更新文件信息；值为 None 的字段保持原值（不会把已有 hash 覆盖成 NULL）。"""
        updates, params = [], []
        for col, val in (('file_hash', file_hash), ('file_size', file_size),
                         ('original_filename', original_filename), ('content_type', content_type)):
            if val is not None:
                updates.append(f"{col} = ?")
                params.append(val)
        if not updates:
            return
        with self.tx() as c:
            c.execute(f"UPDATE illusts SET {', '.join(updates)} WHERE task_key = ?", params + [task_key])

    def update_task_url(self, task_key, url):
        with self.tx() as c:
            c.execute("UPDATE illusts SET url = ? WHERE task_key = ?", (url, task_key))

    def find_by_hash(self, file_hash):
        return self.conn.execute(
            "SELECT task_key, status, file_size FROM illusts WHERE file_hash = ? LIMIT 1", (file_hash,)).fetchone()

    def increment_attempts(self, task_key):
        with self.tx() as c:
            c.execute("UPDATE illusts SET attempts = COALESCE(attempts,0) + 1 WHERE task_key = ?", (task_key,))
            res = c.execute("SELECT attempts FROM illusts WHERE task_key = ?", (task_key,)).fetchone()
            return res[0] if res else None

    def set_attempts(self, task_key, attempts):
        with self.tx() as c:
            c.execute("UPDATE illusts SET attempts = ? WHERE task_key = ?", (attempts, task_key))

    def insert_alert(self, task_key, level, message):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self.tx() as c:
            c.execute("INSERT INTO alerts (task_key, level, message, created_at) VALUES (?,?,?,?)",
                      (task_key, level, message, now))

    def recent_alerts(self, limit=50):
        cur = self.conn.execute("SELECT id, task_key, level, message, created_at FROM alerts "
                                "ORDER BY id DESC LIMIT ?", (int(limit),))
        return [dict(zip([d[0] for d in cur.description], r)) for r in cur.fetchall()]

    def reset_in_progress(self):
        """把遗留的"进行中"(2) 任务放回待处理；用于程序启动 / 新任务开始前。返回条数。"""
        with self.tx() as c:
            return c.execute("UPDATE illusts SET status = 0 WHERE status = 2").rowcount

    def reset_stuck_tasks(self, hours):
        """status=2 且超过 hours 小时：attempts 未超限 → 待处理，否则标记永久失败并告警。"""
        cutoff = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
        try:
            from pixiv_dl.config import Config
            max_attempts = getattr(Config, 'MAX_ATTEMPTS', 3)
        except Exception:
            max_attempts = 3
        rows = self.conn.execute(
            "SELECT task_key, attempts FROM illusts WHERE status = 2 AND updated_at IS NOT NULL AND updated_at <= ?",
            (cutoff,)).fetchall()
        reclaimed = permanent = 0
        for task_key, attempts in rows:
            if (attempts or 0) >= max_attempts:
                with self.tx() as c:
                    c.execute("UPDATE illusts SET status = -1 WHERE task_key = ?", (task_key,))
                self.insert_alert(task_key, 'ERROR', f'任务达到最大重试次数({attempts})并被标记为永久失败')
                permanent += 1
            else:
                with self.tx() as c:
                    c.execute("UPDATE illusts SET status = 0 WHERE task_key = ?", (task_key,))
                reclaimed += 1
        return {'reclaimed': reclaimed, 'permanent_failed': permanent}

    # ------------------------------------------------------------------ 统计
    def stats(self):
        c = self.conn
        by_status = dict(c.execute("SELECT status, COUNT(*) FROM illusts GROUP BY status").fetchall())
        by_type = dict(c.execute("SELECT media_type, COUNT(*) FROM illusts GROUP BY media_type").fetchall())
        bytes_done = c.execute("SELECT COALESCE(SUM(file_size),0) FROM illusts WHERE status = 1").fetchone()[0]
        artists = c.execute(
            "SELECT COUNT(*), COALESCE(SUM(COALESCE(is_deleted,0)=1),0), COALESCE(SUM(is_followed=1),0) "
            "FROM artists").fetchone()
        return {
            'artists': artists[0], 'artists_deleted': artists[1], 'artists_followed': artists[2],
            'works': c.execute("SELECT COUNT(*) FROM illust_metadata").fetchone()[0],
            'tasks': sum(by_status.values()),
            'done': by_status.get(1, 0), 'pending': by_status.get(0, 0),
            'running': by_status.get(2, 0), 'failed': by_status.get(-1, 0), 'ignored': by_status.get(-2, 0),
            'bytes_done': bytes_done, 'by_type': by_type,
        }

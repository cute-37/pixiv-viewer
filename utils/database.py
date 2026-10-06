#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
数据库管理模块
处理图片元数据、标签、评分和缓存
"""
import sqlite3
import json
import threading
from pathlib import Path
from typing import List, Dict, Optional, Any

from utils.logger import get_logger
from utils.constants import DATA_DIR
from utils.schema import migrate

logger = get_logger("Database")

class DatabaseManager:
    _instance = None
    _lock = threading.RLock()  # 可重入：持锁的方法内部会调用其他持锁方法
    
    def __new__(cls, db_path=None):
        if db_path is not None:
            instance = super().__new__(cls)
            instance._initialized = False
            return instance
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super(DatabaseManager, cls).__new__(cls)
                    cls._instance._initialized = False
        return cls._instance
    
    def __init__(self, db_path=None):
        with self._lock:
            if self._initialized:
                return
            self.db_path = Path(db_path) if db_path is not None else DATA_DIR / "library.db"
            self._local = threading.local()
            self._schema_ready = False
            self._initialized = True
        
    @staticmethod
    def _baseline(conn):
        """结构版本 1：最初的表和索引（之前没有版本号的数据库也从这里开始，所以都带 IF NOT EXISTS）"""
        c = conn.cursor()

        # 图片主表
        c.execute('''
            CREATE TABLE IF NOT EXISTS images (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT UNIQUE,
                rating INTEGER DEFAULT 0,
                hash TEXT,
                width INTEGER,
                height INTEGER,
                file_size INTEGER,
                import_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # 标签定义表
        c.execute('''
            CREATE TABLE IF NOT EXISTS tags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE,
                color TEXT DEFAULT '#2563eb'
            )
        ''')

        # 图片-标签关联
        c.execute('''
            CREATE TABLE IF NOT EXISTS image_tags (
                image_id INTEGER,
                tag_id INTEGER,
                PRIMARY KEY (image_id, tag_id),
                FOREIGN KEY(image_id) REFERENCES images(id),
                FOREIGN KEY(tag_id) REFERENCES tags(id)
            )
        ''')

        # Pixiv 元数据
        c.execute('''
            CREATE TABLE IF NOT EXISTS meta_pixiv (
                image_id INTEGER PRIMARY KEY,
                pixiv_id TEXT,
                title TEXT,
                artist TEXT,
                artist_id TEXT,
                original_tags TEXT,
                description TEXT,
                FOREIGN KEY(image_id) REFERENCES images(id)
            )
        ''')

        # 创建索引以提高查询性能
        c.execute('CREATE INDEX IF NOT EXISTS idx_images_path ON images(path)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_images_rating ON images(rating)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_images_hash ON images(hash)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_images_width_height ON images(width, height)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_images_import_date ON images(import_date)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_tags_name ON tags(name)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_image_tags_image_id ON image_tags(image_id)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_image_tags_tag_id ON image_tags(tag_id)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_meta_pixiv_pixiv_id ON meta_pixiv(pixiv_id)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_meta_pixiv_artist_id ON meta_pixiv(artist_id)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_meta_pixiv_artist ON meta_pixiv(artist)')

    def _init_db(self):
        """初始化数据库表结构"""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        
        with self._open_connection() as conn:
            # 要改表结构时往这个列表末尾追加一步，不要改已有的（见 utils/schema.py）
            migrate(conn, [self._baseline], "library.db")

    def batch_save_pixiv_info(self, path_info_pairs: List[tuple[str, Dict[str, Any]]]):
        """批量保存多个图片的Pixiv元数据"""
        if not path_info_pairs:
            return

        conn = self.get_connection()
        with self._lock, conn:
            c = conn.cursor()
            # 获取或创建图片ID
            path_to_id = {}
            for path, _ in path_info_pairs:
                c.execute('SELECT id FROM images WHERE path = ?', (path,))
                row = c.fetchone()
                if row:
                    path_to_id[path] = row[0]
                else:
                    c.execute('INSERT INTO images (path) VALUES (?)', (path,))
                    path_to_id[path] = c.lastrowid

            # 批量插入元数据
            pixiv_data = []
            for path, info in path_info_pairs:
                img_id = path_to_id[path]
                pixiv_data.append((
                    img_id,
                    str(info.get('id', '')),
                    info.get('title', ''),
                    info.get('userName', ''),
                    str(info.get('userId', '')),
                    json.dumps(info.get('tags', [])),
                    info.get('description', '')
                ))
            
            c.executemany('''
                INSERT OR REPLACE INTO meta_pixiv 
                (image_id, pixiv_id, title, artist, artist_id, original_tags, description)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', pixiv_data)
            conn.commit()

    def get_connection(self):
        """获取线程本地的数据库连接"""
        # 快路径：表结构就绪后不再取锁。写操作会整段持有 _lock，
        # 如果读取也要先抢这把锁，批量写入期间界面线程的读操作会被一起卡住
        if self._schema_ready:
            return self._open_connection()
        with self._lock:
            if not self._schema_ready:
                self._init_db()
                self._schema_ready = True
        return self._open_connection()

    def _open_connection(self):
        if not hasattr(self._local, 'conn') or self._local.conn is None:
            self._local.conn = sqlite3.connect(self.db_path, check_same_thread=False)
            # 启用外键约束
            self._local.conn.execute('PRAGMA foreign_keys = ON')
            # 启用WAL模式以提高并发性能
            self._local.conn.execute('PRAGMA journal_mode = WAL')
            # 增加缓存大小
            self._local.conn.execute('PRAGMA cache_size = 10000')
            # 启用同步模式以提高性能（牺牲一点安全性）
            self._local.conn.execute('PRAGMA synchronous = NORMAL')
        return self._local.conn

    def close_thread_connection(self):
        """Release the calling thread's handle after a job or at shutdown."""
        conn = getattr(self._local, 'conn', None)
        if conn is not None:
            try:
                conn.close()
            finally:
                self._local.conn = None

    def get_image_id(self, path: str) -> Optional[int]:
        """获取图片ID，如果不存在则创建"""
        conn = self.get_connection()
        with self._lock, conn:  # 确保并发安全
            c = conn.cursor()
            c.execute('SELECT id FROM images WHERE path = ?', (path,))
            result = c.fetchone()
            if result:
                return result[0]
            
            try:
                c.execute('INSERT INTO images (path) VALUES (?)', (path,))
                conn.commit()
                return c.lastrowid
            except sqlite3.IntegrityError:
                # 并发情况下可能已存在
                c.execute('SELECT id FROM images WHERE path = ?', (path,))
                result = c.fetchone()
                return result[0] if result else None

    def set_rating(self, path: str, rating: int):
        """设置评分"""
        img_id = self.get_image_id(path)
        if not img_id: return
        
        conn = self.get_connection()
        with self._lock, conn:
            conn.execute('UPDATE images SET rating = ? WHERE id = ?', (rating, img_id))
            conn.commit()

    def get_rating(self, path: str) -> int:
        """获取评分"""
        conn = self.get_connection()
        c = conn.cursor()
        c.execute('SELECT rating FROM images WHERE path = ?', (path,))
        result = c.fetchone()
        return result[0] if result else 0

    def add_tag(self, path: str, tag_name: str):
        """添加标签"""
        img_id = self.get_image_id(path)
        if not img_id: return
        
        conn = self.get_connection()
        with self._lock, conn:
            c = conn.cursor()
            # 确保标签存在
            c.execute('INSERT OR IGNORE INTO tags (name) VALUES (?)', (tag_name,))
            c.execute('SELECT id FROM tags WHERE name = ?', (tag_name,))
            tag_id = c.fetchone()[0]
            
            # 关联
            c.execute('INSERT OR IGNORE INTO image_tags VALUES (?, ?)', (img_id, tag_id))
            conn.commit()

    def remove_tag(self, path: str, tag_name: str):
        """移除标签"""
        img_id = self.get_image_id(path)
        if not img_id: return
        
        conn = self.get_connection()
        with self._lock, conn:
            c = conn.cursor()
            c.execute('''
                DELETE FROM image_tags 
                WHERE image_id = ? AND tag_id IN (SELECT id FROM tags WHERE name = ?)
            ''', (img_id, tag_name))
            conn.commit()

    def get_tags(self, path: str) -> List[str]:
        """获取图片标签"""
        conn = self.get_connection()
        c = conn.cursor()
        c.execute('''
            SELECT t.name FROM tags t
            JOIN image_tags it ON t.id = it.tag_id
            JOIN images i ON i.id = it.image_id
            WHERE i.path = ?
        ''', (path,))
        return [r[0] for r in c.fetchall()]

    def get_all_tags(self) -> List[str]:
        """获取所有已存在的标签"""
        conn = self.get_connection()
        c = conn.cursor()
        c.execute('SELECT name FROM tags ORDER BY name')
        return [r[0] for r in c.fetchall()]

    def get_images_by_tag(self, tag_name: str) -> List[str]:
        """获取包含指定标签的所有图片路径"""
        conn = self.get_connection()
        c = conn.cursor()
        c.execute('''
            SELECT i.path FROM images i
            JOIN image_tags it ON i.id = it.image_id
            JOIN tags t ON t.id = it.tag_id
            WHERE t.name = ?
        ''', (tag_name,))
        return [r[0] for r in c.fetchall()]

    def batch_set_ratings(self, path_rating_pairs: List[tuple[str, int]]):
        """批量设置多个图片的评分"""
        if not path_rating_pairs:
            return

        conn = self.get_connection()
        with self._lock, conn:
            c = conn.cursor()
            # 获取或创建图片ID
            path_to_id = {}
            for path, _ in path_rating_pairs:
                c.execute('SELECT id FROM images WHERE path = ?', (path,))
                row = c.fetchone()
                if row:
                    path_to_id[path] = row[0]
                else:
                    c.execute('INSERT INTO images (path) VALUES (?)', (path,))
                    path_to_id[path] = c.lastrowid

            # 批量更新评分
            updates = [(rating, path_to_id[path]) for path, rating in path_rating_pairs]
            c.executemany('UPDATE images SET rating = ? WHERE id = ?', updates)
            conn.commit()

    def batch_get_ratings(self, paths: List[str]) -> Dict[str, int]:
        """批量获取多个图片的评分"""
        if not paths:
            return {}

        conn = self.get_connection()
        result = {}
        unique_paths = list(dict.fromkeys(paths))
        # 900 also supports older SQLite builds with a 999 variable limit.
        for offset in range(0, len(unique_paths), 900):
            chunk = unique_paths[offset:offset + 900]
            placeholders = ','.join('?' * len(chunk))
            result.update(conn.execute(
                f'SELECT path, rating FROM images WHERE path IN ({placeholders})', chunk
            ).fetchall())
        return result

    def batch_get_tags(self, paths: List[str]) -> Dict[str, List[str]]:
        """批量获取多个图片的标签"""
        if not paths:
            return {}

        conn = self.get_connection()
        c = conn.cursor()
        # 获取图片ID
        placeholders = ','.join('?' * len(paths))
        c.execute(f'SELECT id, path FROM images WHERE path IN ({placeholders})', paths)
        path_to_id = {path: img_id for img_id, path in c.fetchall()}

        if not path_to_id:
            return {}

        # 批量获取标签
        img_ids = list(path_to_id.values())
        img_placeholders = ','.join('?' * len(img_ids))
        c.execute(f'''
            SELECT i.path, t.name FROM images i
            JOIN image_tags it ON i.id = it.image_id
            JOIN tags t ON t.id = it.tag_id
            WHERE i.id IN ({img_placeholders})
            ORDER BY i.path, t.name
        ''', img_ids)

        result = {}
        for path, tag in c.fetchall():
            if path not in result:
                result[path] = []
            result[path].append(tag)
        return result

    def add_tags(self, paths: List[str], tag_names: List[str]):
        """为多张图片批量添加多个标签"""
        if not paths or not tag_names:
            return

        conn = self.get_connection()
        with self._lock, conn:
            c = conn.cursor()
            # 确保所有标签存在并取回 id
            tag_ids = {}
            for tag in tag_names:
                c.execute('INSERT OR IGNORE INTO tags (name) VALUES (?)', (tag,))
                c.execute('SELECT id FROM tags WHERE name = ?', (tag,))
                row = c.fetchone()
                if row:
                    tag_ids[tag] = row[0]

            # 批量获取或创建图片ID
            path_to_id = {}
            for path in paths:
                c.execute('SELECT id FROM images WHERE path = ?', (path,))
                row = c.fetchone()
                if row:
                    path_to_id[path] = row[0]
                else:
                    c.execute('INSERT INTO images (path) VALUES (?)', (path,))
                    path_to_id[path] = c.lastrowid

            # 批量插入关联，使用executemany
            associations = []
            for img_id in path_to_id.values():
                for tid in tag_ids.values():
                    associations.append((img_id, tid))
            
            c.executemany('INSERT OR IGNORE INTO image_tags VALUES (?, ?)', associations)
            conn.commit()

    def remove_tags(self, paths: List[str], tag_names: List[str]):
        """为多张图片批量移除多个标签"""
        if not paths or not tag_names:
            return

        conn = self.get_connection()
        with self._lock, conn:
            c = conn.cursor()
            # 获取标签ID
            tag_ids = []
            for tag in tag_names:
                c.execute('SELECT id FROM tags WHERE name = ?', (tag,))
                row = c.fetchone()
                if row:
                    tag_ids.append(row[0])

            if not tag_ids:
                return

            # 获取图片ID
            path_to_id = {}
            for path in paths:
                c.execute('SELECT id FROM images WHERE path = ?', (path,))
                row = c.fetchone()
                if row:
                    path_to_id[path] = row[0]

            if not path_to_id:
                return

            # 批量删除关联
            img_ids = list(path_to_id.values())
            placeholders = ','.join('?' * len(tag_ids))
            img_placeholders = ','.join('?' * len(img_ids))
            c.execute(f'''
                DELETE FROM image_tags 
                WHERE tag_id IN ({placeholders}) AND image_id IN ({img_placeholders})
            ''', tag_ids + img_ids)
            conn.commit()

    def rename_tag(self, old_name: str, new_name: str):
        """将标签重命名（合并到新标签），并保持关联正确。"""
        if not old_name or not new_name or old_name == new_name:
            return

        conn = self.get_connection()
        with self._lock, conn:
            c = conn.cursor()
            c.execute('SELECT id FROM tags WHERE name = ?', (old_name,))
            row = c.fetchone()
            if not row:
                return
            old_id = row[0]
            # 确保目标标签存在
            c.execute('INSERT OR IGNORE INTO tags (name) VALUES (?)', (new_name,))
            c.execute('SELECT id FROM tags WHERE name = ?', (new_name,))
            new_id = c.fetchone()[0]

            # 将旧标签关联的图片关联到新标签（去重）
            c.execute('INSERT OR IGNORE INTO image_tags (image_id, tag_id) SELECT image_id, ? FROM image_tags WHERE tag_id = ?', (new_id, old_id))

            # 删除旧标签的关联并删除标签定义
            c.execute('DELETE FROM image_tags WHERE tag_id = ?', (old_id,))
            c.execute('DELETE FROM tags WHERE id = ?', (old_id,))
            conn.commit()

    def merge_tags(self, source_tags: List[str], target_tag: str):
        """将多个 source_tags 合并为 target_tag（相当于对多个标签执行 rename 到同一目标）。"""
        if not source_tags or not target_tag:
            return
        for t in source_tags:
            if t == target_tag:
                continue
            self.rename_tag(t, target_tag)

    def get_tag_usage(self) -> List[Dict[str, Any]]:
        """返回所有标签及其被关联的图片数量（按数量降序）。"""
        conn = self.get_connection()
        c = conn.cursor()
        c.execute('''
            SELECT t.name, COUNT(it.image_id) as cnt
            FROM tags t
            LEFT JOIN image_tags it ON t.id = it.tag_id
            GROUP BY t.id
            ORDER BY cnt DESC, t.name
        ''')
        return [{'name': r[0], 'count': r[1]} for r in c.fetchall()]

    def save_pixiv_info(self, path: str, info: Dict[str, Any]):
        """保存 Pixiv 元数据"""
        img_id = self.get_image_id(path)
        if not img_id: return
        
        conn = self.get_connection()
        with self._lock, conn:
            conn.execute('''
                INSERT OR REPLACE INTO meta_pixiv 
                (image_id, pixiv_id, title, artist, artist_id, original_tags, description)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (
                img_id, 
                str(info.get('id', '')),
                info.get('title', ''),
                info.get('userName', ''),
                str(info.get('userId', '')),
                json.dumps(info.get('tags', [])),
                info.get('description', '')
            ))
            conn.commit()

    def get_pixiv_info(self, path: str) -> Optional[Dict[str, Any]]:
        """获取 Pixiv 元数据"""
        conn = self.get_connection()
        c = conn.cursor()
        c.execute('''
            SELECT m.* FROM meta_pixiv m
            JOIN images i ON i.id = m.image_id
            WHERE i.path = ?
        ''', (path,))
        
        row = c.fetchone()
        if not row: return None
        
        # row: image_id, pixiv_id, title, artist, artist_id, orig_tags, desc
        try:
            tags = json.loads(row[5]) if row[5] else []
        except (json.JSONDecodeError, TypeError):
            tags = []
            
        return {
            'id': row[1],
            'title': row[2],
            'userName': row[3],
            'userId': row[4],
            'tags': tags,
            'description': row[6]
        }

    def get_artist_average_rating(self, artist_id: str) -> float:
        """获取画师的平均评分（只计算已打分的作品）"""
        conn = self.get_connection()
        c = conn.cursor()
        c.execute('''
            SELECT AVG(i.rating) FROM images i
            JOIN meta_pixiv m ON i.id = m.image_id
            WHERE m.artist_id = ? AND i.rating > 0
        ''', (artist_id,))
        result = c.fetchone()
        return result[0] if result and result[0] else 0.0

    def get_artist_rating_stats(self, artist_id: str) -> Dict[str, Any]:
        """获取画师的评分统计信息"""
        conn = self.get_connection()
        c = conn.cursor()
        c.execute('''
            SELECT 
                COUNT(*) as total_works,
                COUNT(CASE WHEN i.rating > 0 THEN 1 END) as rated_works,
                AVG(CASE WHEN i.rating > 0 THEN i.rating END) as avg_rating,
                MIN(CASE WHEN i.rating > 0 THEN i.rating END) as min_rating,
                MAX(CASE WHEN i.rating > 0 THEN i.rating END) as max_rating
            FROM images i
            JOIN meta_pixiv m ON i.id = m.image_id
            WHERE m.artist_id = ?
        ''', (artist_id,))
        result = c.fetchone()
        if result:
            return {
                'total_works': result[0] or 0,
                'rated_works': result[1] or 0,
                'avg_rating': result[2] or 0.0,
                'min_rating': result[3] or 0,
                'max_rating': result[4] or 0
            }
        return {
            'total_works': 0,
            'rated_works': 0,
            'avg_rating': 0.0,
            'min_rating': 0,
            'max_rating': 0
        }

# 全局单例
db = DatabaseManager()

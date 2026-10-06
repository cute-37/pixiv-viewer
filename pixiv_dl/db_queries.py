"""面向前端的查询：失败分组、作品库、最近更新、运行历史、画师备注、下载计划。

作为 Database 的混入类使用（依赖 self.conn / self.tx()），只读为主；写操作都是「只改状态/只加行」。
"""
import html
import json
import re
from datetime import datetime, timedelta

_TAG_RE = re.compile(r"<[^>]+>")

_WORK_SELECT = (
    "SELECT m.illust_id, m.title, m.author_id, a.author_name, m.page_count, m.illust_type, m.is_r18, m.ai_type, "
    "m.total_bookmarks, m.create_date, m.width, m.height, COUNT(i.task_key) AS tasks, SUM(i.status = 1) AS done, "
    "MAX(CASE WHEN i.page_index = 0 THEN i.task_key END) AS first_key, "
    "MAX(CASE WHEN i.page_index = 0 THEN i.status END) AS first_status, MAX(i.media_type) AS media_type, "
    "MAX(i.download_date) AS dl "
    "FROM illust_metadata m JOIN illusts i ON i.illust_id = m.illust_id AND i.media_type != 'novel' "
    "LEFT JOIN artists a ON a.author_id = m.author_id ")

_SORTS = {
    'recent': "dl DESC, m.illust_id DESC",
    'created': "m.create_date DESC, m.illust_id DESC",
    'bookmarks': "COALESCE(m.total_bookmarks, 0) DESC, m.illust_id DESC",
    'id': "m.illust_id DESC",
}


def _rows(cur):
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


class QueryMixin:
    # ------------------------------------------------------------------ 失败任务
    def failure_groups(self, max_attempts=3):
        """失败任务按原因分组：[{kind, count, auto_retry, sample}]。旧记录（没有原因）归为 unknown。"""
        cur = self.conn.execute(
            "SELECT COALESCE(error_kind, 'unknown') AS kind, COUNT(*) AS count, "
            "SUM(COALESCE(attempts, 0) < ?) AS auto_retry, MAX(last_error) AS sample "
            "FROM illusts WHERE status = -1 GROUP BY 1 ORDER BY count DESC", (max_attempts,))
        return _rows(cur)

    def _failed_where(self, keys=None, kinds=None, author_id=None, status=-1):
        where, params = ["status = ?"], [status]
        if keys:
            where.append(f"task_key IN ({','.join('?' * len(keys))})")
            params += list(keys)
        if kinds:
            real = [k for k in kinds if k != 'unknown']
            parts = []
            if real:
                parts.append(f"error_kind IN ({','.join('?' * len(real))})")
                params += real
            if 'unknown' in kinds:
                parts.append("error_kind IS NULL")
            where.append("(" + " OR ".join(parts) + ")")
        if author_id:
            where.append("illust_id IN (SELECT illust_id FROM illust_metadata WHERE author_id = ?)")
            params.append(author_id)
        return " AND ".join(where), params

    def count_failed(self, kinds=None, author_id=None):
        w, p = self._failed_where(kinds=kinds, author_id=author_id)
        return self.conn.execute(f"SELECT COUNT(*) FROM illusts WHERE {w}", p).fetchone()[0]

    def retry_tasks(self, keys=None, kinds=None, author_id=None):
        """失败 → 待下载（重试次数清零）。返回条数。"""
        w, p = self._failed_where(keys, kinds, author_id)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self.tx() as c:
            return c.execute(f"UPDATE illusts SET status = 0, attempts = 0, updated_at = ? WHERE {w}", [now] + p).rowcount

    def ignore_tasks(self, keys=None, kinds=None, restore=False):
        """失败 ↔ 已忽略。已忽略的任务不会被自动重试，也不再计入失败。"""
        src, dst = (-2, -1) if restore else (-1, -2)
        w, p = self._failed_where(keys, kinds, status=src)
        with self.tx() as c:
            return c.execute(f"UPDATE illusts SET status = ? WHERE {w}", [dst] + p).rowcount

    # ------------------------------------------------------------------ 作品库
    def _work_filters(self, q=None, kind=None, r18=None, ai=None, author_id=None):
        where, params = ["1=1"], []
        q = (q or '').strip()
        if q:
            like = f"%{q}%"
            cond = "(m.title LIKE ? OR m.tags LIKE ? OR m.tags_translated LIKE ? OR a.author_name LIKE ?"
            params += [like, like, like, like]
            if q.isdigit():
                cond += " OR m.illust_id = ? OR m.author_id = ?"
                params += [int(q), int(q)]
            where.append(cond + ")")
        if kind == 'manga':
            where.append("m.illust_type = 1")
        elif kind == 'ugoira':
            where.append("m.illust_type = 2")
        elif kind == 'illust':
            where.append("COALESCE(m.illust_type, 0) = 0")
        elif kind == 'multi':
            where.append("m.page_count > 1")
        if r18 == 'hide':
            where.append("COALESCE(m.is_r18, 0) = 0")
        elif r18 == 'only':
            where.append("m.is_r18 = 1")
        if ai == 'hide':
            where.append("COALESCE(m.ai_type, 1) != 2")
        elif ai == 'only':
            where.append("m.ai_type = 2")
        if author_id:
            where.append("m.author_id = ?")
            params.append(int(author_id))
        return " AND ".join(where), params

    def library(self, q=None, kind=None, r18=None, ai=None, author_id=None, sort='recent', limit=36, offset=0):
        """已下载作品的全局浏览/搜索，返回 (列表, 总数)。"""
        w, p = self._work_filters(q, kind, r18, ai, author_id)
        order = _SORTS.get(sort, _SORTS['recent'])
        total = self.conn.execute(
            "SELECT COUNT(*) FROM (SELECT m.illust_id FROM illust_metadata m "
            "JOIN illusts i ON i.illust_id = m.illust_id AND i.media_type != 'novel' "
            f"LEFT JOIN artists a ON a.author_id = m.author_id WHERE {w} "
            "GROUP BY m.illust_id HAVING SUM(i.status = 1) > 0)", p).fetchone()[0]
        cur = self.conn.execute(
            f"{_WORK_SELECT} WHERE {w} GROUP BY m.illust_id HAVING SUM(i.status = 1) > 0 "
            f"ORDER BY {order} LIMIT ? OFFSET ?", p + [int(limit), int(offset)])
        return _rows(cur), total

    def works_by_ids(self, ids):
        """按给定顺序返回作品（只含已下载的）。"""
        ids = list(ids)
        if not ids:
            return []
        cur = self.conn.execute(
            f"{_WORK_SELECT} WHERE m.illust_id IN ({','.join('?' * len(ids))}) "
            "GROUP BY m.illust_id HAVING SUM(i.status = 1) > 0", ids)
        by_id = {r['illust_id']: r for r in _rows(cur)}
        return [by_id[i] for i in ids if i in by_id]

    def recent_updates(self, limit=60):
        """最近入库的作品（按下载完成时间倒序）。"""
        cur = self.conn.execute(
            "SELECT illust_id, MAX(download_date) AS d FROM (SELECT illust_id, download_date FROM illusts "
            "WHERE download_date IS NOT NULL AND status = 1 AND media_type != 'novel' "
            "ORDER BY download_date DESC LIMIT 1500) GROUP BY illust_id ORDER BY d DESC LIMIT ?", (int(limit),))
        return self.works_by_ids([r[0] for r in cur.fetchall()])

    def work_detail(self, illust_id):
        cur = self.conn.execute(
            "SELECT m.illust_id, m.title, m.author_id, a.author_name, m.page_count, m.illust_type, m.is_r18, "
            "m.ai_type, m.total_bookmarks, m.total_view, m.create_date, m.tags, m.tags_translated, m.caption, "
            "m.width, m.height, m.series_title FROM illust_metadata m LEFT JOIN artists a ON a.author_id = m.author_id "
            "WHERE m.illust_id = ?", (illust_id,))
        rows = _rows(cur)
        if not rows:
            return None
        w = rows[0]
        for key in ('tags', 'tags_translated'):
            try:
                w[key] = json.loads(w[key]) if w.get(key) else []
            except ValueError:
                w[key] = []
        caption = w.get('caption') or ''
        caption = html.unescape(_TAG_RE.sub("", caption.replace("<br />", "\n").replace("<br>", "\n")))
        w['caption'] = caption.strip()[:2000]
        cur = self.conn.execute(
            "SELECT task_key, page_index, media_type, status, file_size FROM illusts "
            "WHERE illust_id = ? AND media_type != 'novel' ORDER BY page_index", (illust_id,))
        w['pages'] = _rows(cur)
        return w

    # ------------------------------------------------------------------ 画师备注 / 置顶
    def set_label(self, author_id, pinned=None, note=None):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self.tx() as c:
            c.execute("INSERT OR IGNORE INTO artist_labels (author_id, pinned, note, updated_at) VALUES (?, 0, '', ?)",
                      (author_id, now))
            if pinned is not None:
                c.execute("UPDATE artist_labels SET pinned = ?, updated_at = ? WHERE author_id = ?",
                          (1 if pinned else 0, now, author_id))
            if note is not None:
                c.execute("UPDATE artist_labels SET note = ?, updated_at = ? WHERE author_id = ?",
                          (note[:500], now, author_id))

    def get_label(self, author_id):
        row = self.conn.execute("SELECT pinned, note FROM artist_labels WHERE author_id = ?", (author_id,)).fetchone()
        return {'pinned': row[0] or 0, 'note': row[1] or ''} if row else {'pinned': 0, 'note': ''}

    # ------------------------------------------------------------------ 运行历史
    def record_run(self, snap):
        """保存一次任务的结果（snap 是 JobState.snapshot() 的结果）。"""
        summary = {'result': snap.get('result'), 'detail': snap.get('detail'), 'message': snap.get('message')}
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO runs (kind, params, status, started, finished, total, success, failed, skipped, bytes, "
                "error, summary) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (snap['kind'], json.dumps(snap.get('params') or {}, ensure_ascii=False), snap['status'],
                 snap.get('started'), snap.get('finished'), snap.get('total', 0), snap.get('success', 0),
                 snap.get('failed', 0), snap.get('skipped', 0), snap.get('bytes', 0), snap.get('error'),
                 json.dumps(summary, ensure_ascii=False, default=str)))
            c.execute("DELETE FROM runs WHERE id <= (SELECT MAX(id) FROM runs) - 200")
            return cur.lastrowid

    def recent_runs(self, limit=10):
        cur = self.conn.execute(
            "SELECT id, kind, status, started, finished, total, success, failed, skipped, bytes, error "
            "FROM runs ORDER BY id DESC LIMIT ?", (int(limit),))
        return _rows(cur)

    def get_run(self, run_id):
        cur = self.conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,))
        rows = _rows(cur)
        if not rows:
            return None
        r = rows[0]
        for key in ('params', 'summary'):
            try:
                r[key] = json.loads(r[key]) if r.get(key) else {}
            except ValueError:
                r[key] = {}
        return r

    def last_run(self, kinds=None):
        q = "SELECT id FROM runs"
        params = []
        if kinds:
            q += f" WHERE kind IN ({','.join('?' * len(kinds))})"
            params = list(kinds)
        row = self.conn.execute(q + " ORDER BY id DESC LIMIT 1", params).fetchone()
        return self.get_run(row[0]) if row else None

    # ------------------------------------------------------------------ 下载计划（给「检查更新并下载」的预览）
    def plan_numbers(self, max_attempts=3):
        c = self.conn
        pending_by_type = dict(c.execute(
            "SELECT media_type, COUNT(*) FROM illusts WHERE status IN (0, -1) AND COALESCE(attempts,0) < ? "
            "GROUP BY media_type", (max_attempts,)).fetchall())
        avg = dict(c.execute(
            "SELECT media_type, AVG(file_size) FROM illusts WHERE status = 1 AND file_size > 0 GROUP BY media_type").fetchall())
        est = sum((avg.get(mt) or 0) * n for mt, n in pending_by_type.items())
        exhausted = c.execute(
            "SELECT COUNT(*) FROM illusts WHERE status = -1 AND COALESCE(attempts,0) >= ?", (max_attempts,)).fetchone()[0]
        last_sync = c.execute("SELECT MAX(last_sync_time) FROM artists").fetchone()[0]
        artists = c.execute("SELECT COUNT(*) FROM artists WHERE COALESCE(is_deleted,0) = 0").fetchone()[0]
        return {
            'pending': sum(pending_by_type.values()), 'pending_by_type': pending_by_type,
            'estimated_bytes': int(est), 'exhausted': exhausted, 'last_sync': last_sync, 'artists': artists,
        }

    def recent_counts_by_artist(self, days=7):
        """最近 N 天新入库的文件数（按画师），画师列表里的「+N」。"""
        since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        cur = self.conn.execute(
            "SELECT m.author_id, COUNT(*) FROM illusts i JOIN illust_metadata m ON m.illust_id = i.illust_id "
            "WHERE i.download_date >= ? AND i.status = 1 GROUP BY m.author_id", (since,))
        return dict(cur.fetchall())

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

    # ------------------------------------------------------------------ 待下载：筛选、汇总、跳过
    # 用户看到的四种作品类型：插画 illust、漫画 manga、动图 ugoira、小说 novel
    WTYPE_SQL = ("CASE WHEN i.media_type = 'novel' THEN 'novel' "
                 "WHEN i.media_type = 'ugoira' OR m.illust_type = 2 THEN 'ugoira' "
                 "WHEN m.illust_type = 1 THEN 'manga' ELSE 'illust' END")

    def pending_where(self, filters=None, max_attempts=None, status=(0, -1)):
        """把筛选条件变成 SQL 的 WHERE（表别名 i = illusts，m = illust_metadata）。返回 (where, params)。"""
        f = filters or {}
        where = [f"i.status IN ({','.join(str(int(x)) for x in status)})"]
        params = []
        if max_attempts is not None and -1 in status:
            where.append("(i.attempts IS NULL OR i.attempts < ?)")
            params.append(max_attempts)
        for key, op in (('author_ids', 'IN'), ('exclude_author_ids', 'NOT IN')):
            ids = [int(x) for x in (f.get(key) or [])]
            if ids:
                where.append(f"m.author_id {op} ({','.join('?' * len(ids))})")
                params += ids
        types = [t for t in (f.get('types') or []) if t in ('illust', 'manga', 'ugoira', 'novel')]
        if types:
            where.append(f"{self.WTYPE_SQL} IN ({','.join('?' * len(types))})")
            params += types
        if f.get('date_from'):
            where.append("substr(COALESCE(m.create_date,''), 1, 10) >= ?")
            params.append(str(f['date_from'])[:10])
        if f.get('date_to'):
            where.append("substr(COALESCE(m.create_date,''), 1, 10) <= ?")
            params.append(str(f['date_to'])[:10])
        if f.get('exclude_r18'):
            where.append("COALESCE(m.x_restrict, 0) = 0")
        if f.get('origin') in ('new', 'old'):
            where.append("COALESCE(i.origin, 'new') = ?")
            params.append(f['origin'])
        keys = [str(k) for k in (f.get('keys') or [])]
        if keys:
            where.append(f"i.task_key IN ({','.join('?' * len(keys))})")
            params += keys
        return " AND ".join(where), params

    def pending_summary(self, filters=None, max_attempts=None, status=(0, -1)):
        """待下载的东西按画师汇总，给“先看再下”用。

        返回 {"artists": [{author_id, name, files, works, illust, manga, ugoira, novel, old, r18, est_bytes,
        newest, is_new_artist}], "totals": {...}}。大小是估算的：用已下载文件里各类型的平均大小。"""
        where, params = self.pending_where(filters, max_attempts, status)
        avg = dict(self.conn.execute(
            "SELECT media_type, AVG(file_size) FROM illusts WHERE status = 1 AND file_size > 0 GROUP BY media_type").fetchall())
        default = avg.get('image') or 2 * 1024 * 1024
        cur = self.conn.execute(
            f"SELECT m.author_id, {self.WTYPE_SQL} AS wtype, i.media_type, COUNT(*) AS files, "
            "COUNT(DISTINCT i.illust_id) AS works, SUM(COALESCE(i.origin, 'new') = 'old') AS old, "
            "SUM(COALESCE(m.x_restrict, 0) > 0) AS r18, MAX(substr(COALESCE(m.create_date,''), 1, 10)) AS newest "
            "FROM illusts i LEFT JOIN illust_metadata m ON i.illust_id = m.illust_id "
            f"WHERE {where} GROUP BY m.author_id, wtype, i.media_type", params)
        artists = {}
        for aid, wtype, media, files, works, old, r18, newest in cur.fetchall():
            a = artists.setdefault(aid, {'author_id': aid, 'files': 0, 'works': 0, 'illust': 0, 'manga': 0, 'ugoira': 0,
                                         'novel': 0, 'old': 0, 'r18': 0, 'est_bytes': 0, 'newest': ''})
            a['files'] += files
            a['works'] += works
            a[wtype] += files
            a['old'] += old or 0
            a['r18'] += r18 or 0
            a['est_bytes'] += int(files * (avg.get(media) or (2000 if media == 'novel' else default)))
            a['newest'] = max(a['newest'], newest or '')
        info = {r[0]: r for r in self.conn.execute(
            "SELECT a.author_id, a.author_name, a.is_followed, "
            "(SELECT COUNT(*) FROM illusts i2 JOIN illust_metadata m2 ON m2.illust_id = i2.illust_id "
            " WHERE m2.author_id = a.author_id AND i2.status = 1) FROM artists a")} if artists else {}
        for aid, a in artists.items():
            row = info.get(aid)
            a['name'] = (row[1] if row else None) or f"画师 {aid}"
            a['is_new_artist'] = 0 if (row and row[3]) else 1      # 这位画师还没有任何已下载的文件
        items = sorted(artists.values(), key=lambda a: -a['files'])
        keys = ('files', 'works', 'illust', 'manga', 'ugoira', 'novel', 'old', 'r18', 'est_bytes')
        totals = {k: sum(a[k] for a in items) for k in keys}
        totals['artists'] = len(items)
        return {'artists': items, 'totals': totals}

    def skip_tasks(self, filters=None, restore=False):
        """待下载 ↔ 不下载。只改状态，不删任何记录；restore=True 把“不下载”的恢复成待下载。返回条数。"""
        src, dst = ((-3,), 0) if restore else ((0, -1), -3)
        where, params = self.pending_where(filters, None, status=src)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self.tx() as c:
            return c.execute(
                "UPDATE illusts SET status = ?, attempts = 0, updated_at = ? WHERE task_key IN ("
                "SELECT i.task_key FROM illusts i LEFT JOIN illust_metadata m ON i.illust_id = m.illust_id "
                f"WHERE {where})", [dst, now] + params).rowcount

    # ------------------------------------------------------------------ 检查失败的画师
    def sync_failures(self):
        """上次检查没成功的画师（不含已经标记“不再检查”的）。[{author_id, name, kind, error, time, count}]"""
        cur = self.conn.execute(
            "SELECT author_id, author_name AS name, COALESCE(sync_error_kind, 'other') AS kind, sync_error AS error, "
            "sync_error_time AS time, COALESCE(sync_fail_count, 0) AS count, COALESCE(is_deleted, 0) AS gone, "
            "last_sync_time FROM artists WHERE sync_status = 'failed' AND COALESCE(sync_skip, 0) = 0 "
            "ORDER BY sync_error_time DESC")
        return _rows(cur)

    def sync_skipped(self):
        cur = self.conn.execute(
            "SELECT author_id, author_name AS name, sync_error_kind AS kind, sync_error AS error, "
            "COALESCE(is_deleted, 0) AS gone FROM artists WHERE COALESCE(sync_skip, 0) = 1 ORDER BY author_name")
        return _rows(cur)

    def set_sync_skip(self, author_ids, skip=True):
        ids = [int(a) for a in author_ids or []]
        if not ids:
            return 0
        with self.tx() as c:
            return c.execute(f"UPDATE artists SET sync_skip = ? WHERE author_id IN ({','.join('?' * len(ids))})",
                             [1 if skip else 0] + ids).rowcount

    def artist_sync_states(self):
        """{author_id: {name, last, status, kind, skip, gone, error_time, followed}}，检查前决定查谁、先查谁用。"""
        out = {}
        for r in self.conn.execute(
                "SELECT author_id, author_name, last_sync_time, sync_status, sync_error_kind, COALESCE(sync_skip,0), "
                "COALESCE(is_deleted,0), sync_error_time, COALESCE(is_followed,0) FROM artists"):
            out[r[0]] = {'name': r[1] or '', 'last': r[2] or '', 'status': r[3], 'kind': r[4], 'skip': r[5],
                         'gone': r[6], 'error_time': r[7] or '', 'followed': r[8]}
        return out

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

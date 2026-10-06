"""数据库维护：备份（原子、不会留下残缺文件）、备份列表/删除/自动备份与清理、完整性检查、优化、压缩、基本信息。

备份文件放在数据库同目录的 backup/ 下，命名 `<库名>_<类型>_<时间>.db`：
  pre_v2 迁移前自动备份 · auto 定期自动备份 · manual 手动备份 · pre_vacuum 压缩前备份
只有 auto 类型的备份会被自动清理（超过保留数量），其余都不会被程序自动删除。
"""
import logging
import os
import re
import shutil
import sqlite3
import time
from datetime import datetime

logger = logging.getLogger("PixivDownloader")

KINDS = ('pre_v2', 'auto', 'manual', 'pre_vacuum')
_NAME_RE = re.compile(r"^[^\\/:*?\"<>|]+\.db(-journal|\.partial)?$")


def backup_dir(db_path):
    return os.path.join(os.path.dirname(os.path.abspath(db_path)), 'backup')


def create_backup(db_path, kind='manual', conn=None):
    """用 SQLite 在线备份 API 生成备份，先写 .partial 再重命名，失败不留残缺文件。返回最终路径。"""
    if kind not in KINDS:
        raise ValueError(f"未知备份类型: {kind}")
    bdir = backup_dir(db_path)
    os.makedirs(bdir, exist_ok=True)
    base = os.path.splitext(os.path.basename(db_path))[0]
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    final, n = os.path.join(bdir, f"{base}_{kind}_{stamp}.db"), 1
    while os.path.exists(final):
        n += 1
        final = os.path.join(bdir, f"{base}_{kind}_{stamp}_{n}.db")
    tmp = final + '.partial'
    opened = conn is None
    src = conn or sqlite3.connect(db_path, timeout=30)
    try:
        dest = sqlite3.connect(tmp)
        try:
            src.backup(dest)
        finally:
            dest.close()
        os.replace(tmp, final)
        return final
    except Exception:
        for p in (tmp, tmp + '-journal'):
            try:
                os.remove(p)
            except OSError:
                pass
        raise
    finally:
        if opened:
            src.close()


def _kind_of(name):
    for k in KINDS:
        if f"_{k}_" in name:
            return k
    return 'other'


def list_backups(db_path):
    """[{name, kind, size, mtime, incomplete}]，新的在前。残缺备份（被中断留下的 .partial / 带 -journal 的 .db）标记 incomplete。"""
    bdir = backup_dir(db_path)
    if not os.path.isdir(bdir):
        return []
    names = set(os.listdir(bdir))
    out = []
    for name in names:
        if name.endswith('-journal'):
            continue
        if not (name.endswith('.db') or name.endswith('.partial')):
            continue
        path = os.path.join(bdir, name)
        try:
            st = os.stat(path)
        except OSError:
            continue
        incomplete = name.endswith('.partial') or (name + '-journal') in names
        out.append({'name': name, 'kind': _kind_of(name), 'size': st.st_size, 'mtime': st.st_mtime, 'incomplete': incomplete})
    return sorted(out, key=lambda b: b['mtime'], reverse=True)


def delete_backup(db_path, name):
    """删除一个备份（连同它的 -journal）。只允许删除 backup/ 目录里符合命名的文件。返回是否删除了东西。"""
    if os.path.basename(name) != name or not _NAME_RE.match(name):
        raise ValueError("不允许删除这个文件")
    bdir = backup_dir(db_path)
    removed = False
    for n in (name, name + '-journal'):
        p = os.path.join(bdir, n)
        if os.path.isfile(p) and os.path.dirname(os.path.abspath(p)) == os.path.abspath(bdir):
            os.remove(p)
            removed = True
    return removed


def auto_backup_if_due(db_path, days, keep, conn=None):
    """距离最近一份完整备份超过 days 天（days<=0 表示关闭）就做一份 auto 备份，并清理多余的 auto 备份。返回新备份路径或 None。"""
    if not days or days <= 0 or not os.path.exists(db_path):
        return None
    good = [b for b in list_backups(db_path) if not b['incomplete']]
    if good and time.time() - good[0]['mtime'] < days * 86400:
        return None
    path = create_backup(db_path, 'auto', conn=conn)
    prune_auto(db_path, keep)
    return path


def prune_auto(db_path, keep):
    autos = [b for b in list_backups(db_path) if b['kind'] == 'auto' and not b['incomplete']]
    for b in autos[max(1, int(keep or 1)):]:
        try:
            delete_backup(db_path, b['name'])
        except (OSError, ValueError):
            pass


def integrity_check(db_path):
    """PRAGMA integrity_check，返回 (是否正常, 说明)。"""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
        try:
            rows = [r[0] for r in conn.execute("PRAGMA integrity_check").fetchall()]
        finally:
            conn.close()
    except sqlite3.DatabaseError as e:
        return False, f"无法读取数据库：{e}"
    ok = rows == ['ok']
    return ok, ('数据库完整，没有发现问题' if ok else '发现问题：' + '；'.join(rows[:5]))


def optimize(db_path):
    """更新查询统计信息（安全、很快，不改动任何数据）。"""
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        conn.execute("PRAGMA optimize")
        conn.execute("ANALYZE")
        conn.commit()
    finally:
        conn.close()


def vacuum(db_path):
    """压缩数据库文件（回收已删除数据占用的空间）。需要额外约一个库大小的空闲磁盘空间。返回 (压缩前, 压缩后) 字节数。"""
    before = os.path.getsize(db_path)
    free = shutil.disk_usage(os.path.dirname(os.path.abspath(db_path))).free
    if free < before * 1.3:
        raise RuntimeError(f"磁盘空间不足：压缩需要约 {before * 1.3 / 1e6:.0f} MB 空闲空间，当前只有 {free / 1e6:.0f} MB")
    conn = sqlite3.connect(db_path, timeout=60, isolation_level=None)
    try:
        conn.execute("VACUUM")
    finally:
        conn.close()
    return before, os.path.getsize(db_path)


def info(db_path):
    """基本信息：路径、大小、日志模式、页数/空闲页、SQLite 版本。"""
    exists = os.path.exists(db_path)
    data = {'path': os.path.abspath(db_path), 'backup_dir': backup_dir(db_path), 'exists': exists, 'size': os.path.getsize(db_path) if exists else 0,
            'sqlite_version': sqlite3.sqlite_version}
    if exists:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
        try:
            data['journal_mode'] = conn.execute("PRAGMA journal_mode").fetchone()[0]
            data['page_count'] = conn.execute("PRAGMA page_count").fetchone()[0]
            data['free_pages'] = conn.execute("PRAGMA freelist_count").fetchone()[0]
            data['page_size'] = conn.execute("PRAGMA page_size").fetchone()[0]
        finally:
            conn.close()
        data['reclaimable'] = data['free_pages'] * data['page_size']
    return data


def set_journal_mode(db_path, mode):
    """切换日志模式（delete / wal），返回实际生效的模式。有其他连接占用时 SQLite 可能暂时无法切换。"""
    if mode not in ('delete', 'wal'):
        raise ValueError("日志模式只能是 delete 或 wal")
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        return str(conn.execute(f"PRAGMA journal_mode = {mode}").fetchone()[0]).lower()
    finally:
        conn.close()

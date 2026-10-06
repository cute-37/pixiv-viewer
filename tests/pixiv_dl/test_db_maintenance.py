"""数据库设置：备份（原子/列表/删除/自动/清理）、完整性检查、优化、压缩、日志模式、Web 接口。"""
import hashlib
import http.client
import json
import os
import sqlite3
import threading
import time

import pytest

from pixiv_dl import dbtools
from pixiv_dl.database import Database
from fakes import FakeAPI, make_illust, make_processor
from pixiv_dl.web import server as web


def make_db(cfg, rows=20):
    db = Database(cfg.DB_PATH)
    db.upsert_artist(1, 'A')
    for i in range(rows):
        db.save_illust({'task_key': f'{i}_0', 'illust_id': i, 'page_index': 0, 'author_id': 1, 'url': 'u', 'title': 't' * 50})
    return db


def sha(path):
    return hashlib.sha256(open(path, 'rb').read()).hexdigest()


# ------------------------------------------------------------------ 备份
def test_backup_is_a_faithful_atomic_copy(cfg):
    db = make_db(cfg)
    path = dbtools.create_backup(cfg.DB_PATH, 'manual', conn=db.conn)
    assert os.path.basename(path).startswith('t_manual_') and path.endswith('.db')
    assert not [f for f in os.listdir(os.path.dirname(path)) if f.endswith('.partial')]
    copy = sqlite3.connect(path)
    assert copy.execute("SELECT COUNT(*) FROM illusts").fetchone()[0] == 20
    assert copy.execute("PRAGMA integrity_check").fetchone()[0] == 'ok'
    copy.close()
    again = dbtools.create_backup(cfg.DB_PATH, 'manual')                       # 同一秒内再备份，不会覆盖
    assert again != path and os.path.exists(path) and os.path.exists(again)


def test_failed_backup_leaves_no_partial_file(cfg):
    make_db(cfg)

    class Boom:
        def backup(self, target):
            target.execute("CREATE TABLE x(a)")
            raise sqlite3.OperationalError("disk I/O error")
    with pytest.raises(sqlite3.OperationalError):
        dbtools.create_backup(cfg.DB_PATH, 'manual', conn=Boom())
    assert os.listdir(dbtools.backup_dir(cfg.DB_PATH)) == []                   # 回归：以前被中断会留下残缺的 .db


def test_list_backups_kinds_and_incomplete(cfg):
    make_db(cfg)
    bdir = dbtools.backup_dir(cfg.DB_PATH)
    os.makedirs(bdir, exist_ok=True)
    for name, age in (('t_pre_v2_20260101_000000.db', 500), ('t_auto_20260102_000000.db', 300), ('t_manual_20260103_000000.db', 100),
                      ('t_pre_v2_20260104_000000.db', 50), ('t_pre_v2_20260104_000000.db-journal', 50), ('t_manual_x.db.partial', 10)):
        p = os.path.join(bdir, name)
        open(p, 'wb').write(b'x' * 100)
        os.utime(p, (time.time() - age, time.time() - age))
    open(os.path.join(bdir, 'notes.txt'), 'w').write('ignore me')
    got = {b['name']: b for b in dbtools.list_backups(cfg.DB_PATH)}
    assert set(got) == {'t_pre_v2_20260101_000000.db', 't_auto_20260102_000000.db', 't_manual_20260103_000000.db',
                        't_pre_v2_20260104_000000.db', 't_manual_x.db.partial'}
    assert got['t_auto_20260102_000000.db']['kind'] == 'auto' and not got['t_auto_20260102_000000.db']['incomplete']
    assert got['t_pre_v2_20260104_000000.db']['incomplete']                    # 带 -journal 的 = 被中断的备份
    assert got['t_manual_x.db.partial']['incomplete']
    assert [b['name'] for b in dbtools.list_backups(cfg.DB_PATH)][0] == 't_manual_x.db.partial'   # 新的在前


def test_delete_backup_is_restricted(cfg):
    make_db(cfg)
    bdir = dbtools.backup_dir(cfg.DB_PATH)
    os.makedirs(bdir)
    open(os.path.join(bdir, 'a_manual_1.db'), 'wb').write(b'x')
    open(os.path.join(bdir, 'a_manual_1.db-journal'), 'wb').write(b'x')
    outside = os.path.join(os.path.dirname(cfg.DB_PATH), 'outside.db')
    open(outside, 'wb').write(b'x')
    for bad in ('../outside.db', '..\\outside.db', 'sub/a.db', '/etc/passwd', 'a_manual_1.txt', 'settings.json', 'C:\\x.db', ''):
        with pytest.raises(ValueError):
            dbtools.delete_backup(cfg.DB_PATH, bad)
    assert os.path.exists(outside) and os.path.exists(cfg.DB_PATH)
    assert dbtools.delete_backup(cfg.DB_PATH, 'a_manual_1.db') is True          # 连同 -journal 一起删
    assert os.listdir(bdir) == []
    assert dbtools.delete_backup(cfg.DB_PATH, 'a_manual_1.db') is False


def test_auto_backup_schedule_and_pruning(cfg):
    db = make_db(cfg)
    assert dbtools.auto_backup_if_due(cfg.DB_PATH, 0, 5) is None               # 0 = 关闭
    first = dbtools.auto_backup_if_due(cfg.DB_PATH, 7, 2, conn=db.conn)
    assert first and '_auto_' in first                                          # 一份备份都没有 → 立刻备份
    assert dbtools.auto_backup_if_due(cfg.DB_PATH, 7, 2) is None               # 刚备份过
    old = time.time() - 8 * 86400
    os.utime(first, (old, old))
    second = dbtools.auto_backup_if_due(cfg.DB_PATH, 7, 2)
    assert second and second != first
    bdir = dbtools.backup_dir(cfg.DB_PATH)
    # 手动 / 迁移前备份永远不会被自动清理，只清理多余的 auto 备份
    manual = dbtools.create_backup(cfg.DB_PATH, 'manual')
    pre = dbtools.create_backup(cfg.DB_PATH, 'pre_v2')
    for i in range(4):
        p = dbtools.create_backup(cfg.DB_PATH, 'auto')
        os.utime(p, (time.time() - 100 * (i + 1), time.time() - 100 * (i + 1)))
    dbtools.prune_auto(cfg.DB_PATH, 2)
    kinds = [b['kind'] for b in dbtools.list_backups(cfg.DB_PATH)]
    assert kinds.count('auto') == 2 and manual in [os.path.join(bdir, b['name']) for b in dbtools.list_backups(cfg.DB_PATH)]
    assert pre in [os.path.join(bdir, b['name']) for b in dbtools.list_backups(cfg.DB_PATH)]


def test_incomplete_backup_does_not_count_as_a_backup(cfg):
    make_db(cfg)
    bdir = dbtools.backup_dir(cfg.DB_PATH)
    os.makedirs(bdir)
    open(os.path.join(bdir, 't_manual_1.db.partial'), 'wb').write(b'x')
    assert dbtools.auto_backup_if_due(cfg.DB_PATH, 7, 5)                        # 只有残缺备份 → 仍要做一份真备份


# ------------------------------------------------------------------ 检查 / 优化 / 压缩 / 信息
def test_integrity_optimize_info(cfg):
    make_db(cfg)
    assert dbtools.integrity_check(cfg.DB_PATH) == (True, '数据库完整，没有发现问题')
    dbtools.optimize(cfg.DB_PATH)
    info = dbtools.info(cfg.DB_PATH)
    assert info['exists'] and info['size'] > 0 and info['journal_mode'] == 'delete' and info['page_count'] > 0
    bad = os.path.join(os.path.dirname(cfg.DB_PATH), 'bad.db')
    open(bad, 'wb').write(b'this is not a database' * 100)
    ok, msg = dbtools.integrity_check(bad)
    assert not ok and '无法读取' in msg


def test_vacuum_reclaims_space_and_checks_disk(cfg, monkeypatch):
    db = make_db(cfg, rows=3000)
    db.conn.execute("DELETE FROM illusts WHERE illust_id >= 10")
    db.conn.execute("DELETE FROM illust_metadata WHERE illust_id >= 10")
    db.conn.commit()
    assert dbtools.info(cfg.DB_PATH)['reclaimable'] > 0
    before, after = dbtools.vacuum(cfg.DB_PATH)
    assert after < before
    assert Database(cfg.DB_PATH).conn.execute("SELECT COUNT(*) FROM illusts").fetchone()[0] == 10    # 数据不变
    monkeypatch.setattr(dbtools.shutil, 'disk_usage', lambda p: type('U', (), {'free': 10})())
    with pytest.raises(RuntimeError, match='磁盘空间不足'):
        dbtools.vacuum(cfg.DB_PATH)


def test_vacuum_job_backs_up_first(cfg, no_sleep):
    pro, _ = make_processor()
    make_db(cfg, rows=50)
    pro.db_vacuum()
    assert pro.job.status == 'done' and pro.job.result['size_before'] >= pro.job.result['size_after']
    assert [b['kind'] for b in dbtools.list_backups(cfg.DB_PATH)] == ['pre_vacuum']
    assert pro.job.result['backup'].startswith('t_pre_vacuum_')


# ------------------------------------------------------------------ 日志模式
def test_default_config_does_not_touch_existing_database(cfg):
    make_db(cfg)
    before = sha(cfg.DB_PATH)
    from pixiv_dl.database import _SCHEMA_DONE
    _SCHEMA_DONE.clear()
    Database(cfg.DB_PATH)                                                       # 重新打开（默认 delete 模式）
    assert sha(cfg.DB_PATH) == before                                           # 文件一个字节都没变


def test_wal_mode_switch_and_back(cfg, monkeypatch):
    make_db(cfg)
    assert dbtools.set_journal_mode(cfg.DB_PATH, 'wal') == 'wal'
    monkeypatch.setattr(cfg, 'DB_JOURNAL', 'delete')
    db2 = Database(cfg.DB_PATH)                                                 # 新连接按配置切回 delete
    assert db2.conn.execute("PRAGMA journal_mode").fetchone()[0] == 'delete'
    with pytest.raises(ValueError):
        dbtools.set_journal_mode(cfg.DB_PATH, 'memory')


# ------------------------------------------------------------------ 自动备份挂在同步/下载上
def test_jobs_trigger_auto_backup_only_when_due(cfg, no_sleep, monkeypatch):
    monkeypatch.setattr(cfg, 'DB_AUTO_BACKUP_DAYS', 1)
    api = FakeAPI()
    api.following['public'] = [(10, 'Alice')]
    api.illusts[(10, 'illust')] = [make_illust(105)]
    pro, _ = make_processor(api)
    pro.sync()
    assert [b['kind'] for b in dbtools.list_backups(cfg.DB_PATH)] == ['auto']
    assert any('自动备份' in l['msg'] for l in pro.job.snapshot()['logs'])
    pro.sync()
    assert len(dbtools.list_backups(cfg.DB_PATH)) == 1                          # 刚备份过，不重复
    pro.verify_storage()                                                        # 核查不触发自动备份
    assert len(dbtools.list_backups(cfg.DB_PATH)) == 1


def test_auto_backup_failure_does_not_fail_the_job(cfg, no_sleep, monkeypatch):
    monkeypatch.setattr(cfg, 'DB_AUTO_BACKUP_DAYS', 1)
    monkeypatch.setattr(dbtools, 'create_backup', lambda *a, **k: (_ for _ in ()).throw(OSError('disk full')))
    api = FakeAPI()
    api.following['public'] = [(10, 'Alice')]
    pro, _ = make_processor(api)
    pro.sync()
    assert pro.job.status == 'done'
    assert any('自动备份数据库失败' in l['msg'] for l in pro.job.snapshot()['logs'])


# ------------------------------------------------------------------ Web
@pytest.fixture
def srv(cfg, no_sleep):
    pro, _ = make_processor()
    make_db(cfg)
    httpd = web.make_server('127.0.0.1', 0, pro)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]

    class C:
        def req(self, method, path, body=None):
            c = http.client.HTTPConnection('127.0.0.1', port, timeout=20)
            h = {'Host': f'127.0.0.1:{port}'}
            if method != 'GET':
                h.update({'X-Pixiv-UI': '1', 'Content-Type': 'application/json'})
            c.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
            r = c.getresponse()
            data = json.loads(r.read())
            c.close()
            return r.status, data

        def get(self, p):
            return self.req('GET', p)

        def post(self, p, b=None):
            return self.req('POST', p, b or {})
    yield C()
    httpd.shutdown()
    httpd.server_close()


def test_web_db_info_backup_delete_flow(srv, cfg):
    s, d = srv.get('/api/db/info')
    assert s == 200 and d['info']['journal_mode'] == 'delete' and d['stats']['tasks'] == 20 and d['backups'] == []
    assert d['settings'] == {'DB_AUTO_BACKUP_DAYS': 0, 'DB_BACKUP_KEEP': 5, 'DB_JOURNAL': 'delete'}
    s, b = srv.post('/api/db/backup')
    assert s == 200 and b['name'].startswith('t_manual_') and b['size'] > 0
    backups = srv.get('/api/db/info')[1]['backups']
    assert [x['name'] for x in backups] == [b['name']] and backups[0]['kind'] == 'manual'
    assert srv.post('/api/db/backup/delete', {'name': '../t.db'})[0] == 400
    assert srv.post('/api/db/backup/delete', {'name': 'nope_manual_1.db'})[0] == 404
    assert srv.post('/api/db/backup/delete', {})[0] == 400
    assert os.path.exists(cfg.DB_PATH)
    assert srv.post('/api/db/backup/delete', {'name': b['name']})[0] == 200
    assert srv.get('/api/db/info')[1]['backups'] == []


def test_web_db_check_optimize_clear(srv, cfg):
    assert srv.post('/api/db/check')[1] == {'ok': True, 'message': '数据库完整，没有发现问题'}
    assert srv.post('/api/db/optimize')[0] == 200
    db = Database.local(cfg.DB_PATH)
    db.record_run({'kind': 'sync', 'status': 'done', 'started': 1, 'finished': 2})
    db.insert_alert('1_0', 'ERROR', 'x')
    assert srv.post('/api/db/clear', {'what': 'runs'})[1]['count'] == 1
    assert srv.post('/api/db/clear', {'what': 'alerts'})[1]['count'] == 1
    assert srv.post('/api/db/clear', {'what': 'illusts'})[0] == 400              # 不能通过这个接口清空作品数据
    assert db.conn.execute("SELECT COUNT(*) FROM illusts").fetchone()[0] == 20


def test_web_db_settings_validation_and_persistence(srv, cfg):
    assert srv.post('/api/settings', {'DB_AUTO_BACKUP_DAYS': -1})[0] == 400
    assert srv.post('/api/settings', {'DB_BACKUP_KEEP': 0})[0] == 400
    assert srv.post('/api/settings', {'DB_JOURNAL': 'memory'})[0] == 400
    assert srv.post('/api/settings', {'DB_PATH': 'x.db'})[0] == 400              # 数据库位置不能从网页改
    s, r = srv.post('/api/settings', {'DB_AUTO_BACKUP_DAYS': 3, 'DB_BACKUP_KEEP': 2, 'DB_JOURNAL': 'wal'})
    assert s == 200 and r['warning'] is None
    assert (cfg.DB_AUTO_BACKUP_DAYS, cfg.DB_BACKUP_KEEP, cfg.DB_JOURNAL) == (3, 2, 'wal')
    assert dbtools.info(cfg.DB_PATH)['journal_mode'] == 'wal'
    assert json.load(open(cfg.SETTINGS_FILE, encoding='utf-8'))['current']['DB_JOURNAL'] == 'wal'
    s, r = srv.post('/api/settings', {'DB_JOURNAL': 'delete'})
    assert s == 200


def test_web_vacuum_job(srv, cfg):
    assert srv.post('/api/job', {'kind': 'db_vacuum'})[0] == 200
    end = time.time() + 20
    while time.time() < end:
        j = srv.get('/api/job')[1]
        if not j['running'] and j['kind'] == 'db_vacuum':
            break
        time.sleep(0.1)
    assert j['status'] == 'done' and 'size_after' in j['result']

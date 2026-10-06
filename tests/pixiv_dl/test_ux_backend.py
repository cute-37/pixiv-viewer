"""面向前端交互的后端能力：失败原因、结果明细、运行历史、作品库、批量操作、通知。"""
import http.client
import json
import os
import sqlite3
import threading
import time

import pytest

from pixiv_dl.database import Database
from fakes import FakeAPI, err, make_illust, make_processor
from pixiv_dl.web import server as web

STALE = 'https://old-host.invalid/x_p0.jpg'
BLOB = b'\x89PNG' + b'z' * 400


def seed(db, aid, iid, pages=1, name='Alice', **meta):
    db.upsert_artist(aid, name)
    for i in range(pages):
        db.save_illust({'task_key': f'{iid}_{i}', 'illust_id': iid, 'page_index': i, 'author_id': aid,
                        'title': meta.pop('title', f't{iid}') if i == 0 else f't{iid}', 'url': STALE,
                        'media_type': 'image', **meta})


# ------------------------------------------------------------------ 迁移兼容
def test_existing_db_gets_new_columns_tables_and_keeps_data(cfg):
    os.makedirs(os.path.dirname(cfg.DB_PATH))
    c = sqlite3.connect(cfg.DB_PATH)
    c.executescript("""
    CREATE TABLE artists (author_id INTEGER PRIMARY KEY, author_name TEXT, last_synced_id INTEGER DEFAULT 0, last_sync_time TIMESTAMP);
    CREATE TABLE illust_metadata (illust_id INTEGER PRIMARY KEY, author_id INTEGER, title TEXT);
    CREATE TABLE illusts (task_key TEXT PRIMARY KEY, illust_id INTEGER, page_index INTEGER, url TEXT, media_type TEXT DEFAULT 'image',
        status INTEGER DEFAULT 0, updated_at TEXT, attempts INTEGER DEFAULT 0, file_hash TEXT, file_size INTEGER DEFAULT 0,
        download_date TEXT, original_filename TEXT, content_type TEXT);
    INSERT INTO artists VALUES (1, 'A', 5, NULL);
    INSERT INTO illusts (task_key, illust_id, page_index, url, status, attempts) VALUES ('1_0', 1, 0, 'u', -1, 2);
    """)
    c.commit()
    c.close()
    db = Database(cfg.DB_PATH)
    cols = [r[1] for r in db.conn.execute("PRAGMA table_info(illusts)")]
    assert 'last_error' in cols and 'error_kind' in cols
    assert db.conn.execute("SELECT status, attempts, error_kind FROM illusts").fetchone() == (-1, 2, None)
    assert db.recent_runs() == [] and db.get_label(1) == {'pinned': 0, 'note': ''}
    assert os.listdir(os.path.join(os.path.dirname(cfg.DB_PATH), 'backup'))      # 改列之前备份过
    # 旧记录（没有原因）归为 unknown，仍可重试/忽略
    assert db.failure_groups()[0]['kind'] == 'unknown'
    assert db.retry_tasks(kinds=['unknown']) == 1


# ------------------------------------------------------------------ 失败原因
def _download_env(cfg, details, routes):
    api = FakeAPI()
    api.details.update(details)
    pro, _ = make_processor(api, routes)
    return pro, api, Database.local(cfg.DB_PATH)


def test_failure_kinds_are_recorded_and_grouped(cfg, no_sleep):
    pro, api, db = _download_env(
        cfg, {2: make_illust(2), 3: make_illust(3), 4: make_illust(4), 5: make_illust(5)},
        {'https://new.example/img/2_p0.png': 404, 'https://new.example/img/3_p0.png': 500,
         'https://new.example/img/4_p0.png': 403, 'https://new.example/img/5_p0.png': b'tiny'})
    for i in (1, 2, 3, 4, 5):
        seed(db, 1, i)          # 1 号作品没有登记 -> API 返回「已删除」
    pro.download()
    kinds = dict(db.conn.execute("SELECT illust_id, error_kind FROM illusts").fetchall())
    assert kinds == {1: 'deleted', 2: 'deleted', 3: 'network', 4: 'http', 5: 'content'}
    groups = {g['kind']: g for g in db.failure_groups(3)}
    assert groups['deleted']['count'] == 2 and groups['deleted']['auto_retry'] == 0      # 永久失败，不会自动重试
    assert groups['network']['count'] == 1 and groups['network']['auto_retry'] == 1 and groups['content']['count'] == 1
    assert 'HTTP' in groups['http']['sample'] or '403' in groups['http']['sample']
    assert pro.job.snapshot()['detail']['fail_kinds']['deleted']['count'] == 2


def test_storage_failure_is_classified_as_storage(cfg, no_sleep, monkeypatch):
    pro, api, db = _download_env(cfg, {1: make_illust(1)}, {'https://new.example/img/1_p0.png': BLOB})
    seed(db, 1, 1)
    monkeypatch.setattr(pro.storage, 'put_bytes', lambda *a, **k: (_ for _ in ()).throw(OSError('disk full')))
    pro.download()
    row = db.conn.execute("SELECT status, error_kind, last_error FROM illusts").fetchone()
    assert row[0] == -1 and row[1] == 'storage' and 'disk full' in row[2]


def test_success_clears_previous_error(cfg, no_sleep):
    pro, api, db = _download_env(cfg, {1: make_illust(1)}, {'https://new.example/img/1_p0.png': 500})
    seed(db, 1, 1)
    pro.download()
    assert db.conn.execute("SELECT error_kind FROM illusts").fetchone() == ('network',)
    pro.session.routes['https://new.example/img/1_p0.png'] = BLOB
    db.retry_tasks(kinds=['network'])
    pro.download()
    assert db.conn.execute("SELECT status, error_kind, last_error FROM illusts").fetchone() == (1, None, None)


def test_retry_and_ignore_by_kind_or_keys(cfg):
    db = Database(cfg.DB_PATH)
    for i, kind in enumerate(['deleted', 'network', 'network'], 1):
        seed(db, 1, i)
        db.mark_failed(f'{i}_0', kind, 'boom', permanent=(kind == 'deleted'))
    assert db.count_failed(kinds=['network']) == 2
    assert db.ignore_tasks(kinds=['deleted']) == 1
    assert db.stats()['failed'] == 2 and db.stats()['ignored'] == 1
    assert 1 not in [r[1] for r in db.get_pending_tasks()]           # 已忽略的不会被下载取到
    assert db.retry_tasks(keys=['2_0']) == 1
    assert db.conn.execute("SELECT status, attempts FROM illusts WHERE task_key='2_0'").fetchone() == (0, 0)
    assert db.reset_failed_tasks_by_filter() == 1                    # 「重试全部」不会碰已忽略的
    assert db.ignore_tasks(kinds=['deleted'], restore=True) == 1
    assert db.conn.execute("SELECT status FROM illusts WHERE task_key='1_0'").fetchone() == (-1,)


# ------------------------------------------------------------------ 结果明细
def test_sync_reports_new_works_per_artist(cfg, no_sleep):
    api = FakeAPI()
    api.following['public'] = [(10, 'Alice'), (20, 'Bob')]
    api.illusts[(10, 'illust')] = [make_illust(102, pages=3), make_illust(101)]
    api.illusts[(20, 'illust')] = []
    pro, _ = make_processor(api)
    pro.sync()
    d = pro.job.snapshot()['detail']['new']
    assert d['10'] == {'name': 'Alice', 'works': 2, 'files': 4, 'old': 0, 'is_new_artist': 1}
    assert '20' not in d
    assert pro.job.result['new_works'] == 2 and pro.job.result['new_files'] == 4 and pro.job.result['artists_with_new']== 1
    pro.sync()
    assert pro.job.result['new_works'] == 0                         # 再同步一次没有新东西
    api.illusts[(10, 'illust')].insert(0, make_illust(103))
    pro.sync()
    assert pro.job.result['new_works'] == 1 and pro.job.snapshot()['detail']['new']['10']['is_new_artist'] == 0


def test_sync_failure_details_and_download_details(cfg, no_sleep):
    api = FakeAPI()
    api.following['public'] = [(10, 'Alice'), (20, 'Bob')]
    api.illusts[(10, 'illust')] = [make_illust(105)]
    api.errors[('user_illusts', 20)] = err('Internal Server Error')
    api.details[105] = make_illust(105)
    pro, _ = make_processor(api, {'https://new.example/img/105_p0.png': BLOB})
    pro.sync()
    assert pro.job.snapshot()['detail']['failed']['20']['name'] == 'Bob'
    pro.download()
    dl = pro.job.snapshot()['detail']['downloaded']['10']
    assert dl == {'name': 'Alice', 'files': 1, 'bytes': len(BLOB)}


def test_runs_are_recorded_and_queryable(cfg, no_sleep):
    api = FakeAPI()
    api.following['public'] = [(10, 'Alice')]
    api.illusts[(10, 'illust')] = [make_illust(105)]
    pro, _ = make_processor(api)
    pro.sync()
    db = Database.local(cfg.DB_PATH)
    runs = db.recent_runs()
    assert len(runs) == 1 and runs[0]['kind'] == 'sync' and runs[0]['status'] == 'done' and runs[0]['success'] == 1
    assert pro.job.run_id == runs[0]['id']
    full = db.get_run(runs[0]['id'])
    assert full['summary']['result']['new_works'] == 1 and full['summary']['detail']['new']['10']['works'] == 1
    # 出错的任务也会记录
    pro.ensure_clients = lambda: {}
    with pytest.raises(RuntimeError):
        pro.sync()
    assert db.recent_runs()[0]['status'] == 'error' and db.recent_runs()[0]['error']
    assert db.last_run(['sync'])['status'] == 'error'


def test_runs_trimmed(cfg):
    db = Database(cfg.DB_PATH)
    for i in range(230):
        db.record_run({'kind': 'sync', 'status': 'done', 'started': 1, 'finished': 2})
    assert len(db.recent_runs(500)) <= 201


# ------------------------------------------------------------------ 作品库
def _library_db(cfg):
    db = Database(cfg.DB_PATH)
    seed(db, 1, 10, name='Alice', title='Blue sky', tags='["landscape","风景"]', is_r18=0, ai_type=1,
         illust_type=0, total_bookmarks=50, create_date='2026-01-01T00:00:00+09:00')
    seed(db, 1, 11, pages=3, name='Alice', title='Spicy', tags='["r18"]', is_r18=1, ai_type=2, illust_type=1,
         page_count=3, total_bookmarks=900, create_date='2026-03-01T00:00:00+09:00')
    seed(db, 2, 20, name='Bob', title='Dance', tags='[]', is_r18=0, ai_type=1, illust_type=2,
         total_bookmarks=5, create_date='2026-02-01T00:00:00+09:00')
    seed(db, 2, 21, name='Bob', title='not downloaded', is_r18=0)
    for key in ('10_0', '11_0', '11_1', '20_0'):
        db.mark_status(key, 1)
    db.conn.execute("UPDATE illusts SET download_date='2026-04-01 10:00:00' WHERE task_key='10_0'")
    db.conn.execute("UPDATE illusts SET download_date='2026-04-03 10:00:00' WHERE task_key IN ('11_0','11_1')")
    db.conn.execute("UPDATE illusts SET download_date='2026-04-02 10:00:00' WHERE task_key='20_0'")
    db.conn.commit()
    return db


def test_library_filters_sorts_and_search(cfg):
    db = _library_db(cfg)
    ids = lambda **kw: [w['illust_id'] for w in db.library(**kw)[0]]
    assert ids() == [11, 20, 10]                                     # 默认：最近入库在前，只含已下载的
    assert db.library()[1] == 3
    assert ids(sort='bookmarks') == [11, 10, 20]
    assert ids(sort='created') == [11, 20, 10]
    assert ids(q='sky') == [10] and ids(q='风景') == [10] and ids(q='bob') == [20] and ids(q='11') == [11]
    assert ids(r18='hide') == [20, 10] and ids(r18='only') == [11]
    assert ids(ai='hide') == [20, 10] and ids(ai='only') == [11]
    assert ids(kind='manga') == [11] and ids(kind='ugoira') == [20] and ids(kind='multi') == [11]
    assert ids(author_id=2) == [20]
    w = db.library()[0][0]
    assert w['author_name'] == 'Alice' and w['done'] == 2 and w['first_key'] == '11_0' and w['first_status'] == 1
    assert len(db.library(limit=2, offset=2)[0]) == 1


def test_recent_updates_order_and_work_detail(cfg):
    db = _library_db(cfg)
    assert [w['illust_id'] for w in db.recent_updates()] == [11, 20, 10]
    db.conn.execute("UPDATE illust_metadata SET caption='<b>hello</b><br />world &amp; co' WHERE illust_id=10")
    d = db.work_detail(10)
    assert d['tags'] == ['landscape', '风景'] and d['caption'] == 'hello\nworld & co' and d['author_name'] == 'Alice'
    assert [p['task_key'] for p in db.work_detail(11)['pages']] == ['11_0', '11_1', '11_2']
    assert db.work_detail(999) is None


def test_artist_labels_and_summaries(cfg):
    db = _library_db(cfg)
    db.set_label(2, pinned=True, note='喜欢的画风')
    db.set_label(2, note='x' * 800)
    lab = db.get_label(2)
    assert lab['pinned'] == 1 and len(lab['note']) == 500
    s = {a['author_id']: a for a in db.get_artist_summaries()}
    assert s[2]['pinned'] == 1 and s[1]['pinned'] == 0 and s[1]['recent'] == 0   # 下载时间是 2026-04，不在最近 7 天
    db.mark_status('10_0', 1)
    assert {a['author_id']: a for a in db.get_artist_summaries()}[1]['recent'] == 1


def test_plan_numbers(cfg):
    db = _library_db(cfg)
    db.conn.execute("UPDATE illusts SET file_size = 1000 WHERE status = 1")
    db.conn.commit()
    db.mark_failed('21_0', 'network', 'x')
    seed(db, 3, 30)
    p = db.plan_numbers(3)
    assert p['pending'] == 3 and p['pending_by_type'] == {'image': 3} and p['estimated_bytes'] == 3000   # 11_2、21_0、30_0
    assert p['exhausted'] == 0 and p['artists'] == 3
    db.mark_failed('30_0', 'deleted', 'gone', permanent=True)
    assert db.plan_numbers(3)['exhausted'] == 1


# ------------------------------------------------------------------ Web 接口
@pytest.fixture
def srv(cfg, no_sleep):
    api = FakeAPI()
    api.following['public'] = [(10, 'Alice'), (20, 'Bob')]
    api.illusts[(10, 'illust')] = [make_illust(105), make_illust(104)]
    api.illusts[(20, 'illust')] = [make_illust(205)]
    pro, _ = make_processor(api)
    httpd = web.make_server('127.0.0.1', 0, pro)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]

    class C:
        def __init__(self):
            self.pro, self.api, self.port = pro, api, port

        def req(self, method, path, body=None):
            conn = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
            h = {'Host': f'127.0.0.1:{port}'}
            if method != 'GET':
                h.update({'X-Pixiv-UI': '1', 'Content-Type': 'application/json'})
            conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
            r = conn.getresponse()
            raw = r.read()
            conn.close()
            return r.status, (json.loads(raw) if 'json' in (r.getheader('Content-Type') or '') else raw)

        def get(self, p):
            return self.req('GET', p)

        def post(self, p, b=None):
            return self.req('POST', p, b or {})

        def wait_job(self):
            end = time.time() + 15
            while time.time() < end:
                j = self.get('/api/job')[1]
                if not j['running'] and j['kind'] != 'idle':
                    return j
                time.sleep(0.05)
            raise AssertionError('job timeout')

    yield C()
    httpd.shutdown()
    httpd.server_close()


def test_web_plan_runs_and_result_detail(srv):
    s, plan = srv.get('/api/plan')
    assert s == 200 and plan['pending'] == 0 and plan['accounts_valid'] == 1 and plan['last_sync_run'] is None
    srv.post('/api/job', {'kind': 'sync'})
    j = srv.wait_job()
    assert j['detail']['new']['10']['works'] == 2 and j['run_id']
    s, runs = srv.get('/api/runs')
    assert runs['items'][0]['kind'] == 'sync'
    s, run = srv.get(f"/api/runs/{runs['items'][0]['id']}")
    assert run['summary']['result']['new_works'] == 3
    assert srv.get('/api/runs/99999')[0] == 404
    plan = srv.get('/api/plan')[1]
    assert plan['pending'] == 3 and plan['last_sync_run']['status'] == 'done'


def test_web_batch_jobs_only_touch_selected_artists(srv, cfg):
    srv.post('/api/job', {'kind': 'sync'})
    srv.wait_job()
    srv.api.details[205] = make_illust(205)
    srv.pro.session.routes['https://new.example/img/205_p0.png'] = BLOB
    assert srv.post('/api/job', {'kind': 'download_artists', 'author_ids': []})[0] == 400
    assert srv.post('/api/job', {'kind': 'download_artists', 'author_ids': ['x']})[0] == 400
    assert srv.post('/api/job', {'kind': 'download_artists', 'author_ids': [20]})[0] == 200
    j = srv.wait_job()
    assert j['kind'] == 'download' or j['kind'] == 'download_artists'
    db = Database.local(cfg.DB_PATH)
    assert db.conn.execute("SELECT COUNT(*) FROM illusts WHERE status=1").fetchone()[0] == 1
    assert db.conn.execute("SELECT illust_id FROM illusts WHERE status=1").fetchone() == (205,)
    # 同步选中的画师（用数据库里已有的名字，不再请求画师详情）
    srv.api.calls.clear()
    srv.post('/api/job', {'kind': 'sync_artists', 'author_ids': [10]})
    srv.wait_job()
    assert not [c for c in srv.api.calls if c[0] == 'user_detail']
    assert {c[1] for c in srv.api.calls if c[0] == 'user_illusts'} == {10}


def test_web_stop_marks_stopping(srv):
    gate = threading.Event()
    orig = srv.api.user_following
    srv.api.user_following = lambda *a, **k: (gate.wait(5), orig(*a, **k))[1]
    srv.post('/api/job', {'kind': 'sync'})
    time.sleep(0.2)
    srv.post('/api/job/stop')
    j = srv.get('/api/job')[1]
    assert j['stopping'] is True and '停止' in j['message']
    gate.set()
    assert srv.wait_job()['status'] in ('cancelled', 'done')


def test_web_failures_retry_ignore_flow(srv, cfg, no_sleep):
    srv.post('/api/job', {'kind': 'sync'})
    srv.wait_job()
    srv.post('/api/job', {'kind': 'download'})           # details 里没有登记 -> 全部「已删除」
    srv.wait_job()
    s, f = srv.get('/api/failures')
    assert f['groups'][0]['kind'] == 'deleted' and f['groups'][0]['count'] == 3
    s, t = srv.get('/api/tasks?status=failed&kind=deleted')
    assert t['total'] == 3 and t['items'][0]['error_kind'] == 'deleted' and t['items'][0]['last_error']
    assert srv.post('/api/tasks/retry', {})[0] == 400
    assert srv.post('/api/tasks/ignore', {'keys': 'nope'})[0] == 400
    s, r = srv.post('/api/tasks/ignore', {'kinds': ['deleted']})
    assert r['count'] == 3
    assert srv.get('/api/tasks?status=ignored')[1]['total'] == 3 and srv.get('/api/tasks?status=failed')[1]['total'] == 0
    assert srv.post('/api/tasks/ignore', {'kinds': ['deleted'], 'restore': True})[1]['count'] == 3
    assert srv.post('/api/tasks/retry', {'keys': ['105_0']})[1]['count'] == 1
    assert srv.get('/api/tasks?status=pending')[1]['total'] == 1


def test_web_library_work_detail_and_updates(srv, cfg):
    db = _library_db(cfg)
    srv.pro.storage  # 触发存储
    s, lib = srv.get('/api/library?sort=bookmarks&r18=hide')
    assert [w['illust_id'] for w in lib['items']] == [10, 20] and lib['total'] == 2
    assert srv.get('/api/library?q=bob')[1]['total'] == 1
    assert [w['illust_id'] for w in srv.get('/api/updates')[1]['items']] == [11, 20, 10]
    s, w = srv.get('/api/works/10')
    assert w['title'] == 'Blue sky' and w['pixiv_url'].endswith('/artworks/10') and w['tags'][0] == 'landscape'
    assert srv.get('/api/works/9999')[0] == 404
    assert srv.get('/api/library?kind=weird&sort=nonsense')[0] == 200      # 未知取值不报错


def test_web_artist_labels_batch_and_filters(srv, cfg):
    srv.post('/api/job', {'kind': 'sync'})
    srv.wait_job()
    assert srv.post('/api/artists/batch', {'ids': [], 'action': 'pin'})[0] == 400
    assert srv.post('/api/artists/batch', {'ids': [10], 'action': 'explode'})[0] == 400
    assert srv.post('/api/artists/batch', {'ids': [20], 'action': 'pin'})[1]['count'] == 1
    items = srv.get('/api/artists?sort=name')[1]['items']
    assert items[0]['author_id'] == 20 and items[0]['pinned'] == 1            # 置顶排最前
    assert srv.get('/api/artists?filter=pinned')[1]['total'] == 1
    s, lab = srv.post('/api/artists/10/label', {'note': '备注', 'pinned': True})
    assert lab == {'ok': True, 'pinned': 1, 'note': '备注'}
    assert srv.post('/api/artists/10/label', {'note': 5})[0] == 400
    a = srv.get('/api/artists/10')[1]
    assert a['label']['note'] == '备注' and 'recent' in a
    assert srv.post('/api/artists/batch', {'ids': [10, 20], 'action': 'unpin'})[1]['count'] == 2
    assert srv.get('/api/artists?filter=pinned')[1]['total'] == 0


def test_web_notifications(srv, cfg, monkeypatch):
    s, n = srv.get('/api/notifications')
    assert s == 200 and n['items'] == []
    cfg.TOKENS['main']['is_valid'] = False
    ids = {i['id'] for i in srv.get('/api/notifications')[1]['items']}
    assert 'no-account' in ids
    cfg.TOKENS['main']['is_valid'] = True
    cfg.TOKENS['b'] = {'token': 'x', 'is_valid': False}
    items = srv.get('/api/notifications')[1]['items']
    assert any(i['id'] == 'acc-b' and i['level'] == 'warn' for i in items)
    cfg.TOKENS.pop('b')
    # 任务出错 -> 通知；失败文件 -> 提示
    srv.pro.ensure_clients = lambda: {}
    srv.post('/api/job', {'kind': 'sync'})
    srv.wait_job()
    items = srv.get('/api/notifications')[1]['items']
    assert any(i['title'] == '上一次任务失败了' and i['action']['route'] == '#/logs' for i in items)
    db = Database.local(cfg.DB_PATH)
    seed(db, 1, 1)
    db.mark_failed('1_0', 'network', 'x')
    items = srv.get('/api/notifications')[1]['items']
    assert any(i['id'].startswith('failed-') and 'tab=failed' in i['action']['route'] for i in items)


def test_composite_job_phases_are_counted_separately(cfg, no_sleep):
    api = FakeAPI()
    api.following['public'] = [(10, 'Alice'), (20, 'Bob')]
    api.illusts[(10, 'illust')] = [make_illust(105), make_illust(104)]
    for i in (104, 105):
        api.details[i] = make_illust(i)
    pro, _ = make_processor(api, {f'https://new.example/img/{i}_p0.png': BLOB for i in (104, 105)})
    pro.sync_and_download()
    r = pro.job.result
    assert r['artists'] == 2 and r['artists_ok'] == 2 and r['new_works'] == 2          # 同步阶段的结果保留
    assert r['tasks'] == 2 and r['success'] == 2 and r['bytes'] == 2 * len(BLOB)        # 下载阶段单独计数
    assert pro.job.success == 2 and pro.job.total == 2                                  # 不再混入同步的 2 位画师
    run = Database.local(cfg.DB_PATH).get_run(pro.job.run_id)
    assert run['kind'] == 'sync_download' and run['success'] == 2
    assert run['summary']['detail']['downloaded']['10']['files'] == 2


def test_works_include_dimensions_for_the_waterfall_layout(cfg):
    db = _library_db(cfg)
    db.conn.execute("UPDATE illust_metadata SET width=800, height=1200 WHERE illust_id=10")
    db.conn.commit()
    items = {w['illust_id']: w for w in db.library()[0]}
    assert (items[10]['width'], items[10]['height']) == (800, 1200)
    assert 'width' in items[11] and 'height' in items[11]

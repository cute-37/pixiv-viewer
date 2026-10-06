import http.client
import io
import json
import threading
import time

import pytest
from PIL import Image

from pixiv_dl.database import Database
from fakes import FakeAPI, make_illust, make_processor
from pixiv_dl.web import server as web


@pytest.fixture
def srv(cfg, no_sleep, monkeypatch):
    monkeypatch.setattr(cfg, 'WEB_HOST', '127.0.0.1')
    api = FakeAPI()
    api.following['public'] = [(10, 'Alice <script>alert(1)</script>')]
    api.illusts[(10, 'illust')] = [make_illust(105), make_illust(104)]
    pro, _ = make_processor(api)
    httpd = web.make_server('127.0.0.1', 0, pro)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    host, port = httpd.server_address[:2]

    class C:
        def __init__(self):
            self.pro, self.api, self.port = pro, api, port

        def req(self, method, path, body=None, headers=None, host_header=None):
            conn = http.client.HTTPConnection(host, port, timeout=10)
            h = {'Host': host_header or f'127.0.0.1:{port}'}
            if method != 'GET':
                h['X-Pixiv-UI'] = '1'
                h['Content-Type'] = 'application/json'
            h.update(headers or {})
            conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
            r = conn.getresponse()
            raw = r.read()
            conn.close()
            ctype = r.getheader('Content-Type') or ''
            return r.status, (json.loads(raw) if 'json' in ctype else raw), r

        def get(self, path, **kw):
            return self.req('GET', path, **kw)

        def post(self, path, body=None, **kw):
            return self.req('POST', path, body or {}, **kw)

        def wait_job(self, timeout=15):
            end = time.time() + timeout
            while time.time() < end:
                _, j, _ = self.get('/api/job')
                if not j['running'] and j['kind'] != 'idle':
                    return j
                time.sleep(0.05)
            raise AssertionError('job did not finish')

    yield C()
    httpd.shutdown()
    httpd.server_close()



def test_host_header_and_csrf_protection(srv):
    assert srv.get('/api/overview', host_header='evil.example.com')[0] == 403            # DNS rebinding
    s, _, _ = srv.req('POST', '/api/job/stop', {}, headers={'X-Pixiv-UI': ''})
    assert s == 403                                                                      # 缺少自定义头
    s, _, _ = srv.post('/api/job/stop', headers={'Origin': 'https://evil.example.com'})
    assert s == 403                                                                      # 跨站来源
    assert srv.post('/api/job/stop')[0] == 200
    conn = http.client.HTTPConnection('127.0.0.1', srv.port)
    conn.request('POST', '/api/job/stop', body='{}', headers={'Host': f'127.0.0.1:{srv.port}'})
    assert conn.getresponse().status == 403                                              # 表单式跨站 POST
    conn.close()


def test_overview_artists_and_xss_data_is_plain_json(srv):
    s, o, _ = srv.get('/api/overview')
    assert s == 200 and o['accounts']['valid'] == 1 and o['stats']['artists'] == 0
    srv.post('/api/job', {'kind': 'sync'})
    j = srv.wait_job()
    assert j['status'] == 'done' and j['success'] == 1
    s, r, _ = srv.get('/api/artists?q=alice')
    assert r['total'] == 1 and r['items'][0]['author_name'].startswith('Alice <script>')   # 原样返回，由前端转义
    s, a, _ = srv.get('/api/artists/10')
    assert a['counts']['tasks'] == 2
    assert srv.get('/api/artists/99999')[0] == 404
    assert srv.get('/api/artists?filter=deleted')[1]['total'] == 0
    assert srv.get('/api/artists?sort=done&order=desc&page=1&per=5')[0] == 200


def test_job_conflict_and_validation(srv):
    assert srv.post('/api/job', {'kind': 'bogus'})[0] == 400
    assert srv.post('/api/job', {'kind': 'sync_artist'})[0] == 400            # 缺少 author_id
    assert srv.post('/api/job', {'kind': 'sync'})[0] == 200
    srv.wait_job()


def test_download_job_and_tasks_and_logs(srv, cfg):
    srv.post('/api/job', {'kind': 'sync'})
    srv.wait_job()
    srv.api.details[105] = make_illust(105)
    srv.api.details[104] = make_illust(104)
    srv.pro.session.routes.update({'https://new.example/img/105_p0.png': b'\x89PNG' + b'a' * 200,
                                   'https://new.example/img/104_p0.png': b'\x89PNG' + b'b' * 200})
    assert srv.post('/api/job', {'kind': 'download', 'limit': '1'})[0] == 200
    j = srv.wait_job()
    assert j['kind'] == 'download' and j['success'] == 1
    s, t, _ = srv.get('/api/tasks?status=pending')
    assert t['total'] == 1
    s, t, _ = srv.get('/api/tasks?status=done')
    assert t['total'] == 1 and t['items'][0]['title'] == 't'
    s, csv_body, r = srv.get('/api/tasks/export.csv')
    assert s == 200 and 'attachment' in r.getheader('Content-Disposition')
    s, logs, _ = srv.get('/api/logs')
    assert s == 200 and isinstance(logs['items'], list)


def test_works_thumb_and_file(srv, cfg):
    srv.post('/api/job', {'kind': 'sync'})
    srv.wait_job()
    srv.api.details[105] = make_illust(105)
    img = io.BytesIO()
    Image.new('RGB', (900, 600), (10, 120, 200)).save(img, 'PNG')
    srv.pro.session.routes['https://new.example/img/105_p0.png'] = img.getvalue()
    srv.post('/api/job', {'kind': 'download_artist', 'author_id': 10})
    srv.wait_job()
    s, w, _ = srv.get('/api/artists/10/works')
    done = [x for x in w['items'] if x['first_status'] == 1]
    assert w['total'] == 2 and len(done) == 1
    key = done[0]['first_key']
    s, data, r = srv.get(f'/api/thumb/{key}')
    assert s == 200 and r.getheader('Content-Type') == 'image/jpeg'
    assert Image.open(io.BytesIO(data)).width == 480                   # 默认 480 宽档（原图 900x600）
    s, big, _ = srv.get(f'/api/thumb/{key}?w=2000')
    assert s == 200 and Image.open(io.BytesIO(big)).width == 900                # 最大一档 1600，原图只有 900 宽：不放大
    s, mid, _ = srv.get(f'/api/thumb/{key}?w=600')
    assert s == 200 and Image.open(io.BytesIO(mid)).width == 640
    assert srv.get(f'/api/thumb/{key}?w=abc')[0] == 200
    s, data, r = srv.get(f'/api/file/{key}')
    assert s == 200 and data == img.getvalue() and r.getheader('Content-Type') == 'image/png'
    s, p, _ = srv.get('/api/works/105/pages')
    assert p['items'][0]['status'] == 1
    assert srv.get('/api/file/104_0')[0] == 404          # 未下载
    assert srv.get('/api/file/does_not_exist')[0] == 404
    assert srv.get('/api/file/..%2f..%2fsettings')[0] == 404


def test_accounts_never_leak_tokens(srv, cfg):
    cfg.TOKENS['main']['token'] = 'SUPER-SECRET-REFRESH-TOKEN'
    s, a, _ = srv.get('/api/accounts')
    assert 'SUPER-SECRET' not in json.dumps(a) and a['items'][0]['is_main']
    s, st, _ = srv.get('/api/settings')
    assert 'SUPER-SECRET' not in json.dumps(st)
    cfg.NAS_PASS = 'nas-secret-pass'
    s, st, _ = srv.get('/api/settings')
    assert 'nas-secret-pass' not in json.dumps(st) and st['settings']['NAS_PASS_SET'] is True
    s, ov, _ = srv.get('/api/overview')
    assert 'SUPER-SECRET' not in json.dumps(ov) and 'nas-secret-pass' not in json.dumps(ov)


def test_settings_validation_and_persistence(srv, cfg):
    s, e, _ = srv.post('/api/settings', {'MAIN_ACCOUNT_DOWNLOAD_THREADS': 99})
    assert s == 400 and 'MAIN_ACCOUNT_DOWNLOAD_THREADS' in e['error']
    assert srv.post('/api/settings', {'TOKENS': {}})[0] == 400                 # 不允许通过设置接口改 token
    assert srv.post('/api/settings', {'WEB_HOST': '0.0.0.0'})[0] == 400        # 监听地址不能从网页改
    assert srv.post('/api/settings', {'DELAY_SYNC': [3, 1]})[0] == 400
    s, _, _ = srv.post('/api/settings', {'MAIN_ACCOUNT_DOWNLOAD_THREADS': 3, 'SYNC_TYPES': ['illust'],
                                         'DELAY_DOWNLOAD': [0.1, 0.2], 'NAS_PASS': '', 'SYNC_NOVELS': False})
    assert s == 200
    assert cfg.MAIN_ACCOUNT_DOWNLOAD_THREADS == 3 and cfg.SYNC_TYPES == ['illust'] and cfg.DELAY_DOWNLOAD == (0.1, 0.2)
    saved = json.load(open(cfg.SETTINGS_FILE, encoding='utf-8'))['current']
    assert saved['SYNC_NOVELS'] is False
    cfg.NAS_PASS = 'keep-me'
    srv.post('/api/settings', {'NAS_PASS': ''})                                # 留空 = 不修改
    assert cfg.NAS_PASS == 'keep-me'


def test_settings_rejected_while_job_running(srv, cfg, monkeypatch):
    gate = threading.Event()
    orig = srv.api.user_following

    def slow(*a, **k):
        gate.wait(5)
        return orig(*a, **k)
    srv.api.user_following = slow
    srv.post('/api/job', {'kind': 'sync'})
    time.sleep(0.2)
    assert srv.post('/api/settings', {'MAX_RETRIES': 2})[0] == 409
    assert srv.post('/api/job', {'kind': 'sync'})[0] == 409                    # 同时只允许一个任务
    gate.set()
    srv.wait_job()


def test_stop_endpoint_cancels_running_job(srv, cfg):
    gate = threading.Event()
    orig = srv.api.user_following

    def slow(*a, **k):
        gate.wait(5)
        return orig(*a, **k)
    srv.api.user_following = slow
    srv.post('/api/job', {'kind': 'sync'})
    time.sleep(0.2)
    s, r, _ = srv.post('/api/job/stop')
    assert r['stopped'] is True
    gate.set()
    j = srv.wait_job()
    assert j['status'] in ('cancelled', 'done')
    assert srv.post('/api/job/stop')[1]['stopped'] is False


def test_artist_actions_and_task_maintenance(srv, cfg):
    srv.post('/api/job', {'kind': 'sync'})
    srv.wait_job()
    db = Database.local(cfg.DB_PATH)
    db.upsert_artist(10, None, is_deleted=1)
    assert srv.get('/api/artists?filter=deleted')[1]['total'] == 1
    srv.web = None
    s, r, _ = srv.post('/api/artists/10/action', {'action': 'clear_deleted'})
    assert s == 200
    db.upsert_artist(10, None, is_deleted=1)
    s, r, _ = srv.post('/api/artists/clear-deleted')
    assert r['count'] == 1
    db.mark_status('105_0', -1)
    s, r, _ = srv.post('/api/artists/10/action', {'action': 'retry_failed'})
    assert r['count'] == 1
    assert srv.post('/api/artists/10/action', {'action': 'bogus'})[0] == 400
    assert srv.post('/api/tasks/reclaim')[0] == 200
    assert srv.get('/api/alerts')[0] == 200
    assert srv.post('/api/illust', {'illust_id': 'abc'})[0] == 400
    assert srv.post('/api/maintenance/clean-temp')[0] == 200


def test_oauth_start_and_bad_finish(srv):
    s, r, _ = srv.post('/api/accounts/oauth/start')
    assert s == 200 and r['url'].startswith('https://app-api.pixiv.net/web/v1/login?') and 'code_challenge=' in r['url']
    assert srv.post('/api/accounts/oauth/finish', {'state': 'nope', 'callback': 'x'})[0] == 400
    assert srv.post('/api/accounts', {'name': '', 'token': ''})[0] == 400
    assert srv.post('/api/accounts/main', {'name': 'ghost'})[0] == 404
    assert srv.req('DELETE', '/api/accounts/ghost', {})[0] == 404


def test_test_storage_endpoint_local(srv, tmp_path):
    s, r, _ = srv.post('/api/settings/test-storage', {'STORAGE_MODE': 'local', 'LOCAL_SAVE_PATH': str(tmp_path / 'x')})
    assert s == 200 and r['ok'] is True
    s, r, _ = srv.get('/api/storage/status')
    assert s == 200 and r['ok'] is True

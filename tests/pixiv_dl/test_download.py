import hashlib
import io
import os
import zipfile

import pytest
from PIL import Image

from pixiv_dl import interrupt
from pixiv_dl.database import Database
from pixiv_dl.downloader import Throttle
from fakes import FakeAPI, err, make_illust, make_processor

STALE = 'https://old-host.invalid/img-original/'
BLOB = b'\x89PNG' + b'z' * 400


def seed(db, aid, iid, pages=1, media='image', name='Alice'):
    db.upsert_artist(aid, name)
    for i in range(pages):
        db.save_illust({'task_key': f'{iid}_{i}', 'illust_id': iid, 'page_index': i, 'author_id': aid,
                        'title': 't', 'url': f'{STALE}{iid}_p{i}.jpg', 'media_type': media})


def fresh_routes(iid, pages=1, data=BLOB):
    return {f'https://new.example/img/{iid}_p{i}.png': data for i in range(pages)}


def setup(cfg, iid=100, pages=1, aid=1):
    api = FakeAPI()
    api.details[iid] = make_illust(iid, pages=pages)
    pro, _ = make_processor(api, fresh_routes(iid, pages))
    db = Database.local(cfg.DB_PATH)
    seed(db, aid, iid, pages)
    return pro, api, db


def test_download_uses_fresh_urls_never_stored_ones(cfg, no_sleep):
    pro, api, db = setup(cfg, pages=3)
    pro.download()
    assert sorted(pro.session.requested) == [f'https://new.example/img/100_p{i}.png' for i in range(3)]
    assert not any('old-host' in u for u in pro.session.requested)
    for i in range(3):
        f = os.path.join(cfg.LOCAL_SAVE_PATH, '[1] Alice', f'100_p{i}.png')
        assert os.path.getsize(f) == len(BLOB)
    rows = db.conn.execute("SELECT status, file_hash, file_size, url FROM illusts ORDER BY page_index").fetchall()
    assert all(r[0] == 1 and r[1] == hashlib.sha256(BLOB).hexdigest() and r[2] == len(BLOB) for r in rows)
    assert rows[0][3] == 'https://new.example/img/100_p0.png'     # 库里的地址已刷新为最新
    assert [c for c in api.calls if c[0] == 'illust_detail'] == [('illust_detail', 100)]  # 多页只请求一次
    assert pro.job.status == 'done' and pro.job.success == 3 and pro.job.failed == 0


def test_second_run_does_nothing_and_existing_file_skips_api(cfg, no_sleep):
    pro, api, db = setup(cfg)
    pro.download()
    pro.session.requested.clear()
    api.calls.clear()
    pro.download()
    assert not pro.session.requested and not api.calls
    # 状态被重置（例如核查后）但文件还在：不请求 API，也不重新下载
    db.conn.execute("UPDATE illusts SET status = 0")
    db.conn.commit()
    pro.download()
    assert not pro.session.requested and not api.calls
    assert db.conn.execute("SELECT status FROM illusts").fetchone() == (1,)
    assert pro.job.skipped == 1


def test_existing_file_with_old_extension_is_recognised(cfg, no_sleep):
    pro, api, db = setup(cfg)
    folder = os.path.join(cfg.LOCAL_SAVE_PATH, '[1] Alice')
    os.makedirs(folder)
    with open(os.path.join(folder, '100_p0.jpg'), 'wb') as f:   # 旧地址的扩展名是 jpg，新地址是 png
        f.write(BLOB)
    pro.download()
    assert not pro.session.requested and not api.calls
    assert db.conn.execute("SELECT status FROM illusts").fetchone() == (1,)


def test_deleted_work_is_permanent_failure_without_http(cfg, no_sleep):
    api = FakeAPI()            # 没有登记 details -> Not Found
    pro, _ = make_processor(api)
    db = Database.local(cfg.DB_PATH)
    seed(db, 1, 555)
    pro.download()
    assert not pro.session.requested
    assert db.conn.execute("SELECT status, attempts FROM illusts").fetchone() == (-1, cfg.MAX_ATTEMPTS)
    assert db.recent_alerts() == []           # 已删除的作品只记失败原因，不刷告警
    assert db.conn.execute("SELECT error_kind FROM illusts").fetchone() == ('deleted',)
    pro.download()   # 永久失败的不会再被取出
    assert len([c for c in api.calls if c[0] == 'illust_detail']) == 1


def test_invisible_work_is_permanent_failure(cfg, no_sleep):
    api = FakeAPI()
    api.details[7] = make_illust(7, visible=False)
    pro, _ = make_processor(api)
    db = Database.local(cfg.DB_PATH)
    seed(db, 1, 7)
    pro.download()
    assert db.conn.execute("SELECT status, attempts FROM illusts").fetchone() == (-1, cfg.MAX_ATTEMPTS)


def test_page_missing_from_fresh_data(cfg, no_sleep):
    api = FakeAPI()
    api.details[8] = make_illust(8, pages=1)      # 现在只有 1 页，库里有 2 页任务
    pro, _ = make_processor(api, fresh_routes(8, 1))
    db = Database.local(cfg.DB_PATH)
    seed(db, 1, 8, pages=2)
    pro.download()
    st = dict(db.conn.execute("SELECT page_index, status FROM illusts").fetchall())
    assert st == {0: 1, 1: -1}


def test_http_errors(cfg, no_sleep):
    api = FakeAPI()
    api.details[1] = make_illust(1)
    api.details[2] = make_illust(2)
    api.details[3] = make_illust(3)
    routes = {'https://new.example/img/1_p0.png': 404, 'https://new.example/img/2_p0.png': 500,
              'https://new.example/img/3_p0.png': b'tiny'}
    pro, _ = make_processor(api, routes)
    db = Database.local(cfg.DB_PATH)
    for i in (1, 2, 3):
        seed(db, 1, i)
    pro.download()
    st = {r[0]: r[1:] for r in db.conn.execute("SELECT illust_id, status, attempts FROM illusts")}
    assert st[1] == (-1, cfg.MAX_ATTEMPTS)          # 404：地址失效，不再重试
    assert st[2] == (-1, 1)                         # 5xx：重试用尽后记一次失败，下次还会再试
    assert st[3] == (-1, 1)                         # 内容过小（被拦截）
    assert pro.session.requested.count('https://new.example/img/2_p0.png') == cfg.MAX_RETRIES
    assert pro.session.requested.count('https://new.example/img/1_p0.png') == 1
    assert not os.path.exists(os.path.join(cfg.LOCAL_SAVE_PATH, '[1] Alice', '1_p0.png'))


def test_auth_failure_requeues_instead_of_failing_tasks(cfg, no_sleep):
    api = FakeAPI()
    api.details[1] = err('Error occurred at the OAuth process. Please check your Access Token')
    api.auth = lambda refresh_token=None: err('invalid_grant')
    pro, _ = make_processor(api)
    db = Database.local(cfg.DB_PATH)
    seed(db, 1, 1)
    pro.download()
    # 账号认证失效不是任务本身的问题：任务保持待处理且不消耗重试次数
    assert db.conn.execute("SELECT status, attempts FROM illusts").fetchone() == (0, 0)


def test_unknown_artist_does_not_rename_existing_folder(cfg, no_sleep):
    pro, api, db = setup(cfg)
    folder = os.path.join(cfg.LOCAL_SAVE_PATH, '[1] Real Name')
    os.makedirs(folder)
    db.conn.execute("DELETE FROM artists")      # 画师记录丢失
    db.conn.commit()
    pro.download()
    assert os.path.exists(os.path.join(folder, '100_p0.png'))
    assert os.listdir(cfg.LOCAL_SAVE_PATH) == ['[1] Real Name']


def test_limit_and_author_filter(cfg, no_sleep):
    api = FakeAPI()
    routes = {}
    pro, _ = make_processor(api, routes)
    db = Database.local(cfg.DB_PATH)
    for aid, iid in ((1, 11), (1, 12), (2, 21)):
        api.details[iid] = make_illust(iid)
        routes.update(fresh_routes(iid))
        seed(db, aid, iid, name=f'a{aid}')
    pro.download(aid=2)
    assert db.conn.execute("SELECT illust_id FROM illusts WHERE status=1").fetchall() == [(21,)]
    pro.download(limit=1)
    assert db.conn.execute("SELECT COUNT(*) FROM illusts WHERE status=1").fetchone()[0] == 2


def test_startup_resets_stale_in_progress(cfg):
    db = Database.local(cfg.DB_PATH)
    seed(db, 1, 1)
    db.mark_status('1_0', 2)
    make_processor()
    assert db.conn.execute("SELECT status FROM illusts").fetchone() == (0,)


def test_interrupt_stops_and_session_is_reusable(cfg, no_sleep):
    api = FakeAPI()
    routes = {}
    pro, _ = make_processor(api, routes)
    db = Database.local(cfg.DB_PATH)
    for iid in range(1, 8):
        api.details[iid] = make_illust(iid)
        routes.update(fresh_routes(iid))
        seed(db, 1, iid)
    real_get = pro.session.get

    def get_and_interrupt(url, **kw):
        interrupt.set()                 # 模拟用户在第一个文件时点了「停止」/按 Ctrl+C
        return real_get(url, **kw)
    pro.session.get = get_and_interrupt
    pro.download()
    assert pro.job.status == 'cancelled'
    done = db.conn.execute("SELECT COUNT(*) FROM illusts WHERE status=1").fetchone()[0]
    assert done < 7
    # 回归：中断后整个会话不能"坏掉"，下一次操作应当正常工作
    assert not interrupt.is_set()
    pro.session.get = real_get
    pro.download()
    assert db.conn.execute("SELECT COUNT(*) FROM illusts WHERE status=1").fetchone()[0] == 7
    assert pro.job.status == 'done'


# ---------------------------------------------------------------- 动图 / 小说
def make_ugoira_zip(n=3):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        for i in range(n):
            im = io.BytesIO()
            Image.new('RGB', (16, 16), (i * 40, 10, 10)).save(im, 'PNG')
            z.writestr(f'{i:06d}.png', im.getvalue())
    return buf.getvalue()


def ugoira_setup(cfg, hq_ok=True):
    zbytes = make_ugoira_zip()
    api = FakeAPI()
    api.ugoira[50] = {'zip_urls': {'medium': 'https://new.example/z/50_ugoira600x600.zip'},
                      'frames': [{'file': f'{i:06d}.png', 'delay': 50} for i in range(3)]}
    routes = {'https://new.example/z/50_ugoira600x600.zip': zbytes}
    if hq_ok:
        routes['https://new.example/z/50_ugoira1920x1080.zip'] = zbytes
    pro, _ = make_processor(api, routes)
    db = Database.local(cfg.DB_PATH)
    db.upsert_artist(1, 'Alice')
    db.save_illust({'task_key': '50_0', 'illust_id': 50, 'page_index': 0, 'author_id': 1, 'title': 'u',
                    'url': 'https://old-host.invalid/50_ugoira600x600.zip', 'media_type': 'ugoira'})
    return pro, api, db


def test_ugoira_downloads_hq_zip_and_creates_webp(cfg, no_sleep):
    pro, api, db = ugoira_setup(cfg)
    pro.download()
    base = os.path.join(cfg.LOCAL_SAVE_PATH, '[1] Alice')
    assert os.path.getsize(os.path.join(base, '动图zip', '50_p0.zip')) > 100
    assert os.path.getsize(os.path.join(base, '50_p0.webp')) > 100
    assert pro.session.requested == ['https://new.example/z/50_ugoira1920x1080.zip']
    assert db.conn.execute("SELECT status FROM illusts").fetchone() == (1,)
    assert '"delay": 50' in db.get_ugoira_data(50)
    assert os.listdir(cfg.LOCAL_TEMP_PATH) == []            # 临时目录已清理


def test_ugoira_falls_back_to_600x600(cfg, no_sleep):
    pro, api, db = ugoira_setup(cfg, hq_ok=False)
    pro.download()
    assert pro.session.requested == ['https://new.example/z/50_ugoira1920x1080.zip',
                                     'https://new.example/z/50_ugoira600x600.zip']
    assert db.conn.execute("SELECT status FROM illusts").fetchone() == (1,)


def test_ugoira_already_saved_is_skipped(cfg, no_sleep):
    pro, api, db = ugoira_setup(cfg)
    pro.download()
    db.conn.execute("UPDATE illusts SET status = 0")
    db.conn.commit()
    api.calls.clear()
    pro.session.requested.clear()
    pro.download()
    assert not api.calls and not pro.session.requested
    assert db.conn.execute("SELECT status FROM illusts").fetchone() == (1,)


def test_ugoira_legacy_bin_zip_counts_as_saved(cfg, no_sleep):
    pro, api, db = ugoira_setup(cfg)
    base = os.path.join(cfg.LOCAL_SAVE_PATH, '[1] Alice')
    os.makedirs(os.path.join(base, '动图zip'))
    open(os.path.join(base, '动图zip', '50_p0.bin'), 'wb').write(b'z' * 200)
    open(os.path.join(base, '50_p0.webp'), 'wb').write(b'w' * 200)
    pro.download()
    assert not pro.session.requested and not api.calls


def test_ugoira_broken_zip_still_keeps_zip_and_alerts(cfg, no_sleep):
    pro, api, db = ugoira_setup(cfg)
    pro.session.routes = {u: b'not a zip' * 30 for u in pro.session.routes}
    pro.download()
    base = os.path.join(cfg.LOCAL_SAVE_PATH, '[1] Alice')
    assert os.path.exists(os.path.join(base, '动图zip', '50_p0.zip'))
    assert not os.path.exists(os.path.join(base, '50_p0.webp'))
    assert db.conn.execute("SELECT status FROM illusts").fetchone() == (1,)
    assert any(a['level'] == 'WARN' for a in db.recent_alerts())


def test_novel_download(cfg, no_sleep):
    api = FakeAPI()
    api.novel_text[900] = '第一章\n正文'
    pro, _ = make_processor(api)
    db = Database.local(cfg.DB_PATH)
    db.upsert_artist(1, 'Alice')
    db.save_novel(900, 1, 'N')
    pro.download()
    p = os.path.join(cfg.LOCAL_SAVE_PATH, '[1] Alice', '900_p0.txt')
    assert open(p, encoding='utf-8').read() == '第一章\n正文'
    assert db.conn.execute("SELECT status FROM illusts WHERE task_key='novel_900_0'").fetchone() == (1,)
    assert not pro.session.requested                       # 小说不走 HTTP


def test_novel_not_found_is_permanent(cfg, no_sleep):
    pro, api = make_processor(FakeAPI())[0], None
    db = Database.local(cfg.DB_PATH)
    db.upsert_artist(1, 'Alice')
    db.save_novel(901, 1, 'N')
    pro.download()
    assert db.conn.execute("SELECT status, attempts FROM illusts").fetchone() == (-1, cfg.MAX_ATTEMPTS)


# ---------------------------------------------------------------- 节流
def test_throttle_failure_rate_triggers_pause(cfg, monkeypatch):
    monkeypatch.setattr(cfg, 'AUTO_THROTTLE_ENABLED', True)
    monkeypatch.setattr(cfg, 'FAILURE_RATE_THRESHOLD', 0.5)
    monkeypatch.setattr(cfg, 'FAILURE_PAUSE_SECONDS', 30)
    t = Throttle()
    for _ in range(4):
        t.record(False)
    assert t._resume_at == 0
    t.record(False)                       # 窗口 >= 5 且全失败
    assert t._resume_at > 0
    interrupt.set()
    try:
        assert t.wait() is True           # 休息期间被中断能立刻返回
    finally:
        interrupt.clear()


def test_throttle_periodic_rest_rule(cfg, monkeypatch):
    monkeypatch.setattr(cfg, 'RATE_LIMIT_ENABLED', True)
    monkeypatch.setattr(cfg, 'REST_EVERY', 3)
    monkeypatch.setattr(cfg, 'REST_SECONDS', 20)
    t = Throttle()
    # 每个账号各数各的：两个账号各下了 2 个，谁都还没到 3 个
    for _ in range(2):
        t.record(True, 'main')
        t.record(True, 'backup')
    assert t.remaining('main') == 0 and t.remaining('backup') == 0
    t.record(True, 'main')                # main 的第 3 个：只有 main 休息
    assert t.remaining('main') > 10 and t.remaining('backup') == 0 and t._resume_at == 0
    t.record(True, 'backup')              # backup 的第 3 个：它也休息
    assert t.remaining('backup') > 10
    monkeypatch.setattr(cfg, 'RATE_LIMIT_ENABLED', False)
    quiet = Throttle()
    for _ in range(9):
        quiet.record(True, 'main')
    assert quiet.remaining('main') == 0   # 关掉之后不休息


def test_throttle_blocks_workers_during_download(cfg, no_sleep, monkeypatch):
    """回归：风控休息曾写在 join() 之后，下载过程中根本不会暂停。"""
    monkeypatch.setattr(cfg, 'RATE_LIMIT_ENABLED', True)
    monkeypatch.setattr(cfg, 'REST_EVERY', 2)
    monkeypatch.setattr(cfg, 'REST_SECONDS', 5)
    waits = []
    from pixiv_dl import downloader
    orig = downloader.Throttle.wait

    def spy(self, account=None):
        waits.append(self._account_until.get(account, 0))
        return orig(self, account)
    monkeypatch.setattr(downloader.Throttle, 'wait', spy)
    monkeypatch.setattr(cfg, 'MAIN_ACCOUNT_DOWNLOAD_THREADS', 1)
    api = FakeAPI()
    routes = {}
    pro, _ = make_processor(api, routes)
    db = Database.local(cfg.DB_PATH)
    for iid in (1, 2, 3, 4):
        api.details[iid] = make_illust(iid)
        routes.update(fresh_routes(iid))
        seed(db, 1, iid)
    pro.download()
    # 第 2 个文件完成后设置了休息；之后的 wait() 看到了休息时间（no_sleep 让等待立即返回）
    assert any(w > 0 for w in waits[2:])


def test_deleted_works_do_not_trigger_failure_rate_pause(cfg, no_sleep, monkeypatch):
    monkeypatch.setattr(cfg, 'AUTO_THROTTLE_ENABLED', True)
    monkeypatch.setattr(cfg, 'MAIN_ACCOUNT_DOWNLOAD_THREADS', 1)
    pro, _ = make_processor(FakeAPI())          # 所有作品都是"已删除"
    db = Database.local(cfg.DB_PATH)
    for iid in range(1, 9):
        seed(db, 1, iid)
    pro.download()
    assert db.conn.execute("SELECT COUNT(*) FROM illusts WHERE status=-1").fetchone()[0] == 8
    assert not any('失败率' in l['msg'] for l in pro.job.snapshot()['logs'])


def test_job_tracks_each_account_for_the_whole_phase(cfg, no_sleep):
    """每个账号在整个阶段都有一行状态（不会因为某个作品处理完就消失）：计数、状态、认证失效后保持「已停用」。"""
    pro, api, db = setup(cfg, pages=2)
    pro.download()
    ws = pro.job.snapshot()['workers']
    assert len(ws) == 1
    w = ws[0]
    assert w['success'] == 2 and w['failed'] == 0 and w['bytes'] == 2 * len(BLOB)
    assert w['state'] == 'done' and w['active'] == 0 and w['text'] == ''

    api2 = FakeAPI()
    api2.details[1] = err('Error occurred at the OAuth process. Please check your Access Token')
    api2.auth = lambda refresh_token=None: err('invalid_grant')
    pro2, _ = make_processor(api2)
    seed(Database.local(cfg.DB_PATH), 1, 1)
    pro2.download()
    w2 = pro2.job.snapshot()['workers'][0]
    assert w2['state'] == 'stopped' and '认证失效' in w2['note']


def test_worker_helpers_are_safe_for_unknown_accounts():
    from pixiv_dl.progress import JobState
    j = JobState('download')
    j.worker_begin('ghost', 'x'); j.worker_add('ghost', success=1); j.worker_end('ghost'); j.worker_exit('ghost')
    j.worker_register('a', 2)
    j.worker_begin('a', 't'); j.worker_begin('a', 't2')
    j.worker_end('a', 'waiting')
    assert j.snapshot()['workers'][0]['state'] == 'working'          # 还有一个线程在处理
    j.worker_end('a', 'waiting')
    assert j.snapshot()['workers'][0]['state'] == 'waiting'
    j.worker_set('a', state='stopped')
    j.worker_set('a', state='resting')                               # 已停用的账号不会被改回
    assert j.snapshot()['workers'][0]['state'] == 'stopped'


# ---- 一个账号看不到 ≠ 作品被删了（备用账号常常看不了主账号能看的 R-18 作品）
def _two_accounts(cfg, main_api, backup_api, routes=None):
    from fakes import make_client
    pro, _ = make_processor(main_api, routes)
    pro.clients = {'backup': make_client(backup_api, 'backup', 'tok-b'), 'main': make_client(main_api)}
    cfg.TOKENS = {'main': {'token': 'tok-main', 'is_valid': True}, 'backup': {'token': 'tok-b', 'is_valid': True}}
    return pro


def test_work_invisible_to_one_account_is_downloaded_by_another(cfg, no_sleep):
    main, backup = FakeAPI(), FakeAPI()
    for iid in (71, 72, 73, 74, 75, 76):
        main.details[iid] = make_illust(iid)
        backup.details[iid] = make_illust(iid, visible=False)        # 备用账号拿到“无权查看”的占位
    routes = {}
    for iid in (71, 72, 73, 74, 75, 76):
        routes.update(fresh_routes(iid, 1))
    pro = _two_accounts(cfg, main, backup, routes)
    db = Database.local(cfg.DB_PATH)
    for iid in (71, 72, 73, 74, 75, 76):
        seed(db, 1, iid)
    pro.download()
    assert db.conn.execute("SELECT status, COUNT(*) FROM illusts GROUP BY 1").fetchall() == [(1, 6)]
    assert pro.job.failed == 0 and pro.job.success == 6
    # 备用账号连续几次看不到这位画师之后，就不再拿这位画师的作品去试它
    assert len([c for c in backup.calls if c[0] == 'illust_detail']) <= 3


def test_work_no_account_can_view_is_marked_restricted_not_deleted(cfg, no_sleep):
    main, backup = FakeAPI(), FakeAPI()
    main.details[81] = make_illust(81, visible=False)
    backup.details[81] = make_illust(81, visible=False)
    pro = _two_accounts(cfg, main, backup)
    db = Database.local(cfg.DB_PATH)
    seed(db, 1, 81)        # 作品还在，只是都看不了
    seed(db, 1, 82)        # 两个账号都找不到：这才是已删除
    pro.download()
    kinds = dict(db.conn.execute("SELECT illust_id, error_kind FROM illusts").fetchall())
    assert kinds == {81: 'restricted', 82: 'deleted'}
    assert db.conn.execute("SELECT COUNT(*) FROM illusts WHERE status = -1").fetchone() == (2,)
    assert pro.job.failed == 2


def test_ugoira_not_found_on_one_account_is_retried_on_another(cfg, no_sleep):
    main, backup = FakeAPI(), FakeAPI()          # 两边都没有登记动图数据 -> Page not found
    pro = _two_accounts(cfg, main, backup)
    db = Database.local(cfg.DB_PATH)
    seed(db, 1, 91, media='ugoira')
    pro.download()
    # 两个账号都问过了，才下“已删除”的结论
    assert [c for c in main.calls if c[0] == 'ugoira_metadata'] and [c for c in backup.calls if c[0] == 'ugoira_metadata']
    assert db.conn.execute("SELECT error_kind FROM illusts").fetchone() == ('deleted',)


def test_each_thread_of_an_account_reports_what_it_is_doing():
    """一个账号开两个线程：两个线程各自正在处理的作品都要报出来，不能后一个盖掉前一个"""
    import threading
    from pixiv_dl.progress import JobState
    j = JobState('download')
    j.worker_register('a', 2)
    go, done = threading.Event(), threading.Event()

    def other():
        j.worker_begin('a', '作品二')
        go.set()
        done.wait(5)
        j.worker_end('a')

    t = threading.Thread(target=other)
    t.start()
    go.wait(5)
    j.worker_begin('a', '作品一')
    w = j.snapshot()['workers'][0]
    assert sorted(w['items']) == ['作品一', '作品二'] and w['threads'] == 2 and 'tasks' not in w
    j.worker_end('a')
    assert j.snapshot()['workers'][0]['items'] == ['作品二']
    done.set()
    t.join()
    assert j.snapshot()['workers'][0]['items'] == []


def test_deleted_novel_reported_as_parse_error_is_still_recognised_as_gone(cfg, no_sleep):
    """小说被删后 Pixiv 返回一小段“Page not found”，pixivpy 只会说“解析失败”。要认出这是“找不到”，不再反复重试"""
    api = FakeAPI()

    def webview_novel(novel_id, raw=False, **kw):
        api._log('webview_novel', novel_id)
        body = '{"error":{"user_message":"Page not found","message":"","reason":"","user_message_details":{}}}'
        if raw:
            return body
        raise Exception("Extract novel content error: 'NoneType' object has no attribute 'groups'")

    api.webview_novel = webview_novel
    pro, _ = make_processor(api)
    db = Database.local(cfg.DB_PATH)
    db.upsert_artist(1, 'Alice')
    db.save_novel(902, 1, 'N')
    pro.download()
    status, attempts, kind = db.conn.execute("SELECT status, attempts, error_kind FROM illusts").fetchone()
    assert (status, attempts) == (-1, cfg.MAX_ATTEMPTS) and kind != 'other'

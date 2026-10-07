"""检查与下载的改进：限速不算失败、检查失败有记录、续查、筛选与“不下载”、暂停、先看再下、马上重试。"""
import threading
import time

import pytest

from pixiv_dl import interrupt, ratelimit
from pixiv_dl.database import Database
from fakes import FakeAPI, err, make_client, make_illust, make_processor

BLOB = b'\x89PNG' + b'z' * 400


def api_with(artists):
    """artists: {aid: (name, [作品号, 新→旧])}"""
    api = FakeAPI()
    api.following['public'] = [(aid, name) for aid, (name, _) in artists.items()]
    for aid, (name, ids) in artists.items():
        api.users[aid] = {'name': name}
        api.illusts[(aid, 'illust')] = [make_illust(i) for i in ids]
    return api


def routes_for(api):
    routes = {}
    for items in api.illusts.values():
        for ill in items:
            api.details[ill['id']] = ill
            for i in range(ill['page_count']):
                routes[f"https://new.example/img/{ill['id']}_p{i}.png"] = BLOB
    return routes


class FlakyAPI(FakeAPI):
    """前 N 次读作品列表都回“限速”，之后恢复正常"""

    def __init__(self, limited_calls):
        super().__init__()
        self.left = limited_calls

    def user_illusts(self, user_id, type='illust', offset=None, **kw):
        with self.lock:
            if self.left > 0:
                self.left -= 1
                self.calls.append(('user_illusts', 'limited'))
                return err('Rate Limit')
        return super().user_illusts(user_id, type=type, offset=offset, **kw)


def statuses(db):
    return dict(db.conn.execute("SELECT task_key, status FROM illusts").fetchall())


# ================================================================ 限速
def test_short_rate_limit_is_waited_out_not_counted_as_failure(cfg, no_sleep, monkeypatch):
    monkeypatch.setattr(ratelimit, 'BUDGET', 10)            # 预算够用：等一等就过去了
    api = FlakyAPI(2)
    api.following['public'] = [(10, 'Alice')]
    api.users[10] = {'name': 'Alice'}
    api.illusts[(10, 'illust')] = [make_illust(105)]
    pro, _ = make_processor(api)
    pro.sync()
    db = Database.local(cfg.DB_PATH)
    assert pro.job.failed == 0 and pro.job.success == 1
    assert db.sync_failures() == []
    assert pro.job.result['rate_limit']['main']['trips'] == 2
    assert any('限速' in line['msg'] for line in pro.job.snapshot()['logs'])


def test_long_rate_limit_leaves_artists_unchecked_instead_of_failed(cfg, no_sleep):
    api = api_with({10: ('Alice', [105]), 20: ('Bob', [205]), 30: ('Carol', [305])})
    for aid in (10, 20, 30):
        api.errors[('user_illusts', aid)] = err('Rate Limit')
    pro, _ = make_processor(api)
    pro.sync()
    db = Database.local(cfg.DB_PATH)
    assert pro.job.failed == 0                               # 一位都不算失败
    assert pro.job.result['unchecked'] == 3
    assert db.sync_failures() == []
    limited = [c for c in api.calls if c[0] == 'user_illusts']
    assert len({c[1] for c in limited}) == 1                 # 第一位就确认账号用不了，没有再去撞后面两位


def test_other_accounts_take_over_when_one_is_rate_limited(cfg, no_sleep):
    good = api_with({10: ('Alice', [105]), 20: ('Bob', [205]), 30: ('Carol', [305])})
    bad = api_with({10: ('Alice', [105]), 20: ('Bob', [205]), 30: ('Carol', [305])})
    for aid in (10, 20, 30):
        bad.errors[('user_illusts', aid)] = err('Rate Limit')
    pro, _ = make_processor(good)
    pro.clients['backup'] = make_client(bad, name='backup', token='tok-b')
    cfg.TOKENS['backup'] = {'token': 'tok-b', 'is_valid': True}
    pro.sync()
    assert pro.job.success == 3 and pro.job.failed == 0 and pro.job.result['unchecked'] == 0
    assert pro.job.result['rate_limit']['backup']['exhausted'] is True


def test_rate_limited_download_keeps_files_pending_without_using_attempts(cfg, no_sleep):
    api = api_with({10: ('Alice', [105])})
    pro, _ = make_processor(api, routes_for(api))
    pro.sync()
    api.details[105] = err('Rate Limit')
    pro.download()
    db = Database.local(cfg.DB_PATH)
    row = db.conn.execute("SELECT status, COALESCE(attempts,0) FROM illusts WHERE task_key = '105_0'").fetchone()
    assert row == (0, 0)                                     # 还是待下载，没有记一次失败
    assert pro.job.failed == 0 and pro.job.result['unprocessed_groups'] == 1


def test_http_429_is_rate_limit_and_does_not_use_attempts(cfg, no_sleep):
    api = api_with({10: ('Alice', [105])})
    routes = routes_for(api)
    routes['https://new.example/img/105_p0.png'] = 429
    pro, _ = make_processor(api, routes)
    pro.sync()
    pro.download()
    db = Database.local(cfg.DB_PATH)
    row = db.conn.execute("SELECT status, COALESCE(attempts,0), error_kind FROM illusts WHERE task_key = '105_0'").fetchone()
    assert row == (-1, 0, 'rate_limit')
    assert [g['kind'] for g in db.failure_groups()] == ['rate_limit']


# ================================================================ 检查失败的记录与处理
def test_failed_artist_is_recorded_and_cleared_on_success(cfg, no_sleep):
    api = api_with({10: ('Alice', [105]), 20: ('Bob', [205])})
    api.errors[('user_illusts', 20)] = err('Internal Server Error')
    pro, _ = make_processor(api)
    pro.sync()
    db = Database.local(cfg.DB_PATH)
    failures = db.sync_failures()
    assert [(f['author_id'], f['name'], f['kind'], f['count']) for f in failures] == [(20, 'Bob', 'network', 1)]
    assert 'Internal Server Error' in failures[0]['error']
    assert pro.job.snapshot()['detail']['failed']['20']['kind'] == 'network'
    pro.sync()
    assert db.sync_failures()[0]['count'] == 2               # 连续失败次数
    del api.errors[('user_illusts', 20)]
    calls_before = len(api.calls)
    pro.sync(scope='failed')                                 # 只重查失败的
    assert db.sync_failures() == []
    assert not any(c[0] == 'user_following' for c in api.calls[calls_before:])      # 不用再读关注列表
    assert {c[1] for c in api.calls[calls_before:] if c[0] == 'user_illusts'} == {20}


def test_gone_artists_are_recorded_and_not_retried_every_time(cfg, no_sleep):
    api = api_with({10: ('Alice', [105])})
    pro, _ = make_processor(api)
    db = Database.local(cfg.DB_PATH)
    db.upsert_artist(30, 'Carol', is_deleted=1)              # 以前标记的，不在关注列表里
    api.errors[('user_illusts', 30)] = err('Not Found')
    pro.sync()                                               # 没有确认记录：顺带确认一次
    assert [f['kind'] for f in db.sync_failures()] == ['gone']
    assert any(c == ('user_illusts', 30, 'illust', None) for c in api.calls)
    before = len(api.calls)
    pro.sync()                                               # 刚确认过：这次不再为它花请求
    assert not any(c[0] == 'user_illusts' and c[1] == 30 for c in api.calls[before:])
    del api.errors[('user_illusts', 30)]                     # 账号其实还在
    api.illusts[(30, 'illust')] = [make_illust(305)]
    api.users[30] = {'name': 'Carol'}
    pro.sync(scope='gone')                                   # 单独复核
    assert db.get_artist_full(30)['is_deleted'] == 0 and db.sync_failures() == []


def test_skipped_artists_are_left_alone(cfg, no_sleep):
    api = api_with({10: ('Alice', [105]), 20: ('Bob', [205])})
    pro, _ = make_processor(api)
    pro.sync()
    db = Database.local(cfg.DB_PATH)
    assert db.set_sync_skip([20]) == 1
    before = len(api.calls)
    pro.sync()
    assert {c[1] for c in api.calls[before:] if c[0] == 'user_illusts'} == {10}
    assert [s['author_id'] for s in db.sync_skipped()] == [20]
    db.set_sync_skip([20], skip=False)
    assert db.sync_skipped() == []


def test_incomplete_following_list_is_reported_and_filled_from_database(cfg, no_sleep):
    api = api_with({10: ('Alice', [105])})
    pro, _ = make_processor(api)
    db = Database.local(cfg.DB_PATH)
    db.upsert_artist(20, 'Bob', is_followed=1)               # 以前查到过、这次关注列表没读到的
    api.illusts[(20, 'illust')] = [make_illust(205)]
    api.errors[('user_following', 'private')] = err('Internal Server Error')
    pro.sync()
    assert '私密' in pro.job.result['following_incomplete']
    assert pro.job.snapshot()['detail']['notes']['following']['name'] == '关注列表没有读全'
    assert db.get_artist(20)[2] == 205                       # 没读到的那位没有被漏掉


def test_scopes_never_stale_and_resume(cfg, no_sleep):
    api = api_with({10: ('Alice', [105]), 20: ('Bob', [205]), 30: ('Carol', [305])})
    pro, _ = make_processor(api)
    db = Database.local(cfg.DB_PATH)
    for aid, name in ((10, 'Alice'), (20, 'Bob'), (30, 'Carol')):
        db.upsert_artist(aid, name, is_followed=1)
    with db.tx() as c:
        c.execute("UPDATE artists SET last_sync_time = '2026-01-01 00:00:00' WHERE author_id = 10")
        c.execute("UPDATE artists SET last_sync_time = '2099-01-01 00:00:00' WHERE author_id = 20")

    def checked(**kw):
        before = len(api.calls)
        pro.sync(**kw)
        return list(dict.fromkeys(c[1] for c in api.calls[before:] if c[0] == 'user_illusts'))

    assert checked(scope='never') == [30]
    with db.tx() as c:
        c.execute("UPDATE artists SET last_sync_time = NULL WHERE author_id = 30")
        c.execute("UPDATE artists SET last_sync_time = '2026-01-01 00:00:00' WHERE author_id = 10")
    assert checked(scope='stale', stale_days=7) == [30, 10]  # 从没查过的在前，然后是最久没查的
    with db.tx() as c:
        c.execute("UPDATE artists SET last_sync_time = '2026-01-01 00:00:00' WHERE author_id = 10")
        c.execute("UPDATE artists SET last_sync_time = '2099-01-01 00:00:00' WHERE author_id IN (20, 30)")
    assert checked(resume_since='2050-01-01 00:00:00') == [10]      # 接着上次没查完的


def test_types_keep_separate_progress_and_old_works_are_marked(cfg, no_sleep):
    api = api_with({10: ('Alice', [105, 104])})
    pro, _ = make_processor(api)
    pro.sync()
    db = Database.local(cfg.DB_PATH)
    assert db.get_artist_marks(10) == {'illust': 105, 'manga': 0}     # 新画师：两种类型各记各的
    # 模拟旧版本留下的数据：只有一个共用的进度，漫画没有单独记过
    with db.tx() as c:
        c.execute("UPDATE artists SET wm_illust = NULL, wm_manga = NULL WHERE author_id = 10")
    assert db.get_artist_marks(10) == {'illust': 105, 'manga': 105}
    # 之后出现了漫画：两个比进度旧，一个比进度新
    api.illusts[(10, 'manga')] = [make_illust(110, type='manga', pages=2), make_illust(90, type='manga'), make_illust(80, type='manga')]
    pro.sync()
    origin = dict(db.conn.execute("SELECT task_key, origin FROM illusts").fetchall())
    assert origin['110_0'] == 'new' and origin['90_0'] == 'old' and origin['80_0'] == 'old'
    assert pro.job.result['new_files'] == 4 and pro.job.result['old_files'] == 2
    assert db.get_artist_marks(10) == {'illust': 105, 'manga': 110}


def test_backfill_scans_one_type_from_the_start(cfg, no_sleep, monkeypatch):
    monkeypatch.setattr(cfg, 'METADATA_REFRESH_LIMIT', 0)    # 不回看：平时一个旧作品都不会补
    api = api_with({10: ('Alice', [105])})
    pro, _ = make_processor(api)
    pro.sync()
    db = Database.local(cfg.DB_PATH)
    with db.tx() as c:                                       # 旧版本留下的数据：漫画没有单独的进度
        c.execute("UPDATE artists SET wm_illust = NULL, wm_manga = NULL WHERE author_id = 10")
    api.illusts[(10, 'manga')] = [make_illust(m, type='manga') for m in (90, 80, 70, 60)]
    pro.sync()
    assert db.conn.execute("SELECT COUNT(*) FROM illusts").fetchone()[0] == 1
    pro.sync(backfill=['manga'])
    assert db.conn.execute("SELECT COUNT(*) FROM illusts WHERE origin = 'old'").fetchone()[0] == 4


# ================================================================ 待下载：筛选、汇总、不下载
@pytest.fixture
def stocked(cfg, no_sleep):
    api = api_with({10: ('Alice', [105, 104, 103]), 20: ('Bob', [205])})
    api.illusts[(10, 'manga')] = [make_illust(120, type='manga', pages=3)]
    api.illusts[(20, 'illust')][0]['x_restrict'] = 1
    api.illusts[(20, 'illust')][0]['create_date'] = '2025-06-01T00:00:00+09:00'
    pro, _ = make_processor(api, routes_for(api))
    pro.sync()
    return pro, api, Database.local(cfg.DB_PATH)


def keys(rows):
    return sorted(r[0] for r in rows)


def test_pending_filters(stocked):
    pro, api, db = stocked
    assert len(db.get_pending_tasks()) == 7
    assert keys(db.get_pending_tasks(filters={'types': ['manga']})) == ['120_0', '120_1', '120_2']
    assert keys(db.get_pending_tasks(filters={'types': ['illust'], 'exclude_author_ids': [20]})) == ['103_0', '104_0', '105_0']
    assert keys(db.get_pending_tasks(filters={'exclude_r18': True, 'types': ['illust']})) == ['103_0', '104_0', '105_0']
    assert keys(db.get_pending_tasks(filters={'date_to': '2025-12-31'})) == ['205_0']
    assert keys(db.get_pending_tasks(filters={'date_from': '2026-01-01', 'author_ids': [20]})) == []
    assert keys(db.get_pending_tasks(filters={'keys': ['104_0', '205_0']})) == ['104_0', '205_0']
    # 每位画师最多 2 个文件：取最新的作品，但同一个作品的各页不拆开
    assert keys(db.get_pending_tasks(filters={'max_per_artist': 2})) == ['120_0', '120_1', '120_2', '205_0']


def test_pending_summary_groups_by_artist(stocked):
    pro, api, db = stocked
    data = db.pending_summary()
    assert data['totals']['files'] == 7 and data['totals']['works'] == 5 and data['totals']['artists'] == 2
    alice, bob = data['artists']
    assert (alice['name'], alice['files'], alice['works'], alice['illust'], alice['manga']) == ('Alice', 6, 4, 3, 3)
    assert (bob['files'], bob['r18'], bob['newest'], bob['is_new_artist']) == (1, 1, '2025-06-01', 1)
    assert alice['est_bytes'] > 0
    assert db.pending_summary({'types': ['manga']})['totals']['files'] == 3


def test_skip_and_restore_pending(stocked):
    pro, api, db = stocked
    assert db.skip_tasks({'author_ids': [10], 'types': ['manga']}) == 3
    assert len(db.get_pending_tasks()) == 4 and db.stats()['skipped'] == 3
    assert db.pending_summary(status=(-3,))['totals']['files'] == 3
    pro.download()
    assert {k: v for k, v in statuses(db).items() if k.startswith('120_')} == {'120_0': -3, '120_1': -3, '120_2': -3}
    assert sum(1 for v in statuses(db).values() if v == 1) == 4          # 没标“不下载”的都下了
    pro.sync()                                                            # 再检查一次，不会把它们变回待下载
    assert db.stats()['skipped'] == 3
    assert db.skip_tasks({'author_ids': [10]}, restore=True) == 3
    assert len(db.get_pending_tasks()) == 3


def test_download_with_filters_only_takes_matching_files(stocked):
    pro, api, db = stocked
    pro.download(filters={'types': ['illust'], 'exclude_author_ids': [20]})
    done = sorted(k for k, v in statuses(db).items() if v == 1)
    assert done == ['103_0', '104_0', '105_0']
    assert len(db.get_pending_tasks()) == 4


# ================================================================ 先看再下、马上重试
def test_sync_download_stops_for_review_when_many_new_files(stocked, cfg):
    pro, api, db = stocked
    api.illusts[(10, 'illust')].insert(0, make_illust(130, pages=4))
    api.details[130] = api.illusts[(10, 'illust')][0]
    pro.sync_and_download(review_over=3)                     # 新发现 4 个 > 3：先不下
    assert pro.job.result['needs_review'] is True
    assert sum(1 for v in statuses(db).values() if v == 1) == 0
    pro.sync_and_download(review_over=3)                     # 这次没有新发现：直接下
    assert 'needs_review' not in pro.job.result
    assert sum(1 for v in statuses(db).values() if v == 1) >= 7


def test_retry_now_downloads_only_the_chosen_failures(stocked):
    pro, api, db = stocked
    for key in ('103_0', '104_0'):
        db.mark_failed(key, 'network', 'boom', permanent=True)
    pro.retry_now(keys=['104_0'])
    st = statuses(db)
    assert st['104_0'] == 1 and st['103_0'] == -1            # 只处理选中的那个
    assert sum(1 for v in st.values() if v == 1) == 1        # 别的待下载没有被顺带下载
    pro.retry_now(kinds=['network'])
    assert statuses(db)['103_0'] == 1


# ================================================================ 暂停
def test_pause_holds_workers_and_resume_continues(stocked):
    pro, api, db = stocked
    real = pro._process_group
    seen = []

    def slow(client, wdb, iid, mt, ts, throttle):
        seen.append(time.time())
        if len(seen) == 1:
            interrupt.pause()
            threading.Timer(0.6, interrupt.resume).start()
        return real(client, wdb, iid, mt, ts, throttle)

    pro._process_group = slow
    pro.download()
    assert sum(1 for v in statuses(db).values() if v == 1) == 7      # 继续之后全部下完
    assert seen[1] - seen[0] >= 0.5                                   # 暂停期间没有开始下一个
    assert not interrupt.is_paused()


def test_stop_while_paused_ends_the_job(stocked):
    pro, api, db = stocked
    real = pro._process_group

    def first_then_pause(client, wdb, iid, mt, ts, throttle):
        out = real(client, wdb, iid, mt, ts, throttle)
        interrupt.pause()
        threading.Timer(0.3, interrupt.set).start()
        return out

    pro._process_group = first_then_pause
    pro.download()
    assert pro.job.status == 'cancelled'
    assert 0 < sum(1 for v in statuses(db).values() if v == 1) < 7


# ================================================================ 头像
def avatar_setup(cfg, with_file_for=()):
    import os
    api = api_with({10: ('Alice', [105]), 20: ('Bob', [205]), 30: ('Carol', [305])})
    routes = {f'https://stored.example/avatar/{a}.png': BLOB for a in (10, 20)}
    routes['https://fresh.example/avatar/30.png'] = BLOB                 # Carol 只有向接口现取的地址能用
    pro, _ = make_processor(api, routes)
    db = Database.local(cfg.DB_PATH)
    for aid, name in ((10, 'Alice'), (20, 'Bob'), (30, 'Carol')):
        # 数据库是导入的：记录里写着本地有头像，实际上头像文件夹是空的
        db.upsert_artist(aid, name, profile_image_local=f'avatars/{aid}.png',
                         profile_image_url=f'https://stored.example/avatar/{aid}.png' if aid != 30 else 'https://stale.example/30.png')
    os.makedirs(cfg.AVATARS_PATH, exist_ok=True)
    for aid in with_file_for:
        with open(os.path.join(cfg.AVATARS_PATH, f'{aid}.png'), 'wb') as f:
            f.write(BLOB)
    return pro, api, db


def test_missing_avatars_are_judged_by_files_not_database_records(cfg, no_sleep):
    pro, api, db = avatar_setup(cfg, with_file_for=(10,))
    assert pro.avatar_ids() == {10}
    pro.download_missing_avatars()
    assert pro.avatar_ids() == {10, 20, 30}
    assert pro.job.total == 2 and pro.job.success == 2 and pro.job.failed == 0
    assert 'https://stored.example/avatar/10.png' not in pro.session.requested      # 已经有的不重下
    # 地址还能用的直接下载，不占接口请求；只有地址失效的那位才去问了一次接口
    assert [c for c in api.calls if c[0] == 'user_detail'] == [('user_detail', 30)]


def test_avatar_failures_are_listed_and_retry_picks_them_up(cfg, no_sleep):
    pro, api, db = avatar_setup(cfg)
    del pro.session.routes['https://fresh.example/avatar/30.png']
    api.errors[('user_detail', 30)] = err('Internal Server Error')
    pro.download_missing_avatars()
    failed = pro.job.snapshot()['detail']['failed']
    assert pro.job.success == 2 and list(failed) == ['30']
    assert failed['30']['name'] == 'Carol' and 'Internal Server Error' in failed['30']['note']
    del api.errors[('user_detail', 30)]
    pro.session.routes['https://fresh.example/avatar/30.png'] = BLOB
    pro.download_missing_avatars()                                       # 再来一次：只补没成功的那一个
    assert pro.job.total == 1 and pro.job.success == 1 and pro.avatar_ids() == {10, 20, 30}


def test_force_redownloads_every_avatar(cfg, no_sleep):
    pro, api, db = avatar_setup(cfg, with_file_for=(10, 20, 30))
    pro.download_missing_avatars()
    assert pro.job.total == 0                                            # 都有了：没有要补的
    pro.download_missing_avatars(force=True)
    assert pro.job.total == 3 and pro.job.success == 3
    assert pro.session.requested.count('https://stored.example/avatar/10.png') == 1


def test_sync_fetches_missing_and_changed_avatars(cfg, no_sleep):
    import os
    api = api_with({10: ('Alice', [105])})
    real = api.user_following

    def following(user_id, restrict='public', offset=None):
        out = real(user_id, restrict=restrict, offset=offset)
        for p in out['user_previews']:
            p['user']['profile_image_urls'] = {'medium': api.avatar_url}
        return out

    api.user_following = following
    api.avatar_url = 'https://a.example/v1.png'
    pro, _ = make_processor(api, {'https://a.example/v1.png': BLOB, 'https://a.example/v2.png': BLOB + b'2'})
    pro.sync()
    path = os.path.join(cfg.AVATARS_PATH, '10.png')
    assert os.path.getsize(path) == len(BLOB)
    pro.sync()                                                           # 没变：不重下
    assert pro.session.requested.count('https://a.example/v1.png') == 1
    api.avatar_url = 'https://a.example/v2.png'                          # 画师换了头像
    pro.sync()
    assert os.path.getsize(path) == len(BLOB) + 1


# ================================================================ 日志里那两个报错
def test_restricted_access_message_is_treated_as_rate_limit():
    from pixiv_dl.pixiv_client import RATE_LIMIT, classify_error
    assert classify_error(err('Your access is currently restricted.')).kind == RATE_LIMIT


def test_ugoira_recorded_as_image_is_downloaded_as_ugoira(cfg, no_sleep):
    api = FakeAPI()
    ill = make_illust(700, type='ugoira')
    api.details[700] = ill
    pro, _ = make_processor(api, {})
    db = Database.local(cfg.DB_PATH)
    db.upsert_artist(1, 'Alice')
    db.save_illust({'task_key': '700_0', 'illust_id': 700, 'page_index': 0, 'author_id': 1, 'title': 't',
                    'url': 'https://old.example/700_p0.jpg', 'media_type': 'image'})        # 旧数据把动图记成了图片
    pro.download()
    row = db.conn.execute("SELECT media_type, last_error FROM illusts WHERE task_key = '700_0'").fetchone()
    assert row[0] == 'ugoira'                                            # 类型改过来了
    assert 'No connection adapters' not in (row[1] or '') and 'ugoira://' not in (row[1] or '')
    assert not any(u.startswith('ugoira://') for u in pro.session.requested)


def test_pause_takes_effect_between_pages_of_one_work(cfg, no_sleep):
    api = api_with({10: ('Alice', [])})
    api.illusts[(10, 'illust')] = [make_illust(500, pages=4)]
    pro, _ = make_processor(api, routes_for(api))
    pro.sync()
    real = pro._download_image_page
    times = []

    def page(db, key, iid, idx, url, folder, old_url):
        times.append(time.time())
        if len(times) == 2:
            interrupt.pause()
            threading.Timer(0.6, interrupt.resume).start()
        return real(db, key, iid, idx, url, folder, old_url)

    pro._download_image_page = page
    pro.download()
    assert len(times) == 4 and times[2] - times[1] >= 0.5                # 第 2 页之后停住了，继续后才下第 3 页
    assert sum(1 for v in statuses(Database.local(cfg.DB_PATH)).values() if v == 1) == 4


# ================================================================ 网络断了：等它回来
def outage(monkeypatch, down_probes, on_recover=None):
    """让网络“断”上几次探测，然后恢复；恢复的那一刻调用 on_recover"""
    from pixiv_dl import netwatch
    state = {'calls': 0}

    def probe():
        state['calls'] += 1
        if state['calls'] == down_probes + 1 and on_recover:
            on_recover()
        return state['calls'] > down_probes

    monkeypatch.setattr(netwatch, 'probe', probe)
    monkeypatch.setattr(netwatch, 'CACHE_SECS', 0)
    monkeypatch.setattr(netwatch, 'RETRY_SECS', 0.01)
    netwatch.reset()
    return state


def test_download_waits_for_network_instead_of_failing(cfg, no_sleep, monkeypatch):
    import requests
    api = api_with({10: ('Alice', [])})
    api.illusts[(10, 'illust')] = [make_illust(500, pages=3)]
    routes = routes_for(api)
    good = routes['https://new.example/img/500_p1.png']
    routes['https://new.example/img/500_p1.png'] = requests.ConnectionError('网络断了')      # 第 2 页下到一半断网
    routes['https://new.example/img/500_p2.png'] = requests.ConnectionError('网络断了')
    pro, _ = make_processor(api, routes)
    pro.sync()

    def recover():
        routes['https://new.example/img/500_p1.png'] = good
        routes['https://new.example/img/500_p2.png'] = good

    state = outage(monkeypatch, down_probes=3, on_recover=recover)
    pro.download()
    db = Database.local(cfg.DB_PATH)
    rows = db.conn.execute("SELECT status, COALESCE(attempts,0) FROM illusts ORDER BY page_index").fetchall()
    assert rows == [(1, 0), (1, 0), (1, 0)]                               # 全部下完，没有占用重试次数
    assert pro.job.failed == 0 and pro.job.success == 3 and pro.job.done == 3      # 已经下好的第 1 页没有被重复计数
    assert pro.session.requested.count('https://new.example/img/500_p0.png') == 1
    logs = ' '.join(line['msg'] for line in pro.job.snapshot()['logs'])
    assert '网络连不上' in logs and '网络恢复了' in logs
    assert pro.job.result['network']['outages'] == 1 and state['calls'] >= 4


def test_network_errors_count_as_failures_when_the_network_is_actually_up(cfg, no_sleep):
    import requests
    api = api_with({10: ('Alice', [105])})
    routes = routes_for(api)
    routes['https://new.example/img/105_p0.png'] = requests.ConnectionError('只有这一个地址连不上')
    pro, _ = make_processor(api, routes)
    pro.sync()
    pro.download()                                                        # 探测是通的：照常记一次失败
    db = Database.local(cfg.DB_PATH)
    assert db.conn.execute("SELECT status, attempts, error_kind FROM illusts").fetchone() == (-1, 1, 'network')


def test_gives_up_waiting_after_the_limit_and_fails_normally(cfg, no_sleep, monkeypatch):
    import requests
    from pixiv_dl import netwatch
    api = api_with({10: ('Alice', [105])})
    routes = routes_for(api)
    routes['https://new.example/img/105_p0.png'] = requests.ConnectionError('一直连不上')
    pro, _ = make_processor(api, routes)
    pro.sync()
    outage(monkeypatch, down_probes=10 ** 6)
    monkeypatch.setattr(netwatch, 'MAX_WAIT', 0)
    pro.download()
    db = Database.local(cfg.DB_PATH)
    assert db.conn.execute("SELECT status, error_kind FROM illusts").fetchone() == (-1, 'network')
    assert any('不再等了' in line['msg'] for line in pro.job.snapshot()['logs'])


def test_api_calls_wait_for_network_without_using_attempts(cfg, no_sleep, monkeypatch):
    class Offline(FakeAPI):
        fails = 10 ** 6                                                   # 网络没恢复之前一直失败

        def user_illusts(self, user_id, type='illust', offset=None, **kw):
            if Offline.fails > 0:
                Offline.fails -= 1
                raise ConnectionError('没有网络')
            return super().user_illusts(user_id, type=type, offset=offset, **kw)

    api = Offline()
    api.following['public'] = [(10, 'Alice')]
    api.users[10] = {'name': 'Alice'}
    api.illusts[(10, 'illust')] = [make_illust(105)]
    pro, _ = make_processor(api)
    outage(monkeypatch, down_probes=6, on_recover=lambda: setattr(Offline, 'fails', 0))
    pro.sync()
    assert pro.job.failed == 0 and pro.job.success == 1                   # 网络回来后查成功了，没有算失败
    assert Database.local(cfg.DB_PATH).sync_failures() == []


# ================================================================ 任务进行时不让电脑睡眠
def test_keep_awake_follows_the_wanted_state(monkeypatch):
    from pixiv_dl import keepawake
    calls, want = [], {'on': False}
    monkeypatch.setattr(keepawake, '_set_state', lambda awake: calls.append(awake) or True)
    keeper = keepawake.KeepAwake(lambda: want['on'])
    keeper.start()
    time.sleep(1.3)
    assert calls == []                                                    # 没有任务：什么都不请求
    want['on'] = True
    time.sleep(1.3)
    assert calls == [True] and keeper.active
    time.sleep(1.2)
    assert calls == [True]                                                # 状态没变就不重复请求
    want['on'] = False
    time.sleep(1.3)
    assert calls == [True, False] and not keeper.active
    want['on'] = True
    time.sleep(1.3)
    keeper.stop()
    time.sleep(1.3)
    assert calls == [True, False, True, False]                            # 停掉时一定会撤回


def test_job_requests_awake_only_while_running_and_not_paused(stocked, cfg, monkeypatch):
    from pixiv_dl import keepawake
    pro, api, db = stocked
    calls = []
    monkeypatch.setattr(keepawake, '_set_state', lambda awake: calls.append(awake) or True)
    real = pro._process_group
    marks = {}

    def slow(client, wdb, iid, mt, ts, throttle):
        if 'first' not in marks:
            marks['first'] = True
            time.sleep(1.4)                                               # 任务在跑：这期间应该已经请求了“别睡”
            marks['while_running'] = list(calls)
            interrupt.pause()
            threading.Timer(1.6, lambda: (marks.__setitem__('while_paused', list(calls)), interrupt.resume())).start()
        return real(client, wdb, iid, mt, ts, throttle)

    pro._process_group = slow
    pro.download()
    time.sleep(1.4)
    assert marks['while_running'] == [True]
    assert marks['while_paused'] == [True, False]                         # 暂停时撤回
    assert calls[-1] is False                                             # 结束后一定是撤回的状态


def test_keep_awake_can_be_turned_off(stocked, cfg, monkeypatch):
    from pixiv_dl import keepawake
    pro, api, db = stocked
    calls = []
    monkeypatch.setattr(keepawake, '_set_state', lambda awake: calls.append(awake) or True)
    monkeypatch.setattr(cfg, 'KEEP_AWAKE', False)
    real = pro._process_group

    def slow(client, wdb, iid, mt, ts, throttle):
        time.sleep(0.4)
        return real(client, wdb, iid, mt, ts, throttle)

    pro._process_group = slow
    pro.download()
    assert calls == []


# ================================================================ 周期性休息：每个账号各算各的
def test_periodic_rest_is_per_account_in_a_real_download(cfg, no_sleep, monkeypatch):
    from pixiv_dl import downloader
    monkeypatch.setattr(cfg, 'RATE_LIMIT_ENABLED', True)
    monkeypatch.setattr(cfg, 'REST_EVERY', 3)
    monkeypatch.setattr(cfg, 'REST_SECONDS', 1)
    api = api_with({10: ('Alice', list(range(120, 100, -1)))})            # 20 个作品
    pro, _ = make_processor(api, routes_for(api))
    pro.clients['backup'] = make_client(api, name='backup', token='tok-b')
    cfg.TOKENS['backup'] = {'token': 'tok-b', 'is_valid': True}
    pauses = []
    real = downloader.Throttle._pause

    def spy(self, seconds, reason, account=None):
        pauses.append(account)
        return real(self, seconds, reason, account)

    monkeypatch.setattr(downloader.Throttle, '_pause', spy)
    pro.sync()
    pro.download()
    assert pro.job.success == 20
    assert pauses and None not in pauses                                  # 只有“某个账号休息”，没有“所有账号一起停”
    workers = {w['name']: w['success'] for w in pro.job.snapshot()['workers']}
    assert sum(workers.values()) == 20
    for name, done in workers.items():                                    # 每个账号按自己下的数量休息
        assert pauses.count(name) == done // 3
    assert any('其他账号继续' in line['msg'] for line in pro.job.snapshot()['logs'])


def test_check_avatars_reports_missing_and_corrects_records(cfg, no_sleep):
    pro, api, db = avatar_setup(cfg, with_file_for=(10,))
    db.upsert_artist(40, 'Gone', is_deleted=1)                           # 已注销的不算
    db.conn.execute("UPDATE artists SET profile_image_local = NULL WHERE author_id = 10")
    db.conn.commit()
    r = pro.check_avatars()
    assert (r['artists'], r['have'], r['missing']) == (3, 1, 2)
    assert [x['name'] for x in r['items']] == ['Bob', 'Carol'] and r['fixed'] == 3
    assert api.calls == [] and pro.session.requested == []               # 只看文件，不访问 Pixiv
    rows = dict(db.conn.execute("SELECT author_id, COALESCE(profile_image_local, '') FROM artists WHERE author_id < 40"))
    assert rows == {10: 'avatars/10.png', 20: '', 30: ''}                # 记录改成和实际一致
    assert pro.check_avatars()['fixed'] == 0
    assert [r[0] for r in db.get_artists_without_avatar()] == [20, 30]


def test_avatar_lookups_are_shared_between_accounts(cfg, no_sleep, monkeypatch):
    """地址失效、要向 Pixiv 问的头像：由所有账号分着做；被限速太久的账号退出，别的账号接着做"""
    from pixiv_dl import ratelimit
    people = {a: (f'P{a}', [a * 10 + 5]) for a in range(1, 9)}
    api, other = api_with(people), api_with(people)
    pro, _ = make_processor(api, {f'https://fresh.example/avatar/{a}.png': BLOB for a in people})
    pro.clients['backup'] = make_client(other, name='backup', token='tok-b')
    cfg.TOKENS['backup'] = {'token': 'tok-b', 'is_valid': True}
    db = Database.local(cfg.DB_PATH)
    for a, (name, _) in people.items():
        db.upsert_artist(a, name)                                        # 没有记下头像地址：都得问接口
    pro.download_missing_avatars()
    asked = [sorted(c[1] for c in x.calls if c[0] == 'user_detail') for x in (api, other)]
    assert sorted(asked[0] + asked[1]) == list(people) and asked[0] and asked[1]     # 每位只问一次，两个账号都出了力
    assert pro.job.success == 8 and pro.avatar_ids() == set(people)
    # 一个账号被限速太久：它不再参与，剩下的全由另一个账号完成
    import os
    for a in people:
        os.remove(os.path.join(cfg.AVATARS_PATH, f'{a}.png'))
    api.calls.clear(), other.calls.clear()
    monkeypatch.setattr(ratelimit.RateGate, 'exhausted', lambda self, name: name == 'backup')
    pro.download_missing_avatars()
    assert not [c for c in other.calls if c[0] == 'user_detail'] and pro.job.success == 8


def test_bytes_are_counted_while_a_file_is_still_downloading(cfg, no_sleep):
    """大文件下载期间文件数不变，但“收到的字节数”边下边涨——界面靠它显示实时速度，不会看着像卡住"""
    pro, _ = make_processor(None, {'https://img.example/big.zip': BLOB * 50})
    assert pro.job.snapshot()['transferred'] == 0
    data = pro._http_get('https://img.example/big.zip')
    assert pro.job.snapshot()['transferred'] == len(data) == len(BLOB) * 50
    assert pro.job.bytes == 0                                # 这个数要等文件处理完才加

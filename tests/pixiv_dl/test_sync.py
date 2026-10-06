from pixiv_dl.database import Database
from fakes import FakeAPI, err, make_illust, make_processor


def _api_with_artists():
    api = FakeAPI()
    api.following['public'] = [(10, 'Alice'), (20, 'Bob')]
    api.illusts[(10, 'illust')] = [make_illust(i) for i in (105, 104, 103, 102, 101)]
    api.illusts[(20, 'illust')] = [make_illust(205)]  # 只有一页
    return api


def test_first_sync_saves_everything_and_watermark_even_for_single_page(cfg, no_sleep):
    api = _api_with_artists()
    pro, _ = make_processor(api)
    pro.sync()
    db = Database.local(cfg.DB_PATH)
    assert db.get_artist(10)[2] == 105
    assert db.get_artist(20)[2] == 205          # 回归：单页画师的水位线以前从未写入
    assert db.conn.execute("SELECT COUNT(*) FROM illusts").fetchone()[0] == 6
    assert db.get_artist_full(10)['is_followed'] == 1 and db.get_artist_full(10)['is_deleted'] == 0
    assert pro.job.status == 'done' and pro.job.success == 2 and pro.job.total == 2


def test_incremental_sync_stops_after_refresh_window_and_advances_watermark(cfg, no_sleep):
    api = _api_with_artists()
    pro, _ = make_processor(api)
    pro.sync()
    api.calls.clear()
    api.illusts[(10, 'illust')].insert(0, make_illust(106))
    pro.sync()
    db = Database.local(cfg.DB_PATH)
    assert db.get_artist(10)[2] == 106
    assert db.conn.execute("SELECT 1 FROM illusts WHERE task_key='106_0'").fetchone()
    # METADATA_REFRESH_LIMIT=2：106 是新作，之后只再刷新 2 个旧作就停止，不会翻完全部
    scanned = [c for c in api.calls if c[0] == 'user_illusts' and c[1] == 10 and c[2] == 'illust']
    assert len(scanned) == 2  # page_size=2：第一页 106,105；第二页 104,103（超过窗口停止）


def test_deep_sync_rescans_everything(cfg, no_sleep):
    api = _api_with_artists()
    pro, _ = make_processor(api)
    pro.sync()
    api.calls.clear()
    pro.sync(deep=True)
    scanned = [c for c in api.calls if c[0] == 'user_illusts' and c[1] == 10 and c[2] == 'illust']
    assert len(scanned) == 3  # 5 个作品 / 每页 2 个


def test_manga_is_synced_and_multipage_expanded(cfg, no_sleep):
    api = _api_with_artists()
    api.illusts[(10, 'manga')] = [make_illust(90, type='manga', pages=3)]
    pro, _ = make_processor(api)
    pro.sync()
    db = Database.local(cfg.DB_PATH)
    assert db.conn.execute("SELECT illust_type, page_count FROM illust_metadata WHERE illust_id=90").fetchone() == (1, 3)
    assert db.conn.execute("SELECT COUNT(*) FROM illusts WHERE illust_id=90").fetchone()[0] == 3
    assert ('user_illusts', 10, 'manga', None) in api.calls


def test_sync_types_config_respected(cfg, no_sleep, monkeypatch):
    monkeypatch.setattr(cfg, 'SYNC_TYPES', ['illust'])
    api = _api_with_artists()
    pro, _ = make_processor(api)
    pro.sync()
    assert not [c for c in api.calls if c[0] == 'user_illusts' and c[2] == 'manga']


def test_invisible_works_are_skipped(cfg, no_sleep):
    api = _api_with_artists()
    api.illusts[(20, 'illust')] = [make_illust(205, visible=False), make_illust(204)]
    pro, _ = make_processor(api)
    pro.sync()
    db = Database.local(cfg.DB_PATH)
    assert not db.conn.execute("SELECT 1 FROM illusts WHERE illust_id=205").fetchone()
    assert db.conn.execute("SELECT 1 FROM illusts WHERE illust_id=204").fetchone()


def test_not_found_marks_deleted_but_transient_error_does_not(cfg, no_sleep):
    api = _api_with_artists()
    api.errors[('user_illusts', 10)] = err('Not Found')       # 画师不存在
    api.errors[('user_illusts', 20)] = err('Internal Server Error')  # 临时故障
    pro, _ = make_processor(api)
    pro.sync()
    db = Database.local(cfg.DB_PATH)
    assert db.get_artist_full(10)['is_deleted'] == 1
    assert db.get_artist_full(20)['is_deleted'] == 0          # 回归：以前任何异常都会标记为注销
    assert pro.job.failed == 2
    # 故障恢复后，下次同步清除标记
    del api.errors[('user_illusts', 10)]
    del api.errors[('user_illusts', 20)]
    pro.sync()
    assert db.get_artist_full(10)['is_deleted'] == 0 and db.get_artist(10)[2] == 105


def test_previously_deleted_artists_are_retried(cfg, no_sleep):
    api = _api_with_artists()
    pro, _ = make_processor(api)
    db = Database.local(cfg.DB_PATH)
    db.upsert_artist(30, 'Carol', is_deleted=1)               # 不在关注列表，但此前被标记
    api.users[30] = {'name': 'Carol'}
    api.illusts[(30, 'illust')] = [make_illust(305)]
    pro.sync()
    assert db.get_artist_full(30)['is_deleted'] == 0 and db.get_artist(30)[2] == 305


def test_ugoira_metadata_fetched_once(cfg, no_sleep):
    api = _api_with_artists()
    api.illusts[(20, 'illust')] = [make_illust(205, type='ugoira')]
    api.ugoira[205] = {'zip_urls': {'medium': 'https://x/205_ugoira600x600.zip'}, 'frames': [{'file': '0.jpg', 'delay': 50}]}
    pro, _ = make_processor(api)
    pro.sync()
    pro.sync(deep=True)
    db = Database.local(cfg.DB_PATH)
    assert '"delay": 50' in db.get_ugoira_data(205)
    assert db.conn.execute("SELECT url, media_type FROM illusts WHERE illust_id=205").fetchone() == ('ugoira://205', 'ugoira')
    assert len([c for c in api.calls if c[0] == 'ugoira_metadata']) == 1


def test_novels_saved_and_never_overwrite_illust_metadata(cfg, no_sleep):
    api = _api_with_artists()
    api.novels[10] = [{'id': 777, 'title': 'a novel'}, {'id': 105, 'title': 'same id as an illust'}]
    pro, _ = make_processor(api)
    pro.sync()
    db = Database.local(cfg.DB_PATH)
    assert db.conn.execute("SELECT media_type, url FROM illusts WHERE task_key='novel_777_0'").fetchone() == ('novel', 'novel://777')
    assert db.conn.execute("SELECT title FROM illust_metadata WHERE illust_id=777").fetchone() == ('a novel',)
    # ID 与已有插画冲突：跳过，且不改动插画的元数据
    assert not db.conn.execute("SELECT 1 FROM illusts WHERE task_key='novel_105_0'").fetchone()
    assert db.conn.execute("SELECT title FROM illust_metadata WHERE illust_id=105").fetchone() == ('t',)


def test_following_failure_falls_back_to_known_artists(cfg, no_sleep):
    api = _api_with_artists()
    pro, _ = make_processor(api)
    pro.sync()
    api.errors[('user_following', 'public')] = err('Internal Server Error')
    api.calls.clear()
    pro.sync()
    assert pro.job.status == 'done' and pro.job.total == 2


def test_single_artist_sync_uses_user_detail(cfg, no_sleep):
    api = _api_with_artists()
    api.users[10] = {'name': 'Alice'}
    pro, _ = make_processor(api)
    pro.sync(aid=10, deep=True)
    db = Database.local(cfg.DB_PATH)
    assert db.get_artist(10)[2] == 105 and not db.artist_exists(20)
    assert pro.job.total == 1


def test_missing_name_keeps_known_name_instead_of_temp(cfg, no_sleep):
    api = _api_with_artists()
    pro, _ = make_processor(api)
    pro.sync()
    api.following['public'] = [(10, ''), (20, 'Bob')]
    api.errors[('user_detail', 10)] = err('Internal Server Error')
    pro.sync()
    db = Database.local(cfg.DB_PATH)
    assert db.get_artist_full(10)['author_name'] == 'Alice' and db.get_artist_full(10)['is_temp_name'] == 0


def test_sync_without_accounts_raises_and_marks_job_error(cfg, no_sleep, monkeypatch):
    import pytest
    monkeypatch.setattr(cfg, 'TOKENS', {})
    pro, _ = make_processor()
    pro.clients = {}
    with pytest.raises(RuntimeError):
        pro.sync()
    assert pro.job.status == 'error'

import json
import os

import pytest

from pixiv_dl.config import Config, mask_token
from pixiv_dl.database import Database
from fakes import FakeAPI, make_illust, make_processor
from pixiv_dl.progress import JobState


def _task(db, aid, iid, status, media='image', ext='png', page=0):
    db.upsert_artist(aid, f'A{aid}')
    db.save_illust({'task_key': f'{iid}_{page}', 'illust_id': iid, 'page_index': page, 'author_id': aid, 'title': 't',
                    'url': f'https://old.invalid/{iid}_p{page}.{ext}', 'media_type': media})
    db.mark_status(f'{iid}_{page}', status)


def _file(cfg, folder, name, size=300):
    d = os.path.join(cfg.LOCAL_SAVE_PATH, folder)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, name), 'wb') as f:
        f.write(b'x' * size)


def test_verify_preview_then_apply(cfg, no_sleep):
    pro, _ = make_processor()
    db = Database.local(cfg.DB_PATH)
    _task(db, 1, 10, 1)            # 已下载，文件在      -> 正常
    _task(db, 1, 11, 1)            # 已下载，文件缺失    -> 重置
    _task(db, 1, 12, 0)            # 未下载，但文件在    -> 找回
    _task(db, 1, 13, 1)            # 已下载，文件损坏(<100B) -> 重置
    _task(db, 2, 20, 1)            # 画师目录不存在      -> 重置
    _task(db, 1, 30, 1, media='ugoira')
    _task(db, 1, 40, 1, media='novel')
    for n, size in (('10_p0.png', 300), ('12_p0.png', 300), ('13_p0.png', 5)):
        _file(cfg, '[1] A1', n, size)
    _file(cfg, '[1] A1/动图zip', '30_p0.zip')
    _file(cfg, '[1] A1', '40_p0.txt', 3)           # 小说很短也算完好
    stats = pro.verify_storage(apply=False)
    assert (stats['missing'], stats['restored'], stats['ok']) == (3, 1, 3)
    assert db.conn.execute("SELECT status FROM illusts WHERE task_key='11_0'").fetchone() == (1,)   # 预览不改库
    stats = pro.verify_storage(apply=True)
    st = dict(db.conn.execute("SELECT task_key, status FROM illusts").fetchall())
    assert st['11_0'] == 0 and st['13_0'] == 0 and st['20_0'] == 0 and st['12_0'] == 1
    assert st['10_0'] == 1 and st['30_0'] == 1 and st['40_0'] == 1


def test_verify_never_treats_storage_errors_as_missing(cfg, no_sleep, monkeypatch):
    pro, _ = make_processor()
    db = Database.local(cfg.DB_PATH)
    _task(db, 1, 10, 1)
    _file(cfg, '[1] A1', '10_p0.png')
    storage = pro.storage
    monkeypatch.setattr(storage, 'list_dir', lambda rel='': None if rel else {'[1] A1': (True, 0)})
    stats = pro.verify_storage(apply=True)
    assert stats['skipped_artists'] == 1 and stats['missing'] == 0
    assert db.conn.execute("SELECT status FROM illusts").fetchone() == (1,)


def test_verify_aborts_if_storage_unavailable(cfg, no_sleep, monkeypatch):
    pro, _ = make_processor()
    db = Database.local(cfg.DB_PATH)
    _task(db, 1, 10, 1)
    monkeypatch.setattr(pro.storage, '_list_base_dirs', lambda force=False: (_ for _ in ()).throw(ConnectionError('NAS down')))
    with pytest.raises(ConnectionError):
        pro.verify_storage(apply=True)
    assert db.conn.execute("SELECT status FROM illusts").fetchone() == (1,)
    assert pro.job.status == 'error'


def test_preview_and_exports(cfg, tmp_path):
    pro, _ = make_processor()
    db = Database.local(cfg.DB_PATH)
    _task(db, 1, 10, 0)
    _task(db, 1, 11, -1)
    prev = pro.preview_pending(limit=5)            # 回归：以前这里会 ValueError(解包 10 列到 8 个变量)
    assert {p['task_key'] for p in prev} == {'10_0', '11_0'}
    out = pro.export_preview_csv(str(tmp_path / 'p.csv'))
    assert os.path.getsize(out) > 0
    assert os.path.getsize(pro.export_failed_tasks(str(tmp_path / 'f.csv'))) > 0
    assert pro.retry_failed_tasks() == 1


def test_clean_temp_and_reclaim(cfg):
    pro, _ = make_processor()
    os.makedirs(os.path.join(cfg.LOCAL_TEMP_PATH, 'sub'))
    old = os.path.join(cfg.LOCAL_TEMP_PATH, 'sub', 'old.tmp')
    open(old, 'w').write('x')
    os.utime(old, (1, 1))
    assert pro.clean_temp(older_than_days=1) == 1
    assert not os.path.exists(os.path.join(cfg.LOCAL_TEMP_PATH, 'sub'))
    assert pro.reclaim_stuck_tasks() == {'reclaimed': 0, 'permanent_failed': 0}


def test_safe_filename():
    from pixiv_dl.processor import Processor
    assert Processor._safe('a/b:c*?') == 'a_b_c__'
    assert Processor._safe('name. ') == 'name'
    assert Processor._safe('CON') == '_CON'
    assert Processor._safe('') == '_'
    assert Processor._safe('[tag] 名前') == '[tag] 名前'


def test_add_illust(cfg, no_sleep):
    api = FakeAPI()
    ill = make_illust(321, pages=2)
    ill['user'] = {'id': 77, 'name': 'Zed'}
    api.details[321] = ill
    pro, _ = make_processor(api)
    assert pro.add_illust(321) == 2
    db = Database.local(cfg.DB_PATH)
    assert db.get_artist(77)[1] == 'Zed'
    assert db.conn.execute("SELECT COUNT(*) FROM illusts WHERE illust_id=321").fetchone()[0] == 2
    with pytest.raises(RuntimeError):
        pro.add_illust(999)


# ------------------------------------------------------------------ 配置 / 安全
def test_settings_roundtrip_types_and_atomic(cfg, tmp_path):
    cfg.DELAY_DOWNLOAD = (0.5, 1.5)
    cfg.RATE_LIMIT_RULES = {1000: 10, 100: 5}
    cfg.TOKENS = {'a': {'token': 'secret-token', 'is_valid': True}}
    assert cfg.save_settings()
    data = json.load(open(cfg.SETTINGS_FILE, encoding='utf-8'))
    assert 'current' in data
    cfg.DELAY_DOWNLOAD = (9, 9)
    cfg.load_settings()
    assert cfg.DELAY_DOWNLOAD == (0.5, 1.5) and isinstance(cfg.DELAY_DOWNLOAD, tuple)
    assert not [f for f in os.listdir(tmp_path) if f.endswith('.tmp')]


def test_settings_load_ignores_unknown_keys_and_applies_env(cfg, monkeypatch):
    json.dump({'current': {'NOT_A_SETTING': 1, 'NAS_IP': '1.2.3.4'}, 'presets': {'p': {'NAS_IP': 'x'}}},
              open(cfg.SETTINGS_FILE, 'w', encoding='utf-8'))
    monkeypatch.setenv('PIXIV_NAS_PASS', 'from-env')
    cfg.load_settings()
    assert cfg.NAS_IP == '1.2.3.4' and cfg.NAS_PASS == 'from-env' and not hasattr(cfg, 'NOT_A_SETTING')
    cfg.save_settings()
    assert json.load(open(cfg.SETTINGS_FILE, encoding='utf-8'))['presets'] == {'p': {'NAS_IP': 'x'}}   # 预设保留


def test_public_view_has_no_secrets(cfg):
    cfg.NAS_PASS = 'hunter2'
    cfg.TOKENS = {'a': {'token': 'secret-token', 'is_valid': True}}
    blob = json.dumps(cfg.public_view())
    assert 'hunter2' not in blob and 'secret-token' not in blob
    assert cfg.public_view()['NAS_PASS_SET'] is True
    assert mask_token('abcdefghijkl') == 'abcd…kl' and mask_token('') == ''


def test_no_hardcoded_credentials_in_source():
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    for name in ('pixiv_dl/config.py', 'pixiv_dl/cli.py', 'pixiv_dl/processor.py', 'pixiv_dl/storage/adapter.py', 'pixiv_dl/token_manager.py'):
        src = open(os.path.join(root, *name.split('/')), encoding='utf-8').read()
        assert 'verify=False' not in src
        assert 'NAS_PASS = "' not in src or 'NAS_PASS = ""' in src


def test_get_accounts_legacy_single_token(cfg, monkeypatch):
    monkeypatch.setattr(cfg, 'TOKENS', {})
    monkeypatch.setattr(cfg, 'REFRESH_TOKEN', 'legacy')
    assert cfg.get_accounts() == {'default': {'token': 'legacy', 'is_valid': True}}
    assert cfg.get_main_token() == 'legacy'


# ------------------------------------------------------------------ 任务状态
def test_job_op_nesting_and_cancel_resets_interrupt(cfg, no_sleep):
    from pixiv_dl import interrupt
    pro, api = make_processor()
    api.following['public'] = []
    # 嵌套调用（同步 + 下载）只产生一个任务
    pro.sync_and_download()
    assert pro.job.kind == 'sync_download' and pro.job.status == 'done'
    # KeyboardInterrupt 被转成 cancelled，且不会遗留中断标志
    def boom(*a, **k):
        raise KeyboardInterrupt
    pro.ensure_clients = boom
    pro.sync()
    assert pro.job.status == 'cancelled' and not interrupt.is_set()


def test_job_snapshot_is_json_serialisable():
    j = JobState('download', {'limit': 5})
    j.add(done=2, success=1, failed=1)
    j.set_current('main', 'x')
    j.log('hello')
    snap = j.snapshot()
    json.dumps(snap)
    assert snap['done'] == 2 and snap['current'] == {'main': 'x'} and snap['logs'][0]['msg'] == 'hello'

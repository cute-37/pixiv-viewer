"""用内存假 SMB 连接验证 SMB 模式的存储逻辑（没有真实 NAS 可用，这里只验证代码路径）。"""
import io

import pytest
from smb.smb_structs import OperationFailure

import pixiv_dl.storage.backends as storage_mod
from pixiv_dl.storage.adapter import StorageAdapter


class FakeShared:
    def __init__(self, name, is_dir, size=0):
        self.filename, self.isDirectory, self.file_size = name, is_dir, size


class FakeSMB:
    instances = []
    fail_store_once = False

    def __init__(self, *a, **k):
        self.fs = FakeSMB.shared_fs
        FakeSMB.instances.append(self)
        self.closed = False

    shared_fs = {}

    connect_ok = True
    known_shares = None      # None = 任意共享名都存在
    share_list = ['V6', 'photos', 'IPC$']

    def connect(self, ip, port, timeout=None):
        return FakeSMB.connect_ok

    def listShares(self):
        class D:
            def __init__(self, name):
                self.name, self.isSpecial, self.type = name, name.endswith('$'), 0
        return [D(n) for n in FakeSMB.share_list]

    def close(self):
        self.closed = True

    @staticmethod
    def _p(path):
        return '/' + path.strip('/')

    def _missing(self):
        return OperationFailure('missing', [])

    def listPath(self, share, path, **kw):
        if FakeSMB.known_shares is not None and share not in FakeSMB.known_shares:
            raise OperationFailure('Unable to connect to shared device', [])
        path = self._p(path)
        if path != '/' and self.fs.get(path) != 'dir':
            raise self._missing()
        prefix = path.rstrip('/') + '/'
        out = [FakeShared('.', True), FakeShared('..', True)]
        for k, v in self.fs.items():
            if k.startswith(prefix) and '/' not in k[len(prefix):]:
                out.append(FakeShared(k[len(prefix):], v == 'dir', 0 if v == 'dir' else len(v)))
        return out

    def getAttributes(self, share, path):
        path = self._p(path)
        v = self.fs.get(path)
        if v is None:
            raise self._missing()
        return FakeShared(path, v == 'dir', 0 if v == 'dir' else len(v))

    def createDirectory(self, share, path):
        path = self._p(path)
        if path in self.fs:
            raise self._missing()
        self.fs[path] = 'dir'

    def storeFile(self, share, path, fobj):
        if FakeSMB.fail_store_once:
            FakeSMB.fail_store_once = False
            raise ConnectionResetError('connection reset by peer')
        self.fs[self._p(path)] = fobj.read()

    def rename(self, share, old, new):
        old, new = self._p(old), self._p(new)
        if old not in self.fs or new in self.fs:
            raise self._missing()
        if self.fs[old] == 'dir':
            for k in [k for k in self.fs if k == old or k.startswith(old + '/')]:
                self.fs[new + k[len(old):]] = self.fs.pop(k)
        else:
            self.fs[new] = self.fs.pop(old)

    def deleteFiles(self, share, path):
        if self._p(path) not in self.fs:
            raise self._missing()
        del self.fs[self._p(path)]

    def retrieveFile(self, share, path, fobj):
        v = self.fs.get(self._p(path))
        if v is None or v == 'dir':
            raise self._missing()
        fobj.write(v)


@pytest.fixture
def smb(cfg, monkeypatch, no_sleep):
    FakeSMB.shared_fs = {}
    FakeSMB.instances = []
    FakeSMB.fail_store_once = False
    FakeSMB.connect_ok = True
    FakeSMB.known_shares = None
    monkeypatch.setattr(storage_mod, 'SMBConnection', FakeSMB)
    monkeypatch.setattr(cfg, 'STORAGE_MODE', 'smb')
    monkeypatch.setattr(cfg, 'NAS_BASE_PATH', 'PIXIV')
    monkeypatch.setattr(cfg, 'NAS_SHARE', 'share')
    return StorageAdapter(), FakeSMB.shared_fs


def test_smb_put_is_atomic_and_leaves_no_part_file(smb):
    sa, fs = smb
    assert fs['/PIXIV'] == 'dir'                       # 基础目录自动创建
    sa.put_bytes('[1] A/f.bin', b'x' * 300)
    assert fs['/PIXIV/[1] A/f.bin'] == b'x' * 300
    assert not [k for k in fs if k.endswith('.part')]
    assert sa.exists('[1] A/f.bin') and sa.get_file_size('[1] A/f.bin') == 300
    assert not sa.exists('[1] A/missing.bin') and sa.get_file_size('[1] A/missing.bin') == 0
    assert sa.read('[1] A/f.bin') == b'x' * 300
    assert sa.list_dir('[1] A') == {'f.bin': (False, 300)}
    assert sa.list_dir('nope') is None


def test_smb_reconnects_after_connection_error(smb):
    sa, fs = smb
    n_before = len(FakeSMB.instances)
    FakeSMB.fail_store_once = True
    sa.put_bytes('a/b.bin', b'y' * 200)
    assert fs['/PIXIV/a/b.bin'] == b'y' * 200
    assert len(FakeSMB.instances) > n_before           # 发生过重连


def test_smb_existing_target_is_not_clobbered_and_part_cleaned(smb):
    sa, fs = smb
    sa.put_bytes('a/b.bin', b'1' * 200)
    sa.put_bytes('a/b.bin', b'2' * 200)                # 目标已存在：视为已完成，不覆盖
    assert fs['/PIXIV/a/b.bin'] == b'1' * 200
    assert not [k for k in fs if k.endswith('.part')]


def test_smb_put_file_and_folder_rename(smb, tmp_path):
    sa, fs = smb
    f = tmp_path / 'x.webp'
    f.write_bytes(b'w' * 250)
    sa.put_file('[7] Old/7_p0.webp', str(f))
    sa._folders.clear()
    sa._base_dirs = None
    assert sa.get_artist_folder(7, 'Unknown', rename=False) == '[7] Old'
    sa._folders.clear()
    sa._base_dirs = None
    assert sa.get_artist_folder(7, 'New') == '[7] New'
    assert '/PIXIV/[7] New/7_p0.webp' in fs and '/PIXIV/[7] Old/7_p0.webp' not in fs
    assert sa.find_artist_folder(7) == '[7] New'


def test_smb_download_end_to_end(cfg, smb, monkeypatch):
    """SMB 模式下完整走一遍下载流程（假 API + 假 NAS）。"""
    from pixiv_dl.database import Database
    from fakes import FakeAPI, make_illust, make_processor
    sa, fs = smb
    api = FakeAPI()
    api.details[100] = make_illust(100, pages=2)
    data = b'\x89PNG' + b'q' * 300
    routes = {f'https://new.example/img/100_p{i}.png': data for i in range(2)}
    pro, _ = make_processor(api, routes)
    db = Database.local(cfg.DB_PATH)
    db.upsert_artist(1, 'Alice')
    for i in range(2):
        db.save_illust({'task_key': f'100_{i}', 'illust_id': 100, 'page_index': i, 'author_id': 1, 'title': 't',
                        'url': 'https://old.invalid/x.jpg', 'media_type': 'image'})
    pro.download()
    assert fs['/PIXIV/[1] Alice/100_p0.png'] == data and fs['/PIXIV/[1] Alice/100_p1.png'] == data
    assert db.conn.execute("SELECT COUNT(*) FROM illusts WHERE status=1").fetchone()[0] == 2


def test_smb_nested_base_path_with_backslashes_is_created(cfg, monkeypatch, no_sleep):
    """配置里的保存文件夹常是 Windows 风格的反斜杠、多层且可能还不存在。"""
    BS = chr(92)
    FakeSMB.shared_fs = {}
    FakeSMB.known_shares = None
    monkeypatch.setattr(storage_mod, 'SMBConnection', FakeSMB)
    monkeypatch.setattr(cfg, 'STORAGE_MODE', 'smb')
    monkeypatch.setattr(cfg, 'NAS_SHARE', 'share')
    monkeypatch.setattr(cfg, 'NAS_BASE_PATH', BS.join(['01 媒体库', '插画', 'PIXIV']))
    sa = StorageAdapter()
    assert {'/01 媒体库', '/01 媒体库/插画', '/01 媒体库/插画/PIXIV'} <= set(FakeSMB.shared_fs)
    assert not [k for k in FakeSMB.shared_fs if BS in k]
    sa.put_bytes('[1] A/f.bin', b'x' * 200)
    assert FakeSMB.shared_fs['/01 媒体库/插画/PIXIV/[1] A/f.bin'] == b'x' * 200
    assert sa.ping()[0] is True

"""每种存储协议都必须通过同一组契约测试；每种协议的「测试连接」都要能正确给出分步结果。"""
import os
import socket
import types

import pytest

import pixiv_dl.storage.diagnose as storagetest
import pixiv_dl.storage.backends as storage_backends
from fake_protocols import DavServer, FakeFTP, FakeSFTPClient, install_fake_ftp, install_fake_paramiko, install_fake_s3
from pixiv_dl.storage.adapter import StorageAdapter

DATA = b'\x89PNG' + b'd' * 300
BS = chr(92)


@pytest.fixture(params=['local', 'webdav', 'ftp', 'ftps', 'sftp', 's3'])
def adapter(request, cfg, monkeypatch, tmp_path):
    """各协议各给一个已配置好的 StorageAdapter（根目录一律叫 PIXIV）。"""
    mode = request.param
    monkeypatch.setattr(cfg, 'STORAGE_MODE', {'ftps': 'ftp'}.get(mode, mode))
    if mode == 'local':
        monkeypatch.setattr(cfg, 'LOCAL_SAVE_PATH', str(tmp_path / 'PIXIV'))
    elif mode == 'webdav':
        srv = DavServer(user='u', password='p')
        request.addfinalizer(srv.stop)
        monkeypatch.setattr(cfg, 'WEBDAV_URL', srv.url + '/PIXIV')
        monkeypatch.setattr(cfg, 'WEBDAV_USER', 'u')
        monkeypatch.setattr(cfg, 'WEBDAV_PASS', 'p')
    elif mode in ('ftp', 'ftps'):
        install_fake_ftp(monkeypatch)
        monkeypatch.setattr(cfg, 'FTP_URL', ('ftps' if mode == 'ftps' else 'ftp') + '://127.0.0.1:2121/files/PIXIV')
        monkeypatch.setattr(cfg, 'FTP_USER', 'u')
        monkeypatch.setattr(cfg, 'FTP_PASS', 'p')
    elif mode == 'sftp':
        install_fake_paramiko(monkeypatch)
        monkeypatch.setattr(cfg, 'SFTP_URL', 'sftp://127.0.0.1:2222/data/PIXIV')
        monkeypatch.setattr(cfg, 'SFTP_USER', 'u')
        monkeypatch.setattr(cfg, 'SFTP_PASS', 'p')
    elif mode == 's3':
        install_fake_s3(monkeypatch)
        for k, v in (('S3_BUCKET', 'media'), ('S3_PREFIX', 'PIXIV'), ('S3_ACCESS_KEY', 'AK'), ('S3_SECRET_KEY', 'SK')):
            monkeypatch.setattr(cfg, k, v)
    sa = StorageAdapter()
    request.addfinalizer(sa.close)
    return sa


class TestContract:
    def test_put_exists_size_read(self, adapter):
        assert not adapter.exists('[1] A/f.png')
        assert adapter.put_bytes('[1] A/f.png', DATA) == len(DATA)
        assert adapter.exists('[1] A/f.png') and adapter.get_file_size('[1] A/f.png') == len(DATA)
        assert adapter.read('[1] A/f.png') == DATA
        assert adapter.get_file_size('[1] A/nope.png') == 0 and adapter.get_file_size('[1] A') == 0   # 目录不算文件

    def test_atomic_no_part_leftovers_and_min_size(self, adapter):
        with pytest.raises(ValueError):
            adapter.put_bytes('[1] A/tiny.bin', b'x' * 10)
        assert not adapter.exists('[1] A/tiny.bin')
        adapter.put_bytes('[1] A/f.png', DATA)
        listing = adapter.list_dir('[1] A')
        assert listing == {'f.png': (False, len(DATA))}                       # 没有 .part / .tmp 残留

    def test_list_dir_and_missing(self, adapter):
        adapter.put_bytes('[1] A/动图zip/z.zip', DATA)
        adapter.put_bytes('[1] A/f.png', DATA)
        for i in range(5):                                                                 # 超过一页，测试分页
            adapter.put_bytes(f'[1] A/extra{i}.png', DATA)
        assert len(adapter.list_dir('[1] A')) == 7
        root = adapter.list_dir('')
        assert root['[1] A'][0] is True
        sub = adapter.list_dir('[1] A')
        assert sub['f.png'] == (False, len(DATA)) and sub['动图zip'][0] is True and sub['extra4.png'][0] is False
        assert adapter.list_dir('不存在的目录') is None

    def test_put_file_and_non_ascii_names(self, adapter, tmp_path):
        f = tmp_path / 'src.webp'
        f.write_bytes(b'w' * 250)
        adapter.put_file('[7] 名前 テスト/7_p0.webp', str(f))
        assert adapter.get_file_size('[7] 名前 テスト/7_p0.webp') == 250
        assert adapter.read('[7] 名前 テスト/7_p0.webp') == b'w' * 250

    def test_existing_target_is_kept(self, adapter):
        adapter.put_bytes('a/b.png', DATA)
        adapter.put_bytes('a/b.png', DATA)                                     # 重复写入：视为已完成
        assert adapter.read('a/b.png') == DATA and list(adapter.list_dir('a')) == ['b.png']

    def test_artist_folder_policy(self, adapter, cfg):
        assert adapter.get_artist_folder(1, 'Old') == '[1] Old'
        adapter.put_bytes('[1] Old/f.png', DATA)
        adapter._folders.clear()
        adapter._base_dirs = None
        assert adapter.get_artist_folder(1, 'Unknown', rename=False) == '[1] Old'      # 下载阶段绝不重命名
        assert adapter.exists('[1] Old/f.png')
        adapter._folders.clear()
        adapter._base_dirs = None
        if cfg.STORAGE_MODE == 's3':                                                     # 对象存储不能重命名目录：沿用原目录，不丢文件
            assert adapter.get_artist_folder(1, 'New') == '[1] Old' and adapter.exists('[1] Old/f.png')
            assert adapter.find_artist_folder(1) == '[1] Old'
            return
        assert adapter.get_artist_folder(1, 'New') == '[1] New'                          # 同步阶段才重命名
        assert adapter.exists('[1] New/f.png') and not adapter.exists('[1] Old/f.png')
        assert adapter.find_artist_folder(1) == '[1] New' and adapter.find_artist_folder(2) is None

    def test_ping_and_describe_hide_password(self, adapter):
        ok, msg = adapter.ping()
        assert ok, msg
        assert 'p@' not in adapter.describe() and ':p@' not in msg


def test_download_end_to_end_on_every_protocol(adapter, cfg, no_sleep):
    """每种协议都完整跑一遍下载流程（假 API）。"""
    from pixiv_dl.database import Database
    from fakes import FakeAPI, make_illust, make_processor
    api = FakeAPI()
    api.details[100] = make_illust(100, pages=2)
    pro, _ = make_processor(api, {f'https://new.example/img/100_p{i}.png': DATA for i in range(2)})
    pro._storage = adapter
    db = Database.local(cfg.DB_PATH)
    db.upsert_artist(1, 'Alice')
    for i in range(2):
        db.save_illust({'task_key': f'100_{i}', 'illust_id': 100, 'page_index': i, 'author_id': 1, 'title': 't',
                        'url': 'https://old.invalid/x.jpg', 'media_type': 'image'})
    pro.download()
    assert db.conn.execute("SELECT COUNT(*) FROM illusts WHERE status=1").fetchone()[0] == 2
    assert adapter.read('[1] Alice/100_p0.png') == DATA and adapter.read('[1] Alice/100_p1.png') == DATA


# ------------------------------------------------------------------ WebDAV 细节
class TestWebDAV:
    def test_server_without_move_falls_back(self, cfg, monkeypatch):
        srv = DavServer(supports_move=False)
        try:
            monkeypatch.setattr(cfg, 'STORAGE_MODE', 'webdav')
            monkeypatch.setattr(cfg, 'WEBDAV_URL', srv.url + '/PIXIV')
            sa = StorageAdapter()
            sa.put_bytes('a/b.png', DATA)
            assert srv.fs['/dav/PIXIV/a/b.png'] == DATA and not [k for k in srv.fs if k.endswith('.part')]
        finally:
            srv.stop()

    def test_url_schemes_and_encoding(self):
        b = storage_backends.WebDAVBackend('webdavs://nas.local:5006/dav/我的 图片', 'u', 'p', True)
        assert b.base.startswith('https://nas.local:5006/dav/')
        assert b._url('[1] A/x y.png').endswith('/%5B1%5D%20A/x%20y.png')
        assert storage_backends.WebDAVBackend('dav://h/x').base == 'http://h/x'


# ------------------------------------------------------------------ FTP 细节
class TestFTP:
    def test_servers_without_mlsd_use_list(self, cfg, monkeypatch):
        install_fake_ftp(monkeypatch)
        FakeFTP.mlsd_supported = False
        monkeypatch.setattr(cfg, 'STORAGE_MODE', 'ftp')
        monkeypatch.setattr(cfg, 'FTP_URL', 'ftp://h/PIXIV')
        monkeypatch.setattr(cfg, 'FTP_USER', 'u')
        monkeypatch.setattr(cfg, 'FTP_PASS', 'p')
        sa = StorageAdapter()
        sa.put_bytes('[1] A/f.png', DATA)
        assert sa.list_dir('[1] A') == {'f.png': (False, len(DATA))}
        assert sa.list_dir('') == {'[1] A': (True, 0)}

    def test_parse_list_line_formats(self):
        p = storage_backends.parse_list_line
        assert p('drwxr-xr-x    2 ftp      ftp          4096 Jan 01 12:00 my dir') == ('my dir', True, 0)
        assert p('-rw-r--r--    1 ftp      ftp          1234 Jan 01  2025 a b.png') == ('a b.png', False, 1234)
        assert p('01-02-25  10:11AM       <DIR>          Folder') == ('Folder', True, 0)
        assert p('01-02-25  10:11PM                 4321 file.jpg') == ('file.jpg', False, 4321)
        assert p('total 12') is None

    def test_reconnect_after_connection_error(self, cfg, monkeypatch, no_sleep):
        install_fake_ftp(monkeypatch)
        monkeypatch.setattr(cfg, 'STORAGE_MODE', 'ftp')
        monkeypatch.setattr(cfg, 'FTP_URL', 'ftp://h/PIXIV')
        monkeypatch.setattr(cfg, 'FTP_USER', 'u')
        monkeypatch.setattr(cfg, 'FTP_PASS', 'p')
        sa = StorageAdapter()
        n = len(FakeFTP.instances)
        orig = FakeFTP.storbinary
        state = {'fail': True}

        def flaky(self, cmd, f):
            if state.pop('fail', False):
                raise ConnectionResetError('connection reset')
            return orig(self, cmd, f)
        monkeypatch.setattr(FakeFTP, 'storbinary', flaky)
        sa.put_bytes('a/b.png', DATA)
        assert len(FakeFTP.instances) > n and sa.read('a/b.png') == DATA


# ------------------------------------------------------------------ SFTP 细节
class TestSFTP:
    def test_home_relative_path(self, cfg, monkeypatch):
        install_fake_paramiko(monkeypatch)
        monkeypatch.setattr(cfg, 'STORAGE_MODE', 'sftp')
        monkeypatch.setattr(cfg, 'SFTP_URL', 'sftp://h/~/PIXIV')
        monkeypatch.setattr(cfg, 'SFTP_USER', 'u')
        monkeypatch.setattr(cfg, 'SFTP_PASS', 'p')
        sa = StorageAdapter()
        sa.put_bytes('a/b.png', DATA)
        assert FakeSFTPClient.fs['/home/u/PIXIV/a/b.png'] == DATA

    def test_key_file_login_uses_password_as_passphrase(self, cfg, monkeypatch, tmp_path):
        mod = install_fake_paramiko(monkeypatch)
        key = tmp_path / 'id'
        key.write_text('KEY')
        monkeypatch.setattr(cfg, 'STORAGE_MODE', 'sftp')
        monkeypatch.setattr(cfg, 'SFTP_URL', 'sftp://h/data/PIXIV')
        monkeypatch.setattr(cfg, 'SFTP_USER', 'u')
        monkeypatch.setattr(cfg, 'SFTP_PASS', 'secret-passphrase')
        monkeypatch.setattr(cfg, 'SFTP_KEY_FILE', str(key))
        StorageAdapter()
        kw = mod.SSHClient.connects[-1]
        assert kw['key_filename'] == str(key) and kw['passphrase'] == 'secret-passphrase' and 'password' not in kw

    def test_missing_paramiko_is_explained(self, cfg, monkeypatch):
        import sys
        monkeypatch.setitem(sys.modules, 'paramiko', None)       # 模拟没安装
        monkeypatch.setattr(cfg, 'STORAGE_MODE', 'sftp')
        monkeypatch.setattr(cfg, 'SFTP_URL', 'sftp://h/x')
        with pytest.raises(RuntimeError, match='paramiko'):
            StorageAdapter()


def test_parse_remote_url():
    p = storage_backends.parse_remote_url
    assert p('ftp://h/x', 21, ('ftp', 'ftps')) == {'scheme': 'ftp', 'host': 'h', 'port': 21, 'path': '/x'}
    assert p('ftps://h:990/a/b c', 21, ('ftp', 'ftps'))['port'] == 990 and p('ftps://h:990/a/b%20c', 21, ('ftp', 'ftps'))['path'] == '/a/b c'
    assert p('nas.local/data', 22, ('sftp', 'ssh'), 'sftp')['host'] == 'nas.local'
    for bad in ('', 'http://h/x', 'ftp://', 'ftp://h:abc/x'):
        with pytest.raises(storage_backends.BackendError):
            p(bad, 21, ('ftp', 'ftps'))


def test_unknown_mode_is_rejected(cfg, monkeypatch):
    monkeypatch.setattr(cfg, 'STORAGE_MODE', 'carrier-pigeon')
    with pytest.raises(ValueError):
        StorageAdapter()


# ====================================================================== 测试连接（每个协议）
class TestDiagnose:
    def names(self, r):
        return [s['name'] for s in r['steps']]

    # ---- 本地
    def test_local(self, tmp_path):
        r = storagetest.diagnose('local', {'LOCAL_SAVE_PATH': str(tmp_path / 'new' / 'dir')})
        assert r['ok'] and self.names(r) == ['本地目录'] and not os.path.exists(tmp_path / 'new' / 'dir' / storagetest.PROBE)
        assert not storagetest.diagnose('local', {'LOCAL_SAVE_PATH': ' '})['ok']
        f = tmp_path / 'afile'
        f.write_text('x')
        assert not storagetest.diagnose('local', {'LOCAL_SAVE_PATH': str(f / 'sub')})['ok']      # 路径被一个文件占用

    # ---- WebDAV
    def test_webdav_ok_with_missing_folder(self, cfg):
        srv = DavServer(user='u', password='p')
        try:
            r = storagetest.diagnose('webdav', {'WEBDAV_URL': srv.url + '/PIXIV/new', 'WEBDAV_USER': 'u', 'WEBDAV_PASS': 'p'})
            assert r['ok'] and self.names(r) == ['解析地址', '连接服务器', '认证', '保存目录', '写入权限']
            assert '还不存在' in r['steps'][3]['detail'] and '上一级' in r['steps'][4]['detail']
            assert not [k for k in srv.fs if storagetest.PROBE in k]                  # 测试文件已清理
        finally:
            srv.stop()

    def test_webdav_wrong_password_uses_saved_password_when_blank(self, cfg, monkeypatch):
        srv = DavServer(user='u', password='p')
        try:
            r = storagetest.diagnose('webdav', {'WEBDAV_URL': srv.url, 'WEBDAV_USER': 'u', 'WEBDAV_PASS': 'wrong'})
            assert not r['ok'] and r['steps'][-1]['name'] == '认证' and '用户名或密码' in r['message']
            monkeypatch.setattr(cfg, 'WEBDAV_PASS', 'p')                                # 表单留空 → 用已保存的
            assert storagetest.diagnose('webdav', {'WEBDAV_URL': srv.url, 'WEBDAV_USER': 'u'})['ok']
        finally:
            srv.stop()

    def test_webdav_not_a_dav_endpoint_and_unreachable_and_bad_url(self, cfg):
        assert not storagetest.diagnose('webdav', {'WEBDAV_URL': ''})['ok']
        s = socket.socket()
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]
        s.close()                                                                      # 关闭的端口
        r = storagetest.diagnose('webdav', {'WEBDAV_URL': f'http://127.0.0.1:{port}/dav'})
        assert not r['ok'] and r['steps'][-1]['name'] == '连接服务器' and '拒绝' in r['message']

    # ---- FTP
    def test_ftp_ok_and_ftps_label(self, cfg, monkeypatch):
        install_fake_ftp(monkeypatch)
        r = storagetest.diagnose('ftp', {'FTP_URL': 'ftp://h:21/files/PIXIV', 'FTP_USER': 'u', 'FTP_PASS': 'p'})
        assert r['ok'] and self.names(r) == ['解析地址', '连接服务器', '登录', '保存目录', '写入权限'] and '不加密' in r['steps'][0]['detail']
        assert not [k for k in FakeFTP.fs if storagetest.PROBE in k]
        FakeFTP.fs.update({'/files': 'dir', '/files/PIXIV': 'dir'})
        r = storagetest.diagnose('ftp', {'FTP_URL': 'ftps://h/files/PIXIV', 'FTP_USER': 'u', 'FTP_PASS': 'p'})
        assert r['ok'] and 'TLS' in r['steps'][0]['detail'] and r['steps'][3]['detail'] == '已存在'

    def test_ftp_failures_are_specific(self, cfg, monkeypatch):
        install_fake_ftp(monkeypatch)
        r = storagetest.diagnose('ftp', {'FTP_URL': 'ftp://h/x', 'FTP_USER': 'u', 'FTP_PASS': 'bad'})
        assert not r['ok'] and r['steps'][-1]['name'] == '登录' and '用户名或密码' in r['message']
        FakeFTP.refuse = True
        r = storagetest.diagnose('ftp', {'FTP_URL': 'ftp://h/x', 'FTP_USER': 'u', 'FTP_PASS': 'p'})
        assert r['steps'][-1]['name'] == '连接服务器' and '拒绝' in r['message']
        FakeFTP.refuse = False
        FakeFTP.tls_ok = False
        r = storagetest.diagnose('ftp', {'FTP_URL': 'ftps://h/x', 'FTP_USER': 'u', 'FTP_PASS': 'p'})
        assert not r['ok'] and 'ftp://' in r['message']
        assert not storagetest.diagnose('ftp', {'FTP_URL': 'http://h/x'})['ok']
        assert not storagetest.diagnose('ftp', {'FTP_URL': ''})['ok']

    # ---- SFTP
    def test_sftp_ok_password_and_key(self, cfg, monkeypatch, tmp_path):
        install_fake_paramiko(monkeypatch)
        monkeypatch.setattr(storagetest.socket, 'create_connection', lambda *a, **k: socket.socket())
        r = storagetest.diagnose('sftp', {'SFTP_URL': 'sftp://h:22/data/PIXIV', 'SFTP_USER': 'u', 'SFTP_PASS': 'p'})
        assert r['ok'] and self.names(r) == ['解析地址', '连接服务器', '登录', '保存目录', '写入权限'] and '密码' in r['steps'][2]['detail']
        assert not [k for k in FakeSFTPClient.fs if storagetest.PROBE in k]
        key = tmp_path / 'id_rsa'
        key.write_text('KEY')
        r = storagetest.diagnose('sftp', {'SFTP_URL': 'sftp://h/~/PIXIV', 'SFTP_USER': 'u', 'SFTP_KEY_FILE': str(key)})
        assert r['ok'] and '密钥' in r['steps'][2]['detail']

    def test_sftp_failures_are_specific(self, cfg, monkeypatch, tmp_path):
        install_fake_paramiko(monkeypatch)
        monkeypatch.setattr(storagetest.socket, 'create_connection', lambda *a, **k: socket.socket())
        r = storagetest.diagnose('sftp', {'SFTP_URL': 'sftp://h/x', 'SFTP_USER': 'u', 'SFTP_PASS': 'bad'})
        assert not r['ok'] and r['steps'][-1]['name'] == '登录' and '用户名或密码' in r['message']
        r = storagetest.diagnose('sftp', {'SFTP_URL': 'sftp://h/x', 'SFTP_USER': 'u', 'SFTP_KEY_FILE': str(tmp_path / 'missing')})
        assert '找不到私钥' in r['message']
        monkeypatch.setattr(storagetest.socket, 'create_connection', lambda *a, **k: (_ for _ in ()).throw(TimeoutError('timed out')))
        assert '超时' in storagetest.diagnose('sftp', {'SFTP_URL': 'sftp://h/x', 'SFTP_USER': 'u', 'SFTP_PASS': 'p'})['message']
        monkeypatch.setitem(__import__('sys').modules, 'paramiko', None)
        r = storagetest.diagnose('sftp', {'SFTP_URL': 'sftp://h/x'})
        assert not r['ok'] and 'pip install paramiko' in r['message']

    def test_unknown_mode(self):
        assert not storagetest.diagnose('floppy', {})['ok']


# ====================================================================== 设置 / 接口
def test_secrets_of_every_protocol_are_hidden_and_urls_sanitised(cfg, monkeypatch):
    for k in ('NAS_PASS', 'WEBDAV_PASS', 'FTP_PASS', 'SFTP_PASS'):
        monkeypatch.setattr(cfg, k, 'TOPSECRET-' + k)
    monkeypatch.setattr(cfg, 'FTP_URL', 'ftp://bob:hunter2@host:2121/x')
    monkeypatch.setattr(cfg, 'STORAGE_MODE', 'ftp')
    import json
    view = json.dumps(cfg.public_view())
    assert 'TOPSECRET' not in view and 'hunter2' not in view                # 地址里误带的密码也不会出现在页面数据里
    assert all(cfg.public_view()[k + '_SET'] for k in ('NAS_PASS', 'WEBDAV_PASS', 'FTP_PASS', 'SFTP_PASS'))
    assert 'hunter2' not in cfg.storage_target() and cfg.storage_target() == 'ftp://host:2121/x'


def test_web_settings_accept_every_protocol_and_keep_passwords(cfg, no_sleep):
    import http.client
    import json
    import threading
    from fakes import make_processor
    from pixiv_dl.web import server as web
    pro, _ = make_processor()
    httpd = web.make_server('127.0.0.1', 0, pro)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]

    def post(path, body):
        c = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
        c.request('POST', path, body=json.dumps(body), headers={'Host': f'127.0.0.1:{port}', 'X-Pixiv-UI': '1', 'Content-Type': 'application/json'})
        r = c.getresponse()
        d = json.loads(r.read())
        c.close()
        return r.status, d
    try:
        for mode in ('local', 'smb', 'webdav', 'ftp', 'sftp'):
            assert post('/api/settings', {'STORAGE_MODE': mode})[0] == 200
        assert post('/api/settings', {'STORAGE_MODE': 'carrier-pigeon'})[0] == 400
        s, r = post('/api/settings', {'WEBDAV_URL': 'https://h/dav', 'WEBDAV_USER': 'u', 'WEBDAV_PASS': 'dav-secret', 'WEBDAV_VERIFY_TLS': False,
                                      'FTP_URL': 'ftp://h/x', 'FTP_PASS': 'ftp-secret', 'SFTP_URL': 'sftp://h/x', 'SFTP_KEY_FILE': '~/.ssh/id'})
        assert s == 200 and 'WEBDAV_PASS' not in r['applied'] and cfg.WEBDAV_VERIFY_TLS is False
        assert post('/api/settings', {'WEBDAV_PASS': '', 'FTP_PASS': '', 'SFTP_PASS': ''})[0] == 200
        assert (cfg.WEBDAV_PASS, cfg.FTP_PASS) == ('dav-secret', 'ftp-secret')          # 留空 = 不修改
        assert post('/api/settings', {'WEBDAV_VERIFY_TLS': 'no'})[0] == 400
        srv = DavServer(user='u', password='p')
        try:
            s, r = post('/api/settings/test-storage', {'STORAGE_MODE': 'webdav', 'WEBDAV_URL': srv.url, 'WEBDAV_USER': 'u', 'WEBDAV_PASS': 'p'})
            assert s == 200 and r['ok'] and len(r['steps']) == 5
        finally:
            srv.stop()
        install = __import__('pytest').MonkeyPatch()
        try:
            install_fake_ftp(install)
            s, r = post('/api/settings/test-storage', {'STORAGE_MODE': 'ftp', 'FTP_URL': 'ftp://h/x', 'FTP_USER': 'u', 'FTP_PASS': 'p'})
            assert r['ok'] and 'p' not in [x for x in json.dumps(r).split('"') if x == 'p-secret']
        finally:
            install.undo()
        assert json.load(open(cfg.SETTINGS_FILE, encoding='utf-8'))['current']['WEBDAV_URL'] == 'https://h/dav'
    finally:
        httpd.shutdown()
        httpd.server_close()

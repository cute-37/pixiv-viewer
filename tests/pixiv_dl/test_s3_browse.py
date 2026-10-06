"""S3 / 对象存储（连接测试、错误翻译、真实 boto3 客户端的参数）以及各协议的「浏览…」。"""
import http.client
import json
import socket
import threading

import pytest

import pixiv_dl.storage.browse as storagebrowse
import pixiv_dl.storage.diagnose as storagetest
from fake_protocols import DavServer, FakeFTP, FakeSFTPClient, install_fake_ftp, install_fake_paramiko, install_fake_s3
from pixiv_dl.storage.adapter import StorageAdapter
from pixiv_dl.storage.backends import BackendError, S3Backend, s3_error_text

DATA = b'\x89PNG' + b'd' * 300


def s3_values(**over):
    v = {'S3_ENDPOINT': '', 'S3_REGION': 'us-east-1', 'S3_BUCKET': 'media', 'S3_PREFIX': 'PIXIV/art',
         'S3_ACCESS_KEY': 'AK', 'S3_SECRET_KEY': 'SK', 'S3_PATH_STYLE': False, 'S3_VERIFY_TLS': True}
    v.update(over)
    return v


# ====================================================================== S3 本身
class TestS3Backend:
    def test_virtual_directories_and_missing(self, cfg, monkeypatch):
        fake = install_fake_s3(monkeypatch)
        b = S3Backend('', 'us-east-1', 'media', 'PIXIV', 'AK', 'SK')
        assert b.stat('') == (True, 0) and b.stat('nope') is None and b.listdir('nope') is None and b.listdir('') == {}
        b.write('[1] A/f.png', lambda: __import__('io').BytesIO(DATA), len(DATA))
        assert fake.store['media']['PIXIV/[1] A/f.png'] == DATA                       # 键 = 前缀/相对路径
        assert b.stat('[1] A') == (True, 0) and b.stat('[1] A/f.png') == (False, len(DATA))
        assert b.listdir('') == {'[1] A': (True, 0)}

    def test_prefix_is_isolated_from_other_data_in_the_bucket(self, cfg, monkeypatch):
        fake = install_fake_s3(monkeypatch)
        fake.store['media'].update({'other/x.bin': b'x' * 10, 'PIXIV2/y.bin': b'y' * 10})
        b = S3Backend('', 'us-east-1', 'media', 'PIXIV', 'AK', 'SK')
        assert b.listdir('') == {}                                                     # 不会看到前缀之外的东西
        b.write('a/b.png', lambda: __import__('io').BytesIO(DATA), len(DATA))
        assert set(fake.store['media']) == {'other/x.bin', 'PIXIV2/y.bin', 'PIXIV/a/b.png'}

    def test_bucket_root_when_prefix_empty(self, cfg, monkeypatch):
        fake = install_fake_s3(monkeypatch)
        b = S3Backend('', '', 'media', '', 'AK', 'SK')
        b.write('[1] A/f.png', lambda: __import__('io').BytesIO(DATA), len(DATA))
        assert '[1] A/f.png' in fake.store['media'] and list(b.listdir('')) == ['[1] A']

    def test_rename_is_refused_with_explanation(self, cfg, monkeypatch):
        install_fake_s3(monkeypatch)
        b = S3Backend('', '', 'media', 'PIXIV', 'AK', 'SK')
        with pytest.raises(BackendError, match='不支持重命名'):
            b.rename('a', 'b')

    def test_bucket_required(self):
        with pytest.raises(BackendError):
            S3Backend('', '', ' ', '')

    def test_real_boto3_client_gets_the_right_settings(self):
        """不联网：只验证传给真实 boto3 的参数（端点 / 路径风格 / 区域）。"""
        pytest.importorskip('boto3')
        c = S3Backend._make_client('https://minio.local:9000', 'cn-north-1', 'AK', 'SK', True, False)
        assert c.meta.endpoint_url == 'https://minio.local:9000' and c.meta.region_name == 'cn-north-1'
        assert c.meta.config.s3['addressing_style'] == 'path'
        aws = S3Backend._make_client('', 'eu-west-1', 'AK', 'SK', False, True)
        assert 'amazonaws.com' in aws.meta.endpoint_url and aws.meta.config.s3['addressing_style'] == 'auto'

    def test_error_translation(self):
        from fake_protocols import _client_error
        assert '密钥' in s3_error_text(_client_error('SignatureDoesNotMatch', 403, 'x'))
        assert '密钥' in s3_error_text(_client_error('InvalidAccessKeyId', 403, 'x'))
        assert '「b」' in s3_error_text(_client_error('NoSuchBucket', 404, 'x'), 'b')
        assert '没有权限' in s3_error_text(_client_error('AccessDenied', 403, 'x'))
        assert '证书' in s3_error_text(Exception('SSLError: certificate verify failed'))


# ====================================================================== S3 测试连接
class TestS3Diagnose:
    names = staticmethod(lambda r: [s['name'] for s in r['steps']])

    def test_ok(self, cfg, monkeypatch):
        fake = install_fake_s3(monkeypatch)
        r = storagetest.diagnose('s3', s3_values())
        assert r['ok'] and self.names(r) == ['解析配置', '存储桶', '保存位置', '写入权限']
        assert '还没有文件' in r['steps'][2]['detail'] and not [k for k in fake.store['media'] if storagetest.PROBE in k]
        fake.store['media']['PIXIV/art/x.png'] = b'x' * 100
        assert '已有文件' in storagetest.diagnose('s3', s3_values())['steps'][2]['detail']

    def test_custom_endpoint_adds_connect_step(self, cfg, monkeypatch):
        install_fake_s3(monkeypatch)
        monkeypatch.setattr(storagetest.socket, 'create_connection', lambda *a, **k: socket.socket())
        r = storagetest.diagnose('s3', s3_values(S3_ENDPOINT='https://minio.local:9000', S3_PATH_STYLE=True))
        assert r['ok'] and '连接服务端点' in self.names(r)

    def test_wrong_secret_wrong_bucket_denied_write(self, cfg, monkeypatch):
        install_fake_s3(monkeypatch)
        r = storagetest.diagnose('s3', s3_values(S3_SECRET_KEY='wrong'))
        assert not r['ok'] and r['steps'][-1]['name'] == '存储桶' and '密钥' in r['message']
        r = storagetest.diagnose('s3', s3_values(S3_BUCKET='nosuch'))
        assert '「nosuch」' in r['message']
        install_fake_s3(monkeypatch, deny_write=True)
        r = storagetest.diagnose('s3', s3_values())
        assert not r['ok'] and r['steps'][-1]['name'] == '写入权限' and '没有权限' in r['message']

    def test_list_denied_is_reported_but_distinct(self, cfg, monkeypatch):
        install_fake_s3(monkeypatch, deny_list=True)
        r = storagetest.diagnose('s3', s3_values())
        step = next(s for s in r['steps'] if s['name'] == '保存位置')
        assert not step['ok'] and '列出' in step['detail']

    def test_input_validation(self, cfg):
        assert '存储桶' in storagetest.diagnose('s3', s3_values(S3_BUCKET=''))['message']
        assert 'https://' in storagetest.diagnose('s3', s3_values(S3_ENDPOINT='minio.local:9000'))['message']
        assert 'Secret' in storagetest.diagnose('s3', s3_values(S3_SECRET_KEY=''))['message']

    def test_saved_secret_used_when_blank_and_unreachable_endpoint(self, cfg, monkeypatch):
        install_fake_s3(monkeypatch)
        monkeypatch.setattr(cfg, 'S3_SECRET_KEY', 'SK')
        assert storagetest.diagnose('s3', s3_values(S3_SECRET_KEY=''))['ok']
        monkeypatch.setattr(storagetest.socket, 'create_connection', lambda *a, **k: (_ for _ in ()).throw(ConnectionRefusedError('refused')))
        r = storagetest.diagnose('s3', s3_values(S3_ENDPOINT='https://down.local'))
        assert r['steps'][-1]['name'] == '连接服务端点' and '拒绝' in r['message']

    def test_missing_boto3_is_explained(self, cfg, monkeypatch):
        import sys
        monkeypatch.setitem(sys.modules, 'boto3', None)
        r = storagetest.diagnose('s3', s3_values())
        assert not r['ok'] and 'pip install boto3' in r['message']


# ====================================================================== 浏览…
class TestBrowse:
    def test_webdav(self, cfg):
        srv = DavServer(user='u', password='p')
        try:
            srv.fs.update({'/dav/Media': 'dir', '/dav/Media/Art': 'dir', '/dav/Media/Art/PIXIV': 'dir', '/dav/Media/@eaDir': 'dir',
                           '/dav/Media/readme.txt': b'x', '/dav/Photos': 'dir'})
            v = {'WEBDAV_URL': srv.url + '/Media/Art', 'WEBDAV_USER': 'u', 'WEBDAV_PASS': 'p'}
            r = storagebrowse.browse('webdav', v, 'dav/Media')
            assert r['ok'] and r['kind'] == 'folders' and r['entries'] == ['Art'] and r['path'] == ['dav', 'Media']   # 隐藏 @eaDir、文件
            assert storagebrowse.browse('webdav', v, '')['entries'] == ['dav']                                     # 从服务器根开始
            r = storagebrowse.browse('webdav', v, 'dav/Media/Gone/Deeper')                                         # 路径不存在 → 退到最近的上级
            assert r['ok'] and r['path'] == ['dav', 'Media']
            bad = storagebrowse.browse('webdav', {**v, 'WEBDAV_PASS': 'wrong'}, '')
            assert not bad['ok'] and '用户名或密码' in bad['error']
        finally:
            srv.stop()

    def test_ftp(self, cfg, monkeypatch):
        install_fake_ftp(monkeypatch)
        FakeFTP.fs.update({'/files': 'dir', '/files/PIXIV': 'dir', '/files/other': 'dir', '/files/a.txt': b'x', '/.hidden': 'dir'})
        v = {'FTP_URL': 'ftp://h/files/PIXIV', 'FTP_USER': 'u', 'FTP_PASS': 'p'}
        r = storagebrowse.browse('ftp', v, 'files')
        assert r['ok'] and r['entries'] == ['other', 'PIXIV'] and r['path'] == ['files']
        assert storagebrowse.browse('ftp', v, '')['entries'] == ['files']
        assert storagebrowse.browse('ftp', v, 'files/nope/x')['path'] == ['files']
        FakeFTP.mlsd_supported = False                                                       # vsftpd 之类不支持 MLSD
        assert storagebrowse.browse('ftp', v, 'files')['entries'] == ['other', 'PIXIV']
        r = storagebrowse.browse('ftp', {**v, 'FTP_PASS': 'bad'}, '')
        assert not r['ok'] and '用户名或密码' in r['error']
        assert not storagebrowse.browse('ftp', {'FTP_URL': 'http://h'}, '')['ok']

    def test_sftp(self, cfg, monkeypatch):
        install_fake_paramiko(monkeypatch)
        FakeSFTPClient.fs.update({'/data': 'dir', '/data/PIXIV': 'dir', '/data/movies': 'dir', '/data/x.bin': b'x'})
        v = {'SFTP_URL': 'sftp://h/data/PIXIV', 'SFTP_USER': 'u', 'SFTP_PASS': 'p'}
        r = storagebrowse.browse('sftp', v, 'data')
        assert r['ok'] and r['entries'] == ['movies', 'PIXIV']
        assert 'data' in storagebrowse.browse('sftp', v, '')['entries']
        assert storagebrowse.browse('sftp', v, 'data/missing')['path'] == ['data']
        r = storagebrowse.browse('sftp', {**v, 'SFTP_PASS': 'bad'}, '')
        assert not r['ok'] and '用户名' in r['error']

    def test_s3_buckets_then_prefixes(self, cfg, monkeypatch):
        fake = install_fake_s3(monkeypatch, buckets=('media', 'backup'))
        fake.store['media'].update({'PIXIV/a/1.png': b'x' * 100, 'PIXIV/b/2.png': b'x' * 100, 'other/3.png': b'x' * 100, 'top.txt': b'x'})
        r = storagebrowse.browse('s3', s3_values(S3_BUCKET=''), '')
        assert r['ok'] and r['kind'] == 'buckets' and r['entries'] == ['backup', 'media']
        r = storagebrowse.browse('s3', s3_values(), '')
        assert r['kind'] == 'folders' and r['entries'] == ['other', 'PIXIV']
        r = storagebrowse.browse('s3', s3_values(), 'PIXIV')
        assert r['entries'] == ['a', 'b']
        assert storagebrowse.browse('s3', s3_values(), 'PIXIV/zzz')['path'] == ['PIXIV']
        r = storagebrowse.browse('s3', s3_values(S3_SECRET_KEY='wrong'), '')
        assert not r['ok'] and '密钥' in r['error']

    def test_s3_cannot_list_buckets_hint(self, cfg, monkeypatch):
        install_fake_s3(monkeypatch, deny_list=True)
        r = storagebrowse.browse('s3', s3_values(S3_BUCKET=''), '')
        assert not r['ok'] and '直接填写存储桶名称' in r['error']


# ====================================================================== Web 接口
def test_web_browse_endpoint_and_s3_settings(cfg, monkeypatch, no_sleep):
    from fakes import make_processor
    from pixiv_dl.web import server as web
    fake = install_fake_s3(monkeypatch, buckets=('media',))
    fake.store['media']['PIXIV/a/1.png'] = b'x' * 100
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
        s, r = post('/api/storage/browse', {'mode': 's3', **s3_values(S3_BUCKET='')})
        assert s == 200 and r['kind'] == 'buckets' and r['entries'] == ['media']
        s, r = post('/api/storage/browse', {'mode': 's3', **s3_values(), 'path': 'PIXIV'})
        assert r['entries'] == ['a']
        assert post('/api/storage/browse', {'mode': 'local'})[0] == 400
        assert post('/api/storage/browse', {'mode': 'floppy'})[0] == 400
        s, r = post('/api/settings/test-storage', {'STORAGE_MODE': 's3', **s3_values()})
        assert s == 200 and r['ok'] and len(r['steps']) == 4
        s, r = post('/api/settings', {'STORAGE_MODE': 's3', 'S3_BUCKET': 'media', 'S3_PREFIX': 'PIXIV', 'S3_ACCESS_KEY': 'AK',
                                      'S3_SECRET_KEY': 'top-secret-sk', 'S3_PATH_STYLE': True, 'S3_ENDPOINT': 'https://minio.local:9000'})
        assert s == 200 and 'S3_SECRET_KEY' not in r['applied'] and cfg.S3_PATH_STYLE is True
        assert post('/api/settings', {'S3_SECRET_KEY': ''})[0] == 200 and cfg.S3_SECRET_KEY == 'top-secret-sk'
        assert post('/api/settings', {'S3_PATH_STYLE': 'yes'})[0] == 400
        view = json.dumps(cfg.public_view())
        assert 'top-secret-sk' not in view and cfg.public_view()['S3_SECRET_KEY_SET'] is True
        assert cfg.storage_target() == 's3://media/PIXIV @ https://minio.local:9000'
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_s3_download_end_to_end(cfg, monkeypatch, no_sleep):
    from pixiv_dl.database import Database
    from fakes import FakeAPI, make_illust, make_processor
    fake = install_fake_s3(monkeypatch)
    for k, v in (('STORAGE_MODE', 's3'), ('S3_BUCKET', 'media'), ('S3_PREFIX', 'PIXIV'), ('S3_ACCESS_KEY', 'AK'), ('S3_SECRET_KEY', 'SK')):
        monkeypatch.setattr(cfg, k, v)
    api = FakeAPI()
    api.details[100] = make_illust(100, pages=2)
    pro, _ = make_processor(api, {f'https://new.example/img/100_p{i}.png': DATA for i in range(2)})
    db = Database.local(cfg.DB_PATH)
    db.upsert_artist(1, 'Alice')
    for i in range(2):
        db.save_illust({'task_key': f'100_{i}', 'illust_id': 100, 'page_index': i, 'author_id': 1, 'title': 't', 'url': 'u', 'media_type': 'image'})
    pro.download()
    assert sorted(fake.store['media']) == ['PIXIV/[1] Alice/100_p0.png', 'PIXIV/[1] Alice/100_p1.png']
    # 核查：按画师目录整体列出，已存在的文件不会被判成缺失
    stats = pro.verify_storage()
    assert stats['missing'] == 0 and stats['ok'] == 2

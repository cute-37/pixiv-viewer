"""设置页「浏览…」：WebDAV / FTP / SFTP / S3 的文件夹浏览（SMB 在 smbtools.browse）。

用表单里（可能还没保存）的值连接，列出某个路径下的子文件夹；路径不存在时自动退回到最近的上级目录。
返回 {ok, kind: 'folders'|'buckets', entries: [名称], path: [实际使用的路径段], error}。
"""
import ftplib

from pixiv_dl.config import Config
from pixiv_dl.storage.backends import (BackendError, FTPBackend, S3Backend, SFTPBackend, WebDAVBackend, parse_remote_url,
                              s3_error_text, split_rel)
from pixiv_dl.storage.diagnose import _net_error, _pw

_HIDDEN = ('@', '#', '.')


def _visible(names):
    return sorted((n for n in names if not n.startswith(_HIDDEN)), key=str.lower)


def _friendly(mode, e, bucket=''):
    if isinstance(e, BackendError):
        return str(e)
    if isinstance(e, ftplib.error_perm) and str(e).startswith('530'):
        return '用户名或密码不正确'
    name = e.__class__.__name__
    if mode == 's3':
        return s3_error_text(e, bucket)
    if name == 'AuthenticationException':
        return '用户名/密码或私钥不正确'
    if isinstance(e, RuntimeError):
        return str(e)
    return _net_error(e)


def open_backend(mode, v):
    """用表单值连接到「服务器根」（浏览时从根开始导航）。"""
    if mode == 'webdav':
        url = (v.get('WEBDAV_URL') or '').strip()
        origin = WebDAVBackend(url).origin
        return WebDAVBackend(origin, v.get('WEBDAV_USER') or '', _pw(v, 'WEBDAV_PASS'), v.get('WEBDAV_VERIFY_TLS', True))
    if mode == 'ftp':
        u = parse_remote_url(v.get('FTP_URL'), 21, ('ftp', 'ftps'), 'ftp')
        return FTPBackend(u['host'], u['port'], v.get('FTP_USER') or '', _pw(v, 'FTP_PASS'), u['scheme'] == 'ftps', '/')
    if mode == 'sftp':
        u = parse_remote_url(v.get('SFTP_URL'), 22, ('sftp', 'ssh'), 'sftp')
        return SFTPBackend(u['host'], u['port'], v.get('SFTP_USER') or '', _pw(v, 'SFTP_PASS'),
                           (v.get('SFTP_KEY_FILE') or '').strip(), '/')
    raise BackendError(f'这种存储方式不支持浏览: {mode}')


def browse(mode, v, path=''):
    parts = split_rel(path)
    if mode == 's3':
        return _browse_s3(v, parts)
    try:
        be = open_backend(mode, v)
    except Exception as e:
        return {'ok': False, 'error': _friendly(mode, e), 'entries': [], 'path': parts}
    try:
        while True:
            listing = be.listdir('/'.join(parts))
            if listing is not None:
                break
            if not parts:
                listing = {}
                break
            parts.pop()                               # 路径不存在：退到最近的上级
        return {'ok': True, 'kind': 'folders', 'entries': _visible(n for n, (d, _) in listing.items() if d), 'path': parts}
    except Exception as e:
        return {'ok': False, 'error': _friendly(mode, e), 'entries': [], 'path': parts}
    finally:
        try:
            be.close()
        except Exception:
            pass


def _browse_s3(v, parts):
    bucket = (v.get('S3_BUCKET') or '').strip()
    kw = dict(endpoint=v.get('S3_ENDPOINT') or '', region=v.get('S3_REGION') or '', access_key=v.get('S3_ACCESS_KEY') or '',
              secret_key=_pw(v, 'S3_SECRET_KEY'), path_style=bool(v.get('S3_PATH_STYLE')), verify=v.get('S3_VERIFY_TLS', True))
    try:
        if not bucket:
            client = S3Backend._make_client(kw['endpoint'], kw['region'] or 'us-east-1', kw['access_key'], kw['secret_key'],
                                            kw['path_style'], kw['verify'])
            names = [b['Name'] for b in client.list_buckets().get('Buckets', [])]
            return {'ok': True, 'kind': 'buckets', 'entries': sorted(names, key=str.lower), 'path': []}
        be = S3Backend(prefix='', bucket=bucket, **kw)
        try:
            while True:
                listing = be.listdir('/'.join(parts))
                if listing is not None:
                    break
                if not parts:
                    listing = {}
                    break
                parts.pop()
            return {'ok': True, 'kind': 'folders', 'entries': _visible(n for n, (d, _) in listing.items() if d), 'path': parts}
        finally:
            be.close()
    except Exception as e:
        hint = '。该密钥可能没有「列出存储桶」的权限，可以直接填写存储桶名称' if not bucket else ''
        return {'ok': False, 'error': _friendly('s3', e, bucket) + hint, 'entries': [], 'path': parts}

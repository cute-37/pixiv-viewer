"""测试用的各协议假服务：真正跑在本机的最小 WebDAV 服务、内存版 ftplib.FTP、内存版 paramiko。"""
import ftplib
import io
import posixpath
import stat as stat_mod
import sys
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, unquote, urlparse

import pytest


# ====================================================================== WebDAV
class DavServer:
    """最小 WebDAV：PROPFIND / MKCOL / PUT / GET / DELETE / MOVE。fs: 路径 -> bytes 或 'dir'。"""

    def __init__(self, user=None, password=None, supports_move=True, ssl_error=False):
        self.fs = {'/': 'dir', '/dav': 'dir'}
        self.user, self.password, self.supports_move = user, password, supports_move
        self.requests = []
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *a):
                pass

            def _auth(self):
                if outer.user is None:
                    return True
                import base64
                want = 'Basic ' + base64.b64encode(f'{outer.user}:{outer.password}'.encode()).decode()
                if self.headers.get('Authorization') == want:
                    return True
                self._send(401, b'', {'WWW-Authenticate': 'Basic realm="x"'})
                return False

            def _send(self, code, body=b'', headers=None):
                self.send_response(code)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                if self.command != 'HEAD':
                    self.wfile.write(body)

            def _path(self):
                return unquote(urlparse(self.path).path).rstrip('/') or '/'

            def _entry(self, p):
                v = outer.fs[p]
                href = quote(p + ('/' if v == 'dir' and p != '/' else ''))
                if v == 'dir':
                    prop = '<d:resourcetype><d:collection/></d:resourcetype>'
                else:
                    prop = f'<d:resourcetype/><d:getcontentlength>{len(v)}</d:getcontentlength>'
                return (f'<d:response><d:href>{href}</d:href><d:propstat><d:prop>{prop}</d:prop>'
                        f'<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>')

            def _body(self):
                n = int(self.headers.get('Content-Length') or 0)
                return self.rfile.read(n) if n else b''

            def do_PROPFIND(self):
                self._body()
                if not self._auth():
                    return
                outer.requests.append(('PROPFIND', self._path()))
                p = self._path()
                if p not in outer.fs:
                    return self._send(404)
                items = [p]
                if self.headers.get('Depth') == '1':
                    prefix = p.rstrip('/') + '/'
                    items += [k for k in outer.fs if k.startswith(prefix) and '/' not in k[len(prefix):] and k != p]
                xml = '<?xml version="1.0"?><d:multistatus xmlns:d="DAV:">' + ''.join(self._entry(i) for i in items) + '</d:multistatus>'
                self._send(207, xml.encode(), {'Content-Type': 'application/xml'})

            def do_MKCOL(self):
                self._body()
                if not self._auth():
                    return
                p = self._path()
                if p in outer.fs:
                    return self._send(405)
                if posixpath.dirname(p) not in outer.fs:
                    return self._send(409)
                outer.fs[p] = 'dir'
                self._send(201)

            def do_PUT(self):
                data = self._body()
                if not self._auth():
                    return
                p = self._path()
                if posixpath.dirname(p) not in outer.fs:
                    return self._send(409)
                outer.fs[p] = data
                self._send(201)

            def do_GET(self):
                if not self._auth():
                    return
                v = outer.fs.get(self._path())
                if v is None or v == 'dir':
                    return self._send(404)
                self._send(200, v)

            def do_DELETE(self):
                if not self._auth():
                    return
                p = self._path()
                for k in [k for k in outer.fs if k == p or k.startswith(p + '/')]:
                    del outer.fs[k]
                self._send(204)

            def do_MOVE(self):
                self._body()
                if not self._auth():
                    return
                if not outer.supports_move:
                    return self._send(405)
                src = self._path()
                dst = unquote(urlparse(self.headers['Destination']).path).rstrip('/')
                if src not in outer.fs:
                    return self._send(404)
                if dst in outer.fs and self.headers.get('Overwrite') == 'F':
                    return self._send(412)
                for k in [k for k in outer.fs if k == src or k.startswith(src + '/')]:
                    outer.fs[dst + k[len(src):]] = outer.fs.pop(k)
                self._send(201)

        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), H)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def url(self):
        return f'http://127.0.0.1:{self.port}/dav'

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


# ====================================================================== FTP
class FakeFTP:
    """内存版 ftplib.FTP / FTP_TLS（只实现程序用到的那部分）。"""
    fs = {'/': 'dir'}
    users = {'u': 'p'}
    mlsd_supported = True
    refuse = False
    tls_ok = True
    instances = []

    def __init__(self, *a, **k):
        self.encoding = 'utf-8'
        self.cwd_ = '/'
        self.logged = False
        self.prot = False
        FakeFTP.instances.append(self)

    def connect(self, host, port=21, timeout=None):
        if FakeFTP.refuse:
            raise ConnectionRefusedError('refused')

    def login(self, user='', passwd=''):
        if FakeFTP.users.get(user) != passwd and not (user == 'anonymous' and 'anonymous' in FakeFTP.users):
            raise ftplib.error_perm('530 Login incorrect.')
        self.logged = True

    def prot_p(self):
        if not FakeFTP.tls_ok:
            raise ConnectionResetError('tls')
        self.prot = True

    def set_pasv(self, b):
        pass

    def voidcmd(self, cmd):
        return '200 ok'

    def _abs(self, path):
        p = path if path.startswith('/') else posixpath.join(self.cwd_, path)
        return posixpath.normpath(p)

    def pwd(self):
        return self.cwd_

    def cwd(self, path):
        p = self._abs(path)
        if FakeFTP.fs.get(p) != 'dir':
            raise ftplib.error_perm('550 No such directory')
        self.cwd_ = p

    def size(self, path):
        v = FakeFTP.fs.get(self._abs(path))
        if v is None or v == 'dir':
            raise ftplib.error_perm('550 Not a file')
        return len(v)

    def _children(self, p):
        prefix = p.rstrip('/') + '/'
        return {k[len(prefix):]: v for k, v in FakeFTP.fs.items() if k.startswith(prefix) and '/' not in k[len(prefix):] and k != p}

    def mlsd(self, path='', facts=()):
        if not FakeFTP.mlsd_supported:
            raise ftplib.error_perm('500 Unknown command')
        p = self._abs(path)
        if FakeFTP.fs.get(p) != 'dir':
            raise ftplib.error_perm('550 No such directory')
        out = [('.', {'type': 'cdir'}), ('..', {'type': 'pdir'})]
        for name, v in self._children(p).items():
            out.append((name, {'type': 'dir'} if v == 'dir' else {'type': 'file', 'size': str(len(v))}))
        return out

    def retrlines(self, cmd, callback):
        p = self._abs(cmd.split(' ', 1)[1])
        if FakeFTP.fs.get(p) != 'dir':
            raise ftplib.error_perm('550 No such directory')
        for name, v in self._children(p).items():
            if v == 'dir':
                callback(f'drwxr-xr-x    2 ftp      ftp          4096 Jan 01 12:00 {name}')
            else:
                callback(f'-rw-r--r--    1 ftp      ftp      {len(v):>10} Jan 01 12:00 {name}')

    def retrbinary(self, cmd, callback):
        v = FakeFTP.fs.get(self._abs(cmd.split(' ', 1)[1]))
        if v is None or v == 'dir':
            raise ftplib.error_perm('550 No such file')
        callback(v)

    def storbinary(self, cmd, fobj):
        p = self._abs(cmd.split(' ', 1)[1])
        if FakeFTP.fs.get(posixpath.dirname(p)) != 'dir':
            raise ftplib.error_perm('550 No such directory')
        FakeFTP.fs[p] = fobj.read()

    def rename(self, a, b):
        a, b = self._abs(a), self._abs(b)
        if a not in FakeFTP.fs:
            raise ftplib.error_perm('550 No such file')
        if b in FakeFTP.fs:
            raise ftplib.error_perm('550 Target exists')
        for k in [k for k in FakeFTP.fs if k == a or k.startswith(a + '/')]:
            FakeFTP.fs[b + k[len(a):]] = FakeFTP.fs.pop(k)

    def mkd(self, path):
        p = self._abs(path)
        if FakeFTP.fs.get(posixpath.dirname(p)) != 'dir' or p in FakeFTP.fs:
            raise ftplib.error_perm('550 cannot create')
        FakeFTP.fs[p] = 'dir'

    def delete(self, path):
        p = self._abs(path)
        if p not in FakeFTP.fs or FakeFTP.fs[p] == 'dir':
            raise ftplib.error_perm('550 No such file')
        del FakeFTP.fs[p]

    def quit(self):
        pass

    def close(self):
        pass


def install_fake_ftp(monkeypatch):
    FakeFTP.fs = {'/': 'dir'}
    FakeFTP.users = {'u': 'p'}
    FakeFTP.mlsd_supported = True
    FakeFTP.refuse = False
    FakeFTP.tls_ok = True
    FakeFTP.instances = []
    monkeypatch.setattr(ftplib, 'FTP', FakeFTP)
    monkeypatch.setattr(ftplib, 'FTP_TLS', FakeFTP)
    return FakeFTP


# ====================================================================== SFTP (paramiko)
class _Attr:
    def __init__(self, filename, is_dir, size):
        self.filename, self.st_size = filename, size
        self.st_mode = stat_mod.S_IFDIR | 0o755 if is_dir else stat_mod.S_IFREG | 0o644


class FakeSFTPClient:
    fs = {'/': 'dir', '/home': 'dir', '/home/u': 'dir'}
    home = '/home/u'

    def _abs(self, path):
        p = path if path.startswith('/') else posixpath.join(FakeSFTPClient.home, path)
        return posixpath.normpath(p)

    def stat(self, path):
        p = self._abs(path)
        v = FakeSFTPClient.fs.get(p)
        if v is None:
            raise FileNotFoundError(path)
        return _Attr(posixpath.basename(p), v == 'dir', 0 if v == 'dir' else len(v))

    def listdir_attr(self, path):
        p = self._abs(path)
        if FakeSFTPClient.fs.get(p) != 'dir':
            raise FileNotFoundError(path)
        prefix = p.rstrip('/') + '/'
        return [_Attr(k[len(prefix):], v == 'dir', 0 if v == 'dir' else len(v)) for k, v in FakeSFTPClient.fs.items()
                if k.startswith(prefix) and '/' not in k[len(prefix):] and k != p]

    def getfo(self, path, fobj):
        v = FakeSFTPClient.fs.get(self._abs(path))
        if v is None or v == 'dir':
            raise FileNotFoundError(path)
        fobj.write(v)

    def putfo(self, fobj, path):
        p = self._abs(path)
        if FakeSFTPClient.fs.get(posixpath.dirname(p)) != 'dir':
            raise FileNotFoundError(path)
        FakeSFTPClient.fs[p] = fobj.read()

    def mkdir(self, path):
        p = self._abs(path)
        if FakeSFTPClient.fs.get(posixpath.dirname(p)) != 'dir':
            raise FileNotFoundError(path)
        FakeSFTPClient.fs[p] = 'dir'

    def rename(self, a, b):
        a, b = self._abs(a), self._abs(b)
        if b in FakeSFTPClient.fs:
            raise OSError('target exists')
        for k in [k for k in FakeSFTPClient.fs if k == a or k.startswith(a + '/')]:
            FakeSFTPClient.fs[b + k[len(a):]] = FakeSFTPClient.fs.pop(k)

    def remove(self, path):
        FakeSFTPClient.fs.pop(self._abs(path), None)

    def close(self):
        pass


def install_fake_paramiko(monkeypatch, password='p', key_ok=True):
    FakeSFTPClient.fs = {'/': 'dir', '/home': 'dir', '/home/u': 'dir'}

    class SSHException(Exception):
        pass

    class AuthenticationException(SSHException):
        pass

    class AutoAddPolicy:
        pass

    class SSHClient:
        connects = []

        def set_missing_host_key_policy(self, p):
            pass

        def connect(self, **kw):
            SSHClient.connects.append(kw)
            if kw.get('key_filename'):
                if not key_ok:
                    raise AuthenticationException('bad key')
            elif kw.get('password') != password:
                raise AuthenticationException('Authentication failed.')

        def open_sftp(self):
            return FakeSFTPClient()

        def close(self):
            pass

    mod = types.ModuleType('paramiko')
    mod.SSHClient, mod.AutoAddPolicy = SSHClient, AutoAddPolicy
    mod.SSHException, mod.AuthenticationException = SSHException, AuthenticationException
    monkeypatch.setitem(sys.modules, 'paramiko', mod)
    return mod


# ====================================================================== S3
def _client_error(code, status, op):
    from botocore.exceptions import ClientError
    return ClientError({'Error': {'Code': code, 'Message': code}, 'ResponseMetadata': {'HTTPStatusCode': status}}, op)


class FakeS3:
    """内存版 S3 客户端（用真实的 botocore.ClientError 报错）。store: {桶: {键: bytes}}"""
    PAGE = 3                                  # 每页只返回 3 条，顺便测试分页

    def __init__(self, buckets=('media',), access_key='AK', secret_key='SK', deny_write=False, deny_list=False):
        self.store = {b: {} for b in buckets}
        self.access_key, self.secret_key = access_key, secret_key
        self.deny_write, self.deny_list = deny_write, deny_list
        self.calls = []
        self.creds = (access_key, secret_key)

    def _auth(self, op):
        self.calls.append(op)
        if self.creds != (self.access_key, self.secret_key):
            raise _client_error('SignatureDoesNotMatch', 403, op)

    def _bucket(self, name, op):
        if name not in self.store:
            raise _client_error('NoSuchBucket', 404, op)
        return self.store[name]

    def list_buckets(self):
        self._auth('ListBuckets')
        if self.deny_list:
            raise _client_error('AccessDenied', 403, 'ListBuckets')
        return {'Buckets': [{'Name': b} for b in self.store]}

    def head_bucket(self, Bucket):
        self._auth('HeadBucket')
        self._bucket(Bucket, 'HeadBucket')
        return {}

    def head_object(self, Bucket, Key):
        self._auth('HeadObject')
        v = self._bucket(Bucket, 'HeadObject').get(Key)
        if v is None:
            raise _client_error('404', 404, 'HeadObject')
        return {'ContentLength': len(v)}

    def list_objects_v2(self, Bucket, Prefix='', Delimiter=None, MaxKeys=1000, ContinuationToken=None):
        self._auth('ListObjectsV2')
        if self.deny_list:
            raise _client_error('AccessDenied', 403, 'ListObjectsV2')
        keys = sorted(k for k in self._bucket(Bucket, 'ListObjectsV2') if k.startswith(Prefix))
        files, prefixes = [], []
        for k in keys:
            rest = k[len(Prefix):]
            if Delimiter and Delimiter in rest:
                p = Prefix + rest.split(Delimiter)[0] + Delimiter
                if p not in prefixes:
                    prefixes.append(p)
            else:
                files.append(k)
        items = [('f', k) for k in files] + [('p', p) for p in prefixes]
        start = int(ContinuationToken or 0)
        limit = min(self.PAGE, MaxKeys or self.PAGE)
        page = items[start:start + limit]
        truncated = start + limit < len(items)
        r = {'Contents': [{'Key': k, 'Size': len(self.store[Bucket][k])} for t, k in page if t == 'f'],
             'CommonPrefixes': [{'Prefix': p} for t, p in page if t == 'p'], 'KeyCount': len(page), 'IsTruncated': truncated}
        if truncated:
            r['NextContinuationToken'] = str(start + limit)
        return r

    def get_object(self, Bucket, Key):
        self._auth('GetObject')
        v = self._bucket(Bucket, 'GetObject').get(Key)
        if v is None:
            raise _client_error('NoSuchKey', 404, 'GetObject')
        return {'Body': io.BytesIO(v)}

    def upload_fileobj(self, Fileobj, Bucket, Key):
        self._auth('PutObject')
        if self.deny_write:
            raise _client_error('AccessDenied', 403, 'PutObject')
        self._bucket(Bucket, 'PutObject')[Key] = Fileobj.read()

    def put_object(self, Bucket, Key, Body):
        self._auth('PutObject')
        if self.deny_write:
            raise _client_error('AccessDenied', 403, 'PutObject')
        self._bucket(Bucket, 'PutObject')[Key] = Body if isinstance(Body, bytes) else Body.read()

    def delete_object(self, Bucket, Key):
        self._auth('DeleteObject')
        self._bucket(Bucket, 'DeleteObject').pop(Key, None)

    def close(self):
        pass


def install_fake_s3(monkeypatch, **kw):
    """让 S3Backend 使用内存客户端（密钥不对会像真服务一样报 SignatureDoesNotMatch）。"""
    import pixiv_dl.storage.backends as storage_backends
    fake = FakeS3(**kw)

    def make(endpoint, region, access_key, secret_key, path_style, verify):
        fake.creds = (access_key, secret_key)
        fake.last_args = dict(endpoint=endpoint, region=region, path_style=path_style, verify=verify)
        return fake
    monkeypatch.setattr(storage_backends.S3Backend, '_make_client', staticmethod(make))
    return fake

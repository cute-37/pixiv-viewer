"""存储后端：本地 / SMB / WebDAV / FTP(FTPS) / SFTP。

所有后端使用同一组原语，路径都是相对「保存根目录」的相对路径（用 / 分隔）：
    stat(rel)      -> None | (是否目录, 大小)
    listdir(rel)   -> None | {名称: (是否目录, 大小)}     rel='' 表示根目录
    read(rel)      -> bytes
    mkdirs(rel)
    write(rel, opener, size)   原子写入：先写 <名>.part，成功后再重命名；opener() 返回一个新的二进制文件对象
    rename(old, new)
    ping()         -> (ok, 说明)
    describe()     -> 给人看的位置描述（不含密码）
上层（storage.StorageAdapter）负责画师目录缓存/重命名策略等与协议无关的逻辑。
"""
import io
import logging
import os
import re
import shutil
import threading
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

from pixiv_dl import interrupt
from pixiv_dl.config import Config

logger = logging.getLogger("PixivDownloader")

MODES = ('local', 'smb', 'webdav', 'ftp', 'sftp', 's3')
MODE_LABELS = {'local': '本地目录', 'smb': 'NAS (SMB)', 'webdav': 'WebDAV', 'ftp': 'FTP / FTPS', 'sftp': 'SFTP (SSH)',
               's3': '对象存储 (S3)'}

try:  # pysmb 只在 SMB 模式下需要
    from smb.SMBConnection import SMBConnection
    from smb.smb_structs import OperationFailure
except ImportError:  # pragma: no cover
    SMBConnection = None

    class OperationFailure(Exception):
        pass


class BackendError(Exception):
    """后端给出的、可以直接显示给用户的错误。"""


def split_rel(rel):
    return [p for p in str(rel or '').replace('\\', '/').split('/') if p]


def parse_remote_url(url, default_port, schemes, default_scheme=None):
    """解析 `协议://主机:端口/路径`，返回 dict(scheme, host, port, path)。不合法时抛 BackendError。"""
    text = (url or '').strip()
    if not text:
        raise BackendError("请填写服务器地址")
    if '://' not in text:
        text = f"{default_scheme or schemes[0]}://{text}"
    u = urlparse(text)
    if u.scheme.lower() not in schemes:
        raise BackendError(f"不支持的协议「{u.scheme}」，应为 {' / '.join(schemes)}")
    if not u.hostname:
        raise BackendError("地址里缺少主机名")
    try:
        port = u.port or default_port
    except ValueError:
        raise BackendError("端口不是有效的数字")
    return {'scheme': u.scheme.lower(), 'host': u.hostname, 'port': port, 'path': unquote(u.path or '')}


class Backend:
    name = ''

    def stat(self, rel):
        raise NotImplementedError

    def listdir(self, rel):
        raise NotImplementedError

    def read(self, rel):
        raise NotImplementedError

    def mkdirs(self, rel):
        raise NotImplementedError

    def write(self, rel, opener, size):
        raise NotImplementedError

    def rename(self, old, new):
        raise NotImplementedError

    def ping(self):
        raise NotImplementedError

    def describe(self):
        return self.name

    def close(self):
        pass


def _retry(backend_lock, connect, op, retries=2, label='操作', passthrough=()):
    """通用的"断线重连后重试"：passthrough 里的异常（如文件不存在）原样抛出。"""
    last = None
    for attempt in range(retries + 1):
        with backend_lock:
            try:
                return op()
            except passthrough:
                raise
            except InterruptedError:
                raise
            except Exception as e:
                last = e
                logger.warning(f"{label}失败（{attempt + 1}/{retries + 1}）: {e}")
                try:
                    connect()
                except Exception as ce:
                    last = ce
        if attempt < retries and interrupt.wait(1.0 * (attempt + 1)):
            raise InterruptedError("操作被中断")
    raise last


# ====================================================================== 本地
class LocalBackend(Backend):
    name = 'local'

    def __init__(self, root):
        self.root = Path(root)

    def _p(self, rel):
        return self.root / rel if rel else self.root

    def stat(self, rel):
        p = self._p(rel)
        if not p.exists():
            return None
        return (True, 0) if p.is_dir() else (False, p.stat().st_size)

    def listdir(self, rel):
        p = self._p(rel)
        if not p.is_dir():
            return None
        out = {}
        for e in p.iterdir():
            try:
                out[e.name] = (e.is_dir(), 0 if e.is_dir() else e.stat().st_size)
            except OSError:
                continue
        return out

    def read(self, rel):
        return self._p(rel).read_bytes()

    def mkdirs(self, rel):
        self._p(rel).mkdir(parents=True, exist_ok=True)

    def write(self, rel, opener, size):
        full = self._p(rel)
        full.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(str(full) + ".tmp")
        try:
            with opener() as src, open(tmp, "wb") as dst:
                shutil.copyfileobj(src, dst)
            tmp.replace(full)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise

    def rename(self, old, new):
        if self._p(new).exists():
            raise FileExistsError(f"目标目录已存在: {new}")
        self._p(old).rename(self._p(new))

    def ping(self):
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            return True, f"本地目录 {self.root}"
        except OSError as e:
            return False, str(e)

    def describe(self):
        return str(self.root)


# ====================================================================== SMB
class SMBBackend(Backend):
    name = 'smb'

    def __init__(self):
        self.conn = None
        self.lock = threading.RLock()  # SMB 连接不是线程安全的
        self._connect()

    def describe(self):
        return f"\\\\{Config.NAS_IP}\\{Config.NAS_SHARE}\\{Config.NAS_BASE_PATH}"

    def _full(self, rel):
        return f"{Config.NAS_BASE_PATH}/{rel}".replace("\\", "/").replace("//", "/")

    def _connect(self):
        if SMBConnection is None:
            raise RuntimeError("缺少 pysmb 库，无法使用 SMB 模式（pip install pysmb）")
        if self.conn:
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = None
        conn = SMBConnection(Config.NAS_USER, Config.NAS_PASS, "PixivClient", Config.NAS_REMOTE_NAME, use_ntlm_v2=True)
        if not conn.connect(Config.NAS_IP, 445, timeout=15):
            raise ConnectionError("无法连接到 NAS SMB 服务器（地址/端口不通或认证失败）")
        self.conn = conn
        base = self._full('').rstrip('/')   # 统一用 / 分隔（配置里可能是反斜杠）
        try:
            conn.listPath(Config.NAS_SHARE, base)
        except OperationFailure:
            try:
                self.mkdirs('')
            except Exception as e:
                logger.warning(f"创建 SMB 基础目录失败: {e}")

    def _smb(self, op, retries=2):
        """在锁内执行。OperationFailure（文件不存在/无权限等）原样抛出；其余异常视为连接问题，重连后重试。"""
        def run():
            if self.conn is None:
                self._connect()
            return op(self.conn)
        return _retry(self.lock, self._connect, run, retries, 'SMB 操作', passthrough=(OperationFailure,))

    def stat(self, rel):
        try:
            a = self._smb(lambda c: c.getAttributes(Config.NAS_SHARE, self._full(rel)))
            return (bool(a.isDirectory), 0 if a.isDirectory else a.file_size)
        except OperationFailure:
            return None

    def listdir(self, rel):
        path = self._full(rel).rstrip("/")
        try:
            files = self._smb(lambda c: c.listPath(Config.NAS_SHARE, path))
        except OperationFailure:
            return None
        return {f.filename: (f.isDirectory, 0 if f.isDirectory else f.file_size)
                for f in files if f.filename not in (".", "..")}

    def read(self, rel):
        def op(c):
            buf = io.BytesIO()
            c.retrieveFile(Config.NAS_SHARE, self._full(rel), buf)
            return buf.getvalue()
        return self._smb(op)

    def mkdirs(self, rel):
        current = ""
        for part in split_rel(self._full(rel)):
            current += "/" + part
            self._ensure_dir(current)

    def _ensure_dir(self, path):
        try:
            self._smb(lambda c: c.listPath(Config.NAS_SHARE, path))
        except OperationFailure:
            try:
                self._smb(lambda c: c.createDirectory(Config.NAS_SHARE, path))
            except OperationFailure:
                self._smb(lambda c: c.listPath(Config.NAS_SHARE, path))   # 并发创建时可能已被别的线程建好

    def write(self, rel, opener, size):
        if interrupt.is_set():
            raise InterruptedError("上传被中断")
        parent = rel.rsplit('/', 1)[0] if '/' in rel else ''
        if parent:
            self.mkdirs(parent)
        full = self._full(rel)
        part = full + ".part"
        last = None
        for attempt in range(3):
            fobj = opener()
            try:
                self._smb(lambda c: c.storeFile(Config.NAS_SHARE, part, fobj), retries=0)
                try:
                    self._smb(lambda c: c.rename(Config.NAS_SHARE, part, full), retries=0)
                except OperationFailure:
                    # 目标已存在（并发/重复）：确认大小正常则视为成功，并清理 .part
                    st = self.stat(rel)
                    if st and st[1] >= size > 0:
                        self._delete(part)
                        return
                    raise
                return
            except OperationFailure as e:
                last = e
                self._delete(part)
            except Exception as e:
                last = e
                with self.lock:
                    try:
                        self._connect()
                    except Exception:
                        pass
            finally:
                try:
                    fobj.close()
                except Exception:
                    pass
            if interrupt.wait(1.0 * (attempt + 1)):
                raise InterruptedError("上传被中断")
        raise RuntimeError(f"SMB 上传失败: {last}")

    def _delete(self, full_path):
        try:
            self._smb(lambda c: c.deleteFiles(Config.NAS_SHARE, full_path), retries=0)
        except Exception:
            pass

    def rename(self, old, new):
        self._smb(lambda c: c.rename(Config.NAS_SHARE, self._full(old), self._full(new)), retries=0)

    def ping(self):
        try:
            self._smb(lambda c: c.listPath(Config.NAS_SHARE, self._full('').rstrip('/')), retries=0)
            return True, self.describe()
        except Exception as e:
            return False, str(e)

    def close(self):
        if self.conn:
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = None


# ====================================================================== WebDAV
_DAV_PROPFIND = ('<?xml version="1.0" encoding="utf-8"?><d:propfind xmlns:d="DAV:"><d:prop>'
                 '<d:resourcetype/><d:getcontentlength/></d:prop></d:propfind>')


def parse_multistatus(text):
    """解析 WebDAV 207 响应 → [(解码后的路径, 是否目录, 大小)]"""
    import xml.etree.ElementTree as ET
    ns = {'d': 'DAV:'}
    items = []
    for resp in ET.fromstring(text).findall('d:response', ns):
        href = resp.findtext('d:href', namespaces=ns) or ''
        is_dir, size = False, 0
        for ps in resp.findall('d:propstat', ns):
            if ' 200' not in (ps.findtext('d:status', namespaces=ns) or ' 200'):
                continue
            prop = ps.find('d:prop', ns)
            if prop is None:
                continue
            rt = prop.find('d:resourcetype', ns)
            if rt is not None and rt.find('d:collection', ns) is not None:
                is_dir = True
            cl = prop.findtext('d:getcontentlength', namespaces=ns)
            if cl and cl.strip().isdigit():
                size = int(cl.strip())
        items.append((unquote(urlparse(href).path), is_dir, size))
    return items


class WebDAVBackend(Backend):
    name = 'webdav'

    def __init__(self, url, user='', password='', verify=True):
        import requests
        u = (url or '').strip()
        for a, b in (('webdavs://', 'https://'), ('davs://', 'https://'), ('webdav://', 'http://'), ('dav://', 'http://')):
            if u.lower().startswith(a):
                u = b + u[len(a):]
        if not re.match(r'^https?://', u, re.I):
            u = 'https://' + u
        p = urlparse(u)
        if not p.hostname:
            raise BackendError("WebDAV 地址里缺少主机名")
        self.base = u.rstrip('/')
        self.origin = f"{p.scheme}://{p.netloc}"
        self.base_path = unquote(p.path).rstrip('/')
        self._base_ready = False
        self.session = requests.Session()
        self.session.verify = bool(verify)
        if user:
            self.session.auth = (user, password or '')
        self.session.proxies = Config.PROXIES or {}
        self.timeout = (10, 60)

    def describe(self):
        return self.base

    def _url(self, rel):
        parts = split_rel(rel)
        return self.base + ('/' + '/'.join(quote(p) for p in parts) if parts else '')

    def _check(self, r, what):
        if r.status_code == 401:
            raise BackendError("用户名或密码不正确（401）")
        if r.status_code == 403:
            raise BackendError(f"没有权限（403）：{what}")
        if r.status_code >= 400:
            raise BackendError(f"{what}失败：HTTP {r.status_code}")

    def _propfind(self, rel, depth, url=None):
        r = self.session.request('PROPFIND', url or self._url(rel), data=_DAV_PROPFIND.encode('utf-8'), timeout=self.timeout,
                                 headers={'Depth': str(depth), 'Content-Type': 'application/xml; charset=utf-8'})
        if r.status_code == 404:
            return None
        self._check(r, '读取目录')
        return parse_multistatus(r.text)

    def stat(self, rel):
        items = self._propfind(rel, 0)
        if not items:
            return None
        _, is_dir, size = items[0]
        return (is_dir, 0 if is_dir else size)

    def listdir(self, rel):
        items = self._propfind(rel, 1)
        if items is None:
            return None
        here = (self.base_path + '/' + '/'.join(split_rel(rel))).rstrip('/')
        out = {}
        for path, is_dir, size in items:
            p = path.rstrip('/')
            if p == here or not p:
                continue
            name = p.rsplit('/', 1)[-1]
            if name:
                out[name] = (is_dir, 0 if is_dir else size)
        return out

    def read(self, rel):
        r = self.session.get(self._url(rel), timeout=self.timeout)
        self._check(r, '读取文件')
        return r.content

    def _ensure_base(self):
        """保存根目录（URL 里的路径）可能还不存在，逐级创建（服务器根下已有的层级会跳过）。"""
        if self._base_ready:
            return
        cur = ''
        for seg in split_rel(self.base_path):
            cur += '/' + quote(seg)
            url = self.origin + cur
            try:
                exists = self._propfind('', 0, url=url) is not None
            except BackendError:
                continue                       # 上级目录不让列（403 等），假定存在，继续往下走
            if not exists:
                r = self.session.request('MKCOL', url, timeout=self.timeout)
                if r.status_code not in (201, 405):
                    self._check(r, f'创建目录 {unquote(cur)}')
        self._base_ready = True

    def mkdirs(self, rel):
        self._ensure_base()
        cur = ''
        for part in split_rel(rel):
            cur = cur + '/' + part
            if self.stat(cur) is not None:
                continue
            r = self.session.request('MKCOL', self._url(cur), timeout=self.timeout)
            if r.status_code not in (201, 405):     # 405 = 已存在
                self._check(r, f'创建目录 {cur}')

    def write(self, rel, opener, size):
        if interrupt.is_set():
            raise InterruptedError("上传被中断")
        parent = rel.rsplit('/', 1)[0] if '/' in rel else ''
        if parent:
            self.mkdirs(parent)
        part = rel + '.part'
        f = opener()
        try:
            r = self.session.put(self._url(part), data=f, timeout=(10, 600))
        finally:
            f.close()
        self._check(r, '上传文件')
        mv = self.session.request('MOVE', self._url(part), timeout=self.timeout,
                                  headers={'Destination': self._url(rel), 'Overwrite': 'F'})
        if mv.status_code in (201, 204, 200):
            return
        if mv.status_code == 412:                     # 目标已存在：大小正常就当作成功
            st = self.stat(rel)
            self.session.delete(self._url(part), timeout=self.timeout)
            if st and st[1] >= size > 0:
                return
            raise BackendError("目标文件已存在但大小不一致")
        if mv.status_code in (405, 501):              # 服务器不支持 MOVE：退而直接写最终文件
            f = opener()
            try:
                r = self.session.put(self._url(rel), data=f, timeout=(10, 600))
            finally:
                f.close()
            self.session.delete(self._url(part), timeout=self.timeout)
            self._check(r, '上传文件')
            return
        self._check(mv, '重命名上传文件')

    def rename(self, old, new):
        r = self.session.request('MOVE', self._url(old), timeout=self.timeout,
                                 headers={'Destination': self._url(new), 'Overwrite': 'F'})
        if r.status_code not in (201, 204, 200):
            self._check(r, '重命名')
            raise BackendError(f"重命名失败：HTTP {r.status_code}")

    def delete(self, rel):
        self.session.delete(self._url(rel), timeout=self.timeout)

    def ping(self):
        try:
            self._ensure_base()
            return True, self.base
        except Exception as e:
            return False, str(e)

    def close(self):
        self.session.close()


# ====================================================================== FTP / FTPS
_LIST_UNIX = re.compile(r'^([\-dl])[rwxsStT\-]{9}\S*\s+\d+\s+\S+\s+\S+\s+(\d+)\s+\w{3}\s+\d+\s+[\d:]+\s+(.+)$')
_LIST_DOS = re.compile(r'^\d{2}-\d{2}-\d{2,4}\s+\d{2}:\d{2}[AP]M\s+(<DIR>|\d+)\s+(.+)$')


def parse_list_line(line):
    """解析 LIST 的一行（unix 风格或 IIS/DOS 风格）→ (名称, 是否目录, 大小) 或 None"""
    line = line.rstrip('\r\n')
    m = _LIST_UNIX.match(line)
    if m:
        kind, size, name = m.groups()
        name = name.split(' -> ')[0]
        return (name, kind == 'd', 0 if kind == 'd' else int(size))
    m = _LIST_DOS.match(line)
    if m:
        size, name = m.groups()
        return (name, size == '<DIR>', 0 if size == '<DIR>' else int(size))
    return None


class FTPBackend(Backend):
    name = 'ftp'

    def __init__(self, host, port=21, user='', password='', tls=False, base='/'):
        self.host, self.port, self.user, self.password, self.tls = host, int(port or 21), user, password, bool(tls)
        self.base = '/' + '/'.join(split_rel(base))
        self.ftp = None
        self.lock = threading.RLock()
        self._connect()

    def describe(self):
        return f"{'ftps' if self.tls else 'ftp'}://{self.host}:{self.port}{self.base}"

    def _connect(self):
        import ftplib
        if self.ftp is not None:
            try:
                self.ftp.close()
            except Exception:
                pass
            self.ftp = None
        ftp = ftplib.FTP_TLS() if self.tls else ftplib.FTP()
        ftp.encoding = 'utf-8'
        ftp.connect(self.host, self.port, timeout=20)
        ftp.login(self.user or 'anonymous', self.password or ('anonymous@' if not self.user else ''))
        if self.tls:
            ftp.prot_p()
        ftp.set_pasv(True)
        ftp.voidcmd('TYPE I')
        self.ftp = ftp

    def _p(self, rel):
        parts = split_rel(rel)
        return self.base.rstrip('/') + ('/' + '/'.join(parts) if parts else '') or '/'

    def _do(self, op, retries=2):
        import ftplib
        def run():
            if self.ftp is None:
                self._connect()
            return op(self.ftp)
        return _retry(self.lock, self._connect, run, retries, 'FTP 操作', passthrough=(ftplib.error_perm,))

    def stat(self, rel):
        import ftplib
        path = self._p(rel)

        def op(ftp):
            try:
                return (False, ftp.size(path))
            except ftplib.error_perm:
                pass
            cur = ftp.pwd()
            try:
                ftp.cwd(path)
                return (True, 0)
            except ftplib.error_perm:
                return None
            finally:
                try:
                    ftp.cwd(cur)
                except Exception:
                    pass
        try:
            return self._do(op)
        except ftplib.error_perm:
            return None

    def listdir(self, rel):
        import ftplib
        path = self._p(rel)

        def op(ftp):
            try:
                out = {}
                for name, facts in ftp.mlsd(path, facts=['type', 'size']):
                    t = (facts.get('type') or '').lower()
                    if t in ('cdir', 'pdir') or name in ('.', '..'):
                        continue
                    is_dir = t == 'dir'
                    out[name] = (is_dir, 0 if is_dir else int(facts.get('size') or 0))
                return out
            except ftplib.error_perm as e:
                if str(e).startswith(('550', '450')):
                    return None            # 目录不存在
                # 服务器不支持 MLSD（如 vsftpd）：改用 LIST
            lines = []
            try:
                ftp.retrlines('LIST ' + path, lines.append)
            except ftplib.error_perm:
                return None
            out = {}
            for ln in lines:
                parsed = parse_list_line(ln)
                if parsed and parsed[0] not in ('.', '..'):
                    out[parsed[0]] = (parsed[1], parsed[2])
            return out
        try:
            return self._do(op)
        except ftplib.error_perm:
            return None

    def read(self, rel):
        path = self._p(rel)

        def op(ftp):
            buf = io.BytesIO()
            ftp.retrbinary('RETR ' + path, buf.write)
            return buf.getvalue()
        return self._do(op)

    def mkdirs(self, rel):
        import ftplib
        parts = split_rel(self.base) + split_rel(rel)      # base 本身也要保证存在
        cur = ''
        for part in parts:
            cur += '/' + part

            def op(ftp, cur=cur):
                try:
                    ftp.cwd(cur)
                except ftplib.error_perm:
                    try:
                        ftp.mkd(cur)
                    except ftplib.error_perm:
                        ftp.cwd(cur)           # 并发创建：现在应当存在了
                finally:
                    try:
                        ftp.cwd('/')
                    except Exception:
                        pass
            self._do(op)

    def write(self, rel, opener, size):
        import ftplib
        if interrupt.is_set():
            raise InterruptedError("上传被中断")
        parent = rel.rsplit('/', 1)[0] if '/' in rel else ''
        self.mkdirs(parent)
        final = self._p(rel)
        part = final + '.part'

        def op(ftp):
            f = opener()
            try:
                ftp.storbinary('STOR ' + part, f)
            finally:
                f.close()
            try:
                ftp.rename(part, final)
            except ftplib.error_perm:
                st = self.stat(rel)
                try:
                    ftp.delete(part)
                except ftplib.error_perm:
                    pass
                if not (st and st[1] >= size > 0):
                    raise
        self._do(op)

    def rename(self, old, new):
        self._do(lambda ftp: ftp.rename(self._p(old), self._p(new)), retries=0)

    def delete(self, rel):
        self._do(lambda ftp: ftp.delete(self._p(rel)), retries=0)

    def ping(self):
        try:
            self.mkdirs('')
            return True, self.describe()
        except Exception as e:
            return False, str(e)

    def close(self):
        if self.ftp is not None:
            try:
                self.ftp.quit()
            except Exception:
                pass
            self.ftp = None


# ====================================================================== SFTP
class SFTPBackend(Backend):
    name = 'sftp'

    def __init__(self, host, port=22, user='', password='', key_file='', base='/'):
        try:
            import paramiko  # noqa: F401
        except ImportError:
            raise RuntimeError("缺少 paramiko 库，无法使用 SFTP（pip install paramiko）")
        self.host, self.port, self.user, self.password, self.key_file = host, int(port or 22), user, password, key_file
        base = (base or '').replace('\\', '/')
        # /~/xxx 表示相对登录用户的主目录
        self.home_relative = base.startswith('/~')
        self.parts = split_rel(base[2:] if self.home_relative else base)
        self.client = None
        self.sftp = None
        self.lock = threading.RLock()
        self._connect()

    def describe(self):
        return f"sftp://{self.host}:{self.port}{'/~/' if self.home_relative else '/'}{'/'.join(self.parts)}"

    def _connect(self):
        import paramiko
        self.close()
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        kw = {'hostname': self.host, 'port': self.port, 'username': self.user or None, 'timeout': 20,
              'banner_timeout': 20, 'auth_timeout': 20}
        if self.key_file:
            kw['key_filename'] = os.path.expanduser(self.key_file)
            if self.password:
                kw['passphrase'] = self.password       # 有密钥时，密码框用作密钥口令
        else:
            kw['password'] = self.password or None
            kw['look_for_keys'] = False
            kw['allow_agent'] = False
        c.connect(**kw)
        self.client = c
        self.sftp = c.open_sftp()

    def _path(self, parts):
        if self.home_relative:
            return '/'.join(parts) or '.'
        return '/' + '/'.join(parts)

    def _p(self, rel):
        return self._path(self.parts + split_rel(rel))

    def _do(self, op, retries=2):
        def run():
            if self.sftp is None:
                self._connect()
            return op(self.sftp)
        return _retry(self.lock, self._connect, run, retries, 'SFTP 操作', passthrough=(FileNotFoundError,))

    @staticmethod
    def _is_missing(e):
        return isinstance(e, FileNotFoundError) or getattr(e, 'errno', None) == 2

    def stat(self, rel):
        import stat as st
        try:
            a = self._do(lambda s: s.stat(self._p(rel)))
        except Exception as e:
            if self._is_missing(e):
                return None
            raise
        return (True, 0) if st.S_ISDIR(a.st_mode) else (False, a.st_size)

    def listdir(self, rel):
        import stat as st
        try:
            items = self._do(lambda s: s.listdir_attr(self._p(rel)))
        except Exception as e:
            if self._is_missing(e):
                return None
            raise
        return {a.filename: (st.S_ISDIR(a.st_mode), 0 if st.S_ISDIR(a.st_mode) else a.st_size) for a in items}

    def read(self, rel):
        def op(s):
            buf = io.BytesIO()
            s.getfo(self._p(rel), buf)
            return buf.getvalue()
        return self._do(op)

    def mkdirs(self, rel):
        full = self.parts + split_rel(rel)
        for i in range(1, len(full) + 1):
            cur = self._path(full[:i])
            try:
                self._do(lambda s, c=cur: s.stat(c))
            except Exception as e:
                if not self._is_missing(e):
                    raise
                self._do(lambda s, c=cur: s.mkdir(c))

    def write(self, rel, opener, size):
        if interrupt.is_set():
            raise InterruptedError("上传被中断")
        parent = rel.rsplit('/', 1)[0] if '/' in rel else ''
        self.mkdirs(parent)
        final = self._p(rel)
        part = final + '.part'

        def op(s):
            f = opener()
            try:
                s.putfo(f, part)
            finally:
                f.close()
            st = None
            try:
                st = s.stat(final)
            except Exception as e:
                if not self._is_missing(e):
                    raise
            if st is not None:                          # 目标已存在：大小正常视为成功
                s.remove(part)
                if st.st_size >= size > 0:
                    return
                raise BackendError("目标文件已存在但大小不一致")
            s.rename(part, final)
        self._do(op)

    def rename(self, old, new):
        self._do(lambda s: s.rename(self._p(old), self._p(new)), retries=0)

    def delete(self, rel):
        self._do(lambda s: s.remove(self._p(rel)), retries=0)

    def ping(self):
        try:
            self.mkdirs('')
            return True, self.describe()
        except Exception as e:
            return False, str(e)

    def close(self):
        for obj in (self.sftp, self.client):
            if obj is not None:
                try:
                    obj.close()
                except Exception:
                    pass
        self.sftp = self.client = None


# ====================================================================== S3 / 对象存储
def s3_error_text(e, bucket=''):
    """把 botocore 的错误翻译成"该检查什么"。"""
    code = ''
    resp = getattr(e, 'response', None)
    if isinstance(resp, dict):
        code = (resp.get('Error') or {}).get('Code', '')
    name = e.__class__.__name__
    low = f"{code} {name} {e}".lower()
    if code in ('InvalidAccessKeyId', 'SignatureDoesNotMatch', 'AuthFailure', 'InvalidClientTokenId') or 'signature' in low:
        return "密钥不正确：Access Key 或 Secret Key 有误（或区域不对）"
    if code in ('NoSuchBucket', '404') and bucket:
        return f"找不到存储桶「{bucket}」：请检查名称、区域和服务端点"
    if code in ('AccessDenied', 'AllAccessDisabled', '403'):
        return "没有权限：这组密钥不能访问该存储桶 / 该操作（需要读、写、列出权限）"
    if 'sslerror' in low or 'ssl' in name.lower():
        return "HTTPS 证书校验失败：自建服务（如 MinIO 自签名证书）可以取消勾选「校验 HTTPS 证书」"
    if 'endpointconnection' in low or 'connecttimeout' in low or 'connection' in low and 'refused' in low:
        return "连不上服务端点：请检查地址（带 https:// 或 http://）、端口和网络"
    if 'could not connect' in low or 'timed out' in low or 'timeout' in low:
        return "连接超时：请检查服务端点地址和网络"
    if 'invalidendpoint' in low or 'invalid endpoint' in low:
        return "服务端点格式不对，应类似 https://s3.example.com"
    if 'nocredentials' in low or 'partialcredentials' in low:
        return "缺少 Access Key / Secret Key"
    return str(e) or name


class S3Backend(Backend):
    """S3 兼容的对象存储（AWS S3、MinIO、Cloudflare R2、阿里云 OSS、腾讯云 COS、Backblaze B2 …）。

    对象存储没有真正的目录：「保存文件夹」就是对象键的前缀，「目录」是由键里的 / 推出来的。
    单个对象的上传本身是原子的，所以不需要 .part 重命名；改画师目录名需要复制全部对象，这里不支持（沿用原目录）。
    """
    name = 's3'

    def __init__(self, endpoint='', region='', bucket='', prefix='', access_key='', secret_key='',
                 path_style=False, verify=True, client=None):
        if not (bucket or '').strip() and client is None:
            raise BackendError("请填写存储桶名称（Bucket）")
        self.bucket = (bucket or '').strip()
        self.endpoint = (endpoint or '').strip()
        self.prefix = '/'.join(split_rel(prefix))
        self.region = (region or '').strip() or 'us-east-1'
        self._client = client or self._make_client(self.endpoint, self.region, access_key, secret_key, path_style, verify)

    @staticmethod
    def _make_client(endpoint, region, access_key, secret_key, path_style, verify):
        try:
            import boto3
            from botocore.config import Config as BotoConfig
        except ImportError:
            raise RuntimeError("缺少 boto3 库，无法使用 S3 存储（pip install boto3）")
        cfg = BotoConfig(signature_version='s3v4', retries={'max_attempts': 3, 'mode': 'standard'},
                         connect_timeout=10, read_timeout=60,
                         s3={'addressing_style': 'path' if path_style else 'auto'},
                         proxies=Config.PROXIES or None)
        return boto3.session.Session().client(
            's3', endpoint_url=endpoint or None, region_name=region, aws_access_key_id=access_key or None,
            aws_secret_access_key=secret_key or None, config=cfg, verify=bool(verify))

    @property
    def client(self):
        return self._client

    def describe(self):
        host = f" @ {self.endpoint}" if self.endpoint else ''
        return f"s3://{self.bucket}/{self.prefix}{host}".rstrip('/')

    def _key(self, rel):
        parts = ([self.prefix] if self.prefix else []) + split_rel(rel)
        return '/'.join(parts)

    @staticmethod
    def _missing(e):
        code = (getattr(e, 'response', None) or {}).get('Error', {}).get('Code', '')
        status = (getattr(e, 'response', None) or {}).get('ResponseMetadata', {}).get('HTTPStatusCode')
        return code in ('404', 'NoSuchKey', 'NotFound') or status == 404

    def _list(self, key_prefix, delimiter=None, max_keys=None):
        kw = {'Bucket': self.bucket, 'Prefix': key_prefix}
        if delimiter:
            kw['Delimiter'] = delimiter
        if max_keys:
            kw['MaxKeys'] = max_keys
        token = None
        while True:
            if token:
                kw['ContinuationToken'] = token
            r = self._client.list_objects_v2(**kw)
            yield r
            token = r.get('NextContinuationToken') if r.get('IsTruncated') else None
            if not token or max_keys:
                return

    def stat(self, rel):
        key = self._key(rel)
        if not split_rel(rel):
            return (True, 0)                       # 保存根目录（桶或前缀）始终视为存在
        try:
            h = self._client.head_object(Bucket=self.bucket, Key=key)
            return (False, int(h.get('ContentLength', 0)))
        except Exception as e:
            if not self._missing(e):
                raise
        for r in self._list(key + '/', max_keys=1):          # 没有同名对象：看是不是"目录"（有以它为前缀的对象）
            if r.get('KeyCount', len(r.get('Contents', []))) or r.get('CommonPrefixes'):
                return (True, 0)
        return None

    def listdir(self, rel):
        key = self._key(rel)
        prefix = key + '/' if key else ''
        out = {}
        for r in self._list(prefix, delimiter='/'):
            for cp in r.get('CommonPrefixes', []):
                name = cp['Prefix'][len(prefix):].rstrip('/')
                if name:
                    out[name] = (True, 0)
            for obj in r.get('Contents', []):
                name = obj['Key'][len(prefix):]
                if name and not name.endswith('/'):
                    out[name] = (False, int(obj.get('Size', 0)))
        if not out and split_rel(rel):
            return None                            # 对象存储里"空目录"等于不存在（保存根目录除外）
        return out

    def read(self, rel):
        return self._client.get_object(Bucket=self.bucket, Key=self._key(rel))['Body'].read()

    def mkdirs(self, rel):
        pass                                       # 目录是虚拟的，写入对象时自然出现

    def write(self, rel, opener, size):
        if interrupt.is_set():
            raise InterruptedError("上传被中断")
        f = opener()
        try:
            self._client.upload_fileobj(f, self.bucket, self._key(rel))
        finally:
            f.close()

    def rename(self, old, new):
        raise BackendError("对象存储不支持重命名目录（画师改名后会继续使用原来的文件夹）")

    def delete(self, rel):
        self._client.delete_object(Bucket=self.bucket, Key=self._key(rel))

    def ping(self):
        try:
            self._client.head_bucket(Bucket=self.bucket)
            return True, self.describe()
        except Exception as e:
            return False, s3_error_text(e, self.bucket)

    def close(self):
        try:
            self._client.close()
        except Exception:
            pass


# ====================================================================== 工厂
def make_backend(mode=None):
    """按当前配置创建后端。"""
    mode = mode or Config.STORAGE_MODE
    if mode == 'local':
        return LocalBackend(Config.LOCAL_SAVE_PATH)
    if mode == 'smb':
        return SMBBackend()
    if mode == 'webdav':
        return WebDAVBackend(Config.WEBDAV_URL, Config.WEBDAV_USER, Config.WEBDAV_PASS, Config.WEBDAV_VERIFY_TLS)
    if mode == 'ftp':
        u = parse_remote_url(Config.FTP_URL, 21, ('ftp', 'ftps'), 'ftp')
        return FTPBackend(u['host'], u['port'], Config.FTP_USER, Config.FTP_PASS, u['scheme'] == 'ftps', u['path'])
    if mode == 'sftp':
        u = parse_remote_url(Config.SFTP_URL, 22, ('sftp', 'ssh'), 'sftp')
        return SFTPBackend(u['host'], u['port'], Config.SFTP_USER, Config.SFTP_PASS, Config.SFTP_KEY_FILE, u['path'])
    if mode == 's3':
        return S3Backend(Config.S3_ENDPOINT, Config.S3_REGION, Config.S3_BUCKET, Config.S3_PREFIX, Config.S3_ACCESS_KEY,
                         Config.S3_SECRET_KEY, Config.S3_PATH_STYLE, Config.S3_VERIFY_TLS)
    raise ValueError(f"未知的存储方式: {mode}")

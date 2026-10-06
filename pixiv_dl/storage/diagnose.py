"""各存储协议的「测试连接」：逐步检查并返回 {ok, message, steps:[{name, ok, detail}]}。

SMB 的诊断在 smbtools.py；这里是 本地 / WebDAV / FTP(FTPS) / SFTP。
表单里没填的密码沿用已保存的密码。所有函数不抛异常，错误翻译成"该检查什么"。
"""
import io
import socket

from pixiv_dl.config import Config
from pixiv_dl.storage.backends import BackendError, WebDAVBackend, parse_remote_url, split_rel

PROBE = '.pixiv_write_test'


class _Steps:
    def __init__(self):
        self.steps = []

    def add(self, name, ok, detail):
        self.steps.append({'name': name, 'ok': ok, 'detail': detail})
        return ok

    def result(self, success_message='连接正常，可以保存'):
        ok = bool(self.steps) and all(s['ok'] for s in self.steps)
        bad = next((s['detail'] for s in self.steps if not s['ok']), '')
        return {'ok': ok, 'message': success_message if ok else bad or '测试失败', 'steps': self.steps}


def _net_error(e):
    low = str(e).lower()
    if isinstance(e, socket.gaierror) or 'name or service not known' in low or 'getaddrinfo' in low or 'nodename' in low:
        return "找不到这个地址：请检查主机名/IP 是否写对"
    if isinstance(e, (socket.timeout, TimeoutError)) or 'timed out' in low or 'timeout' in low:
        return "连接超时：请检查地址和端口、服务器是否开机、防火墙是否放行"
    if isinstance(e, ConnectionRefusedError) or 'refused' in low:
        return "连接被拒绝：服务器没有在这个端口提供服务（请检查端口和协议类型）"
    if isinstance(e, ConnectionResetError) or 'reset' in low:
        return "连接被服务器重置：协议或加密方式可能不匹配（例如 FTP 地址写成了 ftps，或反过来）"
    return str(e) or e.__class__.__name__


def _pw(v, key):
    return v.get(key) or getattr(Config, key, '')


# ====================================================================== 本地
def diagnose_local(path):
    st = _Steps()
    if not (path or '').strip():
        st.add('本地目录', False, '请填写保存目录')
        return st.result()
    try:
        import os
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, PROBE)
        with open(probe, 'w') as f:
            f.write('ok')
        os.remove(probe)
        st.add('本地目录', True, f'{path} 可以读写')
    except Exception as e:
        st.add('本地目录', False, f'无法使用这个目录：{e}')
    return st.result('目录可用，可以保存')


# ====================================================================== WebDAV
def diagnose_webdav(v):
    import requests
    st = _Steps()
    url = (v.get('WEBDAV_URL') or '').strip()
    if not url:
        st.add('解析地址', False, '请填写 WebDAV 地址，例如 https://nas.local:5006/dav/PIXIV')
        return st.result()
    user, verify = v.get('WEBDAV_USER') or '', v.get('WEBDAV_VERIFY_TLS', True)
    try:
        be = WebDAVBackend(url, user, _pw(v, 'WEBDAV_PASS'), verify)
    except BackendError as e:
        st.add('解析地址', False, str(e))
        return st.result()
    plain = be.base.lower().startswith('http://')
    st.add('解析地址', True, be.base + ('（注意：http 不加密，密码会明文传输）' if plain and user else ''))

    try:
        r = be.session.request('PROPFIND', be._url(''), headers={'Depth': '0'}, timeout=(8, 20))
    except requests.exceptions.SSLError:
        st.add('连接服务器', False, 'HTTPS 证书校验失败：如果服务器用的是自签名证书，请取消勾选「校验 HTTPS 证书」')
        return st.result()
    except Exception as e:
        st.add('连接服务器', False, _net_error(e))
        return st.result()
    st.add('连接服务器', True, f'服务器有响应（HTTP {r.status_code}）')
    if r.status_code == 401:
        st.add('认证', False, '用户名或密码不正确（401）。有些服务需要「应用专用密码」而不是登录密码')
        return st.result()
    if r.status_code == 403:
        st.add('认证', False, '没有权限访问这个地址（403）')
        return st.result()
    if r.status_code in (405, 501):
        st.add('认证', False, f'这个地址不支持 WebDAV（HTTP {r.status_code}）：请确认填的是 WebDAV 服务的地址，而不是普通网页地址')
        return st.result()
    if r.status_code >= 500:
        st.add('认证', False, f'服务器内部错误（HTTP {r.status_code}），请稍后再试或检查服务器日志')
        return st.result()
    st.add('认证', True, '认证通过' if r.status_code != 404 else '认证通过（保存目录还不存在）')

    # 保存目录 + 写入权限：在已存在的最深一层目录里做写入测试
    base_missing = r.status_code == 404
    existing = None if base_missing else be
    if base_missing:
        trial = be.base
        while trial.count('/') >= 3:                # 到 scheme://host 为止
            trial = trial.rsplit('/', 1)[0]
            cand = WebDAVBackend(trial, user, _pw(v, 'WEBDAV_PASS'), verify)
            try:
                if cand.stat('') is not None:
                    existing = cand
                    break
            except Exception:
                continue
    st.add('保存目录', True, '已存在' if not base_missing else '还不存在，保存文件时会自动创建')
    if existing is None:
        st.add('写入权限', False, '找不到任何可写入的上级目录')
        return st.result()
    try:
        existing.write(PROBE, lambda: io.BytesIO(b'ok'), 2)
        existing.delete(PROBE)
        st.add('写入权限', True, '可以写入' + ('（已在上一级目录验证）' if base_missing else ''))
    except Exception as e:
        st.add('写入权限', False, str(e) if isinstance(e, BackendError) else _net_error(e))
    return st.result()


# ====================================================================== FTP / FTPS
def diagnose_ftp(v):
    import ftplib
    st = _Steps()
    try:
        u = parse_remote_url(v.get('FTP_URL'), 21, ('ftp', 'ftps'), 'ftp')
    except BackendError as e:
        st.add('解析地址', False, f'{e}。格式：ftp://主机:端口/保存目录（加密用 ftps://）')
        return st.result()
    tls = u['scheme'] == 'ftps'
    st.add('解析地址', True, f"{'FTPS（显式 TLS 加密）' if tls else 'FTP（不加密）'} · {u['host']}:{u['port']} · 目录 {u['path'] or '/'}")

    ftp = ftplib.FTP_TLS() if tls else ftplib.FTP()
    ftp.encoding = 'utf-8'
    try:
        ftp.connect(u['host'], u['port'], timeout=15)
    except Exception as e:
        st.add('连接服务器', False, _net_error(e))
        return st.result()
    st.add('连接服务器', True, '服务器有响应')
    user = v.get('FTP_USER') or ''
    try:
        ftp.login(user or 'anonymous', _pw(v, 'FTP_PASS') or ('anonymous@' if not user else ''))
        if tls:
            ftp.prot_p()
        ftp.set_pasv(True)
        ftp.voidcmd('TYPE I')
    except ftplib.error_perm as e:
        st.add('登录', False, '用户名或密码不正确' if str(e).startswith('530') else f'登录失败：{e}')
        _quit(ftp)
        return st.result()
    except Exception as e:
        st.add('登录', False, ('TLS 握手失败：服务器可能不支持加密，请把地址改成 ftp://' if tls else '') + _net_error(e))
        _quit(ftp)
        return st.result()
    st.add('登录', True, f"{user or '匿名'} 登录成功")

    try:
        existing, missing = '', False
        for part in split_rel(u['path']):
            cur = existing + '/' + part
            try:
                ftp.cwd(cur)
                existing = cur
            except ftplib.error_perm:
                missing = True
                break
        ftp.cwd('/')
        st.add('保存目录', True, '已存在' if not missing else '还不存在，保存文件时会自动创建')
        probe = (existing or '') + '/' + PROBE
        try:
            ftp.storbinary('STOR ' + probe, io.BytesIO(b'ok'))
            ftp.delete(probe)
            st.add('写入权限', True, '可以写入' + ('（已在上一级目录验证）' if missing else ''))
        except ftplib.error_perm as e:
            st.add('写入权限', False, f'没有写入权限：{e}')
    except Exception as e:
        st.add('保存目录', False, _net_error(e))
    finally:
        _quit(ftp)
    return st.result()


def _quit(ftp):
    try:
        ftp.quit()
    except Exception:
        try:
            ftp.close()
        except Exception:
            pass


# ====================================================================== SFTP
def diagnose_sftp(v):
    st = _Steps()
    try:
        u = parse_remote_url(v.get('SFTP_URL'), 22, ('sftp', 'ssh'), 'sftp')
    except BackendError as e:
        st.add('解析地址', False, f'{e}。格式：sftp://主机:端口/保存目录（相对主目录用 sftp://主机/~/目录）')
        return st.result()
    st.add('解析地址', True, f"{u['host']}:{u['port']} · 目录 {u['path'] or '/'}")
    try:
        import paramiko
    except ImportError:
        st.add('依赖', False, '缺少 paramiko 库：请在运行本程序的环境里执行 pip install paramiko，然后重启程序')
        return st.result()

    try:
        socket.create_connection((u['host'], u['port']), timeout=10).close()
        st.add('连接服务器', True, '端口可以访问')
    except Exception as e:
        st.add('连接服务器', False, _net_error(e))
        return st.result()

    key = (v.get('SFTP_KEY_FILE') or '').strip()
    pw = _pw(v, 'SFTP_PASS')
    user = v.get('SFTP_USER') or ''
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        import os
        kw = {'hostname': u['host'], 'port': u['port'], 'username': user or None, 'timeout': 15, 'banner_timeout': 15, 'auth_timeout': 15}
        if key:
            if not os.path.isfile(os.path.expanduser(key)):
                st.add('登录', False, f'找不到私钥文件：{key}')
                return st.result()
            kw['key_filename'] = os.path.expanduser(key)
            if pw:
                kw['passphrase'] = pw
        else:
            kw.update(password=pw or None, look_for_keys=False, allow_agent=False)
        client.connect(**kw)
    except paramiko.AuthenticationException:
        st.add('登录', False, ('私钥或口令不正确，或服务器没有授权这把密钥' if key else '用户名或密码不正确（部分服务器只允许密钥登录）'))
        return st.result()
    except Exception as e:
        st.add('登录', False, _net_error(e))
        return st.result()
    st.add('登录', True, f"{user or '当前用户'} 登录成功（{'密钥' if key else '密码'}）")

    try:
        sftp = client.open_sftp()
        home = u['path'].startswith('/~')
        parts = split_rel(u['path'][2:] if home else u['path'])

        def path_of(ps):
            return ('/'.join(ps) or '.') if home else '/' + '/'.join(ps)
        existing, missing = [], False
        for i in range(1, len(parts) + 1):
            try:
                sftp.stat(path_of(parts[:i]))
                existing = parts[:i]
            except (FileNotFoundError, IOError):
                missing = True
                break
        st.add('保存目录', True, '已存在' if not missing else '还不存在，保存文件时会自动创建')
        probe = path_of(existing + [PROBE])
        try:
            sftp.putfo(io.BytesIO(b'ok'), probe)
            sftp.remove(probe)
            st.add('写入权限', True, '可以写入' + ('（已在上一级目录验证）' if missing else ''))
        except Exception as e:
            st.add('写入权限', False, f'没有写入权限：{e}')
    except Exception as e:
        st.add('保存目录', False, _net_error(e))
    finally:
        try:
            client.close()
        except Exception:
            pass
    return st.result()


# ====================================================================== S3 / 对象存储
def diagnose_s3(v):
    from urllib.parse import urlparse
    from pixiv_dl.storage.backends import S3Backend, s3_error_text
    st = _Steps()
    bucket = (v.get('S3_BUCKET') or '').strip()
    endpoint = (v.get('S3_ENDPOINT') or '').strip()
    prefix = '/'.join(split_rel(v.get('S3_PREFIX')))
    if not bucket:
        st.add('解析配置', False, '请填写存储桶名称（Bucket）')
        return st.result()
    if endpoint and not endpoint.lower().startswith(('http://', 'https://')):
        st.add('解析配置', False, '服务端点要以 https:// 或 http:// 开头，例如 https://s3.example.com（用 AWS S3 的话把它留空）')
        return st.result()
    if not (v.get('S3_ACCESS_KEY') or '').strip() or not _pw(v, 'S3_SECRET_KEY'):
        st.add('解析配置', False, '请填写 Access Key 和 Secret Key')
        return st.result()
    where = endpoint or 'AWS S3'
    st.add('解析配置', True, f"{where} · 存储桶 {bucket} · 保存位置 {prefix or '（桶根目录）'}" +
           ('（注意：http 不加密）' if endpoint.lower().startswith('http://') else ''))
    try:
        import boto3  # noqa: F401
    except ImportError:
        st.add('依赖', False, '缺少 boto3 库：请在运行本程序的环境里执行 pip install boto3，然后重启程序')
        return st.result()

    if endpoint:
        u = urlparse(endpoint)
        try:
            socket.create_connection((u.hostname, u.port or (443 if u.scheme == 'https' else 80)), timeout=10).close()
            st.add('连接服务端点', True, f'{u.hostname} 可以访问')
        except Exception as e:
            st.add('连接服务端点', False, _net_error(e))
            return st.result()
    try:
        be = S3Backend(endpoint, v.get('S3_REGION') or '', bucket, prefix, v.get('S3_ACCESS_KEY') or '', _pw(v, 'S3_SECRET_KEY'),
                       bool(v.get('S3_PATH_STYLE')), v.get('S3_VERIFY_TLS', True))
    except Exception as e:
        st.add('存储桶', False, str(e))
        return st.result()
    try:
        try:
            be.client.head_bucket(Bucket=bucket)
            st.add('存储桶', True, f'存储桶「{bucket}」可以访问（密钥有效）')
        except Exception as e:
            st.add('存储桶', False, s3_error_text(e, bucket))
            return st.result()
        try:
            n = len(next(be._list(be._key('') + '/' if be._key('') else '', max_keys=1)).get('Contents', []))
            st.add('保存位置', True, '该位置下已有文件' if n else '该位置下还没有文件，保存文件时会自动出现（对象存储没有"空文件夹"）')
        except Exception as e:
            st.add('保存位置', False, '无法列出该位置：' + s3_error_text(e, bucket) + '（读取已有文件、核查存储需要「列出」权限）')
        try:
            key = be._key(PROBE)
            be.client.put_object(Bucket=bucket, Key=key, Body=b'ok')
            be.client.delete_object(Bucket=bucket, Key=key)
            st.add('写入权限', True, '可以写入')
        except Exception as e:
            st.add('写入权限', False, s3_error_text(e, bucket))
    finally:
        be.close()
    return st.result()


def diagnose(mode, v):
    """统一入口（SMB 走 smbtools）。v 是设置表单里的值。"""
    if mode == 'local':
        return diagnose_local(v.get('LOCAL_SAVE_PATH') or Config.LOCAL_SAVE_PATH)
    if mode == 'webdav':
        return diagnose_webdav(v)
    if mode == 'ftp':
        return diagnose_ftp(v)
    if mode == 'sftp':
        return diagnose_sftp(v)
    if mode == 's3':
        return diagnose_s3(v)
    return {'ok': False, 'message': f'未知的存储方式: {mode}', 'steps': []}

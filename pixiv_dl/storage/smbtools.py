"""NAS(SMB) 连接诊断与目录浏览：给设置页的「测试连接」「浏览…」用。

所有函数都不抛异常，失败信息整理成人话。密码为空时使用已保存的密码。
"""
import socket

from pixiv_dl.config import Config

_HIDDEN_PREFIX = ('@', '#', '.')   # 群晖的 @eaDir、#recycle 等系统目录


def friendly_error(e, share=None):
    """把 pysmb / socket 的异常翻译成「该检查什么」。"""
    msg = str(e)
    low = msg.lower()
    if isinstance(e, (socket.timeout, TimeoutError)) or 'timed out' in low or 'timeout' in low:
        return "连接超时：请检查 NAS 地址是否正确、NAS 是否开机、是否和本机在同一个网络"
    if isinstance(e, (ConnectionRefusedError, ConnectionResetError)) or 'refused' in low or 'unreachable' in low:
        return "连不上 NAS：请检查地址是否正确、NAS 是否开机、SMB 文件共享服务是否已启用"
    if 'logon_failure' in low or 'authentication' in low or 'password' in low:
        return "用户名或密码不正确"
    if 'bad_network_name' in low or 'unable to connect to shared device' in low:
        return f"找不到共享文件夹「{share}」：请检查共享名称（不是子文件夹）是否写对" if share else "找不到该共享文件夹"
    if 'access_denied' in low:
        return "没有权限：该账号没有这个位置的访问/写入权限"
    if 'object_name_not_found' in low or 'not found' in low or 'no such' in low:
        return "路径不存在"
    return msg or e.__class__.__name__


def _open(info):
    """建立连接，返回 (conn, 错误文本)。"""
    from smb.SMBConnection import SMBConnection
    ip = (info.get('ip') or '').strip()
    if not ip:
        return None, "请填写 NAS 地址"
    try:
        socket.create_connection((ip, 445), timeout=6).close()
    except Exception as e:
        return None, friendly_error(e)
    password = info.get('pass') or Config.NAS_PASS
    conn = SMBConnection(info.get('user') or '', password or '', "PixivClient",
                         info.get('remote_name') or 'NAS', use_ntlm_v2=True)
    try:
        if not conn.connect(ip, 445, timeout=10):
            return None, "用户名或密码不正确（或该账号不允许使用 SMB 访问）"
    except Exception as e:
        return None, friendly_error(e)
    return conn, None


def _norm(path):
    return [p for p in (path or '').replace('\\', '/').split('/') if p]


def diagnose(info):
    """逐步检查并返回 {ok, message, steps:[{name, ok, detail}]}。"""
    steps = []

    def add(name, ok, detail):
        steps.append({'name': name, 'ok': ok, 'detail': detail})
        return ok

    try:
        from smb.SMBConnection import SMBConnection  # noqa: F401
    except ImportError:
        return {'ok': False, 'message': '缺少 pysmb 库（pip install pysmb）', 'steps': []}
    ip, share = (info.get('ip') or '').strip(), (info.get('share') or '').strip()
    if not ip or not share:
        return {'ok': False, 'message': '请先填写完整的网络路径，例如 \\\\192.168.1.100\\共享名\\文件夹', 'steps': []}

    try:
        socket.create_connection((ip, 445), timeout=6).close()
        add('连接 NAS', True, f'{ip} 可以访问（445 端口）')
    except Exception as e:
        add('连接 NAS', False, friendly_error(e))
        return {'ok': False, 'message': steps[-1]['detail'], 'steps': steps}

    conn, err = _open(info)
    if conn is None:
        add('登录', False, err)
        return {'ok': False, 'message': err, 'steps': steps}
    add('登录', True, f"用户「{info.get('user')}」登录成功")
    try:
        try:
            conn.listPath(share, '/')
            add('共享文件夹', True, f'找到共享「{share}」')
        except Exception as e:
            add('共享文件夹', False, friendly_error(e, share))
            return {'ok': False, 'message': steps[-1]['detail'], 'steps': steps}

        parts = _norm(info.get('base_path'))
        existing = ''
        missing = False
        for p in parts:
            cur = existing + '/' + p
            try:
                conn.listPath(share, cur)
                existing = cur
            except Exception:
                missing = True
                break
        shown = '\\'.join(parts) or '（共享根目录）'
        add('保存文件夹', True, f'「{shown}」已存在' if not missing else f'「{shown}」还不存在，保存文件时会自动创建')

        # 写入测试：在已存在的最深一层目录里写一个小文件再删掉
        probe = (existing or '') + '/.pixiv_write_test'
        try:
            import io
            conn.storeFile(share, probe, io.BytesIO(b'ok'))
            conn.deleteFiles(share, probe)
            add('写入权限', True, '可以写入' + ('' if not missing else '（已在上一级目录验证）'))
        except Exception as e:
            add('写入权限', False, friendly_error(e, share))
    finally:
        try:
            conn.close()
        except Exception:
            pass
    ok = all(s['ok'] for s in steps)
    return {'ok': ok, 'message': '连接正常，可以保存' if ok else next(s['detail'] for s in steps if not s['ok']), 'steps': steps}


def browse(info):
    """浏览 NAS：没有 share 时列出共享；有 share 时列出 path 下的文件夹。
    返回 {ok, error, kind: 'shares'|'folders', entries: [名称]}。"""
    conn, err = _open(info)
    if conn is None:
        return {'ok': False, 'error': err, 'entries': []}
    try:
        share = (info.get('share') or '').strip()
        if not share:
            names = []
            try:
                for d in conn.listShares():
                    if not getattr(d, 'isSpecial', False) and getattr(d, 'type', 0) == 0 and not d.name.endswith('$'):
                        names.append(d.name)
            except Exception as e:
                return {'ok': False, 'error': f'无法列出共享文件夹：{friendly_error(e)}。可以直接手动输入路径。', 'entries': []}
            return {'ok': True, 'kind': 'shares', 'entries': sorted(names, key=str.lower)}
        path = '/' + '/'.join(_norm(info.get('path')))
        try:
            items = conn.listPath(share, path)
        except Exception as e:
            return {'ok': False, 'error': friendly_error(e, share), 'entries': []}
        dirs = [f.filename for f in items if f.isDirectory and f.filename not in ('.', '..')
                and not f.filename.startswith(_HIDDEN_PREFIX)]
        return {'ok': True, 'kind': 'folders', 'entries': sorted(dirs, key=str.lower)}
    finally:
        try:
            conn.close()
        except Exception:
            pass

#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
网络挂载管理模块
提供SMB、FTP等网络协议的挂载功能

安全约定：密码不得出现在任何子进程的命令行参数里（同机其他进程可通过进程列表看到）。
- Windows SMB：通过 WNetAddConnection2W 在内存中传递
- Linux/Mac：使用 0600 权限的临时凭据文件 / stdin
"""
import os
import platform
import subprocess
import tempfile
import threading
from typing import Optional, Dict, Any, Callable

from .logger import get_logger
from .credentials import resolve_password
from .config_manager import NetworkMount

logger = get_logger("NetworkMount")

MOUNT_TIMEOUT = 15  # 秒

# Win32 错误码
_ERROR_ALREADY_ASSIGNED = 85
_ERROR_SESSION_CREDENTIAL_CONFLICT = 1219

IS_WINDOWS = platform.system() == "Windows"

if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    class _NETRESOURCEW(ctypes.Structure):
        _fields_ = [
            ("dwScope", wintypes.DWORD),
            ("dwType", wintypes.DWORD),
            ("dwDisplayType", wintypes.DWORD),
            ("dwUsage", wintypes.DWORD),
            ("lpLocalName", wintypes.LPWSTR),
            ("lpRemoteName", wintypes.LPWSTR),
            ("lpComment", wintypes.LPWSTR),
            ("lpProvider", wintypes.LPWSTR),
        ]

    _RESOURCETYPE_DISK = 1
    _mpr = ctypes.WinDLL("mpr")
    _mpr.WNetAddConnection2W.argtypes = [
        ctypes.POINTER(_NETRESOURCEW), wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD
    ]
    _mpr.WNetAddConnection2W.restype = wintypes.DWORD
    _mpr.WNetCancelConnection2W.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.BOOL]
    _mpr.WNetCancelConnection2W.restype = wintypes.DWORD


def _call_with_timeout(func: Callable[[], Any], timeout: float) -> Any:
    """在后台线程中执行阻塞调用，超时则放弃等待并抛出 TimeoutError"""
    result: Dict[str, Any] = {}

    def _runner() -> None:
        try:
            result["value"] = func()
        except BaseException as e:  # 原样转交给调用线程
            result["error"] = e

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise TimeoutError(f"操作超时 ({timeout}s)")
    if "error" in result:
        raise result["error"]
    return result.get("value")


def _is_drive_letter(mount_point: str) -> bool:
    mp = (mount_point or "").strip()
    return len(mp) == 2 and mp[1] == ":" and mp[0].isalpha()


def _build_unc_path(host: str, path: str) -> str:
    """规范化为 \\\\host\\share\\sub 形式"""
    clean_host = host.strip('/').strip('\\')
    clean_path = path.replace('/', '\\').lstrip('\\')
    return "\\\\" + clean_host + "\\" + clean_path


class NetworkMountManager:
    """网络挂载管理器"""

    def __init__(self):
        self._mounted_paths: Dict[str, str] = {}  # mount_point -> actual_path
        self._mount_lock = threading.Lock()

    def mount(self, mount_config: NetworkMount) -> bool:
        """
        挂载网络路径

        Args:
            mount_config: 挂载配置

        Returns:
            是否挂载成功
        """
        with self._mount_lock:
            try:
                protocol = mount_config.protocol.lower()
                if protocol == "smb":
                    return self._mount_smb(mount_config)
                elif protocol in ("ftp", "ftps"):
                    return self._mount_ftp(mount_config)
                elif protocol == "sftp":
                    return self._mount_sftp(mount_config)
                else:
                    logger.error(f"不支持的协议: {mount_config.protocol}")
                    return False
            except Exception as e:
                logger.error(f"挂载失败 {mount_config.protocol}://{mount_config.host}: {e}")
                return False

    def unmount(self, mount_point: str) -> bool:
        """
        卸载网络路径

        Args:
            mount_point: 挂载点（即挂载时配置里的 mount_point）

        Returns:
            是否卸载成功
        """
        with self._mount_lock:
            try:
                if IS_WINDOWS:
                    # 盘符直接取消；否则按挂载时记录的 UNC 路径取消
                    if _is_drive_letter(mount_point):
                        target = mount_point
                    else:
                        target = self._mounted_paths.get(mount_point, mount_point)
                    code = _call_with_timeout(
                        lambda: int(_mpr.WNetCancelConnection2W(target, 0, True)), MOUNT_TIMEOUT
                    )
                    success = code == 0
                    error = "" if success else str(ctypes.WinError(code))
                else:
                    result = subprocess.run(
                        ["umount", mount_point],
                        capture_output=True, text=True, timeout=MOUNT_TIMEOUT
                    )
                    success = result.returncode == 0
                    error = result.stderr

                if success:
                    self._mounted_paths.pop(mount_point, None)
                    logger.info(f"卸载成功: {mount_point}")
                else:
                    logger.error(f"卸载失败: {mount_point}, {error}")
                return success
            except Exception as e:
                logger.error(f"卸载异常: {e}")
                return False

    # ------------------------------------------------------------------
    # SMB
    # ------------------------------------------------------------------

    def _mount_smb(self, config: NetworkMount) -> bool:
        """挂载SMB共享"""
        try:
            password = resolve_password(config.encrypted_password)

            if IS_WINDOWS:
                share_path = _build_unc_path(config.host, config.path)
                local_name = config.mount_point.strip() if _is_drive_letter(config.mount_point) else None

                logger.info(f"挂载 SMB: {share_path}" + (f" -> {local_name}" if local_name else ""))
                code = _call_with_timeout(
                    lambda: self._wnet_add_connection(share_path, local_name, config.username, password),
                    MOUNT_TIMEOUT,
                )

                if code == 0:
                    self._mounted_paths[config.mount_point] = share_path
                    logger.info(f"SMB挂载成功: {share_path} -> {config.mount_point}")
                    return True
                if code in (_ERROR_SESSION_CREDENTIAL_CONFLICT, _ERROR_ALREADY_ASSIGNED):
                    # 已存在到该服务器的连接，直接复用
                    logger.warning(f"检测到已存在连接，尝试复用 (错误码 {code})")
                    self._mounted_paths[config.mount_point] = share_path
                    return True
                logger.error(f"SMB挂载失败: {ctypes.WinError(code)}")
                return False

            # Linux/Mac: 0600 权限的临时 credentials 文件
            share_path = f"//{config.host}/{config.path}"
            cred_file = self._write_temp_file(
                f"username={config.username}\npassword={password}\n"
            )
            try:
                result = subprocess.run(
                    ["mount", "-t", "cifs", share_path, config.mount_point,
                     "-o", f"credentials={cred_file}"],
                    capture_output=True, text=True, timeout=MOUNT_TIMEOUT
                )
            finally:
                self._remove_file(cred_file)
            if result.returncode == 0:
                self._mounted_paths[config.mount_point] = share_path
                logger.info(f"SMB挂载成功: {share_path} -> {config.mount_point}")
                return True
            logger.error(f"SMB挂载失败: {result.stderr}")
            return False
        except Exception as e:
            logger.error(f"SMB挂载异常: {e}")
            return False

    @staticmethod
    def _wnet_add_connection(remote: str, local: Optional[str], username: str, password: str) -> int:
        """调用 WNetAddConnection2W，返回 Win32 错误码（0 表示成功）"""
        nr = _NETRESOURCEW()
        nr.dwType = _RESOURCETYPE_DISK
        nr.lpLocalName = local
        nr.lpRemoteName = remote
        return int(_mpr.WNetAddConnection2W(
            ctypes.byref(nr), password or None, username or None, 0
        ))

    @staticmethod
    def _write_temp_file(content: str) -> str:
        """写入仅当前用户可读的临时文件，返回路径"""
        fd, path = tempfile.mkstemp(prefix="pv_cred_")
        try:
            os.chmod(path, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
        except Exception:
            NetworkMountManager._remove_file(path)
            raise
        return path

    @staticmethod
    def _remove_file(path: str) -> None:
        try:
            os.remove(path)
        except OSError:
            pass

    # ------------------------------------------------------------------
    # FTP / SFTP (仅 Linux/Mac)
    # ------------------------------------------------------------------

    def _mount_ftp(self, config: NetworkMount) -> bool:
        """挂载FTP（使用 curlftpfs，凭据经临时 HOME 下的 .netrc 传递）"""
        try:
            if IS_WINDOWS:
                logger.warning("Windows FTP挂载暂不支持")
                return False

            password = resolve_password(config.encrypted_password)
            home = tempfile.mkdtemp(prefix="pv_home_")
            netrc = os.path.join(home, ".netrc")
            try:
                with open(netrc, "w", encoding="utf-8") as f:
                    f.write(f"machine {config.host} login {config.username} password {password}\n")
                os.chmod(netrc, 0o600)

                url = f"ftp://{config.host}:{config.port or 21}{config.path}"
                result = subprocess.run(
                    ["curlftpfs", "-o", "netrc", url, config.mount_point],
                    capture_output=True, text=True, timeout=MOUNT_TIMEOUT,
                    env={**os.environ, "HOME": home},
                )
            finally:
                self._remove_file(netrc)
                try:
                    os.rmdir(home)
                except OSError:
                    pass

            if result.returncode == 0:
                self._mounted_paths[config.mount_point] = url
                logger.info(f"FTP挂载成功: {url} -> {config.mount_point}")
                return True
            logger.error(f"FTP挂载失败: {result.stderr}")
            return False
        except Exception as e:
            logger.error(f"FTP挂载异常: {e}")
            return False

    def _mount_sftp(self, config: NetworkMount) -> bool:
        """挂载SFTP（使用 sshfs，密码经 stdin 传入）"""
        try:
            if IS_WINDOWS:
                logger.warning("Windows SFTP挂载暂不支持")
                return False

            password = resolve_password(config.encrypted_password)
            url = f"{config.username}@{config.host}:{config.path}"
            result = subprocess.run(
                ["sshfs", "-p", str(config.port or 22), "-o", "password_stdin",
                 url, config.mount_point],
                input=password + "\n", capture_output=True, text=True, timeout=MOUNT_TIMEOUT
            )
            if result.returncode == 0:
                self._mounted_paths[config.mount_point] = url
                logger.info(f"SFTP挂载成功: {url} -> {config.mount_point}")
                return True
            logger.error(f"SFTP挂载失败: {result.stderr}")
            return False
        except Exception as e:
            logger.error(f"SFTP挂载异常: {e}")
            return False

    def get_mounted_paths(self) -> Dict[str, str]:
        """获取已挂载的路径"""
        return self._mounted_paths.copy()

    def is_mounted(self, mount_point: str) -> bool:
        """检查挂载点是否已挂载"""
        return mount_point in self._mounted_paths


# 全局挂载管理器实例
_mount_manager: Optional[NetworkMountManager] = None


def get_mount_manager() -> NetworkMountManager:
    """获取全局挂载管理器实例"""
    global _mount_manager
    if _mount_manager is None:
        _mount_manager = NetworkMountManager()
    return _mount_manager

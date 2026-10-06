#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
凭据存储模块
优先把密码存入系统凭据库（Windows 凭据管理器 / macOS 钥匙串 / Secret Service），
配置文件里只保存形如 ``keyring:<账户名>`` 的引用。

keyring 不可用时回退到 Fernet 加密（密钥与密文同在 data/config，仅起混淆作用），
并记录警告。旧版本保存的 Fernet 密文仍可读取。
"""
from __future__ import annotations

from typing import Optional

from .logger import get_logger

logger = get_logger("Credentials")

SERVICE_NAME = "PixivImageViewer"
KEYRING_PREFIX = "keyring:"


def _get_keyring():
    """返回可用的 keyring 模块，不可用时返回 None"""
    try:
        import keyring
        from keyring.backends import fail
        if isinstance(keyring.get_keyring(), fail.Keyring):
            return None
        return keyring
    except Exception as e:
        logger.debug(f"keyring 不可用: {e}")
        return None


def make_account_name(protocol: str, username: str, host: str, port: int, path: str) -> str:
    """生成凭据库中的账户名"""
    return f"{protocol}://{username}@{host}:{port}/{path.strip('/')}"


def is_keyring_ref(ref: str) -> bool:
    return bool(ref) and ref.startswith(KEYRING_PREFIX)


def is_legacy_ref(ref: str) -> bool:
    """旧版 Fernet 密文（非空且不是 keyring 引用）"""
    return bool(ref) and not ref.startswith(KEYRING_PREFIX)


def store_password(account: str, password: str) -> str:
    """
    保存密码，返回应写入配置文件的引用字符串。
    空密码返回空字符串。
    """
    if not password:
        return ""

    kr = _get_keyring()
    if kr is not None:
        try:
            kr.set_password(SERVICE_NAME, account, password)
            return KEYRING_PREFIX + account
        except Exception as e:
            logger.warning(f"写入系统凭据库失败，回退到本地加密: {e}")

    logger.warning("系统凭据库不可用，密码仅以本地密钥加密保存（安全性较低）")
    from .encryption import get_encryptor
    return get_encryptor().encrypt(password)


def resolve_password(ref: str) -> str:
    """根据配置中的引用取回明文密码，失败返回空字符串"""
    if not ref:
        return ""

    if is_keyring_ref(ref):
        kr = _get_keyring()
        if kr is None:
            logger.error("系统凭据库不可用，无法读取密码")
            return ""
        try:
            return kr.get_password(SERVICE_NAME, ref[len(KEYRING_PREFIX):]) or ""
        except Exception as e:
            logger.error(f"读取系统凭据库失败: {e}")
            return ""

    try:
        from .encryption import get_encryptor
        return get_encryptor().decrypt(ref)
    except Exception as e:
        logger.error(f"解密旧版密码失败: {e}")
        return ""


def delete_password(ref: Optional[str]) -> None:
    """删除引用对应的凭据（仅 keyring 引用有效）"""
    if not is_keyring_ref(ref or ""):
        return
    kr = _get_keyring()
    if kr is None:
        return
    try:
        kr.delete_password(SERVICE_NAME, ref[len(KEYRING_PREFIX):])
    except Exception as e:
        logger.debug(f"删除凭据失败（可能已不存在）: {e}")


def migrate_legacy_ref(ref: str, account: str) -> str:
    """
    把旧版 Fernet 密文迁移到系统凭据库。
    成功返回新引用；无需迁移、系统凭据库不可用或解密失败时原样返回 ref。
    """
    if not is_legacy_ref(ref) or _get_keyring() is None:
        return ref
    password = resolve_password(ref)
    if not password:
        return ref
    new_ref = store_password(account, password)
    return new_ref if is_keyring_ref(new_ref) else ref

#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
加密工具模块
提供密码加密和解密功能
"""
import base64
from cryptography.fernet import Fernet
from pathlib import Path
from typing import Optional

from .logger import get_logger
from .constants import CONFIG_DIR

logger = get_logger("Encryption")


class PasswordEncryptor:
    """密码加密器"""

    def __init__(self, key_file: Optional[Path] = None):
        """
        初始化加密器

        Args:
            key_file: 密钥文件路径，如果为None则使用默认路径
        """
        self.key_file = key_file or CONFIG_DIR / "encryption.key"
        self._fernet: Optional[Fernet] = None
        self._load_or_generate_key()

    def _load_or_generate_key(self) -> None:
        """加载或生成加密密钥"""
        try:
            if self.key_file.exists():
                with open(self.key_file, 'rb') as f:
                    key = f.read()
                self._fernet = Fernet(key)
                logger.debug("加密密钥加载成功")
            else:
                # 生成新密钥
                key = Fernet.generate_key()
                self.key_file.parent.mkdir(parents=True, exist_ok=True)
                with open(self.key_file, 'wb') as f:
                    f.write(key)
                self._fernet = Fernet(key)
                logger.info("生成新的加密密钥")
        except Exception as e:
            logger.error(f"初始化加密器失败: {e}")
            raise

    def encrypt(self, plaintext: str) -> str:
        """
        加密明文

        Args:
            plaintext: 要加密的明文

        Returns:
            加密后的密文（Base64编码）
        """
        if not self._fernet:
            raise RuntimeError("加密器未初始化")

        try:
            encrypted = self._fernet.encrypt(plaintext.encode('utf-8'))
            return base64.urlsafe_b64encode(encrypted).decode('utf-8')
        except Exception as e:
            logger.error(f"加密失败: {e}")
            raise

    def decrypt(self, ciphertext: str) -> str:
        """
        解密密文

        Args:
            ciphertext: 要解密的密文（Base64编码）

        Returns:
            解密后的明文
        """
        if not self._fernet:
            raise RuntimeError("加密器未初始化")

        try:
            encrypted = base64.urlsafe_b64decode(ciphertext.encode('utf-8'))
            decrypted = self._fernet.decrypt(encrypted)
            return decrypted.decode('utf-8')
        except Exception as e:
            logger.error(f"解密失败: {e}")
            raise


# 全局加密器实例
_encryptor: Optional[PasswordEncryptor] = None


def get_encryptor() -> PasswordEncryptor:
    """获取全局加密器实例"""
    global _encryptor
    if _encryptor is None:
        _encryptor = PasswordEncryptor()
    return _encryptor
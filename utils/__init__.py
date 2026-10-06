#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Utils 模块初始化
"""
from .logger import get_logger, setup_logging


def __getattr__(name):
    # 按需导入，避免只用日志的模块也加载配置
    if name in {'ConfigManager', 'AppConfig'}:
        from .config_manager import ConfigManager, AppConfig
        return {'ConfigManager': ConfigManager, 'AppConfig': AppConfig}[name]
    raise AttributeError(name)

__all__ = [
    'get_logger', 
    'setup_logging', 
    'ConfigManager', 
    'AppConfig',
]

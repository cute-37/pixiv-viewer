#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
日志系统模块
提供统一的日志记录功能，支持文件和控制台输出
支持运行时级别控制和日志自动清理
"""
import logging
import sys
import json
from datetime import datetime, timedelta
from typing import List, Dict
from logging.handlers import RotatingFileHandler

from .constants import LOG_DIR, CONFIG_DIR

# 日志配置文件
LOG_CONFIG_FILE = CONFIG_DIR / "log_config.json"

# 日志格式
CONSOLE_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
FILE_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(filename)s:%(lineno)d | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# 日志级别映射
LEVEL_NAMES = {
    'DEBUG': logging.DEBUG,
    'INFO': logging.INFO,
    'WARNING': logging.WARNING,
    'ERROR': logging.ERROR,
    'CRITICAL': logging.CRITICAL
}

# 默认配置
DEFAULT_CONFIG = {
    'level': 'INFO',
    'log_to_file': True,
    'log_to_console': True,
    'max_file_size_mb': 10,
    'backup_count': 5,
    'auto_cleanup': True,
    'retention_days': 30
}

# 全局日志级别
_log_level = logging.INFO
_initialized = False
_config: Dict = DEFAULT_CONFIG.copy()


def _load_config() -> Dict:
    """加载日志配置"""
    global _config
    try:
        if LOG_CONFIG_FILE.exists():
            with open(LOG_CONFIG_FILE, 'r', encoding='utf-8') as f:
                loaded = json.load(f)
                _config = {**DEFAULT_CONFIG, **loaded}
    except Exception:
        _config = DEFAULT_CONFIG.copy()
    return _config


def _save_config() -> None:
    """保存日志配置"""
    try:
        LOG_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(_config, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"保存日志配置失败: {e}")


def cleanup_old_logs(retention_days: int = 30) -> int:
    """
    清理过期日志文件
    
    Args:
        retention_days: 保留天数
        
    Returns:
        删除的文件数量
    """
    if retention_days <= 0:
        return 0
    
    cutoff_date = datetime.now() - timedelta(days=retention_days)
    deleted_count = 0
    
    try:
        for log_file in LOG_DIR.glob("*.log*"):
            try:
                # 获取文件修改时间
                mtime = datetime.fromtimestamp(log_file.stat().st_mtime)
                if mtime < cutoff_date:
                    log_file.unlink()
                    deleted_count += 1
            except Exception:
                continue
    except Exception:
        pass
    
    return deleted_count


def get_log_files() -> List[Dict]:
    """
    获取日志文件列表
    
    Returns:
        日志文件信息列表
    """
    files = []
    try:
        for log_file in sorted(LOG_DIR.glob("*.log*"), key=lambda x: x.stat().st_mtime, reverse=True):
            stat = log_file.stat()
            files.append({
                'name': log_file.name,
                'path': str(log_file),
                'size': stat.st_size,
                'size_str': f"{stat.st_size / 1024:.1f} KB" if stat.st_size < 1024 * 1024 else f"{stat.st_size / (1024 * 1024):.2f} MB",
                'modified': datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M:%S')
            })
    except Exception:
        pass
    return files


def get_total_log_size() -> str:
    """获取日志文件总大小"""
    total = 0
    try:
        for log_file in LOG_DIR.glob("*.log*"):
            total += log_file.stat().st_size
    except Exception:
        pass
    
    if total < 1024:
        return f"{total} B"
    elif total < 1024 * 1024:
        return f"{total / 1024:.1f} KB"
    else:
        return f"{total / (1024 * 1024):.2f} MB"


def setup_logging(
    level: int = None,
    log_to_file: bool = None,
    log_to_console: bool = None,
    max_file_size: int = None,
    backup_count: int = None
) -> None:
    """
    初始化日志系统
    
    Args:
        level: 日志级别（None 则从配置加载）
        log_to_file: 是否输出到文件
        log_to_console: 是否输出到控制台
        max_file_size: 单个日志文件最大大小（字节）
        backup_count: 保留的日志文件数量
    """
    global _log_level, _initialized, _config
    
    if _initialized:
        return
    
    # 加载配置
    _load_config()
    
    # 使用配置或参数
    if level is None:
        level = LEVEL_NAMES.get(_config['level'], logging.INFO)
    if log_to_file is None:
        log_to_file = _config['log_to_file']
    if log_to_console is None:
        log_to_console = _config['log_to_console']
    if max_file_size is None:
        max_file_size = _config['max_file_size_mb'] * 1024 * 1024
    if backup_count is None:
        backup_count = _config['backup_count']
        
    _log_level = level
    
    # 获取根日志记录器
    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    
    # 清除已有的处理器
    root_logger.handlers.clear()
    
    # 控制台处理器
    if log_to_console:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(level)
        console_handler.setFormatter(
            ColoredFormatter(CONSOLE_FORMAT, DATE_FORMAT)
        )
        root_logger.addHandler(console_handler)
    
    # 文件处理器
    if log_to_file:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        log_file = LOG_DIR / f"pixiv_viewer_{datetime.now().strftime('%Y%m%d')}.log"
        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=max_file_size,
            backupCount=backup_count,
            encoding='utf-8'
        )
        file_handler.setLevel(level)
        file_handler.setFormatter(logging.Formatter(FILE_FORMAT, DATE_FORMAT))
        root_logger.addHandler(file_handler)
    
    _initialized = True
    
    # 自动清理旧日志
    if _config.get('auto_cleanup', True):
        deleted = cleanup_old_logs(_config.get('retention_days', 30))
        if deleted > 0:
            root_logger.info(f"自动清理了 {deleted} 个过期日志文件")
    
    # 记录启动日志
    logger = get_logger("System")
    logger.info("=" * 60)
    logger.info("Pixiv 图片查看器启动")
    logger.info(f"日志级别: {logging.getLevelName(level)}")
    logger.info(f"日志目录: {LOG_DIR}")
    logger.info("=" * 60)


def set_log_level(level: str) -> None:
    """
    运行时设置日志级别
    
    Args:
        level: 级别名称（DEBUG, INFO, WARNING, ERROR, CRITICAL）
    """
    global _log_level, _config
    
    level_value = LEVEL_NAMES.get(level.upper(), logging.INFO)
    _log_level = level_value
    _config['level'] = level.upper()
    
    # 更新所有处理器
    root_logger = logging.getLogger()
    root_logger.setLevel(level_value)
    for handler in root_logger.handlers:
        handler.setLevel(level_value)
    
    _save_config()
    get_logger("System").info(f"日志级别已更改为: {level.upper()}")


def get_log_level() -> str:
    """获取当前日志级别名称"""
    return logging.getLevelName(_log_level)


def get_log_config() -> Dict:
    """获取日志配置"""
    return _config.copy()


def set_log_config(config: Dict) -> None:
    """
    设置日志配置
    
    Args:
        config: 配置字典
    """
    global _config
    _config.update(config)
    _save_config()
    
    # 如果级别变化，更新运行时级别
    if 'level' in config:
        set_log_level(config['level'])


class ColoredFormatter(logging.Formatter):
    """带颜色的控制台日志格式化器"""
    
    # ANSI 颜色代码
    COLORS = {
        'DEBUG': '\033[36m',     # 青色
        'INFO': '\033[32m',      # 绿色
        'WARNING': '\033[33m',   # 黄色
        'ERROR': '\033[31m',     # 红色
        'CRITICAL': '\033[35m',  # 紫色
    }
    RESET = '\033[0m'
    
    def format(self, record: logging.LogRecord) -> str:
        # 为级别名称添加颜色
        levelname = record.levelname
        if levelname in self.COLORS:
            record.levelname = f"{self.COLORS[levelname]}{levelname}{self.RESET}"
        
        result = super().format(record)
        
        # 恢复原始级别名称
        record.levelname = levelname
        return result


def get_logger(name: str) -> logging.Logger:
    """
    获取指定名称的日志记录器
    
    Args:
        name: 日志记录器名称（通常使用模块名）
        
    Returns:
        配置好的 Logger 实例
    """
    # main() owns logging setup; library imports must not create files or remove
    # handlers installed by pytest or an embedding application.
    logger = logging.getLogger(name)
    return logger


class LoggerMixin:
    """
    日志混入类，为类提供 logger 属性
    
    使用方式:
        class MyClass(LoggerMixin):
            def my_method(self):
                self.logger.info("Hello")
    """
    
    @property
    def logger(self) -> logging.Logger:
        if not hasattr(self, '_logger'):
            self._logger = get_logger(self.__class__.__name__)
        return self._logger


# 便捷的日志函数
def debug(msg: str, *args, **kwargs) -> None:
    get_logger("App").debug(msg, *args, **kwargs)

def info(msg: str, *args, **kwargs) -> None:
    get_logger("App").info(msg, *args, **kwargs)

def warning(msg: str, *args, **kwargs) -> None:
    get_logger("App").warning(msg, *args, **kwargs)

def error(msg: str, *args, **kwargs) -> None:
    get_logger("App").error(msg, *args, **kwargs)

def critical(msg: str, *args, **kwargs) -> None:
    get_logger("App").critical(msg, *args, **kwargs)

def exception(msg: str, *args, **kwargs) -> None:
    get_logger("App").exception(msg, *args, **kwargs)

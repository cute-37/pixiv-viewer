#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
配置管理器模块
提供类型安全的配置管理，支持自动保存和验证
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional, Any, Dict

from .logger import get_logger
from .constants import (
    CONFIG_DIR, DEFAULT_CONFIG, 
    THUMBNAIL_SIZE_MIN, THUMBNAIL_SIZE_MAX,
    MemoryStrategy, SortMode
)

logger = get_logger("ConfigManager")


@dataclass
class NetworkMount:
    """网络挂载配置"""
    protocol: str = ""  # smb, ftp, etc.
    host: str = ""
    port: int = 0
    path: str = ""
    username: str = ""
    encrypted_password: str = ""  # 加密后的密码
    mount_point: str = ""  # 本地挂载点
    enabled: bool = True

@dataclass
class AppConfig:
    """应用配置数据类"""
    image_path: str = ""  # 兼容旧配置
    image_paths: list = field(default_factory=list)  # 多路径支持
    last_folder: str = ""
    window_geometry: Optional[str] = None  # Base64 编码的 QByteArray
    thumbnail_size: int = 150
    memory_strategy: str = MemoryStrategy.MODERATE
    theme: str = "light"
    primary_color: str = "#007aff"  # 强调色（预设色会按亮/暗模式自动换成对应版本）
    toolbar_big_icons: bool = True
    toolbar_align: str = "center"
    hide_artist_id: bool = False
    sort_mode: str = SortMode.BY_ID
    spacing_mode: str = "standard"
    pixiv_metadata_path: str = ""
    avatar_path: str = ""
    browser_path: str = ""
    show_ai_watermark: bool = True
    show_rating_watermark: bool = True
    network_mounts: list = field(default_factory=list)  # 网络挂载配置
    max_concurrent_thumbnail_loads: int = 4  # 最大同时加载缩略图数量
    pregenerate_thumbnails: bool = True      # 浏览文件夹后在空闲时后台预生成其余缩略图
    function_order: list = field(default_factory=lambda: ["all_images", "todo_list"])  # 侧边栏功能入口顺序
    # 查看与浏览行为
    preload_images: bool = True          # 查看时预加载相邻图片
    smooth_scaling: bool = True          # 平滑缩放（关闭即像素模式）
    animations_enabled: bool = True      # 界面动画
    remember_last_path: bool = True      # 启动时打开上次浏览的文件夹
    expand_root: bool = False            # 启动时展开根目录
    fit_on_load: bool = True             # 打开图片时适应窗口
    double_click_action: str = "exit"    # exit / fullscreen / toggle_fit
    wheel_action: str = "zoom"           # zoom / navigate
    sidebar_collapsed: bool = False      # 侧栏收起为图标条

    def __post_init__(self) -> None:
        """初始化后的验证"""
        self.validate()
    
    def validate(self) -> None:
        """验证配置值的有效性"""
        # 验证缩略图大小
        self.thumbnail_size = max(
            THUMBNAIL_SIZE_MIN, 
            min(self.thumbnail_size, THUMBNAIL_SIZE_MAX)
        )
        
        # 验证内存策略
        valid_strategies = [
            MemoryStrategy.CONSERVATIVE, 
            MemoryStrategy.MODERATE, 
            MemoryStrategy.AGGRESSIVE
        ]
        if self.memory_strategy not in valid_strategies:
            logger.warning(f"无效的内存策略: {self.memory_strategy}，使用默认值")
            self.memory_strategy = MemoryStrategy.MODERATE
        
        # 验证主题
        if self.theme not in ("light", "dark"):
            self.theme = "light"
        
        # 验证排序模式
        valid_sort_modes = [SortMode.BY_ID, SortMode.BY_TIME, SortMode.BY_NAME]
        if self.sort_mode not in valid_sort_modes:
            self.sort_mode = SortMode.BY_ID
        
        # 验证工具栏对齐
        if self.toolbar_align not in ("left", "center", "right"):
            self.toolbar_align = "center"
        
        if self.double_click_action not in ("exit", "fullscreen", "toggle_fit"):
            self.double_click_action = "exit"
        if self.wheel_action not in ("zoom", "navigate"):
            self.wheel_action = "zoom"
        if not isinstance(self.function_order, list):
            self.function_order = ["all_images", "todo_list"]

        # 验证间距模式
        if self.spacing_mode not in ("compact", "standard", "wide"):
            self.spacing_mode = "standard"
        
        # 兼容旧配置：如果有 image_path 但 image_paths 为空
        if self.image_path and not self.image_paths:
            self.image_paths = [self.image_path]
        
        # 验证 image_paths 是列表
        if not isinstance(self.image_paths, list):
            self.image_paths = []

        # 验证 Pixiv 元数据路径
        if not isinstance(self.pixiv_metadata_path, str):
            self.pixiv_metadata_path = ""

        # 验证头像路径
        if not isinstance(self.avatar_path, str):
            self.avatar_path = ""

        # 验证浏览器路径
        if not isinstance(self.browser_path, str):
            self.browser_path = ""

        if not isinstance(self.show_ai_watermark, bool):
            self.show_ai_watermark = True
        if not isinstance(self.show_rating_watermark, bool):
            self.show_rating_watermark = True
        
        # 验证 network_mounts
        if not isinstance(self.network_mounts, list):
            self.network_mounts = []
        else:
            # 清理无效的挂载配置
            valid_mounts = []
            for mount in self.network_mounts:
                if isinstance(mount, dict):
                    try:
                        mount_obj = NetworkMount(**mount)
                        # 验证协议
                        if mount_obj.protocol not in ("smb", "ftp", "ftps", "sftp"):
                            continue
                        # 验证主机
                        if not mount_obj.host:
                            continue
                        valid_mounts.append(mount_obj)
                    except Exception as e:
                        logger.warning(f"无效的网络挂载配置: {e}")
                elif isinstance(mount, NetworkMount):
                    valid_mounts.append(mount)
            self.network_mounts = valid_mounts
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'AppConfig':
        """从字典创建配置"""
        # 只保留有效的字段
        valid_fields = {f.name for f in cls.__dataclass_fields__.values()}
        filtered_data = {k: v for k, v in data.items() if k in valid_fields}
        return cls(**filtered_data)


class ConfigManager:
    """配置管理器"""
    
    _instance: Optional['ConfigManager'] = None
    _config_file = CONFIG_DIR / "config.json"
    
    def __new__(cls, config_file=None) -> 'ConfigManager':
        """单例模式"""
        if config_file is not None:
            instance = super().__new__(cls)
            instance._initialized = False
            return instance
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(self, config_file=None) -> None:
        if self._initialized:
            return
        if config_file is not None:
            self._config_file = Path(config_file)
            
        self._config: AppConfig = AppConfig()
        self._auto_save: bool = True
        self._initialized = True
        
        logger.info(f"配置管理器初始化，配置文件: {self._config_file}")
    
    @property
    def config(self) -> AppConfig:
        """获取当前配置"""
        return self._config
    
    def load(self) -> AppConfig:
        """
        加载配置文件
        
        Returns:
            加载的配置对象
        """
        if not self._config_file.exists():
            logger.info("配置文件不存在，使用默认配置")
            self._config = AppConfig()
            return self._config
        
        try:
            with open(self._config_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            # 合并默认配置（处理新增的配置项）
            merged_data = {**DEFAULT_CONFIG, **data}
            self._config = AppConfig.from_dict(merged_data)
            
            logger.info("配置文件加载成功")
            logger.debug(f"配置内容: {self._config.to_dict()}")
            
        except json.JSONDecodeError as e:
            logger.error(f"配置文件格式错误: {e}")
            self._config = AppConfig()
        except Exception as e:
            logger.error(f"加载配置文件失败: {e}")
            self._config = AppConfig()
        
        return self._config
    
    def save(self) -> bool:
        """
        保存配置到文件
        
        Returns:
            是否保存成功
        """
        try:
            # 确保目录存在
            self._config_file.parent.mkdir(parents=True, exist_ok=True)
            
            with open(self._config_file, 'w', encoding='utf-8') as f:
                json.dump(self._config.to_dict(), f, indent=4, ensure_ascii=False)
            
            logger.info("配置文件保存成功")
            return True
            
        except Exception as e:
            logger.error(f"保存配置文件失败: {e}")
            return False
    
    def update(self, **kwargs: Any) -> None:
        """
        更新配置项
        
        Args:
            **kwargs: 要更新的配置项
        """
        for key, value in kwargs.items():
            if hasattr(self._config, key):
                setattr(self._config, key, value)
                logger.debug(f"配置项更新: {key} = {value}")
            else:
                logger.warning(f"未知的配置项: {key}")
        
        # 重新验证
        self._config.validate()
        
        # 自动保存
        if self._auto_save:
            self.save()
    
    def reset(self) -> None:
        """重置为默认配置"""
        self._config = AppConfig()
        logger.info("配置已重置为默认值")
        
        if self._auto_save:
            self.save()
    
    def set_auto_save(self, enabled: bool) -> None:
        """设置是否自动保存"""
        self._auto_save = enabled
        logger.debug(f"自动保存: {'启用' if enabled else '禁用'}")
    
    def get(self, key: str, default: Any = None) -> Any:
        """
        获取配置值
        
        Args:
            key: 配置键名
            default: 默认值
            
        Returns:
            配置值
        """
        return getattr(self._config, key, default)
    
    def set(self, key: str, value: Any) -> None:
        """
        设置配置值
        
        Args:
            key: 配置键名
            value: 配置值
        """
        self.update(**{key: value})


# 全局配置管理器实例
_config_manager: Optional[ConfigManager] = None


def get_config_manager() -> ConfigManager:
    """获取全局配置管理器实例"""
    global _config_manager
    if _config_manager is None:
        _config_manager = ConfigManager()
    return _config_manager


def get_config() -> AppConfig:
    """获取当前配置的便捷函数"""
    return get_config_manager().config

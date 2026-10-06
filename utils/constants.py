#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
常量定义模块
集中管理所有魔法数字和字符串常量
"""
from pathlib import Path
from typing import Dict, FrozenSet
import os

# ==================== 应用信息 ====================
APP_NAME = "Pixiv 图片查看器"
# 整个软件唯一的版本号：安装包文件名、“关于”页、下载模块都读这里。发版时改这一处，并在 CHANGELOG.md 里记一笔。
APP_VERSION = "1.1.3"
# 新版本发布在这个 GitHub 仓库的 Releases 里；“检查更新”从这里取（见 webapp/updater.py、scripts/release.py）
UPDATE_REPO = "cute-37/pixiv-viewer"
APP_AUTHOR = "Pixiv Viewer Team"
APP_YEAR = "2026"

# ==================== 路径常量 ====================
# 项目根目录
PROJECT_ROOT = Path(__file__).parent.parent.resolve()

# 数据目录（存储在项目本地）
DATA_DIR = Path(os.environ.get("PIXIV_VIEWER_DATA_DIR", PROJECT_ROOT / "data")).resolve()
CACHE_DIR = DATA_DIR / "cache"
LOG_DIR = DATA_DIR / "logs"
CONFIG_DIR = DATA_DIR / "config"

# Directories are created by their writers, never just by importing constants.

# ==================== 图片格式 ====================
IMAGE_EXTENSIONS: FrozenSet[str] = frozenset({
    '.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.jfif', '.tiff', '.tif'
})

# ==================== 缩略图设置 ====================
THUMBNAIL_SIZE_MIN = 80
THUMBNAIL_SIZE_MAX = 500
THUMBNAIL_SIZE_DEFAULT = 150
THUMBNAIL_SIZE_STEP = 20

# 缩略图质量 (JPEG)
THUMBNAIL_QUALITY = 90

# 缩略图渲染倍率 (提高清晰度)
THUMBNAIL_RENDER_SCALE = 1.5

# 悬停预览设置
HOVER_PREVIEW_SIZE = 400  # 预览窗口尺寸
HOVER_PREVIEW_DELAY = 300  # 延迟显示时间 (ms)

# ==================== 内存策略 ====================
class MemoryStrategy:
    """内存使用策略"""
    CONSERVATIVE = "conservative"
    MODERATE = "moderate"
    AGGRESSIVE = "aggressive"

# 缩略图内存缓存上限 (字节)。每张缩略图约 0.2MB，下列数值足够容纳数千张，
# 实际还受 MAX_MEMORY_CACHE_IMAGES（条目数）约束。
MEMORY_LIMITS: Dict[str, int] = {
    MemoryStrategy.CONSERVATIVE: 128 * 1024 * 1024,   # 128MB
    MemoryStrategy.MODERATE: 256 * 1024 * 1024,       # 256MB
    MemoryStrategy.AGGRESSIVE: 768 * 1024 * 1024,     # 768MB
}

# ==================== 性能优化设置 ====================
# 最大同时加载的图片数量
MAX_CONCURRENT_THUMBNAIL_LOADS = 4

# 最大内存缓存图片数量（视图可见性优先）
MAX_MEMORY_CACHE_IMAGES = 200

# 视图外图片清理阈值（距离当前视图的行数）
VIEW_OUTSIDE_CLEANUP_THRESHOLD = 50

# 图片查看器的解码缓存内存预算（字节）；另有张数上限
VIEWER_CACHE_MAX_BYTES = 400 * 1024 * 1024

# ==================== 窗口设置 ====================
WINDOW_MIN_WIDTH = 800
WINDOW_MIN_HEIGHT = 600
WINDOW_DEFAULT_WIDTH = 1400
WINDOW_DEFAULT_HEIGHT = 900

# 分割器比例
SPLITTER_LEFT_MIN_WIDTH = 200

# ==================== 缩放设置 ====================
ZOOM_FACTOR_IN = 1.25
ZOOM_FACTOR_OUT = 0.8
ZOOM_MIN = 0.1
ZOOM_MAX = 10.0

# ==================== 动画/定时器 ====================
NAV_BUTTON_HIDE_DELAY = 1200  # ms

# ==================== 线程池设置 ====================
THREAD_POOL_SIZE = 8
FOLDER_SCAN_BATCH_SIZE = 50
MAX_DEEP_SCAN_FILES = 500

# ==================== 排序模式 ====================
class SortMode:
    """排序模式"""
    BY_ID = "id"
    BY_TIME = "time"
    BY_NAME = "name"

# ==================== 间距预设 ====================
SPACING_PRESETS: Dict[str, int] = {
    "compact": 2,
    "standard": 8,
    "wide": 18,
}

# ==================== 颜色常量 ====================
class Colors:
    """颜色常量"""
    # 主题色
    PRIMARY = "#0096fa"
    PRIMARY_DARK = "#0077cc"
    PRIMARY_LIGHT = "#e3f2fd"
    
    # 背景色
    BG_LIGHT = "#f6f7fb"
    BG_DARK = "#1f1f1f"
    
    # 文字色
    TEXT_PRIMARY = "#2b2f36"
    TEXT_SECONDARY = "#666666"
    TEXT_DISABLED = "#999999"
    
    # 边框色
    BORDER_LIGHT = "#e5e7eb"
    BORDER_DARK = "#333333"
    
    # 选中色
    SELECTION_BG = "#e6f0ff"
    SELECTION_BORDER = "#c7ddff"
    
    # 查看器背景
    VIEWER_BG = "#2b2b2b"
    
    # 占位符
    PLACEHOLDER_BG = "#333333"
    PLACEHOLDER_TEXT = "#666666"

# ==================== 默认配置 ====================
DEFAULT_CONFIG = {
    "image_path": "",
    "last_folder": "",
    "window_geometry": None,
    "thumbnail_size": THUMBNAIL_SIZE_DEFAULT,
    "memory_strategy": MemoryStrategy.MODERATE,
    "theme": "light",
    "toolbar_big_icons": True,
    "toolbar_align": "center",
    "hide_artist_id": False,
    "sort_mode": SortMode.BY_ID,
    "spacing_mode": "standard",
    "pixiv_metadata_path": "",
    "show_ai_watermark": True,
    "show_rating_watermark": True,
    "network_mounts": [],
    "max_concurrent_thumbnail_loads": MAX_CONCURRENT_THUMBNAIL_LOADS,
}

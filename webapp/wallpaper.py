#!/usr/bin/env python
# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import ctypes
from utils.logger import get_logger

logger = get_logger("Features.Wallpaper")

def set_wallpaper(image_path: str) -> bool:
    """
    设置桌面壁纸
    
    Args:
        image_path: 图片路径
    
    Returns:
        是否成功
    """
    if os.name != 'nt':
        logger.warning("壁纸设置仅支持 Windows")
        return False
    
    try:
        # Windows API
        SPI_SETDESKWALLPAPER = 20
        SPIF_UPDATEINIFILE = 1
        SPIF_SENDCHANGE = 2
        
        ctypes.windll.user32.SystemParametersInfoW(
            SPI_SETDESKWALLPAPER, 0, 
            os.path.abspath(image_path),
            SPIF_UPDATEINIFILE | SPIF_SENDCHANGE
        )
        
        logger.info(f"壁纸设置成功: {image_path}")
        return True
    except Exception as e:
        logger.error(f"壁纸设置失败: {e}")
        return False

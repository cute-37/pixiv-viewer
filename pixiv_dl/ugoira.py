"""动图(Ugoira) zip → 动画 WebP。"""
import logging
import os
import tempfile
import zipfile

from pixiv_dl.config import Config

logger = logging.getLogger("PixivDownloader")


def convert_ugoira(zip_path, frames=None, out_dir=None):
    """把 ugoira zip 转成动画 WebP（默认 quality=90，可通过 UGOIRA_WEBP_LOSSLESS 改为无损；zip 原件始终会保存），返回 webp 路径。失败抛异常。

    frames: [{'file': 'xxx.jpg', 'delay': 60}, ...]（来自 ugoira_metadata）。缺省时按文件名排序、每帧 100ms。
    out_dir: 输出目录，调用方负责清理；缺省在 Config.LOCAL_TEMP_PATH 下新建临时目录。
    """
    from PIL import Image

    if not os.path.exists(zip_path):
        raise FileNotFoundError(f"ZIP 文件不存在: {zip_path}")
    if os.path.getsize(zip_path) < 100:
        raise ValueError("ZIP 文件过小，可能已损坏")

    if out_dir is None:
        os.makedirs(Config.LOCAL_TEMP_PATH, exist_ok=True)
        out_dir = tempfile.mkdtemp(prefix="ugoira_", dir=Config.LOCAL_TEMP_PATH)
    frames_dir = os.path.join(out_dir, "frames")
    os.makedirs(frames_dir, exist_ok=True)

    with zipfile.ZipFile(zip_path) as z:
        bad = z.testzip()
        if bad:
            raise ValueError(f"ZIP 文件损坏，损坏的文件: {bad}")
        names = z.namelist()
        if not names:
            raise ValueError("ZIP 文件为空")
        z.extractall(frames_dir)

    if not frames:
        files = sorted(n for n in os.listdir(frames_dir) if n.lower().endswith(('.png', '.jpg', '.jpeg')))
        frames = [{'file': n, 'delay': 100} for n in files]

    # 逐帧读取并立刻放进列表；帧很多时占内存，这里不额外复制
    images, durations = [], []
    for fr in frames:
        fname = fr.get('file') or ''
        path = os.path.join(frames_dir, fname)
        if not os.path.exists(path):
            matches = [x for x in os.listdir(frames_dir) if fname and x.endswith(os.path.basename(fname))]
            if not matches:
                logger.warning(f"帧文件不存在: {fname}")
                continue
            path = os.path.join(frames_dir, matches[0])
        try:
            with Image.open(path) as im:
                images.append(im.convert('RGBA'))
            durations.append(max(int(fr.get('delay') or 100), 10))
        except Exception as e:
            logger.warning(f"加载帧失败 {fname}: {e}")
    if not images:
        raise ValueError("未能从 zip 中读取到有效的帧图片")

    base = os.path.splitext(os.path.basename(zip_path))[0]
    out = os.path.join(out_dir, base + ".webp")
    images[0].save(out, format='WEBP', save_all=True, append_images=images[1:],
                   duration=durations, loop=0,
                   lossless=bool(getattr(Config, 'UGOIRA_WEBP_LOSSLESS', True)), quality=90, method=4)
    if not os.path.exists(out) or os.path.getsize(out) < 100:
        raise RuntimeError("WebP 生成失败或文件过小")
    return out

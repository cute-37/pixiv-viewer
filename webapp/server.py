#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
本机 HTTP 服务：提供网页界面文件、缩略图与原图

- 只监听 127.0.0.1，端口随机
- /thumb 与 /image 需要启动时生成的随机令牌，且只读取资料库根目录下的文件
- 缩略图用 Pillow 生成，缓存到 CACHE_DIR（键包含 路径 + 修改时间 + 大小 + 尺寸）
"""
from __future__ import annotations

import io
import json
import mimetypes
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import parse_qs, urlparse

from utils import thumbnail_cache
from utils.logger import get_logger

logger = get_logger("WebServer")

WEBUI_DIR = Path(__file__).resolve().parent.parent / "webui"
THUMB_SIZE = 512
# 返回内容可能很大的只读接口走 HTTP：pywebview 的桥会把结果拼成脚本塞回界面线程执行，
# 上万个作品的列表会让窗口卡住；fetch 是异步的，不影响点击和滚动
HTTP_METHODS = frozenset({"get_library", "list_works", "suggest", "get_details", "my_tag_list"})
STATIC_TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
                ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml",
                ".woff2": "font/woff2", ".ttf": "font/ttf", ".otf": "font/otf"}


def make_thumbnail(path: str, size: int = THUMB_SIZE) -> Optional[bytes]:
    """生成（或读取缓存的）缩略图 JPEG"""
    try:
        st = os.stat(path)
    except OSError:
        return None
    cached = thumbnail_cache.cache_path(path, st.st_mtime, st.st_size, size)
    if cached.exists():
        thumbnail_cache.touch(cached)
        try:
            return cached.read_bytes()
        except OSError:
            pass
    try:
        from PIL import Image, ImageOps
        with Image.open(path) as im:
            im = ImageOps.exif_transpose(im)
            im.draft("RGB", (size * 2, size * 2))
            if im.mode not in ("RGB", "L"):
                bg = Image.new("RGB", im.size, (255, 255, 255))
                rgba = im.convert("RGBA")
                bg.paste(rgba, mask=rgba.split()[-1])
                im = bg
            elif im.mode == "L":
                im = im.convert("RGB")
            im.thumbnail((size, size * 2), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=86, optimize=True)
            data = buf.getvalue()
    except Exception as e:
        logger.debug(f"生成缩略图失败 {path}: {e}")
        return None
    try:
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_bytes(data)
    except OSError:
        pass
    return data


# 同时生成缩略图的数量：Pillow 解码很吃 CPU，不加限制时会和界面、扫描线程抢资源
_thumb_slots = threading.BoundedSemaphore(3)


def _client_gone(req: BaseHTTPRequestHandler) -> bool:
    """浏览器已取消请求（缩略图滚出了可视范围）时，连接会被关闭，就不必再生成了"""
    try:
        import select
        import socket
        sock = req.connection
        readable, _, _ = select.select([sock], [], [], 0)
        if not readable:
            return False
        return sock.recv(1, socket.MSG_PEEK) == b""
    except (OSError, ValueError):
        return True


class MediaServer:
    def __init__(self, token: str, allowed: Callable[[str], bool], webui_dir: Path = WEBUI_DIR, api=None,
                 avatar: Optional[Callable[[int], Optional[Path]]] = None) -> None:
        self.avatar = avatar     # 画师 ID -> 头像文件（来自下载器）
        self.token = token
        self.allowed = allowed
        self.webui_dir = webui_dir
        self.api = api
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):  # 不往控制台刷访问日志
                pass

            def do_POST(self):
                try:
                    server.handle_api(self)
                except (ConnectionError, BrokenPipeError):
                    pass
                except Exception as e:
                    logger.error(f"接口调用失败 {self.path}: {e}")
                    try:
                        self.send_error(500)
                    except Exception:
                        pass

            def do_GET(self):
                try:
                    server.handle(self)
                except (ConnectionError, BrokenPipeError):
                    pass
                except Exception as e:
                    logger.error(f"请求处理失败 {self.path}: {e}")
                    try:
                        self.send_error(500)
                    except Exception:
                        pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.thread: Optional[threading.Thread] = None
        # 缩略图单独用一个端口：浏览器对同一个地址最多同时开 6 个连接，几十张缩略图排队时
        # 会把数据接口和大图的请求一起堵住（点侧栏没反应、打开图片要等）
        self.thumbd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thumbd.daemon_threads = True
        self.thumb_port = self.thumbd.server_address[1]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="media-server", daemon=True)
        self.thread.start()
        threading.Thread(target=self.thumbd.serve_forever, name="thumb-server", daemon=True).start()
        logger.info(f"界面服务已启动: {self.url}")

    def stop(self) -> None:
        for httpd in (self.httpd, self.thumbd):
            httpd.shutdown()
            httpd.server_close()

    # ---------- 路由 ----------
    def handle(self, req: BaseHTTPRequestHandler) -> None:
        url = urlparse(req.path)
        if url.path == "/avatar":
            qs = parse_qs(url.query)
            if qs.get("t", [""])[0] != self.token:
                return req.send_error(403)
            aid = qs.get("id", [""])[0]
            path = self.avatar(int(aid)) if self.avatar and aid.isdigit() else None
            if path is None:
                return req.send_error(404)
            return self._send_file(req, str(path))
        if url.path in ("/thumb", "/image"):
            qs = parse_qs(url.query)
            if qs.get("t", [""])[0] != self.token:
                return req.send_error(403)
            path = qs.get("path", [""])[0]
            if not path or not self.allowed(path):
                return req.send_error(403)
            if url.path == "/thumb":
                with _thumb_slots:
                    if _client_gone(req):
                        return None
                    data = make_thumbnail(path)
                if data is None:
                    return req.send_error(404)
                return self._send(req, data, "image/jpeg", cache=True)
            return self._send_file(req, path)
        return self._send_static(req, url.path)

    def handle_api(self, req: BaseHTTPRequestHandler) -> None:
        url = urlparse(req.path)
        name = url.path[len("/api/"):] if url.path.startswith("/api/") else ""
        if req.headers.get("X-PV-Token") != self.token:
            return req.send_error(403)
        if self.api is None or name not in HTTP_METHODS:
            return req.send_error(404)
        length = int(req.headers.get("Content-Length") or 0)
        args = json.loads(req.rfile.read(length) or b"[]") if length else []
        if not isinstance(args, list):
            return req.send_error(400)
        if name == "list_works":
            data = self.api._list_works_json(args[0] if args else {})
        else:
            data = json.dumps(getattr(self.api, name)(*args), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._send(req, data, "application/json; charset=utf-8")

    def _send(self, req, data: bytes, ctype: str, cache: bool = False) -> None:
        req.send_response(200)
        req.send_header("Content-Type", ctype)
        req.send_header("Content-Length", str(len(data)))
        req.send_header("Cache-Control", "private, max-age=86400" if cache else "no-cache")
        req.end_headers()
        req.wfile.write(data)

    def _send_file(self, req, path: str) -> None:
        try:
            size = os.path.getsize(path)
            ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
            req.send_response(200)
            req.send_header("Content-Type", ctype)
            req.send_header("Content-Length", str(size))
            req.send_header("Cache-Control", "private, max-age=3600")
            req.end_headers()
            with open(path, "rb") as f:
                shutil_copy(f, req.wfile)
        except OSError:
            req.send_error(404)

    def _send_static(self, req, path: str) -> None:
        rel = path.lstrip("/") or "index.html"
        target = (self.webui_dir / rel).resolve()
        if not str(target).startswith(str(self.webui_dir.resolve())) or not target.is_file():
            return req.send_error(404)
        data = target.read_bytes()
        if target.name == "index.html":
            # 网页文件本身不含 doctype（同一份文件也用于浏览器预览），这里补上，并告诉前端是桌面版
            boot = (f'<script>window.__PV_DESKTOP__=true;window.__PV_TOKEN__="{self.token}";'
                    f'window.__PV_THUMB__="http://127.0.0.1:{self.thumb_port}";</script>')
            data = b"<!doctype html>\n" + data.replace(b'<script type="module"', boot.encode() + b'\n<script type="module"', 1)
        ctype = STATIC_TYPES.get(target.suffix.lower()) or mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self._send(req, data, ctype)


def shutil_copy(src, dst, chunk: int = 1 << 16) -> None:
    while True:
        buf = src.read(chunk)
        if not buf:
            break
        dst.write(buf)

import argparse

from pixiv_dl.web.server import serve

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Pixiv 下载器 Web 前端")
    ap.add_argument("--host", default=None, help="监听地址，默认 127.0.0.1")
    ap.add_argument("--port", type=int, default=None, help="端口，默认 8765")
    ap.add_argument("--open", action="store_true", help="启动后打开浏览器")
    a = ap.parse_args()
    serve(a.host, a.port, open_browser=a.open)

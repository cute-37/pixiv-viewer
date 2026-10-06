# -*- coding: utf-8 -*-
"""
下载功能的工作进程（由 webapp.downloader.DownloaderBridge 启动，不要直接运行）

    python -m webapp.dl_worker <数据目录> [代码目录]

在独立进程里运行项目内置的下载模块（pixiv_dl），不启动网页服务、不占用端口：
父进程从标准输入逐行发来 JSON 请求，这里直接调用下载器后端的处理函数，再把结果逐行写回标准输出。

    请求  {"id": 1, "method": "GET", "path": "/api/plan", "query": {"limit": "5"}, "body": {...}}
    应答  {"id": 1, "ok": true, "data": {...}}   或   {"id": 1, "ok": false, "status": 409, "error": "..."}

放在单独的进程里有两个原因：下载、同步时很忙，不能拖慢看图这边；下载出问题也不会带垮看图。
数据目录（账号、设置、数据库、头像）通过环境变量 PIXIV_DL_HOME 交给 pixiv_dl.config。
第二个参数只在测试里用：从别的目录加载一个假的 pixiv_dl。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor


def main(home: str, code_root: str = "") -> int:
    # 标准输出留给协议专用：先把它另存一份，再把进程里其他所有输出（print、进度条、日志）改道到标准错误。
    # 打包成窗口程序后 sys.stdout / sys.stdin 可能是 None，所以直接用文件描述符 0、1、2。
    proto = os.fdopen(os.dup(1), "wb")
    try:
        os.dup2(2, 1)
        sys.stderr = sys.stderr or open(2, "w", encoding="utf-8", errors="replace", closefd=False)
    except OSError:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    sys.stdout = sys.stderr
    stdin = sys.stdin.buffer if sys.stdin is not None else open(0, "rb", closefd=False)
    write_lock = threading.Lock()

    def send(obj: dict) -> None:
        data = json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8") + b"\n"
        with write_lock:
            proto.write(data)
            proto.flush()

    try:
        os.makedirs(home, exist_ok=True)
        os.environ["PIXIV_DL_HOME"] = home          # 必须在导入 pixiv_dl 之前
        if code_root:
            sys.path.insert(0, code_root)
        from pixiv_dl import interrupt
        from pixiv_dl.applog import setup_logging
        from pixiv_dl.web import server as srv
        setup_logging(console=False, to_file=True)
        app = srv.App()
    except Exception as e:  # 下载器加载失败：告诉父进程原因后退出
        send({"event": "fatal", "error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()[-1500:]})
        return 1

    def handle(req: dict) -> None:
        rid = req.get("id")
        try:
            method, path = str(req.get("method") or "GET").upper(), str(req.get("path") or "")
            query = {k: [str(v)] for k, v in (req.get("query") or {}).items() if v is not None}
            for m, rx, fn in srv.ROUTES:
                match = rx.match(path)
                if match and m == method:
                    result = fn(srv.Request(app, match, query, req.get("body") or {}))
                    if isinstance(result, srv.Raw):
                        return send({"id": rid, "ok": False, "status": 415, "error": "这个接口返回的是文件，不能这样调用"})
                    return send({"id": rid, "ok": True, "data": result})
            send({"id": rid, "ok": False, "status": 404, "error": "接口不存在"})
        except srv.HttpError as e:
            send({"id": rid, "ok": False, "status": e.status, "error": e.message})
        except Exception as e:
            srv.logger.error(f"处理 {req.get('path')} 失败: {type(e).__name__}: {e}")
            send({"id": rid, "ok": False, "status": 500, "error": f"{type(e).__name__}: {e}"})

    send({"event": "ready", "version": getattr(srv, "VERSION", "")})
    pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="dl-rpc")
    for line in stdin:                 # 父进程退出或关闭管道时这里结束
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line.decode("utf-8"))
        except ValueError:
            continue
        if req.get("op") == "quit":
            break
        pool.submit(handle, req)
    # 收尾：让正在跑的任务在当前文件处理完后停下（下载器是原子写入，不会留下半个文件）
    interrupt.set()
    runner = getattr(app.runner, "_thread", None)
    if runner is not None:
        runner.join(timeout=20)
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:3]))

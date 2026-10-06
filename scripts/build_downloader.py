# -*- coding: utf-8 -*-
"""把独立的 Pixiv 下载器（dl_main.py）打包成免安装的 Windows 程序

    python scripts/build_downloader.py

产物：dist/PixivDownloader/（文件夹）和 dist/PixivDownloader-<版本>-win64.zip
需要 PyInstaller（pip install pyinstaller）。包里只有程序和界面文件，不含任何账号、设置或数据。
"""
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
WORK = ROOT / "build" / "downloader"

README = """Pixiv 下载器（Windows 64 位，免安装）

【怎么用】
1. 把整个文件夹放到你想放的位置（不要只拿走 exe，_internal 文件夹必须和它在一起）。
2. 双击 PixivDownloader.exe。
3. 第一次使用：
   - 「账号」：添加 Pixiv 账号（浏览器登录授权，或粘贴 refresh token）。
   - 「保存位置」：选择下载的文件放在哪里（默认在本文件夹的 data\downloads 里）。
   - 「更新」：点「检查更新并下载」。会先说明要做什么，确认后才开始。

【数据放在哪】
运行后会在本文件夹里生成 data 文件夹：账号和设置（settings.json，含登录凭证，不要发给别人）、
作品数据库、头像、日志，以及默认的下载目录。
想换电脑或备份，把 data 整个拷走即可；想恢复成全新状态，关闭程序后删掉它。

【其他】
- 需要系统里有 Microsoft Edge WebView2 运行时。Windows 11 自带；Windows 10 一般也已随 Edge 装好，
  没有的话到微软官网搜索 “WebView2 Runtime” 下载安装。
- 第一次运行时 Windows 可能提示“未知发布者”，点“更多信息 → 仍要运行”。程序没有数字签名。
- 不会自动检查更新：只有你点了“开始”才会访问 Pixiv。
"""


def main() -> int:
    env = dict(os.environ)
    # conda 环境里 sqlite、OpenSSL 等动态库在 Library/bin，要让打包工具找得到
    prefix = Path(sys.executable).parent
    env["PATH"] = os.pathsep.join([str(prefix / "Library" / "bin"), str(prefix), env.get("PATH", "")])
    out = DIST / "PixivDownloader"
    shutil.rmtree(out, ignore_errors=True)
    cmd = [
        sys.executable, "-m", "PyInstaller", str(ROOT / "dl_main.py"), "--name", "PixivDownloader", "--noconfirm", "--clean",
        "--onedir", "--windowed", "--icon", str(ROOT / "webui" / "app.ico"),
        "--distpath", str(DIST), "--workpath", str(WORK), "--specpath", str(WORK),
        "--add-data", f"{ROOT / 'webui'}{os.pathsep}webui",
        "--hidden-import", "paramiko", "--hidden-import", "boto3", "--hidden-import", "smb.SMBConnection", "--hidden-import", "socks",
        "--collect-data", "botocore", "--collect-data", "boto3", "--collect-all", "cloudscraper",
    ]
    for mod in ("PyQt5", "tkinter", "numpy", "matplotlib", "pytest", "IPython"):
        cmd += ["--exclude-module", mod]
    r = subprocess.run(cmd, cwd=str(ROOT), env=env)
    if r.returncode != 0:
        return r.returncode
    (out / "使用说明.txt").write_text(README, encoding="utf-8")

    sys.path.insert(0, str(ROOT))
    from pixiv_dl.config import VERSION
    target = DIST / f"PixivDownloader-{VERSION}-win64.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for path in sorted(out.rglob("*")):
            if path.is_file():
                z.write(path, path.relative_to(DIST))
    print(f"已生成 {target}（{target.stat().st_size / 2**20:.1f} MB）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

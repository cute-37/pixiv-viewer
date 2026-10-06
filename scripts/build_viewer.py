# -*- coding: utf-8 -*-
"""把查看器（web_main.py，含内置的下载功能）打包成免安装的 Windows 程序

    python scripts/build_viewer.py

产物：dist/PixivViewer/（文件夹）和 dist/PixivViewer-<版本>-win64.zip
需要 PyInstaller（pip install pyinstaller）。包里只有程序和界面文件，不含任何配置、账号或数据；
第一次运行时一切都是默认设置。
"""
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
WORK = ROOT / "build" / "viewer"

README = """Pixiv Viewer（Windows 64 位，免安装）

【怎么用】
1. 把整个文件夹放到你想放的位置（不要只拿走 exe，_internal 文件夹必须和它在一起）。
2. 双击 PixivViewer.exe。
3. 第一次使用：
   - 已经有图片：点左下角「添加文件夹」，选择存放图片的文件夹（里面每个子文件夹算一位画师）。
   - 想从 Pixiv 下载：点左下角「下载与更新」，先在「账号」页添加 Pixiv 账号，在「保存位置」页
     选好保存到哪里并点「加入资料库」，然后回到「更新」页点「检查更新并下载」。

【数据放在哪】
运行后会在本文件夹里生成 data 文件夹：界面设置、评分与标签、缩略图缓存、日志，
以及下载功能的账号、设置、数据库（data\pixiv，其中 settings.json 含登录凭证，不要发给别人）。
想换电脑或备份，把 data 整个拷走即可；想恢复成全新状态，关闭程序后删掉它。

【其他】
- 需要系统里有 Microsoft Edge WebView2 运行时。Windows 11 自带；Windows 10 一般也已随 Edge 装好，
  没有的话到微软官网搜索 “WebView2 Runtime” 下载安装。
- 第一次运行时 Windows 可能提示“未知发布者”，点“更多信息 → 仍要运行”。程序没有数字签名。
- 不会自动检查更新或下载：只有你点了“开始”才会访问 Pixiv。
- 软件本身的新版本：在「设置 → 关于」点「检查更新」，可以直接下载并替换（data 文件夹不会被改动）。
- 快捷键：按 ? 查看。
"""


def main() -> int:
    env = dict(os.environ)
    # conda 环境里 sqlite、OpenSSL 等动态库在 Library/bin，要让打包工具找得到
    prefix = Path(sys.executable).parent
    env["PATH"] = os.pathsep.join([str(prefix / "Library" / "bin"), str(prefix), env.get("PATH", "")])
    out = DIST / "PixivViewer"
    shutil.rmtree(out, ignore_errors=True)
    cmd = [
        sys.executable, "-m", "PyInstaller", str(ROOT / "web_main.py"), "--name", "PixivViewer", "--noconfirm", "--clean",
        "--onedir", "--windowed", "--icon", str(ROOT / "webui" / "app.ico"),
        "--distpath", str(DIST), "--workpath", str(WORK), "--specpath", str(WORK),
        "--add-data", f"{ROOT / 'webui'}{os.pathsep}webui",
        # 下载模块里按需导入的库
        "--hidden-import", "paramiko", "--hidden-import", "boto3", "--hidden-import", "smb.SMBConnection",
        "--collect-data", "botocore", "--collect-data", "boto3", "--collect-all", "cloudscraper",
        # 系统凭据库（保存网络共享的密码）
        "--collect-submodules", "keyring", "--hidden-import", "win32ctypes.core",
    ]
    for mod in ("PyQt5", "tkinter", "numpy", "matplotlib", "pytest", "IPython"):
        cmd += ["--exclude-module", mod]
    r = subprocess.run(cmd, cwd=str(ROOT), env=env)
    if r.returncode != 0:
        return r.returncode
    (out / "使用说明.txt").write_text(README, encoding="utf-8")

    sys.path.insert(0, str(ROOT))
    from utils.constants import APP_VERSION
    target = DIST / f"PixivViewer-{APP_VERSION}-win64.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for path in sorted(out.rglob("*")):
            if path.is_file():
                z.write(path, path.relative_to(DIST))
    print(f"已生成 {target}（{target.stat().st_size / 2**20:.1f} MB）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

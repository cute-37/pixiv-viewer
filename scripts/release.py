#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""发布一个新版本到 GitHub Releases（软件里的“检查更新”就是从那里取的）。

发版前先做好这两件事：改 utils/constants.py 的 APP_VERSION；在 CHANGELOG.md 顶部写好这一版的变化。然后：

    python scripts/release.py              # 打包查看器、推送代码、创建 Release（会先列出要做的事并等你确认）
    python scripts/release.py --skip-build # 直接用 dist/ 里已经打好的包
    python scripts/release.py --dry-run    # 只检查和显示，不推送、不发布

需要装好并登录 GitHub 命令行工具（gh auth login）。发布的内容是公开的，发出去就收不回来，所以每次都会问一遍。
"""
import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def run(cmd, check=True, capture=False):
    result = subprocess.run(cmd, cwd=str(ROOT), text=True, encoding="utf-8", capture_output=capture)
    if check and result.returncode != 0:
        sys.exit(f"命令失败: {' '.join(map(str, cmd))}\n{result.stderr or ''}")
    return result


def changelog_section(version: str) -> str:
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    match = re.search(rf"^## {re.escape(version)}\s*\n(.*?)(?=^## |\Z)", text, re.S | re.M)
    return match.group(1).strip() if match else ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--skip-build", action="store_true", help="不重新打包，用 dist/ 里现有的")
    parser.add_argument("--dry-run", action="store_true", help="只检查，不推送也不发布")
    parser.add_argument("--yes", action="store_true", help="不询问，直接发布")
    parser.add_argument("--notes", help="发布页面上的说明；不给就用 CHANGELOG.md 里这一版的内容（软件里“检查更新”时会显示它）")
    args = parser.parse_args()

    from utils.constants import APP_VERSION, UPDATE_REPO
    tag = f"v{APP_VERSION}"
    notes = changelog_section(APP_VERSION)
    shown = args.notes or notes
    # 只发布查看器；只有下载功能的小程序（scripts/build_downloader.py）不随版本发布
    packages = [ROOT / "dist" / f"PixivViewer-{APP_VERSION}-win64.zip"]

    problems = []
    if not notes:
        problems.append(f"CHANGELOG.md 里没有 “## {APP_VERSION}” 这一节")
    if run(["git", "status", "--porcelain"], capture=True).stdout.strip():
        problems.append("有还没提交的改动：先提交，发布的包才和代码对得上")
    existing = run(["gh", "release", "view", tag, "--repo", UPDATE_REPO], check=False, capture=True)
    if existing.returncode == 0:
        problems.append(f"{UPDATE_REPO} 上已经有 {tag} 了：要发新版本请先改版本号")
    if problems:
        sys.exit("不能发布：\n- " + "\n- ".join(problems))

    if not args.skip_build:
        run([sys.executable, "scripts/build_viewer.py"])
    missing = [p.name for p in packages if not p.is_file()]
    if missing:
        sys.exit(f"dist/ 里没有 {', '.join(missing)}")

    print(f"\n将要发布 {tag} 到 https://github.com/{UPDATE_REPO}（公开）")
    for package in packages:
        print(f"  {package.name}  {package.stat().st_size / 2**20:.1f} MB")
    print("发布说明：\n" + "\n".join("  " + line for line in shown.splitlines()))
    if args.dry_run:
        print("\n(--dry-run：到此为止)")
        return 0
    if not args.yes and input("\n确认发布？输入 yes 继续: ").strip().lower() != "yes":
        print("已取消")
        return 1

    branch = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], capture=True).stdout.strip()
    run(["git", "push", "origin", branch])
    notes_file = ROOT / "build" / "release-notes.md"
    notes_file.parent.mkdir(exist_ok=True)
    notes_file.write_text(shown + "\n", encoding="utf-8")
    run(["gh", "release", "create", tag, *map(str, packages), "--repo", UPDATE_REPO, "--target", branch,
         "--title", f"{APP_VERSION}", "--notes-file", str(notes_file)])
    print(f"\n已发布：https://github.com/{UPDATE_REPO}/releases/tag/{tag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

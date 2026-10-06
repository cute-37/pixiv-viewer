# 开发说明

给想看代码、改代码或自己打包的人。软件怎么用见 [README](../README.md)。

## 从源码运行

```bash
pip install -r requirements.txt
python web_main.py        # 或双击 run_web.bat
```

界面用网页技术实现（`webui/`），由 pywebview 在系统自带的 WebView2 中显示；资料库、缩略图、评分、标签由 Python 后端（`webapp/`）提供。
下载在独立的子进程里运行（`webapp/dl_worker.py`），通过管道通信，不占用网络端口。

## 项目结构

```
image_viewer/
├── web_main.py          # 查看器入口（pywebview 窗口）
├── dl_main.py           # 独立下载器程序的入口（同一套下载界面，不带看图）
├── webui/               # 前端：HTML / CSS / JS，不需要构建
│   ├── index.html, downloader.html
│   ├── css/             # 按功能分文件，在页面里按顺序加载：
│   │                    #   base → sidebar → toolbar → grid → viewer → overlays → responsive → desktop → downloader
│   │                    #   fonts.css 由 scripts/fetch_fonts.py 生成
│   ├── fonts/           # 随程序发布的默认字体（霞鹜文楷 TC、M PLUS Rounded 1c）
│   └── js/
│       ├── main.js      # 入口：启动、快捷键、拖动调整宽度
│       ├── state.js     # 共用状态 ctx / view
│       ├── prefs.js     # 设置的保存与应用
│       ├── sidebar.js   # 侧栏、画师文件夹、拖动
│       ├── toolbar.js   # 标题、搜索建议、标签条、筛选
│       ├── grid.js      # 作品网格、选择、批量操作
│       ├── menu.js      # 右键菜单
│       ├── viewer.js, hover.js, dialogs.js, downloader.js, winctl.js, lazy.js
│       ├── settings.js  # 外观设置的定义（风格、密度、字体）
│       ├── api.js       # 与后端通信；没有后端时（浏览器预览）换成 mock.js 的示例数据
│       └── mock.js
├── webapp/              # 后端：资料库索引、前端接口、本机图片服务、导入、下载子进程的通信
├── pixiv_dl/            # 内置的 Pixiv 下载模块（同步、下载、多种存储方式、账号可见性检测）
├── utils/               # 通用模块：配置、日志、数据库、缩略图缓存、网络挂载、数据库结构版本（schema.py）
├── scripts/             # 打包、发布与维护脚本（见下）
└── tests/               # 测试：后端（tests/、tests/pixiv_dl/）和前端（tests/ui/）
```

`pixiv_dl/` 最初来自独立的 pixiv_downloader 项目，并入后已经在这里继续开发（可见性检测、失败判定等只在这边有）。
**以本项目里的这份为准**，不要再从原项目同步代码。

## 开发与维护

### 验证

```bash
python -m pytest -q        # 全部测试
python -m ruff check .     # 静态检查（pip install -e ".[dev]" 安装 ruff）
```

- 测试在收集之前自动切到临时数据目录，不读写真实配置、凭据或图片库，也不会访问 Pixiv。
- `tests/ui/` 是前端测试：用真实浏览器打开 `webui/`（示例数据），像用户一样点击、双击、拖动、按键，并检查页面脚本没有报错。
  需要 `pip install playwright`，浏览器用系统自带的 Edge；缺少时这一组自动跳过。改界面后至少跑一遍这一组。
- 单独调界面：`python -m http.server -d webui`，浏览器打开 `index.html`（示例数据，加 `?works=20000` 可以模拟大库）。

### 发版

1. 改 `utils/constants.py` 的 `APP_VERSION`（唯一的版本号，安装包文件名、“关于”页、下载模块都读它）。
2. 在 `CHANGELOG.md` 顶部记下这一版改了什么。
3. `python scripts/check_probes.py`：检查内置的 R-18 / R-18G 测试作品是否还有效（只读，访问 Pixiv 三十来次）。
4. 跑测试，提交。
5. `python scripts/release.py`：打包查看器（产物在 `dist/`，不含任何个人数据）、推送代码、在 GitHub 上创建 Release 并附上压缩包。
   发布是公开的，脚本会先列出要发的内容并等你输入 yes；`--dry-run` 只检查不发布。

已经装好的程序通过“检查更新”拿到新版本：它读 `UPDATE_REPO`（`utils/constants.py`）这个仓库最新的 Release，
下载名字以 `PixivViewer-` 开头的压缩包。所以**压缩包的名字和里面的文件夹结构不要改**，
Release 的标签用 `v版本号`。更新的实现和替换失败时的回滚见 `webapp/updater.py`。只想单独打包不发布时，
直接运行 `scripts/build_viewer.py`。只有下载功能的小程序（`dl_main.py`）可以用 `scripts/build_downloader.py` 自己打包，它不随版本发布。

README 里的截图由 `python scripts/make_screenshots.py` 生成（示例数据，两倍像素密度），界面改动后重新跑一遍。

### 约定

- **改数据库表结构**：不要改已有的建表语句，在对应的步骤列表末尾追加一步（`webapp/store.py` 的 `SCHEMA`、
  `utils/database.py` 的 `_init_db`），打开数据库时会自动把旧文件升上来。机制见 `utils/schema.py`。
- **样式**：颜色、尺寸只用 `css/base.css` 里的变量；新样式放进对应功能的文件，不要追加到别的文件末尾。
- **前端模块**：跨模块的函数直接 `import`；`viewer.js`、`dialogs.js` 等通过 `ctx` 上挂的函数调用。
- **字体**：默认的两款随程序发布；`settings.js` 里带 `web` 的几款只在选用时联网加载。更新字体文件用 `scripts/fetch_fonts.py`。
- **下载功能**：任何更新 / 下载都必须由用户确认后才开始，不能自动运行；测试里不要启动真实的同步。

## 数据目录

所有数据在项目（或安装包）自己的 `data/` 下；设置环境变量 `PIXIV_VIEWER_DATA_DIR` 可以改用别的目录。

```
data/
├── config/        # 设置
├── cache/         # 缩略图缓存（自动清理过旧或超量的部分）
├── logs/          # 日志
├── library.db     # 评分、自定义标签
├── webapp.db      # 收藏、最近查看、置顶、画师文件夹
└── pixiv/         # 下载功能的数据（账号与下载设置、作品数据库、头像、默认的下载位置）
```

## 网络挂载与密码

网络挂载密码保存在系统凭据库（Windows 凭据管理器），配置文件里只有 `keyring:` 引用。Windows 下通过 Win32 API 挂载 SMB，密码不会出现在命令行里。系统凭据库不可用时回退到本地密钥加密（安全性较低，日志会有警告）。

## 技术栈

Python 3.9+、pywebview（WebView2）、Pillow、pixivpy3；前端是不经构建的原生 HTML / CSS / JS。

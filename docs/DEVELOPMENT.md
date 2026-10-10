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

### 重要更新提醒（notices.json）

仓库根目录的 `notices.json` 是给已经装出去的旧版本看的。软件启动时和每天一次读它（`webapp/notices.py`），
把“针对当前版本”的通知显示出来。平时保持 `{"notices": []}`。出了必须让用户更新的事（比如 Pixiv 改了接口）时：
先修好并发布新版本，再往里加一条并推送到 `main`：

```json
{"notices": [{"id": "2026-11-api", "below": "1.2.0", "level": "important",
  "title": {"zh": "这个版本的下载功能已经不能用了", "en": "Downloading no longer works in this version"},
  "text": {"zh": "Pixiv 调整了接口，请更新到最新版本。", "en": "Pixiv changed its API. Please update."}}]}
```

`below`：低于这个版本的才提醒；`level`：`important` 打开软件就弹出，`download` 只在下载面板里提示。
`tests/test_notices.py` 会检查这个文件的格式，写错了测试会失败。

### Pixiv 接口体检

`pixiv_dl/apicheck.py` 里有一张清单（`CONTRACT`）：软件用到的每个 Pixiv 接口、以及代码依赖的字段和类型。
代码里新用了某个字段，就往清单里补一条。体检对每个接口各发一次只读请求并对照检查：

```
python scripts/check_api.py            # 发版前跑一次；有接口变了会以非零状态退出
```

软件里打开开发者模式（“设置 → 关于”）后，“设置 → 开发者”里也能跑。接口变了：修代码 → 发新版本 →
往 `notices.json` 里加一条（见上一节），旧版本的用户下次启动就会看到提醒。

### 给 AI 助手用的接口（MCP）

`webapp/mcp_server.py` 是转发程序（`PixivViewer.exe --mcp`，标准输入输出上的 JSON-RPC），它把工具调用转给正在运行的窗口；
真正的实现和工具清单在 `webapp/mcp_tools.py`（`TOOLS`）。加工具：在清单里加一行（名字、需要的级别、给模型看的英文说明、参数），
再写一个 `_t_<名字>` 方法。级别 `read / edit / full` 由用户在设置里选，默认 `off`。
返回给模型的作品标题、说明、标签是不可信的文字，只当数据返回；会动 Pixiv 或文件的操作一律走“先 plan 再 start”。

### 界面语言

界面文字在代码里用中文写，显示时由 `webui/js/i18n.js` 按对照表换成英文（`i18n_en.js`）或日语（`i18n_ja.js`）。
两张表的条目要一一对应：往英文表里加了一条，日语表也要加（测试会检查）；日语表里没有的会退回英文。
加了新的界面文字，就往对照表里补一条（数字写成 `{0}`，带名字的句子写成正则）；漏了只是那一处仍显示中文。
找漏掉的：`set PV_I18N_COLLECT=seen.jsonl` 后跑 `pytest tests/ui`，再 `node scripts/check_i18n.mjs seen.jsonl`。
托盘菜单、系统通知这些原生显示的文字在 Python 里用 `utils/lang.py` 的 `pick("中文", "English")`。日志不翻译。

### 发版前的冒烟检查

```
python scripts/build_viewer.py && python scripts/smoke_packaged.py
```

在打包好的程序、真实的窗口里把主要功能走一遍（语言、设置各页、备份、开发者模式、MCP、重启后的扫描结果等），
用临时数据，不碰真实数据也不访问 Pixiv。单元测试和浏览器预览发现不了“打包之后才出”的问题，所以发版前跑一次。

### 约定

- **改数据库表结构**：不要改已有的建表语句，在对应的步骤列表末尾追加一步（`webapp/store.py` 的 `SCHEMA`、
  `utils/database.py` 的 `_init_db`），打开数据库时会自动把旧文件升上来。机制见 `utils/schema.py`。
- **样式**：颜色、尺寸只用 `css/base.css` 里的变量；新样式放进对应功能的文件，不要追加到别的文件末尾。
- **前端模块**：跨模块的函数直接 `import`；`viewer.js`、`dialogs.js` 等通过 `ctx` 上挂的函数调用。
- **字体**：默认的两款随程序发布；`settings.js` 里带 `web` 的几款只在选用时联网加载。更新字体文件用 `scripts/fetch_fonts.py`。
- **下载功能**：任何更新 / 下载都必须由用户确认后才开始，不能自动运行；测试里不要启动真实的同步。
- **限速**：对 Pixiv 接口的调用都经过 `PixivClient.call`，它在每次请求前过一道按账号分的“闸”（`pixiv_dl/ratelimit.py`）。
  新写的循环里要在开始下一件事之前调用 `interrupt.wait_if_paused()`，这样“暂停”才管得到它。
- **断网**：网络类的错误先问 `pixiv_dl/netwatch.py` 网络通不通；不通就等，不要直接记失败。
- **代理**：访问 Pixiv 的请求都用 `Config.PROXIES`；长期使用的 `requests.Session` 要用 `pixiv_dl.proxy.configure_session()` 设置，不要直接赋值 `session.proxies`（那样“不使用代理”会被系统代理盖掉，原因见该函数的说明）。

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

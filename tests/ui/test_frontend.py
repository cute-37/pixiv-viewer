"""前端关键流程：网格、看图页、侧边栏文件夹、设置。全部走真实的鼠标 / 键盘事件。"""
import re

import pytest


def folder(page, name):
    return page.locator("#artists [data-folder]", has_text=name).first


def is_open(page, name):
    return "open" in (folder(page, name).get_attribute("class") or "").split()


def members(page, name):
    """文件夹里的画师数（侧栏上显示的数字）"""
    return int(folder(page, name).locator(".n").inner_text())


# ---------------- 网格与看图页 ----------------
def test_grid_renders_tiles(page):
    assert page.locator("#grid-root .tile").count() > 10
    assert page.locator("#viewer").is_hidden()


def test_double_click_opens_viewer_and_escape_closes(page):
    page.locator("#grid-root .tile").first.dblclick()
    page.wait_for_selector("#viewer:not([hidden])")
    assert page.locator("#v-name").inner_text().strip()
    page.keyboard.press("Escape")
    page.wait_for_selector("#viewer", state="hidden")


def test_viewer_arrow_keys_change_work(page):
    page.locator("#grid-root .tile").first.dblclick()
    page.wait_for_selector("#viewer:not([hidden])")
    first = page.locator("#v-idx").inner_text()
    page.keyboard.press("ArrowRight")
    page.wait_for_function("t => document.querySelector('#v-idx').innerText !== t", arg=first)
    page.keyboard.press("ArrowLeft")
    page.wait_for_function("t => document.querySelector('#v-idx').innerText === t", arg=first)


def test_click_selects_tile(page):
    tile = page.locator("#grid-root .tile").nth(2)
    tile.click()
    assert "sel" in tile.get_attribute("class").split()
    assert page.locator("#viewer").is_hidden()


def test_search_filters_grid(page):
    before = page.locator("#status").inner_text()
    page.fill("#q", "zzzz-不存在的关键词")
    page.keyboard.press("Enter")
    page.wait_for_function("t => document.querySelector('#status').innerText !== t", arg=before)
    assert page.locator("#grid-root .tile").count() == 0


# ---------------- 侧边栏：文件夹 ----------------
def test_folder_single_click_opens_folder_page(page):
    folder(page, "风景").click()
    page.wait_for_function("document.title.startsWith('风景')")


def test_folder_double_click_toggles(page):
    # 第一下单击会重绘侧栏，所以这里必须用真实的双击来测（见 main.js 里按连击次数判断的说明）
    start = is_open(page, "风景")
    folder(page, "风景").dblclick()
    page.wait_for_function("s => document.querySelector('#artists [data-folder]').classList.contains('open') !== s", arg=start)
    folder(page, "风景").dblclick()
    page.wait_for_function("s => document.querySelector('#artists [data-folder]').classList.contains('open') === s", arg=start)


def test_folder_rapid_clicks_toggle_on_every_second_click(page):
    start = is_open(page, "风景")
    box = folder(page, "风景").bounding_box()
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    page.mouse.move(x, y)
    for count in range(1, 5):                      # 不间断地连点 4 下 = 两次双击
        page.mouse.down(click_count=count)
        page.mouse.up(click_count=count)
        expected = start if count in (1, 4) else (not start)
        if count in (2, 4):
            page.wait_for_function(
                "e => document.querySelector('#artists [data-folder]').classList.contains('open') === e", arg=expected)
    assert is_open(page, "风景") == start


def test_folder_arrow_toggles_without_opening_page(page):
    title = page.title()
    start = is_open(page, "风景")
    folder(page, "风景").locator("[data-fold]").click()
    assert is_open(page, "风景") != start
    assert page.title() == title


def drag(page, source, target):
    a, b = source.bounding_box(), target.bounding_box()
    page.mouse.move(a["x"] + a["width"] / 2, a["y"] + a["height"] / 2)
    page.mouse.down()
    page.mouse.move(a["x"] + a["width"] / 2 + 20, a["y"] + a["height"] / 2 + 12, steps=4)
    return b


def top_level_artist(page):
    """不在任何文件夹里的一位画师"""
    return page.locator("#artists [data-artist]:not(.sub)").last


def test_drag_artist_into_folder(page):
    artist = top_level_artist(page)
    before = members(page, "常看")
    target = folder(page, "常看")
    b = drag(page, artist, target)
    page.wait_for_selector(".drag-ghost")
    assert page.locator("#artist-drop [data-drop=new]").is_visible()
    b = target.bounding_box()                      # 顶部多出投放区后位置会变
    page.mouse.move(b["x"] + b["width"] / 2, b["y"] + b["height"] / 2, steps=6)
    assert "drop" in target.get_attribute("class").split()
    page.mouse.up()
    page.wait_for_selector(".drag-ghost", state="detached")
    page.wait_for_function("document.querySelector('#artist-drop') === null")
    page.wait_for_function("n => +document.querySelectorAll('#artists [data-folder]')[1].querySelector('.n').innerText === n", arg=before + 1)
    assert members(page, "常看") == before + 1


def test_drag_cancelled_with_escape(page):
    artist = top_level_artist(page)
    before = members(page, "常看")
    target = folder(page, "常看")
    drag(page, artist, target)
    page.wait_for_selector(".drag-ghost")
    b = target.bounding_box()
    page.mouse.move(b["x"] + b["width"] / 2, b["y"] + b["height"] / 2, steps=6)
    page.keyboard.press("Escape")
    page.wait_for_selector(".drag-ghost", state="detached")
    page.mouse.up()
    page.wait_for_timeout(200)
    assert members(page, "常看") == before


def test_short_press_on_artist_is_a_click_not_a_drag(page):
    artist = top_level_artist(page)
    name = artist.locator(".lbl").inner_text().strip()
    artist.click()
    page.wait_for_function("n => document.title.startsWith(n)", arg=name)
    assert page.locator(".drag-ghost").count() == 0


# ---------------- 设置 ----------------
def test_settings_dialog_opens(page):
    page.click("#btn-settings")
    page.wait_for_selector("[data-page]")
    assert page.locator("[data-page]").count() >= 4


def test_r18_is_off_by_default_and_switch_reveals_rating_control(page):
    assert page.locator("#rating").is_hidden()                    # 默认不显示 R18：分级切换也不出现
    page.click("#btn-settings")
    switch = page.locator("[data-toggle=showR18]")
    if not switch.count():
        for nav in page.locator(".dnav [data-page]").all():
            nav.click()
            if switch.count():
                break
    assert switch.get_attribute("aria-checked") == "false"
    switch.click()
    page.wait_for_function("document.querySelector('#rating').offsetParent !== null")
    page.locator("[data-toggle=showR18]").click()
    page.wait_for_function("document.querySelector('#rating').offsetParent === null")
    page.keyboard.press("Escape")
    assert not re.search("R-?18", page.locator("#main").inner_text())


# ---------------- 冒烟：把各处入口都点一遍，页面不能报错（page 夹具会在结束时检查脚本错误） ----------------
def test_context_menus_open(page):
    page.locator("#grid-root .tile").first.click(button="right")
    page.wait_for_selector("#menu:not([hidden])")
    assert page.locator("#menu").inner_text().strip()
    page.keyboard.press("Escape")
    top_level_artist(page).click(button="right")
    page.wait_for_selector("#menu:not([hidden])")
    page.keyboard.press("Escape")
    folder(page, "风景").click(button="right")
    page.wait_for_selector("#menu:not([hidden])")
    assert "重命名" in page.locator("#menu").inner_text()


def test_toolbar_popups_and_sort(page):
    page.click("#tag-more")
    page.wait_for_selector("#tagpop:not([hidden])")
    page.keyboard.press("Escape")
    page.click("#btn-filters")
    page.wait_for_selector("#filterpop:not([hidden])")
    page.keyboard.press("Escape")
    page.locator("#tagrow .chip").first.click()
    page.wait_for_selector("#tagrow .chip.on")
    page.click("#btn-mode")
    page.click("#btn-collapse")
    page.click("#btn-expand")


def test_nav_scopes_and_artist_page(page):
    for i in range(page.locator("#nav .row-btn").count()):
        page.locator("#nav .row-btn").nth(i).click()
        page.wait_for_timeout(120)
    top_level_artist(page).click()
    page.wait_for_selector("#grid-root .tile")
    page.fill("#artist-filter", "a")
    page.fill("#artist-filter", "")


def test_expand_multi_page_work(page):
    button = page.locator("#grid-root .tile [data-expand]").first
    button.scroll_into_view_if_needed()
    button.hover()
    button.click()
    page.wait_for_selector("#grid-root .tile.sub")
    page.locator("#grid-root .tile.expanded [data-expand]").first.click()
    page.wait_for_selector("#grid-root .tile.sub", state="detached")


def test_selection_batch_and_keyboard(page):
    tiles = page.locator("#grid-root .tile")
    tiles.nth(0).click()
    tiles.nth(3).click(modifiers=["Shift"])
    page.wait_for_selector("#batch:not([hidden])")
    assert page.locator("#grid-root .tile.sel").count() >= 4
    page.keyboard.press("Escape")
    tiles.nth(0).click()
    page.keyboard.press("ArrowRight")
    page.keyboard.press("ArrowDown")
    page.keyboard.press("Enter")
    page.wait_for_selector("#viewer:not([hidden])")
    page.keyboard.press("i")
    page.keyboard.press("Escape")


def test_settings_pages_and_download_panel(page):
    page.click("#btn-settings")
    page.wait_for_selector("[data-page]")
    for i in range(page.locator("[data-page]").count()):
        page.locator("[data-page]").nth(i).click()
        page.wait_for_timeout(80)
    seg = page.locator("[data-set] button:not(.on)").first
    if seg.count():
        seg.click()
    page.keyboard.press("Escape")
    page.click("#btn-dl")
    page.wait_for_selector(".dlc")


# ---------------- 字体 ----------------
def test_default_fonts_load_without_network(page):
    # page 夹具已经把 Google Fonts 全部拦掉：默认的两款字体必须能从程序自带的文件里读到
    loaded = page.evaluate(
        """async () => {
            await Promise.all([document.fonts.load('400 13px "LXGW WenKai TC"', '画师作品'),
                               document.fonts.load('700 19px "M PLUS Rounded 1c"', 'Pixiv')]);
            return [document.fonts.check('400 13px "LXGW WenKai TC"', '画师作品'),
                    document.fonts.check('700 19px "M PLUS Rounded 1c"', 'Pixiv'),
                    [...document.fonts].filter((f) => f.status === 'loaded').length];
        }""")
    assert loaded[0] and loaded[1] and loaded[2] >= 2


# ---------------- 软件更新（模拟后端演示完整流程） ----------------
def test_update_flow_in_about_page(page):
    page.click("#btn-settings")
    page.wait_for_selector(".dnav [data-page]")
    page.click(".dnav [data-page=about]")
    box = page.locator("#upd-box")
    assert "不会自动检查" in box.inner_text()
    box.locator("[data-upd=check]").click()
    page.wait_for_selector("#upd-box [data-upd=start]")
    assert "9.9.9" in box.inner_text() and "这一版的变化" in box.inner_text()
    box.locator("[data-upd=start]").click()
    page.wait_for_selector("#upd-box .upd-bar")
    # 关掉设置再打开，进度还在
    page.keyboard.press("Escape")
    page.click("#btn-settings")
    page.click(".dnav [data-page=about]")
    page.wait_for_selector("#upd-box [data-upd=apply]", timeout=15000)
    assert "data 文件夹" in box.inner_text()


def test_update_download_can_be_cancelled(page):
    page.click("#btn-settings")
    page.click(".dnav [data-page=about]")
    page.click("#upd-box [data-upd=check]")
    page.click("#upd-box [data-upd=start]")
    page.click("#upd-box [data-upd=cancel]")
    page.wait_for_selector("#upd-box [data-upd=start]")


# ---------------- 下载代理（设置 → 速度与网络） ----------------
def open_download_options(page):
    page.click("#btn-settings")
    page.click(".dnav [data-page=dl-speed]")
    page.wait_for_selector("[data-dlproxysel]")


def open_general(page):
    page.click("#btn-settings")
    page.click(".dnav [data-page=general]")
    page.wait_for_selector("[data-dlnow=KEEP_AWAKE]")


def test_proxy_setting_custom_address_test_and_save(page):
    open_download_options(page)
    assert page.locator("[data-dlproxysel]").input_value() == "system"
    assert page.locator("[data-dlin=PROXY_URL]").count() == 0          # 不是“自定义”时不显示地址框
    page.select_option("[data-dlproxysel]", "custom")
    page.fill("[data-dlin=PROXY_URL]", "127.0.0.1:7890")
    page.click("[data-dl=test-proxy]")
    page.wait_for_selector(".dl-test.ok")
    assert "127.0.0.1:7890" in page.locator(".dl-test").inner_text()
    assert page.locator("[data-dlin=PROXY_URL]").input_value() == "127.0.0.1:7890"   # 测试后填的内容还在
    save = page.locator("[data-dl=save]")
    assert save.is_enabled()
    save.click()
    page.wait_for_selector("[data-dlproxysel]")
    page.wait_for_function("document.querySelector('[data-dlproxysel]').value === 'custom'")
    assert page.locator("[data-dlin=PROXY_URL]").input_value() == "127.0.0.1:7890"


def test_proxy_test_reports_failure_and_missing_address(page):
    open_download_options(page)
    page.select_option("[data-dlproxysel]", "none")
    page.click("[data-dl=test-proxy]")
    page.wait_for_selector(".dl-test.bad")
    page.select_option("[data-dlproxysel]", "custom")
    assert page.locator(".dl-test").count() == 0                        # 换了方式，旧的测试结果不再显示
    page.click("[data-dl=test-proxy]")
    page.wait_for_selector(".dl-test.bad")
    assert "代理地址" in page.locator(".dl-test").inner_text()


# ---------------- 添加账号：登录窗口（模拟后端过几秒“登录成功”） ----------------
def open_accounts(page):
    page.click("#btn-settings")
    page.click(".dnav [data-page=dl-accounts]")
    page.wait_for_selector("[data-dl=login-start]")


def test_login_window_flow(page):
    open_accounts(page)
    page.click("[data-dl=login-start]")
    page.wait_for_selector("[data-dl=login-cancel]")
    assert "弹出的窗口" in page.locator("#dl-page").inner_text()
    page.wait_for_selector("[data-dl=login-start]", timeout=10000)        # 完成后回到初始状态
    assert "已添加账号" in page.locator("#toast-t").inner_text()


def test_login_window_cancel(page):
    open_accounts(page)
    page.click("[data-dl=login-start]")
    page.click("[data-dl=login-cancel]")
    page.wait_for_selector("[data-dl=login-start]")
    page.wait_for_timeout(1200)
    assert page.locator(".upd-err").count() == 0                          # 自己取消的不算出错


def test_manual_login_instructions_mention_preserve_log(page):
    open_accounts(page)
    page.click("[data-dl=oauth-start]")
    text = page.locator(".dl-steps").inner_text()
    assert "先不要登录" in text and "保留日志" in text and "callback?" in text
    assert "地址栏" not in text                                           # 地址栏里不会出现要找的那个地址


# ---------------- 缩略图缓存 ----------------
def test_clear_thumbnail_cache_from_settings(page):
    page.click("#btn-settings")
    page.click(".dnav [data-page=library]")
    row = page.locator(".set", has=page.locator("[data-act=clear-cache]"))
    assert "缩略图缓存" in row.inner_text() and "214" in row.inner_text() and "90 天" in row.locator(".help-text").text_content()      # 细节在小问号里
    page.click("[data-act=clear-cache]")
    page.wait_for_function("document.querySelector('[data-act=clear-cache]') && document.querySelector('[data-act=clear-cache]').disabled")
    assert "已清空缩略图缓存" in page.locator("#toast-t").inner_text()


# ---------------- 下载与更新：任务选项、暂停、先看再下、失败处理 ----------------
def open_dl(page):
    page.click("#btn-dl")
    page.wait_for_selector(".dl-plan")


def test_task_options_pause_and_review_flow(page):
    open_dl(page)
    page.click("[data-dl=ask][data-kind=sync_download]")
    page.click("[data-dl=opts]")
    assert page.locator(".dl-opts [data-opt=scope]").input_value() == "all"
    page.select_option(".dl-opts [data-opt=scope]", "stale")
    page.wait_for_selector(".dl-opts [data-opt=staleDays]")
    page.click(".dl-opts [data-opttype=novel]")                          # 这次不要小说
    assert "on" not in page.locator(".dl-opts [data-opttype=novel]").get_attribute("class")
    page.click("[data-dl=start]")
    page.wait_for_selector("[data-dl=pause]")
    page.click("[data-dl=pause]")
    page.wait_for_selector("[data-dl=resume]")
    assert "已暂停" in page.locator(".dl-run-card").inner_text()
    done = page.locator(".dl-nums b").first.inner_text()
    page.wait_for_timeout(1500)
    assert page.locator(".dl-nums b").first.inner_text() == done         # 暂停期间进度不动
    page.click("[data-dl=resume]")
    page.wait_for_selector(".dl-result", timeout=20000)
    text = page.locator(".dl-result").inner_text()
    assert "还没有开始下载" in text and "检查失败" in text               # 新发现的多：先让我看；失败的画师列出来了
    page.click(".dl-result [data-dl=review]")
    page.wait_for_selector(".dl-rv-row")
    rows = page.locator(".dl-rv-row")
    total = rows.count()
    rows.first.locator("input").uncheck()
    assert f"已选 {total - 1} 位" in page.locator(".dl-rv-sum").inner_text().replace("\xa0", " ")
    page.click("[data-dl=rv-skip]")
    page.wait_for_function("n => document.querySelectorAll('.dl-rv-row').length === n", arg=total - 1)
    assert "不下载" in page.locator("#toast-t").inner_text()
    page.click("[data-dl=rv-download]")
    page.wait_for_selector("[data-dl=pause]")
    page.wait_for_selector(".dl-result", timeout=30000)
    assert "下载成功" in page.locator(".dl-result").inner_text()


def test_review_pending_and_restore_skipped(page):
    open_dl(page)
    page.click("[data-dl=review]")
    page.wait_for_selector(".dl-rv-row")
    before = page.locator(".dl-rv-row").count()
    page.click("[data-rvtype=illust]")                                   # 去掉插画：只剩有漫画 / 动图的画师
    page.wait_for_function("n => document.querySelectorAll('.dl-rv-row').length < n", arg=before)
    page.click("[data-rvtype=illust]")
    page.wait_for_function("n => document.querySelectorAll('.dl-rv-row').length === n", arg=before)
    page.click("[data-dl=rv-none]")
    assert page.locator("[data-dl=rv-download]").is_disabled()
    page.locator(".dl-rv-row").nth(1).locator("input").check()
    page.click("[data-dl=rv-skip]")                                      # 没选中的都标成不下载
    page.wait_for_function("document.querySelectorAll('.dl-rv-row').length === 1")
    page.click("[data-dl=rv-close]")
    page.wait_for_selector(".dl-plan")
    assert "不下载" in page.locator(".dl-plan").inner_text()
    page.click(".dl-plan [data-dl=review][data-skipped]")
    page.wait_for_selector(".dl-rv-row")
    page.click("[data-dl=rv-restore]")
    page.wait_for_selector(".dl-review .dl-empty")


def test_failed_artists_tab(page):
    open_dl(page)
    assert "上次检查失败" in page.locator(".dl-plan").inner_text()
    page.click(".dl-plan [data-dl=fail-tab]")
    page.wait_for_selector(".dl-tabs")
    text = page.locator("#dl-page").inner_text()
    assert "被限速" in text and "已注销或不存在" in text
    rows = page.locator(".dl-task")
    before = rows.count()
    rows.first.locator("[data-dl=artist-skip]").click()
    page.wait_for_function("n => document.querySelectorAll('.dl-task').length === n - 1", arg=before)
    assert "不再检查的画师 1 位" in page.locator("#dl-page").inner_text()
    page.click("[data-dl=ask-failed]")                                   # 全部重查：回到更新页，范围已经选成“上次失败的”
    page.wait_for_selector(".dl-confirm")
    assert page.locator(".dl-opts [data-opt=scope]").input_value() == "failed"


def test_failed_files_list_and_retry_now(page):
    open_dl(page)
    page.click("[data-dlpage=failed]")
    page.wait_for_selector(".dl-tabs")
    page.locator("[data-dl=list-open][data-kind=network]").click()
    page.wait_for_selector(".dl-task")
    assert page.locator(".dl-task").count() == 5
    assert page.locator(".dl-task [data-dl=open-url]").first.get_attribute("data-url").startswith("https://www.pixiv.net/artworks/")
    page.locator("[data-dl=retry-now][data-kinds=network]").click()      # 马上重试这一组：直接开始下载
    page.wait_for_selector(".dl-run-card")
    assert "重试失败的文件" in page.locator(".dl-run-card").inner_text()


# ---------------- 关闭窗口：询问、记住选择、设置 ----------------
@pytest.fixture
def desktop_page(browser, base_url):
    """装作桌面窗口（显示右上角的窗口按钮）；window.__pvLastWindowAction 记下界面让窗口做了什么"""
    context = browser.new_context(viewport={"width": 1280, "height": 800})
    context.route("**/fonts.g*/**", lambda route: route.abort())
    pg = context.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.goto(f"{base_url}/index.html?works=60&desktop=1")
    pg.wait_for_selector("#grid-root .tile")
    pg.wait_for_selector("#winctl:not([hidden])")
    yield pg
    context.close()
    assert not errors, f"页面脚本报错: {errors}"


def last_action(pg):
    return pg.evaluate("window.__pvLastWindowAction || null")


def test_close_asks_and_can_be_cancelled(desktop_page):
    pg = desktop_page
    pg.click("#winctl [data-win=close]")
    pg.wait_for_selector(".closeask")
    assert "托盘" in pg.locator(".closeask").inner_text() and "记住我的选择" in pg.locator(".closeask").inner_text()
    pg.keyboard.press("Escape")
    pg.wait_for_selector(".closeask", state="detached")
    assert last_action(pg) is None                                # 取消：什么都不做
    pg.click("#winctl [data-win=close]")
    pg.click(".closeask [data-choice=tray]")
    pg.wait_for_function("window.__pvLastWindowAction === 'tray'")
    pg.click("#winctl [data-win=close]")                          # 没勾“记住”：下次还问
    pg.wait_for_selector(".closeask")
    pg.click(".closeask [data-choice=exit]")
    pg.wait_for_function("window.__pvLastWindowAction === 'close'")


def test_close_choice_is_remembered_and_editable_in_settings(desktop_page):
    pg = desktop_page
    pg.click("#winctl [data-win=close]")
    pg.check(".closeask-remember input")
    pg.click(".closeask [data-choice=tray]")
    pg.wait_for_function("window.__pvLastWindowAction === 'tray'")
    pg.evaluate("window.__pvLastWindowAction = null")
    pg.click("#winctl [data-win=close]")                          # 记住了：不再询问，直接放到托盘
    pg.wait_for_function("window.__pvLastWindowAction === 'tray'")
    assert pg.locator(".closeask").count() == 0
    pg.click("#btn-settings")
    pg.click(".dnav [data-page=general]")
    seg = pg.locator("[data-set=closeAction]")
    assert "on" in seg.locator("[data-v=tray]").get_attribute("class")
    seg.locator("[data-v=ask]").click()                           # 在设置里改回“每次询问”
    pg.keyboard.press("Escape")
    pg.click("#winctl [data-win=close]")
    pg.wait_for_selector(".closeask")


def test_close_request_from_system_goes_through_the_same_prompt(desktop_page):
    pg = desktop_page
    pg.evaluate("void window.__pvRequestClose()")                    # 从任务栏 / Alt+F4 关窗口时后端调用的入口
    pg.wait_for_selector(".closeask")
    pg.evaluate("void window.__pvRequestClose()")                    # 连着触发两次也只有一个询问框
    assert pg.locator(".closeask").count() == 1


# ---------------- 快捷键：默认值、修改、鼠标侧键 ----------------
def focused_id(page):
    return page.evaluate("document.activeElement && document.activeElement.id")


def side_button(page, button):
    """按一下鼠标侧键（3 = 后退，4 = 前进）"""
    page.evaluate("""(b) => { for (const t of ['mousedown', 'mouseup']) document.body.dispatchEvent(new MouseEvent(t, { button: b, bubbles: true, cancelable: true })); }""", button)


def test_search_shortcut_is_ctrl_f_and_ctrl_k_still_works(page):
    assert page.locator("#search .kbd").inner_text() == "Ctrl F"
    page.keyboard.press("Control+f")
    assert focused_id(page) == "q"
    page.locator("#grid-root .tile").first.click()
    page.keyboard.press("Control+k")
    assert focused_id(page) == "q"


def test_shortcuts_can_be_changed_and_reset(page):
    page.click("#btn-settings")
    page.click(".dnav [data-page=keys]")
    chip = page.locator("[data-key='v.rotate'][data-key-i='0']")
    assert chip.inner_text() == "R"
    chip.click()
    page.wait_for_selector(".kbd.key.wait")
    page.keyboard.press("f")                                          # 看图里 F 已经是“收藏”：不让重复
    assert "已经用于" in page.locator("#toast-t").inner_text()
    page.keyboard.press("x")
    page.wait_for_selector("[data-key='v.rotate'][data-key-i='0']")
    assert page.locator("[data-key='v.rotate'][data-key-i='0']").inner_text() == "X"
    page.locator("[data-key='search'][data-key-i='-1']").click()      # 给“搜索”再加一个按键
    page.keyboard.press("Control+Shift+p")
    assert "Ctrl Shift P" in page.locator(".keys.edit").first.inner_text()
    page.keyboard.press("Escape")
    page.locator("#grid-root .tile").first.dblclick()                 # 进看图页试新按键
    page.wait_for_selector("#viewer:not([hidden])")
    before = page.evaluate("document.querySelector('#v-img').style.transform")
    page.keyboard.press("r")                                          # 旧按键不再起作用
    assert page.evaluate("document.querySelector('#v-img').style.transform") == before
    page.keyboard.press("x")
    page.wait_for_function("t => document.querySelector('#v-img').style.transform !== t", arg=before)
    page.keyboard.press("Control+Shift+p")                            # 新加的搜索键：回到网格并聚焦搜索框
    assert focused_id(page) == "q" and page.locator("#viewer").is_hidden()
    page.click("#btn-settings")
    page.click(".dnav [data-page=keys]")
    page.click("[data-key-reset='v.rotate']")
    assert page.locator("[data-key='v.rotate'][data-key-i='0']").inner_text() == "R"
    page.click("[data-act=keys-reset]")
    assert page.locator("[data-key-reset]").count() == 0


def test_mouse_side_buttons_go_back_and_forward(page):
    first = page.title()
    top_level_artist(page).click()
    page.wait_for_function("t => document.title !== t", arg=first)
    artist_title = page.title()
    page.locator("#grid-root .tile").first.dblclick()
    page.wait_for_selector("#viewer:not([hidden])")
    side_button(page, 3)                                              # 看图页里：后退 = 回到网格
    page.wait_for_selector("#viewer", state="hidden")
    assert page.title() == artist_title
    side_button(page, 3)                                              # 再后退：回到之前浏览的位置
    page.wait_for_function("t => document.title === t", arg=first)
    side_button(page, 4)                                              # 前进：又回到那位画师
    page.wait_for_function("t => document.title === t", arg=artist_title)
    page.click("#btn-settings")
    side_button(page, 3)                                              # 开着设置时：后退 = 关掉设置
    page.wait_for_function("document.querySelector('#scrim').hidden")
    assert page.title() == artist_title


def test_side_button_can_be_rebound(page):
    page.click("#btn-settings")
    page.click(".dnav [data-page=keys]")
    page.locator("[data-key='v.next'][data-key-i='-1']").click()      # 把“下一个作品”也绑到前进侧键
    side_button(page, 4)
    assert "已经用于" in page.locator("#toast-t").inner_text()        # 它现在是全局的“前进”：要先改掉那边
    page.keyboard.press("Escape")
    page.locator("[data-key='forward'][data-key-i='0']").click()
    page.keyboard.press("Delete")
    page.locator("[data-key='v.next'][data-key-i='-1']").click()
    side_button(page, 4)
    assert "鼠标侧键（前进）" in page.locator(".keys.edit").nth(2).inner_text()
    page.keyboard.press("Escape")
    page.locator("#grid-root .tile").first.dblclick()
    page.wait_for_selector("#viewer:not([hidden])")
    idx = page.locator("#v-idx").inner_text()
    side_button(page, 4)
    page.wait_for_function("t => document.querySelector('#v-idx').innerText !== t", arg=idx)


def test_keep_awake_switch_in_general_settings(page):
    open_general(page)
    switch = page.locator("[data-dlnow=KEEP_AWAKE]")
    assert switch.get_attribute("aria-checked") == "true"                # 默认打开
    assert "睡眠" in page.locator("#dpage").inner_text()
    switch.click()                                                       # 这一页的开关马上生效，没有“保存”按钮
    page.wait_for_function("document.querySelector('[data-dlnow=KEEP_AWAKE]').getAttribute('aria-checked') === 'false'")
    assert page.locator("#dpage [data-dl=save]").count() == 0
    page.click(".dnav [data-page=dl-speed]")
    page.wait_for_selector("[data-dlproxysel]")
    page.click(".dnav [data-page=general]")                              # 换页再回来：确实存下了
    page.wait_for_selector("[data-dlnow=KEEP_AWAKE][aria-checked=false]")


# ---------------- 下载完成后做什么 ----------------
def test_after_job_action_countdown_and_cancel(page):
    open_dl(page)
    page.click("[data-dl=ask][data-kind=download]")
    select = page.locator(".dl-confirm [data-after]")
    assert select.input_value() == "none"
    assert page.locator(".dl-confirm [data-after] option[value=command]").is_disabled()     # 没填命令：选不了
    select.select_option("shutdown")
    assert "倒计时" in page.locator(".dl-after").inner_text()
    page.click("[data-dl=start]")
    page.wait_for_selector(".dl-run-card [data-after]")
    assert page.locator(".dl-run-card [data-after]").input_value() == "shutdown"            # 进行中还能看到、还能改
    page.wait_for_selector(".afterask", timeout=40000)
    assert "秒后关机" in page.locator(".afterask h3").inner_text()
    page.click("[data-after-cancel]")
    page.wait_for_selector(".afterask", state="detached")
    assert "已取消" in page.locator("#toast-t").inner_text()
    page.click("[data-dl=dismiss]")
    page.click("[data-dl=ask][data-kind=sync]")
    assert page.locator(".dl-confirm [data-after]").input_value() == "none"                 # 不会记成默认：下一次又是“什么都不做”


def test_after_job_settings_in_general_settings(page):
    open_general(page)
    switch = page.locator("[data-uisw=notifyOnFinish]")
    assert switch.get_attribute("aria-checked") == "true"
    switch.click()
    assert page.locator("[data-uisw=notifyOnFinish]").get_attribute("aria-checked") == "false"
    page.fill("[data-uiin=afterCommand]", "D:\\scripts\\after.bat")
    page.locator("[data-uiin=afterCommand]").blur()
    page.keyboard.press("Escape")
    open_dl(page)
    page.click("[data-dl=ask][data-kind=sync]")
    assert not page.locator(".dl-confirm [data-after] option[value=command]").is_disabled()  # 填了命令之后可以选


def test_unreachable_library_says_so_and_can_reconnect(page, base_url):
    """图库在网络共享上、共享没连上时：说明是连不上（而不是“这里没有图片”），点“重新连接”后恢复"""
    page.goto(f"{base_url}/index.html?works=200&offline=1")
    box = page.locator("#grid-root .empty")
    box.wait_for()
    text = box.inner_text()
    assert "连不上图库所在的位置" in text and "用户名或密码不正确" in text and "NAS" in text
    assert "这里没有图片" not in text
    box.get_by_role("button", name="重新连接").click()
    page.locator("#grid-root .tile").first.wait_for()
    assert page.locator("#grid-root .empty").count() == 0


def test_light_dark_switch_cross_fades_for_half_a_second(page):
    """切换亮色 / 暗色：整页 0.5 秒交叉淡化，结束后主题确实换了"""
    before = page.evaluate("document.documentElement.dataset.theme")
    page.click("#btn-mode")
    page.wait_for_function("document.documentElement.dataset.theme !== %r" % before)
    durations = page.evaluate(
        "document.getAnimations().filter(a => (a.effect.pseudoElement || '').startsWith('::view-transition'))"
        ".map(a => a.effect.getTiming().duration)")
    assert durations and set(durations) == {500}
    page.wait_for_function("!document.getAnimations().some(a => (a.effect.pseudoElement || '').startsWith('::view-transition'))")
    # 只改别的设置（不换亮暗）时不做过渡
    page.evaluate("window.__vt = 0; const o = document.startViewTransition.bind(document); document.startViewTransition = (f) => { window.__vt++; return o(f); }; 0")
    page.evaluate("document.querySelector('#sort button:not(.on)').click()")
    assert page.evaluate("window.__vt") == 0


def test_artist_page_header_can_join_folder_and_pin(page):
    """画师页标题栏：加入文件夹、置顶，不用回侧栏里找这位画师；侧栏会自动滚到他"""
    key = page.evaluate("""() => {
        const rows = [...document.querySelectorAll('#artists .row-btn[data-artist]:not(.sub)')];
        const row = rows[rows.length - 1];                      // 侧栏最底下的一位：一开始看不到
        window.__last = row.dataset.artist;
        return row.dataset.artist;
    }""")
    page.fill("#artist-filter", "")
    page.evaluate("k => document.querySelector(`#artists .row-btn[data-artist=\"${CSS.escape(k)}\"]`).scrollIntoView()", key)
    page.locator(f'#artists .row-btn[data-artist="{key}"]:not(.sub)').first.click()
    page.locator('#title-area [data-act="folders"]').wait_for()
    page.evaluate("document.querySelector('#artists').scrollTop = 0")
    # 加入文件夹
    btn = page.locator('#title-area [data-act="folders"]')
    assert "加入文件夹" in btn.inner_text()
    btn.click()
    page.locator("#menu button", has_text="风景").click()
    page.wait_for_function("document.querySelector('#title-area [data-act=folders]').innerText.includes('风景')")
    assert "on" in page.locator('#title-area [data-act="folders"]').get_attribute("class")
    # 置顶
    pin = page.locator('#title-area [data-act="pin"]')
    assert pin.inner_text().strip() == "置顶"
    pin.click()
    page.wait_for_function("document.querySelector('#title-area [data-act=pin]').innerText.trim() === '已置顶'")
    # 重新打开这位画师的页面：侧栏滚到能看见他
    page.click('#nav [data-nav="all"]')
    page.evaluate("document.querySelector('#artists').scrollTop = 1e6")
    page.evaluate("document.dispatchEvent(new MouseEvent('mouseup', {button: 3, bubbles: true}))")     # 鼠标侧键“后退”
    page.wait_for_function("!!document.querySelector('#artists .row-btn.on')")
    visible = page.evaluate("""() => { const b = document.querySelector('#artists'), r = document.querySelector('#artists .row-btn.on').getBoundingClientRect(), o = b.getBoundingClientRect();
        return r.top >= o.top - 1 && r.bottom <= o.bottom + 1; }""")
    assert visible


def test_artist_header_name_opens_pixiv_and_actions_sit_on_the_right(page):
    """画师页标题栏：点头像 / 名字去 Pixiv 主页（不再有单独的“主页”按钮）；操作按钮靠右，紧挨搜索框"""
    page.evaluate("window.__opened = []; window.open = (u) => { window.__opened.push(u); return null; }; 0")
    page.locator('#artists .row-btn[data-artist]').nth(2).click()
    page.locator('#title-area [data-act="sync"]').wait_for()
    assert page.locator('#title-area button[data-act="pixiv"]').count() == 0
    assert "检查新作" in page.locator('#title-area [data-act="sync"]').inner_text()
    gap = page.evaluate("""() => { const a = document.querySelector('#title-area .acts').getBoundingClientRect(), s = document.querySelector('#search').getBoundingClientRect();
        return s.left - a.right; }""")
    assert 0 <= gap <= 24
    urls = page.evaluate("""async () => {
        const seen = []; const api = (await import('./js/state.js')).ctx.api; const real = api.openUrl;
        api.openUrl = (u) => { seen.push(u); };
        document.querySelector('#title-area h1').click();
        document.querySelector('#title-area .big-avatar').click();
        api.openUrl = real; return seen; }""")
    assert len(urls) == 2 and all(u.startswith("https://www.pixiv.net/users/") for u in urls)


def test_long_setting_descriptions_hide_behind_a_help_mark(page):
    """设置页：长说明收进小问号，鼠标移上去才显示；短说明照常直接显示"""
    page.keyboard.press("Control+,")
    page.locator('.dnav [data-page="library"]').click()
    row = page.locator(".set", has_text="显示 R18 内容")
    assert row.locator("small").count() == 0
    mark = row.locator(".help")
    assert page.locator("#helptip:visible").count() == 0
    mark.hover()
    tip = page.locator("#helptip")
    tip.wait_for()
    assert "R18 作品不会出现在任何地方" in tip.inner_text()
    box, view = tip.bounding_box(), page.viewport_size
    assert box["x"] >= 0 and box["x"] + box["width"] <= view["width"] and box["y"] + box["height"] <= view["height"]
    page.mouse.move(5, 5)
    page.wait_for_function("document.querySelector('#helptip').hidden")
    page.locator('.dnav [data-page="dl-speed"]').click()
    short = page.locator(".set", has_text="检查更新的线程数")
    short.wait_for()
    assert "主账号 / 每个备用账号" in short.locator("small").inner_text()


def test_avatar_check_and_fill_live_next_to_the_avatar_item(page):
    """数据与导入：头像的“检查 / 补全”就在“画师头像”那一行；检查不访问 Pixiv，直接说出缺谁的"""
    page.keyboard.press("Control+,")
    page.locator('.dnav [data-page="dl-link"]').click()
    item = page.locator(".dl-kinds > div", has_text="画师头像")
    item.wait_for()
    assert page.get_by_text("之后再补").count() == 0 and page.get_by_text("全部重新下载").count() == 0
    item.get_by_role("button", name="检查").click()
    res = item.locator("small.res")
    res.wait_for()
    assert "2 位缺头像" in res.inner_text().replace("\xa0", " ").replace("  ", " ") or "2" in res.inner_text()
    item.get_by_role("button", name="补全…").click()
    page.get_by_text("补全头像").first.wait_for()


def test_running_spinner_keeps_turning_across_redraws(page):
    """任务进行时页面每秒重画一次：转圈动画要接着刚才的角度转，不能每次从头开始（看上去一顿一顿）"""
    open_dl(page)
    page.click("[data-dl=ask][data-kind=sync_download]")
    page.click("[data-dl=start]")
    page.wait_for_selector(".dl-run-card .dl-spin")
    page.evaluate("document.querySelector('.dl-spin').__mark = 1; 0")

    def angle_error():
        # 转一圈 800 毫秒、对齐到页面时钟：任何时刻的角度都应当等于 时间 % 800 对应的角度
        return page.evaluate("""() => { const a = document.querySelector('.dl-spin').getAnimations()[0];
            return [a.startTime, Math.abs((a.currentTime % 800) - (document.timeline.currentTime % 800))]; }""")
    start, off = angle_error()
    assert start == 0 and off < 0.01
    page.wait_for_function("!document.querySelector('.dl-spin').__mark")          # 已经重画过，是个新的元素
    start, off = angle_error()
    assert start == 0 and off < 0.01


def test_settings_are_grouped_by_what_they_are_about(page):
    """设置的分类：界面 / 图库 / 下载 / 通用；每一项只出现在说得通的那一页"""
    page.click("#btn-settings")
    nav = page.locator(".dnav").inner_text().split()
    assert nav == ["设置", "界面", "外观", "网格", "看图", "图库", "图库", "数据导入", "下载", "Pixiv", "账号", "保存位置", "下载内容", "速度与网络",
                   "通用", "常规", "快捷键", "关于"]

    def texts(key, wait):
        page.click(f".dnav [data-page={key}]")
        page.wait_for_selector(wait)
        return page.locator("#dpage").inner_text()

    lib = texts("library", "#dpage .dl-file")                           # 图库：图片在哪、数据在哪、显示什么、缓存
    assert all(x in lib for x in ("图片文件夹", "数据文件夹", "显示 R18 内容", "缩略图缓存")) and "关闭窗口时" not in lib
    assert lib.index("图片文件夹") < lib.index("数据文件夹") < lib.index("显示 R18 内容")
    imp = texts("dl-link", ".dl-kinds")
    assert "导入已有的数据" in imp and "画师头像" in imp and "数据文件夹\n" not in imp
    content = texts("dl-content", "[data-dltype]")                      # 下什么
    assert all(x in content for x in ("作品类型", "小说", "动图", "先问我")) and "线程数" not in content and "代理" not in content
    speed = texts("dl-speed", "[data-dlproxysel]")                      # 多快、怎么连
    assert all(x in speed for x in ("网络代理", "预设", "线程数", "周期性休息")) and "作品类型" not in speed and "睡眠" not in speed
    gen = texts("general", "[data-dlnow=KEEP_AWAKE]")                   # 程序本身的行为
    assert all(x in gen for x in ("打开上次浏览的位置", "弹出系统通知", "完成后运行的命令", "不让电脑自动睡眠"))
    # 改“下载内容”只保存这一页的项目
    page.click(".dnav [data-page=dl-content]")
    page.wait_for_selector("[data-dltype]")
    page.click("[data-dltype=manga]")
    assert set(page.locator("[data-dl=save]").get_attribute("data-keys").split(",")) >= {"SYNC_TYPES", "REVIEW_THRESHOLD"}
    assert "PROXY_MODE" not in page.locator("[data-dl=save]").get_attribute("data-keys")
    assert page.locator("[data-dl=save]").is_enabled()


def test_one_click_on_artist_name_opens_pixiv_in_the_desktop_window(desktop_page):
    """桌面窗口里标题栏是拖动窗口的把手：画师的名字和头像不算在内，单击一次就打开主页"""
    pg = desktop_page
    pg.locator('#artists .row-btn[data-artist]').nth(2).click()
    pg.locator('#title-area h1[role=link]').wait_for()
    pg.evaluate("""async () => { const api = (await import('./js/state.js')).ctx.api; window.__urls = []; window.__pvWindowActions = [];
        api.openUrl = (u) => { window.__urls.push(u); }; }""")
    pg.locator('#title-area h1[role=link]').click()
    pg.locator('#title-area .big-avatar').click()
    assert len(pg.evaluate("window.__urls")) == 2 and pg.evaluate("window.__pvWindowActions") == []
    pg.locator('#title-area .meta').click()                              # 旁边的空白处仍然可以拖动窗口
    assert "drag" in pg.evaluate("window.__pvWindowActions")


def test_running_card_shows_every_thread_of_an_account(page):
    """任务进行时：一个账号有几个线程，就列出几行正在处理的作品（账号名只写在第一行）"""
    open_dl(page)
    page.click("[data-dl=ask][data-kind=download]")
    page.click("[data-dl=start]")
    page.wait_for_function("document.querySelectorAll('.dl-workers .tx').length >= 2")
    rows = page.evaluate("[...document.querySelectorAll('.dl-workers > div')].map(d => [d.querySelector('.nm').innerText.trim(), d.querySelector('.tx').innerText])")
    backup = [i for i, r in enumerate(rows) if r[0].startswith("backup")]
    assert len(backup) == 1 and "×2" in rows[backup[0]][0]
    assert "线程一" in rows[backup[0]][1] and rows[backup[0] + 1][0] == "" and "线程二" in rows[backup[0] + 1][1]

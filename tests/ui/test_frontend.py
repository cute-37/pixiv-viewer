"""前端关键流程：网格、看图页、侧边栏文件夹、设置。全部走真实的鼠标 / 键盘事件。"""
import re


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


# ---------------- 下载代理（设置 → 下载选项） ----------------
def open_download_options(page):
    page.click("#btn-settings")
    page.click(".dnav [data-page=dl-options]")
    page.wait_for_selector("[data-dlproxysel]")


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

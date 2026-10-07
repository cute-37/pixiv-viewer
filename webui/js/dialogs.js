// 对话框：设置（所有外观参数都是可选项，改动立即生效）、批量添加标签
import { $, $$, esc, icon, toast, fmtNum, fmtSize } from "./util.js";
import { mountUpdate } from "./update.js";
import { ACTIONS, FIXED, GROUPS, comboLabel, comboOf, mouseCombo } from "./keymap.js";
import { FAMILIES, ACCENTS, FONTS, HEAD_FONTS, DEFAULTS, setDensity, markCustomDensity, palette } from "./settings.js";

import { DL_SETTINGS_PAGES } from "./downloader.js";
import { hintHTML } from "./help.js";

// 侧栏分四段：界面、图库、下载、通用。名字前带 dl- 的页面整页由 downloader.js 提供。
const DL = Object.fromEntries(DL_SETTINGS_PAGES.map(([k, t, ic]) => [k, ["dl-" + k, t, ic]]));
const PAGES = [
  ["#", "界面"], ["look", "外观", "sun"], ["grid", "网格", "grid"], ["viewer", "看图", "fit"],
  ["#", "图库"], ["library", "图库", "folder"], DL.link,
  ["#", "下载"], DL.accounts, DL.storage, DL.content, DL.speed,
  ["#", "通用"], ["general", "常规", "gear"], ["keys", "快捷键", "cmd"], ["about", "关于", "info"],
];
// 这两页中间嵌着一段由 downloader.js 负责的内容：图库页里的“数据文件夹”，常规页里和下载任务有关的几项
const EMBED = { library: "home", general: "general" };

export function initDialogs(ctx) {
  const scrim = $("#scrim");
  let page = "look";
  let mode = null; // "settings" | "tags"

  ctx.dialogOpen = () => !scrim.hidden;
  ctx.closeDialog = () => { scrim.hidden = true; mode = null; stopCapture(); };
  scrim.addEventListener("mousedown", (e) => { if (e.target === scrim) ctx.closeDialog(); });

  // ================= 设置 =================
  ctx.openSettings = (which) => {
    if (which) page = which;
    mode = "settings";
    scrim.innerHTML = `<div class="dialog" role="dialog" aria-label="设置" data-part="dialog">
      <nav class="dnav"><div class="dt">设置</div>${PAGES.filter(([k]) => !k.startsWith("dl-") || ctx.dlMount).map(([k, t, ic]) => k === "#" ? `<div class="grp">${t}</div>`
        : `<button class="row-btn" data-page="${k}">${icon(ic)}<span class="lbl">${t}</span></button>`).join("")}</nav>
      <div class="dmain"><div class="dhead"><h3 id="d-title"></h3><button class="icon-btn" data-close title="关闭 (Esc)" aria-label="关闭">${icon("x")}</button></div>
      <div class="dpage" id="dpage"></div></div></div>`;
    scrim.hidden = false;
    renderPage();
  };
  ctx.onSettingsDialog = () => { if (mode === "settings") renderPage(true); };

  function renderPage(keepScroll) {
    const box = $("#dpage"); if (!box) return;
    const top = box.scrollTop;
    $$(".dnav [data-page]", scrim).forEach((b) => b.classList.toggle("on", b.dataset.page === page));
    $("#d-title").textContent = PAGES.find((p) => p[0] === page)[1];
    if (page.startsWith("dl-")) {
      // 下载相关的设置页：内容和交互都由 downloader.js 负责，这里只提供位置
      if (keepScroll && box.querySelector("#dl-page")) return;
      box.innerHTML = `<div id="dl-page" class="dl-host"></div>`;
      box.scrollTop = 0;
      return ctx.dlMount(page.slice(3));
    }
    const html = { look, grid, viewer, library, general, keys, about }[page]();
    if (EMBED[page] && ctx.dlMount) {
      // 自己的内容分成前后两段，中间留给 downloader.js；重画时只换自己的两段，不打扰中间那段
      if (keepScroll && box.querySelector("#own-a")) { $("#own-a").innerHTML = html[0]; $("#own-b").innerHTML = html[1]; return; }
      box.innerHTML = `<div id="own-a" class="own">${html[0]}</div><div id="dl-page" class="dl-host"></div><div id="own-b" class="own">${html[1]}</div>`;
      box.scrollTop = 0;
      return ctx.dlMount(EMBED[page]);
    }
    box.innerHTML = [].concat(html).join("");
    if (page === "about") mountUpdate(ctx, $("#upd-box"), ctx.lib.version);
    box.scrollTop = keepScroll ? top : 0;   // 切换到别的分页时从顶部开始
  }

  const S = () => ctx.S;
  const seg = (key, opts) => `<div class="seg" data-set="${key}">${opts.map(([v, l]) => `<button data-v="${esc(String(v))}" class="${String(S()[key]) === String(v) ? "on" : ""}">${l}</button>`).join("")}</div>`;
  const sw = (key, on = S()[key]) => `<button class="switch ${on === true || on === "on" ? "on" : ""}" data-toggle="${key}" role="switch" aria-checked="${!!(on === true || on === "on")}"></button>`;
  const range = (key, min, max, step = 1, unit = "px") => `<input type="range" data-range="${key}" min="${min}" max="${max}" step="${step}" value="${S()[key]}" aria-label="${key}"><output>${S()[key]}${unit}</output>`;
  const row = (title, hint, ctl) => `<div class="set"><div class="t">${title}${hintHTML(hint)}</div><div class="ctl">${ctl}</div></div>`;
  const group = (title, rows) => `<div class="group">${title ? `<div class="gh">${title}</div>` : ""}${rows.join("")}</div>`;

  function look() {
    const s = S(), p = palette(s);
    const fams = Object.entries(FAMILIES).map(([k, f]) => `<button data-family="${k}" class="${s.family === k ? "on" : ""}"><span class="sw">${["bg", "side", "line", "text", "accent"].map((c) => `<i style="background:${f[p.mode][c]}"></i>`).join("")}</span>${f.name}<small>${f.hint}</small></button>`).join("");
    const accents = ACCENTS.map((c) => `<button style="background:${c}" data-accent="${c}" class="${p.accent.toLowerCase() === c ? "on" : ""}" aria-label="强调色 ${c}"></button>`).join("");
    const fonts = (map, key) => `<div class="fontpick" data-font="${key}">${Object.entries(map).map(([k, f]) => `<button data-v="${k}" class="${s[key] === k ? "on" : ""}" style="font-family:${k === "same" ? "inherit" : f.stack.replace(/"/g, "'")}">${f.label}</button>`).join("")}</div>`;
    return [
      group("主题", [
        row("颜色模式", "跟随系统时会随 Windows 的亮 / 暗设置切换", seg("mode", [["system", "跟随系统"], ["light", "亮色"], ["dark", "暗色"]])),
        `<div class="set" style="display:block"><div class="t" style="margin-bottom:8px">风格</div><div class="fam">${fams}</div></div>`,
        row("强调色", "选中项、按钮和链接使用的颜色", `<div class="swatches">${accents}<input type="color" data-accent-custom value="${p.accent}" aria-label="自定义强调色" title="自定义颜色"><button class="linkbtn" data-accent-reset>默认</button></div>`),
      ]),
      group("字体", [
        row("正文", "霞鹜文楷和 M+ 圆体随软件自带；其余几款选用时从网上加载，没有网络时显示为系统字体", fonts(FONTS, "font")),
        row("标题", "页面标题、画师名等", fonts(HEAD_FONTS, "hfont")),
      ]),
      group("信息密度", [
        row("预设", s.density === "custom" ? "当前是自定义数值" : "", seg("density", [["compact", "紧凑"], ["medium", "适中"], ["relaxed", "宽松"]])),
        row("正文字号", "", range("fs", 11, 16, 0.5)),
        row("控件高度", "按钮、输入框、分段按钮的高度", range("ctl", 22, 40)),
        row("间距", "", range("gap", 4, 14)),
        row("圆角", "", range("radius", 0, 14)),
        row("页面标题字号", "", range("title", 14, 30)),
        row("侧栏行高", "", range("row", 22, 40)),
      ]),
    ].join("");
  }
  function grid() {
    const s = S();
    return [
      group("布局", [
        row("排列方式", "齐行和瀑布流会保留每张图的原始比例", seg("layout", [["uniform", "等比卡片"], ["justified", "齐行"], ["masonry", "瀑布流"]])),
        row("卡片比例", s.layout === "uniform" ? "只用于等比卡片，图片会居中裁剪" : "只在“等比卡片”时生效", seg("ratio", [["1 / 1", "1:1"], ["4 / 5", "4:5"], ["3 / 4", "3:4"], ["2 / 3", "2:3"]])),
        row("卡片大小", "也可以按住 Ctrl 滚动滚轮", range("tile", 110, 340)),
        row("卡片间距", "", range("tgap", 0, 28)),
        row("卡片圆角", "", range("tradius", 0, 20)),
        row("阴影", "", range("shadow", 0, 1.5, 0.1, "")),
      ]),
      group("内容", [
        row("文件名", "", seg("caption", [["hover", "悬停时显示"], ["always", "始终显示"], ["none", "不显示"]])),
        row("显示 AI / R18 标记", "", sw("badges")),
        row("合并多页作品", "同一作品的 p0、p1… 合成一张卡片，看图时用 ↑ ↓ 翻页", sw("mergePages")),
        row("按月分组", "按最新或修改时间排序时，在网格中插入月份标题", sw("groupByMonth")),
        row("悬停预览", "鼠标停在作品或画师上时，在旁边显示大一些的预览；多页作品可滚轮翻页", sw("hoverPreview")),
        row("标签条", "标题栏下方一行常用标签，其余在“全部标签”里", sw("tagchips")),
        row("底部状态条", "显示选中图片的尺寸、大小等", sw("status")),
      ]),
    ].join("");
  }
  function viewer() {
    return [
      group("看图", [
        row("打开时显示信息栏", "看图时按 I 切换", sw("infoOpen")),
        row("背景", "", seg("viewerBg", [["canvas", "跟随主题"], ["black", "黑色"], ["checker", "棋盘格"]])),
        row("打开时适应窗口", "关闭后以 100% 原始大小打开", sw("fitOnOpen")),
        row("双击图片", "", seg("doubleClick", [["fit", "适应 / 100%"], ["back", "返回网格"], ["fullscreen", "沉浸模式"]])),
        row("鼠标滚轮", "选择“翻页”时，按住 Ctrl 滚动仍是缩放", seg("wheel", [["zoom", "缩放"], ["page", "翻页"]])),
        row("像素图模式", "放大时不做平滑处理，适合像素画", sw("pixelated")),
        row("预加载前后的图片", "翻页更快，占用少量内存", sw("preload")),
      ]),
      group("幻灯片", [row("切换间隔", "", range("slideInterval", 1, 15, 0.5, " 秒"))]),
    ].join("");
  }
  function library() {
    const lib = ctx.lib;
    const folders = lib.roots.map((r) => `<div class="f">${icon(r.auto ? "dl" : "folder")}<code title="${esc(r.path)}">${esc(r.path)}</code><small>${r.auto ? "下载的保存位置 · " : ""}${r.offline ? "未连接" : fmtNum(r.count) + " 张"}</small>
      ${r.auto ? `<button class="icon-btn xs" data-open-dl="storage" title="在“下载与更新 → 保存位置”里修改" aria-label="修改保存位置">${icon("gear")}</button>`
        : `<button class="icon-btn xs" data-rm-root="${esc(r.path)}" title="从资料库移除（不会删除文件）" aria-label="移除">${icon("x")}</button>`}</div>`).join("");
    // 前一段：图片放在哪；（中间是数据文件夹，由 downloader.js 提供）；后一段：显示什么、缓存
    return [
      `<div class="group"><div class="gh">图片文件夹</div><div class="folders">${folders || `<div class="f"><small>还没有添加文件夹</small></div>`}</div>
        <div class="set"><div class="t"><small>支持本地目录和网络共享。下载的保存位置会自动包含在内。移除只影响资料库，不会删除任何文件。</small></div><div class="ctl"><button class="btn" data-act="add-root">${icon("plus")}添加文件夹</button></div></div></div>`,
      [
      group("内容", [
        row("显示 R18 内容", "关闭后，R18 作品不会出现在任何地方（网格、预览、搜索建议、看图页的其他作品），工具栏上的分级切换也会隐藏。", sw("showR18")),
      ]),
      group("缓存", [
        row(`缩略图缓存 · ${fmtSize(lib.cache?.size || 0)}`,
          `为了翻页快，看过的图片会存一份小图在本机。${lib.cache?.maxBytes ? `超过 ${fmtSize(lib.cache.maxBytes)} 或 ${lib.cache.maxAgeDays} 天没用到的部分，会在下次启动时自动清掉。` : ""}清空不会影响任何图片，之后浏览时会重新生成。`,
          ctx.api.clearCache ? `<button class="btn" data-act="clear-cache" ${lib.cache?.size ? "" : "disabled"}>清空</button>` : ""),
      ]),
      ].join(""),
    ];
  }
  function general() {
    return [[
      group("启动与关闭", [
        row("打开上次浏览的位置", "", sw("rememberLast")),
        ...(ctx.api.windowAction ? [row("关闭窗口时", "放到托盘后程序继续在后台运行，下载不会中断；点托盘里的图标回来，右键可以退出。", seg("closeAction", [["ask", "每次询问"], ["tray", "放到托盘"], ["exit", "直接退出"]]))] : []),
      ]),
    ].join(""), ""];
  }
  // ---------- 快捷键：可以改 ----------
  let capture = null;      // 正在等新按键的那一格：{id, index}（index = -1 表示新加一个）
  const stopCapture = () => { capture = null; ctx.capturingKey = false; };
  function keys() {
    const km = ctx.keymap;
    const chip = (id, combo, i) => (capture && capture.id === id && capture.index === i
      ? `<span class="kbd key wait">按下新的按键…</span>`
      : `<button class="kbd key" data-key="${id}" data-key-i="${i}" title="点一下，然后按新的按键；按 Delete 去掉这个按键">${esc(comboLabel(combo))}</button>`);
    const any = ACTIONS.some((a) => !km.isDefault(a[1]));
    return `<div class="group"><div class="set"><div class="t">快捷键可以改<small>点某个按键，再按下想用的新按键（鼠标侧键也可以）。按 Esc 取消，按 Delete 去掉这个按键。</small></div>
        <div class="ctl"><button class="btn" data-act="keys-reset" ${any ? "" : "disabled"}>全部恢复默认</button></div></div></div>`
      + GROUPS.map((g) => `<div class="group"><div class="gh">${g}</div><div class="keys edit">${ACTIONS.filter((a) => a[0] === g).map(([, id, label]) => {
        const list = km.bindings(id);
        return `<div>${label}</div><div class="kb">${list.map((c, i) => chip(id, c, i)).join("")}${capture && capture.id === id && capture.index === -1 ? chip(id, "", -1) : `<button class="kbd key add" data-key="${id}" data-key-i="-1" title="再加一个按键">+</button>`}
          ${km.isDefault(id) ? "" : `<button class="linkbtn" data-key-reset="${id}">恢复默认</button>`}</div>`;
      }).join("")}${FIXED.filter((f) => f[0] === g).map(([, label, how]) => `<div>${label}</div><div class="kb"><span class="kbd">${esc(how)}</span><small>不能改</small></div>`).join("")}</div></div>`).join("");
  }
  function setBinding(id, list) {
    const all = { ...(S().keys || {}) };
    if (list) all[id] = list; else delete all[id];
    ctx.setSetting({ keys: all });
    ctx.syncKeyHints && ctx.syncKeyHints();
  }
  function onCaptured(combo) {
    const { id, index } = capture, km = ctx.keymap, list = [...km.bindings(id)];
    if (combo === "Escape") { stopCapture(); return renderPage(true); }
    if (combo === "Delete" || combo === "Backspace") {
      if (index >= 0) { list.splice(index, 1); setBinding(id, list); }
      stopCapture(); return renderPage(true);
    }
    const used = km.conflict(id, combo);
    if (used) { toast(`${comboLabel(combo)} 已经用于“${used}”，先把那边改掉`); return; }
    if (list.includes(combo)) { stopCapture(); return renderPage(true); }
    if (index >= 0) list[index] = combo; else list.push(combo);
    setBinding(id, list);
    stopCapture(); renderPage(true);
  }
  document.addEventListener("keydown", (e) => {
    if (!capture) return;
    const combo = comboOf(e); if (!combo) return;
    e.preventDefault(); e.stopPropagation();
    onCaptured(combo);
  }, true);
  document.addEventListener("mouseup", (e) => {
    if (!capture) return;
    const combo = mouseCombo(e); if (!combo) return;
    e.preventDefault(); e.stopPropagation();
    onCaptured(combo);
  }, true);
  function about() {
    return group("", [
      row("Pixiv Viewer", "为 Pixiv 下载目录设计的本地图片浏览器", ctx.lib.version ? `<span class="ver">版本 ${esc(ctx.lib.version)}</span>` : ""),
      row("界面", ctx.api.isMock ? "当前是浏览器预览版，使用示例数据" : "桌面版", ""),
      row("恢复默认外观", "字体、颜色、密度、网格设置回到初始值；资料库不受影响",
        `<button class="btn" data-act="reset-look">恢复默认</button>`),
    ]) + (ctx.api.updateCheck ? `<div class="group"><div class="gh">更新</div><div id="upd-box"></div></div>` : "");
  }

  // ---------- 事件 ----------
  scrim.addEventListener("click", async (e) => {
    if (mode !== "settings") return;
    const t = e.target;
    if (t.closest("[data-close]")) return ctx.closeDialog();
    const od = t.closest("[data-open-dl]");
    if (od && ctx.openDownloader) return ctx.openDownloader(od.dataset.openDl);
    const nav = t.closest("[data-page]");
    if (nav) { page = nav.dataset.page; return renderPage(); }
    const fam = t.closest("[data-family]");
    if (fam) { ctx.setSetting({ family: fam.dataset.family, accent: null }); return renderPage(true); }
    const acc = t.closest("[data-accent]");
    if (acc) { ctx.setSetting({ accent: acc.dataset.accent }); return renderPage(true); }
    if (t.closest("[data-accent-reset]")) { ctx.setSetting({ accent: null }); return renderPage(true); }
    const fp = t.closest("[data-font] button");
    if (fp) { ctx.setSetting({ [fp.parentElement.dataset.font]: fp.dataset.v }); return renderPage(true); }
    const sb = t.closest("[data-set] button");
    if (sb) {
      const key = sb.parentElement.dataset.set, v = sb.dataset.v;
      if (key === "density") { setDensity(ctx.S, v); ctx.setSetting({}); }
      else ctx.setSetting({ [key]: v }, { grid: ["layout", "caption"].includes(key) });
      return renderPage(true);
    }
    const tg = t.closest("[data-toggle]");
    if (tg) {
      const key = tg.dataset.toggle, cur = ctx.S[key];
      const next = typeof cur === "boolean" ? !cur : cur === "on" ? "off" : "on";
      ctx.setSetting({ [key]: next }, { reload: key === "mergePages" || key === "showR18", grid: ["badges", "groupByMonth"].includes(key) });
      return renderPage(true);
    }
    const act = t.closest("[data-act]");
    if (act?.dataset.act === "add-root") { await ctx.addFolder(); return renderPage(true); }
    if (act?.dataset.act === "clear-cache") {
      act.disabled = true;
      const r = await ctx.api.clearCache();
      await ctx.reloadLibrary();
      toast(`已清空缩略图缓存，释放 ${fmtSize((r && r.freed) || 0)}`);
      return renderPage(true);
    }
    const keyChip = e.target.closest("[data-key]");
    if (keyChip) { capture = { id: keyChip.dataset.key, index: +keyChip.dataset.keyI }; ctx.capturingKey = true; return renderPage(true); }
    const keyReset = e.target.closest("[data-key-reset]");
    if (keyReset) { stopCapture(); setBinding(keyReset.dataset.keyReset, null); return renderPage(true); }
    if (act?.dataset.act === "keys-reset") { stopCapture(); ctx.setSetting({ keys: {} }); ctx.syncKeyHints && ctx.syncKeyHints(); return renderPage(true); }
    if (act?.dataset.act === "reset-look") {
      const keep = { lastScope: ctx.S.lastScope, sort: ctx.S.sort, rating: ctx.S.rating };
      Object.assign(ctx.S, DEFAULTS, keep);
      ctx.setSetting({}, { grid: true });
      toast("外观已恢复默认");
      return renderPage(true);
    }
    const rm = t.closest("[data-rm-root]");
    if (rm) {
      const path = rm.dataset.rmRoot;
      await ctx.api.removeFolder(path);
      if (ctx.api.isMock) toast("预览版不会修改资料库");
      else { await ctx.reloadLibrary(); ctx.loadWorks(); toast(`已移除 ${path}`); }
      return renderPage(true);
    }
    const pick = t.closest("[data-pick]");
    if (pick) {
      const r = await ctx.api.pickDirectory(pick.dataset.pick);
      if (r) { await ctx.reloadLibrary(); ctx.loadWorks(); renderPage(true); toast("已更新"); }
      else if (ctx.api.isMock) toast("预览版不能选择文件夹");
    }
  });
  scrim.addEventListener("input", (e) => {
    if (mode !== "settings") return;
    const r = e.target.closest("[data-range]");
    if (r) {
      const key = r.dataset.range, v = +r.value;
      markCustomDensity(ctx.S, key);
      ctx.setSetting({ [key]: v });
      const unit = key === "shadow" ? "" : key === "slideInterval" ? " 秒" : "px";
      r.nextElementSibling.textContent = v + unit;
      return;
    }
    if (e.target.matches("[data-accent-custom]")) ctx.setSetting({ accent: e.target.value });
  });
  scrim.addEventListener("change", (e) => { if (mode === "settings" && (e.target.matches("[data-range]") || e.target.matches("[data-accent-custom]"))) renderPage(true); });

  // ================= 输入一行文字（新建 / 重命名文件夹） =================
  ctx.askText = (title, value = "", placeholder = "") => new Promise((resolve) => {
    mode = "ask";
    scrim.innerHTML = `<div class="pop" style="position:static;width:min(360px,100%)" role="dialog" aria-label="${esc(title)}">
      <b style="font-family:var(--font-head)">${esc(title)}</b>
      <label class="search inpop"><input id="ask-input" value="${esc(value)}" placeholder="${esc(placeholder)}" autocomplete="off" maxlength="60"></label>
      <div class="pop-foot"><span class="sp"></span><button class="btn ghost" data-ask="cancel">取消</button><button class="btn primary" data-ask="ok">确定</button></div></div>`;
    scrim.hidden = false;
    const input = $("#ask-input");
    input.focus(); input.select();
    const done = (v) => { if (mode !== "ask") return; ctx.closeDialog(); resolve(v); };
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); done(input.value.trim() || null); }
      if (e.key === "Escape") { e.stopPropagation(); done(null); }
    });
    scrim.onclick = (e) => {
      if (mode !== "ask") return;
      const b = e.target.closest("[data-ask]");
      if (b) done(b.dataset.ask === "ok" ? input.value.trim() || null : null);
    };
  });

  // ================= 添加标签 =================
  ctx.promptTags = async (keys) => {
    mode = "tags";
    const existing = await ctx.api.myTagList();
    scrim.innerHTML = `<div class="pop" style="position:static;width:min(420px,100%)" role="dialog" aria-label="添加标签">
      <b style="font-family:var(--font-head)">为 ${keys.length} 个作品添加标签</b>
      <label class="search inpop">${icon("tag")}<input id="tp-input" placeholder="输入标签，回车添加；可连续添加多个" autocomplete="off"></label>
      <div class="pop-sel" id="tp-chosen"></div>
      <div class="pop-tools"><span>我用过的标签</span></div>
      <div class="pop-list" id="tp-list">${existing.map((t) => `<button class="chip" data-t="${esc(t)}">${esc(t)}</button>`).join("")}</div>
      <div class="pop-foot"><span class="sp"></span><button class="btn ghost" data-tp="cancel">取消</button><button class="btn primary" data-tp="ok">添加</button></div></div>`;
    scrim.hidden = false;
    const chosen = new Set();
    const draw = () => { $("#tp-chosen").innerHTML = [...chosen].map((t) => `<button class="chip on" data-rm="${esc(t)}">${esc(t)}<span class="x">×</span></button>`).join(""); };
    const input = $("#tp-input");
    input.focus();
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); const v = input.value.trim(); if (v) { chosen.add(v); input.value = ""; draw(); } else if (chosen.size) finish(); }
      if (e.key === "Escape") { e.stopPropagation(); ctx.closeDialog(); }
    });
    const finish = async () => {
      const v = input.value.trim(); if (v) chosen.add(v);
      if (!chosen.size) return ctx.closeDialog();
      await ctx.api.addTags(keys, [...chosen]);
      ctx.closeDialog();
      toast(`已为 ${keys.length} 个作品添加 ${chosen.size} 个标签`, async () => { await ctx.api.removeTags(keys, [...chosen]); ctx.onWorkChanged && ctx.onWorkChanged(); });
      ctx.onWorkChanged && ctx.onWorkChanged();
    };
    scrim.onclick = (e) => {
      if (mode !== "tags") return;
      const c = e.target.closest("#tp-list [data-t]"); if (c) { chosen.add(c.dataset.t); draw(); }
      const rm = e.target.closest("[data-rm]"); if (rm) { chosen.delete(rm.dataset.rm); draw(); }
      const b = e.target.closest("[data-tp]"); if (b) b.dataset.tp === "ok" ? finish() : ctx.closeDialog();
    };
  };
}

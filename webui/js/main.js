// 应用入口：启动、拖动调整宽度、快捷键。其余按功能分在 sidebar / toolbar / grid / menu / prefs 里，共用状态在 state.js
import { $, debounce, clamp, toast, initToast } from "./util.js";
import { normalize, applySettings, loadPreviewFonts, withThemeFade } from "./settings.js";
import { connect } from "./api.js";
import { announceUpdate } from "./update.js";
import { createKeymap, comboOf, mouseCombo } from "./keymap.js";
import { initViewer } from "./viewer.js";
import { initDialogs } from "./dialogs.js";
import { initHover } from "./hover.js";
import { initDownloader } from "./downloader.js";
import { initWindowControls } from "./winctl.js";
import { ctx, view } from "./state.js";
import { ensureTile, loadWorks, refreshSelection, runAction, selectedKeys, setStars, tileEls } from "./grid.js";
import { persist, syncChrome } from "./prefs.js";
import { folderById, navGo, navRecord, reloadLibrary, renderSide } from "./sidebar.js";
import { closePops, renderTagRow } from "./toolbar.js";

// ================= 拖动调整宽度 =================
function resizer(el, { get, set, min, max, def, sign, onCollapse }) {
  el.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    el.setPointerCapture(e.pointerId);
    el.classList.add("drag"); document.body.classList.add("resizing");
    const x0 = e.clientX, w0 = get();
    const move = (ev) => {
      const w = w0 + sign * (ev.clientX - x0);
      if (onCollapse && w < min - 60) { onCollapse(); return; }
      set(clamp(Math.round(w), min, max), false);
    };
    const up = () => {
      el.classList.remove("drag"); document.body.classList.remove("resizing");
      el.removeEventListener("pointermove", move); el.removeEventListener("pointerup", up);
      set(get(), true);
    };
    el.addEventListener("pointermove", move); el.addEventListener("pointerup", up);
  });
  el.addEventListener("dblclick", () => set(def, true));
}
function liveVar(name, key) {
  return (w, save) => {
    ctx.S[key] = w;
    document.documentElement.style.setProperty(name, w + "px");
    if (save) { persist(); renderTagRow(); }
  };
}
resizer($("#side-resizer"), { get: () => ctx.S.sidew, set: liveVar("--side-w", "sidew"), min: 180, max: 420, def: 228, sign: 1,
  onCollapse: () => ctx.setSetting({ sidebarCollapsed: true }) });
resizer($("#info-resizer"), { get: () => ctx.S.infow, set: liveVar("--info-w", "infow"), min: 240, max: 560, def: 300, sign: -1 });

// ================= 键盘 =================
function gridMove(dir) {
  if (view.cur) { const at = view.flat.indexOf(view.cur); const ahead = view.flat[Math.min(view.flat.length - 1, at + 40)]; if (ahead) ensureTile(ahead); }
  const tiles = view.flat.map((k) => tileEls.get(k)).filter(Boolean);
  if (!tiles.length) return;
  let i = view.cur ? tiles.indexOf(tileEls.get(view.cur)) : -1;
  if (i < 0) i = 0;
  else if (dir === "left") i = Math.max(0, i - 1);
  else if (dir === "right") i = Math.min(tiles.length - 1, i + 1);
  else {
    const r = tiles[i].getBoundingClientRect(), cx = r.left + r.width / 2;
    const lo = Math.max(0, i - 80), hi = Math.min(tiles.length, i + 81);
    const cand = tiles.slice(lo, hi).map((t, j) => ({ j: lo + j, r: t.getBoundingClientRect() }))
      .filter(({ r: q }) => dir === "down" ? q.top > r.top + r.height / 2 : q.bottom < r.top + r.height / 2);
    if (cand.length) {
      const rowY = dir === "down" ? Math.min(...cand.map((c) => c.r.top)) : Math.max(...cand.map((c) => c.r.top));
      const rowTiles = cand.filter((c) => Math.abs(c.r.top - rowY) < 4);
      i = rowTiles.sort((a, b) => Math.abs(a.r.left + a.r.width / 2 - cx) - Math.abs(b.r.left + b.r.width / 2 - cx))[0].j;
    }
  }
  const k = tiles[i].dataset.k;
  view.cur = k; view.anchor = k; view.sel = new Set([k]);
  refreshSelection();
  tiles[i].scrollIntoView({ block: "nearest" });
  if (ctx.quicklookOpen()) ctx.openQuicklook(k);
}
// 按键和鼠标侧键都先认成“动作”（见 keymap.js，可以在设置里改），再按当前在哪个界面去执行
const keymap = createKeymap(() => ctx.S.keys);
ctx.keymap = keymap;
ctx.syncKeyHints = () => { const k = $("#search .kbd"); if (k) k.textContent = keymap.label("search"); };
function goBack() {
  if (ctx.dialogOpen && ctx.dialogOpen()) return ctx.closeDialog();
  if (!$("#menu").hidden) { $("#menu").hidden = true; return; }
  if (!$("#tagpop").hidden || !$("#filterpop").hidden) return closePops();
  if (ctx.quicklookOpen && ctx.quicklookOpen()) { $("#quicklook").hidden = true; return; }
  if (ctx.viewerOpen && ctx.viewerOpen()) return ctx.closeViewer();
  navGo(-1);
}
function runGlobal(id) {
  if (id === "search") { ctx.closeViewer && ctx.closeViewer(); if (ctx.dialogOpen && ctx.dialogOpen()) ctx.closeDialog(); $("#q").focus(); $("#q").select(); }
  else if (id === "settings") ctx.openSettings();
  else if (id === "sidebar") ctx.setSetting({ sidebarCollapsed: !ctx.S.sidebarCollapsed });
  else if (id === "keys") ctx.openSettings("keys");
  else if (id === "back") goBack();
  else if (id === "forward") { if (!(ctx.dialogOpen && ctx.dialogOpen()) && !(ctx.viewerOpen && ctx.viewerOpen())) navGo(1); }
}
function handleCombo(combo, e, mouse = false) {
  if (!combo || ctx.capturingKey || document.querySelector(".closeask")) return;
  const tag = (e.target.tagName || "").toLowerCase();
  const typing = !mouse && (tag === "input" || tag === "textarea" || tag === "select");
  const global = keymap.find(combo, ["全局"]);
  // 鼠标侧键，以及带 Ctrl / Alt 的全局动作，在哪里都生效（正在打字、开着对话框也行）
  if (global && (mouse || /^(Ctrl|Alt)\+/.test(combo) || /^F\d+$/.test(combo))) { e.preventDefault(); runGlobal(global); return; }
  if (typing) return;
  if (ctx.dialogOpen && ctx.dialogOpen()) { if (combo === "Escape") ctx.closeDialog(); return; }
  if (!$("#menu").hidden) return;
  if (combo === "Escape" && (!$("#tagpop").hidden || !$("#filterpop").hidden)) { closePops(); return; }
  if (ctx.viewerOpen && ctx.viewerOpen()) {
    const id = keymap.find(combo, ["看图"]);
    if (id) { e.preventDefault(); ctx.handleViewerAction(id, combo); }
    else if (/^[0-5]$/.test(combo)) ctx.handleViewerStar(+combo);
    else if (global) { e.preventDefault(); runGlobal(global); }
    return;
  }
  if (ctx.handleQuicklookKey && ctx.handleQuicklookKey(e)) return;
  const id = keymap.find(combo, ["网格"]), keys = selectedKeys();
  if (id && id.startsWith("grid.") && ["left", "right", "up", "down"].includes(id.slice(5))) { e.preventDefault(); gridMove(id.slice(5)); }
  else if (id === "grid.open") { if (view.cur) ctx.openViewer(view.flat.indexOf(view.cur)); }
  else if (id === "grid.quicklook") { if (view.cur) { e.preventDefault(); ctx.openQuicklook(view.cur); } }
  else if (id === "grid.selectAll") { e.preventDefault(); view.sel = new Set(view.flat); refreshSelection(); }
  else if (id === "grid.copy") { if (keys.length) { e.preventDefault(); runAction("copy", keys); } }
  else if (id === "grid.clear") { if (view.sel.size) { view.sel.clear(); refreshSelection(); } }
  else if (id === "grid.fav") { if (keys.length) runAction("fav", keys); }
  else if (id === "grid.tag") { if (keys.length) { e.preventDefault(); runAction("tag", keys); } }
  else if (/^[0-5]$/.test(combo)) { if (keys.length) setStars(keys, +combo); }
  else if (global) { e.preventDefault(); runGlobal(global); }
}
document.addEventListener("keydown", (e) => handleCombo(comboOf(e), e));
// 鼠标侧键：按下时先拦住浏览器自己的“后退 / 前进”，松开时当作一次按键
document.addEventListener("mousedown", (e) => { if (e.button === 3 || e.button === 4) e.preventDefault(); });
document.addEventListener("mouseup", (e) => { const combo = mouseCombo(e); if (combo && (e.button !== 1 || keymap.find(combo, ["全局", "网格", "看图"]))) { if (e.button !== 1) e.preventDefault(); handleCombo(combo, e, true); } });
window.addEventListener("resize", debounce(renderTagRow, 100));
if (window.matchMedia) window.matchMedia("(prefers-color-scheme: dark)").addEventListener?.("change", () => { if (ctx.S.mode === "system") withThemeFade(ctx.S, () => { applySettings(ctx.S); syncChrome(); }); });

// ================= 启动 =================
async function boot() {
  initToast();
  ctx.api = await connect();
  if (ctx.api.isMock) loadPreviewFonts();
  ctx.S = normalize(await ctx.api.getConfig());
  applySettings(ctx.S);
  ctx.lib = await ctx.api.getLibrary();
  initViewer(ctx);
  initDialogs(ctx);
  initHover(ctx);
  initDownloader(ctx);
  if (!ctx.api.dl) $("#btn-dl").hidden = true;
  initWindowControls(ctx.api, ".head, .vhead, .brand, .rail-logo, [data-drag]",
    { get: () => ctx.S.closeAction, set: (v) => ctx.setSetting({ closeAction: v }) });
  syncChrome();
  const last = ctx.S.rememberLast && ctx.S.lastScope;
  if (last && (last.scope !== "artist" || ctx.artistByKey(last.artist)) && (last.scope !== "folder" || folderById(last.folder))) {
    view.scope = last.scope; view.artist = last.artist || null; view.folder = last.folder || null;
  }
  ctx.syncKeyHints();
  renderSide();
  await loadWorks(true);
  navRecord();
  // 桌面版后台索引完成后刷新计数与列表
  window.__pvLibraryUpdated = async (done) => {
    await reloadLibrary();
    if (done || !view.works.length) loadWorks();
  };
  announceUpdate(ctx.api, ctx.lib.version);
  if (ctx.api.isMock && ctx.lib.demo) {
    let shown = false;
    try { shown = !!sessionStorage.getItem("pv-demo-hint"); sessionStorage.setItem("pv-demo-hint", "1"); } catch (e) { /* 浏览器不允许存储时忽略 */ }
    if (!shown) toast("这是预览版：图片和画师是示例数据");
  }
}
boot();

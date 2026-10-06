// 应用入口：启动、拖动调整宽度、快捷键。其余按功能分在 sidebar / toolbar / grid / menu / prefs 里，共用状态在 state.js
import { $, debounce, clamp, toast, initToast } from "./util.js";
import { normalize, applySettings, loadPreviewFonts } from "./settings.js";
import { connect } from "./api.js";
import { announceUpdate } from "./update.js";
import { initViewer } from "./viewer.js";
import { initDialogs } from "./dialogs.js";
import { initHover } from "./hover.js";
import { initDownloader } from "./downloader.js";
import { initWindowControls } from "./winctl.js";
import { ctx, view } from "./state.js";
import { ensureTile, loadWorks, refreshSelection, runAction, selectedKeys, setStars, tileEls } from "./grid.js";
import { persist, syncChrome } from "./prefs.js";
import { folderById, reloadLibrary, renderSide } from "./sidebar.js";
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
document.addEventListener("keydown", (e) => {
  const tag = (e.target.tagName || "").toLowerCase();
  const typing = tag === "input" || tag === "textarea" || tag === "select";
  const ctrl = e.ctrlKey || e.metaKey;
  if (ctrl && e.key.toLowerCase() === "k") { e.preventDefault(); ctx.closeViewer && ctx.closeViewer(); $("#q").focus(); $("#q").select(); return; }
  if (ctrl && e.key === ",") { e.preventDefault(); ctx.openSettings(); return; }
  if (ctrl && e.key.toLowerCase() === "b") { e.preventDefault(); ctx.setSetting({ sidebarCollapsed: !ctx.S.sidebarCollapsed }); return; }
  if (typing) return;
  if (ctx.dialogOpen && ctx.dialogOpen()) { if (e.key === "Escape") ctx.closeDialog(); return; }
  if (!$("#menu").hidden) return;
  if (e.key === "Escape" && (!$("#tagpop").hidden || !$("#filterpop").hidden)) { closePops(); return; }
  if (ctx.handleViewerKey && ctx.handleViewerKey(e)) return;
  if (ctx.handleQuicklookKey && ctx.handleQuicklookKey(e)) return;
  // 网格
  const keys = selectedKeys();
  switch (true) {
    case e.key === "ArrowLeft": e.preventDefault(); gridMove("left"); break;
    case e.key === "ArrowRight": e.preventDefault(); gridMove("right"); break;
    case e.key === "ArrowUp": e.preventDefault(); gridMove("up"); break;
    case e.key === "ArrowDown": e.preventDefault(); gridMove("down"); break;
    case e.key === "Enter" && !!view.cur: ctx.openViewer(view.flat.indexOf(view.cur)); break;
    case e.key === " " && !!view.cur: e.preventDefault(); ctx.openQuicklook(view.cur); break;
    case ctrl && e.key.toLowerCase() === "a": e.preventDefault(); view.sel = new Set(view.flat); refreshSelection(); break;
    case ctrl && e.key.toLowerCase() === "c" && keys.length > 0: e.preventDefault(); runAction("copy", keys); break;
    case e.key === "Escape" && view.sel.size > 0: view.sel.clear(); refreshSelection(); break;
    case /^[0-5]$/.test(e.key) && !ctrl && keys.length > 0: setStars(keys, +e.key); break;
    case e.key.toLowerCase() === "f" && !ctrl && keys.length > 0: runAction("fav", keys); break;
    case e.key.toLowerCase() === "t" && !ctrl && keys.length > 0: e.preventDefault(); runAction("tag", keys); break;
    case e.key === "?": ctx.openSettings("keys"); break;
    default: return;
  }
});
window.addEventListener("resize", debounce(renderTagRow, 100));
if (window.matchMedia) window.matchMedia("(prefers-color-scheme: dark)").addEventListener?.("change", () => { if (ctx.S.mode === "system") { applySettings(ctx.S); syncChrome(); } });

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
  initWindowControls(ctx.api, ".head, .vhead, .brand, .rail-logo, [data-drag]");
  syncChrome();
  const last = ctx.S.rememberLast && ctx.S.lastScope;
  if (last && (last.scope !== "artist" || ctx.artistByKey(last.artist)) && (last.scope !== "folder" || folderById(last.folder))) {
    view.scope = last.scope; view.artist = last.artist || null; view.folder = last.folder || null;
  }
  renderSide();
  await loadWorks(true);
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

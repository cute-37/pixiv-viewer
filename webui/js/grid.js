// 作品网格：加载数据、渲染、选择、状态栏、批量操作、对作品的各种操作
import { $, $$, esc, icon, fmtDate, fmtMonth, fmtSize, fmtNum, toast, copyText } from "./util.js";
import { ctx, view } from "./state.js";
import { createLazyLoader } from "./lazy.js";
import { workMenu } from "./menu.js";
import { addFolder, openScope, renderSide } from "./sidebar.js";
import { renderTagPop, renderTagRow, renderTitle } from "./toolbar.js";

// ================= 数据加载 =================
let loadToken = 0;
// 还没下载完的缩略图不再需要了：马上取消，把连接让给新列表
function cancelThumbs() {
  thumbs.reset();
  $$("#grid-root img[src]:not(.ok)").forEach((img) => img.removeAttribute("src"));
}
export async function loadWorks(resetScroll = false) {
  const token = ++loadToken;
  view.loading = true;
  cancelThumbs();
  $("#content").classList.add("loading");
  const S = ctx.S;
  const res = await ctx.api.listWorks({
    scope: view.scope, artist: view.artist, folder: view.folder, sort: S.sort, rating: ctx.rating(), tags: [...view.tags], q: view.q,
    filters: view.filters, mergePages: S.mergePages,
  });
  if (token !== loadToken) return;
  view.loading = false;
  $("#content").classList.remove("loading");
  if ("indexing" in res) ctx.lib.indexing = res.indexing;
  view.works = res.works;
  view.scopeTags = res.scopeTags || [];
  const keys = new Set(view.works.map((w) => w.key));
  view.sel = new Set([...view.sel].filter((k) => keys.has(k)));
  if (view.cur && !keys.has(view.cur)) view.cur = null;
  // 先换掉旧网格（只生成首批卡片），之后标签行量尺寸时就不用对旧的上千张卡片排版
  if (resetScroll) $("#content").scrollTop = 0;
  renderGrid();
  renderTitle();
  renderTagRow();
  if (!$("#tagpop").hidden) renderTagPop();
  if (ctx.pendingOpen) { const k = ctx.pendingOpen; ctx.pendingOpen = null; const i = view.works.findIndex((w) => w.key === k || w.key.split("#")[0] === k); if (i >= 0) ctx.openViewer(i); }
  ctx.onWorks && ctx.onWorks();
}
ctx.loadWorks = loadWorks;

// ================= 网格 =================
function tileHTML(w) {
  const p = w.pages[0];
  const sel = view.sel.has(w.key), cur = view.cur === w.key;
  const badges = (w.rating !== "safe" ? `<span class="badge r18">${w.rating === "r18g" ? "R18G" : "R18"}</span>` : "") + (w.ai ? `<span class="badge">AI</span>` : "")
    + (w.anim ? `<span class="badge anim" title="动图：封面是静止的，鼠标停在上面或打开后会播放">${icon("play")}动图</span>` : "");
  const stars = w.stars ? `<span class="stars-mini">${icon("star").repeat(w.stars)}</span>` : "";
  const open = w.pages.length > 1 && view.expanded.has(w.key);
  return `<div class="tile ${sel ? "sel" : ""} ${cur ? "cur" : ""} ${open ? "expanded" : ""}" data-k="${esc(w.key)}" style="--ar:${w.ar}${open ? `;--grp:${view.expanded.get(w.key)}` : ""}">
    <div class="thumb"><img data-src="${esc(p.thumb || p.path)}" alt="" decoding="async" draggable="false">
      <div class="badges">${badges}</div>
      <button class="favbtn ${w.fav ? "on" : ""}" data-fav title="${w.fav ? "取消收藏" : "收藏"} (F)" aria-label="收藏">${icon("heart")}</button>
      <span class="check">${icon("check")}</span>${stars}
      ${w.pages.length > 1 ? `<div class="pagebar"><span class="pages" title="共 ${w.pages.length} 页">${icon("pages")}${w.pages.length}</span>${expandBtnHTML(open)}</div>` : ""}
      <div class="overcap">${esc(w.title)} · ${esc(p.file)}</div>
    </div>
    <div class="undercap"><div class="f">${esc(w.title)}</div><div class="m">${esc(w.artistName)}${p.w ? ` · ${p.w}×${p.h}` : ""}</div></div>
  </div>${open ? subTilesHTML(w) : ""}`;
}
// 展开按钮：平时是页数旁边的一个小箭头，鼠标移到卡片上时带出“展开”两个字
const expandBtnHTML = (open) => `<button class="expand" data-expand title="${open ? "收起，只显示第 1 页" : "把这个作品的每一页都摆出来"}" aria-label="${open ? "收起全部页" : "展开全部页"}"><span>${open ? "收起" : "展开"}</span>${icon(open ? "fold" : "unfold")}</button>`;
// 每展开一组换一种颜色，相邻的几组不会撞色
const GROUP_COLORS = ["#e5484d", "#12a594", "#f59e0b", "#8e4ec6", "#0091ff", "#e93d82", "#5c9a31", "#f76808"];
let groupTurn = 0;
// 展开多页作品时，第 2 页起各占一张卡片，紧跟在作品后面；同一组的卡片顶部色条、页码都是同一种颜色
function subTilesHTML(w) {
  const sel = view.sel.has(w.key), color = view.expanded.get(w.key);
  let html = "";
  for (let i = 1; i < w.pages.length; i++) {
    const p = w.pages[i];
    const ar = p.w && p.h ? (p.w / p.h).toFixed(4) : w.ar;
    html += `<div class="tile sub ${sel ? "sel" : ""}" data-k="${esc(w.key)}" data-pg="${i}" style="--ar:${ar};--grp:${color}">
      <div class="thumb"><img data-src="${esc(p.thumb || p.path)}" alt="" decoding="async" draggable="false">
        <span class="pgno">${i + 1} / ${w.pages.length}</span>
        <div class="overcap">${esc(w.title)} · ${esc(p.file)}</div>
      </div>
      <div class="undercap"><div class="f">${esc(w.title)}</div><div class="m">第 ${i + 1} 页${p.w ? ` · ${p.w}×${p.h}` : ""}</div></div>
    </div>`;
  }
  return html;
}
export function toggleExpand(k) {
  const w = workByKey(k), main = tileEls.get(k);
  if (!w || !main || w.pages.length < 2) return;
  if (view.expanded.has(k)) {
    view.expanded.delete(k);
    (subEls.get(k) || []).forEach((el) => el.remove());
    subEls.delete(k);
    main.classList.remove("expanded");
    main.style.removeProperty("--grp");
    main.querySelector(".expand").outerHTML = expandBtnHTML(false);
    main.scrollIntoView({ block: "nearest" });
    return;
  }
  const color = GROUP_COLORS[groupTurn++ % GROUP_COLORS.length];
  view.expanded.set(k, color);
  main.classList.add("expanded");
  main.style.setProperty("--grp", color);
  main.querySelector(".expand").outerHTML = expandBtnHTML(true);
  main.insertAdjacentHTML("afterend", subTilesHTML(w));
  const els = [];
  for (let el = main.nextElementSibling; el && el.dataset.pg && el.dataset.k === k; el = el.nextElementSibling) els.push(el);
  subEls.set(k, els);
  thumbs.observeAll(els.map((el) => el.querySelector("img")));
}
ctx.toggleExpand = toggleExpand;
// 作品多时一次性生成全部卡片会让界面停顿：先生成首屏附近的一批，其余分批在空闲时追加，
// 期间点击、滚动都不受影响。缩略图只在进入可视范围时才下载。
const FIRST_CHUNK = 150, CHUNK = 300;
const thumbs = createLazyLoader($("#content"), (src) => ctx.api.thumbSrc(src));
let renderToken = 0;
export const tileEls = new Map();     // 作品 key -> 卡片元素，用于局部更新选中状态
const subEls = new Map();      // 展开的多页作品 key -> 它后面那几张“第 N 页”卡片
let marked = new Set();        // 当前带 sel / cur 样式的 key
let gridPump = () => {};
export let ensureTile = () => null;
$("#content").addEventListener("scroll", () => gridPump(), { passive: true });
ctx.ensureTile = (k) => ensureTile(k);
export function renderGrid() {
  const S = ctx.S, root = $("#grid-root");
  const list = view.works;
  const token = ++renderToken;
  const keepScroll = $("#content").scrollTop;
  view.flat = list.map((w) => w.key);
  view.index = new Map(list.map((w) => [w.key, w]));
  tileEls.clear();
  subEls.clear();
  marked = new Set();
  thumbs.reset();
  const cls = `grid ${S.layout} cap-${S.caption} ${S.badges === "off" ? "no-badges" : ""} ${view.sel.size > 1 ? "multi" : ""}`;
  if (!list.length) {
    root.innerHTML = emptyHTML();
    renderStatus(); renderBatch();
    return;
  }
  // 先建好分节骨架，再把卡片分批放进对应的网格
  const group = S.groupByMonth && (S.sort === "id" || S.sort === "time") && view.scope !== "recent";
  const sections = [];
  if (group) {
    const byMonth = new Map();
    for (const w of list) { const m = S.sort === "time" ? monthOf(w.mtime) : w.month; if (!byMonth.has(m)) byMonth.set(m, []); byMonth.get(m).push(w); }
    for (const [m, ws] of byMonth) sections.push({ title: `${fmtMonth(m)}<small>${ws.length} 个作品</small>`, works: ws });
  } else {
    sections.push({ title: "", works: list });
  }
  // 分节在第一张卡片放进去时才创建，未渲染到的月份不会先露出一排空标题
  root.innerHTML = "";
  const grids = [];
  const gridOf = (i) => {
    if (!grids[i]) {
      const sec = document.createElement("section");
      sec.className = "section";
      sec.innerHTML = `${sections[i].title ? `<h2>${sections[i].title}</h2>` : ""}<div class="${cls}"></div>`;
      root.append(sec);
      grids[i] = sec.lastElementChild;
    }
    return grids[i];
  };
  const queue = [];
  sections.forEach((s, i) => s.works.forEach((w) => queue.push([i, w])));
  const content = $("#content");
  let pos = 0, scheduled = false;
  const step = (n) => {
    if (token !== renderToken) return;
    const end = Math.min(queue.length, pos + n);
    let current = -1, html = "";
    const flush = () => {
      if (current < 0 || !html) return;
      const tpl = document.createElement("template");
      tpl.innerHTML = html;
      for (const el of tpl.content.children) {
        if (!el.dataset.pg) tileEls.set(el.dataset.k, el);
        else { if (!subEls.has(el.dataset.k)) subEls.set(el.dataset.k, []); subEls.get(el.dataset.k).push(el); }
      }
      const imgs = tpl.content.querySelectorAll("img[data-src]");
      gridOf(current).append(tpl.content);
      thumbs.observeAll(imgs);
      html = "";
    };
    for (; pos < end; pos++) {
      const [g, w] = queue[pos];
      if (g !== current) { flush(); current = g; }
      html += tileHTML(w);
    }
    flush();
  };
  // 只渲染到可视范围往下几屏；滚动接近底部时再接着追加。作品再多，页面上的卡片数也只和浏览过的范围有关
  const needMore = () => pos < queue.length && content.scrollTop + content.clientHeight * 4 > content.scrollHeight;
  const pump = () => {
    scheduled = false;
    if (token !== renderToken) return;
    if (needMore()) { step(CHUNK); schedule(); }
  };
  const schedule = () => { if (!scheduled && pos < queue.length) { scheduled = true; setTimeout(pump, 0); } };
  gridPump = schedule;
  // 把某张卡片（以及它之前的卡片）立即渲染出来：键盘移动、关闭看图页时定位用
  ensureTile = (k) => {
    if (token !== renderToken) return null;
    while (!tileEls.has(k) && pos < queue.length) step(CHUNK);
    return tileEls.get(k) || null;
  };
  step(FIRST_CHUNK);
  // 重新载入（例如改了评分筛选）时保持原来的滚动位置
  while (keepScroll && pos < queue.length && content.scrollHeight < keepScroll + content.clientHeight * 2) step(CHUNK);
  if (keepScroll) content.scrollTop = keepScroll;
  schedule();
  for (const k of [...view.sel, view.cur]) if (k) marked.add(k);
  renderStatus(); renderBatch();
}
ctx.renderGrid = renderGrid;
const monthOf = (ts) => { const d = new Date(ts * 1000); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`; };

async function reconnect(btn) {
  btn.disabled = true; btn.textContent = "正在连接…";
  await ctx.api.libraryReconnect();
  await ctx.reloadLibrary();            // 后端开始重新扫描；扫完会再通知一次
  ctx.loadWorks();
}

function emptyHTML() {
  if (ctx.lib.indexing && view.scope !== "artist") {
    return `<div class="empty"><div class="box"><div class="ic">${icon("clock")}</div><h3>正在扫描图库</h3>
      <p>第一次打开或图库在网络共享上时需要一点时间。扫描到的作品会陆续出现在这里，也可以先从左侧打开某位画师。</p></div></div>`;
  }
  if (!ctx.lib.roots.length) {
    // 第一次打开：两条路各自说清楚要做什么，每一步都能直接点过去
    const dl = !!ctx.openDownloader;
    return `<div class="empty"><div class="welcome">
      <h3>欢迎使用 Pixiv Viewer</h3><p class="lead">选一种方式开始。两种可以都用：之后下载的图会和已有的图放在同一个图库里。</p>
      <div class="ways">
        <section><div class="ic">${icon("folder")}</div><h4>我已经有图片</h4>
          <p>选存放它们的文件夹，里面每个子文件夹算一位画师。图片留在原处，不会被移动或修改。</p>
          <div class="acts"><button class="btn primary" data-act="add">${icon("plus")}添加文件夹</button></div>
          ${dl ? `<button class="linkbtn" data-act="open-settings" data-page="dl-link">以前用别的工具下载过？把数据库和头像也导进来</button>` : ""}</section>
        ${dl ? `<section><div class="ic">${icon("dl")}</div><h4>从 Pixiv 下载</h4>
          <ol class="steps">
            <li><button class="linkbtn" data-act="open-settings" data-page="dl-accounts">登录 Pixiv 账号</button><small>在弹出的官方登录页里登录，软件看不到你的密码。</small></li>
            <li><button class="linkbtn" data-act="open-settings" data-page="dl-storage">选保存位置</button><small>不选就存在程序文件夹的 data 里；也可以存到网络共享。</small></li>
            <li><button class="linkbtn" data-act="open-dl">检查关注的画师并下载</button><small>开始前会先告诉你要做什么，确认了才会访问 Pixiv。</small></li>
          </ol></section>` : ""}
      </div></div></div>`;
  }
  // 图库所在的位置（多半是网络共享）读不到：说清楚是没连上，而不是“没有图片”
  const off = ctx.lib.roots.filter((r) => r.offline);
  if (off.length === ctx.lib.roots.length && !ctx.lib.artists.length) {
    return `<div class="empty"><div class="box"><div class="ic">${icon("warn")}</div><h3>连不上图库所在的位置</h3>
      <p>${off.map((r) => `<code>${esc(r.path)}</code>${r.error ? `<br>${esc(r.error)}` : ""}`).join("<br>")}</p>
      <p>图片和数据都还在，只是现在读不到。检查这台设备是否开着、网络是否正常；如果是共享的账号或密码变了，到“下载与更新 → 设置 → 保存位置”里改。</p>
      <div class="acts"><button class="btn primary" data-act="reconnect">重新连接</button>${ctx.openDownloader ? `<button class="btn" data-act="open-dl">${icon("dl")}下载与更新</button>` : ""}</div></div></div>`;
  }
  const filtered = view.q || view.tags.size || Object.values(view.filters).some(Boolean) || (ctx.S.showR18 !== false && ctx.S.rating !== "all");
  if (filtered) {
    return `<div class="empty"><div class="box"><div class="ic">${icon("search")}</div><h3>没有符合条件的作品</h3>
      <p>当前的搜索词、标签或筛选条件下没有图片。</p><div class="acts"><button class="btn" data-act="clear-filters">清除全部条件</button></div></div></div>`;
  }
  const text = { fav: ["还没有收藏", "在图片上点 ♥ 或按 F 收藏，收藏的作品会出现在这里。"], recent: ["还没有浏览记录", "打开过的作品会按时间出现在这里。"] }[view.scope] || ["这里没有图片", "这个文件夹里没有支持的图片格式（JPG、PNG、GIF、WebP、BMP、TIFF）。"];
  return `<div class="empty"><div class="box"><div class="ic">${icon(view.scope === "fav" ? "heart" : "grid")}</div><h3>${text[0]}</h3><p>${text[1]}</p></div></div>`;
}
$("#grid-root").addEventListener("click", (e) => {
  const act = e.target.closest("[data-act]");
  if (act && act.dataset.act === "add") return addFolder();
  if (act && act.dataset.act === "open-dl") return ctx.openDownloader();
  if (act && act.dataset.act === "open-settings") return ctx.openSettings(act.dataset.page);
  if (act && act.dataset.act === "reconnect") return reconnect(act);
  if (act && act.dataset.act === "clear-filters") { view.q = ""; $("#q").value = ""; view.tags.clear(); view.filters = {}; ctx.setSetting({ rating: "all" }, { reload: true }); return; }
  const tile = e.target.closest(".tile"); if (!tile) return;
  const k = tile.dataset.k;
  if (e.target.closest("[data-fav]")) { const w = workByKey(k); setFav([k], !w.fav); return; }
  if (e.target.closest("[data-expand]")) return toggleExpand(k);
  if (e.shiftKey && view.anchor) selectRange(view.anchor, k, e.ctrlKey || e.metaKey);
  else if (e.ctrlKey || e.metaKey) { view.sel.has(k) ? view.sel.delete(k) : view.sel.add(k); view.anchor = k; }
  else { view.sel = new Set([k]); view.anchor = k; }
  view.cur = k;
  refreshSelection();
});
$("#grid-root").addEventListener("dblclick", (e) => {
  const tile = e.target.closest(".tile"); if (!tile || e.target.closest("[data-fav], [data-expand]")) return;
  ctx.openViewer(view.flat.indexOf(tile.dataset.k), +tile.dataset.pg || 0);
});
$("#grid-root").addEventListener("contextmenu", (e) => {
  const tile = e.target.closest(".tile"); if (!tile) return;
  e.preventDefault();
  const k = tile.dataset.k;
  if (!view.sel.has(k)) { view.sel = new Set([k]); view.anchor = k; }
  view.cur = k;
  refreshSelection();
  workMenu([...view.sel], e.clientX, e.clientY);
});
function selectRange(a, b, add) {
  const i = view.flat.indexOf(a), j = view.flat.indexOf(b);
  if (i < 0 || j < 0) return;
  const range = view.flat.slice(Math.min(i, j), Math.max(i, j) + 1);
  view.sel = add ? new Set([...view.sel, ...range]) : new Set(range);
}
export function refreshSelection() {
  // 只更新状态有变化的卡片，不遍历整个网格
  const next = new Set(view.sel);
  if (view.cur) next.add(view.cur);
  for (const k of new Set([...marked, ...next])) {
    const el = tileEls.get(k);
    if (!el) continue;
    el.classList.toggle("sel", view.sel.has(k));
    el.classList.toggle("cur", view.cur === k);
    (subEls.get(k) || []).forEach((s) => s.classList.toggle("sel", view.sel.has(k)));
  }
  marked = next;
  const multi = view.sel.size > 1;
  $$("#grid-root .grid").forEach((g) => g.classList.toggle("multi", multi));
  renderStatus(); renderBatch();
}
ctx.refreshSelection = refreshSelection;
export const workByKey = (k) => (view.index && view.index.get(k)) || view.works.find((w) => w.key === k);
ctx.workByKey = workByKey;
export function selectedKeys() { return view.sel.size ? [...view.sel] : view.cur ? [view.cur] : []; }

function renderStatus() {
  const w = view.cur && workByKey(view.cur);
  const total = view.works.length;
  if (view.sel.size > 1) $("#status").innerHTML = `<span>已选 <b>${view.sel.size}</b> 个作品</span><span class="sp"></span><span>共 ${fmtNum(total)} 个</span>`;
  else if (w) $("#status").innerHTML = `<span><b>${esc(w.pages[0].file)}</b></span>${w.pages[0].w ? `<span>${w.pages[0].w} × ${w.pages[0].h}</span>` : ""}<span>${fmtSize(w.pages[0].size)}</span>${w.pages.length > 1 ? `<span>${w.pages.length} 页</span>` : ""}<span>${fmtDate(w.posted)}</span><span class="sp"></span><span>共 ${fmtNum(total)} 个</span>`;
  else $("#status").innerHTML = `<span>共 ${fmtNum(total)} 个作品</span><span class="sp"></span><span>缩略图缓存 ${fmtSize(ctx.lib.cache?.size || 0)}</span>`;
}

// ================= 批量操作 =================
function renderBatch() {
  const n = view.sel.size, show = n > 1;
  $("#batch").hidden = !show;
  $("#main").classList.toggle("has-batch", show);
  if (!show) return;
  $("#batch-count").textContent = `已选 ${n} 个`;
  $("#batch-stars").innerHTML = [1, 2, 3, 4, 5].map((s) => `<button data-star="${s}" title="${s} 星">${icon("star")}</button>`).join("");
}
$("#batch").addEventListener("click", (e) => {
  const star = e.target.closest("[data-star]");
  const keys = [...view.sel];
  if (star) return setStars(keys, +star.dataset.star);
  const b = e.target.closest("[data-act]"); if (!b) return;
  runAction(b.dataset.act, keys);
});

// ================= 操作 =================
async function setFav(keys, on) {
  const before = keys.map((k) => [k, workByKey(k)?.fav]);
  await ctx.api.setFavorite(keys, on);
  keys.forEach((k) => { const w = workByKey(k); if (w) w.fav = on; });
  ctx.lib.totals.fav += on ? keys.length : -keys.length;
  afterChange();
  toast(on ? `已收藏 ${keys.length > 1 ? keys.length + " 个作品" : ""}` : "已取消收藏", on ? null : async () => {
    for (const [k, v] of before) { await ctx.api.setFavorite([k], !!v); const w = workByKey(k); if (w) w.fav = !!v; }
    ctx.lib.totals.fav += keys.length;
    afterChange();
  });
}
export async function setStars(keys, n) {
  await ctx.api.setStars(keys, n);
  keys.forEach((k) => { const w = workByKey(k); if (w) w.stars = n; });
  afterChange();
  toast(n ? `已评为 ${"★".repeat(n)}` : "已清除评分");
}
ctx.setFav = setFav;
ctx.setStars = setStars;
function afterChange() {
  if (view.scope === "fav") loadWorks(); else renderGrid();
  renderSide();
  ctx.onWorkChanged && ctx.onWorkChanged();
}
export async function sysAction(promise, okText) {
  const r = await promise;
  if (r === "preview") toast("这是预览版，桌面版里会执行这个操作");
  else if (okText) toast(okText);
}
ctx.sysAction = sysAction;
function pathsOf(keys) { return keys.flatMap((k) => workByKey(k)?.pages.map((p) => p.path) || []); }
ctx.pathsOf = pathsOf;
export async function runAction(act, keys) {
  if (!keys.length) return;
  const w = workByKey(keys[0]);
  switch (act) {
    case "open": ctx.openViewer(view.flat.indexOf(keys[0])); break;
    case "peek": ctx.openQuicklook(keys[0]); break;
    case "fav": setFav(keys, !keys.every((k) => workByKey(k)?.fav)); break;
    case "tag": ctx.promptTags(keys); break;
    case "copy": { const ok = await copyText(pathsOf(keys).join("\n"), ctx.api); toast(ok ? `已复制 ${pathsOf(keys).length} 个路径` : "复制失败"); break; }
    case "reveal": sysAction(ctx.api.reveal(pathsOf(keys).slice(0, 1))); break;
    case "external": sysAction(ctx.api.openExternal(w.pages[0].path)); break;
    case "wallpaper": sysAction(ctx.api.setWallpaper(w.pages[0].path), "已设为桌面壁纸"); break;
    case "pixiv": ctx.api.openUrl(`https://www.pixiv.net/artworks/${w.pid}`); break;
    case "export": sysAction(ctx.api.exportWorks(pathsOf(keys)), "导出完成"); break;
    case "artist": openScope("artist", w.artistKey); break;
    case "clear": view.sel.clear(); refreshSelection(); break;
  }
}
ctx.runAction = runAction;

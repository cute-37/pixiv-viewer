// 主区顶部：标题、搜索建议、排序 / 分级 / 布局、标签条、更多筛选
import { $, $$, esc, highlight, icon, fmtDate, fmtNum, debounce, clamp } from "./util.js";
import { app, ctx, view } from "./state.js";
import { loadWorks, sysAction } from "./grid.js";
import { syncChrome } from "./prefs.js";
import { avatarHTML, deleteFolder, folderById, openScope, renameFolder, renderSide } from "./sidebar.js";

// ================= 标题栏 =================
export function renderTitle() {
  const n = view.works.length;
  const imgs = view.works.reduce((s, w) => s + w.pages.length, 0);
  const area = $("#title-area");
  if (view.scope === "artist") {
    const a = ctx.artistByKey(view.artist);
    if (!a) return;
    const last = view.works.reduce((m, w) => Math.max(m, w.posted || 0), 0);
    area.innerHTML = `${avatarHTML(a, "big-avatar")}
      <div class="t"><div class="row"><h1>${esc(a.name)}</h1></div>
      <span class="meta">ID ${a.id} · ${fmtNum(n)} 个作品 · ${fmtNum(imgs)} 张${last ? " · 最近 " + fmtDate(last) : ""}</span></div>
      <div class="acts">
        ${a.id && ctx.api.dl ? `<button class="btn ghost" data-act="sync" title="检查这位画师有没有新作品并下载（会先让你确认）">${icon("sync")}更新</button>` : ""}
        <button class="btn ghost" data-act="pixiv" title="在 Pixiv 打开画师主页">${icon("ext")}主页</button>
        <button class="btn ghost" data-act="folder" title="打开文件夹">${icon("folder")}</button>
        <button class="btn ghost ${a.pinned ? "on" : ""}" data-act="pin" title="${a.pinned ? "取消置顶" : "置顶到侧栏"}">${icon("pin")}</button>
      </div>`;
  } else if (view.scope === "folder") {
    const fd = folderById(view.folder);
    if (!fd) return;
    area.innerHTML = `<span class="big-avatar avatar folder-ic">${icon("folder")}</span>
      <div class="t"><div class="row"><h1>${esc(fd.name)}</h1></div>
      <span class="meta">文件夹 · ${fd.artists.length} 位画师 · ${fmtNum(n)} 个作品 · ${fmtNum(imgs)} 张</span></div>
      <div class="acts">
        <button class="btn ghost" data-act="folder-rename" title="重命名">${icon("tag")}重命名</button>
        <button class="btn ghost" data-act="folder-delete" title="删除文件夹（画师和图片不受影响）">${icon("x")}</button>
      </div>`;
  } else {
    const titles = { all: "全部图片", recent: "最近查看", fav: "收藏" };
    let meta = view.scope === "all" ? `${fmtNum(imgs)} 张 · ${fmtNum(n)} 个作品 · ${ctx.lib.artists.length} 位画师` : `${fmtNum(n)} 个作品`;
    let more = "";
    if (ctx.lib.indexing) {
      // 扫描期间网格不自动重建（会打断操作），新发现的作品由用户点一下再载入
      const found = ctx.lib.totals ? ctx.lib.totals.works : 0;
      const shown = view.scope === "all" ? new Set(view.works.map((w) => w.key.split("#")[0])).size : found;
      meta += found > shown ? ` · 正在扫描，已发现 ${fmtNum(found)} 个作品` : " · 正在扫描…";
      if (found > shown) more = `<button class="linkbtn" data-act="refresh" title="把扫描到的新作品载入列表">载入新作品</button>`;
    }
    area.innerHTML = `<div class="t"><div class="row"><h1>${titles[view.scope]}</h1><span class="meta">${meta}</span>${more}</div></div>`;
  }
  document.title = (view.scope === "artist" ? ctx.artistByKey(view.artist)?.name : view.scope === "folder" ? folderById(view.folder)?.name
    : { all: "全部图片", recent: "最近查看", fav: "收藏" }[view.scope]) + " · Pixiv Viewer";
}
$("#title-area").addEventListener("click", (e) => {
  const b = e.target.closest("[data-act]"); if (!b) return;
  if (b.dataset.act === "refresh") return loadWorks();
  if (b.dataset.act === "folder-rename") return renameFolder(folderById(view.folder));
  if (b.dataset.act === "folder-delete") return deleteFolder(folderById(view.folder));
  const a = ctx.artistByKey(view.artist); if (!a) return;
  if (b.dataset.act === "sync") ctx.askSyncArtist(a);
  if (b.dataset.act === "pixiv") ctx.api.openUrl(`https://www.pixiv.net/users/${a.id}`);
  if (b.dataset.act === "folder") sysAction(ctx.api.reveal([a.folder]));
  if (b.dataset.act === "pin") ctx.api.pinArtist(a.key, !a.pinned).then(() => { a.pinned = !a.pinned; renderSide(); renderTitle(); });
});

// ================= 搜索建议 =================
let sugItems = [], sugIndex = 0;
const runSuggest = debounce(async () => {
  const text = $("#q").value.trim();
  if (!text) { $("#suggest").hidden = true; return; }
  const r = await ctx.api.suggest(text, ctx.S.showR18 === false);
  sugItems = [{ kind: "text", value: text }];
  let html = `<div class="opt" data-i="0">${icon("search")}<span class="lbl">搜索“${esc(text)}”</span><span class="n">Enter</span></div>`;
  if (r.artists.length) { html += `<div class="grp">画师</div>`; r.artists.forEach((a) => { sugItems.push({ kind: "artist", value: a.key }); html += `<div class="opt" data-i="${sugItems.length - 1}">${avatarHTML(a)}<span class="lbl">${highlight(a.name, text)}</span><span class="n">${a.id}</span></div>`; }); }
  if (r.tags.length) { html += `<div class="grp">标签</div>`; r.tags.forEach(([t, n]) => { sugItems.push({ kind: "tag", value: t }); html += `<div class="opt" data-i="${sugItems.length - 1}">${icon("tag")}<span class="lbl">${highlight(t, text)}</span><span class="n">${n}</span></div>`; }); }
  if (r.works.length) { html += `<div class="grp">作品</div>`; r.works.forEach((w) => { sugItems.push({ kind: "work", value: w }); html += `<div class="opt" data-i="${sugItems.length - 1}"><img src="${esc(ctx.api.thumbSrc(w.pages[0].thumb || w.pages[0].path))}" alt=""><span class="lbl">${highlight(w.title, text)} · ${esc(w.artistName)}</span><span class="n">${highlight(String(w.pid), text)}</span></div>`; }); }
  sugIndex = 0;
  $("#suggest").innerHTML = html;
  $("#suggest").hidden = false;
  markSug();
}, 120);
function markSug() { $$("#suggest .opt").forEach((o) => o.classList.toggle("act", +o.dataset.i === sugIndex)); }
function pickSug(i) {
  const it = sugItems[i]; if (!it) return;
  $("#suggest").hidden = true;
  if (it.kind === "text") { view.q = it.value; view.sel.clear(); loadWorks(true); }
  if (it.kind === "artist") openScope("artist", it.value);
  if (it.kind === "tag") { $("#q").value = ""; $("#search").classList.remove("has-text"); view.tags.add(it.value); view.sel.clear(); loadWorks(true); }
  if (it.kind === "work") { openScope("artist", it.value.artistKey); ctx.pendingOpen = it.value.key; }
  $("#q").blur();
}
$("#q").addEventListener("input", () => { $("#search").classList.toggle("has-text", !!$("#q").value); if (!$("#q").value && view.q) { view.q = ""; loadWorks(); } runSuggest(); });
$("#q").addEventListener("keydown", (e) => {
  const open = !$("#suggest").hidden;
  if (e.key === "ArrowDown" && open) { e.preventDefault(); sugIndex = Math.min(sugItems.length - 1, sugIndex + 1); markSug(); }
  else if (e.key === "ArrowUp" && open) { e.preventDefault(); sugIndex = Math.max(0, sugIndex - 1); markSug(); }
  else if (e.key === "Enter") { e.preventDefault(); if (open) pickSug(sugIndex); else if ($("#q").value.trim()) { view.q = $("#q").value.trim(); loadWorks(true); } }
  else if (e.key === "Escape") { e.stopPropagation(); if (open) $("#suggest").hidden = true; else { $("#q").value = ""; $("#search").classList.remove("has-text"); if (view.q) { view.q = ""; loadWorks(); } $("#q").blur(); } }
});
$("#q").addEventListener("focus", () => { if ($("#q").value.trim()) runSuggest(); });
$("#suggest").addEventListener("mousedown", (e) => { const o = e.target.closest(".opt"); if (o) { e.preventDefault(); pickSug(+o.dataset.i); } });
document.addEventListener("mousedown", (e) => { if (!e.target.closest("#search")) $("#suggest").hidden = true; });

// ================= 工具行：排序 / 分级 / 布局 / 尺寸 =================
$("#sort").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) ctx.setSetting({ sort: b.dataset.v }, { reload: true }); });
$("#rating").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) ctx.setSetting({ rating: b.dataset.v }, { reload: true }); });
$("#layout-quick").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) ctx.setSetting({ layout: b.dataset.v }, { grid: true }); });
$("#size-quick").addEventListener("input", (e) => ctx.setSetting({ tile: +e.target.value, density: "custom" }));
$("#content").addEventListener("wheel", (e) => {
  if (!e.ctrlKey) return;
  e.preventDefault();
  ctx.setSetting({ tile: clamp(ctx.S.tile + (e.deltaY < 0 ? 16 : -16), 110, 340), density: "custom" });
}, { passive: false });
$("#btn-mode").addEventListener("click", () => {
  const dark = document.documentElement.dataset.theme === "dark";
  ctx.setSetting({ mode: dark ? "light" : "dark" });
});

// ================= 标签条 + 全部标签 =================
export function renderTagRow() {
  const counts = new Map(view.scopeTags);
  const row = $("#tagrow");
  const chips = [];
  if (view.q) chips.push(`<button class="chip on" data-q="1" title="清除搜索">${icon("search", "s")}&nbsp;${esc(view.q)}<span class="x">×</span></button>`);
  for (const t of view.tags) chips.push(chipHTML(t, counts.get(t) ?? null, true));
  for (const [t, n] of view.scopeTags.slice(0, 30)) if (!view.tags.has(t)) chips.push(chipHTML(t, n, false));
  row.innerHTML = chips.join("");
  // 先一次性量完再隐藏：边量边改会让浏览器对整页（含上千张卡片）反复重新排版
  const limit = row.getBoundingClientRect().right;
  const overflow = [...row.children].filter((c) => c.getBoundingClientRect().right > limit + 0.5);
  overflow.forEach((c) => { c.hidden = true; });
  $("#tag-more-t").textContent = view.tags.size ? `全部标签 · 已选 ${view.tags.size}` : `全部标签 ${view.scopeTags.length}`;
}
const chipHTML = (t, n, on) => `<button class="chip ${on ? "on" : ""}" data-t="${esc(t)}" title="${on ? "取消筛选" : "按此标签筛选"}">${esc(t)}${n != null ? `<span class="cnt">${n}</span>` : ""}${on ? '<span class="x">×</span>' : ""}</button>`;
function toggleTag(t) {
  if (view.tags.has(t)) view.tags.delete(t); else view.tags.add(t);
  view.sel.clear();
  loadWorks(true);
}
ctx.filterByTag = (t) => { view.tags = new Set([t]); view.sel.clear(); loadWorks(true); };
let tagSort = "count";
export function renderTagPop() {
  const q = $("#tag-q").value.trim().toLowerCase();
  let list = view.scopeTags.filter(([t]) => !q || t.toLowerCase().includes(q));
  if (tagSort === "name") list = [...list].sort((a, b) => a[0].localeCompare(b[0], "ja"));
  const item = ([t, n]) => `<button class="chip ${view.tags.has(t) ? "on" : ""}" data-t="${esc(t)}">${highlight(t, q)}<span class="cnt">${n}</span></button>`;
  let html;
  if (tagSort === "count" && !q) {
    const common = list.filter(([, n]) => n >= 8), rest = list.filter(([, n]) => n < 8);
    html = (common.length ? `<div class="grp">常用 · ${common.length}</div>` + common.map(item).join("") : "")
      + (rest.length ? `<div class="grp">其他 · ${rest.length}</div>` + rest.map(item).join("") : "");
  } else {
    html = list.map(item).join("") || `<span style="color:var(--text-3)">没有匹配“${esc(q)}”的标签</span>`;
  }
  $("#tag-list").innerHTML = html;
  $("#tag-sel").innerHTML = [...view.tags].map((t) => chipHTML(t, null, true)).join("");
  const scope = view.scope === "artist" ? ctx.artistByKey(view.artist)?.name + " 的" : { all: "全部图片中的", fav: "收藏中的", recent: "最近查看中的" }[view.scope];
  $("#tag-scope").textContent = `${scope} ${view.scopeTags.length} 个标签`;
  $$("#tag-sort button").forEach((b) => b.classList.toggle("on", b.dataset.v === tagSort));
}
function placePop(pop, anchor) {
  const r = anchor.getBoundingClientRect(), box = app.getBoundingClientRect();
  pop.hidden = false;
  pop.style.left = Math.max(12, Math.min(r.left - box.left, box.width - pop.offsetWidth - 12)) + "px";
  pop.style.top = (r.bottom - box.top + 6) + "px";
}
function openTagPop() { closePops(); placePop($("#tagpop"), $("#tag-more")); renderTagPop(); $("#tag-q").value = ""; renderTagPop(); $("#tag-q").focus(); }
export function closePops() { $("#tagpop").hidden = true; $("#filterpop").hidden = true; }
ctx.closePops = closePops;
$("#tag-more").addEventListener("click", (e) => { e.stopPropagation(); $("#tagpop").hidden ? openTagPop() : closePops(); });
$("#tagrow").addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  if (b.dataset.q) { view.q = ""; $("#q").value = ""; $("#search").classList.remove("has-text"); loadWorks(true); return; }
  toggleTag(b.dataset.t);
});
for (const id of ["#tag-list", "#tag-sel"]) $(id).addEventListener("click", (e) => { const b = e.target.closest("button[data-t]"); if (b) toggleTag(b.dataset.t); });
$("#tag-q").addEventListener("input", renderTagPop);
$("#tag-q").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { const b = $("#tag-list .chip"); if (b) { toggleTag(b.dataset.t); $("#tag-q").value = ""; } }
  if (e.key === "Escape") { e.stopPropagation(); closePops(); }
});
$("#tag-sort").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) { tagSort = b.dataset.v; renderTagPop(); } });
$("#tag-clear").addEventListener("click", () => { view.tags.clear(); loadWorks(true); });
document.addEventListener("mousedown", (e) => {
  if (!e.target.closest(".pop") && !e.target.closest("#tag-more") && !e.target.closest("#btn-filters")) closePops();
});
new ResizeObserver(() => renderTagRow()).observe($("#tagstrip"));

// ================= 更多筛选 =================
function renderFilterPop() {
  const f = view.filters;
  const seg = (key, opts) => `<div class="seg sm" data-f="${key}">${opts.map(([v, l]) => `<button data-v="${v}" class="${(f[key] || "") === v ? "on" : ""}">${l}</button>`).join("")}</div>`;
  $("#filterpop").innerHTML = `
    <div class="frow"><span>方向</span>${seg("orientation", [["", "不限"], ["portrait", "竖图"], ["landscape", "横图"], ["square", "方图"]])}</div>
    <div class="frow"><span>AI 作品</span>${seg("ai", [["", "包含"], ["exclude", "排除"], ["only", "只看 AI"]])}</div>
    <div class="frow"><span>我的评分</span>${seg("minStars", [["", "不限"], ["3", "★3 以上"], ["4", "★4 以上"], ["5", "★5"]])}</div>
    <div class="frow"><span>多页作品</span>${seg("multiPage", [["", "不限"], ["1", "只看多页"]])}</div>
    <div class="pop-foot"><span>筛选只影响当前列表</span><span class="sp"></span><button class="linkbtn" id="f-clear">清除筛选</button></div>`;
}
$("#btn-filters").addEventListener("click", (e) => {
  e.stopPropagation();
  if (!$("#filterpop").hidden) return closePops();
  closePops(); renderFilterPop(); placePop($("#filterpop"), $("#btn-filters"));
  const pop = $("#filterpop"), r = $("#btn-filters").getBoundingClientRect(), box = app.getBoundingClientRect();
  pop.style.left = Math.max(12, r.right - box.left - pop.offsetWidth) + "px";
});
$("#filterpop").addEventListener("click", (e) => {
  if (e.target.id === "f-clear") { view.filters = {}; renderFilterPop(); syncChrome(); loadWorks(true); return; }
  const b = e.target.closest("button[data-v]"), g = e.target.closest("[data-f]"); if (!b || !g) return;
  const key = g.dataset.f, v = b.dataset.v;
  view.filters[key] = key === "minStars" ? (v ? +v : 0) : key === "multiPage" ? !!v : v;
  renderFilterPop(); syncChrome(); loadWorks(true);
});

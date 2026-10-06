// 侧边栏：导航、画师列表、画师文件夹、拖动画师到文件夹
import { $, $$, esc, highlight, icon, fmtNum, toast } from "./util.js";
import { ctx, view } from "./state.js";
import { loadWorks, sysAction } from "./grid.js";
import { openMenu } from "./menu.js";
import { persist } from "./prefs.js";
import { renderTitle } from "./toolbar.js";

// ================= 侧栏 =================
const NAV = [
  ["all", "grid", "全部图片", () => ctx.lib.totals.images],
  ["recent", "clock", "最近查看", () => ctx.lib.totals.recent],
  ["fav", "heart", "收藏", () => ctx.lib.totals.fav],
];
export function renderSide() {
  $("#nav").innerHTML = NAV.map(([k, ic, label, n]) =>
    `<button class="row-btn ${view.scope === k ? "on" : ""}" data-nav="${k}">${icon(ic)}<span class="lbl">${label}</span><span class="n">${fmtNum(n())}</span></button>`).join("");
  const f = $("#artist-filter").value.trim().toLowerCase();
  let list = ctx.lib.artists.filter((a) => !f || a.name.toLowerCase().includes(f) || String(a.id).includes(f));
  const by = ctx.S.artistSort;
  list.sort(by === "count" ? (a, b) => b.count - a.count : by === "updated" ? (a, b) => b.updated - a.updated : (a, b) => a.name.localeCompare(b.name, "ja"));
  const pinned = list.filter((a) => a.pinned), rest = list.filter((a) => !a.pinned);
  const row = (a) => `<button class="row-btn ${view.scope === "artist" && view.artist === a.key ? "on" : ""}" data-artist="${esc(a.key)}" title="${esc(a.name)} · ${a.id}" role="option">
      ${avatarHTML(a)}<span class="lbl">${highlight(a.name, f)}</span>${a.pinned ? icon("pin", "pin") : ""}<span class="n">${fmtNum(a.count)}</span></button>`;
  // 文件夹：给画师分类用。点名字看里面所有画师的作品，点箭头展开看有哪些画师
  const folders = ctx.lib.folders || [], open = new Set(ctx.S.openFolders || []);
  const byKey = new Map(ctx.lib.artists.map((a) => [a.key, a]));
  const folderHTML = f ? "" : folders.map((fd) => {
    const members = fd.artists.map((k) => byKey.get(k)).filter(Boolean);
    const isOpen = open.has(fd.id);
    return `<button class="row-btn folder ${view.scope === "folder" && String(view.folder) === String(fd.id) ? "on" : ""} ${isOpen ? "open" : ""}" data-folder="${fd.id}" title="${esc(fd.name)}">
        <span class="tw" data-fold="${fd.id}" title="${isOpen ? "收起" : "展开"}">${icon("chev")}</span>${icon("folder")}<span class="lbl">${esc(fd.name)}</span><span class="n">${members.length}</span></button>
      ${isOpen ? (members.map((a) => row(a).replace('class="row-btn ', 'class="row-btn sub ')).join("") || `<div class="grp sub">还没有画师：在画师上点右键 → 加入文件夹</div>`) : ""}`;
  }).join("");
  // 重画列表会让滚动位置丢失（头像还没加载完时尤其明显），先记下再还原
  const box = $("#artists"), top = box.scrollTop;
  box.innerHTML = (folderHTML ? `<div class="grp">文件夹</div>` + folderHTML + (pinned.length ? "" : `<div class="grp">全部</div>`) : "")
    + (pinned.length && !f ? `<div class="grp">置顶</div>` + pinned.map(row).join("") + `<div class="grp">全部</div>` : "")
    + ((f ? list : rest).map(row).join("") || `<div class="grp">没有匹配的画师</div>`);
  box.scrollTop = top;
  $("#artist-total").textContent = ctx.lib.artists.length;
  $$(".side-rail [data-nav]").forEach((b) => b.classList.toggle("on", b.dataset.nav === view.scope));
}
export function avatarHTML(a, cls = "") {
  const style = a.avatar ? `background-image:url('${a.avatar}')` : `background:${a.color || "#8b8b93"}`;
  return `<span class="avatar ${cls}" style="${style}">${a.avatar ? "" : esc(a.name.slice(0, 1))}</span>`;
}
ctx.avatarHTML = avatarHTML;
ctx.artistByKey = (key) => ctx.lib.artists.find((a) => a.key === key);

export function openScope(scope, artist = null) {
  view.expanded.clear();
  view.scope = scope;
  view.artist = scope === "artist" ? artist : null;
  view.folder = scope === "folder" ? artist : null;     // 第二个参数：画师页是画师，文件夹页是文件夹的编号
  view.tags.clear();
  view.q = "";
  $("#q").value = "";
  $("#search").classList.remove("has-text");
  view.sel.clear();
  view.cur = null;
  ctx.closeViewer && ctx.closeViewer(true);
  renderSide();
  loadWorks(true);
  ctx.S.lastScope = { scope, artist: view.artist, folder: view.folder };
  persist();
}
ctx.openScope = openScope;

$("#nav").addEventListener("click", (e) => { const b = e.target.closest("[data-nav]"); if (b) openScope(b.dataset.nav); });
$(".side-rail").addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  if (b.dataset.nav) openScope(b.dataset.nav);
  if (b.dataset.act === "settings") ctx.openSettings();
  if (b.dataset.act === "add-folder") addFolder();
  if (b.dataset.act === "downloader" && ctx.openDownloader) ctx.openDownloader();
});
function flipFolder(id) {
  const open = new Set(ctx.S.openFolders || []);
  open.has(id) ? open.delete(id) : open.add(id);
  ctx.S.openFolders = [...open]; persist();
  renderSide();
}
$("#artists").addEventListener("click", (e) => {
  if (dragJustEnded) return;                             // 刚拖完松手，不当作点击
  const fold = e.target.closest("[data-fold]");
  if (fold) return flipFolder(+fold.dataset.fold);       // 小箭头：只展开 / 收起，不切换页面
  const fd = e.target.closest("[data-folder]");
  if (fd) {
    // 双击：展开 / 收起。第一下单击已经重绘过侧栏，dblclick 事件落不到新按钮上，所以按连击次数判断
    // 连续快速点击时浏览器会一直往上数（3、4、5…），所以每逢偶数下都算一次双击
    if (e.detail > 1) { if (e.detail % 2 === 0) flipFolder(+fd.dataset.folder); return; }
    return openScope("folder", +fd.dataset.folder);
  }
  const b = e.target.closest("[data-artist]"); if (b) openScope("artist", b.dataset.artist);
});
$("#btn-folder-add").addEventListener("click", () => newFolder());

// ---------- 拖动画师到文件夹 ----------
// 自己用指针事件实现（不依赖系统的拖放）：按住画师拖出一小段距离后开始，文件夹和顶部的两个投放区会亮起来
let dragJustEnded = false;
(function initArtistDrag() {
  const box = $("#artists");
  let start = null, drag = null, scrollTimer = 0;
  const targetAt = (x, y) => {
    const el = document.elementFromPoint(x, y);
    return el && el.closest("#artists [data-folder], #artist-drop [data-drop]");
  };
  const mark = (t) => {
    if (drag.over === t) return;
    if (drag.over) drag.over.classList.remove("drop");
    drag.over = t;
    if (t) t.classList.add("drop");
  };
  function begin(e) {
    const a = ctx.artistByKey(start.key);
    if (!a) return;
    const from = start.row.classList.contains("sub") ? (() => { let p = start.row.previousElementSibling; while (p && !p.dataset.folder) p = p.previousElementSibling; return p ? folderById(p.dataset.folder) : null; })() : null;
    const ghost = document.createElement("div");
    ghost.className = "drag-ghost";
    ghost.innerHTML = `${avatarHTML(a)}<span>${esc(a.name)}</span>`;
    document.body.append(ghost);
    // 顶部的投放区：新建文件夹；如果是从某个文件夹里拖出来的，还可以移出
    const zone = document.createElement("div");
    zone.id = "artist-drop";
    zone.innerHTML = `<div data-drop="new">${icon("folderplus")}新建文件夹</div>${from ? `<div data-drop="out">${icon("x")}移出“${esc(from.name)}”</div>` : ""}`;
    box.before(zone);
    document.body.classList.add("dragging-artist");
    ctx.hidePeek && ctx.hidePeek();
    drag = { a, from, ghost, zone, over: null };
    move(e);
  }
  function move(e) {
    drag.ghost.style.transform = `translate(${e.clientX + 12}px, ${e.clientY + 8}px)`;
    mark(targetAt(e.clientX, e.clientY));
    // 拖到列表上下边缘时自动滚动
    const r = box.getBoundingClientRect();
    const dy = e.clientY < r.top + 28 ? -10 : e.clientY > r.bottom - 28 ? 10 : 0;
    clearInterval(scrollTimer);
    if (dy) scrollTimer = setInterval(() => { box.scrollTop += dy; }, 16);
  }
  async function end(e, cancelled) {
    clearInterval(scrollTimer);
    const d = drag; drag = null; start = null;
    if (!d) return;
    const t = cancelled ? null : targetAt(e.clientX, e.clientY);
    d.ghost.remove(); d.zone.remove();
    if (d.over) d.over.classList.remove("drop");
    document.body.classList.remove("dragging-artist");
    dragJustEnded = true; setTimeout(() => { dragJustEnded = false; }, 0);     // 松手时那一下不算点击
    if (!t) return;
    if (t.dataset.drop === "new") return newFolder([d.a.key]);
    if (t.dataset.drop === "out") return toggleFolder(d.from, d.a);
    const fd = folderById(t.dataset.folder);
    if (!fd) return;
    if (fd.artists.includes(d.a.key)) return toast(`${d.a.name} 已经在“${fd.name}”里了`);
    await toggleFolder(fd, d.a);
  }
  box.addEventListener("pointerdown", (e) => {
    const row = e.target.closest("[data-artist]");
    if (e.button !== 0 || !row) return;
    start = { x: e.clientX, y: e.clientY, key: row.dataset.artist, row, id: e.pointerId };
  });
  document.addEventListener("pointermove", (e) => {
    if (drag) return move(e);
    if (!start || e.pointerId !== start.id) return;
    if (!(e.buttons & 1)) { start = null; return; }
    if (Math.abs(e.clientX - start.x) + Math.abs(e.clientY - start.y) > 7) begin(e);
  });
  document.addEventListener("pointerup", (e) => { if (drag) end(e, false); else start = null; });
  document.addEventListener("pointercancel", (e) => { if (drag) end(e, true); else start = null; });
  document.addEventListener("keydown", (e) => { if (drag && e.key === "Escape") { e.stopPropagation(); end(e, true); } }, true);
})();

// ---------- 画师文件夹 ----------
export const folderById = (id) => (ctx.lib.folders || []).find((x) => String(x.id) === String(id));
async function newFolder(artistKeys = []) {
  const name = await ctx.askText("新建文件夹", "", "文件夹的名字，例如 风景、常看");
  if (!name) return;
  const id = await ctx.api.folderCreate(name, artistKeys);
  ctx.S.openFolders = [...new Set([...(ctx.S.openFolders || []), id])]; persist();
  await reloadLibrary();
  toast(artistKeys.length ? `已新建“${name}”并加入` : `已新建文件夹“${name}”，在画师上点右键可以加入`);
}
export async function renameFolder(fd) {
  const name = await ctx.askText("重命名文件夹", fd.name);
  if (!name || name === fd.name) return;
  await ctx.api.folderRename(fd.id, name);
  await reloadLibrary();
}
export async function deleteFolder(fd) {
  await ctx.api.folderDelete(fd.id);
  if (view.scope === "folder" && String(view.folder) === String(fd.id)) openScope("all");
  await reloadLibrary();
  toast(`已删除文件夹“${fd.name}”（画师和图片不受影响）`, async () => {
    await ctx.api.folderCreate(fd.name, fd.artists);
    await reloadLibrary();
  });
}
async function toggleFolder(fd, a) {
  const inside = fd.artists.includes(a.key);
  await ctx.api.folderSet(fd.id, [a.key], !inside);
  await reloadLibrary();
  if (view.scope === "folder" && String(view.folder) === String(fd.id)) loadWorks();
  toast(inside ? `已把 ${a.name} 移出“${fd.name}”` : `已把 ${a.name} 加入“${fd.name}”`);
}
function folderMenu(fd, x, y) {
  openMenu([
    ["打开", () => openScope("folder", fd.id), "folder"],
    ["重命名…", () => renameFolder(fd), "tag"],
    "-",
    ["删除文件夹", () => deleteFolder(fd), "x", "", true],
  ], x, y);
}
$("#artists").addEventListener("contextmenu", (e) => {
  const fd = e.target.closest("[data-folder]");
  if (fd) { e.preventDefault(); return folderMenu(folderById(fd.dataset.folder), e.clientX, e.clientY); }
  const b = e.target.closest("[data-artist]"); if (!b) return;
  e.preventDefault();
  artistMenu(ctx.artistByKey(b.dataset.artist), e.clientX, e.clientY);
});
$("#artist-filter").addEventListener("input", renderSide);
$("#btn-artist-sort").addEventListener("click", (e) => {
  const r = e.currentTarget.getBoundingClientRect();
  openMenu([
    ["按名称", () => ctx.setSetting({ artistSort: "name" }) || renderSide(), ctx.S.artistSort === "name" ? "check" : ""],
    ["按作品数", () => ctx.setSetting({ artistSort: "count" }) || renderSide(), ctx.S.artistSort === "count" ? "check" : ""],
    ["按最近更新", () => ctx.setSetting({ artistSort: "updated" }) || renderSide(), ctx.S.artistSort === "updated" ? "check" : ""],
  ], r.left, r.bottom + 4);
});
$("#btn-collapse").addEventListener("click", () => ctx.setSetting({ sidebarCollapsed: true }));
$("#btn-expand").addEventListener("click", () => ctx.setSetting({ sidebarCollapsed: false }));
$("#btn-add-folder").addEventListener("click", () => addFolder());
$("#btn-settings").addEventListener("click", () => ctx.openSettings());

export async function addFolder() {
  const path = await ctx.api.addFolder();
  if (path) { await reloadLibrary(); toast(`已添加 ${path}`); }
  else if (ctx.api.isMock) toast("预览版不能选择文件夹；桌面版会打开系统的文件夹选择框");
}
ctx.addFolder = addFolder;
export async function reloadLibrary() { ctx.lib = await ctx.api.getLibrary(); renderSide(); renderTitle(); }
ctx.reloadLibrary = reloadLibrary;

function artistMenu(a, x, y) {
  openMenu([
    [a.pinned ? "取消置顶" : "置顶", async () => { await ctx.api.pinArtist(a.key, !a.pinned); a.pinned = !a.pinned; renderSide(); renderTitle(); }, "pin"],
    ...(a.id && ctx.askSyncArtist ? [["检查更新并下载…", () => ctx.askSyncArtist(a), "sync"]] : []),
    ["在资源管理器中打开", () => sysAction(ctx.api.reveal([a.folder])), "folder"],
    ["在 Pixiv 打开主页", () => ctx.api.openUrl(`https://www.pixiv.net/users/${a.id}`), "ext"],
    "-",
    { html: `<div class="mhead">加入文件夹</div>` },
    ...(ctx.lib.folders || []).map((fd) => [fd.name, () => toggleFolder(fd, a), fd.artists.includes(a.key) ? "check" : ""]),
    ["新建文件夹并加入…", () => newFolder([a.key]), "folderplus"],
  ], x, y);
}

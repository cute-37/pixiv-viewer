// 右键菜单：通用菜单 + 作品菜单
import { $, $$, esc, icon } from "./util.js";
import { ctx, view } from "./state.js";
import { runAction, setStars, toggleExpand, workByKey } from "./grid.js";
import { addFolder } from "./sidebar.js";

// ================= 右键菜单 =================
let menuItems = [];
export function openMenu(items, x, y) {
  const m = $("#menu");
  menuItems = [];
  m.innerHTML = items.map((it) => {
    if (it === "-") return "<hr>";
    if (it.html) return it.html;
    menuItems.push(it);
    const [label, , ic, key, danger] = it;
    return `<button data-mi="${menuItems.length - 1}" class="${danger ? "danger" : ""}" role="menuitem">${ic ? icon(ic) : `<span style="width:16px"></span>`}${esc(label)}${key ? `<span class="kbd">${key}</span>` : ""}</button>`;
  }).join("");
  m.hidden = false;
  const r = m.getBoundingClientRect();
  m.style.left = Math.min(x, window.innerWidth - r.width - 8) + "px";
  m.style.top = Math.min(y, window.innerHeight - r.height - 8) + "px";
  const first = m.querySelector("button"); if (first) first.focus({ preventScroll: true });
}
ctx.openMenu = openMenu;
function closeMenu() { $("#menu").hidden = true; }
$("#menu").addEventListener("click", (e) => {
  const s = e.target.closest("[data-mstar]");
  if (s) { closeMenu(); return setStars(JSON.parse($("#menu").dataset.keys), +s.dataset.mstar); }
  const b = e.target.closest("[data-mi]"); if (!b) return;
  closeMenu(); menuItems[+b.dataset.mi][1]();
});
$("#menu").addEventListener("keydown", (e) => {
  const items = $$("#menu button[data-mi]"), i = items.indexOf(document.activeElement);
  if (e.key === "ArrowDown") { e.preventDefault(); (items[i + 1] || items[0]).focus(); }
  if (e.key === "ArrowUp") { e.preventDefault(); (items[i - 1] || items[items.length - 1]).focus(); }
  if (e.key === "Escape") { e.stopPropagation(); closeMenu(); }
});
document.addEventListener("mousedown", (e) => { if (!e.target.closest("#menu")) closeMenu(); });
window.addEventListener("blur", closeMenu);
export function workMenu(keys, x, y) {
  const many = keys.length > 1, w = workByKey(keys[0]);
  const allFav = keys.every((k) => workByKey(k)?.fav);
  const items = [];
  if (!many) items.push(["打开", () => runAction("open", keys), "", "Enter"], ["快速预览", () => runAction("peek", keys), "", "Space"]);
  if (!many && w.pages.length > 1) items.push([view.expanded.has(w.key) ? "收起全部页" : `展开全部 ${w.pages.length} 页`, () => toggleExpand(w.key), "pages"]);
  if (!many) items.push("-");
  items.push([allFav ? "取消收藏" : "收藏", () => runAction("fav", keys), "heart", "F"]);
  items.push({ html: `<div class="mstars" title="评分">${[1, 2, 3, 4, 5].map((s) => `<button data-mstar="${s}">${icon("star")}</button>`).join("")}</div>` });
  items.push(["添加标签…", () => runAction("tag", keys), "tag", "T"], "-");
  items.push([many ? `复制 ${keys.length} 个路径` : "复制路径", () => runAction("copy", keys), "copy", "Ctrl C"]);
  items.push(["在资源管理器中显示", () => runAction("reveal", keys), "folder"]);
  if (!many) {
    items.push(["用默认程序打开", () => runAction("external", keys), "ext"]);
    items.push(["设为桌面壁纸", () => runAction("wallpaper", keys), "wall"]);
    items.push("-", ["在 Pixiv 打开作品", () => runAction("pixiv", keys), "ext"]);
    if (view.scope !== "artist") items.push([`查看 ${w.artistName} 的全部作品`, () => runAction("artist", keys), "grid"]);
  }
  items.push("-", [many ? `导出 ${keys.length} 个作品…` : "导出…", () => runAction("export", keys), "export"]);
  openMenu(items, x, y);
  $("#menu").dataset.keys = JSON.stringify(keys);
}
ctx.workMenu = workMenu;

$("#btn-more").addEventListener("click", (e) => {
  const r = e.currentTarget.getBoundingClientRect(), S = ctx.S;
  openMenu([
    ["按月分组", () => ctx.setSetting({ groupByMonth: !S.groupByMonth }, { grid: true }), S.groupByMonth ? "check" : ""],
    ["合并多页作品", () => ctx.setSetting({ mergePages: !S.mergePages }, { reload: true }), S.mergePages ? "check" : ""],
    ["显示标签条", () => ctx.setSetting({ tagchips: S.tagchips === "on" ? "off" : "on" }), S.tagchips === "on" ? "check" : ""],
    ["显示底部状态条", () => ctx.setSetting({ status: S.status === "on" ? "off" : "on" }), S.status === "on" ? "check" : ""],
    "-",
    ["添加文件夹…", addFolder, "plus"],
    ["设置", () => ctx.openSettings(), "gear", ctx.keymap ? ctx.keymap.label("settings") : ""],
    ["快捷键", () => ctx.openSettings("keys"), "cmd", "?"],
  ], r.right - 200, r.bottom + 4);
});

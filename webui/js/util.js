// 小工具：DOM 查询、转义、格式化、防抖、Toast
export const $ = (sel, el = document) => el.querySelector(sel);
export const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];

export const esc = (text) => String(text ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

export function highlight(text, q) {
  if (!q) return esc(text);
  const i = text.toLowerCase().indexOf(q.toLowerCase());
  if (i < 0) return esc(text);
  return esc(text.slice(0, i)) + "<mark>" + esc(text.slice(i, i + q.length)) + "</mark>" + esc(text.slice(i + q.length));
}

export const icon = (name, cls = "") => `<svg class="i ${cls}"><use href="#i-${name}"/></svg>`;

export function fmtDate(ts) {
  const d = new Date(ts * 1000);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}
export function fmtMonth(key) {
  const [y, m] = key.split("-");
  return `${y} 年 ${+m} 月`;
}
export function fmtSize(bytes) {
  if (bytes >= 1 << 30) return (bytes / (1 << 30)).toFixed(1) + " GB";
  if (bytes >= 1 << 20) return (bytes / (1 << 20)).toFixed(1) + " MB";
  if (bytes >= 1 << 10) return Math.round(bytes / (1 << 10)) + " KB";
  return bytes + " B";
}
export const fmtNum = (n) => Number(n).toLocaleString("en-US");

export function debounce(fn, ms) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

export const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

let toastTimer = null;
let toastUndo = null;
/** 底部提示；传入 undo 回调时显示“撤销”按钮 */
export function toast(text, undo = null, ms = 2600) {
  const el = $("#toast");
  $("#toast-t").textContent = text;
  const btn = $("#toast-undo");
  btn.hidden = !undo;
  toastUndo = undo;
  el.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), undo ? Math.max(ms, 5000) : ms);
}
export function initToast() {
  $("#toast-undo").addEventListener("click", () => {
    const fn = toastUndo;
    toastUndo = null;
    $("#toast").classList.remove("show");
    if (fn) fn();
  });
}

/** 复制文本：优先用浏览器剪贴板，失败时交给后端 */
export async function copyText(text, api) {
  try { await navigator.clipboard.writeText(text); return true; } catch (e) { /* 继续尝试后端 */ }
  try { return await api.copyText(text); } catch (e) { return false; }
}

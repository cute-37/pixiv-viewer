// 重要更新提醒：旧版本有严重问题（比如 Pixiv 的接口变了、下载会全部失败）时，告诉用户该更新了。
// 通知是后端从仓库里的一个小文件读来的（见 webapp/notices.py）；这里只负责显示。
// important：打开软件时弹出来，可以“稍后”，但每次启动都会再提醒；download：只在“下载与更新”里提示。
// 通知里的文字一律当纯文本显示。
import { esc, icon } from "./util.js";

const AGAIN_EVERY = 6 * 3600 * 1000;      // 软件一直开着的话，隔这么久再问一次后端（它自己决定要不要重新读）

export function initNotices(ctx) {
  if (!ctx.api.notices) return;
  ctx.notices = [];
  // 通知里没有当前语言的文字时：日语、英语的用户看英文，其余看中文
  const pick = (t) => (t && (t[ctx.S.lang || "zh"] || ((ctx.S.lang || "zh") === "zh" ? t.zh || t.en : t.en || t.zh))) || "";
  const shown = new Set();               // 这次运行已经弹过的

  /** 下载相关的地方用：一条提示的 HTML（没有要提醒的返回空字符串） */
  ctx.noticeBanner = () => ctx.notices.map((n) => `<div class="dl-note lv-warn notice">${icon("warn")}<div><b>${esc(pick(n.title))}</b>
      <small>${esc(pick(n.text))}</small></div><button class="btn primary" data-notice-update>去更新</button></div>`).join("");

  function popup(n) {
    shown.add(n.id);
    const box = document.createElement("div");
    box.className = "closeask notice-ask";
    box.innerHTML = `<div class="closeask-card" role="alertdialog" aria-label="${esc(pick(n.title))}">
      <h3>${icon("warn")}${esc(pick(n.title))}</h3>
      <p>${esc(pick(n.text))}</p>
      <p class="sub">你现在用的是 ${esc(ctx.lib.version || "")}，更新到 ${esc(n.below)} 或更高的版本后这条提示会消失。更新不会改动你的图片和数据。</p>
      <div class="closeask-actions"><button class="btn ghost" data-later>稍后</button><span class="sp"></span><button class="btn primary" data-notice-update>去更新</button></div></div>`;
    box.addEventListener("click", (e) => { if (e.target.closest("[data-later], [data-notice-update]")) box.remove(); });
    document.body.append(box);
  }

  async function check(force) {
    let r = null;
    try { r = await ctx.api.notices(!!force); } catch (e) { return; }
    ctx.notices = (r && r.items) || [];
    const first = ctx.notices.find((n) => n.level === "important" && !shown.has(n.id));
    if (first && !document.querySelector(".notice-ask")) popup(first);
    if (ctx.onNotices) ctx.onNotices();
  }
  ctx.checkNotices = check;
  // “去更新”：打开“设置 → 关于”，那里有检查和下载更新的按钮
  document.addEventListener("click", (e) => { if (e.target.closest("[data-notice-update]")) ctx.openSettings("about"); });
  check();
  setInterval(check, AGAIN_EVERY);
}

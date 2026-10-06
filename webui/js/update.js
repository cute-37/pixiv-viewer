// 软件更新：检查 → 下载 → 重启替换。显示在“设置 → 关于”里。
// 每一步都要点了才做；状态放在模块里，所以关掉设置再打开，进度还在。
import { esc, fmtSize, toast } from "./util.js";

const st = {
  phase: "idle",      // idle | checking | latest | found | downloading | unpacking | ready | applying | error
  info: null,         // 检查的结果
  done: 0, total: 0,
  error: "",
};
let timer = 0, host = null, api = null, current = "";

const btn = (act, text, cls = "") => `<button class="btn ${cls}" data-upd="${act}">${text}</button>`;
const row = (title, hint, ctl) => `<div class="set"><div class="t">${title}${hint ? `<small>${hint}</small>` : ""}</div><div class="ctl">${ctl}</div></div>`;

function render() {
  if (!host || !host.isConnected) return;
  const i = st.info || {};
  const version = `当前版本 ${esc(current || i.current || "")}`;
  let html = "";
  if (st.phase === "idle" || st.phase === "checking" || st.phase === "latest") {
    const note = st.phase === "latest" ? (i.latest ? `已经是最新版本（${esc(i.latest)}）。` : `${esc(i.notes || "还没有发布过版本")}。`) : "点了才会联网（访问 GitHub），不会自动检查。";
    html = row("软件更新", `${version}。${note}`, btn("check", st.phase === "checking" ? "正在检查…" : "检查更新", st.phase === "checking" ? "busy" : ""));
  } else if (st.phase === "error") {
    html = row("软件更新", `${version}。<span class="upd-err">${esc(st.error)}</span>`, btn("check", "重试") + (i.page ? btn("page", "打开发布页面") : ""));
  } else {
    const head = `有新版本 ${esc(i.latest)}${i.size ? `（${fmtSize(i.size)}）` : ""}`;
    if (st.phase === "found") {
      html = row(head, i.canApply ? `${version}。下载后需要重启程序才会换成新版本。` : `${version}。${esc(i.reason)}`,
        (i.canApply ? btn("start", "下载") : "") + (i.page ? btn("page", "打开发布页面") : ""));
    } else if (st.phase === "downloading" || st.phase === "unpacking") {
      const pct = st.total ? Math.min(100, Math.round((st.done / st.total) * 100)) : 0;
      html = row(head, st.phase === "unpacking" ? "正在解压…" : `正在下载 ${fmtSize(st.done)} / ${fmtSize(st.total)}`,
        `<div class="upd-bar"><i style="width:${st.phase === "unpacking" ? 100 : pct}%"></i></div><output>${st.phase === "unpacking" ? "" : pct + "%"}</output>${st.phase === "downloading" ? btn("cancel", "取消") : ""}`);
    } else if (st.phase === "ready") {
      html = row(`新版本 ${esc(i.latest)} 已经下载好`, "点“重启并更新”后程序会关闭，几秒钟后自动以新版本重新打开。正在进行的下载会在处理完当前文件后停止；你的数据（data 文件夹）不会被改动。",
        btn("apply", "重启并更新", "primary"));
    } else if (st.phase === "applying") {
      html = row(`正在更新到 ${esc(i.latest)}`, "程序马上关闭，稍后自动重新打开。", "");
    }
    if (i.notes && st.phase !== "applying") html += `<div class="upd-notes"><b>这一版的变化</b>${esc(i.notes)}</div>`;
  }
  host.innerHTML = html;
}

function fail(error) { st.phase = "error"; st.error = String(error || "出错了"); render(); }

async function poll() {
  clearTimeout(timer);
  const r = await api.updateStatus();
  if (!r.ok) return fail(r.error);
  st.done = r.done; st.total = r.total;
  if (r.state === "downloading" || r.state === "unpacking") { st.phase = r.state; timer = setTimeout(poll, 400); }
  else if (r.state === "ready") st.phase = "ready";
  else if (r.state === "error") return fail(r.error);
  else st.phase = st.info && st.info.newer ? "found" : "idle";        // 取消了
  render();
}

async function act(name) {
  if (name === "check") {
    if (st.phase === "checking") return;
    st.phase = "checking"; render();
    const r = await api.updateCheck();
    if (!r.ok) return fail(r.error);
    st.info = r;
    st.phase = r.newer ? "found" : "latest";
    render();
  } else if (name === "start") {
    const r = await api.updateStart();
    if (!r.ok) return fail(r.error);
    st.phase = "downloading"; st.done = 0; st.total = r.total || 0;
    render(); poll();
  } else if (name === "cancel") {
    await api.updateCancel(); poll();
  } else if (name === "apply") {
    st.phase = "applying"; render();
    const r = await api.updateApply();
    if (!r.ok) return fail(r.error);
    if (r.preview) { st.phase = "ready"; render(); toast("预览版不会真的更新"); }
  } else if (name === "page") {
    api.openUrl(st.info.page);
  }
}

/** 把“软件更新”这一块放进 el。没有更新接口（很旧的后端）时什么都不显示。 */
export function mountUpdate(ctx, el, version = "") {
  if (!el || !ctx.api.updateCheck) return;
  host = el; api = ctx.api; current = version;
  if (!el.dataset.bound) {
    el.dataset.bound = "1";
    el.addEventListener("click", (e) => { const b = e.target.closest("[data-upd]"); if (b) act(b.dataset.upd); });
  }
  render();
  if (st.phase === "downloading" || st.phase === "unpacking") poll();
}

/** 程序启动时调用：如果这次启动是更新之后的第一次，提示一下 */
export async function announceUpdate(apiObj, version) {
  if (!apiObj.updateDone) return;
  const done = await apiObj.updateDone();
  if (done) toast(`已更新到 ${version || "新版本"}${done.from ? `（之前是 ${done.from}）` : ""}`, null, 6000);
}

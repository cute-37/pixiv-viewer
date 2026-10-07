// 下载与更新：查看器内置的 Pixiv 下载功能（后端是项目里的 pixiv_dl 模块）。
// 更新（检查新作品并下载，带进度）、失败处理、账号、保存位置、下载内容、速度与网络、数据导入、常规。
// 下载在独立的进程里跑，这里通过 ctx.api.dl(method, path, body, query) 调它的接口。
import { $, $$, esc, icon, toast, fmtNum } from "./util.js";
import { createTasks } from "./dl_tasks.js";
import { hintHTML } from "./help.js";

// 两组页面：操作（更新、失败处理）在“下载与更新”面板里；设置（账号、保存位置、下载内容、速度与网络、数据导入、常规）并入软件的“设置”。
// 独立的下载器程序没有另外的设置窗口，两组都在同一个侧栏里，分成“下载”“设置”两段。
const OPS = [["update", "更新", "sync"], ["failed", "失败处理", "warn"]];
// 设置页：下载内容（下什么）和速度与网络（多快、怎么连）分开；和下载内容无关的程序行为（通知、睡眠）放在“常规”。
// home（数据文件夹那一组）不单独成页：查看器里嵌在“图库”页，独立的下载器里放在“数据导入”页顶上。
const SETS = [["accounts", "Pixiv 账号", "user"], ["storage", "保存位置", "drive"], ["content", "下载内容", "dl"], ["speed", "速度与网络", "sliders"],
  ["link", "数据导入", "link"], ["general", "常规", "gear"]];
const PAGES = [...OPS, ...SETS, ["home", "数据文件夹", "folder"]];
const isSetting = (page) => page === "home" || SETS.some((x) => x[0] === page);
export const DL_SETTINGS_PAGES = SETS;
/** 下载器的某一页在查看器的“设置”对话框里叫什么 */
const settingsKey = (page) => ({ general: "general", home: "library" }[page] || "dl-" + page);
const KIND_LABEL = {
  sync: "检查更新", sync_download: "检查更新并下载", download: "下载", sync_artist: "检查这位画师", download_artist: "下载这位画师的作品",
  sync_download_artist: "更新这位画师", sync_artist_download: "更新这位画师", sync_artists: "检查所选画师", download_artists: "下载所选画师", sync_download_artists: "更新所选画师",
  verify: "核查文件", refresh_profiles: "刷新画师资料", download_avatars: "补全头像", fill_sizes: "补全文件大小", db_vacuum: "压缩数据库", idle: "空闲",
  retry_now: "重试失败的文件", recheck_gone: "复核已注销的画师",
};
const STATUS_LABEL = { running: "进行中", done: "已完成", cancelled: "已停止", error: "出错了" };
const FAIL_KINDS = {
  deleted: ["作品已删除", "所有账号都找不到这个作品。一般重试没有用，可以忽略；如果是旧版本记下的，可以重试一次重新判断。", true],
  restricted: ["无权查看", "作品还在，但所有账号都看不了：作者限制了可见范围，或账号没有在 Pixiv 的设置里开启 R-18 / 敏感作品的显示。调整账号设置后可以重试。", true],
  rate_limit: ["被限速", "下载时被 Pixiv 限速。不是文件本身的问题，也不占重试次数，过一会儿重试即可。", true],
  network: ["网络或服务器问题", "通常重试就能恢复。", true],
  http: ["访问被拒绝 (403)", "下载地址过期或被拦截，重试时会重新获取地址。", true],
  storage: ["保存失败", "请检查磁盘空间或保存位置的连接，然后重试。", true],
  content: ["内容异常", "下载到的内容不完整，重试一般可以解决。", true],
  api: ["接口请求失败", "多为暂时限速，稍后重试。", true],
  auth: ["账号登录失效", "请到“账号”页重新登录。", true],
  other: ["其他错误", "可以先重试；反复失败请看下载器的日志。", true],
  unknown: ["未知（旧记录）", "这些记录没有保存原因，可以重试一次。", true],
};
const MODES = [["local", "本地目录"], ["smb", "SMB"], ["webdav", "WebDAV"], ["ftp", "FTP"], ["sftp", "SFTP"], ["s3", "对象存储"]];
// 每种保存方式要填的项：[键, 标签, 类型, 提示]
const STORAGE_FIELDS = {
  local: [["LOCAL_SAVE_PATH", "保存目录", "path", "例如 D:\\Pixiv"]],
  smb: [["NAS_IP", "服务器地址", "text", "IP 或主机名，例如 192.168.1.100"], ["NAS_USER", "用户名", "text", ""], ["NAS_PASS", "密码", "password", ""],
    ["NAS_SHARE", "共享文件夹", "text", "共享的名字，例如 media"], ["NAS_BASE_PATH", "子目录", "text", "共享里的路径，例如 图片/PIXIV"], ["NAS_REMOTE_NAME", "服务器名称", "text", "一般不用改"]],
  webdav: [["WEBDAV_URL", "地址", "text", "https://nas.local:5006/dav/PIXIV"], ["WEBDAV_USER", "用户名", "text", ""], ["WEBDAV_PASS", "密码", "password", ""], ["WEBDAV_VERIFY_TLS", "校验 HTTPS 证书", "bool", "自签名证书请关闭"]],
  ftp: [["FTP_URL", "地址", "text", "ftp://主机:端口/保存目录（加密用 ftps://）"], ["FTP_USER", "用户名", "text", ""], ["FTP_PASS", "密码", "password", ""]],
  sftp: [["SFTP_URL", "地址", "text", "sftp://主机:端口/保存目录"], ["SFTP_USER", "用户名", "text", ""], ["SFTP_PASS", "密码", "password", "用私钥登录时可以不填"], ["SFTP_KEY_FILE", "私钥文件", "text", "私钥文件的完整路径，可选"]],
  s3: [["S3_ENDPOINT", "服务地址", "text", "AWS 可留空；MinIO / R2 / OSS 等填它们的地址"], ["S3_REGION", "区域", "text", "例如 us-east-1"], ["S3_BUCKET", "存储桶", "text", ""], ["S3_PREFIX", "前缀（目录）", "text", "可选"],
    ["S3_ACCESS_KEY", "Access Key", "text", ""], ["S3_SECRET_KEY", "Secret Key", "password", ""], ["S3_PATH_STYLE", "路径风格访问", "bool", "MinIO 等自建服务通常要打开"], ["S3_VERIFY_TLS", "校验 HTTPS 证书", "bool", ""]],
};
const PRESETS = {
  conservative: ["保守", "最不容易被限速，速度较慢", { MAIN_ACCOUNT_SYNC_THREADS: 1, BACKUP_ACCOUNT_SYNC_THREADS: 1, MAIN_ACCOUNT_DOWNLOAD_THREADS: 1, BACKUP_ACCOUNT_DOWNLOAD_THREADS: 1, DELAY_SYNC: [2, 4], DELAY_DOWNLOAD: [1.5, 3.5], FAILURE_RATE_THRESHOLD: 0.4, RATE_LIMIT_ENABLED: true, REST_EVERY: 80, REST_SECONDS: 10, MAX_RETRIES: 3 }],
  balanced: ["平衡", "速度和稳定性折中，推荐", { MAIN_ACCOUNT_SYNC_THREADS: 1, BACKUP_ACCOUNT_SYNC_THREADS: 1, MAIN_ACCOUNT_DOWNLOAD_THREADS: 1, BACKUP_ACCOUNT_DOWNLOAD_THREADS: 2, DELAY_SYNC: [1.5, 3], DELAY_DOWNLOAD: [0.8, 2], FAILURE_RATE_THRESHOLD: 0.5, RATE_LIMIT_ENABLED: true, REST_EVERY: 150, REST_SECONDS: 10, MAX_RETRIES: 3 }],
  aggressive: ["激进", "更快，但更容易被 Pixiv 限速，只建议短时间使用", { MAIN_ACCOUNT_SYNC_THREADS: 2, BACKUP_ACCOUNT_SYNC_THREADS: 2, MAIN_ACCOUNT_DOWNLOAD_THREADS: 2, BACKUP_ACCOUNT_DOWNLOAD_THREADS: 3, DELAY_SYNC: [0.8, 1.5], DELAY_DOWNLOAD: [0.2, 1], FAILURE_RATE_THRESHOLD: 0.6, RATE_LIMIT_ENABLED: true, REST_EVERY: 300, REST_SECONDS: 10, MAX_RETRIES: 4 }],
};

const fmtDur = (s) => { s = Math.max(0, Math.round(s)); const m = Math.floor(s / 60); return m >= 60 ? `${Math.floor(m / 60)} 小时 ${m % 60} 分` : m ? `${m} 分 ${s % 60} 秒` : `${s} 秒`; };
const fmtWhen = (v) => {
  if (!v) return "从未";
  const d = typeof v === "number" ? new Date(v * 1000) : new Date(String(v).replace(" ", "T"));
  if (isNaN(d)) return String(v);
  const diff = (Date.now() - d) / 1000;
  if (diff < 60) return "刚刚";
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
  if (diff < 86400 * 30) return `${Math.floor(diff / 86400)} 天前`;
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
};

export function initDownloader(ctx) {
  const scrim = $("#scrim");
  const api = ctx.api;
  if (!api.dl) return;                       // 这个环境里没有下载器
  const state = {
    open: false, page: "update", job: null, plan: null, notes: [], runs: [],
    confirm: null,           // 等待确认的任务 {kind, params, title, text}
    dismissed: null,         // 已经看过结果的任务 id
    refreshed: null,         // 已经为哪个任务刷新过图库
    speed: { t: 0, bytes: 0, done: 0, bps: 0, ips: 0 },
    settings: null, form: {}, test: null, accounts: null, failures: null, info: null, busy: "", link: null,
    after: "none",           // 这次任务完成后做什么（每次任务单独选，不记成默认）
    oauth: null, login: null, loginError: "", browse: null, logsOpen: false, logLines: [], logSince: 0,
    imp: { items: [], results: null, busy: false },      // 导入：已选并识别出来的文件、导入结果
  };
  let pollTimer = 0;

  // 调下载器接口：成功返回数据，失败抛出带中文说明的错误
  async function dl(method, path, body, query) {
    const r = await api.dl(method, path, body || null, query || null);
    if (!r || !r.ok) throw new Error((r && r.error) || "下载器没有响应");
    return r.data;
  }
  const guard = async (fn, okText) => {
    try { const r = await fn(); if (okText) toast(okText); return r; } catch (e) { toast(e.message || String(e)); return null; }
  };
  const isOpen = () => !scrim.hidden && !!scrim.querySelector("#dl-page");

  // ================= 打开 / 关闭 =================
  // 独立的下载器程序里，这个面板就是整个界面：没有“关闭”，也没有图库可刷新
  const solo = !!ctx.standalone;
  const navBtn = ([k, t, ic]) => `<button class="row-btn" data-dlpage="${k}">${icon(ic)}<span class="lbl">${t}</span><span class="n" data-dlbadge="${k}"></span></button>`;
  ctx.openDownloader = async (page) => {
    page = page || (solo ? state.page : "update");
    if (!solo && isSetting(page)) return ctx.openSettings(settingsKey(page));      // 设置类的页面在“设置”里
    scrim.innerHTML = solo
      ? `<div class="dialog dlc" role="dialog" aria-label="Pixiv 下载器">
          <nav class="dnav"><div class="dt"><span class="logo">P</span>Pixiv 下载器</div>
            <div class="grp">下载</div>${OPS.map(navBtn).join("")}<div class="grp">设置</div>${SETS.map(navBtn).join("")}</nav>
          <div class="dmain"><div class="dhead"><h3 id="dl-title"></h3></div><div class="dpage" id="dl-page"></div></div></div>`
      : `<div class="dialog dlc ops" role="dialog" aria-label="下载与更新">
          <div class="dmain"><div class="dhead"><h3>下载与更新</h3>
            <div class="seg" role="tablist">${OPS.map(([k, t]) => `<button data-dlpage="${k}" role="tab">${t}<span class="n" data-dlbadge="${k}"></span></button>`).join("")}</div>
            <span class="sp"></span>
            <button class="btn ghost" data-dlgo="accounts" title="账号、保存位置、下载内容、速度与网络都在“设置”里">${icon("gear")}下载设置</button>
            <button class="icon-btn" data-dlclose title="关闭 (Esc)" aria-label="关闭">${icon("x")}</button></div>
          <div class="dpage" id="dl-page"></div></div></div>`;
    scrim.hidden = false;
    state.info = await api.dlInfo();
    mount(page);
  };
  // “设置”对话框显示下载相关的页面时调用（那边已经放好了 #dl-page）
  ctx.dlMount = (page) => mount(page);
  const prevClose = ctx.closeDialog;
  ctx.closeDialog = () => { if (solo) return; state.confirm = null; state.browse = null; prevClose(); };
  const scroller = () => { const b = $("#dl-page"); return (b && b.closest(".dpage")) || b; };

  function mount(page) {
    state.page = page;
    state.confirm = null; state.test = null; state.browse = null; state.ptest = null;
    if (!isOpen()) return;
    $$("[data-dlpage]", scrim).forEach((b) => b.classList.toggle("on", b.dataset.dlpage === page));
    if ($("#dl-title")) $("#dl-title").textContent = PAGES.find((x) => x[0] === page)[1];
    state.review = null;
    $("#dl-page").innerHTML = `<div class="dl-loading">正在读取…</div>`;
    return load(page);
  }
  // 换页：操作页和设置页不在同一个窗口里时，换到对应的窗口
  function go(page) {
    const inSettings = !!scrim.querySelector("#dl-page.dl-host");
    if (!solo && isSetting(page) && !inSettings) return ctx.openSettings(settingsKey(page));
    if (!solo && !isSetting(page) && inSettings) return ctx.openDownloader(page);
    if (!solo && inSettings && page !== state.page) return ctx.openSettings(settingsKey(page));
    scroller().scrollTop = 0;
    return mount(page);
  }
  async function load(page) {
    try {
      if (page === "update") { await refreshUpdate(); }
      if (page === "failed") { await tasks.loadFailed(); state.job = state.job || await dl("GET", "/api/job"); }
      if (page === "accounts") state.accounts = (await dl("GET", "/api/accounts")).items;
      if (["storage", "content", "speed", "general"].includes(page)) {
        const r = await dl("GET", "/api/settings");
        state.settings = r.settings; state.form = {};
        state.job = state.job || await dl("GET", "/api/job");
        if (page === "storage") state.link = solo ? null : await api.dlStorageLink();
      }
      if (page === "link" || page === "home") { state.info = await api.dlInfo(); state.job = state.job || await dl("GET", "/api/job").catch(() => null); }
    } catch (e) {
      if (isOpen() && state.page === page) $("#dl-page").innerHTML = `<div class="dl-empty">${icon("warn")}<b>读取失败</b><span>${esc(e.message)}</span><button class="btn" data-dl="reload">重试</button></div>`;
      return;
    }
    if (state.page === page) draw();
  }
  async function refreshUpdate() {
    const [job, plan, notes, runs] = await Promise.all([
      dl("GET", "/api/job"), dl("GET", "/api/plan"), dl("GET", "/api/notifications"), dl("GET", "/api/runs", null, { limit: 5 })]);
    state.plan = plan; state.notes = notes.items || []; state.runs = runs.items || [];
    onJob(job);
  }
  function draw(keepScroll = true) {
    if (!isOpen()) return;
    const box = $("#dl-page"), sc = scroller(), top = sc.scrollTop;
    const lb = $("#dl-logbox"), lbTop = lb ? lb.scrollTop : 0, lbStick = !lb || lb.scrollTop + lb.clientHeight >= lb.scrollHeight - 8;
    const active = document.activeElement && box.contains(document.activeElement) ? document.activeElement : null;
    const focusKey = active && (active.dataset.f || active.id), sel = active && active.selectionStart;
    box.innerHTML = { update: pageUpdate, failed: pageFailed, accounts: pageAccounts, storage: pageStorage, content: pageContent, speed: pageSpeed, general: pageGeneral, link: pageLink, home: pageHome }[state.page]();
    // 页面每秒重画一次，转圈、进度条这些循环动画会跟着从头开始，看上去一顿一顿的。
    // 让它们都对齐到同一个时钟：重画之后接着刚才的角度继续转。
    for (const a of box.getAnimations ? box.getAnimations({ subtree: true }) : []) {
      if (a.effect && a.effect.getTiming().iterations === Infinity) a.startTime = 0;
    }
    if (keepScroll) sc.scrollTop = top;
    const lb2 = $("#dl-logbox"); if (lb2) lb2.scrollTop = lbStick ? lb2.scrollHeight : lbTop;
    if (focusKey) { const el = box.querySelector(`[data-f="${focusKey}"], #${CSS.escape(focusKey)}`); if (el) { el.focus(); try { el.setSelectionRange(sel, sel); } catch (e) { /* 不是文本框 */ } } }
    const failed = state.plan ? (state.plan.failed_total || 0) + (state.plan.sync_failed || 0) : 0;
    const fb = scrim.querySelector('[data-dlbadge="failed"]'); if (fb) fb.textContent = failed ? fmtNum(failed) : "";
  }

  // ================= 任务状态 =================
  function onJob(job) {
    const prev = state.job;
    state.job = job;
    const running = job && (job.running || job.status === "running");
    if (running) {
      // 速度：按两次查询之间的增量估算，稍微平滑一下
      // 用“实际收到的字节数”算（边下边涨）；旧的下载模块没有这个数时退回到“已完成文件的大小”
      const now = performance.now(), sp = state.speed, got = job.transferred ?? job.bytes;
      if (sp.t && now - sp.t > 400) {
        const dt = (now - sp.t) / 1000;
        const bps = Math.max(0, (got - sp.bytes) / dt), ips = Math.max(0, (job.done - sp.done) / dt);
        sp.bps = sp.bps ? sp.bps * 0.6 + bps * 0.4 : bps;
        sp.ips = sp.ips ? sp.ips * 0.7 + ips * 0.3 : ips;
        sp.t = now; sp.bytes = got; sp.done = job.done;
      } else if (!sp.t || job.done < sp.done) { state.speed = { t: now, bytes: got, done: job.done, bps: 0, ips: 0 }; }
      startPolling();
    } else {
      state.speed = { t: 0, bytes: 0, done: 0, bps: 0, ips: 0 };
      if (prev && (prev.running || prev.status === "running") && job.id === prev.id) finished(job);
    }
    syncBadge();
  }
  async function pollOnce() {
    try {
      onJob(await dl("GET", "/api/job"));
      if (state.logsOpen) await fetchLogs();
      if (!(state.job.running || state.job.status === "running")) { clearInterval(pollTimer); pollTimer = 0; }
      if (isOpen() && state.page === "update") draw();
    } catch (e) { clearInterval(pollTimer); pollTimer = 0; }
  }
  function startPolling() {
    if (pollTimer) return;
    pollTimer = setInterval(pollOnce, 1000);
  }
  // 窗口最小化、被别的窗口挡住或放进托盘时，浏览器内核会把页面里的定时器放慢（最慢每分钟才跑一次），
  // 界面上的进度就不动了——下载本身在另一个进程里照常进行。窗口回到眼前时立刻刷新一次，不等下一次定时。
  const catchUp = () => { if (!document.hidden && pollTimer) pollOnce(); };
  document.addEventListener("visibilitychange", catchUp);
  window.addEventListener("focus", catchUp);
  // 详细日志：下载模块自己的运行记录（每个文件的成功 / 失败原因都在里面），按上次取到的位置接着取
  async function fetchLogs() {
    try {
      const r = await dl("GET", "/api/logs", null, { since: state.logSince });
      const items = r.items || [];
      if (items.length) {
        state.logSince = items[items.length - 1].id;
        state.logLines = state.logLines.concat(items).slice(-400);
      }
    } catch (e) { /* 取不到就显示已有的 */ }
  }
  const logBox = () => {
    const lines = state.logLines;
    return `<div class="dl-logs open" id="dl-logbox">${lines.length ? lines.map((l) => `<div class="${l.level === "WARNING" ? "w" : l.level === "ERROR" ? "e" : ""}"><span class="tm">${l.t ? new Date(l.t * 1000).toTimeString().slice(0, 8) : ""}</span>${esc(l.msg || "")}</div>`).join("")
      : `<div>还没有日志</div>`}</div>`;
  };
  // 任务结束：有新文件就重新扫描对应的画师文件夹，并提示结果
  // 任务完成、“完成后做什么”进入倒计时的时候：显示出来，可以取消
  let afterTimer = 0;
  function watchAfter() {
    clearInterval(afterTimer);
    if (!api.afterJobState) return;
    const box = () => document.querySelector(".afterask");
    const stop = () => { clearInterval(afterTimer); afterTimer = 0; const b = box(); if (b) b.remove(); };
    afterTimer = setInterval(async () => {
      let st; try { st = await api.afterJobState(); } catch (e) { return stop(); }
      if (st.state === "watching") return;
      if (st.state !== "countdown") { stop(); if (st.message && st.action !== "none") toast(st.message); return; }
      let b = box();
      if (!b) {
        b = document.createElement("div"); b.className = "closeask afterask";
        b.innerHTML = `<div class="closeask-card"><h3></h3><p>任务已经完成。不想执行的话点“取消”。</p><div class="closeask-actions"><span class="sp"></span><button class="btn primary" data-after-cancel>取消</button></div></div>`;
        b.querySelector("[data-after-cancel]").addEventListener("click", async () => { await api.afterJobCancel(); stop(); toast("已取消"); });
        document.body.append(b);
      }
      b.querySelector("h3").textContent = `${st.seconds} 秒后${st.label}`;
    }, 500);
  }
  async function finished(job) {
    if (state.after && state.after !== "none") watchAfter();
    state.after = "none";
    const got = Object.keys((job.detail && job.detail.downloaded) || {});
    const text = job.status === "done" ? `${KIND_LABEL[job.kind] || "任务"}完成${job.success ? `：新下载 ${fmtNum(job.success)} 个文件` : "，没有新文件"}`
      : job.status === "cancelled" ? "任务已停止" : `任务出错：${job.error || ""}`;
    toast(text, isOpen() ? null : undefined);
    if (!solo && state.refreshed !== job.id && (job.success || got.length)) {
      state.refreshed = job.id;
      await guard(() => api.dlRefreshLibrary(got));
      await ctx.reloadLibrary();
      ctx.loadWorks();
    }
    if (isOpen() && state.page === "update") { try { await refreshUpdate(); } catch (e) { /* 下次打开再读 */ } draw(); }
  }
  function syncBadge() {
    const job = state.job, running = job && (job.running || job.status === "running");
    const pct = running && job.total ? Math.floor((job.done / job.total) * 100) : null;
    const txt = running ? (pct != null ? pct + "%" : "…") : "";
    const b = $("#dl-badge"); if (b) { b.textContent = txt; b.classList.toggle("live", !!running); }
    $$("[data-act='downloader'], #btn-dl").forEach((el) => el.classList.toggle("busy", !!running));
  }

  async function startJob(kind, params) {
    state.confirm = null;
    const ok = await guard(() => dl("POST", "/api/job", { kind, ...(params || {}) }));
    if (!ok) return draw();
    // 让后端盯着这个任务：结束时发系统通知，并执行选好的“完成后做什么”
    if (api.jobWatch) api.jobWatch({ action: state.after || "none", command: (ctx.S && ctx.S.afterCommand) || "", notify: !ctx.S || ctx.S.notifyOnFinish !== false });
    state.dismissed = null;
    state.logLines = [];
    onJob(await dl("GET", "/api/job"));
    startPolling();
    if (isOpen()) { if (state.page !== "update") go("update"); else draw(); }
  }
  // 给画师页、右键菜单用：先打开面板让用户确认，不会直接开始
  ctx.askSyncArtist = (artist) => {
    if (!artist || !artist.id) return toast("这个文件夹没有对应的 Pixiv 画师，无法更新");
    state.page = "update";
    ctx.openDownloader("update").then(() => {
      state.review = null;
      state.confirm = { kind: "sync_download_artist", params: { author_id: artist.id }, title: `更新 ${artist.name}`,
        text: `检查 ${artist.name} 有没有新作品，有就下载。只访问这一位画师。` };
      draw();
    });
  };

  // ================= 更新、失败处理（内容在 dl_tasks.js） =================
  const tasks = createTasks({ state, dl, guard, draw, go, startJob, onJob, ctx, api, solo, fmtDur, fmtWhen, KIND_LABEL, STATUS_LABEL, FAIL_KINDS, logBox, fetchLogs });
  const pageUpdate = () => tasks.pageUpdate(), pageFailed = () => tasks.pageFailed();

  // ================= 账号 =================
  // 可见性标记：true 看得到，false 看不到，其余是还没测
  const visTag = (label, v) => `<span class="dl-tag ${v === true ? "ok" : v === false ? "error" : ""}" title="${v === true ? "验证时确认看得到" : v === false ? "验证时确认看不到：这类作品不会分配给它" : "还没有检测，点“验证”"}">${label} ${v === true ? "可见" : v === false ? "不可见" : "未测"}</span>`;
  function pageAccounts() {
    const list = state.accounts || [];
    const o = state.oauth;
    return `<div class="group">
      ${list.length ? list.map((a) => `<div class="set dl-acc"><div class="t"><span class="dl-acc-n">${esc(a.name)}${a.is_main ? `<span class="dl-tag done">主账号</span>` : ""}<span class="dl-tag ${a.is_valid ? "ok" : "error"}">${a.is_valid ? "可用" : "登录已失效"}</span>${a.is_valid ? visTag("R-18", a.r18) + visTag("R-18G", a.r18g) : ""}</span>
          ${a.is_valid && (a.r18 === false || a.r18g === false) ? `<small class="dl-vis-hint">${a.r18 === false ? "R-18 和 R-18G" : "R-18G"} 作品不会分配给这个账号下载。要让它也能下载：用这个账号登录 Pixiv 网页 → 设置 → 浏览限制，打开对应的显示，然后回来点“验证”。</small>` : ""}
          <small>${a.username ? esc(a.username) + " · " : ""}${a.user_id ? "ID " + esc(String(a.user_id)) + " · " : ""}${a.remark ? esc(a.remark) + " · " : ""}上次验证 ${a.last_tested ? esc(String(a.last_tested).slice(0, 16)) : "—"}</small></div>
        <div class="ctl">${a.is_main ? "" : `<button class="btn ghost" data-dl="acc-main" data-name="${esc(a.name)}" title="主账号负责读取关注列表">设为主账号</button>`}<button class="btn ghost" data-dl="acc-del" data-name="${esc(a.name)}">删除</button></div></div>`).join("")
        : `<div class="set"><div class="t">还没有账号<small>添加一个 Pixiv 账号后才能检查更新和下载。</small></div></div>`}
      ${list.length ? `<div class="set"><div class="t">验证全部账号<small>逐个检查登录是否还有效，并检测每个账号能不能看到 R-18 / R-18G 作品（会访问 Pixiv，每个账号几次请求）。下载时，这两类作品只分配给看得到的账号。</small></div><div class="ctl"><button class="btn" data-dl="acc-test" ${state.busy === "acc-test" ? "disabled" : ""}>${state.busy === "acc-test" ? "正在验证…" : "验证"}</button></div></div>` : ""}
    </div>
    ${api.loginStart ? `<div class="group"><div class="gh">添加账号 · 登录（推荐）</div>
      ${state.login ? `<div class="set"><div class="t">${state.login.status === "finishing" ? "登录成功，正在添加账号…" : "已经打开登录窗口"}
          <small>${state.login.status === "finishing" ? "正在向 Pixiv 确认并检测这个账号，稍等几秒。" : "请在弹出的窗口里登录 Pixiv。登录完成后窗口会自动关闭，账号会出现在上面的列表里。"}</small></div>
          <div class="ctl">${state.login.status === "finishing" ? "" : `<button class="btn ghost" data-dl="login-cancel">取消</button>`}</div></div>`
        : `<div class="set"><div class="t">登录 Pixiv 账号<small>会弹出一个窗口显示 Pixiv 官方的登录页。账号和密码只输入在 Pixiv 的页面里，软件不会看到，也不会保存它们。</small>
          ${state.loginError ? `<small class="upd-err">${esc(state.loginError)}</small>` : ""}</div>
          <div class="ctl"><button class="btn primary" data-dl="login-start">登录…</button></div></div>`}
    </div>` : ""}
    <div class="group"><div class="gh">${api.loginStart ? "添加账号 · 用浏览器登录（登录窗口用不了时）" : "添加账号 · 用浏览器登录"}</div>
      ${o ? `<div class="dl-steps">
          <div><b>1</b><span>已经用浏览器打开了 Pixiv 的登录页（没打开的话 <button class="linkbtn" data-dl="oauth-open">再打开一次</button>）。<b>先不要登录。</b></span></div>
          <div><b>2</b><span>在那个页面按 <span class="kbd">F12</span> 打开开发者工具，切到“网络 / Network”，勾选“保留日志 / Preserve log”，并在筛选框里输入 <span class="mono">callback?</span></span></div>
          <div><b>3</b><span>现在登录。登录后页面会变成空白，这是正常的。网络列表里会出现一行 <span class="mono">callback?state=…&amp;code=…</span>，点它，把完整的请求地址（Request URL）复制下来。</span></div>
          <div><b>4</b><span>粘贴到这里：</span></div>
        </div>
        <div class="dl-form"><label>回调地址或 code<input data-f="oauth-cb" id="dl-oauth-cb" placeholder="https://app-api.pixiv.net/web/v1/users/auth/pixiv/callback?state=…&amp;code=…" autocomplete="off" spellcheck="false"></label>
          <label>账号名（可选）<input data-f="oauth-name" id="dl-oauth-name" placeholder="留空则用 Pixiv 昵称"></label></div>
        <div class="dl-actions"><button class="btn primary" data-dl="oauth-finish" ${state.busy === "oauth" ? "disabled" : ""}>${state.busy === "oauth" ? "正在验证…" : "完成添加"}</button><button class="btn ghost" data-dl="oauth-cancel">取消</button></div>`
        : `<div class="set"><div class="t">用浏览器登录 Pixiv<small>在你自己的浏览器里登录，再把登录后产生的一个地址复制回来。步骤多一些，需要用到浏览器的开发者工具。</small></div><div class="ctl"><button class="btn ${api.loginStart ? "" : "primary"}" data-dl="oauth-start">开始</button></div></div>`}
    </div>
    <div class="group"><div class="gh">添加账号 · 已有 refresh token</div>
      <div class="dl-form"><label>账号名<input data-f="tok-name" id="dl-tok-name" placeholder="给这个账号起个名字" autocomplete="off"></label>
        <label>refresh token<input data-f="tok-token" id="dl-tok-token" type="password" placeholder="粘贴 refresh token" autocomplete="off" spellcheck="false"></label>
        <label>备注（可选）<input data-f="tok-remark" id="dl-tok-remark" autocomplete="off"></label></div>
      <div class="dl-actions"><button class="btn" data-dl="tok-add" ${state.busy === "tok" ? "disabled" : ""}>${state.busy === "tok" ? "正在验证…" : "验证并添加"}</button></div>
    </div>`;
  }

  // ================= 保存位置 =================
  const val = (k) => (k in state.form ? state.form[k] : state.settings[k]);
  const dirty = (keys) => keys.some((k) => k in state.form && JSON.stringify(state.form[k]) !== JSON.stringify(state.settings[k]));
  const jobRunning = () => state.job && (state.job.running || state.job.status === "running");
  function field([key, label, type, hint]) {
    if (type === "bool") return `<div class="set"><div class="t">${label}${hintHTML(hint)}</div><div class="ctl"><button class="switch ${val(key) ? "on" : ""}" data-dlsw="${key}" role="switch" aria-checked="${!!val(key)}"></button></div></div>`;
    const isPw = type === "password", saved = isPw && state.settings[key + "_SET"];
    return `<div class="set"><div class="t">${label}${hintHTML(hint)}</div><div class="ctl">
      <input type="${isPw ? "password" : "text"}" data-f="${key}" data-dlin="${key}" value="${esc(isPw ? (state.form[key] || "") : (val(key) ?? ""))}" placeholder="${saved ? "已保存，留空表示不修改" : ""}" autocomplete="off" spellcheck="false">
      ${type === "path" ? `<button class="btn" data-dl="pick-local">选择…</button>` : ""}</div></div>`;
  }
  function pageStorage() {
    const s = state.settings; if (!s) return "";
    const mode = val("STORAGE_MODE"), fields = STORAGE_FIELDS[mode] || [];
    const keys = ["STORAGE_MODE", ...Object.values(STORAGE_FIELDS).flat().map((f) => f[0])];
    const t = state.test, b = state.browse;
    return `${jobRunning() ? `<div class="dl-note lv-warn">${icon("warn")}<div><b>有任务正在运行</b><small>任务结束后才能修改保存位置。</small></div></div>` : ""}
      <div class="group"><div class="set"><div class="t">保存方式<small>新下载的文件放在哪里</small></div>
        <div class="ctl"><select data-dlmodesel aria-label="保存方式">${MODES.map(([v, l]) => `<option value="${v}" ${mode === v ? "selected" : ""}>${l}</option>`).join("")}</select></div></div></div>
      ${linkCard()}
      <div class="group">${fields.map(field).join("")}
        ${mode === "smb" ? `<div class="set"><div class="t">浏览共享<small>填好地址、用户名、密码后，可以点着选共享和文件夹。</small></div><div class="ctl"><button class="btn" data-dl="browse">浏览…</button></div></div>` : ""}</div>
      ${b ? browseBox(b) : ""}
      ${t ? `<div class="group dl-test ${t.ok ? "ok" : "bad"}"><div class="gh">${t.pending ? "正在测试…" : t.ok ? "连接正常" : "连接有问题"}</div>
        ${(t.steps || []).map((st) => `<div class="dl-step ${st.ok ? "ok" : "bad"}">${icon(st.ok ? "check" : "x")}<b>${esc(st.name)}</b><span>${esc(st.detail || "")}</span></div>`).join("")}
        ${!t.pending && !(t.steps || []).length ? `<div class="dl-step bad">${icon("x")}<span>${esc(t.message || "测试失败")}</span></div>` : ""}</div>` : ""}
      <div class="dl-actions sticky"><button class="btn" data-dl="test">测试连接</button><span class="sp"></span>
        ${dirty(keys) ? `<button class="btn ghost" data-dl="revert">放弃修改</button>` : ""}<button class="btn primary" data-dl="save" data-keys="${keys.join(",")}" ${dirty(keys) && !jobRunning() ? "" : "disabled"}>保存</button></div>`;
  }
  // 保存位置和“资料库”的关系：下载到的地方要在资料库里，新图才看得到
  function linkCard() {
    const l = state.link; if (!l) return "";
    if (dirty(["STORAGE_MODE", ...Object.values(STORAGE_FIELDS).flat().map((f) => f[0])])) {
      return `<div class="dl-note">${icon("info")}<div><b>保存后生效</b><small>新的保存位置会自动算作资料库的一部分。</small></div></div>`;
    }
    if (!l.readable) {
      return `<div class="dl-note lv-warn">${icon("warn")}<div><b>这种保存方式下，查看器不能直接看到新下载的图</b><small>查看器只能读取本地目录和 SMB 共享。用 WebDAV / FTP / SFTP / 对象存储时，需要你另外把它挂载成本机能访问的文件夹，再加到资料库。</small></div></div>`;
    }
    return `<div class="dl-note lv-ok">${icon("check")}<div><b>下载的图会自动出现在资料库里</b><small>保存位置自动算作资料库的一部分，不用再单独添加。</small><small class="mono">${esc(l.path)}</small></div></div>`;
  }
  function browseBox(b) {
    const crumbs = [`<button class="linkbtn" data-dlbrowse="">全部共享</button>`, ...(b.share ? [`<button class="linkbtn" data-dlbrowse="${esc(b.share)}|">${esc(b.share)}</button>`] : []),
      ...b.path.map((p, i) => `<button class="linkbtn" data-dlbrowse="${esc(b.share)}|${esc(b.path.slice(0, i + 1).join("/"))}">${esc(p)}</button>`)].join(" / ");
    return `<div class="group dl-browse"><div class="gh">${crumbs}</div>
      ${b.loading ? `<div class="dl-loading">正在读取…</div>` : b.error ? `<div class="dl-step bad">${icon("x")}<span>${esc(b.error)}</span></div>`
        : b.entries.length ? `<div class="dl-folders">${b.entries.map((n) => `<button data-dlbrowse="${esc(b.share ? b.share : n)}|${esc(b.share ? [...b.path, n].join("/") : "")}">${icon("folder")}${esc(n)}</button>`).join("")}</div>` : `<div class="dl-loading">这里没有子文件夹</div>`}
      <div class="dl-actions">${b.share ? `<button class="btn primary" data-dl="browse-use">保存到这里</button>` : ""}<button class="btn ghost" data-dl="browse-close">关闭</button></div></div>`;
  }
  const PROXY_MODES = [["system", "跟随系统设置"], ["custom", "自定义"], ["none", "不使用代理"]];
  const PROXY_HINTS = {
    system: "使用 Windows 里设置的代理；系统没有设置代理时直接连接。",
    custom: "只有这个软件访问 Pixiv 时走下面填的代理，不影响其他程序，也不影响连接保存位置。登录账号的窗口要重启软件后才会用上新设置。",
    none: "直接连接 Pixiv，即使系统设置了代理也不用。登录账号的窗口要重启软件后才会用上新设置。",
  };
  function proxyGroup(row) {
    const mode = val("PROXY_MODE") || "system", t = state.ptest;
    return `<div class="group"><div class="gh">网络代理</div>
        ${row("访问 Pixiv 时", PROXY_HINTS[mode], `<select data-dlproxysel aria-label="代理方式">${PROXY_MODES.map(([v, l]) => `<option value="${v}" ${mode === v ? "selected" : ""}>${l}</option>`).join("")}</select>`)}
        ${mode === "custom" ? row("代理地址", "代理软件里显示的地址和端口。支持 http:// 和 socks5://；只写 127.0.0.1:7890 这样的会按 http 处理。",
          `<input type="text" data-f="PROXY_URL" data-dlin="PROXY_URL" value="${esc(val("PROXY_URL") ?? "")}" placeholder="http://127.0.0.1:7890" autocomplete="off" spellcheck="false">`) : ""}
        ${row("测试连接", "按上面选的方式试着连一下 Pixiv（不用先保存，不会登录或下载）。", `<button class="btn" data-dl="test-proxy" ${t && t.pending ? "disabled" : ""}>${t && t.pending ? "正在测试…" : "测试"}</button>`)}
        ${t && !t.pending ? `<div class="dl-test ${t.ok ? "ok" : "bad"}"><div class="gh">${t.ok ? "可以连上 Pixiv" : "连不上 Pixiv"}${t.using ? `<small> · ${esc(t.using)}</small>` : ""}</div>
          ${(t.steps || []).map((st) => `<div class="dl-step ${st.ok ? "ok" : "bad"}">${icon(st.ok ? "check" : "x")}<b>${esc(st.name)}</b><span>${esc(st.detail || "")}</span></div>`).join("")}
          ${!(t.steps || []).length ? `<div class="dl-step bad">${icon("x")}<span>${esc(t.message || "测试没有完成")}</span></div>` : ""}</div>` : ""}
      </div>`;
  }
  const CONTENT_KEYS = ["SYNC_TYPES", "SYNC_NOVELS", "METADATA_REFRESH_LIMIT", "UGOIRA_PREFER_HQ", "UGOIRA_WEBP_LOSSLESS", "REVIEW_THRESHOLD"];
  const SPEED_KEYS = ["PROXY_MODE", "PROXY_URL", "MAIN_ACCOUNT_SYNC_THREADS", "BACKUP_ACCOUNT_SYNC_THREADS", "MAIN_ACCOUNT_DOWNLOAD_THREADS", "BACKUP_ACCOUNT_DOWNLOAD_THREADS",
    "DELAY_SYNC", "DELAY_DOWNLOAD", "FAILURE_RATE_THRESHOLD", "RATE_LIMIT_ENABLED", "REST_EVERY", "REST_SECONDS", "MAX_RETRIES"];
  // 这几页共用的小控件
  const num = (key, min, max, step = 1) => `<input type="number" data-f="${key}" data-dlnum="${key}" min="${min}" max="${max}" step="${step}" value="${esc(String(val(key)))}">`;
  const pair = (key) => { const v = val(key) || [0, 0]; return `<input type="number" data-f="${key}0" data-dlpair="${key}:0" min="0" max="60" step="0.1" value="${v[0]}"><span>到</span><input type="number" data-f="${key}1" data-dlpair="${key}:1" min="0" max="60" step="0.1" value="${v[1]}"><span>秒</span>`; };
  const sw = (key) => `<button class="switch ${val(key) ? "on" : ""}" data-dlsw="${key}" role="switch" aria-checked="${!!val(key)}"></button>`;
  // 点了马上生效的开关（不用再点“保存”）
  const nowSw = (key) => `<button class="switch ${state.settings[key] ? "on" : ""}" data-dlnow="${key}" role="switch" aria-checked="${!!state.settings[key]}"></button>`;
  const row = (t, h, c) => `<div class="set"><div class="t">${t}${hintHTML(h)}</div><div class="ctl">${c}</div></div>`;
  const busyNote = () => (jobRunning() ? `<div class="dl-note lv-warn">${icon("warn")}<div><b>有任务正在运行</b><small>任务结束后才能修改这些选项。</small></div></div>` : "");
  const saveBar = (keys) => `<div class="dl-actions sticky"><span class="sp"></span>${dirty(keys) ? `<button class="btn ghost" data-dl="revert">放弃修改</button>` : ""}<button class="btn primary" data-dl="save" data-keys="${keys.join(",")}" ${dirty(keys) && !jobRunning() ? "" : "disabled"}>保存</button></div>`;

  // ================= 下载内容：下什么 =================
  function pageContent() {
    if (!state.settings) return "";
    const types = val("SYNC_TYPES") || [];
    return `${busyNote()}
      <div class="group"><div class="gh">下载哪些内容</div>
        ${row("作品类型", "动图算在插画里", `<div class="seg multi">${[["illust", "插画"], ["manga", "漫画"]].map(([v, l]) => `<button data-dltype="${v}" class="${types.includes(v) ? "on" : ""}">${l}</button>`).join("")}</div>`)}
        ${row("小说", "", sw("SYNC_NOVELS"))}
        ${row("顺带刷新旧作品的数据", "增量检查时，遇到已有作品后再往前刷新多少个（收藏数、标签等）", `${num("METADATA_REFRESH_LIMIT", 0, 1000)}<span>个</span>`)}
      </div>
      <div class="group"><div class="gh">动图</div>
        ${row("下载最高清的版本", "Pixiv 的动图是一个装着每一帧图片的压缩包，有大（最长边 1920）、小（600）两种。打开 = 下载大的，这就是 Pixiv 能给的原始画质；压缩包会原样保存。", sw("UGOIRA_PREFER_HQ"))}
        ${row("预览动画不再压缩", "除了原始压缩包，还会另外生成一个能直接播放的 WebP 动画。关闭 = 生成时再压缩一次（体积小，画质略降）；打开 = 和原始帧完全一致（体积大很多、生成慢）。原始压缩包不受这一项影响。", sw("UGOIRA_WEBP_LOSSLESS"))}
      </div>
      <div class="group"><div class="gh">检查之后</div>
        ${row("新发现的文件超过多少先问我", "“检查更新并下载”时，如果这次新发现的文件比这个数多，就先停下来列出是谁的，等你确认后再下载。填 0 表示从不询问。", `${num("REVIEW_THRESHOLD", 0, 1000000)}<span>个</span>`)}
      </div>
      ${saveBar(CONTENT_KEYS)}`;
  }

  // ================= 速度与网络：多快、怎么连 =================
  function pageSpeed() {
    if (!state.settings) return "";
    const preset = Object.entries(PRESETS).find(([, p]) => Object.entries(p[2]).every(([k, v]) => JSON.stringify(val(k)) === JSON.stringify(v)));
    return `${busyNote()}
      ${proxyGroup(row)}
      <div class="group"><div class="gh">速度与风控</div>
        ${row("预设", "一次设置好下面的线程数和间隔。越快越容易被 Pixiv 限速。", `<div class="seg">${Object.entries(PRESETS).map(([k, p]) => `<button data-dlpreset="${k}" class="${preset && preset[0] === k ? "on" : ""}" title="${esc(p[1])}">${p[0]}</button>`).join("")}</div>`)}
        ${row("检查更新的线程数", "主账号 / 每个备用账号", `${num("MAIN_ACCOUNT_SYNC_THREADS", 1, 8)}<span>/</span>${num("BACKUP_ACCOUNT_SYNC_THREADS", 1, 8)}`)}
        ${row("下载的线程数", "主账号 / 每个备用账号", `${num("MAIN_ACCOUNT_DOWNLOAD_THREADS", 1, 8)}<span>/</span>${num("BACKUP_ACCOUNT_DOWNLOAD_THREADS", 1, 8)}`)}
        ${row("检查时每翻一页等待", "随机取这个范围内的时间", pair("DELAY_SYNC"))}
        ${row("每下载一个作品等待", "随机取这个范围内的时间", pair("DELAY_DOWNLOAD"))}
        ${row("周期性休息", "每个账号各算各的：某个账号下完一定数量后自己歇一会儿，别的账号照常下，所以账号越多总速度越快。真被限速时程序会自动等待，这一项只是预防，可以关掉。", sw("RATE_LIMIT_ENABLED"))}
        ${val("RATE_LIMIT_ENABLED") ? row("每个账号每下载", "", `${num("REST_EVERY", 10, 1000000)}<span>个文件，休息</span>${num("REST_SECONDS", 1, 3600)}<span>秒</span>`) : ""}
        ${row("失败率超过多少就暂停", "最近 20 次里失败的比例（0.1 – 0.9）", num("FAILURE_RATE_THRESHOLD", 0.1, 0.9, 0.05))}
        ${row("单个文件最多重试", "", `${num("MAX_RETRIES", 1, 10)}<span>次</span>`)}
      </div>
      ${saveBar(SPEED_KEYS)}`;
  }

  // ================= 常规：和下载内容无关的程序行为（这一页的改动都是马上生效） =================
  function pageGeneral() {
    if (!state.settings) return "";
    return `${solo && ctx.setSetting ? `<div class="group"><div class="gh">窗口</div>
      <div class="set"><div class="t">关闭窗口时${hintHTML("放到托盘后程序继续在后台运行，下载不会中断；点托盘里的图标回来，右键可以退出。")}</div>
        <div class="ctl"><div class="seg">${[["ask", "每次询问"], ["tray", "放到托盘"], ["exit", "直接退出"]].map(([v, l]) => `<button data-dlclose-pref="${v}" class="${(ctx.S.closeAction || "ask") === v ? "on" : ""}">${l}</button>`).join("")}</div></div></div></div>` : ""}
      ${ctx.setSetting && api.jobWatch ? `<div class="group"><div class="gh">任务结束后</div>
        <div class="set"><div class="t">弹出系统通知${hintHTML("检查或下载结束时在屏幕右下角提醒，窗口放在托盘里也能看到。")}</div>
          <div class="ctl"><button class="switch ${ctx.S.notifyOnFinish !== false ? "on" : ""}" data-uisw="notifyOnFinish" role="switch" aria-checked="${ctx.S.notifyOnFinish !== false}"></button></div></div>
        <div class="set"><div class="t">完成后运行的命令${hintHTML(`开始任务时可以选“完成后运行命令”。这里填要运行的程序或脚本，例如 <span class="mono">D:\\scripts\\after.bat</span>。留空则不能选这一项。`)}</div>
          <div class="ctl"><input type="text" data-f="afterCommand" data-uiin="afterCommand" value="${esc(ctx.S.afterCommand || "")}" placeholder="程序或脚本的完整路径" autocomplete="off" spellcheck="false"></div></div>
      </div>` : ""}
      <div class="group"><div class="gh">电脑睡眠</div>
        ${row("任务进行时不让电脑自动睡眠", "锁屏、关屏幕都不影响下载；会让下载停下来的是电脑空闲一段时间后自动睡眠。打开后，有任务在运行时电脑不会自己睡，任务结束或暂停后恢复正常。挡不住合上笔记本盖子和手动睡眠。", nowSw("KEEP_AWAKE"))}
      </div>`;
  }

  // ================= 数据文件夹 / 数据导入 =================
  // 画师头像：不导入也能补。“检查”只看头像文件夹里实际有没有文件（不访问 Pixiv），“补全”把缺的下回来
  function avatarTools(ready) {
    const c = state.avatars;
    return `<span class="tools">${hintHTML("“检查”看头像文件夹里是不是真的有每位画师的头像、还缺谁的，不访问 Pixiv。“补全”把缺的从 Pixiv 下回来：有地址的直接下，地址失效的由所有账号分着去问。平时检查更新时也会顺带把缺的和换过的头像下回来。")}
      <button class="btn" data-dl="check-avatars" ${ready && !(c && c.pending) && !jobRunning() ? "" : "disabled"}>${c && c.pending ? "正在检查…" : "检查"}</button>
      <button class="btn" data-dl="fill-avatars" ${ready && !jobRunning() && !(c && c.missing === 0) ? "" : "disabled"}>补全…</button></span>`;
  }
  function avatarResult() {
    const c = state.avatars;
    if (!c || c.pending) return "";
    if (c.error) return `<small class="res bad">${esc(c.error)}</small>`;
    if (!c.missing) return `<small class="res ok">${fmtNum(c.artists)} 位画师都有头像。</small>`;
    const names = c.items.slice(0, 6).map((x) => esc(x.name)).join("、");
    return `<small class="res warn">${fmtNum(c.artists)} 位画师里有 <b>${fmtNum(c.missing)}</b> 位缺头像：${names}${c.missing > 6 ? " 等" : ""}。点“补全”下载。</small>`;
  }

  // 数据文件夹：数据库、设置、头像都放在哪里
  function homeGroup() {
    const i = state.info || {}, files = i.files || {}, kinds = i.kinds || {};
    const have = (k) => files[k] && files[k].exists;
    const fileRow = (k) => !files[k] ? "" : `<div class="dl-file"><span class="nm">${esc(kinds[k]?.label || k)}</span>
        <span class="mono">${esc(files[k].path)}</span><span class="dl-tag ${have(k) ? "ok" : ""}">${have(k) ? esc(files[k].detail || "已有") : "还没有"}</span></div>`;
    return `<div class="group">
      <div class="set"><div class="t">数据文件夹<small class="mono">${esc(i.home || "")}</small><small>作品数据库、账号与下载设置、画师头像都在这个文件夹里。</small></div>
        <div class="ctl">${solo ? `<button class="btn" data-dl="open-home">打开文件夹</button>` : `<button class="btn" data-dl="set-home">更换…</button>`}</div></div>
      <div class="dl-files">${["works_db", "dl_settings", "avatars"].map(fileRow).join("")}</div>
      ${i.external ? `<div class="set"><div class="t">搬进查看器<small>现在用的是以前单独使用下载器时留下的数据文件夹。可以把里面的数据库、设置、头像复制到查看器自己的数据目录（<span class="mono">${esc(i.builtinHome || "")}</span>），之后只用这一份。原来的文件不会动。</small></div>
        <div class="ctl"><button class="btn" data-dl="migrate" ${state.busy === "migrate" ? "disabled" : ""}>${state.busy === "migrate" ? "正在复制…" : "复制过来"}</button></div></div>` : ""}
    </div>`;
  }
  const pageHome = () => homeGroup();

  function pageLink() {
    const i = state.info || {}, files = i.files || {}, kinds = i.kinds || {};
    const imp = state.imp;
    const have = (k) => files[k] && files[k].exists;
    const fileRow = (k) => !files[k] ? "" : `<div class="dl-file"><span class="nm">${esc(kinds[k]?.label || k)}</span>
        <span class="mono">${esc(files[k].path)}</span><span class="dl-tag ${have(k) ? "ok" : ""}">${have(k) ? esc(files[k].detail || "已有") : "还没有"}</span></div>`;
    const ready = imp.items.filter((x) => x.ok);
    return `${solo ? homeGroup() : ""}
    <div class="group"><div class="gh">导入已有的数据</div>
      <div class="dl-about"><p>把以前的数据导进来。<b>不用管文件名</b>：选好之后会按文件内容认出它是哪一种，改过名的文件也认得。也可以直接选原来的整个数据文件夹，里面认得的会一起找出来。${hintHTML(i.viewerKinds ? "评分、自定义标签、收藏、最近查看只保存在本机，Pixiv 上没有，不导入就无法补回。账号与下载设置不导入的话，重新登录、重新设置即可；头像可以用下面的“补全”下载。" : "账号与下载设置不导入的话，重新登录、重新设置即可；头像可以用下面的“补全”下载。")}</p></div>
      <div class="dl-kinds">${Object.entries(kinds).map(([k, v]) => `<div><span class="dl-tag ${v.need === "核心" ? "done" : ""}">${v.need}</span><b>${esc(v.label)}</b><span class="was">原名 ${esc(v.was)}</span>${k === "avatars" ? avatarTools(have("works_db")) : ""}<small>${esc(v.what)}</small>${k === "avatars" ? avatarResult() : ""}</div>`).join("")}</div>
      <div class="dl-actions"><button class="btn" data-dl="imp-files">选择文件…</button><button class="btn" data-dl="imp-folder">选择文件夹…</button>
        ${imp.items.length ? `<span class="sp"></span><button class="btn ghost" data-dl="imp-clear">清空</button>` : ""}</div>
      ${imp.items.length ? `<div class="dl-picked">${imp.items.map((x) => `<div class="${x.ok ? "ok" : "bad"}">${icon(x.ok ? "check" : "x")}
          <div><b>${esc(x.name)}</b><span class="arrow">→</span><b>${esc(x.ok ? x.label : "认不出来")}</b>${x.detail ? `<span class="was">${esc(x.detail)}</span>` : ""}
          <small>${x.ok ? (x.merge ? "会合并进现有的记录，现有的不会丢。" : x.replaces ? "会换成这一份。现有的那份改名后留在原来的文件夹里，不删除。" : "现在还没有这一项，直接放进去。") : esc(x.problem || "")}</small></div></div>`).join("")}</div>
        <div class="dl-actions"><button class="btn primary" data-dl="imp-apply" ${ready.length && !imp.busy && !jobRunning() ? "" : "disabled"}>${imp.busy ? "正在导入…" : `导入这 ${ready.length} 项`}</button>
          ${jobRunning() ? `<span class="dl-phase">有下载任务在运行，结束后才能导入</span>` : ready.some((x) => x.replaces) ? `<span class="dl-phase">其中有会替换现有数据的项目，点击后还要再确认一次</span>` : ""}</div>` : ""}
      ${imp.results ? `<div class="dl-picked">${imp.results.map((r) => `<div class="${r.ok ? "ok" : "bad"}">${icon(r.ok ? "check" : "x")}<div><b>${esc(r.label)}</b>
          <small>${esc(r.message || (r.ok ? "已导入" : "没有成功"))}${r.kept ? `<br>原来的那份保留在：<span class="mono">${esc(r.kept)}</span>` : ""}</small></div></div>`).join("")}</div>` : ""}
    </div>
    <div class="group"><div class="gh">维护</div>
      <div class="set"><div class="t">补全文件大小${hintHTML("早期下载的和导入的记录大多没有记文件大小，“待下载大约多大”的估算因此不准。这一项把保存位置里的文件逐个文件夹看一遍，把大小补进数据库。不访问 Pixiv、不改动任何文件；图库在网络共享上时要花几分钟，可以随时停止，已经补上的会保留。")}</div>
        <div class="ctl"><button class="btn" data-dl="fill-sizes" ${have("works_db") && !jobRunning() ? "" : "disabled"}>开始…</button></div></div></div>
    <div class="group"><div class="gh">说明</div><div class="dl-about">
      ${solo ? `<p>想换电脑或备份，把数据文件夹整个拷走即可；想恢复成全新状态，关闭程序后删掉它。里面的 settings.json 含有登录凭证，不要发给别人。</p>`
        : `<p>下载在后台以独立的进程运行，下载再忙也不影响看图，也不占用网络端口；关闭查看器时会处理完当前文件再退出。</p>`}
      <p>不会自动检查更新：只有你点了“开始”才会访问 Pixiv。</p>
      ${i.external ? `<p>如果你还在单独运行原来的下载器，请不要和这里同时使用——两边会读写同一个数据库。</p>` : ""}
      ${i.version ? `<p>版本 ${esc(i.version)}</p>` : ""}</div></div>`;
  }

  // ================= 事件 =================
  scrim.addEventListener("click", async (e) => {
    if (!isOpen()) return;
    const t = e.target;
    if (t.closest("[data-dlclose]")) return ctx.closeDialog();
    const nav = t.closest("[data-dlpage], [data-dlgo]");
    if (nav) return go(nav.dataset.dlpage || nav.dataset.dlgo);
    const now = t.closest("[data-dlnow]");
    if (now) {
      const k = now.dataset.dlnow, v = !state.settings[k];
      if (await guard(() => dl("POST", "/api/settings", { [k]: v }))) state.settings[k] = v;
      return draw();
    }
    const sw = t.closest("[data-dlsw]");
    if (sw) { state.form[sw.dataset.dlsw] = !val(sw.dataset.dlsw); return draw(); }
    const md = t.closest("[data-dlmode]");
    if (md) { state.form.STORAGE_MODE = md.dataset.dlmode; state.test = null; state.browse = null; return draw(); }
    const ps = t.closest("[data-dlpreset]");
    if (ps) { Object.assign(state.form, JSON.parse(JSON.stringify(PRESETS[ps.dataset.dlpreset][2]))); return draw(); }
    const ty = t.closest("[data-dltype]");
    if (ty) {
      const cur = new Set(val("SYNC_TYPES") || []);
      cur.has(ty.dataset.dltype) ? cur.delete(ty.dataset.dltype) : cur.add(ty.dataset.dltype);
      if (!cur.size) return toast("至少要选一种");
      state.form.SYNC_TYPES = ["illust", "manga"].filter((x) => cur.has(x)); return draw();
    }
    const br = t.closest("[data-dlbrowse]");
    if (br) { const [share, path] = br.dataset.dlbrowse.split("|"); return browse(share || "", path ? path.split("/").filter(Boolean) : []); }
    if (tasks.toggle(t)) return;
    const us = t.closest("[data-uisw]");
    if (us) { ctx.setSetting({ [us.dataset.uisw]: !(ctx.S[us.dataset.uisw] !== false) }); return draw(); }
    const cp = t.closest("[data-dlclose-pref]");
    if (cp) { ctx.setSetting({ closeAction: cp.dataset.dlclosePref }); return draw(); }
    const b = t.closest("[data-dl]"); if (!b || b.disabled) return;
    const act = b.dataset.dl;
    if (act === "reload") return go(state.page);
    if (await tasks.click(act, b)) return;
    // ---- 账号
    if (act === "acc-main") { await guard(() => dl("POST", "/api/accounts/main", { name: b.dataset.name }), "已设为主账号"); return go("accounts"); }
    if (act === "acc-del") {
      if (b.dataset.sure !== "1") { b.dataset.sure = "1"; b.textContent = "确定删除？"; b.classList.add("danger"); return; }
      await guard(() => dl("DELETE", "/api/accounts/" + encodeURIComponent(b.dataset.name)), "已删除账号"); return go("accounts");
    }
    if (act === "acc-test") {
      state.busy = "acc-test"; draw();
      const r = await guard(() => dl("POST", "/api/accounts/test"));
      state.busy = "";
      if (r) { const blind = r.items.filter((x) => x.ok && x.r18 === false).length; toast(`验证完成：${r.items.filter((x) => x.ok).length} / ${r.items.length} 个可用${blind ? `，其中 ${blind} 个看不到 R-18` : ""}`); }
      return go("accounts");
    }
    if (act === "oauth-start") {
      const r = await guard(() => dl("POST", "/api/accounts/oauth/start"));
      if (!r) return;
      state.oauth = r; api.openUrl(r.url); return draw();
    }
    if (act === "oauth-open") return void api.openUrl(state.oauth.url);
    if (act === "login-start") {
      state.loginError = "";
      const r = await api.loginStart("");
      if (!r || !r.ok) { state.loginError = (r && r.error) || "登录窗口没能打开"; return draw(); }
      state.login = { status: r.status || "waiting" };
      draw();
      return pollLogin();
    }
    if (act === "login-cancel") { await api.loginCancel(); state.login = null; return draw(); }
    if (act === "oauth-cancel") { state.oauth = null; return draw(); }
    if (act === "oauth-finish") {
      const callback = $("#dl-oauth-cb").value.trim(), name = $("#dl-oauth-name").value.trim();
      if (!callback) return toast("请先粘贴回调地址");
      state.busy = "oauth"; draw();
      const r = await guard(() => dl("POST", "/api/accounts/oauth/finish", { state: state.oauth.state, callback, name }));
      state.busy = "";
      if (r) { state.oauth = null; toast(`已添加账号 ${r.name}`); return go("accounts"); }
      return draw();
    }
    if (act === "tok-add") {
      const name = $("#dl-tok-name").value.trim(), token = $("#dl-tok-token").value.trim(), remark = $("#dl-tok-remark").value.trim();
      if (!name || !token) return toast("账号名和 refresh token 都要填");
      state.busy = "tok"; draw();
      const r = await guard(() => dl("POST", "/api/accounts", { name, token, remark }));
      state.busy = "";
      if (r) { toast(`已添加账号 ${name}${r.username ? "（" + r.username + "）" : ""}`); return go("accounts"); }
      return draw();
    }
    // ---- 设置
    if (act === "revert") { state.form = {}; state.test = null; state.ptest = null; return draw(); }
    if (act === "test-proxy") {
      state.ptest = { pending: true }; draw();
      const body = { PROXY_MODE: val("PROXY_MODE") || "system", PROXY_URL: val("PROXY_URL") || "" };
      try { state.ptest = await dl("POST", "/api/settings/test-proxy", body); }
      catch (err) { state.ptest = { ok: false, message: err.message || "测试没有完成", steps: [] }; }
      return draw();
    }
    if (act === "pick-local") { const p = await api.pickDirectory(); if (p) { state.form.LOCAL_SAVE_PATH = p; draw(); } return; }
    if (act === "test") {
      state.test = { pending: true, steps: [] }; draw();
      const body = { ...state.settings, ...state.form };
      const r = await guard(() => dl("POST", "/api/settings/test-storage", body));
      state.test = r || { ok: false, message: "测试没有完成", steps: [] };
      return draw();
    }
    if (act === "save") {
      const body = {};
      for (const k of b.dataset.keys.split(",")) if (k in state.form && JSON.stringify(state.form[k]) !== JSON.stringify(state.settings[k])) body[k] = state.form[k];
      const r = await guard(() => dl("POST", "/api/settings", body));
      if (r) {
        toast(r.warning || "已保存");
        // 换了保存位置：资料库跟着变，重新读一遍
        if (state.page === "storage" && !solo) { await guard(() => api.dlRefreshLibrary([])); await ctx.reloadLibrary(); ctx.loadWorks(); }
        return go(state.page);
      }
      return;
    }
    if (act === "browse") return browse("", []);
    if (act === "browse-close") { state.browse = null; return draw(); }
    if (act === "browse-use") { state.form.NAS_SHARE = state.browse.share; state.form.NAS_BASE_PATH = state.browse.path.join("/"); state.browse = null; return draw(); }
    // ---- 连接
    if (act === "link-lib") {
      const r = await api.dlLinkLibrary();
      if (!r.ok) return toast(r.error || "没有成功");
      state.link = r; toast("已加入资料库，正在扫描…");
      draw();
      await ctx.reloadLibrary(); ctx.loadWorks();
      return;
    }
    if (act === "open-home") return void api.openHome();
    // ---- 导入
    if (act === "imp-files" || act === "imp-folder") {
      const picked = await api.importPick(act === "imp-folder" ? "folder" : "files");
      if (!picked || !picked.length) return;
      const imp = state.imp;
      imp.results = null;
      for (const x of picked) {
        // 同一种数据只留最后选的那个；认不出来的按路径去重
        imp.items = imp.items.filter((y) => (x.ok ? y.kind !== x.kind : y.path !== x.path));
        imp.items.push(x);
      }
      imp.items.sort((p, q) => (q.ok - p.ok));
      return draw();
    }
    if (act === "imp-clear") { state.imp = { items: [], results: null, busy: false }; return draw(); }
    if (act === "imp-apply") {
      const ready = state.imp.items.filter((x) => x.ok);
      if (ready.some((x) => x.replaces) && b.dataset.sure !== "1") { b.dataset.sure = "1"; b.textContent = "确定替换并导入？"; b.classList.add("danger"); return; }
      state.imp.busy = true; draw();
      const r = await api.importApply(ready.map((x) => x.path));
      state.imp = { items: [], results: r.results || [{ label: "导入", ok: false, message: r.error || "没有成功" }], busy: false };
      toast(r.ok ? (r.restart ? "已安排导入：请关闭程序后重新打开" : "导入完成") : (r.error || "有项目没有导入成功"));
      state.info = await api.dlInfo();
      state.plan = null; state.accounts = null; state.settings = null;
      if (!solo) { await ctx.reloadLibrary(); ctx.loadWorks(); }
      return draw();
    }
    if (act === "fill-sizes") {
      const c = { kind: "fill_sizes", simple: true, title: "补全文件大小", text: "把保存位置里已有文件的大小补进数据库。不访问 Pixiv、不改动任何文件，可以随时停止。" };
      if (solo) { mount("update"); state.confirm = c; return; }
      return void ctx.openDownloader("update").then(() => { state.confirm = c; draw(); });
    }
    if (act === "check-avatars") {
      state.avatars = { pending: true }; draw();
      try { state.avatars = await dl("POST", "/api/avatars/check"); } catch (e) { state.avatars = { error: e.message }; }
      if (!solo && state.avatars.fixed) await ctx.reloadLibrary();
      return draw();
    }
    if (act === "fill-avatars") {
      const c = { kind: "download_avatars", simple: true, title: "补全头像", text: "下载头像文件夹里还没有的画师头像。会访问 Pixiv，可以随时暂停或停止；没成功的再点一次就会接着补。" };
      state.avatars = null;
      if (solo) { mount("update"); state.confirm = c; return; }
      return void ctx.openDownloader("update").then(() => { state.confirm = c; draw(); });
    }
    if (act === "set-home") {
      state.info = await api.dlSetHome();
      toast("已改用这个数据目录"); await ctx.reloadLibrary();
      return draw();
    }
    if (act === "migrate") {
      if (b.dataset.sure !== "1") { b.dataset.sure = "1"; b.textContent = "确定复制？"; return; }
      state.busy = "migrate"; draw();
      const r = await api.dlMigrate();
      state.busy = "";
      if (!r.ok) toast(r.error || "没有成功"); else { state.info = r.info; toast("已复制完成，现在使用查看器自己的数据目录"); await ctx.reloadLibrary(); }
      return draw();
    }
  });
  async function browse(share, path) {
    state.browse = { share, path, entries: [], loading: true }; draw();
    const r = await guard(() => dl("POST", "/api/storage/browse", { mode: "smb", ...state.settings, ...state.form, share, path: path.join("/") }));
    if (!state.browse) return;
    state.browse = { share, path, entries: (r && r.entries) || [], error: r && !r.ok ? r.error : (r ? "" : "读取失败") };
    draw();
  }
  scrim.addEventListener("change", (e) => {
    if (isOpen() && e.target.dataset.uiin) { ctx.setSetting({ [e.target.dataset.uiin]: e.target.value.trim() }); return; }
    if (isOpen() && tasks.change(e)) return;
    if (isOpen() && e.target.matches("[data-dlproxysel]")) { state.form.PROXY_MODE = e.target.value; state.ptest = null; return draw(); }
    if (!isOpen() || !e.target.matches("[data-dlmodesel]")) return;
    state.form.STORAGE_MODE = e.target.value; state.test = null; state.browse = null;
    draw();
  });
  scrim.addEventListener("input", (e) => {
    if (!isOpen()) return;
    const t = e.target;
    if (t.dataset.dlin) { state.form[t.dataset.dlin] = t.value; syncSave(); }
    if (t.dataset.dlnum) { state.form[t.dataset.dlnum] = t.value === "" ? "" : +t.value; syncSave(); }
    if (t.dataset.dlpair) { const [k, i] = t.dataset.dlpair.split(":"); const v = [...(val(k) || [0, 0])]; v[+i] = +t.value; state.form[k] = v; syncSave(); }
  });
  // 登录窗口开着的时候，隔一会儿问一次进行到哪了
  async function pollLogin() {
    while (state.login) {
      await new Promise((r) => setTimeout(r, 600));
      if (!state.login) return;
      const r = await api.loginStatus();
      const status = (r && r.status) || "error";
      if (status === "waiting" || status === "finishing") {
        if (status !== state.login.status) { state.login.status = status; if (isOpen() && state.page === "accounts") draw(); }
        continue;
      }
      state.login = null;
      if (status === "done") { toast(`已添加账号 ${r.name || ""}`); if (isOpen() && state.page === "accounts") go("accounts"); return; }
      state.loginError = status === "cancelled" ? "" : (r && r.message) || "没能完成登录";
      if (isOpen() && state.page === "accounts") draw();
      return;
    }
  }
  // 输入过程中不整页重画（会打断输入），只更新“保存”按钮能不能点
  function syncSave() {
    const b = scrim.querySelector('[data-dl="save"]'); if (!b) return;
    b.disabled = !(dirty(b.dataset.keys.split(",")) && !jobRunning());
  }

  // ================= 入口 =================
  if ($("#btn-dl")) $("#btn-dl").addEventListener("click", () => ctx.openDownloader());
}

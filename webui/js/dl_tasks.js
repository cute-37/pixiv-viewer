// “下载与更新”面板里的两页：更新（计划、任务选项、进行中、结果、先看再下）和失败处理（文件、画师）。
// 由 downloader.js 创建：D 里是它那边的状态和工具（state、dl、guard、draw、go、startJob……）。
import { esc, icon, toast, fmtNum, fmtSize } from "./util.js";

const TYPE_LABEL = { illust: "插画", manga: "漫画", ugoira: "动图", novel: "小说" };
const TYPES = Object.keys(TYPE_LABEL);
const SYNC_FAIL = {
  rate_limit: ["被限速", "检查时被 Pixiv 限速，而且等了很久也没恢复。过一段时间重查即可。"],
  network: ["网络或服务器问题", "读取作品列表时出错，通常重查就能恢复。"],
  auth: ["账号登录失效", "检查用的账号登录失效了，请到“账号”页重新登录后重查。"],
  other: ["其他错误", "可以先重查一次；反复失败请看详细日志。"],
  gone: ["已注销或不存在", "Pixiv 上找不到这位画师了。确认之后可以设为“不再检查”，以后检查时就不会再为他们花请求。"],
};
const SCOPES = [
  ["all", "关注的全部画师"], ["folder", "某个文件夹里的画师"], ["pinned", "置顶的画师"], ["failed", "上次检查失败的"],
  ["never", "从来没查成功过的"], ["stale", "超过几天没查的"],
];
const AFTER = [["none", "什么都不做"], ["sleep", "让电脑睡眠"], ["hibernate", "让电脑休眠"], ["shutdown", "关机"], ["exit", "退出软件"], ["command", "运行命令"]];
const isSync = (kind) => kind === "sync" || kind === "sync_download";
const isDownload = (kind) => kind === "download" || kind === "sync_download";

export function createTasks(D) {
  const { state, dl, guard, draw, go, startJob, onJob, ctx, api, solo, fmtDur, fmtWhen, KIND_LABEL, STATUS_LABEL, FAIL_KINDS, logBox, fetchLogs } = D;
  Object.assign(state, {
    opts: null,              // 确认卡片里“更多选项”的当前取值
    optsOpen: false,
    review: null,            // 先看再下：{data, off:Set(不下载的画师), types:Set, origin, skipped}
    failTab: "files",        // 失败处理页当前看的是 文件 还是 画师
    syncFails: null,         // /api/sync/failures 的结果
    failList: null,          // 展开的某一组失败文件：{kind, status, items, total, page, q}
    openGroups: new Set(),   // 展开的“检查失败”分组
  });
  const running = () => { const j = state.job; return !!(j && (j.running || j.status === "running")); };
  // “完成后做什么”的下拉框：每次任务单独选；没填命令时“运行命令”选不了
  const afterSelect = () => (!api.jobWatch ? "" : `<select data-after aria-label="完成后">${AFTER.map(([v, l]) => {
    const noCmd = v === "command" && !((ctx.S && ctx.S.afterCommand) || "").trim();
    return `<option value="${v}" ${(state.after || "none") === v ? "selected" : ""} ${noCmd ? "disabled" : ""}>${l}${noCmd ? "（先在“设置 → 常规”里填命令）" : ""}</option>`;
  }).join("")}</select>`);
  const viewerArtist = (authorId) => (ctx.lib && ctx.lib.artists || []).find((a) => String(a.id) === String(authorId));
  const pixivWork = (id) => `https://www.pixiv.net/artworks/${id}`;
  const pixivUser = (id) => `https://www.pixiv.net/users/${id}`;

  // ================================================================ 更新页
  function pageUpdate() {
    const job = state.job || {}, plan = state.plan || {};
    const showResult = !running() && job.kind && job.kind !== "idle" && state.dismissed !== job.id;
    let html = (state.notes || []).map((n) => `<div class="dl-note lv-${n.level}">${icon(n.level === "info" ? "info" : "warn")}<div><b>${esc(n.title)}</b>${n.detail ? `<small>${esc(n.detail)}</small>` : ""}</div>
      ${n.action ? `<button class="btn" data-dlgo="${n.action.route.includes("accounts") ? "accounts" : n.action.route.includes("tasks") ? "failed" : "update"}">${esc(n.action.label)}</button>` : ""}</div>`).join("");
    if (running()) html += runningCard(job);
    else if (state.review) html += reviewCard();
    else if (state.confirm) html += confirmCard(state.confirm, plan);
    else if (showResult) html += resultCard(job);
    else html += planCard(plan);
    if (!running() && !state.review && (state.runs || []).length) {
      html += `<div class="group"><div class="gh">最近的任务</div>${state.runs.map((r) => `<div class="set dl-run"><div class="t">${esc(KIND_LABEL[r.kind] || r.kind)}<small>${fmtWhen(r.finished || r.started)}${runLine(r)}</small></div>
        <div class="ctl"><span class="dl-tag ${r.status}">${STATUS_LABEL[r.status] || r.status}</span></div></div>`).join("")}</div>`;
    }
    return html;
  }
  const runLine = (r) => (r.kind.startsWith("sync") && !r.kind.includes("download")
    ? `${r.success ? ` · 查了 ${fmtNum(r.success)} 位` : ""}${r.failed ? ` · 失败 ${fmtNum(r.failed)} 位` : ""}`
    : `${r.success ? ` · 下载 ${fmtNum(r.success)} 个` : ""}${r.failed ? ` · 失败 ${fmtNum(r.failed)}` : ""}`);

  function planCard(plan) {
    const noAccount = !plan.accounts_valid;
    const hints = [];
    if (plan.resume) hints.push(`<div class="dl-hintrow">${icon("info")}<span>上次检查没有查完，还有 <b>${fmtNum(plan.resume.remaining)}</b> 位画师没查到。</span><button class="btn" data-dl="ask-resume">接着查</button></div>`);
    if (plan.sync_failed) hints.push(`<div class="dl-hintrow">${icon("warn")}<span>有 <b>${fmtNum(plan.sync_failed)}</b> 位画师上次检查失败。</span><button class="btn" data-dl="ask-failed">重查这些</button><button class="btn ghost" data-dl="fail-tab" data-tab="artists">查看</button></div>`);
    if (plan.skipped_total) hints.push(`<div class="dl-hintrow">${icon("info")}<span>有 <b>${fmtNum(plan.skipped_total)}</b> 个文件被标为“不下载”。</span><button class="btn ghost" data-dl="review" data-skipped="1">查看 / 恢复</button></div>`);
    return `<div class="group dl-plan">
      <div class="dl-stats">
        <div><b>${fmtWhen(plan.last_sync)}</b><span>上次检查更新</span></div>
        <div><b>${fmtNum(plan.artists || 0)}</b><span>位关注的画师</span></div>
        <div><b>${fmtNum(plan.pending || 0)}</b><span>个文件待下载${plan.estimated_bytes ? ` · 约 ${fmtSize(plan.estimated_bytes)}` : ""}</span></div>
      </div>
      <div class="dl-actions">
        <button class="btn primary big" data-dl="ask" data-kind="sync_download" ${noAccount ? "disabled" : ""}>${icon("sync")}检查更新并下载</button>
        <button class="btn" data-dl="ask" data-kind="download" ${noAccount || !plan.pending ? "disabled" : ""}>只下载待下载的</button>
        <button class="btn" data-dl="ask" data-kind="sync" ${noAccount ? "disabled" : ""}>只检查，不下载</button>
        ${plan.pending ? `<span class="sp"></span><button class="btn ghost" data-dl="review">看看待下载的有哪些</button>` : ""}
      </div>
      ${hints.join("")}
      <p class="dl-hint">${noAccount ? "还没有可用的 Pixiv 账号，请先到“账号”页添加。" : "点击后会先告诉你要做什么，确认了才开始。不会在后台自动运行。"}</p>
    </div>`;
  }

  // ---------------------------------------------------------------- 任务选项
  const defaults = () => ({ scope: "all", folder: "", staleDays: 7, deep: false, backfillManga: false, resume: "",
    types: new Set(TYPES), dateFrom: "", dateTo: "", perArtist: "", limit: "", noR18: false, review: "auto", accounts: null });
  const folders = () => (ctx.lib && ctx.lib.folders) || [];
  function scopeIds(o) {
    const artists = (ctx.lib && ctx.lib.artists) || [];
    let list = [];
    if (o.scope === "folder") { const f = folders().find((x) => String(x.id) === String(o.folder)); const keys = new Set(f ? f.artists : []); list = artists.filter((a) => keys.has(a.key)); }
    if (o.scope === "pinned") list = artists.filter((a) => a.pinned);
    return list.map((a) => +a.id).filter(Boolean);
  }
  /** 把选项变成后端认识的参数；选得不对时返回 {error} */
  function buildParams(c) {
    const o = state.opts || defaults(), p = { ...(c.params || {}) };
    if (isSync(c.kind)) {
      p.deep = !!o.deep;
      if (o.scope === "folder" || o.scope === "pinned") {
        const ids = scopeIds(o);
        if (!ids.length) return { error: o.scope === "folder" ? "这个文件夹里没有可以检查的画师" : "还没有置顶的画师" };
        p.author_ids = ids;
      } else if (o.scope !== "all") { p.scope = o.scope; if (o.scope === "stale") p.stale_days = +o.staleDays || 7; }
      if (o.resume) p.resume_since = o.resume;
      if (o.backfillManga) p.backfill = ["manga"];
    }
    if (isDownload(c.kind)) {
      if (o.types.size && o.types.size < TYPES.length) p.types = [...o.types];
      if (!o.types.size) return { error: "至少要选一种作品类型" };
      if (o.dateFrom) p.date_from = o.dateFrom;
      if (o.dateTo) p.date_to = o.dateTo;
      if (+o.perArtist > 0) p.max_per_artist = +o.perArtist;
      if (+o.limit > 0) p.limit = +o.limit;
      if (o.noR18) p.exclude_r18 = true;
    }
    if (c.kind === "sync_download" && o.review !== "auto") p.review = o.review;
    if (o.accounts && state.accounts && o.accounts.size < state.accounts.filter((a) => a.is_valid).length) {
      if (!o.accounts.size) return { error: "至少要用一个账号" };
      p.accounts = [...o.accounts];
    }
    return p;
  }
  function optionsHTML(c, plan) {
    const o = state.opts;
    const row = (t, h, ctl) => `<div class="set"><div class="t">${t}${h ? `<small>${h}</small>` : ""}</div><div class="ctl">${ctl}</div></div>`;
    const sel = (key, items) => `<select data-opt="${key}">${items.map(([v, l]) => `<option value="${esc(String(v))}" ${String(o[key]) === String(v) ? "selected" : ""}>${esc(l)}</option>`).join("")}</select>`;
    const chk = (key) => `<button class="switch ${o[key] ? "on" : ""}" data-optsw="${key}" role="switch" aria-checked="${!!o[key]}"></button>`;
    const num = (key, ph) => `<input type="number" min="0" data-opt="${key}" value="${esc(String(o[key] || ""))}" placeholder="${ph}">`;
    let html = "";
    if (isSync(c.kind) && !(c.params && c.params.author_id)) {
      const scopes = SCOPES.filter(([v]) => (v !== "folder" || folders().length) && (v !== "pinned" || !solo) && (v !== "folder" || !solo));
      html += `<div class="gh">查谁</div>
        ${row("范围", o.resume ? "只查上次没查到的那些。" : "最久没查的画师排在前面：中途停下的话，下次不会总是同一批轮不到。", sel("scope", scopes)
          + (o.scope === "folder" ? sel("folder", folders().map((f) => [f.id, `${f.name}（${f.artists.length}）`])) : "")
          + (o.scope === "stale" ? `<span>超过</span>${num("staleDays", "7")}<span>天</span>` : ""))}
        ${row("全量检查", "重新扫描每位画师的全部作品，很慢，一般不需要。", chk("deep"))}
        ${row("补上以前漏掉的漫画", "旧版本只按一个进度检查，后来才有的漫画类作品只会补到最近几个。打开后把漫画从头扫一遍（请求会多不少）。", chk("backfillManga"))}`;
    }
    if (isDownload(c.kind)) {
      html += `<div class="gh">下什么</div>
        ${row("作品类型", "", `<div class="seg multi">${TYPES.map((t) => `<button data-opttype="${t}" class="${o.types.has(t) ? "on" : ""}">${TYPE_LABEL[t]}</button>`).join("")}</div>`)}
        ${row("投稿日期", "只下载这段时间内投稿的作品，留空表示不限。", `<input type="date" data-opt="dateFrom" value="${esc(o.dateFrom)}"><span>到</span><input type="date" data-opt="dateTo" value="${esc(o.dateTo)}">`)}
        ${row("每位画师最多", "只取每位画师最新的若干个文件（同一个作品的各页不拆开）。留空表示不限。", `${num("perArtist", "不限")}<span>个文件</span>`)}
        ${row("这次一共最多", "留空表示不限。", `${num("limit", "不限")}<span>个文件</span>`)}
        ${row("不下载 R-18 作品", "", chk("noR18"))}`;
    }
    if (c.kind === "sync_download") {
      html += `<div class="gh">查完之后</div>
        ${row("是否先让我看一眼", `“按设置”是指新发现的文件超过 ${fmtNum(plan.review_threshold || 0)} 个时先停下来（在“设置 → 下载内容”里改）。`,
          sel("review", [["auto", plan.review_threshold ? `按设置（超过 ${fmtNum(plan.review_threshold)} 个先问我）` : "按设置（不询问）"], ["always", "每次都先让我看"], ["never", "直接下载"]]))}`;
    }
    const valid = (state.accounts || []).filter((a) => a.is_valid);
    if (valid.length > 1) {
      const on = o.accounts || new Set(valid.map((a) => a.name));
      html += `<div class="gh">用哪些账号</div>
        ${row("本次使用的账号", "没选中的账号这次不发任何请求。", `<div class="seg multi">${valid.map((a) => `<button data-optacc="${esc(a.name)}" class="${on.has(a.name) ? "on" : ""}">${esc(a.name)}</button>`).join("")}</div>`)}`;
    }
    return `<div class="dl-opts">${html}</div>`;
  }
  function confirmCard(c, plan) {
    const o = state.opts || defaults();
    const text = c.text || {
      sync_download: `检查${o.resume ? "上次没查到的" : ""}画师有没有新作品，然后下载新作品${plan.pending ? `和现有的 ${fmtNum(plan.pending)} 个待下载文件` : ""}。`,
      download: `下载现有的 ${fmtNum(plan.pending || 0)} 个待下载文件${plan.estimated_bytes ? `（约 ${fmtSize(plan.estimated_bytes)}）` : ""}，不检查新作品。`,
      sync: `检查${o.resume ? "上次没查到的" : ""}画师有没有新作品，只记录、不下载。`,
    }[c.kind];
    const hasOpts = isSync(c.kind) || isDownload(c.kind);
    return `<div class="group dl-confirm">
      <div class="dl-confirm-t">${icon("sync")}<b>${esc(c.title || KIND_LABEL[c.kind])}</b></div>
      <p>${esc(text)}</p>
      <p class="dl-hint">期间会按“设置 → 速度与网络”里的间隔访问 Pixiv，可以随时暂停或停止。${plan.accounts_valid ? `将使用 ${plan.accounts_valid} 个账号。` : ""}</p>
      ${hasOpts && !c.simple ? `<button class="linkbtn dl-more" data-dl="opts">${state.optsOpen ? "收起选项" : "更多选项：查谁、下什么、用哪些账号…"}</button>${state.optsOpen ? optionsHTML(c, plan) : ""}` : ""}
      ${api.jobWatch ? `<div class="dl-after"><span>完成后</span>${afterSelect()}<small>${(state.after || "none") === "none" ? "只对这一次任务有效。" : "任务正常做完才会执行，执行前有倒计时可以取消；手动停止或出错时不执行。"}</small></div>` : ""}
      <div class="dl-actions"><button class="btn primary" data-dl="start">开始</button><button class="btn ghost" data-dl="cancel-ask">取消</button></div>
    </div>`;
  }

  // ---------------------------------------------------------------- 进行中 / 结果
  function runningCard(job) {
    const pct = job.total ? Math.min(100, (job.done / job.total) * 100) : 0;
    const sp = state.speed, left = job.total - job.done;
    const eta = !job.paused && sp.ips > 0.01 && left > 0 ? fmtDur(left / sp.ips) : "";
    const workers = (job.workers || []).filter((w) => w.text || w.state || w.note);
    const logs = (job.logs || []).slice(-3);
    // 一个账号可以有几个线程：每个线程正在处理的作品各占一行（账号名只写在第一行）。
    // 账号整体在休息、暂停、被停用时只写一行状态。
    const workerRows = (w) => {
      const label = w.note || ({ resting: w.text || "休息中", paused: "已暂停", stopped: "已停用" }[w.state]);
      const lines = label ? [label] : (w.items && w.items.length ? w.items : [w.text || ({ queue: "等待", waiting: "间隔中", done: "已完成" }[w.state] || "")]);
      const many = (w.threads || 1) > 1 ? `<span class="th" title="这个账号用 ${w.threads} 个线程">×${w.threads}</span>` : "";
      return lines.map((t, i) => `<div><span class="nm" title="${esc(w.name)}">${i ? "" : esc(w.name) + many}</span><span class="tx">${esc(t)}</span></div>`).join("");
    };
    const checking = job.phase === "同步" || (job.kind.startsWith("sync") && job.phase !== "下载");
    return `<div class="group dl-run-card ${job.paused ? "paused" : ""}">
      <div class="dl-run-head"><span class="dl-spin"></span><b>${esc(KIND_LABEL[job.kind] || job.kind)}</b><span class="dl-phase">${job.paused ? "已暂停" : esc(job.phase === "同步" ? "检查中" : job.phase || "")}</span>
        <span class="sp"></span>
        <button class="btn" data-dl="${job.paused ? "resume" : "pause"}" ${job.stopping ? "disabled" : ""} title="${job.paused ? "接着做，进度保留" : "正在处理的文件做完后停下来，进度保留"}">${job.paused ? "继续" : "暂停"}</button>
        <button class="btn" data-dl="stop" ${job.stopping ? "disabled" : ""}>${job.stopping ? "正在停止…" : "停止"}</button></div>
      <div class="dl-bar ${job.total ? "" : "indet"}"><i style="width:${pct}%"></i></div>
      <div class="dl-nums">
        <span><b>${fmtNum(job.done)}</b>${job.total ? ` / ${fmtNum(job.total)}` : ""}${checking ? " 位画师" : ""}</span>
        ${job.success ? `<span>成功 ${fmtNum(job.success)}</span>` : ""}${job.skipped ? `<span>跳过 ${fmtNum(job.skipped)}</span>` : ""}${job.failed ? `<span class="bad">失败 ${fmtNum(job.failed)}</span>` : ""}
        ${job.bytes ? `<span>${fmtSize(job.bytes)}</span>` : ""}${!job.paused && sp.bps > 1024 ? `<span>${fmtSize(sp.bps)}/s</span>` : ""}
        <span class="sp"></span><span>已用 ${fmtDur(job.elapsed || 0)}${eta ? ` · 约剩 ${eta}` : ""}</span>
      </div>
      ${job.message ? `<p class="dl-hint">${esc(job.message)}</p>` : ""}
      ${!job.paused && job.idle >= 120 ? `<p class="dl-hint warn">${icon("clock")}已经 ${fmtDur(job.idle)} 没有新进展，仍在等待。通常是被限速、网络不通或保存位置响应慢，看下面每个账号的状态；一直不恢复的话可以停止后重新开始，进度会保留。</p>` : ""}
      ${api.jobWatch ? `<div class="dl-after"><span>完成后</span>${afterSelect()}</div>` : ""}
      ${workers.length ? `<div class="dl-workers">${workers.map(workerRows).join("")}</div>` : ""}
      ${state.logsOpen ? logBox() : logs.length ? `<div class="dl-logs">${logs.map((l) => `<div>${esc(l.msg)}</div>`).join("")}</div>` : ""}
      <button class="linkbtn" data-dl="logs">${state.logsOpen ? "收起日志" : "查看详细日志"}</button>
    </div>`;
  }
  function resultCard(job) {
    const d = job.detail || {}, res = job.result || {};
    const list = (group, fmt, n = 14) => Object.values(d[group] || {}).sort((a, b) => (b.files || b.works || 0) - (a.files || a.works || 0)).slice(0, n).map(fmt).join("");
    const more = (group, n = 14) => { const c = Object.keys(d[group] || {}).length; return c > n ? `<span class="dl-chip more">等 ${c} 位</span>` : ""; };
    const got = list("downloaded", (x) => `<span class="dl-chip">${esc(x.name || "?")}<b>${fmtNum(x.files || 0)}</b></span>`);
    const fresh = list("new", (x) => `<span class="dl-chip">${esc(x.name || "?")}<b>+${fmtNum(x.files || x.works || 0)}</b></span>`);
    const fails = Object.entries(d.fail_kinds || {}).map(([k, v]) => `<span class="dl-chip bad">${esc((FAIL_KINDS[k] || FAIL_KINDS.other)[0])}<b>${fmtNum(v.count || 0)}</b></span>`).join("");
    const failedArtists = Object.entries(d.failed || {});
    const checkedOnly = !("tasks" in res) && "artists" in res;            // 只检查、没有下载阶段
    const nums = [];
    if ("artists" in res) nums.push(`<span>检查了 <b>${fmtNum(res.artists_ok || 0)}</b> 位画师</span>`);
    if (res.new_files) nums.push(`<span>新发现 <b>${fmtNum(res.new_files)}</b> 个文件${res.old_files ? `（其中 ${fmtNum(res.old_files)} 个是以前漏掉的旧作品）` : ""}</span>`);
    if (!checkedOnly) nums.push(`<span>下载成功 <b>${fmtNum(job.success || 0)}</b></span>`);
    if (!checkedOnly && job.skipped) nums.push(`<span>跳过 ${fmtNum(job.skipped)}</span>`);
    if (!checkedOnly && job.failed) nums.push(`<span class="bad">下载失败 ${fmtNum(job.failed)}</span>`);
    if (!checkedOnly && job.bytes) nums.push(`<span>${fmtSize(job.bytes)}</span>`);
    const notes = [];
    if (res.needs_review) notes.push(`<div class="dl-hintrow strong">${icon("info")}<span>新发现的文件比较多，还没有开始下载。先看看都是谁的，再决定下哪些。</span><button class="btn primary" data-dl="review">查看并选择</button></div>`);
    const avatarJob = job.kind === "download_avatars";
    if (job.kind === "fill_sizes") {
      nums.length = 0;
      nums.push(`<span>检查了 <b>${fmtNum(res.checked || 0)}</b> 个文件</span>`, `<span>补上 <b>${fmtNum(res.sizes || 0)}</b> 个的大小</span>`);
      if (res.skipped_artists) nums.push(`<span>${fmtNum(res.skipped_artists)} 位画师的文件夹读不到，跳过</span>`);
      if (res.missing) notes.push(`<div class="dl-hintrow">${icon("info")}<span>另外有 <b>${fmtNum(res.missing)}</b> 个文件记录着“已下载”，但保存位置里没有找到。这次没有改动它们。</span></div>`);
    }
    if (avatarJob) {
      nums.length = 0;
      nums.push(`<span>下载了 <b>${fmtNum(job.success || 0)}</b> 个头像</span>`);
      if (job.failed) nums.push(`<span class="bad">没成功 ${fmtNum(job.failed)} 个</span>`);
      if (res.not_done) nums.push(`<span>还有 ${fmtNum(res.not_done)} 个没轮到</span>`);
      if (failedArtists.length) notes.push(`<div class="dl-hintrow">${icon("warn")}<span>这些画师的头像没下载成功，再试一次就会接着补（已经有的不会重下）。</span><button class="btn" data-dl="fill-avatars">再试一次</button></div>
        <div class="dl-tasklist">${failedArtists.slice(0, 200).map(([id, v]) => `<div class="dl-task"><div class="t"><b>${esc(v.name || "画师 " + id)}</b><span class="by">ID ${esc(id)}</span>${v.note ? `<small class="mono">${esc(v.note)}</small>` : ""}</div></div>`).join("")}</div>`);
    }
    else if (failedArtists.length) notes.push(`<div class="dl-hintrow">${icon("warn")}<span><b>${fmtNum(failedArtists.length)}</b> 位画师检查失败：${failedArtists.slice(0, 4).map(([, v]) => esc(v.name || "?")).join("、")}${failedArtists.length > 4 ? " 等" : ""}</span><button class="btn" data-dl="ask-failed">重查这些</button><button class="btn ghost" data-dl="fail-tab" data-tab="artists">查看原因</button></div>`);
    if (res.unchecked) notes.push(`<div class="dl-hintrow">${icon("warn")}<span>账号被限速太久，还有 <b>${fmtNum(res.unchecked)}</b> 位画师这次没查到（不算失败）。过一会儿可以接着查。</span><button class="btn" data-dl="ask-resume">接着查</button></div>`);
    if (res.following_incomplete) notes.push(`<div class="dl-hintrow">${icon("warn")}<span>关注列表没有读全：${esc(res.following_incomplete)}。没读到的画师已按以前的记录补上，但新关注的可能漏了。</span></div>`);
    const partial = Object.values(d.partial || {});
    if (partial.length) notes.push(`<div class="dl-hintrow">${icon("info")}<span>${fmtNum(partial.length)} 位画师的部分内容没读到（${esc(partial[0].note || "")}），下次检查会再试。</span></div>`);
    if (res.unprocessed_groups && job.status === "done") notes.push(`<div class="dl-hintrow">${icon("info")}<span>还有 ${fmtNum(res.unprocessed_groups)} 个作品这次没下完（账号被限速太久），仍在待下载里。</span></div>`);
    return `<div class="group dl-result ${job.status}">
      <div class="dl-run-head">${icon(job.status === "done" ? "check" : "warn")}<b>${esc(KIND_LABEL[job.kind] || job.kind)}${STATUS_LABEL[job.status] ? " · " + STATUS_LABEL[job.status] : ""}</b>
        <span class="sp"></span><span class="dl-phase">用时 ${fmtDur(job.elapsed || 0)}</span></div>
      ${job.error ? `<p class="dl-err">${esc(job.error)}</p>` : ""}
      <div class="dl-nums">${nums.join("")}</div>
      ${notes.join("")}
      ${fresh ? `<div class="dl-sec"><span class="lbl">发现新作品（文件数）</span><div class="dl-chips">${fresh}${more("new")}</div></div>` : ""}
      ${got ? `<div class="dl-sec"><span class="lbl">已下载</span><div class="dl-chips">${got}${more("downloaded")}</div></div>` : ""}
      ${fails ? `<div class="dl-sec"><span class="lbl">下载失败的原因</span><div class="dl-chips">${fails}</div></div>` : ""}
      ${!fresh && !got && !fails && !notes.length && job.status === "done" ? `<p class="dl-hint">没有新的内容。</p>` : ""}
      ${state.logsOpen ? logBox() : ""}
      <div class="dl-actions"><button class="btn ${res.needs_review ? "" : "primary"}" data-dl="dismiss">好的</button>${!checkedOnly && job.failed ? `<button class="btn" data-dlgo="failed">处理失败的文件</button>` : ""}
        <span class="sp"></span><button class="linkbtn" data-dl="logs">${state.logsOpen ? "收起日志" : "查看详细日志"}</button></div>
    </div>`;
  }

  // ---------------------------------------------------------------- 先看再下
  async function openReview(skipped) {
    state.confirm = null;
    state.review = { loading: true, skipped: !!skipped, off: new Set(), types: new Set(TYPES), origin: "", data: null };
    draw();
    await loadReview();
  }
  async function loadReview() {
    const rv = state.review; if (!rv) return;
    const filters = {};
    if (rv.types.size < TYPES.length) filters.types = [...rv.types];
    if (rv.origin) filters.origin = rv.origin;
    const data = await guard(() => dl("POST", "/api/pending/summary", { filters, skipped: rv.skipped }));
    if (!state.review) return;
    rv.loading = false; rv.data = data || { artists: [], totals: {} };
    draw();
  }
  function reviewCard() {
    const rv = state.review;
    if (rv.loading || !rv.data) return `<div class="group"><div class="dl-loading">正在统计…</div></div>`;
    const rows = rv.data.artists, on = rows.filter((a) => !rv.off.has(a.author_id));
    const sum = (k) => on.reduce((n, a) => n + (a[k] || 0), 0);
    const filterBar = `<div class="dl-rv-filters"><div class="seg multi">${TYPES.map((t) => `<button data-rvtype="${t}" class="${rv.types.has(t) ? "on" : ""}">${TYPE_LABEL[t]}</button>`).join("")}</div>
      <div class="seg">${[["", "全部"], ["new", "新发的"], ["old", "以前漏掉的"]].map(([v, l]) => `<button data-rvorigin="${v}" class="${rv.origin === v ? "on" : ""}">${l}</button>`).join("")}</div>
      <span class="sp"></span><button class="linkbtn" data-dl="rv-all">全选</button><button class="linkbtn" data-dl="rv-none">全不选</button></div>`;
    const tags = (a) => [a.is_new_artist ? `<span class="dl-tag done">新画师</span>` : "", a.manga ? `<span class="dl-tag">漫画 ${fmtNum(a.manga)}</span>` : "", a.ugoira ? `<span class="dl-tag">动图 ${fmtNum(a.ugoira)}</span>` : "",
      a.novel ? `<span class="dl-tag">小说 ${fmtNum(a.novel)}</span>` : "", a.old ? `<span class="dl-tag">旧作品 ${fmtNum(a.old)}</span>` : ""].join("");
    const list = rows.length ? rows.map((a) => `<label class="dl-rv-row ${rv.off.has(a.author_id) ? "off" : ""}"><input type="checkbox" data-rvartist="${a.author_id}" ${rv.off.has(a.author_id) ? "" : "checked"}>
        <span class="nm">${esc(a.name)}</span><span class="tg">${tags(a)}</span><span class="n">${fmtNum(a.works)} 个作品</span><span class="n"><b>${fmtNum(a.files)}</b> 个文件</span><span class="n sz">约 ${fmtSize(a.est_bytes)}</span></label>`).join("")
      : `<div class="dl-empty"><b>${rv.skipped ? "没有被标为“不下载”的文件" : "没有符合条件的待下载文件"}</b></div>`;
    const head = rv.skipped
      ? `<div class="dl-confirm-t">${icon("info")}<b>标为“不下载”的文件</b></div><p>这些文件不会被下载，也不会再出现在待下载里。恢复后会回到待下载。</p>`
      : `<div class="dl-confirm-t">${icon("dl")}<b>待下载的文件</b></div><p>按画师列出。去掉不想要的画师，或用上面的条件筛选，然后只下载选中的部分。大小是按以往文件的平均大小估算的。</p>`;
    const picked = `已选 <b>${fmtNum(on.length)}</b> 位画师 · <b>${fmtNum(sum("files"))}</b> 个文件 · 约 ${fmtSize(sum("est_bytes"))}`;
    const offCount = rows.length - on.length;
    return `<div class="group dl-review">${head}${filterBar}
      <div class="dl-rv-list">${list}</div>
      <div class="dl-actions sticky"><span class="dl-rv-sum">${picked}</span><span class="sp"></span>
        ${rv.skipped ? `<button class="btn primary" data-dl="rv-restore" ${on.length ? "" : "disabled"}>恢复选中的</button>`
          : `${offCount ? `<button class="btn" data-dl="rv-skip" title="没选中的画师的这些文件以后也不下载；可以在这里恢复">把没选中的标为“不下载”</button>` : ""}
             <button class="btn primary" data-dl="rv-download" ${on.length && sum("files") ? "" : "disabled"}>下载选中的</button>`}
        <button class="btn ghost" data-dl="rv-close">关闭</button></div>
    </div>`;
  }
  const reviewFilters = () => {
    const rv = state.review, f = {};
    if (rv.types.size < TYPES.length) f.types = [...rv.types];
    if (rv.origin) f.origin = rv.origin;
    return f;
  };

  // ================================================================ 失败处理页
  async function loadFailed() {
    const [files, artists, avatars] = await Promise.all([dl("GET", "/api/failures"), dl("GET", "/api/sync/failures"),
      dl("POST", "/api/avatars/check", { dry: true }).catch(() => null)]);     // 缺头像的画师：只看头像文件夹，不访问 Pixiv
    state.failures = files; state.syncFails = artists; state.noAvatar = avatars;
    if (state.failList) await loadFailList(state.failList.kind, state.failList.status, state.failList.page);
  }
  async function loadFailList(kind, status = "failed", page = 1) {
    const query = { status, per: 50, page };
    if (kind && status === "failed") query.kind = kind;
    const r = await guard(() => dl("GET", "/api/tasks", null, query));
    state.failList = r ? { kind, status, items: r.items, total: r.total, page } : null;
  }
  function pageFailed() {
    const f = state.failures || { groups: [], ignored: 0 }, s = state.syncFails || { groups: [], total: 0, skipped: [] };
    const fileTotal = f.groups.reduce((n, g) => n + g.count, 0);
    const av = state.noAvatar || { missing: 0, items: [], artists: 0 };
    const tabs = `<div class="dl-tabs"><div class="seg">
      <button data-dl="fail-tab" data-tab="files" class="${state.failTab === "files" ? "on" : ""}">下载失败的文件${fileTotal ? `<span class="n">${fmtNum(fileTotal)}</span>` : ""}</button>
      <button data-dl="fail-tab" data-tab="artists" class="${state.failTab === "artists" ? "on" : ""}">检查失败的画师${s.total ? `<span class="n">${fmtNum(s.total)}</span>` : ""}</button>
      <button data-dl="fail-tab" data-tab="avatars" class="${state.failTab === "avatars" ? "on" : ""}">缺头像的画师${av.missing ? `<span class="n">${fmtNum(av.missing)}</span>` : ""}</button></div></div>`;
    return tabs + (state.failTab === "artists" ? artistsTab(s) : state.failTab === "avatars" ? avatarsTab(av) : filesTab(f, fileTotal));
  }
  function taskRows(list) {
    if (!list.items.length) return `<div class="dl-empty"><b>没有了</b></div>`;
    const pages = Math.ceil(list.total / 50);
    return `<div class="dl-tasklist">${list.items.map((t) => {
      const a = viewerArtist(t.author_id);
      return `<div class="dl-task"><div class="t"><b>${esc(t.title || t.task_key)}</b><span class="by">${esc(t.author_name || (t.author_id ? "画师 " + t.author_id : ""))} · 第 ${(+t.page_index || 0) + 1} 页${t.attempts ? ` · 试过 ${t.attempts} 次` : ""}</span>
          ${t.last_error ? `<small class="mono">${esc(String(t.last_error).slice(0, 160))}</small>` : ""}</div>
        <div class="ctl">${list.status === "failed"
          ? `<button class="btn" data-dl="task-retry" data-key="${esc(t.task_key)}" ${running() ? "disabled" : ""}>重试</button><button class="btn ghost" data-dl="task-ignore" data-key="${esc(t.task_key)}">忽略</button>`
          : `<button class="btn" data-dl="task-restore" data-key="${esc(t.task_key)}">恢复</button>`}
          <button class="btn ghost" data-dl="open-url" data-url="${pixivWork(t.illust_id)}" title="在浏览器里打开这个作品，看看它还在不在">Pixiv</button>
          ${a ? `<button class="btn ghost" data-dl="locate" data-key="${esc(a.key)}" title="在图库里打开这位画师">画师</button>` : ""}</div></div>`;
    }).join("")}</div>
      ${pages > 1 ? `<div class="dl-pager"><button class="btn ghost" data-dl="list-page" data-page="${list.page - 1}" ${list.page <= 1 ? "disabled" : ""}>上一页</button><span>${list.page} / ${pages}</span><button class="btn ghost" data-dl="list-page" data-page="${list.page + 1}" ${list.page >= pages ? "disabled" : ""}>下一页</button></div>` : ""}`;
  }
  function filesTab(f, total) {
    const open = state.failList;
    if (!f.groups.length && !(open && open.status === "ignored")) {
      return `<div class="dl-empty">${icon("check")}<b>没有下载失败的文件</b>${f.ignored ? `<span>有 ${fmtNum(f.ignored)} 个已忽略的文件</span><button class="btn" data-dl="list-open" data-status="ignored">查看</button><button class="btn" data-dl="restore">全部恢复</button>` : ""}</div>`;
    }
    const retryable = f.groups.filter((g) => (FAIL_KINDS[g.kind] || FAIL_KINDS.other)[2]);
    return `${f.groups.length ? `<div class="group"><div class="set"><div class="t">共 ${fmtNum(total)} 个文件下载失败<small>按原因分组，点“查看”可以逐个处理。“马上重试”会立刻只下载这些文件；“放回队列”则等下次下载时再试。</small></div>
        <div class="ctl">${retryable.length ? `<button class="btn primary" data-dl="retry-now" data-kinds="${esc(retryable.map((g) => g.kind).join(","))}" ${running() ? "disabled" : ""}>全部马上重试</button>` : ""}
          <button class="btn ghost" data-dl="export-failed">导出清单</button></div></div></div>` : ""}
      <div class="group">${f.groups.map((g) => {
        const [label, hint] = FAIL_KINDS[g.kind] || FAIL_KINDS.other;
        const isOpen = open && open.status === "failed" && open.kind === g.kind;
        return `<div class="set dl-fail"><div class="t"><span class="dl-fail-t">${esc(label)}<b>${fmtNum(g.count)}</b></span><small>${esc(hint)}${g.sample && !isOpen ? `<br><span class="mono">${esc(String(g.sample).slice(0, 140))}</span>` : ""}</small></div>
          <div class="ctl"><button class="btn" data-dl="retry-now" data-kinds="${esc(g.kind)}" ${running() ? "disabled" : ""}>马上重试</button><button class="btn ghost" data-dl="retry" data-kinds="${esc(g.kind)}">放回队列</button>
          <button class="btn ghost" data-dl="ignore" data-kinds="${esc(g.kind)}">忽略</button><button class="btn ghost" data-dl="${isOpen ? "list-close" : "list-open"}" data-kind="${esc(g.kind)}" data-status="failed">${isOpen ? "收起" : "查看"}</button>
          ${g.kind === "auth" ? `<button class="btn ghost" data-dlgo="accounts">去处理账号</button>` : g.kind === "storage" ? `<button class="btn ghost" data-dlgo="storage">检查保存位置</button>` : ""}</div></div>
          ${isOpen ? taskRows(open) : ""}`;
      }).join("")}</div>
      ${f.ignored ? `<div class="group"><div class="set"><div class="t">已忽略 ${fmtNum(f.ignored)} 个<small>忽略的文件不会再自动重试，也不计入失败。</small></div><div class="ctl">
        <button class="btn ghost" data-dl="${open && open.status === "ignored" ? "list-close" : "list-open"}" data-status="ignored">${open && open.status === "ignored" ? "收起" : "查看"}</button><button class="btn" data-dl="restore">全部恢复</button></div></div>
        ${open && open.status === "ignored" ? taskRows(open) : ""}</div>` : ""}`;
  }
  // 缺头像的画师：补全没成功的、导入数据后还没补的都在这里，随时能看到（不依赖上一次任务的结果）
  function avatarsTab(av) {
    if (!av.missing) return `<div class="dl-empty">${icon("check")}<b>所有画师都有头像</b><span>哪位画师的头像没下载下来，会列在这里。</span></div>`;
    return `<div class="group"><div class="set"><div class="t">${fmtNum(av.missing)} 位画师还没有头像<small>“补全”会把这些头像下回来；有的画师已经注销或换了地址，可能一直补不上。</small></div>
        <div class="ctl"><button class="btn primary" data-dl="fill-avatars" ${running() ? "disabled" : ""}>补全…</button></div></div></div>
      <div class="group"><div class="dl-tasklist">${av.items.map((x) => `<div class="dl-task"><div class="t"><b>${esc(x.name)}</b><span class="by">ID ${x.id}</span></div>
        <div class="ctl"><button class="btn ghost" data-dl="open-url" data-url="${pixivUser(x.id)}" title="在浏览器里打开这位画师的主页">Pixiv</button></div></div>`).join("")}
        ${av.missing > av.items.length ? `<div class="dl-task"><div class="t"><span class="by">还有 ${fmtNum(av.missing - av.items.length)} 位没有列出</span></div></div>` : ""}</div></div>`;
  }
  function artistsTab(s) {
    if (!s.groups.length && !s.skipped.length) return `<div class="dl-empty">${icon("check")}<b>没有检查失败的画师</b><span>检查时某位画师没查成功，会列在这里，可以单独重查。</span></div>`;
    const retryIds = s.groups.filter((g) => g.kind !== "gone").flatMap((g) => g.items.map((x) => x.author_id));
    const row = (x, kind) => `<div class="dl-task"><div class="t"><b>${esc(x.name || "画师 " + x.author_id)}</b><span class="by">ID ${x.author_id}${x.count > 1 ? ` · 连续失败 ${x.count} 次` : ""}${x.time ? ` · ${fmtWhen(x.time)}` : ""}</span>
        ${x.error ? `<small class="mono">${esc(String(x.error).slice(0, 160))}</small>` : ""}</div>
      <div class="ctl">${kind === "skipped" ? `<button class="btn" data-dl="artist-unskip" data-id="${x.author_id}">恢复检查</button>`
        : `<button class="btn" data-dl="artist-recheck" data-id="${x.author_id}" ${running() ? "disabled" : ""}>重查</button><button class="btn ghost" data-dl="artist-skip" data-ids="${x.author_id}">不再检查</button>`}
        <button class="btn ghost" data-dl="open-url" data-url="${pixivUser(x.author_id)}" title="在浏览器里打开这位画师的主页">Pixiv</button></div></div>`;
    return `${retryIds.length ? `<div class="group"><div class="set"><div class="t">${fmtNum(retryIds.length)} 位画师上次检查没有成功<small>这些画师的检查进度没有变，重查时会从原来的地方接着查，不会漏作品。</small></div>
        <div class="ctl"><button class="btn primary" data-dl="ask-failed" ${running() ? "disabled" : ""}>全部重查</button></div></div></div>` : ""}
      <div class="group">${s.groups.map((g) => {
        const [label, hint] = SYNC_FAIL[g.kind] || SYNC_FAIL.other, isOpen = state.openGroups.has(g.kind) || g.count <= 6;
        return `<div class="set dl-fail"><div class="t"><span class="dl-fail-t">${esc(label)}<b>${fmtNum(g.count)}</b></span><small>${esc(hint)}</small></div>
          <div class="ctl">${g.kind === "gone" ? `<button class="btn" data-dl="ask-gone" ${running() ? "disabled" : ""}>复核一遍</button>` : ""}
            <button class="btn ghost" data-dl="artist-skip" data-ids="${g.items.map((x) => x.author_id).join(",")}">都不再检查</button>
            ${g.count > 6 ? `<button class="btn ghost" data-dl="group-toggle" data-kind="${g.kind}">${isOpen ? "收起" : "查看"}</button>` : ""}</div></div>
          ${isOpen ? `<div class="dl-tasklist">${g.items.map((x) => row(x, g.kind)).join("")}</div>` : ""}`;
      }).join("")}</div>
      ${s.skipped.length ? `<div class="group"><div class="set"><div class="t">不再检查的画师 ${fmtNum(s.skipped.length)} 位<small>检查更新时会跳过他们。已经下载的作品不受影响。</small></div>
        <div class="ctl"><button class="btn ghost" data-dl="group-toggle" data-kind="skipped">${state.openGroups.has("skipped") ? "收起" : "查看"}</button></div></div>
        ${state.openGroups.has("skipped") ? `<div class="dl-tasklist">${s.skipped.map((x) => row(x, "skipped")).join("")}</div>` : ""}</div>` : ""}`;
  }

  // ================================================================ 点击
  async function ask(kind, patch, extra) {
    if (state.page !== "update") await go("update");
    state.review = null;
    state.confirm = { kind, ...(extra || {}) };
    state.optsOpen = !!patch;
    state.opts = { ...defaults(), ...(patch || {}) };
    if (!state.accounts) dl("GET", "/api/accounts").then((r) => { state.accounts = r.items; if (state.confirm) draw(); }).catch(() => {});
    draw();
  }
  async function reload() { state.plan = null; return go(state.page); }
  /** 处理这两页上的按钮。处理了返回 true。 */
  async function click(act, b) {
    // ---- 更新
    if (act === "ask") { await ask(b.dataset.kind); return true; }
    if (act === "ask-gone") {
      await ask("recheck_gone", null, { simple: true, title: "复核已注销的画师", text: "把标记为“已注销或不存在”的画师逐个向 Pixiv 再确认一遍。还在的会恢复正常，以后照常检查。" });
      return true;
    }
    if (act === "ask-resume" || act === "ask-failed") {
      const since = ((state.plan || {}).resume || {}).since || "";
      await ask("sync_download", act === "ask-failed" ? { scope: "failed" } : { resume: since });
      return true;
    }
    if (act === "opts") { state.optsOpen = !state.optsOpen; draw(); return true; }
    if (act === "cancel-ask") { state.confirm = null; draw(); return true; }
    if (act === "start") {
      const c = state.confirm, p = buildParams(c);
      if (p.error) { toast(p.error); return true; }
      await startJob(c.kind, p); return true;
    }
    if (act === "stop") { await guard(() => dl("POST", "/api/job/stop")); onJob(await dl("GET", "/api/job")); draw(); return true; }
    if (act === "pause" || act === "resume") { await guard(() => dl("POST", "/api/job/" + act)); onJob(await dl("GET", "/api/job")); draw(); return true; }
    if (act === "logs") { state.logsOpen = !state.logsOpen; if (state.logsOpen) await fetchLogs(); draw(); return true; }
    if (act === "dismiss") { state.dismissed = state.job.id; draw(); return true; }
    // ---- 先看再下
    if (act === "review") { const skipped = b.dataset.skipped === "1"; if (state.page !== "update") await go("update"); await openReview(skipped); return true; }
    if (act === "rv-close") { state.review = null; draw(); return true; }
    if (act === "rv-all" || act === "rv-none") { const rv = state.review; rv.off = new Set(act === "rv-all" ? [] : rv.data.artists.map((a) => a.author_id)); draw(); return true; }
    if (act === "rv-download") {
      const rv = state.review, on = rv.data.artists.filter((a) => !rv.off.has(a.author_id)).map((a) => a.author_id), f = reviewFilters();
      state.review = null;
      await startJob("download", { author_ids: on, ...(f.types ? { types: f.types } : {}), ...(f.origin ? { origin: f.origin } : {}) });
      return true;
    }
    if (act === "rv-skip" || act === "rv-restore") {
      const rv = state.review, restore = act === "rv-restore";
      const ids = rv.data.artists.filter((a) => (restore ? !rv.off.has(a.author_id) : rv.off.has(a.author_id))).map((a) => a.author_id);
      const r = await guard(() => dl("POST", "/api/pending/skip", { filters: { ...reviewFilters(), author_ids: ids }, restore }));
      if (r) toast(restore ? `已恢复 ${fmtNum(r.count)} 个文件到待下载` : `已把 ${fmtNum(r.count)} 个文件标为“不下载”`);
      rv.off = new Set(); state.plan = await guard(() => dl("GET", "/api/plan")) || state.plan;
      await loadReview(); return true;
    }
    // ---- 失败处理：文件
    if (act === "fail-tab") { state.failTab = b.dataset.tab; state.failList = null; if (state.page !== "failed") await go("failed"); else draw(); return true; }
    if (act === "retry" || act === "ignore") {
      const kinds = b.dataset.kinds.split(",");
      const r = await guard(() => dl("POST", act === "retry" ? "/api/tasks/retry" : "/api/tasks/ignore", { kinds }));
      if (r) toast(act === "retry" ? `已把 ${fmtNum(r.count)} 个文件放回待下载` : `已忽略 ${fmtNum(r.count)} 个文件`);
      state.failList = null; await reload(); return true;
    }
    if (act === "retry-now") { await startJob("retry_now", { kinds: b.dataset.kinds.split(",") }); return true; }
    if (act === "restore") {
      const r = await guard(() => dl("POST", "/api/tasks/ignore", { kinds: Object.keys(FAIL_KINDS), restore: true }));
      if (r) toast(`已恢复 ${fmtNum(r.count)} 个文件`);
      state.failList = null; await reload(); return true;
    }
    if (act === "list-open") { await loadFailList(b.dataset.kind || "", b.dataset.status || "failed", 1); draw(); return true; }
    if (act === "list-close") { state.failList = null; draw(); return true; }
    if (act === "list-page") { const l = state.failList; await loadFailList(l.kind, l.status, +b.dataset.page); draw(false); return true; }
    if (act === "task-retry") { await startJob("retry_now", { keys: [b.dataset.key] }); return true; }
    if (act === "task-ignore" || act === "task-restore") {
      const r = await guard(() => dl("POST", "/api/tasks/ignore", { keys: [b.dataset.key], restore: act === "task-restore" }));
      if (r) toast(act === "task-restore" ? "已恢复，可以重试了" : "已忽略");
      state.failures = await dl("GET", "/api/failures"); const l = state.failList; if (l) await loadFailList(l.kind, l.status, l.page); state.plan = null; draw(); return true;
    }
    if (act === "export-failed") {
      if (!api.saveText) { toast("这个环境里不能导出文件"); return true; }
      const r = await guard(() => dl("GET", "/api/failures/export"));
      if (r) { const saved = await api.saveText("下载失败的文件.csv", r.csv); toast(saved ? `已导出 ${fmtNum(r.count)} 条到 ${saved}` : "已取消"); }
      return true;
    }
    if (act === "open-url") { api.openUrl(b.dataset.url); return true; }
    if (act === "locate") { ctx.closeDialog(); ctx.openScope && ctx.openScope("artist", b.dataset.key); return true; }
    // ---- 失败处理：画师
    if (act === "group-toggle") { const k = b.dataset.kind; state.openGroups.has(k) ? state.openGroups.delete(k) : state.openGroups.add(k); draw(); return true; }
    if (act === "artist-recheck") { await startJob("sync_download_artists", { author_ids: [+b.dataset.id] }); return true; }
    if (act === "artist-skip" || act === "artist-unskip") {
      const ids = (b.dataset.ids || b.dataset.id).split(",").map(Number);
      if (act === "artist-skip" && ids.length > 1 && b.dataset.sure !== "1") { b.dataset.sure = "1"; b.textContent = `确定 ${ids.length} 位都不再检查？`; b.classList.add("danger"); return true; }
      const r = await guard(() => dl("POST", "/api/sync/skip", { author_ids: ids, restore: act === "artist-unskip" }));
      if (r) toast(act === "artist-unskip" ? "已恢复检查" : `${fmtNum(r.count)} 位画师以后不再检查`);
      state.syncFails = await dl("GET", "/api/sync/failures"); state.plan = null; draw(); return true;
    }
    return false;
  }
  /** 选项面板和清单里的输入。处理了返回 true。 */
  function change(e) {
    const t = e.target;
    if (t.dataset.after !== undefined && "after" in t.dataset) {
      state.after = t.value;
      if (running() && api.afterJobSet) api.afterJobSet(state.after, (ctx.S && ctx.S.afterCommand) || "");
      draw(); return true;
    }
    if (t.dataset.opt && state.opts) {
      state.opts[t.dataset.opt] = t.value;
      if (t.dataset.opt === "scope") { if (t.value === "folder" && !state.opts.folder && folders().length) state.opts.folder = folders()[0].id; state.opts.resume = ""; draw(); }
      return true;
    }
    if (t.dataset.rvartist && state.review) {
      const id = +t.dataset.rvartist;
      t.checked ? state.review.off.delete(id) : state.review.off.add(id);
      draw(); return true;
    }
    return false;
  }
  function toggle(t) {
    const sw = t.closest("[data-optsw]");
    if (sw && state.opts) { state.opts[sw.dataset.optsw] = !state.opts[sw.dataset.optsw]; draw(); return true; }
    const ty = t.closest("[data-opttype]");
    if (ty && state.opts) { const s = state.opts.types; s.has(ty.dataset.opttype) ? s.delete(ty.dataset.opttype) : s.add(ty.dataset.opttype); draw(); return true; }
    const acc = t.closest("[data-optacc]");
    if (acc && state.opts) {
      const o = state.opts;
      if (!o.accounts) o.accounts = new Set((state.accounts || []).filter((a) => a.is_valid).map((a) => a.name));
      o.accounts.has(acc.dataset.optacc) ? o.accounts.delete(acc.dataset.optacc) : o.accounts.add(acc.dataset.optacc);
      draw(); return true;
    }
    const rt = t.closest("[data-rvtype]");
    if (rt && state.review) { const s = state.review.types; s.has(rt.dataset.rvtype) ? s.delete(rt.dataset.rvtype) : s.add(rt.dataset.rvtype); if (!s.size) TYPES.forEach((x) => s.add(x)); state.review.off = new Set(); loadReview(); return true; }
    const ro = t.closest("[data-rvorigin]");
    if (ro && state.review) { state.review.origin = ro.dataset.rvorigin; state.review.off = new Set(); loadReview(); return true; }
    return false;
  }
  return { pageUpdate, pageFailed, loadFailed, click, change, toggle, askKind: ask };
}

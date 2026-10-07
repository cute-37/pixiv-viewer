// 示例后端：没有 Python 后端时（浏览器里预览）使用。接口与 api.js 中的真实后端一致。
// 图片用 canvas 现场画，画师与作品都是虚构的。

function rng(seed) {
  return () => { seed |= 0; seed = seed + 0x6D2B79F5 | 0; let t = Math.imul(seed ^ seed >>> 15, 1 | seed); t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t; return ((t ^ t >>> 14) >>> 0) / 4294967296; };
}
const PALETTES = [
  ["#ffb4a2", "#e5989b", "#6d6875", "#ffd6a5", "#fff1e6"], ["#0b132b", "#1c2541", "#3a506b", "#5bc0be", "#f4d35e"],
  ["#a8dadc", "#457b9d", "#1d3557", "#f1faee", "#e63946"], ["#ffcad4", "#f4acb7", "#9d8189", "#d8e2dc", "#ffe5d9"],
  ["#22223b", "#4a4e69", "#9a8c98", "#c9ada7", "#f2e9e4"], ["#2b2d42", "#8d99ae", "#edf2f4", "#ef233c", "#ffd166"],
  ["#ccd5ae", "#e9edc9", "#fefae0", "#faedcd", "#d4a373"], ["#03045e", "#0077b6", "#00b4d8", "#90e0ef", "#caf0f8"],
  ["#3d0066", "#7b2cbf", "#c77dff", "#e0aaff", "#ffd6ff"], ["#264653", "#2a9d8f", "#e9c46a", "#f4a261", "#e76f51"],
];
const artCache = new Map();
function art(seed, ar, W) {
  const key = seed + ":" + W;
  if (artCache.has(key)) return artCache.get(key);
  const r = rng(seed), H = Math.round(W / ar), s = W / 300;
  const c = document.createElement("canvas"); c.width = W; c.height = H;
  const g = c.getContext("2d"), P = PALETTES[seed % PALETTES.length], kind = seed % 3;
  const sky = g.createLinearGradient(0, 0, 0, H);
  sky.addColorStop(0, kind === 1 ? P[0] : P[4]); sky.addColorStop(1, kind === 1 ? P[2] : P[1]);
  g.fillStyle = sky; g.fillRect(0, 0, W, H);
  if (kind === 1) for (let i = 0; i < 90; i++) { g.fillStyle = `rgba(255,255,255,${0.3 + r() * 0.7})`; g.fillRect(r() * W, r() * H * 0.7, 1.4 * s, 1.4 * s); }
  g.save(); g.shadowColor = P[3]; g.shadowBlur = 40 * s; g.fillStyle = kind === 1 ? "#fdf6d8" : P[3];
  g.beginPath(); g.arc(W * (0.25 + r() * 0.5), H * (0.18 + r() * 0.25), W * (0.08 + r() * 0.1), 0, Math.PI * 2); g.fill(); g.restore();
  for (let layer = 0; layer < 3; layer++) {
    g.fillStyle = P[(layer + 1) % 5]; g.globalAlpha = 0.55 + layer * 0.2;
    const base = H * (0.55 + layer * 0.13);
    g.beginPath(); g.moveTo(0, H);
    for (let x = 0; x <= W; x += 10 * s) g.lineTo(x, base - Math.sin(x / s / (40 + layer * 25) + seed) * (16 + layer * 6) * s - r() * 4 * s);
    g.lineTo(W, H); g.fill();
  }
  g.globalAlpha = 1;
  if (kind === 2) {
    g.fillStyle = P[2]; const cx = W * (0.4 + r() * 0.2);
    g.beginPath(); g.ellipse(cx, H * 0.52, W * 0.07, W * 0.08, 0, 0, Math.PI * 2); g.fill();
    g.beginPath(); g.moveTo(cx - W * 0.16, H); g.quadraticCurveTo(cx, H * 0.52, cx + W * 0.16, H); g.fill();
  }
  const url = c.toDataURL("image/jpeg", 0.86);
  artCache.set(key, url);
  return url;
}

const ARTISTS = [
  ["あおい凪", 30418271, "#e5989b"], ["白川ルカ", 11904356, "#457b9d"], ["霧島 透", 58233190, "#6d6875"],
  ["mizuna", 4471023, "#2a9d8f"], ["七瀬ゆき", 27765001, "#c77dff"], ["Ren Hoshino", 16602318, "#e76f51"],
  ["綾瀬みお", 9821370, "#0077b6"], ["柚木", 40255874, "#d4a373"], ["KAITO_art", 2183477, "#4a4e69"],
  ["sora", 61130926, "#ef233c"], ["月見里", 7745120, "#3a506b"], ["ぽぷら", 22019384, "#ffb4a2"],
].map(([name, id, color], i) => ({ key: `[${id}] ${name}`, id, name, color, folder: `D:\\Pixiv\\[${id}] ${name}`, pinned: i < 2, avatar: null }));

const TAG_POOL = ["オリジナル", "女の子", "風景", "創作", "空", "星空", "夏", "制服", "ファンタジー", "猫", "少女", "海",
  "花", "夜景", "和服", "眼鏡", "ロングヘア", "ショートヘア", "青空", "雨", "雪", "桜", "紅葉", "夕焼け", "街", "背景",
  "建物", "光", "透明感", "水彩", "厚塗り", "落書き", "原神", "崩壊：スターレイル", "ブルーアーカイブ", "初音ミク",
  "VOCALOID", "東方Project", "ホロライブ", "アークナイツ", "ウマ娘", "獣耳", "メイド", "セーラー服", "パーカー",
  "ヘッドホン", "リボン", "猫耳", "笑顔", "横顔", "後ろ姿", "見返り", "百合", "男の子", "ドラゴン", "魔法少女", "天使",
  "サイバーパンク", "メカ", "宇宙", "月", "花火", "浴衣", "祭り", "教室", "放課後", "電車", "カフェ", "本", "うさぎ",
  "金魚", "ひまわり", "あじさい", "クリスマス", "ハロウィン", "誕生日", "pixivファンタジア", "創作百合", "漫画",
  "モノクロ", "線画", "ドット絵", "ポートレート", "静物"];
const TITLES = ["夏の終わりに", "星を数える夜", "放課後の空", "雨上がり", "遠い街", "ひかりの庭", "旅立ち", "夜明け前",
  "青の記憶", "風の通り道", "海辺の午後", "ねむれない夜", "春をまつ", "花火のあと", "雪の音", "ひとやすみ"];
const CAPTIONS = ["夏の終わりの夕方。背景は海辺の小さな町を参考にしました。", "久しぶりの風景メインです。光の表現を練習中。",
  "オリジナルの子。設定は追々…\nTwitter: https://twitter.com/example", "", "依頼で描かせていただきました。ありがとうございました！"];
const ARS = [0.70, 0.75, 0.71, 0.8, 0.67, 1, 0.75, 1.41, 0.7, 0.56, 0.75, 1.33, 0.71, 0.8, 0.62];

function pickTags(i) {
  const r = rng(i * 131 + 17), out = new Set();
  const n = 6 + Math.floor(r() * 5);
  while (out.size < n) out.add(TAG_POOL[Math.floor(Math.pow(r(), 2.2) * TAG_POOL.length)]);
  return [...out];
}

const NOW = 1759450000;
const WORKS = [];
let offline = new URLSearchParams(location.search).has("offline");     // ?offline=1：图库所在的共享没连上
const COUNT = Math.min(20000, Math.max(30, +(new URLSearchParams(location.search).get("works")) || 150));
for (let i = 0; i < COUNT; i++) {
  const a = ARTISTS[(i * 7) % ARTISTS.length], ar = ARS[i % ARS.length], r = rng(i * 977 + 5);
  const pid = 131500000 - i * 41873;
  const pageCount = r() < 0.18 ? 2 + Math.floor(r() * 6) : 1;
  const posted = NOW - Math.floor(i * 1.25 * 86400 + r() * 86400);
  const w = ar >= 1 ? 3000 : Math.round(3000 * ar), h = ar >= 1 ? Math.round(3000 / ar) : 3000;
  const pages = Array.from({ length: pageCount }, (_, p) => ({
    path: `${a.folder}\\${pid}_p${p}.${i % 4 ? "jpg" : "png"}`, file: `${pid}_p${p}.${i % 4 ? "jpg" : "png"}`,
    w, h, size: Math.round((1.2 + r() * 4.5) * 1048576), seed: i * 31 + p * 7 + 3,
  }));
  const d = new Date(posted * 1000);
  WORKS.push({
    key: String(pid), pid, title: TITLES[i % TITLES.length], artistKey: a.key, artistName: a.name, artistId: a.id,
    pages, w, h, ar, posted, mtime: posted + 3600 * (i % 40), month: `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`,
    rating: i % 9 === 4 ? "r18" : "safe", ai: i % 13 === 6, tags: pickTags(i), myTags: i % 6 === 0 ? ["壁纸候选"] : [],
    fav: i % 7 === 2, stars: i % 5 === 1 ? 4 : i % 11 === 3 ? 5 : 0, caption: CAPTIONS[i % CAPTIONS.length], viewed: i < 20 ? NOW - i * 3000 : 0,
  });
}
const byPath = new Map();
for (const w of WORKS) for (const p of w.pages) byPath.set(p.path, { w, p });

let config = null;
try { config = JSON.parse(localStorage.getItem("pv-web-settings") || "null"); } catch (e) { config = null; }

// 列表里只放占位地址，真正的图片在缩略图进入可视范围时才画（与桌面版按需下载的行为一致）
function thumbOf(p, ar) { return `mock:${p.seed}:${ar}`; }
function publicWork(w) {
  return {
    key: w.key, pid: w.pid, title: w.title, artistKey: w.artistKey, artistName: w.artistName, artistId: w.artistId,
    w: w.w, h: w.h, ar: w.ar, posted: w.posted, mtime: w.mtime, month: w.month, rating: w.rating, ai: w.ai,
    tags: w.tags, fav: w.fav, stars: w.stars,
    pages: w.pages.map((p) => ({ path: p.path, file: p.file, w: p.w, h: p.h, size: p.size, thumb: thumbOf(p, w.ar) })),
  };
}
function flattenPages(list) {
  // 不合并多页时，每一页都是一张独立卡片
  return list.flatMap((w) => w.pages.map((p, k) => ({ ...w, key: `${w.key}#${k}`, pages: [p] })));
}

// 示例的画师文件夹
let folderSeq = 2;
const FOLDERS = [{ id: 1, name: "风景", artists: ARTISTS.slice(0, 3).map((a) => a.key) }, { id: 2, name: "常看", artists: ARTISTS.slice(4, 6).map((a) => a.key) }];

let updState = { state: "idle", done: 0, total: 0, error: "" };
let mockLogin = null;
let mockAfter = null;
let mockCacheSize = 214 * 1048576;

export const mock = {
  isMock: true,
  async getConfig() { return config; },
  async saveConfig(partial) {
    config = { ...(config || {}), ...partial };
    try { localStorage.setItem("pv-web-settings", JSON.stringify(config)); } catch (e) { /* 浏览器不允许存储时忽略 */ }
  },
  async getLibrary() {
    const counts = new Map(), updated = new Map();
    for (const w of WORKS) { counts.set(w.artistKey, (counts.get(w.artistKey) || 0) + w.pages.length); updated.set(w.artistKey, Math.max(updated.get(w.artistKey) || 0, w.posted)); }
    return {
      roots: offline ? [{ path: "\\\\NAS\\pixiv", name: "pixiv", count: 0, offline: true, error: "用户名或密码不正确。", auto: true }] : [{ path: "D:\\Pixiv", name: "Pixiv", count: WORKS.reduce((n, w) => n + w.pages.length, 0) }, { path: "\\\\NAS\\pixiv-archive", name: "pixiv-archive", count: 0, offline: true }],
      artists: offline ? [] : ARTISTS.map((a) => ({ ...a, count: counts.get(a.key) || 0, updated: updated.get(a.key) || 0 })),
      totals: { images: WORKS.reduce((n, w) => n + w.pages.length, 0), works: WORKS.length, fav: WORKS.filter((w) => w.fav).length, recent: WORKS.filter((w) => w.viewed).length },
      metadata: { path: "D:\\Pixiv\\db\\pixiv.db", ok: true },
      cache: { size: mockCacheSize, maxBytes: 500 * 1048576, maxAgeDays: 90 },
      folders: FOLDERS.map((f) => ({ ...f, artists: [...f.artists] })),
      demo: true,
      version: "预览",
    };
  },
  async listWorks(q) {
    let list = WORKS.filter((w) => {
      if (offline) return false;
      if (q.scope === "artist" && w.artistKey !== q.artist) return false;
      if (q.scope === "folder" && !(FOLDERS.find((f) => String(f.id) === String(q.folder))?.artists || []).includes(w.artistKey)) return false;
      if (q.scope === "fav" && !w.fav) return false;
      if (q.scope === "recent" && !w.viewed) return false;
      return true;
    });
    const scopeTags = new Map();
    for (const w of list) for (const t of w.tags) scopeTags.set(t, (scopeTags.get(t) || 0) + 1);
    const text = (q.q || "").trim().toLowerCase();
    const f = q.filters || {};
    list = list.filter((w) => {
      if (q.rating === "safe" && w.rating !== "safe") return false;
      if (q.rating === "r18" && w.rating === "safe") return false;
      if (q.tags && q.tags.length && !q.tags.every((t) => w.tags.includes(t) || w.myTags.includes(t))) return false;
      if (text && !(w.title.toLowerCase().includes(text) || w.artistName.toLowerCase().includes(text) || String(w.pid).includes(text)
        || String(w.artistId).includes(text) || w.tags.some((t) => t.toLowerCase().includes(text)))) return false;
      if (f.ai === "exclude" && w.ai) return false;
      if (f.ai === "only" && !w.ai) return false;
      if (f.orientation === "portrait" && w.ar >= 1) return false;
      if (f.orientation === "landscape" && w.ar <= 1) return false;
      if (f.orientation === "square" && Math.abs(w.ar - 1) > 0.05) return false;
      if (f.minStars && w.stars < f.minStars) return false;
      if (f.multiPage && w.pages.length < 2) return false;
      return true;
    });
    if (q.sort === "id") list.sort((a, b) => b.pid - a.pid);
    else if (q.sort === "time") list.sort((a, b) => b.mtime - a.mtime);
    else if (q.sort === "name") list.sort((a, b) => a.pages[0].file.localeCompare(b.pages[0].file));
    if (q.scope === "recent") list.sort((a, b) => b.viewed - a.viewed);
    let out = list.map(publicWork);
    if (!q.mergePages) out = flattenPages(out);
    return { works: out, scopeTags: [...scopeTags.entries()].sort((a, b) => b[1] - a[1]) };
  },
  async suggest(text, safeOnly) {
    const t = text.trim().toLowerCase();
    const WORKS_ = safeOnly ? WORKS_.filter((w) => w.rating === "safe") : WORKS;
    if (!t) return { artists: [], tags: [], works: [] };
    const artists = ARTISTS.filter((a) => a.name.toLowerCase().includes(t) || String(a.id).includes(t)).slice(0, 5);
    const tagCounts = new Map();
    for (const w of WORKS_) for (const tg of w.tags) if (tg.toLowerCase().includes(t)) tagCounts.set(tg, (tagCounts.get(tg) || 0) + 1);
    const tags = [...tagCounts.entries()].sort((a, b) => b[1] - a[1]).slice(0, 6);
    const works = WORKS_.filter((w) => String(w.pid).includes(t) || w.title.toLowerCase().includes(t)).slice(0, 5).map(publicWork);
    return { artists, tags, works };
  },
  thumbSrc(src) {
    if (!String(src).startsWith("mock:")) return src;
    const [, seed, ar] = src.split(":");
    return art(+seed, +ar, 360);
  },
  imageUrl(path) {
    const hit = byPath.get(path);
    return hit ? art(hit.p.seed, hit.w.ar, 1100) : "";
  },
  async getDetails(key) {
    const w = WORKS.find((x) => x.key === key.split("#")[0]);
    if (!w) return null;
    return { title: w.title, caption: w.caption, tags: w.tags, myTags: w.myTags, pid: w.pid, url: `https://www.pixiv.net/artworks/${w.pid}`,
      artistUrl: `https://www.pixiv.net/users/${w.artistId}`, posted: w.posted, bookmarks: 120 + (w.pid % 900), views: 2000 + (w.pid % 9000) };
  },
  async setStars(keys, n) { for (const k of keys) { const w = WORKS.find((x) => x.key === k.split("#")[0]); if (w) w.stars = n; } },
  async setFavorite(keys, on) { for (const k of keys) { const w = WORKS.find((x) => x.key === k.split("#")[0]); if (w) w.fav = on; } },
  async addTags(keys, tags) { for (const k of keys) { const w = WORKS.find((x) => x.key === k.split("#")[0]); if (w) for (const t of tags) if (!w.myTags.includes(t)) w.myTags.push(t); } },
  async removeTags(keys, tags) { for (const k of keys) { const w = WORKS.find((x) => x.key === k.split("#")[0]); if (w) w.myTags = w.myTags.filter((t) => !tags.includes(t)); } },
  async markViewed(key) { const w = WORKS.find((x) => x.key === key.split("#")[0]); if (w) w.viewed = Date.now() / 1000; },
  async myTagList() { const s = new Set(); WORKS.forEach((w) => w.myTags.forEach((t) => s.add(t))); return [...s, "参考", "头像候选", "待整理"]; },
  async folderCreate(name, artists) { const id = ++folderSeq; FOLDERS.push({ id, name, artists: [...(artists || [])] }); return id; },
  async folderRename(id, name) { const f = FOLDERS.find((x) => x.id === +id); if (f) f.name = name; return true; },
  async folderDelete(id) { const i = FOLDERS.findIndex((x) => x.id === +id); if (i >= 0) FOLDERS.splice(i, 1); return true; },
  async folderSet(id, artists, on) {
    const f = FOLDERS.find((x) => x.id === +id); if (!f) return false;
    for (const a of artists) { const i = f.artists.indexOf(a); if (on && i < 0) f.artists.push(a); if (!on && i >= 0) f.artists.splice(i, 1); }
    return true;
  },
  async pinArtist(key, on) { const a = ARTISTS.find((x) => x.key === key); if (a) a.pinned = on; },
  // 以下是只在桌面版有效果的系统操作；预览里给出提示即可
  async addFolder() { return null; },
  async removeFolder() { return true; },
  async reveal() { return "preview"; },
  async openExternal() { return "preview"; },
  // 预览版里没有真的窗口。地址里带 ?desktop 时装作有（显示窗口按钮），用来演示和测试“关闭时询问”
  ...(new URLSearchParams(location.search).has("desktop") ? { async windowAction(action) { if (action === "tray" || action === "close") window.__pvLastWindowAction = action; return false; } } : {}),
  async saveText(name) { return "D:\\导出\\" + name; },
  async clearCache() { const freed = mockCacheSize; mockCacheSize = 0; return { removed: 4321, freed }; },
  // 任务结束后的动作：预览版里只演示倒计时，不会真的关机
  async jobWatch(o) { mockAfter = { action: (o && o.action) || "none", state: "watching", until: 0 }; return { ok: true, ...mockAfter }; },
  async afterJobSet(action) { if (mockAfter) mockAfter.action = action; return { ok: true }; },
  async afterJobCancel() { if (mockAfter) { mockAfter.state = "idle"; mockAfter.action = "none"; } return { ok: true }; },
  async afterJobState() {
    const a = mockAfter || { action: "none", state: "idle" };
    const job = await this.dl("GET", "/api/job");
    if (a.state === "watching" && !job.data.running) { if (a.action === "none" || job.data.status !== "done") a.state = "idle"; else { a.state = "countdown"; a.until = Date.now() + 5000; } }
    if (a.state === "countdown" && Date.now() >= a.until) { a.state = "idle"; a.message = "预览版不会真的执行"; }
    const labels = { sleep: "让电脑睡眠", hibernate: "让电脑休眠", shutdown: "关机", exit: "退出软件", command: "运行命令" };
    return { ok: true, action: a.action, state: a.state, seconds: Math.max(0, Math.ceil((a.until - Date.now()) / 1000)), message: a.message || "", label: labels[a.action] || "" };
  },
  // 登录窗口：预览版里假装过几秒登录成功
  async loginStart() { mockLogin = { status: "waiting", at: Date.now() }; return { ok: true, status: "waiting" }; },
  async loginStatus() {
    if (!mockLogin) return { ok: true, status: "idle" };
    const age = Date.now() - mockLogin.at;
    if (mockLogin.status === "cancelled") return { ok: true, status: "cancelled" };
    return { ok: true, status: age > 2400 ? "done" : age > 1500 ? "finishing" : "waiting", name: "示例账号" };
  },
  async loginCancel() { if (mockLogin) mockLogin.status = "cancelled"; return { ok: true, status: "cancelled" }; },
  // 软件更新：预览版里演示一遍流程，不会真的下载或替换
  async updateCheck() { await new Promise((r) => setTimeout(r, 500)); updState = { state: "idle", done: 0, total: 67 * 1048576, error: "" }; return { ok: true, current: "预览", latest: "9.9.9", newer: true, size: 67 * 1048576, canApply: true, reason: "", page: "https://github.com/", notes: "- 示例：这里显示这一版的更新记录\n- 预览版不会真的更新" }; },
  async updateStart() { updState.state = "downloading"; updState.done = 0; const t = setInterval(() => { if (updState.state !== "downloading") return clearInterval(t); updState.done = Math.min(updState.total, updState.done + updState.total / 12); if (updState.done >= updState.total) { updState.state = "ready"; clearInterval(t); } }, 250); return { ok: true, ...updState }; },
  async updateStatus() { return { ok: true, ...updState }; },
  async updateCancel() { updState.state = "idle"; return { ok: true, ...updState }; },
  async updateApply() { return { ok: true, preview: true }; },
  async updateDone() { return null; },
  async openUrl(url) { window.open(url, "_blank", "noopener"); return true; },
  async setWallpaper() { return "preview"; },
  async exportWorks() { return "preview"; },
  async copyText() { return false; },
  async pickDirectory() { return null; },
  ...mockDownloader(),
};


// ---------------- 示例下载器：让“下载与更新”在预览里也能点（任务是模拟的，不访问网络） ----------------
function mockDownloader() {
  const now = () => Date.now() / 1000;
  const settings = {
    STORAGE_MODE: "smb", LOCAL_SAVE_PATH: "D:\\Pixiv", NAS_IP: "192.168.1.100", NAS_USER: "pixiv", NAS_PASS_SET: true, NAS_SHARE: "media", NAS_BASE_PATH: "插画/PIXIV", NAS_REMOTE_NAME: "",
    WEBDAV_URL: "", WEBDAV_USER: "", WEBDAV_PASS_SET: false, WEBDAV_VERIFY_TLS: true, FTP_URL: "", FTP_USER: "", FTP_PASS_SET: false, SFTP_URL: "", SFTP_USER: "", SFTP_PASS_SET: false, SFTP_KEY_FILE: "",
    S3_ENDPOINT: "", S3_REGION: "", S3_BUCKET: "", S3_PREFIX: "", S3_ACCESS_KEY: "", S3_SECRET_KEY_SET: false, S3_PATH_STYLE: false, S3_VERIFY_TLS: true,
    PROXY_MODE: "system", PROXY_URL: "", REVIEW_THRESHOLD: 2000, KEEP_AWAKE: true,
    MAIN_ACCOUNT_SYNC_THREADS: 1, BACKUP_ACCOUNT_SYNC_THREADS: 1, MAIN_ACCOUNT_DOWNLOAD_THREADS: 1, BACKUP_ACCOUNT_DOWNLOAD_THREADS: 2, DELAY_SYNC: [1.5, 3], DELAY_DOWNLOAD: [0.8, 2],
    FAILURE_RATE_THRESHOLD: 0.5, RATE_LIMIT_ENABLED: true, REST_EVERY: 150, REST_SECONDS: 10, MAX_RETRIES: 3, SYNC_TYPES: ["illust", "manga"], SYNC_NOVELS: true, METADATA_REFRESH_LIMIT: 20, UGOIRA_PREFER_HQ: true, UGOIRA_WEBP_LOSSLESS: true,
  };
  let accounts = [
    { name: "main", username: "示例用户", user_id: 1234567, remark: "", is_valid: true, last_tested: "2026-10-02 21:10:00", is_main: true, token_hint: "abcd…wxyz", r18: true, r18g: true },
    { name: "backup", username: "备用", user_id: 7654321, remark: "小号", is_valid: true, last_tested: "2026-10-02 21:10:00", is_main: false, token_hint: "efgh…stuv", r18: false, r18g: false },
  ];
  let failures = [{ kind: "deleted", count: 12, auto_retry: 0, sample: "作品不存在或已被删除" }, { kind: "network", count: 5, auto_retry: 5, sample: "ReadTimeout: i.pximg.net" }];
  let ignored = 3, linked = false;
  // 待下载：按画师分的示例数据（“先看再下”用）
  const mk = (i, files, extra) => ({ author_id: +ARTISTS[i].id, name: ARTISTS[i].name, files, works: Math.ceil(files / 3), illust: files, manga: 0, ugoira: 0, novel: 0, old: 0, r18: 0,
    est_bytes: files * 3.4e6, newest: "2026-10-01", is_new_artist: 0, ...(extra || {}) });
  let queue = [mk(0, 14), mk(1, 9, { manga: 4, illust: 5, old: 4 }), mk(2, 8, { is_new_artist: 1 }), mk(3, 4, { ugoira: 1, illust: 3 }), mk(4, 2)];
  let skippedQueue = [];
  const pendingTotal = () => queue.reduce((n, a) => n + a.files, 0);
  let syncFails = [
    { author_id: +ARTISTS[5].id, name: ARTISTS[5].name, kind: "rate_limit", error: "获取 illust 作品列表失败: Rate Limit", time: "2026-10-06 19:20:11", count: 1, gone: 0 },
    { author_id: +ARTISTS[6].id, name: ARTISTS[6].name, kind: "network", error: "获取 manga 作品列表失败: Internal Server Error", time: "2026-10-06 19:21:40", count: 2, gone: 0 },
    { author_id: 90000001, name: "已经不在的画师", kind: "gone", error: "账号已注销或不存在", time: "2026-10-01 10:00:00", count: 3, gone: 1 },
  ];
  let syncSkipped = [];
  const failedTasks = (kind) => Array.from({ length: (failures.find((g) => g.kind === kind) || { count: 0 }).count }, (_, i) => ({
    task_key: `${131000000 + i}_0`, illust_id: 131000000 + i, page_index: 0, media_type: "image", status: -1, attempts: kind === "deleted" ? 3 : 1, last_error: (failures.find((g) => g.kind === kind) || {}).sample,
    error_kind: kind, author_id: +ARTISTS[i % ARTISTS.length].id, author_name: ARTISTS[i % ARTISTS.length].name, title: `示例作品 ${i + 1}` }));
  const imported = new Set(["works_db"]);
  const runs = [{ id: 1, kind: "sync_download", status: "done", started: now() - 86400 * 2, finished: now() - 86400 * 2 + 300, success: 64, failed: 0 }];
  let job = { id: "idle0", kind: "idle", status: "idle", running: false, total: 0, done: 0, success: 0, failed: 0, skipped: 0, bytes: 0, phase: "", message: "", detail: {}, workers: [], logs: [], elapsed: 0 };
  let timer = 0;
  const names = ARTISTS.map((a) => a.name);
  const bump = (group, key, patch) => { const d = (job.detail[group] = job.detail[group] || {}); d[key] = { ...(d[key] || {}), ...patch(d[key] || {}) }; };
  function finish(status) {
    clearInterval(timer);
    Object.assign(job, { status, running: false, finished: now(), workers: [] });
    runs.unshift({ id: runs.length + 1, kind: job.kind, status, started: job.started, finished: job.finished, success: job.success, failed: job.failed });
  }
  function run(kind, body = {}) {
    const sync = kind.startsWith("sync") || kind === "recheck_gone", one = kind.endsWith("_artist") || kind === "retry_now";
    const only = body.author_ids ? new Set(body.author_ids) : null;
    const files = one ? 6 : sync ? 40 : queue.filter((a) => !only || only.has(a.author_id)).reduce((n, a) => n + a.files, 0);
    const review = kind === "sync_download" && body.review !== "never";
    job = { id: "j" + Math.round(now()), kind, status: "running", running: true, total: sync ? (one ? 1 : ARTISTS.length) : files, done: 0, success: 0, failed: 0, skipped: 0, bytes: 0,
      phase: sync ? "同步" : "下载", message: "", detail: {}, workers: [{ name: "main", text: "" }, { name: "backup", text: "" }], logs: [], elapsed: 0, started: now(), stopping: false };
    let stage = sync ? "sync" : "download";
    clearInterval(timer);
    timer = setInterval(() => {
      if (job.stopping) return finish("cancelled");
      if (job.paused) return;
      job.elapsed = now() - job.started;
      const who = names[Math.floor(Math.random() * names.length)];
      if (stage === "sync") {
        job.done++; job.workers[0].text = `检查 ${who}`;
        if (Math.random() < 0.5) bump("new", who, (x) => ({ name: who, works: (x.works || 0) + 1 + Math.floor(Math.random() * 3) }));
        job.logs.push({ t: now(), msg: `[同步] ${who} 完成` });
        if (job.done >= job.total) {
          job.result = { artists: job.total, artists_ok: job.total - 1, artists_failed: 1, new_files: pendingTotal(), old_files: 4, unchecked: 0, following_incomplete: "" };
          job.detail.failed = { [syncFails[0] ? syncFails[0].author_id : 1]: { name: (syncFails[0] || {}).name || "示例画师", note: "获取 illust 作品列表失败: Rate Limit", kind: "rate_limit" } };
          job.success = job.total - 1; job.failed = 1;
          if (review) { job.result.needs_review = true; finish("done"); }
          else if (kind.includes("download")) { stage = "download"; job.phase = "下载"; job.total = files; job.done = 0; job.success = 0; job.failed = 0; } else finish("done");
        }
        return;
      }
      const n = 1 + Math.floor(Math.random() * 3);
      for (let i = 0; i < n && job.done < job.total; i++) {
        job.done++;
        const size = 1.2e6 + Math.random() * 5e6;
        if (Math.random() < 0.04) { job.failed++; bump("fail_kinds", "network", (x) => ({ count: (x.count || 0) + 1 })); }
        else { job.success++; job.bytes += size; bump("downloaded", who, (x) => ({ name: who, files: (x.files || 0) + 1, bytes: (x.bytes || 0) + size })); }
      }
      job.workers[1].text = `下载 ${who} 的作品`;
      job.logs.push({ t: now(), msg: `[下载] ${who} · 第 ${job.done} 个` });
      if (job.done >= job.total) { job.result = { ...(job.result || {}), tasks: job.total }; queue = queue.filter((a) => only && !only.has(a.author_id)); finish("done"); }
    }, 450);
  }
  const ok = (data) => ({ ok: true, data }), err = (status, error) => ({ ok: false, status, error });
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));
  const failedTotal = () => failures.reduce((n, g) => n + g.count, 0);
  return {
    async dlInfo() {
      const home = "D:\\Pixiv Viewer\\data\\pixiv";
      return { available: true, home, builtinHome: home, external: false, hasData: true, started: true, version: "2.1.0", viewerKinds: true,
        files: { works_db: { path: home + "\\db\\pixiv_manager.db", exists: imported.has("works_db"), detail: "12 位画师 · 600 个作品" },
          dl_settings: { path: home + "\\settings.json", exists: true, detail: "2 个账号 · 保存方式 smb" },
          avatars: { path: home + "\\avatars", exists: imported.has("avatars"), detail: "12 个头像" } },
        kinds: { works_db: { label: "作品数据库", was: "pixiv_manager.db", need: "核心", what: "画师、作品、标题、标签、分级和下载记录。没有它，图片只能按文件名显示。" },
          dl_settings: { label: "账号与下载设置", was: "settings.json", need: "可选", what: "Pixiv 账号的登录凭证、保存位置、下载选项。不导入的话重新登录、重新设置即可。" },
          avatars: { label: "画师头像", was: "avatars 文件夹", need: "可选", what: "侧栏里的画师头像。不导入的话可以之后用“补全头像”从 Pixiv 下载。" },
          viewer_db: { label: "收藏与最近查看", was: "webapp.db", need: "可选", what: "收藏、最近查看、置顶的画师。只存在本机，不导入就没有。" },
          ratings_db: { label: "评分与自定义标签", was: "library.db", need: "可选", what: "你给图片打的星和加的标签。只存在本机，不导入就没有。" } } };
    },
    // 预览里没有真的文件可选：每点一次“选择”，给出一组示例的识别结果
    async importPick(what) {
      return what === "folder"
        ? [{ path: "E:\\备份\\头像", name: "头像", kind: "avatars", label: "画师头像", detail: "543 个头像", ok: true, problem: "", replaces: imported.has("avatars"), merge: false }]
        : [{ path: "E:\\备份\\我的库-2026.bak", name: "我的库-2026.bak", kind: "works_db", label: "作品数据库", detail: "547 位画师 · 92280 个作品", ok: true, problem: "", replaces: imported.has("works_db"), merge: false },
          { path: "E:\\备份\\stars.sqlite", name: "stars.sqlite", kind: "ratings_db", label: "评分与自定义标签", detail: "310 张有评分 · 42 张有标签", ok: true, problem: "", replaces: false, merge: true },
          { path: "E:\\备份\\说明.txt", name: "说明.txt", kind: "", label: "认不出来", detail: "", ok: false, problem: "既不是数据库，也不是设置文件" }];
    },
    async importApply(paths) {
      await wait(900);
      const results = [];
      if (paths.some((x) => x.includes(".bak"))) { results.push({ kind: "works_db", label: "作品数据库", ok: true, message: "547 位画师 · 92280 个作品", kept: imported.has("works_db") ? "D:\\Pixiv Viewer\\data\\pixiv\\db\\pixiv_manager.旧-20261006-101500.db" : "" }); imported.add("works_db"); }
      if (paths.some((x) => x.includes("头像"))) { results.push({ kind: "avatars", label: "画师头像", ok: true, message: "543 个头像", kept: "" }); imported.add("avatars"); }
      if (paths.some((x) => x.includes("stars"))) results.push({ kind: "ratings_db", label: "评分与自定义标签", ok: true, message: "合并了 310 张图的评分、42 条标签记录（按文件路径对应）", kept: "" });
      return { ok: true, results };
    },
    async dlSetHome() { return this.dlInfo(); },
    async openHome() { return false; },
    async dlMigrate() { return { ok: true, info: await this.dlInfo() }; },
    async dlStorageLink() { return { mode: settings.STORAGE_MODE, path: settings.STORAGE_MODE === "smb" ? `\\\\${settings.NAS_IP}\\${settings.NAS_SHARE}\\${settings.NAS_BASE_PATH.replace(/\//g, "\\")}` : settings.STORAGE_MODE === "local" ? settings.LOCAL_SAVE_PATH : "", readable: ["smb", "local"].includes(settings.STORAGE_MODE), inLibrary: ["smb", "local"].includes(settings.STORAGE_MODE), auto: true }; },
    async dlLinkLibrary() { linked = true; return { ok: true, ...(await this.dlStorageLink()) }; },
    async dlRefreshLibrary() { return { scanned: 0 }; },
    async libraryReconnect() { offline = false; return { ok: true, indexing: false }; },
    async dl(method, path, body) {
      await wait(120);
      body = body || {};
      if (path === "/api/job" && method === "GET") return ok({ ...job, logs: job.logs.slice(-60) });
      if (path === "/api/job" && method === "POST") { if (job.running) return err(409, "已有任务在运行，请先等待完成或点击「停止」"); run(body.kind, body); return ok({ ok: true }); }
      if (path === "/api/job/pause") { job.paused = true; job.message = "已暂停"; return ok({ ok: true, paused: true }); }
      if (path === "/api/job/resume") { job.paused = false; job.message = ""; return ok({ ok: true, resumed: true }); }
      if (path === "/api/pending/summary") {
        const src = body.skipped ? skippedQueue : queue, f = body.filters || {};
        const list = src.filter((a) => !f.types || f.types.some((t) => a[t] > 0)).filter((a) => !f.origin || (f.origin === "old" ? a.old > 0 : a.files > a.old));
        const sum = (k) => list.reduce((n, a) => n + a[k], 0);
        return ok({ artists: list.map((a) => ({ ...a })), totals: { files: sum("files"), works: sum("works"), est_bytes: sum("est_bytes"), artists: list.length }, skipped_total: skippedQueue.reduce((n, a) => n + a.files, 0) });
      }
      if (path === "/api/pending/skip") {
        const ids = new Set((body.filters || {}).author_ids || []);
        const from = body.restore ? skippedQueue : queue, moved = from.filter((a) => ids.has(a.author_id));
        if (body.restore) { skippedQueue = skippedQueue.filter((a) => !ids.has(a.author_id)); queue = queue.concat(moved); }
        else { queue = queue.filter((a) => !ids.has(a.author_id)); skippedQueue = skippedQueue.concat(moved); }
        return ok({ ok: true, count: moved.reduce((n, a) => n + a.files, 0) });
      }
      if (path === "/api/sync/failures") {
        const order = ["rate_limit", "network", "auth", "other", "gone"];
        return ok({ groups: order.filter((k) => syncFails.some((x) => x.kind === k)).map((k) => ({ kind: k, count: syncFails.filter((x) => x.kind === k).length, items: syncFails.filter((x) => x.kind === k) })),
          total: syncFails.length, skipped: syncSkipped });
      }
      if (path === "/api/sync/skip") {
        const ids = new Set(body.author_ids || []);
        if (body.restore) { syncSkipped = syncSkipped.filter((x) => !ids.has(x.author_id)); }
        else { syncSkipped = syncSkipped.concat(syncFails.filter((x) => ids.has(x.author_id))); syncFails = syncFails.filter((x) => !ids.has(x.author_id)); }
        return ok({ ok: true, count: ids.size });
      }
      if (path === "/api/tasks" && method === "GET") {
        const q = arguments[3] || {}, all = q.status === "ignored" ? Array.from({ length: ignored }, (_, i) => ({ ...failedTasks("deleted")[0], task_key: `ign${i}_0`, title: `已忽略的作品 ${i + 1}`, status: -2 })) : failedTasks(q.kind);
        const page = +q.page || 1;
        return ok({ items: all.slice((page - 1) * 50, page * 50), total: all.length, page, per: 50 });
      }
      if (path === "/api/failures/export") return ok({ csv: "task_key,illust_id\n", count: failedTotal() });
      if (path === "/api/job/stop") { job.stopping = true; job.message = "正在停止，等待当前文件处理完…"; return ok({ ok: true }); }
      if (path === "/api/plan") return ok({ skipped_total: skippedQueue.reduce((n, a) => n + a.files, 0), review_threshold: settings.REVIEW_THRESHOLD, sync_failed: syncFails.filter((x) => !x.gone).length,
        resume: null, pending: pendingTotal(), estimated_bytes: pendingTotal() * 3.4e6, exhausted: 0, last_sync: "2026-10-02 21:16:44", artists: ARTISTS.length, accounts_valid: accounts.filter((a) => a.is_valid).length, main_account: "main", failed_total: failedTotal(), running: job.running });
      if (path === "/api/notifications") return ok({ items: failures.length ? [{ id: "f", level: "info", title: `有 ${failedTotal()} 个文件下载失败`, detail: "其中一部分可以重试，其余多半是作品已被删除。", action: { label: "去处理", route: "#/tasks?tab=failed" } }] : [] });
      if (path === "/api/runs") return ok({ items: runs.slice(0, 5) });
      if (path === "/api/logs") return ok({ items: job.logs.map((l, i) => ({ id: i + 1, t: l.t, level: l.msg.includes("失败") ? "WARNING" : "INFO", msg: l.msg })).filter((l) => l.id > +((arguments[3] || {}).since || 0)) });
      if (path === "/api/failures") return ok({ groups: failures, ignored });
      if (path === "/api/tasks/retry" || (path === "/api/tasks/ignore" && !body.restore)) {
        const n = failures.filter((g) => body.kinds.includes(g.kind)).reduce((s, g) => s + g.count, 0);
        failures = failures.filter((g) => !body.kinds.includes(g.kind));
        if (!path.endsWith("retry")) ignored += n;
        return ok({ ok: true, count: n });
      }
      if (path === "/api/tasks/ignore") { const n = ignored; ignored = 0; if (n) failures.push({ kind: "deleted", count: n, auto_retry: 0, sample: "" }); return ok({ ok: true, count: n }); }
      if (path === "/api/accounts" && method === "GET") return ok({ items: accounts });
      if (path === "/api/accounts" && method === "POST") { accounts.push({ name: body.name, username: "新账号", user_id: 1, remark: body.remark || "", is_valid: true, last_tested: "", is_main: false, token_hint: "····" }); return ok({ ok: true, username: "新账号" }); }
      if (path === "/api/accounts/test") { await wait(900); return ok({ items: accounts.map((a) => ({ name: a.name, ok: true, r18: a.r18, r18g: a.r18g })) }); }
      if (path === "/api/accounts/main") { accounts.forEach((a) => { a.is_main = a.name === body.name; }); return ok({ ok: true }); }
      if (path.startsWith("/api/accounts/") && method === "DELETE") { accounts = accounts.filter((a) => a.name !== decodeURIComponent(path.slice(14))); return ok({ ok: true }); }
      if (path === "/api/accounts/oauth/start") return ok({ state: "demo", url: "https://app-api.pixiv.net/web/v1/login" });
      if (path === "/api/accounts/oauth/finish") return err(400, "预览版不能真的登录 Pixiv");
      if (path === "/api/settings" && method === "GET") return ok({ settings: { ...settings }, presets: [] });
      if (path === "/api/settings" && method === "POST") {
        if (job.running) return err(409, "任务运行中，暂不能修改设置");
        for (const [k, v] of Object.entries(body)) { if (k.endsWith("_PASS") || k === "S3_SECRET_KEY") settings[k + "_SET"] = !!v; else settings[k] = v; }
        return ok({ ok: true, applied: Object.keys(body) });
      }
      if (path === "/api/settings/test-proxy") {
        await wait(600);
        if (body.PROXY_MODE === "custom" && !body.PROXY_URL) return err(400, "请先填写代理地址");
        const good = body.PROXY_MODE !== "none";
        return ok({ ok: good, message: good ? "可以连上 Pixiv" : "连不上 Pixiv", using: body.PROXY_MODE === "custom" ? body.PROXY_URL : body.PROXY_MODE === "none" ? "不使用代理" : "跟随系统设置（系统没有设置代理，直接连接）",
          steps: [{ name: "Pixiv 接口", ok: good, detail: good ? "连得上，312 毫秒" : "8 秒内没有回应" }, { name: "图片服务器", ok: good, detail: good ? "连得上，208 毫秒" : "8 秒内没有回应" }] });
      }
      if (path === "/api/settings/test-storage") { await wait(700); return ok({ ok: true, message: "连接正常，可以保存", steps: [{ name: "连接服务器", ok: true, detail: "192.168.1.100 可以连上" }, { name: "登录", ok: true, detail: "用户名和密码正确" }, { name: "目录", ok: true, detail: "media/插画/PIXIV 存在" }, { name: "写入", ok: true, detail: "可以写入文件" }] }); }
      if (path === "/api/storage/browse") return ok(body.share ? { ok: true, kind: "folders", entries: body.path ? ["2024", "2025", "合集"] : ["插画", "漫画", "备份"] } : { ok: true, kind: "shares", entries: ["media", "backup", "home"] });
      return err(404, "接口不存在");
    },
  };
}

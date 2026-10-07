// 外观设置：风格、密度、字体的定义，以及把设置写成 CSS 变量
export const FAMILIES = {
  baisha: { name: "白砂", hint: "中性白 · Pixiv 蓝",
    light: { bg: "#ffffff", side: "#f6f6f7", surface: "#ffffff", sunken: "#f0f0f2", canvas: "#ececef", line: "#e4e4e8", text: "#18181b", text2: "#52525b", text3: "#8b8b93", accent: "#0096fa", accentFg: "#ffffff", shadow: "20 20 30" },
    dark: { bg: "#18181b", side: "#121214", surface: "#202024", sunken: "#27272b", canvas: "#0c0c0e", line: "#2e2e33", text: "#ececef", text2: "#a8a8b0", text3: "#71717a", accent: "#2aa6ff", accentFg: "#06121c", shadow: "0 0 0" } },
  chenwu: { name: "晨雾", hint: "冷灰蓝 · 靛青",
    light: { bg: "#f8f9fb", side: "#eef1f5", surface: "#ffffff", sunken: "#e7ebf1", canvas: "#e3e7ed", line: "#dce1e9", text: "#1a2130", text2: "#4b5466", text3: "#8a93a4", accent: "#4457d8", accentFg: "#ffffff", shadow: "30 40 70" },
    dark: { bg: "#151922", side: "#10131a", surface: "#1c212c", sunken: "#232937", canvas: "#0b0d12", line: "#2a3141", text: "#e8ebf2", text2: "#a3abbd", text3: "#6d7588", accent: "#7d8cff", accentFg: "#0d1020", shadow: "0 0 0" } },
  shimo: { name: "石墨", hint: "石墨灰 · 青绿",
    light: { bg: "#fbfbfa", side: "#f1f2f1", surface: "#ffffff", sunken: "#ebeceb", canvas: "#e6e7e6", line: "#e0e2e0", text: "#1b1d1c", text2: "#545957", text3: "#8d928f", accent: "#0f8f7a", accentFg: "#ffffff", shadow: "25 30 28" },
    dark: { bg: "#171918", side: "#111312", surface: "#1f2221", sunken: "#262928", canvas: "#0b0c0c", line: "#2d3130", text: "#e9ecea", text2: "#a7adaa", text3: "#717875", accent: "#2fc4a6", accentFg: "#06140f", shadow: "0 0 0" } },
};

export const ACCENTS = ["#0096fa", "#4457d8", "#7048e8", "#d6336c", "#e8590c", "#0f8f7a"];

export const DENSITY = {
  compact: { fs: 12, ctl: 26, gap: 6, radius: 6, row: 26, title: 17, tile: 150, tgap: 6, tradius: 6, barbtn: 28 },
  medium: { fs: 13, ctl: 28, gap: 8, radius: 7, row: 30, title: 19, tile: 176, tgap: 10, tradius: 8, barbtn: 30 },
  relaxed: { fs: 14, ctl: 34, gap: 11, radius: 10, row: 36, title: 24, tile: 216, tgap: 16, tradius: 12, barbtn: 36 },
};

const FB = '"Microsoft YaHei UI", "PingFang SC", sans-serif';
// 霞鹜文楷和 M+ 圆体随程序自带（css/fonts.css）。带 web 的几款选用时才从网上加载；没有网络时显示为后面的备用字体。
export const FONTS = {
  system: { label: "系统默认", stack: '"Segoe UI Variable Text", "Segoe UI", ' + FB },
  noto: { label: "思源黑体", stack: '"Noto Sans SC", ' + FB, web: "Noto+Sans+SC:wght@400;500;600" },
  wenkai: { label: "霞鹜文楷", stack: '"LXGW WenKai", "LXGW WenKai TC", "Noto Sans SC", ' + FB },
  mplus: { label: "M+ 圆体", stack: '"M PLUS Rounded 1c", "Noto Sans SC", ' + FB },
  zenmaru: { label: "Zen 丸ゴシック", stack: '"Zen Maru Gothic", "Noto Sans SC", ' + FB, web: "Zen+Maru+Gothic:wght@400;500;700" },
  kiwi: { label: "Kiwi 丸", stack: '"Kiwi Maru", "Noto Sans SC", ' + FB, web: "Kiwi+Maru:wght@400;500" },
  klee: { label: "Klee 铅笔", stack: '"Klee One", "Noto Sans SC", ' + FB, web: "Klee+One:wght@400;600" },
};
export const HEAD_FONTS = {
  same: { label: "同正文", stack: "var(--font)" },
  mplus: FONTS.mplus,
  zenmaru: FONTS.zenmaru,
  wenkai: FONTS.wenkai,
  kuaile: { label: "站酷快乐体", stack: '"ZCOOL KuaiLe", ' + FB, web: "ZCOOL+KuaiLe" },
  mochiy: { label: "Mochiy Pop", stack: '"Mochiy Pop One", "ZCOOL KuaiLe", ' + FB, web: "Mochiy+Pop+One&family=ZCOOL+KuaiLe" },
};

/** 默认值：来自界面原型里定稿的参数 */
export const DEFAULTS = {
  family: "baisha", mode: "light", accent: null, font: "wenkai", hfont: "mplus",
  density: "medium", ...DENSITY.medium,
  sidew: 228, infow: 300, shadow: 0.5,
  layout: "uniform", ratio: "4 / 5", caption: "hover", status: "off", tagchips: "on",
  badges: "on", mergePages: true, groupByMonth: true, hoverPreview: true, sidebarCollapsed: false,
  notifyOnFinish: true,     // 任务结束时弹系统通知
  afterCommand: "",         // “完成后运行命令”要运行的命令
  keys: {},                 // 改过的快捷键：{ 动作: [按键, …] }，见 keymap.js
  closeAction: "ask",       // 关闭窗口时：ask 每次询问 / tray 放到托盘 / exit 直接退出
  showR18: false, openFolders: [],   // R18 内容默认不显示，要在设置里打开
  // 看图
  infoOpen: true, viewerBg: "canvas", doubleClick: "fit", wheel: "zoom", fitOnOpen: true, preload: true,
  slideInterval: 2.5, pixelated: false,
  // 浏览
  sort: "id", rating: "all", rememberLast: true, artistSort: "name",
};

const DENSITY_KEYS = Object.keys(DENSITY.medium);

export function normalize(raw) {
  const s = { ...DEFAULTS, ...(raw || {}) };
  // 幻灯片间隔的默认值从 4 秒改成了 2.5 秒：还停在旧默认值上的配置跟着改一次
  if (!s.slideDefault25) { if (s.slideInterval === 4) s.slideInterval = DEFAULTS.slideInterval; s.slideDefault25 = true; }
  if (!FAMILIES[s.family]) s.family = DEFAULTS.family;
  if (!["light", "dark", "system"].includes(s.mode)) s.mode = DEFAULTS.mode;
  if (!FONTS[s.font]) s.font = DEFAULTS.font;
  if (!HEAD_FONTS[s.hfont]) s.hfont = DEFAULTS.hfont;
  return s;
}

export function setDensity(s, name) {
  Object.assign(s, DENSITY[name]);
  s.density = name;
}
export function markCustomDensity(s, key) {
  if (DENSITY_KEYS.includes(key)) s.density = "custom";
}

export function effectiveMode(s) {
  if (s.mode !== "system") return s.mode;
  return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

function mix(hex, bg, t) {
  const h = (x) => [1, 3, 5].map((i) => parseInt(x.slice(i, i + 2), 16));
  const a = h(hex), b = h(bg);
  return "#" + a.map((v, i) => Math.round(b[i] + (v - b[i]) * t).toString(16).padStart(2, "0")).join("");
}

export function palette(s) {
  const mode = effectiveMode(s);
  const p = { ...FAMILIES[s.family][mode] };
  if (s.accent) p.accent = s.accent;
  p.accentSoft = mix(p.accent, p.bg, mode === "dark" ? 0.2 : 0.11);
  return { mode, ...p };
}

/** 选了不随程序自带的字体时，按需加载它（只加载一次；断网时什么都不发生，用备用字体显示） */
const webFontsLoaded = new Set();
function loadWebFont(def) {
  if (!def || !def.web || webFontsLoaded.has(def.web)) return;
  webFontsLoaded.add(def.web);
  const link = document.createElement("link");
  link.rel = "stylesheet";
  link.href = `https://fonts.googleapis.com/css2?family=${def.web}&display=swap`;
  document.head.append(link);
}

/** 浏览器预览版（没有后端）可能被放在不带 fonts/ 文件夹的地方，这时默认字体也从网上取一份 */
export function loadPreviewFonts() {
  loadWebFont({ web: "M+PLUS+Rounded+1c:wght@400;500;700&family=LXGW+WenKai+TC:wght@400;700" });
}

/**
 * 亮色 / 暗色切换时整页淡入淡出（0.5 秒，时长在 base.css 里），不然一下子全白或全黑很刺眼。
 * 用的是浏览器的“视图过渡”：先拍下旧画面，执行 apply 换好颜色，再把新旧两张画面交叉淡化——
 * 不管页面上有多少缩略图都一样流畅。亮暗没变、系统设置了“减少动态效果”或不支持时直接执行。
 */
export function withThemeFade(s, apply) {
  const root = document.documentElement;
  const changes = !!root.dataset.theme && palette(s).mode !== root.dataset.theme;
  const calm = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (!changes || calm || !document.startViewTransition) return apply();
  document.startViewTransition(apply);
}

/** 把设置写到 :root 的 CSS 变量与 data-theme 上 */
export function applySettings(s) {
  const p = palette(s);
  const root = document.documentElement;
  loadWebFont(FONTS[s.font]);
  loadWebFont(HEAD_FONTS[s.hfont]);
  const vars = {
    bg: p.bg, side: p.side, surface: p.surface, sunken: p.sunken, canvas: p.canvas, line: p.line,
    text: p.text, "text-2": p.text2, "text-3": p.text3, accent: p.accent, "accent-fg": p.accentFg,
    "accent-soft": p.accentSoft, "shadow-rgb": p.shadow,
    fs: s.fs + "px", ctl: s.ctl + "px", gap: s.gap + "px", radius: s.radius + "px", row: s.row + "px",
    "title-fs": s.title + "px", "side-w": s.sidew + "px", "info-w": s.infow + "px", tile: s.tile + "px",
    "tile-gap": s.tgap + "px", "tile-radius": s.tradius + "px", ratio: s.ratio, "shadow-k": s.shadow,
    "bar-btn": s.barbtn + "px", font: FONTS[s.font].stack, "font-head": HEAD_FONTS[s.hfont].stack,
  };
  for (const [k, v] of Object.entries(vars)) root.style.setProperty("--" + k, v);
  root.dataset.theme = p.mode;
  root.style.colorScheme = p.mode;
  return p;
}

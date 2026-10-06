// 快捷键：有哪些动作、默认按什么、用户改成了什么；把一次按键 / 鼠标侧键认成一个动作。
// 用户改过的存在设置的 keys 里：{ 动作: [按键, …] }，没改过的动作不出现在里面（跟随默认值）。

// [分组, 动作, 名称, 默认按键]。分组决定在哪里生效：全局到处都行；网格、看图只在各自的界面里。
export const ACTIONS = [
  ["全局", "search", "搜索", ["Ctrl+F", "Ctrl+K"]],
  ["全局", "back", "返回 / 后退", ["Mouse4"]],
  ["全局", "forward", "前进", ["Mouse5"]],
  ["全局", "sidebar", "收起 / 展开侧栏", ["Ctrl+B"]],
  ["全局", "settings", "设置", ["Ctrl+,"]],
  ["全局", "keys", "快捷键一览", ["?"]],
  ["网格", "grid.left", "向左移动", ["ArrowLeft"]],
  ["网格", "grid.right", "向右移动", ["ArrowRight"]],
  ["网格", "grid.up", "向上移动", ["ArrowUp"]],
  ["网格", "grid.down", "向下移动", ["ArrowDown"]],
  ["网格", "grid.open", "打开作品", ["Enter"]],
  ["网格", "grid.quicklook", "快速预览", ["Space"]],
  ["网格", "grid.selectAll", "全选", ["Ctrl+A"]],
  ["网格", "grid.clear", "取消选择", ["Escape"]],
  ["网格", "grid.fav", "收藏 / 取消收藏", ["F"]],
  ["网格", "grid.tag", "添加标签", ["T"]],
  ["网格", "grid.copy", "复制路径", ["Ctrl+C"]],
  ["看图", "v.prev", "上一个作品", ["ArrowLeft"]],
  ["看图", "v.next", "下一个作品", ["ArrowRight"]],
  ["看图", "v.pageUp", "上一页（多页作品）", ["ArrowUp", "PageUp"]],
  ["看图", "v.pageDown", "下一页（多页作品）", ["ArrowDown", "PageDown", "Space"]],
  ["看图", "v.first", "第一个作品", ["Home"]],
  ["看图", "v.last", "最后一个作品", ["End"]],
  ["看图", "v.zoomIn", "放大", ["+", "="]],
  ["看图", "v.zoomOut", "缩小", ["-"]],
  ["看图", "v.fit", "适应窗口", ["Ctrl+0"]],
  ["看图", "v.actual", "原始大小", ["Ctrl+1"]],
  ["看图", "v.rotate", "旋转", ["R"]],
  ["看图", "v.fav", "收藏 / 取消收藏", ["F"]],
  ["看图", "v.info", "信息栏", ["I"]],
  ["看图", "v.pages", "全部页（多页作品）", ["G"]],
  ["看图", "v.slide", "幻灯片", ["F5"]],
  ["看图", "v.zen", "沉浸模式", ["F11", "Alt+Enter"]],
  ["看图", "v.copy", "复制路径", ["Ctrl+C"]],
  ["看图", "v.close", "返回网格", ["Escape"]],
];
// 不能改的操作，只在一览里列出来
export const FIXED = [
  ["网格", "多选", "Ctrl + 点击 / Shift + 点击"], ["网格", "打开作品", "双击"], ["网格", "评分 / 清除评分", "1 – 5 / 0"], ["网格", "缩略图大小", "Ctrl + 滚轮"],
  ["看图", "评分 / 清除评分", "1 – 5 / 0"], ["看图", "缩放", "滚轮"], ["看图", "适应窗口 / 原始大小", "双击"],
];
export const GROUPS = ["全局", "网格", "看图"];
const BY_ID = new Map(ACTIONS.map((a) => [a[1], a]));
const NAMES = { ArrowLeft: "←", ArrowRight: "→", ArrowUp: "↑", ArrowDown: "↓", Escape: "Esc", Mouse4: "鼠标侧键（后退）", Mouse5: "鼠标侧键（前进）", MouseMiddle: "鼠标中键", " ": "Space" };

/** 给人看的写法：Ctrl+ArrowLeft → Ctrl ← */
export const comboLabel = (combo) => String(combo).split("+").map((p) => NAMES[p] || p).join(" ").replace(/^ $/, "+") || "+";

/** 把一次键盘事件写成 "Ctrl+Shift+K" 这样；只按了 Ctrl / Shift 之类的修饰键时返回空字符串 */
export function comboOf(e) {
  let key = e.key;
  if (!key || ["Control", "Shift", "Alt", "Meta", "OS", "Dead", "Process", "Unidentified"].includes(key)) return "";
  if (key === " ") key = "Space";
  const printable = key.length === 1;
  if (printable && /[a-z]/i.test(key)) key = key.toUpperCase();
  const mods = [];
  if (e.ctrlKey || e.metaKey) mods.push("Ctrl");
  if (e.altKey) mods.push("Alt");
  // 符号键（? + 等）本身就是按着 Shift 打出来的，不再重复写 Shift
  if (e.shiftKey && !(printable && !/[a-z0-9]/i.test(key))) mods.push("Shift");
  return [...mods, key].join("+");
}
/** 鼠标侧键 / 中键 → "Mouse4" 这样的写法；别的键返回空字符串 */
export const mouseCombo = (e) => ({ 1: "MouseMiddle", 3: "Mouse4", 4: "Mouse5" }[e.button] || "");

export function createKeymap(getOverrides) {
  const bindings = (id) => { const o = getOverrides() || {}; return Array.isArray(o[id]) ? o[id] : BY_ID.get(id)[3]; };
  /** 在某个界面里，这个按键是哪个动作。groups 按优先顺序给，例如 ["看图", "全局"]。 */
  function find(combo, groups) {
    if (!combo) return "";
    for (const g of groups) for (const a of ACTIONS) if (a[0] === g && bindings(a[1]).includes(combo)) return a[1];
    return "";
  }
  /** 这个按键已经被谁用了（会和 id 冲突的动作）：同一组里的，或者全局和别的组之间 */
  function conflict(id, combo) {
    const group = BY_ID.get(id)[0];
    const clash = ACTIONS.find((a) => a[1] !== id && bindings(a[1]).includes(combo) && (a[0] === group || a[0] === "全局" || group === "全局"));
    return clash ? clash[2] : "";
  }
  const isDefault = (id) => !Array.isArray((getOverrides() || {})[id]);
  const label = (id) => { const b = bindings(id).filter((c) => !c.startsWith("Mouse")); return b.length ? comboLabel(b[0]) : ""; };
  return { bindings, find, conflict, isDefault, label };
}

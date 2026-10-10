// 界面语言。
//
// 界面上的文字在代码里是直接用中文写的。这里不逐处改写，而是在显示的那一刻翻译：盯着页面的变化，
// 新出现的文字（以及 title、placeholder 这些属性）如果在对照表里，就换成对应的英文。
// 这样加新功能时照常用中文写，再往对照表（i18n_en.js）里补一条就行；漏了的只是那一处仍显示中文，不会出错。
//
// 对照表的三种写法：
//   exact     整段文字一模一样：        "添加文件夹" -> "Add folder"
//   数字模板   文字里的数字换成 {0} {1}： "{0} 张 · {1} 个作品" -> "{0} images · {1} works"
//   patterns  带名字等可变内容的句子，用正则：[/^已添加 (.+)$/, "Added $1"]
//
// 不翻译的：用户自己的内容（画师名、标签、作品标题、文件夹名、日志）。它们所在的区域列在 SKIP 里；
// 另外只有“整段完全等于对照表里某一条”才会被替换，所以正文里碰巧含有某个词不会被动到。
export const LANGS = [["zh", "中文"], ["en", "English"], ["ja", "日本語"]];
const TABLE_FILES = { en: "./i18n_en.js", ja: "./i18n_ja.js" };      // 用到哪种语言才去读它的对照表
const CJK = /[㐀-鿿぀-ヿ]/;
// 这些区域里是用户的内容或后端的日志，一律不动
const SKIP = "[data-nt], .avatar, .tile, .chip[data-t], .chip[data-q], .suggest, #artists .lbl, #title-area h1[role=link], .v-title, .v-tags, .v-caption, "
  + ".dl-logs, #dl-logbox, .dl-chip, .dl-task b, .dl-task .mono, .mono, code, .dl-workers .tx, .dl-workers .nm, .dl-file .nm + .mono, .mhead + button";
const ATTRS = ["title", "placeholder", "aria-label"];

let lang = "zh", table = null, observer = null;
const seen = new Set();                    // 收集模式下见过的文字（开发时用来找还没翻译的）
let collecting = false;

/** 文字里的数字（含千分位、小数）换成 {0} {1}…，返回 [模板, 数字们] */
function templated(text) {
  const nums = [];
  const key = text.replace(/\d[\d,]*(?:\.\d+)?/g, (m) => { nums.push(m); return `{${nums.length - 1}}`; });
  return [key, nums];
}

/** 用一张对照表翻译一段文字；没有对应的译文返回 null */
export function translateWith(tbl, core) {
  const hit = tbl.exact[core];
  if (hit !== undefined) return hit;
  const [key, nums] = templated(core);
  if (nums.length) {
    const t = tbl.exact[key];
    if (t !== undefined) return t.replace(/\{(\d+)\}/g, (m, i) => nums[+i] ?? m);
  }
  for (const [re, to] of tbl.patterns) {
    const m = re.exec(core);
    if (!m) continue;
    // $1 $2：原样带过去；{t1}：那一段本身也是界面文字，再翻译一次（翻不了就原样）
    return to.replace(/\$(\d)/g, (x, i) => m[+i] ?? "").replace(/\{t(\d)\}/g, (x, i) => translateWith(tbl, m[+i] ?? "") ?? (m[+i] ?? ""));
  }
  return null;
}
const lookup = (core) => translateWith(table, core);

/** 给代码里直接用的地方（窗口标题、原生对话框）：翻译一段文字，没有译文就原样返回 */
export function tr(text) {
  if (!table || !text || !CJK.test(text)) return text;
  const m = /^(\s*)([\s\S]*?)(\s*)$/.exec(String(text));
  const out = lookup(m[2]);
  if (out !== null) produced.add(out);
  return out === null ? text : m[1] + out + m[3];
}

function note(core) {
  if (!collecting || core.length > 400) return;
  const key = templated(core)[0];
  if (seen.has(key)) return;
  seen.add(key);
  if (window.__i18nReport) window.__i18nReport(key);      // 测试里用它把见过的文字收集到一个文件里
}

// 没有译文的文字记下来（开发时检查用：window.__i18nMissed() 列出它们和所在的位置）
const misses = new Map();
const produced = new Set();                  // 已经换上去的译文
function missed(core, el) {
  if (misses.size > 2000 || misses.has(core)) return;
  misses.set(core, `${el.tagName.toLowerCase()}${el.id ? "#" + el.id : ""}${el.className && typeof el.className === "string" ? "." + el.className.trim().split(/\s+/).join(".") : ""}`);
}

function textNode(node) {
  const v = node.nodeValue;
  if (!v || !CJK.test(v)) return;
  const parent = node.parentElement;
  if (!parent || parent.closest(SKIP) || parent.tagName === "SCRIPT" || parent.tagName === "STYLE") return;
  const m = /^(\s*)([\s\S]*?)(\s*)$/.exec(v);
  note(m[2]);
  if (!table) return;
  if (produced.has(m[2])) return;            // 这是刚换上去的译文（日语里也有汉字），不要再当成原文处理
  const out = lookup(m[2]);
  if (out === null) missed(m[2], parent);
  else if (out !== m[2]) { produced.add(out); node.nodeValue = m[1] + out + m[3]; }
}

function attrs(el) {
  for (const a of ATTRS) {
    const v = el.getAttribute(a);
    if (!v || !CJK.test(v) || el.closest("[data-nt]")) continue;
    note(v.trim());
    if (!table) continue;
    if (produced.has(v.trim())) continue;
    const out = lookup(v.trim());
    if (out === null) { if (!el.classList.contains("help")) missed(v.trim(), el); } else if (out !== v) { produced.add(out); el.setAttribute(a, out); }
  }
}

function walk(root) {
  if (root.nodeType === 3) return textNode(root);
  if (root.nodeType !== 1) return;
  attrs(root);
  const it = document.createTreeWalker(root, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT);
  for (let n = it.nextNode(); n; n = it.nextNode()) (n.nodeType === 3 ? textNode(n) : attrs(n));
}

export const currentLang = () => lang;

/** 开始按这个语言显示（要先读对照表，所以是异步的）。中文是原文，不需要做任何事。 */
export async function setLang(next) {
  lang = TABLE_FILES[next] ? next : "zh";
  table = null;
  if (lang !== "zh") {
    try { table = (await import(TABLE_FILES[lang])).default; } catch (e) { lang = "zh"; }      // 读不到就保持中文，不让界面打不开
  }
  collecting = !!window.__i18nCollect || new URLSearchParams(location.search).has("i18ncollect");
  document.documentElement.lang = lang === "zh" ? "zh-CN" : lang;
  document.documentElement.dataset.lang = lang;
  if (observer) { observer.disconnect(); observer = null; }
  if (!table && !collecting) return;
  walk(document.documentElement);
  observer = new MutationObserver((list) => {
    for (const m of list) {
      if (m.type === "characterData") textNode(m.target);
      else if (m.type === "attributes") attrs(m.target);
      else for (const n of m.addedNodes) walk(n);
    }
  });
  observer.observe(document.documentElement, { subtree: true, childList: true, characterData: true, attributes: true, attributeFilter: ATTRS });
  if (table) {
    // 窗口标题不是页面里的节点，单独处理
    const title = Object.getOwnPropertyDescriptor(Document.prototype, "title");
    try { Object.defineProperty(document, "title", { configurable: true, get: () => title.get.call(document), set: (v) => title.set.call(document, tr(v)) }); } catch (e) { /* 改不了就保持原样 */ }
  }
}

if (typeof window !== "undefined") { window.__i18nSeen = () => [...seen]; window.__i18nMissed = () => [...misses]; }

// 设置项的说明：短的直接写在标题下面；长的收进标题旁的小问号里，鼠标移上去（或用键盘聚焦、点一下）才显示。
// 这样设置页一眼看过去只有“是什么”和“怎么选”，想知道细节时再看说明。

const SHORT = 20;      // 不超过这么多字的说明不值得藏起来

/** 一条说明的 HTML（可以带标签）。空 = 没有说明。 */
export function hintHTML(hint) {
  if (!hint) return "";
  const plain = String(hint).replace(/<[^>]+>/g, "");
  if (plain.length <= SHORT) return `<small>${hint}</small>`;
  return `<span class="help" tabindex="0" role="note" aria-label="${plain.replace(/"/g, "&quot;")}">?<span class="help-text" hidden>${hint}</span></span>`;
}

let tip = null, owner = null;
function show(el) {
  if (owner === el) return;
  owner = el;
  if (!tip) { tip = document.createElement("div"); tip.id = "helptip"; document.body.append(tip); }
  tip.innerHTML = el.querySelector(".help-text").innerHTML;
  tip.hidden = false;
  // 默认出现在问号的右下方；放不下时往左、往上挪，始终留在窗口里
  const r = el.getBoundingClientRect(), t = tip.getBoundingClientRect(), pad = 8;
  const left = Math.max(pad, Math.min(r.left - 6, window.innerWidth - t.width - pad));
  const below = r.bottom + 6, top = below + t.height + pad <= window.innerHeight ? below : Math.max(pad, r.top - t.height - 6);
  tip.style.left = left + "px";
  tip.style.top = top + "px";
}
function hide() { owner = null; if (tip) tip.hidden = true; }
const helpOf = (e) => (e.target instanceof Element ? e.target.closest(".help") : null);

document.addEventListener("mouseover", (e) => { const h = helpOf(e); if (h) show(h); });
document.addEventListener("mouseout", (e) => { if (helpOf(e) && !(e.relatedTarget instanceof Element && e.relatedTarget.closest(".help") === owner)) hide(); });
document.addEventListener("focusin", (e) => { const h = helpOf(e); if (h) show(h); });
document.addEventListener("focusout", (e) => { if (helpOf(e)) hide(); });
document.addEventListener("click", (e) => { const h = helpOf(e); if (h) { e.preventDefault(); e.stopPropagation(); show(h); } }, true);
document.addEventListener("scroll", hide, true);
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && owner) hide(); }, true);

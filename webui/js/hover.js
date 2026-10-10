// 悬停预览：鼠标在作品卡片上停一会儿，旁边浮出一张大一些的预览（多页作品可滚轮翻页）；
// 停在侧栏的画师上，浮出这位画师最近的几张作品。浮层不接收鼠标事件，不会挡住操作。
import { $, esc, fmtNum, icon } from "./util.js";

const DELAY = 420;          // 停留多久才出现（毫秒），避免鼠标路过时到处弹
const ARTIST_SHOTS = 6;

export function initHover(ctx) {
  const { view } = ctx;
  const peek = $("#peek");
  let timer = 0, anchor = null, cur = null;   // cur: { work, page } 当前预览的作品
  const artistCache = new Map();

  // inViewer：目标在看图页里（信息栏的“其他作品”）；其余目标只在网格页有效
  const enabled = (inViewer) => ctx.S.hoverPreview !== false && ctx.viewerOpen() === !!inViewer && !ctx.quicklookOpen()
    && $("#menu").hidden && $("#scrim").hidden;

  function hide() {
    clearTimeout(timer);
    timer = 0; anchor = null; cur = null;
    if (!peek.hidden) { peek.hidden = true; peek.innerHTML = ""; }
  }
  function schedule(el, show, inViewer) {
    if (anchor === el) return;
    hide();
    anchor = el;
    timer = setTimeout(() => { if (anchor === el && el.isConnected && enabled(inViewer)) show(el); }, DELAY);
  }

  // 放在目标的右侧，放不下就放左侧；上下不超出窗口
  // side：水平方向按这个元素来避让（看图页里用整个信息栏，预览就不会盖住信息栏）
  function place(el, w, h, side = el) {
    const r = el.getBoundingClientRect(), s = side.getBoundingClientRect(), box = $("#app").getBoundingClientRect(), gap = 10;
    let x = s.right + gap;
    if (x + w > box.right - 8) x = s.left - gap - w;
    if (x < box.left + 8) x = Math.max(box.left + 8, Math.min(box.right - 8 - w, r.left + (r.width - w) / 2));
    const y = Math.max(box.top + 8, Math.min(box.bottom - 8 - h, r.top + (r.height - h) / 2));
    peek.style.left = x - box.left + "px";
    peek.style.top = y - box.top + "px";
  }

  // ---------- 作品 ----------
  function showWork(tile) {
    const w = tile.dataset.k ? ctx.workByKey(tile.dataset.k) : ctx.moreWorkByKey(tile.dataset.key);
    if (!w) return;
    cur = { work: w, page: Math.min(w.pages.length - 1, +tile.dataset.pg || 0), tile };
    renderWork();
  }
  // 预览着多页作品时，滚轮用来翻页
  function wheelPages(e) {
    if (!cur || peek.hidden || cur.work.pages.length < 2 || e.ctrlKey) return;
    e.preventDefault();
    const n = cur.work.pages.length;
    cur.page = (cur.page + (e.deltaY > 0 ? 1 : -1) + n) % n;
    renderWork();
  }
  function renderWork() {
    const { work: w, page, tile } = cur;
    const p = w.pages[page];
    const ar = p.w && p.h ? p.w / p.h : w.ar || 0.75;
    const capH = 44, maxW = 380, maxH = Math.min(window.innerHeight - 40, 620) - capH;
    const width = Math.round(Math.max(200, Math.min(maxW, maxH * ar)));
    const height = Math.round(Math.min(maxH, width / ar));
    const many = w.pages.length > 1;
    peek.className = "peek";
    peek.style.width = width + "px";
    peek.innerHTML = `<div class="peek-img" style="height:${height}px"><img src="${esc(ctx.api.thumbSrc(p.thumb || p.path))}" alt="">
        ${w.anim ? `<span class="badge anim peek-play">${icon("play")}动图</span>` : ""}
        ${many ? `<span class="peek-pg">${page + 1} / ${w.pages.length}</span>` : ""}</div>
      <div class="peek-cap"><b>${esc(w.title)}</b><span>${esc(w.artistName)}${many ? (tile.dataset.k ? " · 滚轮翻页" : ` · 共 ${w.pages.length} 页`) : ""}</span></div>`;
    peek.hidden = false;
    place(tile, width, height + capH, tile.closest("#info") || tile);
    // 动图：缩略图是静止的第一帧。预览出来之后去读原文件，读到了就换上去播放（鼠标移走了就不换）
    if (w.anim && ctx.api.imageUrl) {
      const img = peek.querySelector(".peek-img img"), full = new Image();
      full.onload = () => { if (!peek.hidden && peek.contains(img)) { img.src = full.src; peek.querySelector(".peek-play")?.remove(); } };
      full.src = ctx.api.imageUrl(p.path);
    }
  }

  const grid = $("#grid-root");
  grid.addEventListener("mouseover", (e) => {
    const tile = e.target.closest(".tile");
    if (!tile || e.buttons) return;
    schedule(tile, showWork);
  });
  grid.addEventListener("mouseleave", hide);
  grid.addEventListener("mouseout", (e) => { if (anchor && !e.relatedTarget?.closest?.(".tile")) hide(); });
  // 其余情况滚轮照常滚动网格（并收起预览）
  grid.addEventListener("wheel", wheelPages, { passive: false });

  // 看图页信息栏里“这位画师的作品”那一排
  const more = $("#info-more");
  more.addEventListener("mouseover", (e) => {
    const b = e.target.closest("button[data-key]");
    if (!b || e.buttons) return;
    schedule(b, showWork, true);
  });
  more.addEventListener("mouseout", (e) => { if (anchor && !e.relatedTarget?.closest?.("button[data-key]")) hide(); });
  more.addEventListener("mouseleave", hide);
  // 这一排里滚轮只用来横向滚动（每格一张，连续滚动会累加）；预览的翻页留给网格里的卡片，免得两种行为混在一起
  let wheelTo = null, wheelTimer = 0;
  more.addEventListener("wheel", (e) => {
    const strip = e.target.closest?.(".more-works");
    if (!strip || e.ctrlKey || Math.abs(e.deltaX) > Math.abs(e.deltaY)) return;
    const max = strip.scrollWidth - strip.clientWidth;
    const from = wheelTo ?? strip.scrollLeft;
    if ((e.deltaY > 0 && from >= max - 1) || (e.deltaY < 0 && from <= 0)) return;   // 到头了：让信息栏照常上下滚动
    e.preventDefault();
    const step = (strip.firstElementChild?.offsetWidth || 90) + 4;
    wheelTo = Math.max(0, Math.min(max, from + Math.sign(e.deltaY) * step));
    strip.scrollTo({ left: wheelTo, behavior: "smooth" });
    clearTimeout(wheelTimer);
    wheelTimer = setTimeout(() => { wheelTo = null; }, 400);
  }, { passive: false });

  // ---------- 画师 ----------
  function artistWorks(key) {
    key = key + "|" + ctx.rating();              // 分级设置变了就重新取
    if (!artistCache.has(key)) {
      if (artistCache.size > 40) artistCache.clear();
      artistCache.set(key, ctx.api.listWorks({ scope: "artist", artist: key.split("|")[0], sort: "id", rating: ctx.rating(), tags: [], q: "", filters: {}, mergePages: true })
        .then((r) => r.works, () => []));
    }
    return artistCache.get(key);
  }
  async function showArtist(row) {
    const a = ctx.artistByKey(row.dataset.artist);
    if (!a) return;
    const works = await artistWorks(a.key);
    if (anchor !== row || !works.length) return;
    const shots = works.slice(0, ARTIST_SHOTS);
    const width = 300, cols = Math.min(3, shots.length);
    peek.className = "peek artist";
    peek.style.width = width + "px";
    peek.innerHTML = `<div class="peek-head">${ctx.avatarHTML(a)}<b>${esc(a.name)}</b><span>${fmtNum(works.length)} 个作品</span></div>
      <div class="peek-grid" style="grid-template-columns:repeat(${cols}, 1fr)">${shots.map((w) => `<img src="${esc(ctx.api.thumbSrc(w.pages[0].thumb || w.pages[0].path))}" alt="">`).join("")}</div>`;
    peek.hidden = false;
    const cell = (width - 16 - (cols - 1) * 4) / cols;
    place(row, width, 46 + Math.ceil(shots.length / cols) * (cell * 4 / 3 + 4) + 8);
  }
  const artists = $("#artists");
  artists.addEventListener("mouseover", (e) => {
    const row = e.target.closest("[data-artist]");
    if (!row || e.buttons) return;
    schedule(row, showArtist);
  });
  artists.addEventListener("mouseleave", hide);

  // ---------- 何时收起 ----------
  document.addEventListener("mousedown", hide, true);
  document.addEventListener("keydown", hide, true);
  document.addEventListener("scroll", hide, true);
  window.addEventListener("blur", hide);
  ctx.hidePeek = hide;
}

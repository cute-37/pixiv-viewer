// 看图页：画布缩放 / 平移、多页作品翻页、胶片条、信息栏、幻灯片；以及网格里的空格快速预览
import { $, $$, esc, icon, fmtDate, fmtSize, fmtNum, clamp, toast, copyText } from "./util.js";
import { createLazyLoader } from "./lazy.js";

export function initViewer(ctx) {
  const { view } = ctx;
  const viewerEl = $("#viewer"), stage = $("#stage"), canvas = $("#canvas"), img = $("#v-img");
  const V = { open: false, index: 0, page: 0, scale: 1, cx: 0, cy: 0, rot: 0, fit: true, nw: 0, nh: 0, playing: false, timer: null, idleTimer: null };

  const work = () => view.works[V.index];
  const page = () => work()?.pages[V.page];

  // ---------- 打开 / 关闭 ----------
  ctx.openViewer = (index, pageNo = 0) => {
    if (index < 0 || index >= view.works.length) return;
    V.open = true;
    viewerEl.hidden = false;
    $("#main").style.visibility = "hidden";
    viewerEl.classList.toggle("no-info", !ctx.S.infoOpen);
    show(index, pageNo);
  };
  ctx.closeViewer = (silent) => {
    if (!V.open) return;
    stopSlideshow();
    V.open = false;
    pagesOv.hidden = true;
    viewerEl.hidden = true;
    viewerEl.classList.remove("zen");
    $("#main").style.visibility = "";
    if (!silent && work()) {
      const k = work().key;
      view.cur = k; view.anchor = k; view.sel = new Set([k]);
      ctx.refreshSelection();
      const tile = ctx.ensureTile(k);
      if (tile) tile.scrollIntoView({ block: "nearest" });
    }
  };
  ctx.viewerOpen = () => V.open;
  ctx.onWorks = () => { if (V.open) { const w = work(); if (!w) ctx.closeViewer(true); else renderFilm(); } };
  ctx.onWorkChanged = () => { if (V.open) { renderInfo(); syncFav(); } };
  ctx.onSettings = () => {
    if (!V.open) return;
    viewerEl.classList.toggle("no-info", !ctx.S.infoOpen);
    applyBg();
    canvas.classList.toggle("pixel", !!ctx.S.pixelated);
  };

  function show(index, pageNo = 0) {
    V.index = clamp(index, 0, view.works.length - 1);
    const w = work();
    V.page = clamp(pageNo, 0, w.pages.length - 1);
    V.rot = 0; V.fit = ctx.S.fitOnOpen !== false;
    const p = page();
    loadImage(p);
    $("#v-name").innerHTML = `<b>${esc(w.title)}</b><span>${esc(w.artistName)} · ${esc(p.file)}</span>`;
    $("#v-idx").textContent = `${V.index + 1} / ${view.works.length}`;
    const vp = $("#v-page");
    vp.hidden = w.pages.length < 2;
    vp.innerHTML = `<button data-pg="-1" title="上一页 (↑)">${icon("up")}</button>第 ${V.page + 1} / ${w.pages.length} 页<button data-pg="1" title="下一页 (↓)">${icon("down")}</button><button data-pg="all" class="all" title="查看全部页 (G)">${icon("grid")}</button>`;
    syncPagesOverview();
    view.cur = w.key;
    syncFav();
    applyBg();
    canvas.classList.toggle("pixel", !!ctx.S.pixelated);
    renderFilm();
    renderInfo();
    ctx.api.markViewed(w.key);
    if (ctx.S.preload) preload();
    poke();
  }
  // 先显示（多半已在缓存里的）缩略图，原图在后台下载并解码好之后再换上：
  // 打开和翻页立刻有画面，换图那一下也不会卡住界面
  let imgToken = 0;
  function loadImage(p) {
    const token = ++imgToken;
    const fullUrl = ctx.api.imageUrl(p.path);
    const setSize = (w, h) => { V.nw = w; V.nh = h; img.style.width = w + "px"; img.style.height = h + "px"; };
    const place = () => { if (V.fit) fitNow(); else actual(); };
    let fullShown = false;
    const thumb = new Image();
    thumb.onload = () => {
      if (token !== imgToken || fullShown) return;
      // 尺寸已知时按原图尺寸摆放（缩略图被拉伸显示），之后换成原图时位置和大小都不变
      const w = p.w || thumb.naturalWidth, h = p.h || thumb.naturalHeight;
      img.src = thumb.src; setSize(w, h); place(); img.style.opacity = "1";
    };
    img.style.opacity = "0";
    thumb.src = ctx.api.thumbSrc(p.thumb || p.path);
    const full = new Image();
    full.decoding = "async";
    full.src = fullUrl;
    const done = () => {
      if (token !== imgToken) return;
      fullShown = true;
      const shown = V.nw ? V.scale * V.nw : 0;      // 占位图当前显示的宽度
      img.src = fullUrl;
      setSize(full.naturalWidth, full.naturalHeight);
      if (V.fit || !shown) place(); else { V.scale = shown / V.nw; apply(); }
      img.style.opacity = "1";
      if (!p.w && token === imgToken) renderInfo();   // 尺寸原先未知，现在可以显示了
    };
    (full.decode ? full.decode() : Promise.reject()).then(done, () => { full.complete && full.naturalWidth ? done() : (full.onload = done); });
  }
  function preload() {
    const next = [];
    const w = work();
    if (w.pages[V.page + 1]) next.push(w.pages[V.page + 1].path);
    if (view.works[V.index + 1]) next.push(view.works[V.index + 1].pages[0].path);
    if (view.works[V.index - 1]) next.push(view.works[V.index - 1].pages[0].path);
    next.forEach((p) => { const im = new Image(); im.src = ctx.api.imageUrl(p); });
  }
  const goWork = (d) => { const i = V.index + d; if (i >= 0 && i < view.works.length) show(i); else toast(d > 0 ? "已经是最后一个作品" : "已经是第一个作品"); };
  const goPage = (d) => {
    const w = work(), p = V.page + d;
    if (p >= 0 && p < w.pages.length) show(V.index, p);
    else if (d > 0) goWork(1);
    else if (V.index > 0) { const prev = view.works[V.index - 1]; show(V.index - 1, prev.pages.length - 1); }
  };

  // ---------- 缩放 / 平移 ----------
  let boxCache = null;
  function box() {
    if (!boxCache) { const r = canvas.getBoundingClientRect(); boxCache = { w: r.width, h: r.height, left: r.left, top: r.top }; }
    return boxCache;
  }
  function dims() { return V.rot % 180 ? [V.nh, V.nw] : [V.nw, V.nh]; }
  // 拖动 / 滚轮一帧里可能来好几个事件：合并成每帧只改一次 transform（只走合成，不重新排版）
  let applyQueued = false, zoomText = "";
  img.style.transformOrigin = "50% 50%";
  function apply() {
    if (applyQueued) return;
    applyQueued = true;
    requestAnimationFrame(() => {
      applyQueued = false;
      img.style.transform = `translate(${V.cx - V.nw / 2}px, ${V.cy - V.nh / 2}px) rotate(${V.rot}deg) scale(${V.scale})`;
      const t = V.fit ? "适应" : Math.round(V.scale * 100) + "%";
      if (t !== zoomText) { zoomText = t; $("#v-zoom").textContent = t; }
    });
  }
  function fitNow() {
    const b = box(), [w, h] = dims();
    if (!w || !h) return;
    V.scale = Math.min((b.w - 48) / w, (b.h - 48) / h);
    V.cx = b.w / 2; V.cy = b.h / 2; V.fit = true;
    apply();
  }
  function actual(mx, my) { zoomTo(1, mx, my); }
  function zoomTo(s, mx, my) {
    const b = box();
    if (mx == null) { mx = b.w / 2; my = b.h / 2; }
    s = clamp(s, 0.05, 16);
    V.cx = mx - (mx - V.cx) * (s / V.scale);
    V.cy = my - (my - V.cy) * (s / V.scale);
    V.scale = s; V.fit = false;
    apply();
  }
  const zoomBy = (f, mx, my) => zoomTo(V.scale * f, mx, my);
  new ResizeObserver(() => { boxCache = null; if (V.open && V.fit) fitNow(); }).observe(canvas);

  canvas.addEventListener("wheel", (e) => {
    e.preventDefault();
    const b = box();
    if (ctx.S.wheel === "page" && !e.ctrlKey) { goPage(e.deltaY > 0 ? 1 : -1); return; }
    zoomBy(e.deltaY < 0 ? 1.15 : 1 / 1.15, e.clientX - b.left, e.clientY - b.top);
  }, { passive: false });
  let drag = null;
  canvas.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    drag = { x: e.clientX, y: e.clientY, cx: V.cx, cy: V.cy, moved: false };
    canvas.setPointerCapture(e.pointerId);
    canvas.classList.add("dragging");
  });
  canvas.addEventListener("pointermove", (e) => {
    if (!drag) return;
    const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
    if (Math.abs(dx) + Math.abs(dy) > 3) drag.moved = true;
    V.cx = drag.cx + dx; V.cy = drag.cy + dy; V.fit = false;
    apply();
  });
  canvas.addEventListener("pointerup", () => { drag = null; canvas.classList.remove("dragging"); });
  canvas.addEventListener("dblclick", (e) => {
    const b = box(), mode = ctx.S.doubleClick;
    if (mode === "back") return ctx.closeViewer();
    if (mode === "fullscreen") return toggleZen();
    V.fit ? actual(e.clientX - b.left, e.clientY - b.top) : fitNow();
  });

  // ---------- 控件 ----------
  $("#v-back").addEventListener("click", () => ctx.closeViewer());
  $("#v-prev").addEventListener("click", () => goWork(-1));
  $("#v-next").addEventListener("click", () => goWork(1));
  $("#v-page").addEventListener("click", (e) => {
    const b = e.target.closest("[data-pg]"); if (!b) return;
    if (b.dataset.pg === "all") togglePages(); else goPage(+b.dataset.pg);
  });

  // ---------- 多页作品：全部页总览 ----------
  const pagesOv = $("#pages-ov");
  const pageThumbs = createLazyLoader(pagesOv, (src) => ctx.api.thumbSrc(src), "300px 0px");
  let pagesFor = null;
  function togglePages(force) {
    const w = work();
    const open = force ?? pagesOv.hidden;
    if (!open || !w || w.pages.length < 2) { pagesOv.hidden = true; return; }
    stopSlideshow();
    if (pagesFor !== w) {
      pagesFor = w;
      pageThumbs.reset();
      pagesOv.innerHTML = `<div class="po-head"><b>${esc(w.title)}</b><span>共 ${w.pages.length} 页 · 点击跳转</span><span class="sp"></span><button class="btn ghost" data-po-close>关闭 (Esc)</button></div>
        <div class="po-grid">${w.pages.map((p, i) => `<button data-po="${i}" style="--ar:${p.w && p.h ? (p.w / p.h).toFixed(4) : 0.75}" title="第 ${i + 1} 页 · ${esc(p.file)}"><img data-src="${esc(p.thumb || p.path)}" alt=""><span class="no">${i + 1}</span></button>`).join("")}</div>`;
      pageThumbs.observe(pagesOv);
    }
    pagesOv.hidden = false;
    syncPagesOverview(true);
  }
  // 标出当前页；切到别的作品时收起
  function syncPagesOverview(scroll) {
    if (pagesOv.hidden) return;
    if (pagesFor !== work()) { pagesOv.hidden = true; return; }
    const old = pagesOv.querySelector(".here"); if (old) old.classList.remove("here");
    const el = pagesOv.querySelector(`[data-po="${V.page}"]`);
    if (el) { el.classList.add("here"); if (scroll) el.scrollIntoView({ block: "center" }); }
  }
  pagesOv.addEventListener("click", (e) => {
    const b = e.target.closest("[data-po]");
    if (b) { pagesOv.hidden = true; show(V.index, +b.dataset.po); return; }
    if (e.target.closest("[data-po-close]") || e.target === pagesOv) pagesOv.hidden = true;
  });
  pagesOv.addEventListener("wheel", (e) => e.stopPropagation());
  $(".vbar").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-v]"); if (!b) return;
    const act = b.dataset.v;
    if (act === "zin") zoomBy(1.25);
    if (act === "zout") zoomBy(0.8);
    if (act === "zoomreset") V.fit ? actual() : fitNow();
    if (act === "rotate") { V.rot = (V.rot + 90) % 360; V.fit ? fitNow() : apply(); }
    if (act === "bg") { const order = ["canvas", "black", "checker"]; ctx.setSetting({ viewerBg: order[(order.indexOf(ctx.S.viewerBg) + 1) % 3] }); applyBg(); }
    if (act === "fav") ctx.setFav([work().key], !work().fav);
    if (act === "slideshow") V.playing ? stopSlideshow() : startSlideshow();
    if (act === "info") toggleInfo();
  });
  function applyBg() { stage.classList.toggle("bg-black", ctx.S.viewerBg === "black"); stage.classList.toggle("bg-checker", ctx.S.viewerBg === "checker"); }
  function syncFav() { $("#v-fav").classList.toggle("on", !!work()?.fav); }
  function toggleInfo() { ctx.setSetting({ infoOpen: !ctx.S.infoOpen }); $("#v-info").classList.toggle("on", ctx.S.infoOpen); requestAnimationFrame(() => V.fit && fitNow()); }
  function toggleZen() { viewerEl.classList.toggle("zen"); requestAnimationFrame(() => V.fit && fitNow()); }

  // 鼠标静止时隐藏控件
  function poke() {
    stage.classList.remove("idle");
    clearTimeout(V.idleTimer);
    V.idleTimer = setTimeout(() => { if (!stage.matches(":hover") || !$$(".vbar, .vtop", stage).some((el) => el.matches(":hover"))) stage.classList.add("idle"); }, 2200);
  }
  stage.addEventListener("pointermove", poke);
  stage.addEventListener("pointerleave", () => stage.classList.remove("idle"));

  // ---------- 幻灯片 ----------
  function startSlideshow() {
    V.playing = true;
    $("#v-play use").setAttribute("href", "#i-pause");
    $("#v-play").classList.add("on");
    tick();
    toast(`幻灯片：每 ${ctx.S.slideInterval} 秒切换 · 空格暂停`);
  }
  function tick() {
    clearTimeout(V.timer);
    const bar = $("#slide-progress"), fill = bar.firstElementChild;
    bar.hidden = false;
    fill.style.transition = "none"; fill.style.width = "0";
    requestAnimationFrame(() => { fill.style.transition = `width ${ctx.S.slideInterval}s linear`; fill.style.width = "100%"; });
    V.timer = setTimeout(() => {
      const w = work();
      if (V.page + 1 < w.pages.length) show(V.index, V.page + 1);
      else show(V.index + 1 < view.works.length ? V.index + 1 : 0);
      if (V.playing) tick();
    }, ctx.S.slideInterval * 1000);
  }
  function stopSlideshow() {
    if (!V.playing) return;
    V.playing = false;
    clearTimeout(V.timer);
    $("#slide-progress").hidden = true;
    $("#v-play use").setAttribute("href", "#i-play");
    $("#v-play").classList.remove("on");
  }

  // ---------- 胶片条 ----------
  const filmThumbs = createLazyLoader($("#film"), (src) => ctx.api.thumbSrc(src), "0px 600px");
  // 只放当前作品前后各 FILM_HALF 个：作品上万时一次建好全部按钮，打开看图页会卡好几秒
  const FILM_HALF = 40;
  let filmFor = null, filmLo = 0, filmHi = 0;
  function renderFilm() {
    const film = $("#film"), n = view.works.length;
    const nearEdge = (V.index < filmLo + 8 && filmLo > 0) || (V.index >= filmHi - 8 && filmHi < n);
    if (filmFor !== view.works || V.index < filmLo || V.index >= filmHi || nearEdge) {
      filmFor = view.works;
      filmLo = Math.max(0, V.index - FILM_HALF); filmHi = Math.min(n, V.index + FILM_HALF + 1);
      filmThumbs.reset();
      let html = "";
      for (let i = filmLo; i < filmHi; i++) {
        const w = view.works[i];
        html += `<button data-i="${i}" title="${esc(w.title)}"><img data-src="${esc(w.pages[0].thumb || w.pages[0].path)}" alt="">${w.pages.length > 1 ? `<span class="pg">${w.pages.length}</span>` : ""}</button>`;
      }
      film.innerHTML = html;
      filmThumbs.observe(film);
    }
    const prev = film.querySelector(".on");
    if (prev) prev.classList.remove("on");
    const on = film.children[V.index - filmLo];
    if (on) { on.classList.add("on"); on.scrollIntoView({ block: "nearest", inline: "center" }); }
  }
  $("#film").addEventListener("click", (e) => { const b = e.target.closest("button[data-i]"); if (b) show(+b.dataset.i); });
  $("#film").addEventListener("wheel", (e) => { if (Math.abs(e.deltaY) > Math.abs(e.deltaX)) { e.preventDefault(); $("#film").scrollLeft += e.deltaY; } }, { passive: false });

  // ---------- 信息栏 ----------
  let infoToken = 0;
  async function renderInfo() {
    const w = work(); if (!w) return;
    const token = ++infoToken;
    const p = page();
    const a = ctx.artistByKey(w.artistKey) || { name: w.artistName, id: w.artistId };
    const info = $("#info-main");
    $("#v-info").classList.toggle("on", !!ctx.S.infoOpen);
    info.innerHTML = basicInfo(w, p, a, null);
    renderMore(w, a);
    const d = await ctx.api.getDetails(w.key);
    if (token !== infoToken || !d) return;
    info.innerHTML = basicInfo(w, p, a, d);
    const box = $("#v-tags"), more = $("#v-tags-more");
    if (box && more) more.hidden = box.scrollHeight <= box.clientHeight + 2;
  }
  function linkify(text) {
    return esc(text).replace(/https?:\/\/[^\s<]+/g, (u) => `<a href="#" data-url="${u}">${u}</a>`).replace(/\n/g, "<br>");
  }
  // 多页作品在信息栏里直接列出每一页的小图：一眼看全，点哪张看哪张
  const PAGES_INLINE = 12;
  function pagesBlock(w) {
    const n = w.pages.length;
    if (n < 2) return "";
    const shown = n > PAGES_INLINE ? PAGES_INLINE - 1 : n;
    let cells = "";
    for (let i = 0; i < shown; i++) {
      const pg = w.pages[i];
      cells += `<button data-page="${i}" class="${i === V.page ? "here" : ""}" title="第 ${i + 1} 页"><img src="${esc(ctx.api.thumbSrc(pg.thumb || pg.path))}" alt="" loading="lazy"><span class="no">${i + 1}</span></button>`;
    }
    if (shown < n) cells += `<button data-page="all" class="rest" title="查看全部 ${n} 页 (G)">+${n - shown}</button>`;
    return `<div class="blk"><span class="lbl">本作品 · ${n} 页<span class="sp"></span><button class="linkbtn" data-page="all">全部展开</button></span><div class="page-grid">${cells}</div></div>`;
  }
  function basicInfo(w, p, a, d) {
    const stars = [1, 2, 3, 4, 5].map((n) => `<button data-star="${n}" class="${n <= w.stars ? "on" : ""}" title="${n} 星（再点一次清除）">${icon("star")}</button>`).join("");
    const pixivTags = (d ? d.tags : w.tags || []).map((t) => `<button class="tag" data-t="${esc(t)}" title="按此标签筛选">${esc(t)}</button>`).join("");
    const myTags = (d?.myTags || []).map((t) => `<span class="tag mine">${esc(t)}<button class="x" data-rm="${esc(t)}" title="移除标签" aria-label="移除标签 ${esc(t)}">×</button></span>`).join("");
    return `
      <div class="blk"><h2>${esc(d?.title || w.title)}</h2>
        <button class="who" data-act="artist" title="打开 ${esc(a.name)} 的画师主页">${ctx.avatarHTML(a)}<div class="nm"><b>${esc(a.name)}</b><small>ID ${a.id}</small></div>${icon("right", "go")}</button></div>
      ${pagesBlock(w)}
      <div class="blk"><span class="lbl">我的评分<span class="sp"></span></span><div class="stars" id="v-stars">${stars}</div></div>
      <dl class="kv">
        <dt>作品 ID</dt><dd>${w.pid}</dd>
        <dt>尺寸</dt><dd>${p.w ? `${p.w} × ${p.h}` : $("#v-img").naturalWidth ? `${$("#v-img").naturalWidth} × ${$("#v-img").naturalHeight}` : "—"}</dd>
        <dt>大小</dt><dd>${fmtSize(p.size)}</dd>
        ${w.pages.length > 1 ? `<dt>页数</dt><dd>${w.pages.length} 页（当前第 ${V.page + 1} 页）</dd>` : ""}
        <dt>投稿</dt><dd>${fmtDate(w.posted)}</dd>
        <dt>分级</dt><dd>${w.rating === "safe" ? "全年龄" : w.rating.toUpperCase()}${w.ai ? " · AI 生成" : ""}</dd>
        ${d?.bookmarks ? `<dt>收藏 / 浏览</dt><dd>${fmtNum(d.bookmarks)} / ${fmtNum(d.views)}</dd>` : ""}
        <dt>文件</dt><dd>${esc(p.file)}</dd>
      </dl>
      <div class="blk"><span class="lbl">Pixiv 标签 · ${(d ? d.tags : w.tags || []).length}</span>
        <div class="tags clamp" id="v-tags">${pixivTags}</div>
        <button class="linkbtn" id="v-tags-more" style="align-self:flex-start" hidden>展开全部</button></div>
      <div class="blk"><span class="lbl">我的标签</span>
        <div class="tags">${myTags}<span class="tag-add">${icon("plus", "s")}<input id="v-tag-add" placeholder="添加标签" autocomplete="off" spellcheck="false" aria-label="添加标签"><div class="tag-sug" id="v-tag-sug" hidden role="listbox"></div></span></div></div>
      ${d?.caption ? `<div class="blk"><span class="lbl">作品说明</span><p class="desc">${linkify(d.caption)}</p></div>` : ""}
      <div class="acts">
        <button class="btn primary" data-act="pixiv">${icon("ext")}在 Pixiv 打开</button>
        <button class="btn" data-act="reveal" title="在资源管理器中显示">${icon("folder")}</button>
        <button class="btn" data-act="copy" title="复制路径">${icon("copy")}</button>
        <button class="btn" data-act="wallpaper" title="设为桌面壁纸">${icon("wall")}</button>
      </div>`;
  }

  // ---------- 同一画师的作品：一排三张，可左右翻页 ----------
  const artistWorks = new Map();   // 画师 key -> 作品列表（Promise）
  let moreIndex = new Map();       // 当前这一排里的作品：key -> 作品
  ctx.moreWorkByKey = (k) => moreIndex.get(k);
  let moreFor = null;
  function worksOf(artistKey) {
    // 当前列表本来就是这位画师的作品时直接用；否则向后端要（结果后端有缓存）
    if (view.scope === "artist" && view.artist === artistKey && !view.q && !view.tags.size) return Promise.resolve(view.works);
    if (!artistWorks.has(artistKey)) {
      if (artistWorks.size > 20) artistWorks.clear();
      artistWorks.set(artistKey, ctx.api.listWorks({ scope: "artist", artist: artistKey, sort: "id", rating: ctx.S.showR18 === false ? "safe" : "all", tags: [], q: "", filters: {}, mergePages: true })
        .then((r) => r.works, () => []));
    }
    return artistWorks.get(artistKey);
  }
  async function renderMore(w, a) {
    const box = $("#info-more");
    const base = w.key.split("#")[0];
    if (moreFor === w.artistKey && box.querySelector("[data-key]")) return markHere(box, base);
    moreFor = w.artistKey;
    box.hidden = true;
    const list = await worksOf(w.artistKey);
    if (moreFor !== w.artistKey || work()?.artistKey !== w.artistKey) return;
    if (list.length < 2) { box.innerHTML = ""; return; }
    box.innerHTML = `<div class="mw-head"><span class="lbl">${esc(a.name)} 的作品 · ${fmtNum(list.length)}</span></div>
      <div class="mw-wrap">
        <div class="more-works" id="more-works">${list.map((x) => `<button data-key="${esc(x.key.split("#")[0])}" title="${esc(x.title)}"><img data-src="${esc(x.pages[0].thumb || x.pages[0].path)}" alt="">${x.pages.length > 1 ? `<span class="pg">${x.pages.length}</span>` : ""}</button>`).join("")}</div>
        <button class="mw-arrow l" data-mw="-1" title="上一组" aria-label="上一组">${icon("left")}</button>
        <button class="mw-arrow r" data-mw="1" title="下一组" aria-label="下一组">${icon("right")}</button>
        <div class="mw-bar" id="mw-bar" title="拖动快速浏览"><i></i></div>
      </div>`;
    box.hidden = false;
    moreIndex = new Map(list.map((x) => [x.key.split("#")[0], x]));
    moreThumbs.reset();
    moreThumbs.observe(box);
    markHere(box, base);
  }
  // 标出当前作品，并把它滚到这一排的最左边（后面跟着的就是相邻的作品）
  function markHere(box, base) {
    const strip = $("#more-works"); if (!strip) return;
    const old = strip.querySelector(".here"); if (old) old.classList.remove("here");
    const el = strip.querySelector(`[data-key="${CSS.escape(base)}"]`);
    if (el) { el.classList.add("here"); strip.scrollLeft += el.getBoundingClientRect().left - strip.getBoundingClientRect().left; }
    syncMoreArrows();
  }
  function syncMoreArrows() {
    const strip = $("#more-works"); if (!strip) return;
    const [prev, next] = $$("#info-more [data-mw]");
    prev.disabled = strip.scrollLeft <= 1;
    next.disabled = strip.scrollLeft + strip.clientWidth >= strip.scrollWidth - 1;
    // 自绘滚动条：滑块长度 = 可见部分占比，位置跟随滚动
    const bar = $("#mw-bar"), thumb = bar.firstElementChild;
    const ratio = strip.clientWidth / Math.max(1, strip.scrollWidth);
    bar.style.display = ratio >= 1 ? "none" : "";
    const w = Math.max(18, bar.clientWidth * ratio);
    thumb.style.width = w + "px";
    thumb.style.left = (bar.clientWidth - w) * (strip.scrollLeft / Math.max(1, strip.scrollWidth - strip.clientWidth)) + "px";
  }
  // 滚动时短暂显示滚动条；按住滚动条可以直接拖到任意位置
  let barTimer = 0;
  $("#info-more").addEventListener("scroll", () => {
    const wrap = $(".mw-wrap"); if (!wrap) return;
    wrap.classList.add("scrolling");
    clearTimeout(barTimer);
    barTimer = setTimeout(() => wrap.classList.remove("scrolling"), 900);
  }, { passive: true, capture: true });
  $("#info-more").addEventListener("pointerdown", (e) => {
    const bar = e.target.closest(".mw-bar"); if (!bar) return;
    e.preventDefault();
    const strip = $("#more-works"), r = bar.getBoundingClientRect();
    const seek = (x) => { strip.scrollLeft = (strip.scrollWidth - strip.clientWidth) * Math.min(1, Math.max(0, (x - r.left) / r.width)); };
    strip.style.scrollSnapType = "none";      // 拖动时不吸附，松开后恢复
    bar.classList.add("drag");
    bar.setPointerCapture(e.pointerId);
    seek(e.clientX);
    const move = (ev) => seek(ev.clientX);
    const up = () => { bar.removeEventListener("pointermove", move); bar.classList.remove("drag"); strip.style.scrollSnapType = ""; };
    bar.addEventListener("pointermove", move);
    bar.addEventListener("pointerup", up, { once: true });
    bar.addEventListener("pointercancel", up, { once: true });
  });
  const moreThumbs = createLazyLoader($("#info-more"), (src) => ctx.api.thumbSrc(src), "0px 300px");
  $("#info-more").addEventListener("scroll", () => syncMoreArrows(), { passive: true, capture: true });
  $("#info").addEventListener("click", async (e) => {
    const w = work(); if (!w) return;
    const star = e.target.closest("[data-star]");
    if (star) { const n = +star.dataset.star; return ctx.setStars([w.key], w.stars === n ? 0 : n); }
    if (e.target.id === "v-tags-more") { const b = $("#v-tags"); b.classList.toggle("clamp"); e.target.textContent = b.classList.contains("clamp") ? "展开全部" : "收起"; return; }
    const t = e.target.closest(".tag[data-t]");
    if (t) { ctx.closeViewer(); ctx.filterByTag(t.dataset.t); toast(`已按“${t.dataset.t}”筛选`); return; }
    const rm = e.target.closest("[data-rm]");
    if (rm) {
      const tag = rm.dataset.rm;
      await ctx.api.removeTags([w.key], [tag]); renderInfo();
      toast(`已移除标签“${tag}”`, async () => { await ctx.api.addTags([w.key], [tag]); renderInfo(); });
      return;
    }
    const link = e.target.closest("[data-url]");
    if (link) { e.preventDefault(); ctx.api.openUrl(link.dataset.url); return; }
    const pg = e.target.closest("[data-page]");
    if (pg) { if (pg.dataset.page === "all") togglePages(true); else show(V.index, +pg.dataset.page); return; }
    const mw = e.target.closest("[data-mw]");
    if (mw) { const strip = $("#more-works"); strip.scrollBy({ left: +mw.dataset.mw * (strip.clientWidth + 4), behavior: "smooth" }); return; }
    const other = e.target.closest("[data-key]");
    if (other) {
      const k = other.dataset.key;
      const i = view.works.findIndex((x) => x.key === k || x.key.split("#")[0] === k);
      if (i >= 0) show(i);
      else { ctx.pendingOpen = k; ctx.openScope("artist", w.artistKey); }   // 不在当前列表里：到画师主页打开它
      return;
    }
    const act = e.target.closest("[data-act]");
    if (!act) return;
    const p = page();
    if (act.dataset.act === "artist") {
      // 已经在这位画师的主页里看图时，回到网格即可
      if (view.scope === "artist" && view.artist === w.artistKey) ctx.closeViewer();
      else ctx.openScope("artist", w.artistKey);
    }
    if (act.dataset.act === "pixiv") ctx.api.openUrl(`https://www.pixiv.net/artworks/${w.pid}`);
    if (act.dataset.act === "reveal") ctx.sysAction(ctx.api.reveal([p.path]));
    if (act.dataset.act === "copy") { const ok = await copyText(p.path, ctx.api); toast(ok ? "已复制路径" : "复制失败"); }
    if (act.dataset.act === "wallpaper") ctx.sysAction(ctx.api.setWallpaper(p.path), "已设为桌面壁纸");
  });
  // 添加标签：输入框下面是自己画的联想列表（用过的标签），↑ ↓ 选择、Enter 添加、Esc 取消
  let tagList = null, sugItems = [], sugAt = -1;
  function renderTagSug() {
    const inp = $("#v-tag-add"), box = $("#v-tag-sug");
    if (!inp || !box) return;
    const text = inp.value.trim().toLowerCase();
    const have = new Set($$("#info .tag.mine").map((el) => el.firstChild.textContent));
    sugItems = (tagList || []).filter((t) => !have.has(t) && (!text || t.toLowerCase().includes(text))).slice(0, 8);
    sugAt = Math.min(sugAt, sugItems.length - 1);
    box.hidden = !sugItems.length || document.activeElement !== inp;
    box.innerHTML = sugItems.map((t, i) => `<div class="opt ${i === sugAt ? "act" : ""}" data-sug="${i}" role="option">${esc(t)}</div>`).join("");
  }
  async function addTag(tag) {
    tag = tag.trim(); if (!tag) return;
    await ctx.api.addTags([work().key], [tag]);
    if (tagList && !tagList.includes(tag)) tagList.push(tag);
    await renderInfo();
    toast(`已添加标签“${tag}”`);
    const inp = $("#v-tag-add"); if (inp) inp.focus();
  }
  $("#info").addEventListener("focusin", async (e) => {
    if (e.target.id !== "v-tag-add") return;
    sugAt = -1;
    if (!tagList) tagList = await ctx.api.myTagList();
    renderTagSug();
  });
  $("#info").addEventListener("focusout", (e) => { if (e.target.id === "v-tag-add") { const box = $("#v-tag-sug"); if (box) box.hidden = true; } });
  $("#info").addEventListener("input", (e) => { if (e.target.id === "v-tag-add") { sugAt = -1; renderTagSug(); } });
  // 用 mousedown 而不是 click：点击列表项时输入框还没失去焦点
  $("#info").addEventListener("mousedown", (e) => {
    const opt = e.target.closest("[data-sug]"); if (!opt) return;
    e.preventDefault();
    addTag(sugItems[+opt.dataset.sug]);
  });
  $("#info").addEventListener("keydown", async (e) => {
    if (e.target.id !== "v-tag-add") return;
    if (e.key === "Escape") { e.target.blur(); e.stopPropagation(); return; }
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault(); e.stopPropagation();
      if (sugItems.length) { sugAt = (sugAt + (e.key === "ArrowDown" ? 1 : -1) + sugItems.length + (sugAt < 0 && e.key === "ArrowUp" ? 1 : 0)) % sugItems.length; renderTagSug(); }
      return;
    }
    if (e.key !== "Enter") return;
    addTag(sugAt >= 0 ? sugItems[sugAt] : e.target.value);
  });

  // ---------- 键盘 ----------
  ctx.viewerOpen = () => V.open;
  ctx.handleViewerStar = (n) => { if (V.open) ctx.setStars([work().key], n); };
  // 看图页里的快捷键动作（按什么键由 keymap.js 和设置决定）
  ctx.handleViewerAction = (id, combo) => {
    if (!V.open) return false;
    if (id === "v.close") {
      if (!pagesOv.hidden) pagesOv.hidden = true;
      else if (viewerEl.classList.contains("zen")) toggleZen();
      else if (V.playing) stopSlideshow();
      else ctx.closeViewer();
    }
    else if (id === "v.pages") togglePages();
    else if (id === "v.prev") goWork(-1);
    else if (id === "v.next") goWork(1);
    else if (id === "v.pageUp") goPage(-1);
    else if (id === "v.pageDown") { if (V.playing && combo === "Space") stopSlideshow(); else goPage(1); }
    else if (id === "v.first") show(0);
    else if (id === "v.last") show(view.works.length - 1);
    else if (id === "v.zoomIn") zoomBy(1.25);
    else if (id === "v.zoomOut") zoomBy(0.8);
    else if (id === "v.fit") fitNow();
    else if (id === "v.actual") actual();
    else if (id === "v.rotate") { V.rot = (V.rot + 90) % 360; V.fit ? fitNow() : apply(); }
    else if (id === "v.fav") ctx.setFav([work().key], !work().fav);
    else if (id === "v.info") toggleInfo();
    else if (id === "v.slide") V.playing ? stopSlideshow() : startSlideshow();
    else if (id === "v.zen") toggleZen();
    else if (id === "v.copy") copyText(page().path, ctx.api).then((ok) => toast(ok ? "已复制路径" : "复制失败"));
    else return false;
    return true;
  };

  // ---------- 空格快速预览 ----------
  const ql = $("#quicklook");
  ctx.quicklookOpen = () => !ql.hidden;
  ctx.openQuicklook = (key) => {
    const w = ctx.workByKey(key); if (!w) return;
    const p = w.pages[0];
    $("#ql-img").src = ctx.api.imageUrl(p.path);
    $("#ql-cap").textContent = `${w.title} · ${w.artistName}${p.w ? ` · ${p.w}×${p.h}` : ""}${w.pages.length > 1 ? ` · ${w.pages.length} 页` : ""}　空格关闭 · Enter 打开 · ← → 切换`;
    ql.hidden = false;
  };
  ql.addEventListener("click", () => { ql.hidden = true; });
  ctx.handleQuicklookKey = (e) => {
    if (ql.hidden) return false;
    if (e.key === " " || e.key === "Escape") { ql.hidden = true; e.preventDefault(); return true; }
    if (e.key === "Enter") { ql.hidden = true; ctx.openViewer(view.flat.indexOf(view.cur)); e.preventDefault(); return true; }
    return false; // 方向键交给网格处理，预览会跟着更新
  };
}

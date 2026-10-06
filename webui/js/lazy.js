// 按需加载缩略图：进入可视范围才开始下载；还没下载完就滚出范围时取消请求，
// 这样快速滚动不会积压一大串请求，也不会挤占看图页的原图下载。
export function createLazyLoader(root, resolve, margin = "500px 0px") {
  const io = new IntersectionObserver((entries) => {
    for (const e of entries) {
      const img = e.target;
      if (e.isIntersecting) {
        if (!img.getAttribute("src")) img.src = resolve(img.dataset.src);
      } else if (!img.classList.contains("ok") && img.getAttribute("src")) {
        img.removeAttribute("src");   // 取消尚未完成的下载
      }
    }
  }, { root, rootMargin: margin });

  root.addEventListener("load", (e) => {
    const img = e.target;
    if (img.tagName === "IMG" && img.getAttribute("src")) {
      img.classList.add("ok");
      io.unobserve(img);            // 已加载完的不再需要跟踪
    }
  }, true);

  return {
    observe(scope) { scope.querySelectorAll("img[data-src]").forEach((img) => io.observe(img)); },
    observeAll(imgs) { for (const img of imgs) io.observe(img); },
    reset() { io.disconnect(); },
  };
}

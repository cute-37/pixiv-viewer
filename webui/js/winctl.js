import { $, debounce } from "./util.js";

// 桌面版窗口没有系统标题栏：右上角三个按钮 + 顶部空白处拖动窗口、双击最大化
export function initWindowControls(api, dragSelector) {
  const act = api.windowAction;
  if (!act) return;
  document.documentElement.classList.add("desktop");
  $("#winctl").hidden = false;
  const setMax = (on) => {
    document.documentElement.classList.toggle("maximized", !!on);
    $("#win-max").title = on ? "还原" : "最大化";
  };
  const sync = debounce(async () => setMax(await act("state")), 150);
  $("#winctl").addEventListener("click", async (e) => {
    const b = e.target.closest("[data-win]"); if (!b) return;
    const on = await act(b.dataset.win);
    if (b.dataset.win === "toggle") setMax(on);
  });
  document.addEventListener("mousedown", (e) => {
    if (e.button !== 0) return;
    const edge = e.target.closest("[data-edge]");
    if (edge) { e.preventDefault(); act("resize:" + edge.dataset.edge); return; }
    if (!e.target.closest(dragSelector)) return;
    if (e.target.closest("button, input, select, textarea, a, .search, .seg, .suggest")) return;
    e.preventDefault();
    if (e.detail === 2) return void act("toggle").then(setMax);
    if (e.detail !== 1) return;
    act("drag");
    // 系统接管拖动后，页面在松开鼠标前收不到鼠标移动；如果还能收到，说明没接管成功，改用备用方式
    const t0 = performance.now();
    const onMove = (ev) => {
      if (!(ev.buttons & 1)) return stop();
      if (performance.now() - t0 > 150) { stop(); act("follow"); }
    };
    const stop = () => { document.removeEventListener("mousemove", onMove); document.removeEventListener("mouseup", stop); };
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", stop);
  });
  window.addEventListener("resize", sync);
  sync();
}

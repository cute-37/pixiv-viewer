import { $, debounce } from "./util.js";

// 桌面版窗口没有系统标题栏：右上角三个按钮 + 顶部空白处拖动窗口、双击最大化
// prefs = { get(): "ask" | "tray" | "exit", set(值) }：关闭窗口时怎么办。不给的话按“每次询问”，记住的选择不保存。
export function initWindowControls(api, dragSelector, prefs) {
  const act = api.windowAction;
  if (!act) return;
  // ---- 关闭：按设置放到托盘 / 退出，或者问一下
  let asking = null;
  function askClose() {
    if (asking) return asking;
    asking = new Promise((resolve) => {
      const box = document.createElement("div");
      box.className = "closeask";
      box.innerHTML = `<div class="closeask-card" role="dialog" aria-label="关闭窗口">
        <h3>要退出，还是放到托盘？</h3>
        <p>放到托盘后程序继续在后台运行，正在进行的下载不会中断；点托盘里的图标就能回来。退出则会在处理完当前文件后停止下载。</p>
        <label class="closeask-remember"><input type="checkbox"> 记住我的选择，下次不再询问<small>以后可以在“设置”里改</small></label>
        <div class="closeask-actions"><button class="btn ghost" data-choice="">取消</button><span class="sp"></span>
          <button class="btn" data-choice="exit">退出</button><button class="btn primary" data-choice="tray">放到托盘</button></div></div>`;
      const done = (choice) => {
        const remember = box.querySelector("input").checked;
        document.removeEventListener("keydown", onKey, true);
        box.remove(); asking = null;
        resolve({ choice, remember });
      };
      const onKey = (e) => { if (e.key === "Escape") { e.stopPropagation(); e.preventDefault(); done(""); } };
      box.addEventListener("click", (e) => { const b = e.target.closest("[data-choice]"); if (b) done(b.dataset.choice); else if (e.target === box) done(""); });
      document.addEventListener("keydown", onKey, true);
      document.body.append(box);
      box.querySelector('[data-choice="tray"]').focus();
    });
    return asking;
  }
  async function requestClose() {
    const pref = (prefs && prefs.get()) || "ask";
    if (pref === "tray") return act("tray");
    if (pref === "exit") return act("close");
    const { choice, remember } = await askClose();
    if (!choice) return false;
    if (remember && prefs) prefs.set(choice);
    return act(choice === "tray" ? "tray" : "close");
  }
  window.__pvRequestClose = requestClose;      // 从任务栏、Alt+F4 关窗口时，后端会调用它
  document.documentElement.classList.add("desktop");
  $("#winctl").hidden = false;
  const setMax = (on) => {
    document.documentElement.classList.toggle("maximized", !!on);
    $("#win-max").title = on ? "还原" : "最大化";
  };
  const sync = debounce(async () => setMax(await act("state")), 150);
  $("#winctl").addEventListener("click", async (e) => {
    const b = e.target.closest("[data-win]"); if (!b) return;
    if (b.dataset.win === "close") return void requestClose();
    const on = await act(b.dataset.win);
    if (b.dataset.win === "toggle") setMax(on);
  });
  document.addEventListener("mousedown", (e) => {
    if (e.button !== 0) return;
    const edge = e.target.closest("[data-edge]");
    if (edge) { e.preventDefault(); act("resize:" + edge.dataset.edge); return; }
    if (!e.target.closest(dragSelector)) return;
    if (e.target.closest("button, input, select, textarea, a, [role=link], .search, .seg, .suggest")) return;   // 能点的东西不当作拖动窗口的把手
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

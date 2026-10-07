// 设置：保存、应用到界面
import { $, $$, debounce } from "./util.js";
import { applySettings, withThemeFade } from "./settings.js";
import { app, ctx, view } from "./state.js";
import { loadWorks, renderGrid } from "./grid.js";

// ================= 设置 =================
export const persist = debounce(() => ctx.api.saveConfig(ctx.S), 400);
ctx.setSetting = (patch, opts = {}) => {
  Object.assign(ctx.S, patch);
  withThemeFade(ctx.S, () => {
    applySettings(ctx.S);
    syncChrome();
    if (opts.reload) loadWorks();
    else if (opts.grid) renderGrid();
    persist();
    ctx.onSettings && ctx.onSettings();
  });
};

// 设置里关掉“显示 R18 内容”后，所有地方都只取全年龄作品
ctx.rating = () => (ctx.S.showR18 === false ? "safe" : ctx.S.rating);

export function syncChrome() {
  const S = ctx.S;
  $("#rating").hidden = S.showR18 === false;
  app.classList.toggle("collapsed", !!S.sidebarCollapsed);
  app.classList.toggle("no-status", S.status === "off");
  $("#tagstrip").hidden = S.tagchips === "off";
  $("#btn-mode use").setAttribute("href", document.documentElement.dataset.theme === "dark" ? "#i-sun" : "#i-moon");
  $$("#sort button").forEach((b) => b.classList.toggle("on", b.dataset.v === S.sort));
  $$("#rating button").forEach((b) => b.classList.toggle("on", b.dataset.v === S.rating));
  $$("#layout-quick button").forEach((b) => b.classList.toggle("on", b.dataset.v === S.layout));
  $("#size-quick").value = S.tile;
  const active = Object.values(view.filters).some(Boolean);
  $("#btn-filters").classList.toggle("on", active);
}

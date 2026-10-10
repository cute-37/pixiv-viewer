// 独立的 Pixiv 下载器：把“下载与更新”面板当作整个程序的界面
import { initToast } from "./util.js";
import { normalize, applySettings } from "./settings.js";
import { connect } from "./api.js";
import { initDownloader } from "./downloader.js";
import { initWindowControls } from "./winctl.js";
import { setLang } from "./i18n.js";

async function boot() {
  // 图标都画在查看器主页面里的一张图标表上，这里直接取来用，不另存一份
  const doc = new DOMParser().parseFromString(await (await fetch("index.html")).text(), "text/html");
  document.body.prepend(document.importNode(doc.querySelector("svg"), true));
  initToast();
  const api = await connect();
  const S = normalize(await api.getConfig());
  await setLang(new URLSearchParams(location.search).get("lang") || S.lang || "zh");
  applySettings(S);
  const ctx = { api, S, standalone: true, view: {}, closeDialog() {}, reloadLibrary: async () => {}, loadWorks() {},
    setSetting(patch) { Object.assign(S, patch); api.saveConfig(S); } };
  initDownloader(ctx);
  initWindowControls(api, ".dhead, .dnav .dt, [data-drag]", { get: () => S.closeAction, set: (v) => ctx.setSetting({ closeAction: v }) });
  await ctx.openDownloader("update");
}
// 启动失败时把原因显示出来，而不是留一个空白窗口
boot().catch((e) => {
  const box = document.querySelector("#scrim");
  box.innerHTML = `<div class="dl-empty" style="padding-top:120px"><b>程序没能启动</b><span></span></div>`;
  box.querySelector("span").textContent = String((e && (e.stack || e.message)) || e);
});

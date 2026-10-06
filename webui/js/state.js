// 整个界面共用的状态：各个模块都从这里拿 ctx / view
import { $ } from "./util.js";

export const app = $("#app");
export const ctx = {
  api: null,
  S: null,
  lib: null,
  view: {
    scope: "all", artist: null, q: "", tags: new Set(), filters: {},
    works: [], flat: [], scopeTags: [], sel: new Set(), cur: null, anchor: null, loading: false,
    expanded: new Map(),   // 在网格里展开了全部页的多页作品：key -> 这一组的颜色
  },
};
export const view = ctx.view;

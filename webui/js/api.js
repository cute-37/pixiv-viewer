// 后端接口：桌面版通过 pywebview 调用 Python（window.pywebview.api），浏览器预览时使用示例后端。
import { mock } from "./mock.js";

function waitForBridge(timeoutMs) {
  return new Promise((resolve) => {
    if (window.pywebview && window.pywebview.api) return resolve(true);
    // 不是在桌面窗口里运行（例如直接用浏览器打开）就不必等待
    if (!/pywebview/i.test(navigator.userAgent) && !window.__PV_DESKTOP__) return resolve(false);
    const done = () => resolve(!!(window.pywebview && window.pywebview.api));
    window.addEventListener("pywebviewready", done, { once: true });
    setTimeout(done, timeoutMs);
  });
}

// list_works 的紧凑格式还原成普通作品对象（格式见 webapp/api.py 的 _pack）
function unpackWorks(res) {
  if (!res || !res.packed) return res;
  const sep = res.sep || "\\";
  const absolute = /^([a-zA-Z]:|[\\/])/;
  const artists = res.artists.map(([key, name, id]) => ({ key, name, id, base: key.endsWith(sep) ? key : key + sep }));
  res.works = res.works.map(([key, pid, title, ai, posted, mtime, month, rating, flags, stars, pages]) => {
    const a = artists[ai];
    const pg = pages.map(([rel, w, h, size]) => {
      const path = absolute.test(rel) ? rel : a.base + rel;
      return { path, file: path.slice(Math.max(path.lastIndexOf("\\"), path.lastIndexOf("/")) + 1), w, h, size };
    });
    const p0 = pg[0];
    return {
      key: key === 0 ? String(pid) : key, pid, title: title || p0.file.replace(/\.[^.]*$/, ""),
      artistKey: a.key, artistName: a.name, artistId: a.id,
      w: p0.w, h: p0.h, ar: p0.w && p0.h ? Math.round((p0.w / p0.h) * 1e4) / 1e4 : res.defaultAr,
      posted, mtime, month, rating, ai: !!(flags & 1), fav: !!(flags & 2), stars, pages: pg,
    };
  });
  delete res.artists;
  return res;
}

function desktopApi() {
  const call = (name) => (...args) => window.pywebview.api[name](...args);
  const rawToken = window.__PV_TOKEN__ || "";
  const token = encodeURIComponent(rawToken);
  // 返回大量数据的只读接口走本机 HTTP（异步，不占用界面线程）；失败时退回 pywebview 的桥
  const http = (name) => async (...args) => {
    try {
      const r = await fetch(`/api/${name}`, { method: "POST", headers: { "Content-Type": "application/json", "X-PV-Token": rawToken }, body: JSON.stringify(args) });
      if (r.ok) return await r.json();
    } catch (e) { /* 退回下面的桥调用 */ }
    return call(name)(...args);
  };
  return {
    isMock: false,
    getConfig: call("get_config"),
    saveConfig: call("save_config"),
    // 头像来自下载器，和缩略图走同一个端口
    getLibrary: async () => {
      const lib = await http("get_library")();
      for (const a of lib.artists) if (a.avatar) a.avatar = (window.__PV_THUMB__ || "") + a.avatar;
      return lib;
    },
    // Pixiv 下载器（独立进程，经由后端转发）
    dl: call("dl"), dlInfo: call("dl_info"), dlSetHome: call("dl_set_home"), dlMigrate: call("dl_migrate"), dlRefreshLibrary: call("dl_refresh_library"),
    dlStorageLink: call("dl_storage_link"), dlLinkLibrary: call("dl_link_library"), openHome: call("open_home"),
    importPick: call("import_pick"), importApply: call("import_apply"),
    listWorks: async (q) => unpackWorks(await http("list_works")({ ...q, packed: 1 })),
    suggest: http("suggest"),     // (文字, 是否只要全年龄)
    getDetails: http("get_details"),
    setStars: call("set_stars"),
    setFavorite: call("set_favorite"),
    addTags: call("add_tags"),
    removeTags: call("remove_tags"),
    markViewed: call("mark_viewed"),
    myTagList: http("my_tag_list"),
    pinArtist: call("pin_artist"),
    folderCreate: call("folder_create"), folderRename: call("folder_rename"), folderDelete: call("folder_delete"), folderSet: call("folder_set"),
    addFolder: call("add_folder"),
    removeFolder: call("remove_folder"),
    reveal: call("reveal"),
    openExternal: call("open_external"),
    openUrl: call("open_url"),
    setWallpaper: call("set_wallpaper"),
    exportWorks: call("export_works"),
    copyText: call("copy_text"),
    pickDirectory: call("pick_directory"),
    imageUrl: (path) => `/image?t=${token}&path=${encodeURIComponent(path)}`,
    // 作品列表里只给文件路径（减小数据量），缩略图地址在这里拼出来
    // 缩略图走单独的端口，不和数据接口、大图抢浏览器的并发连接
    thumbSrc: (src) => (src.includes("/thumb?") ? src : `${window.__PV_THUMB__ || ""}/thumb?t=${token}&path=${encodeURIComponent(src)}`),
    windowAction: call("window_action"),
    clearCache: call("clear_cache"), saveText: call("save_text"),
    // 任务结束后：系统通知、完成后睡眠 / 关机 / 退出 / 运行命令（见 webapp/afterjob.py）
    jobWatch: call("job_watch"), afterJobSet: call("after_job_set"), afterJobState: call("after_job_state"), afterJobCancel: call("after_job_cancel"),
    // 在软件里弹出窗口登录 Pixiv（见 webapp/login_window.py）
    loginStart: call("login_start"), loginStatus: call("login_status"), loginCancel: call("login_cancel"),
    // 软件自身的更新（见 update.js）
    updateCheck: call("update_check"), updateStart: call("update_start"), updateStatus: call("update_status"),
    updateCancel: call("update_cancel"), updateApply: call("update_apply"), updateDone: call("update_done"),
  };
}

export async function connect() {
  const desktop = await waitForBridge(4000);
  return desktop ? desktopApi() : mock;
}

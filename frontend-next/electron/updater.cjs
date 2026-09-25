// 自动更新模块（electron-updater）。
// 在构建时启用（app.isPackaged），开发态跳过。
// 从 GitHub Releases 或自建更新服务器拉取 NSIS 安装包差异更新。
//
// ★ 用户可见反馈的唯一出口是**原生对话框**（2026-09-25 修）。
//   原实现把状态经 `webContents.send("update-status" / "update-progress")` 发给渲染层，
//   但渲染层从未订阅这两个通道（preload.cjs 也没有暴露），消息进的是黑洞 ——
//   属「孤儿逻辑」：既不产生任何用户可见效果，也让「谁在消费」无法回答。
//   同时托盘「检查更新」在**无更新**时只有一行日志，用户点了菜单什么也看不到。
//   这里统一改走已经存在的可见通道（dialog）：
//     · 手动检查（托盘菜单）→ 无论结果如何都给反馈；
//     · 静默检查（启动后 5s）→ **绝不弹窗**（无网络时开机就弹框是最典型的骚扰型缺陷）。
const { app, dialog } = require("electron");
const log = require("console");

// 注意：`electron-updater` 必须在运行时惰性加载。
// 顶层直接 require 会在模块加载期就构造 autoUpdater，进而调用 app.getVersion()；
// 开发态（非 Electron 主进程上下文 / electron 被解析为 npm shim 包）会抛
// `Cannot read properties of undefined (reading 'getVersion')` 直接崩掉主进程。
// 因此这里按 app.isPackaged 惰性加载，并对加载失败做降级（只是没有自动更新，
// 绝不允许拖垮启动）。
let _autoUpdater = null;
let _loadTried = false;

function autoUpdater() {
  if (!_loadTried) {
    _loadTried = true;
    if (app.isPackaged) {
      try {
        _autoUpdater = require("electron-updater").autoUpdater;
        _autoUpdater.autoDownload = false;   // 询问用户后再下载
        _autoUpdater.allowPrerelease = false;
      } catch (err) {
        log.error("[updater] 自动更新模块加载失败，已跳过：", err && err.message);
        _autoUpdater = null;
      }
    }
  }
  return _autoUpdater;
}

let _win = null;
let _checking = false;
// 本次检查是否由用户手动触发（托盘「检查更新」）。只有手动检查才弹反馈框。
let _manual = false;

function _dialogOpts() {
  return { type: "info", title: "检查更新", noLink: true };
}

async function _info(message, detail) {
  try {
    const opts = { ..._dialogOpts(), message, detail, buttons: ["确定"] };
    if (_win && !_win.isDestroyed()) await dialog.showMessageBox(_win, opts);
    else await dialog.showMessageBox(opts);
  } catch { /* 对话框失败不影响主流程 */ }
}

function init(mainWindow) {
  _win = mainWindow;
  const au = autoUpdater();
  if (!au || !au.isUpdaterActive()) return;

  au.on("checking-for-update", () => {
    _checking = true;
    log.log("[updater] checking for update...");
  });

  au.on("update-available", (info) => {
    _checking = false;
    _manual = false; // 本次检查已给出明确出口（下载对话框），不需要再补反馈
    log.log("[updater] update available:", info.version);
    _notify(info);
  });

  au.on("update-not-available", (info) => {
    _checking = false;
    log.log("[updater] no update available");
    if (_manual) {
      _manual = false;
      _info(`已是最新版本（${app.getVersion()}）`,
            info && info.version ? `远端版本：${info.version}` : "");
    }
  });

  au.on("error", (err) => {
    _checking = false;
    log.error("[updater] error:", err.message);
    if (_manual) {
      _manual = false;
      _info("检查更新失败", String(err && err.message ? err.message : err));
    }
  });

  au.on("update-downloaded", async (info) => {
    log.log("[updater] update downloaded, will install on quit");
    // 静默标记：下次退出时自动安装。
    // ★ 这条链路的落点是 `app.on("quit")`（electron-updater/out/BaseUpdater
    //   .addQuitHandler → ElectronAppAdapter.onQuit）；因此主进程收尾**必须**走
    //   `app.quit()`，用 `app.exit()` 会绕过该事件使更新永远装不上
    //   （见 main.cjs fullShutdown 的注释）。
    au.autoInstallOnAppQuit = true;
    await _info(`qmt_work ${info.version} 已下载完成`,
                "退出应用后会自动安装新版本，无需手动干预。");
  });
}

async function _notify(info) {
  if (!_win || _win.isDestroyed()) return;
  const { response } = await dialog.showMessageBox(_win, {
    type: "info",
    title: "发现新版本",
    message: `qmt_work ${info.version} 可用`,
    detail: `当前版本将被更新至 ${info.version}。\n是否立即下载？`,
    buttons: ["下载", "稍后"],
    defaultId: 0,
    cancelId: 1,
  });
  if (response === 0) {
    const au = autoUpdater();
    if (au) au.downloadUpdate();
  }
}

function check() {
  const au = autoUpdater();
  if (!au || !au.isUpdaterActive()) {
    log.log("[updater] not active (dev mode or no publish config)");
    return;
  }
  // 置位必须在 `_checking` 早退**之前**：若此刻正有一次静默检查在飞，
  // 早退会让用户「点了菜单却没反应」；先置位则那次检查的结果会按手动反馈出来。
  // （_manual 只在 check() 里置位，而 check() 只由托盘菜单触发 —— 静默检查
  //   永远不会把它设为 true，所以开机不会有任何弹窗。）
  _manual = true;
  if (_checking) return;
  au.checkForUpdates().catch((err) => {
    log.error("[updater] check failed:", err.message);
    _manual = false;
  });
}

function checkSilent() {
  const au = autoUpdater();
  if (!au || !au.isUpdaterActive()) return;
  _manual = false;
  au.checkForUpdates().catch(() => {});
}

module.exports = { init, check, checkSilent };
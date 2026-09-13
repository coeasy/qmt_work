// 自动更新模块（electron-updater）。
// 在构建时启用（app.isPackaged），开发态跳过。
// 从 GitHub Releases 或自建更新服务器拉取 NSIS 安装包差异更新。
const { app, BrowserWindow, dialog } = require("electron");
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
    log.log("[updater] update available:", info.version);
    _notify(info);
  });

  au.on("update-not-available", (info) => {
    _checking = false;
    log.log("[updater] no update available");
    if (_win && _win.webContents) {
      _win.webContents.send("update-status", { status: "up-to-date", version: info?.version });
    }
  });

  au.on("error", (err) => {
    _checking = false;
    log.error("[updater] error:", err.message);
    if (_win && _win.webContents) {
      _win.webContents.send("update-status", { status: "error", error: err.message });
    }
  });

  au.on("download-progress", (progress) => {
    if (_win && _win.webContents) {
      _win.webContents.send("update-progress", {
        percent: progress.percent,
        bytesPerSecond: progress.bytesPerSecond,
        downloaded: progress.transferred,
        total: progress.total,
      });
    }
  });

  au.on("update-downloaded", (info) => {
    log.log("[updater] update downloaded, will install on quit");
    if (_win && _win.webContents) {
      _win.webContents.send("update-status", { status: "downloaded", version: info.version });
    }
    // 静默标记：下次退出时自动安装
    au.autoInstallOnAppQuit = true;
  });
}

async function _notify(info) {
  if (!_win) return;
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
  if (_checking) return;
  au.checkForUpdates().catch((err) => {
    log.error("[updater] check failed:", err.message);
  });
}

function checkSilent() {
  const au = autoUpdater();
  if (!au || !au.isUpdaterActive()) return;
  au.checkForUpdates().catch(() => {});
}

module.exports = { init, check, checkSilent };
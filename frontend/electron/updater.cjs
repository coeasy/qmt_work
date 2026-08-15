// 自动更新模块（electron-updater）。
// 在构建时启用（app.isPackaged），开发态跳过。
// 从 GitHub Releases 或自建更新服务器拉取 NSIS 安装包差异更新。
const { autoUpdater } = require("electron-updater");
const { BrowserWindow, dialog } = require("electron");
const log = require("console");

// 更新服务器配置（GitHub Releases 默认）
// 若自建，在 electron-builder.yml 中设置 publish.serverUrl
autoUpdater.autoDownload = false; // 询问用户后再下载
autoUpdater.allowPrerelease = false;

let _win = null;
let _checking = false;

function init(mainWindow) {
  _win = mainWindow;
  if (!autoUpdater.isUpdaterActive()) return;

  autoUpdater.on("checking-for-update", () => {
    _checking = true;
    log.log("[updater] checking for update...");
  });

  autoUpdater.on("update-available", (info) => {
    _checking = false;
    log.log("[updater] update available:", info.version);
    _notify(info);
  });

  autoUpdater.on("update-not-available", (info) => {
    _checking = false;
    log.log("[updater] no update available");
    if (_win && _win.webContents) {
      _win.webContents.send("update-status", { status: "up-to-date", version: info?.version });
    }
  });

  autoUpdater.on("error", (err) => {
    _checking = false;
    log.error("[updater] error:", err.message);
    if (_win && _win.webContents) {
      _win.webContents.send("update-status", { status: "error", error: err.message });
    }
  });

  autoUpdater.on("download-progress", (progress) => {
    if (_win && _win.webContents) {
      _win.webContents.send("update-progress", {
        percent: progress.percent,
        bytesPerSecond: progress.bytesPerSecond,
        downloaded: progress.transferred,
        total: progress.total,
      });
    }
  });

  autoUpdater.on("update-downloaded", (info) => {
    log.log("[updater] update downloaded, will install on quit");
    if (_win && _win.webContents) {
      _win.webContents.send("update-status", { status: "downloaded", version: info.version });
    }
    // 静默标记：下次退出时自动安装
    autoUpdater.autoInstallOnAppQuit = true;
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
    autoUpdater.downloadUpdate();
  }
}

function check() {
  if (!autoUpdater.isUpdaterActive()) {
    log.log("[updater] not active (dev mode or no publish config)");
    return;
  }
  if (_checking) return;
  autoUpdater.checkForUpdates().catch((err) => {
    log.error("[updater] check failed:", err.message);
  });
}

function checkSilent() {
  if (!autoUpdater.isUpdaterActive()) return;
  autoUpdater.checkForUpdates().catch(() => {});
}

module.exports = { init, check, checkSilent };
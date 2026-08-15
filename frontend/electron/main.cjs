// Electron 主进程（桌面壳）：启动 Python 后端子进程 -> 等待就绪 -> 加载同源前端 URL。
// 端口发现：后端通过 QMT_PORT_FILE 写出实际端口（run.py 支持端口被占用自动 +1），
// 桌面壳读取该文件后按实际端口连接，彻底规避端口冲突。
const { app, BrowserWindow, Tray, Menu, ipcMain, shell, nativeImage, dialog } = require("electron");
const { spawn, execFile } = require("child_process");
const path = require("path");
const fs = require("fs");
const http = require("http");

const DEFAULT_PORT = 21117;
const HEALTH = `/api/docs`;
let backend = null;
let win = null;
let tray = null;
let quitting = false;
let shuttingDown = false; // 异步停机中标志（防止 before-quit 重入）
let activePort = DEFAULT_PORT;

// 自动更新（electron-updater）：打包后启用，开发态自动跳过
const updater = require("./updater.cjs");

// 单实例锁
const gotLock = app.requestSingleInstanceLock();
if (!gotLock) { app.quit(); }

function portFile() {
  return path.join(app.getPath("userData"), "port.txt");
}

function backendEntry() {
  // 打包后：extraResources/backend/qmt_work/qmt_work.exe（PyInstaller onedir 产物）
  if (app.isPackaged) {
    const exe = path.join(process.resourcesPath, "backend", "qmt_work", "qmt_work.exe");
    return { cmd: exe, args: [] };
  }
  // 开发态：用系统 python 跑 run.py
  return { cmd: "python", args: [path.join(__dirname, "..", "..", "backend", "run.py")] };
}

function startBackend() {
  const { cmd, args } = backendEntry();
  // 启动前清理陈旧端口文件（上次异常退出可能残留过期端口，导致健康检查等错端口）
  try { fs.unlinkSync(portFile()); } catch { /* 不存在则忽略 */ }
  // 运行时数据（SQLite）写入用户可写目录，避免 Program Files 只读问题
  const env = {
    ...process.env,
    QMT_DB_PATH: path.join(app.getPath("userData"), "app.db"),
    QMT_PORT_FILE: portFile(),
  };
  backend = spawn(cmd, args, { stdio: ["ignore", "pipe", "pipe"], windowsHide: true, env });
  backend.stdout.on("data", (d) => console.log("[backend]", d.toString().trim()));
  backend.stderr.on("data", (d) => console.error("[backend-err]", d.toString().trim()));
  backend.on("exit", (code) => {
    if (quitting) return;
    console.error("backend exited", code);
    dialog.showErrorBox("qmt_work 后端异常退出",
      `后端进程已退出（code=${code}）。\n请检查是否端口被占用或数据文件损坏，然后重新启动桌面客户端。`);
  });
}

// ---- 退出清理：桌面壳关闭后，后端及其所有子进程（含 bridge 桥接进程）必须全部退出 ----

function killBackendTree() {
  if (!backend) return;
  try {
    if (backend.exitCode !== null || backend.signalCode !== null) return; // 已退出
    const pid = backend.pid;
    if (process.platform === "win32") {
      // taskkill /T 递归终止进程树（含 bridge 等孙进程），/F 强杀
      execFile("taskkill", ["/PID", String(pid), "/T", "/F"], { windowsHide: true }, () => {});
    } else {
      try { process.kill(-pid, "SIGKILL"); } catch { backend.kill("SIGKILL"); }
    }
  } catch { /* 进程可能已消失 */ }
}

// 优雅停机优先：先通知后端执行 lifespan 关闭（会清理 bridge 子进程与各引擎），
// 短暂等待后再强杀进程树兜底，确保「关闭后零残留」。
function shutdownBackend() {
  return new Promise((resolve) => {
    let done = false;
    const finish = () => { if (!done) { done = true; resolve(); } };
    try {
      const req = http.request({
        host: "127.0.0.1", port: activePort,
        path: "/api/v1/scheduler/shutdown", method: "POST",
        timeout: 1500, headers: { "Content-Type": "application/json" },
      }, (res) => { res.resume(); res.on("end", finish); });
      req.on("error", finish);
      req.on("timeout", () => { req.destroy(); finish(); });
      req.end("{}");
      setTimeout(finish, 2000); // 最多等 2s
    } catch { finish(); }
  });
}

async function fullShutdown() {
  quitting = true;
  try { await shutdownBackend(); } catch { /* 忽略 */ }
  // 再给后端短暂时间完成优雅停机
  await new Promise((r) => setTimeout(r, 1200));
  killBackendTree(); // 兜底：确保整棵进程树退出
  setTimeout(() => { app.exit(0); }, 300);
}

function readPortFile() {
  try {
    const v = parseInt(fs.readFileSync(portFile(), "utf8").trim(), 10);
    return v > 0 ? v : null;
  } catch { return null; }
}

// 平滑端口发现：后端启动时会写 QMT_PORT_FILE（实际监听端口，端口被占用自动 +1），
// 桌面壳分两阶段等待：
//   阶段1 等端口文件出现（后端写文件，最多 ~30s）；
//   阶段2 对实际端口做健康检查（后端仍在启动，最多 ~60s）。
// 端口文件一直没出现（异常）才回退 DEFAULT_PORT 探测，彻底规避端口冲突。
function waitReady(retries = 90) {
  const PORT_FILE_TIMEOUT = 30; // 端口文件最长等待秒数
  return new Promise((resolve, reject) => {
    // ---- 阶段1：等端口文件 ----
    let port = null;
    const waitPortFile = (n) => {
      port = readPortFile();
      if (port) { console.log("[desktop] backend port file ->", port); return stage2(retries); }
      if (n <= 0) {
        console.warn("[desktop] port file missing after", PORT_FILE_TIMEOUT, "s; fallback to default", DEFAULT_PORT);
        port = DEFAULT_PORT; return stage2(retries);
      }
      setTimeout(() => waitPortFile(n - 1), 1000);
    };
    // ---- 阶段2：健康检查实际端口 ----
    const stage2 = (n) => {
      const req = http.get({ host: "127.0.0.1", port, path: HEALTH, timeout: 1000 }, (res) => {
        res.resume(); activePort = port; console.log("[desktop] backend ready on", port); resolve(true);
      });
      req.on("error", () => {
        if (n <= 0) reject(new Error(`backend not ready (port ${port})`));
        else setTimeout(() => stage2(n - 1), 1000);
      });
      req.on("timeout", () => {
        req.destroy();
        if (n <= 0) reject(new Error(`backend timeout (port ${port})`));
        else setTimeout(() => stage2(n - 1), 1000);
      });
    };
    waitPortFile(PORT_FILE_TIMEOUT);
  });
}

function createWindow() {
  win = new BrowserWindow({
    width: 1440, height: 900, minWidth: 1024, minHeight: 720,
    backgroundColor: "#0f1420",
    webPreferences: { preload: path.join(__dirname, "preload.cjs"), contextIsolation: true, nodeIntegration: false },
  });
  win.loadURL(`http://127.0.0.1:${activePort}/`);
  win.on("close", (e) => {
    if (!quitting) { e.preventDefault(); win.hide(); }
  });
}

function trayIcon() {
  // 优先 build/icon.png；其次内嵌 1x1 占位，避免托盘缺失
  const iconPath = path.join(__dirname, "..", "build", "icon.png");
  if (fs.existsSync(iconPath)) return nativeImage.createFromPath(iconPath);
  return nativeImage.createEmpty();
}

function createTray() {
  const icon = trayIcon();
  if (icon.isEmpty()) return; // 无图标则不创建托盘
  tray = new Tray(icon.resize({ width: 16, height: 16 }));
  const ctx = Menu.buildFromTemplate([
    { label: "显示", click: () => win.show() },
    { label: "打开浏览器", click: () => shell.openExternal(`http://127.0.0.1:${activePort}/`) },
    { type: "separator" },
    { label: "检查更新", click: () => updater.check() },
    { type: "separator" },
    { label: "退出", click: () => { quitting = true; app.quit(); } },
  ]);
  tray.setToolTip("qmt_work 量化平台");
  tray.setContextMenu(ctx);
  tray.on("click", () => win.show());
}

app.whenReady().then(async () => {
  startBackend();
  try { await waitReady(); } catch (e) {
    console.error(e.message);
    dialog.showErrorBox("qmt_work 启动失败",
      `${e.message}\n\n请确认后端进程未被占用端口，或查看日志后重试。`);
  }
  createWindow();
  createTray();
  // 自动更新：启动后静默检查一次（打包环境生效）
  updater.init(win);
  setTimeout(() => updater.checkSilent(), 5000);
});

app.on("second-instance", () => win && win.show());
// 退出：先优雅停机后端再强杀兜底，保证桌面壳关闭后零残留进程
app.on("before-quit", (e) => {
  if (shuttingDown) return; // 正在停机中，放行本次退出
  e.preventDefault();       // 拦截，先异步清理后端
  shuttingDown = true;
  fullShutdown();
});
app.on("quit", () => { quitting = true; killBackendTree(); });
process.on("exit", () => killBackendTree()); // 极端兜底（如系统关机）

ipcMain.handle("app-version", () => app.getVersion());

// 开机自启（全自动运行）：设置/查询是否随系统启动
ipcMain.handle("set-auto-launch", (e, enabled) => {
  try {
    app.setLoginItemSettings({ openAtLogin: Boolean(enabled), path: process.execPath });
    return { ok: true, enabled: app.getLoginItemSettings().openAtLogin };
  } catch (err) {
    return { ok: false, error: String(err) };
  }
});
ipcMain.handle("get-auto-launch", () => {
  try { return { enabled: app.getLoginItemSettings().openAtLogin }; }
  catch (err) { return { enabled: false, error: String(err) }; }
});
ipcMain.handle("open-external", (e, url) => shell.openExternal(url));

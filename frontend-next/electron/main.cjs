// Electron 主进程（桌面壳）：启动 Python 后端子进程 -> 等待就绪 -> 加载同源前端 URL。
// 端口发现：后端通过 QMT_PORT_FILE 写出实际端口（run.py 支持端口被占用自动 +1），
// 桌面壳读取该文件后按实际端口连接，彻底规避端口冲突。
const { app, BrowserWindow, Tray, Menu, ipcMain, shell, nativeImage, dialog, session } = require("electron");
const { spawn, execFile } = require("child_process");
const path = require("path");
const fs = require("fs");
const http = require("http");

// ---- 启动模式：自动化测试 / 无 GPU 降级 ----
// 受限会话（无 GPU 的远程/沙箱/CI 环境）里，Chromium 的 GPU 进程与网络服务会反复崩溃，
// 表现是「窗口一闪即退、控制台只见 GPU process isn't usable / Network service crashed」。
// 这些开关把渲染整体降级到软件路径，使客户端在该类环境下仍能稳定起窗口。
//   QMT_CLIENT_TEST_MODE=1 → 自动化测试：安全模式 + 免模态框 + 失败以 exit 2 退出 + 错误落盘
//   QMT_CLIENT_SAFE_MODE=1 或命令行 --disable-gpu/--safe → 仅启用降级开关
const TEST_MODE = process.env.QMT_CLIENT_TEST_MODE === "1";
const SAFE_MODE = TEST_MODE
  || process.env.QMT_CLIENT_SAFE_MODE === "1"
  || process.argv.includes("--disable-gpu")
  || process.argv.includes("--safe");
if (SAFE_MODE) {
  app.disableHardwareAcceleration(); // 必须在 app ready 之前调用
  app.commandLine.appendSwitch("no-sandbox");
  app.commandLine.appendSwitch("disable-gpu");
  app.commandLine.appendSwitch("disable-gpu-compositing");
  app.commandLine.appendSwitch("disable-features", "Vulkan");
  // 显式要求软件渲染兜底：无 GPU 的虚拟化会话里，Chromium 会先尝试建 GLES3/GLES2
  // 上下文，失败后若拿不到软件路径就 FATAL 崩溃。
  app.commandLine.appendSwitch("use-gl", "swiftshader");
  app.commandLine.appendSwitch("use-angle", "swiftshader");
  // 无 GPU 时不产生 GPU 崩溃弹窗/断电式退出
  app.commandLine.appendSwitch("disable-crash-reporter");
  // 注意：不要加 --in-process-gpu 与 --disable-software-rasterizer。
  // 前者把 GPU 线程并进主进程，GPU 侧一旦 FATAL（gles2_cmd_decoder
  // "Validating command decoder is not supported"）会连带整个客户端退出
  // （实测 exit 0x80000003）；后者会掐断软件渲染兜底，使上面的 GLES 失败无路可退。
}

const DEFAULT_PORT = 21118; // 与 backend/run.py 的默认起始端口保持一致
// 就绪探针优先用轻量存活端点（/api/v1/live 不做依赖检查、不生成 Swagger 页面），
// 回退 /api/docs 兼容旧版后端；判据是「拿到任何 HTTP 响应」即视为后端已监听。
const HEALTH_PATHS = ["/api/v1/live", "/api/docs"];
let backend = null;
let win = null;
let tray = null;
let quitting = false;
let shuttingDown = false; // 异步停机中标志（防止 before-quit 重入）
let activePort = DEFAULT_PORT;
let backendExit = null;   // 后端已退出记录 {code, signal}
// 后端 stdout/stderr 环形缓冲（最近 40 行）：启动失败时随 startup-error.log 落盘，
// 无头环境下模态框不可见，只能靠这份日志定位根因。
const BACKEND_TAIL = [];
function _tail(line) {
  BACKEND_TAIL.push(line);
  if (BACKEND_TAIL.length > 40) BACKEND_TAIL.shift();
}

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
  // 开发态：优先使用显式指定 / 仓库自带运行时，避免依赖系统 python /
  // Windows Store 占位符导致启动失败。
  // QMT_DEV_PYTHON：指向「装了后端依赖」的解释器。仓库内置的 runtimes/cp311 只含
  // pip/wheel（没有 fastapi），因此自动化测试与本地开发可以用它切到自己的 venv，
  // 免去把依赖装进内置运行时。
  const candidates = [
    process.env.QMT_DEV_PYTHON,
    path.join(__dirname, "..", "..", "backend", ".venv", "Scripts", "python.exe"),
    path.join(__dirname, "..", "..", "backend", "venv", "Scripts", "python.exe"),
    path.join(__dirname, "..", "..", "backend", "runtimes", "cp311", "python.exe"),
  ].filter(Boolean);
  const runtimePy = candidates.find((p) => {
    try { return fs.existsSync(p); } catch { return false; }
  });
  const cmd = runtimePy || "python";
  return { cmd, args: [path.join(__dirname, "..", "..", "backend", "run.py")] };
}

function startBackend() {
  const { cmd, args } = backendEntry();
  // 启动前清理陈旧端口文件（上次异常退出可能残留过期端口，导致健康检查等错端口）
  try { fs.unlinkSync(portFile()); } catch { /* 不存在则忽略 */ }
  // 运行时数据（SQLite / 日志）写入用户可写目录，避免 Program Files 与
  // extraResources 资源目录只读导致日志无法落地（历史上曾因 asar 不可写
  // 导致用户点击「连接」后看不到任何后端日志，无从排障）。
  const env = {
    ...process.env,
    QMT_DB_PATH: path.join(app.getPath("userData"), "app.db"),
    QMT_LOG_DIR: path.join(app.getPath("userData"), "logs"),
    QMT_PORT_FILE: portFile(),
    // 后端（PyInstaller noconsole 产物）向管道输出时默认按系统 ANSI 代码页
    // （简体中文 Windows = cp936）编码，而 Electron 侧按 UTF-8 解码，中文日志
    // 会变成「锟斤拷」乱码，排障时无法阅读。显式指定子进程 IO 编码为 UTF-8，
    // 与桥接子进程的约定保持一致（见 backend/xtquant_client/bridge_server.py）。
    PYTHONIOENCODING: "utf-8",
  };
  // 确保日志目录存在；PyInstaller 打包后的 EXE 无写权限到 resources/，
  // 必须提前在 userData 创建好 logs 目录。
  try { fs.mkdirSync(env.QMT_LOG_DIR, { recursive: true }); } catch { /* 忽略 */ }
  backend = spawn(cmd, args, { stdio: ["ignore", "pipe", "pipe"], windowsHide: true, env });
  backend.stdout.on("data", (d) => {
    const s = d.toString().trim();
    s.split("\n").forEach((l) => _tail(l));
    console.log("[backend]", s);
  });
  backend.stderr.on("data", (d) => {
    const s = d.toString().trim();
    s.split("\n").forEach((l) => _tail("[err] " + l));
    console.error("[backend-err]", s);
  });
  backend.on("error", (err) => {
    backendExit = { code: null, signal: null, error: String(err) };
    _tail("[spawn-error] " + String(err));
  });
  backend.on("exit", (code) => {
    backendExit = { code, signal: null };
    if (quitting) return;
    console.error("backend exited", code);
    reportStartupFailure(`后端进程异常退出（code=${code}）`);
  });
}

// 启动失败统一出口：① 落盘 startup-error.log ② 交互式场景弹模态框
// ③ 测试模式直接以 exit 2 退出，让自动化用例拿到确定性信号。
function writeStartupError(reason) {
  const lines = [
    `[${new Date().toISOString()}] ${reason}`,
    `  execPath=${process.execPath}`,
    `  isPackaged=${app.isPackaged} testMode=${TEST_MODE} safeMode=${SAFE_MODE}`,
    `  userData=${app.getPath("userData")}`,
    "  --- backend tail (last 40 lines) ---",
    ...(BACKEND_TAIL.length ? BACKEND_TAIL : ["  (无输出)"]),
    "",
  ];
  try {
    fs.appendFileSync(path.join(app.getPath("userData"), "startup-error.log"),
                      lines.join("\n") + "\n", "utf8");
  } catch { /* 落盘失败不阻断退出流程 */ }
}

function reportStartupFailure(reason) {
  writeStartupError(`startup failed: ${reason}`);
  if (TEST_MODE) { quitting = true; app.exit(2); return; }
  try {
    dialog.showErrorBox("qmt_work 启动失败",
      `${reason}\n\n请确认后端进程未被占用端口，或查看日志后重试。`);
  } catch { /* 无 GUI 会话忽略 */ }
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
    // 后端已退出则该等待必然失败：立刻失败并带上原因，不再空等到超时
    // （旧行为下后端秒退时窗口要等 90s 才报错，自动化用例只能靠超时判定）。
    const died = () => backendExit && (backendExit.error
      ? `后端无法启动：${backendExit.error}`
      : `后端进程已退出（code=${backendExit.code}）`);
    // ---- 阶段1：等端口文件 ----
    let port = null;
    const waitPortFile = (n) => {
      const dead = died();
      if (dead) return reject(new Error(dead));
      port = readPortFile();
      if (port) { console.log("[desktop] backend port file ->", port); return stage2(retries); }
      if (n <= 0) {
        console.warn("[desktop] port file missing after", PORT_FILE_TIMEOUT, "s; fallback to default", DEFAULT_PORT);
        port = DEFAULT_PORT; return stage2(retries);
      }
      setTimeout(() => waitPortFile(n - 1), 1000);
    };
    // ---- 阶段2：健康检查实际端口（依次尝试轻量存活端点，任一有响应即就绪）----
    const stage2 = (n) => {
      const dead = died();
      if (dead) return reject(new Error(dead));
      let idx = 0;
      const tryPath = () => {
        const p = HEALTH_PATHS[idx];
        const req = http.get({ host: "127.0.0.1", port, path: p, timeout: 1000 }, (res) => {
          res.resume(); activePort = port;
          console.log("[desktop] backend ready on", port, "via", p);
          resolve(true);
        });
        const next = () => {
          if (idx < HEALTH_PATHS.length - 1) { idx += 1; tryPath(); }
          else if (n <= 0) reject(new Error(`backend not ready (port ${port})`));
          else setTimeout(() => stage2(n - 1), 1000);
        };
        req.on("error", next);
        req.on("timeout", () => { req.destroy(); next(); });
      };
      tryPath();
    };
    waitPortFile(PORT_FILE_TIMEOUT);
  });
}

function isLocalOrigin(url) {
  // 允许的来源 = 本机后端（任意端口，端口可能自动 +1 重试）
  return /^http:\/\/127\.0\.0\.1:\d+(\/|$|\?|#)/.test(url);
}

function isSafeExternalUrl(url) {
  // 仅放行 http/https，杜绝 file://、ms-msdt: 等协议被 openExternal 滥用
  return /^https?:\/\//i.test(url);
}

function openExternalSafe(rawUrl) {
  try {
    const url = String(rawUrl || "").trim();
    if (!isSafeExternalUrl(url)) return false;
    shell.openExternal(url);
    return true;
  } catch { return false; }
}

function applySecurityPolicy() {
  // CSP：页面源是本机后端；脚一律 'self'，样式允许内联（React 内联样式/动画），
  // 连接面 = 本机 REST + 本机 WS。这样即使渲染层被注入 <script src=外域> 也会被拦截。
  const csp = [
    "default-src 'self' http://127.0.0.1:*",
    "script-src 'self'",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob: http://127.0.0.1:*",
    "font-src 'self' data:",
    "connect-src 'self' http://127.0.0.1:* ws://127.0.0.1:*",
    "media-src 'self' data:",
    "object-src 'none'",
    "frame-src 'none'",
    "form-action 'self'",
    "base-uri 'self'",
  ].join("; ");
  const ses = session.defaultSession;
  ses.webRequest.onHeadersReceived((details, callback) => {
    if (isLocalOrigin(details.url)) {
      callback({ responseHeaders: {
        ...details.responseHeaders,
        "Content-Security-Policy": [csp],
        "X-Content-Type-Options": ["nosniff"],
      }});
    } else {
      callback({});
    }
  });
}

function createWindow() {
  win = new BrowserWindow({
    width: 1440, height: 900, minWidth: 1024, minHeight: 720,
    backgroundColor: "#0f1420",
    // 显式标题：不设置时窗口会先显示 package.json 的 name（qmt-work-frontend），
    // 页面加载完成后才被 <title> 覆盖，观感像未完成品。
    title: "qmt_work · 多券商量化平台",
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true, // preload 仅用 ipcRenderer/contextBridge，可安全沙箱化
    },
  });
  // 站内导航防护：只允许本机后端源；外链转交系统浏览器而非就地跳转
  win.webContents.on("will-navigate", (e, url) => {
    if (!isLocalOrigin(url)) {
      e.preventDefault();
      openExternalSafe(url);
    }
  });
  // window.open / target=_blank 一律拒绝建窗；外链转系统浏览器
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (isSafeExternalUrl(url)) shell.openExternal(url);
    return { action: "deny" };
  });
  win.loadURL(`http://127.0.0.1:${activePort}/`);
  // 页面加载结果必须显式留痕：无头/自动化场景下没有控制台可看，
  // 「窗口在但白屏」曾是最难定位的故障形态。这里同时打日志 + 写就绪标记文件，
  // 失败路径写 startup-error.log，供 scripts/client_start_test.py 判定。
  win.webContents.on("did-finish-load", () => {
    console.log("[desktop] window loaded");
    try {
      fs.writeFileSync(path.join(app.getPath("userData"), "window-ready.txt"),
                       `${Date.now()}\n`, "utf8");
    } catch { /* 标记文件写失败不影响运行 */ }
  });
  win.webContents.on("did-fail-load", (_e, code, desc, url) => {
    console.error(`[desktop] window load FAILED code=${code} desc=${desc} url=${url}`);
    writeStartupError(`前端页面加载失败 code=${code} ${desc} url=${url}`);
  });
  win.webContents.on("render-process-gone", (_e, details) => {
    console.error("[desktop] renderer process gone:", JSON.stringify(details));
    writeStartupError(`渲染进程退出：${JSON.stringify(details)}`);
  });
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
  applySecurityPolicy(); // CSP 必须在首次建窗前挂上
  startBackend();
  let ready = true;
  try { await waitReady(); } catch (e) {
    ready = false;
    console.error(e.message);
    // 测试模式：落盘诊断信息并以 exit 2 结束（模态框在无头环境不可见，会永久挂起）；
    // 交互模式：保持原有「弹框提示」行为，随后照常建窗便于用户看到界面与日志入口。
    reportStartupFailure(e.message);
  }
  if (!ready && TEST_MODE) return;
  createWindow();
  createTray();
  if (TEST_MODE) return; // 自动化测试不触发联网检查更新，保证用例确定性
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
// 仅放行 http/https 外链；非法协议静默拒绝（渲染进程被注入时无法借道拉起任意 handler）
ipcMain.handle("open-external", (e, url) => {
  if (!openExternalSafe(url)) return { ok: false, error: "unsupported url" };
  return { ok: true };
});

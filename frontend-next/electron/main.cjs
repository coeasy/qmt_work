// Electron 主进程（桌面壳）：启动 Python 后端子进程 -> 等待就绪 -> 加载同源前端 URL。
// 端口发现：后端通过 QMT_PORT_FILE 写出实际端口（run.py 支持端口被占用自动 +1），
// 桌面壳读取该文件后按实际端口连接，彻底规避端口冲突。
const { app, BrowserWindow, Tray, Menu, ipcMain, shell, nativeImage, dialog, session, screen } = require("electron");
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
// ---- 免模态框（**不等于**安全模式）----
//
// 为什么要把这两件事拆开（2026-09-21）：
//   GPU 崩溃自愈的收尾会弹一个 ``showMessageBoxSync`` 提示框。``showMessageBoxSync``
//   是**同步阻塞**的，在非交互会话（自动化 / 远程 / 无桌面）里无人可点 ⇒ 进程
//   一直挂在对话框上，「崩溃后到底写了什么判定」变得不可观测。
//   而 ``QMT_CLIENT_TEST_MODE`` 会**顺带强制安全模式**，于是想复现「正常模式崩一次」
//   这条路径就没有开关可用了。
//   拆出这个独立开关后，可以在正常模式下跑崩溃路径并观察判定文件的演化
//   —— 见 ``scripts/verify_gpu_safe_mode_persistence.py``（隔次启动失败的回归用例）。
const NO_MODAL = TEST_MODE || process.env.QMT_CLIENT_NO_MODAL === "1";

// ---- GPU 可用性判定（持久化）----
//
// 为什么不再用「崩溃后重新拉起子进程」：实测（2026-09-19）``app.relaunch()``、
// ``spawn(detached, stdio:"ignore")``、``cmd /c start`` 三条路**全部失败** ——
// spawn 都成功（有 pid、bat 也执行了），但父进程 ``app.exit(0)`` 后子进程随即消失，
// 最终零进程、零输出，客户端永远起不来。根因是父进程退出会带走子进程。
//
// 改为**跨次启动自愈**：本进程崩溃前把判定写盘，下次启动读到「GPU 不可用」就直接
// 进安全模式。代价是用户需要再点一次，但**一定会起来**，远好于永远起不来。
const GPU_VERDICT_FILE = "gpu-verdict.json";
const GPU_VERDICT_MAX_AGE_MS = 30 * 24 * 3600 * 1000; // 30 天后重新探测（驱动可能已修好）
let _gpuVerdictPathCache = null;
function gpuVerdictPath() {
  if (_gpuVerdictPathCache) return _gpuVerdictPathCache;
  try {
    _gpuVerdictPathCache = path.join(app.getPath("userData"), GPU_VERDICT_FILE);
  } catch {
    _gpuVerdictPathCache = path.join(require("os").tmpdir(), `qmt-${GPU_VERDICT_FILE}`);
  }
  return _gpuVerdictPathCache;
}
function readGpuVerdict() {
  try {
    const raw = fs.readFileSync(gpuVerdictPath(), "utf8");
    const v = JSON.parse(raw);
    if (v && typeof v.ok === "boolean") {
      const age = Date.now() - Number(v.ts || 0);
      if (Number.isFinite(age) && age < GPU_VERDICT_MAX_AGE_MS) return v.ok;
      if (!Number.isFinite(age)) return v.ok;
    }
  } catch { /* 无判定 / 坏文件 ⇒ 正常模式探测一次 */ }
  return null; // null = 未知
}
function writeGpuVerdict(ok) {
  try {
    fs.writeFileSync(gpuVerdictPath(), JSON.stringify({ ok, ts: Date.now() }), "utf8");
  } catch { /* 写不进就算了，退化为每次都探测 */ }
}
const GPU_VERDICT_BAD = readGpuVerdict() === false;

const SAFE_MODE = TEST_MODE
  || process.env.QMT_CLIENT_SAFE_MODE === "1"
  || process.argv.includes("--disable-gpu")
  || process.argv.includes("--safe")
  || GPU_VERDICT_BAD; // 上次判定 GPU 不可用 ⇒ 本次直接进入软件渲染
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

// ---- GPU 崩溃自愈：无 GPU 环境下自动以安全模式重启一次 ----
//
// 为什么需要它：上面那批开关**只在显式指定** `--disable-gpu` / `SAFE_MODE` 时才生效。
// 于是真实用户若遇到「GPU 进程起不来」的环境（虚拟机、RDP/远程桌面会话、显卡驱动异常、
// 组策略禁用硬件加速 —— 交易场景里都很常见），默认启动会走到 Chromium 的
// `FATAL: gpu_data_manager_impl_private.cc: GPU process isn't usable. Goodbye.`
// **直接闪退，且没有任何提示**：窗口一闪即没，日志只有几行 GPU 报错，用户无从排障。
//
// 自愈策略：GPU 子进程在启动阶段崩溃 N 次 → 追加 `--disable-gpu` 重新拉起本进程。
// 该参数会让上面的 `SAFE_MODE` 分支生效，因此**不会无限重启**（第二次已是安全模式）。
const GPU_CRASH_LIMIT = 2;          // 启动阶段允许崩溃的次数
const GPU_CRASH_WINDOW_MS = 12000;  // 「启动阶段」的时间窗（此后崩溃不再触发重启）
let gpuCrashes = 0;
let gpuRestarted = false;
const bootAt = Date.now();

function installGpuCrashGuard() {
  if (SAFE_MODE) return; // 已是安全模式：没有 GPU 崩溃可自愈，也无需重启
  app.on("child-process-gone", (_e, details) => {
    if (details.type !== "GPU") return;
    if (gpuRestarted) return;
    if (Date.now() - bootAt > GPU_CRASH_WINDOW_MS) return; // 运行中偶发崩溃不重启
    gpuCrashes += 1;
    console.warn(`[desktop] GPU 子进程崩溃 ${gpuCrashes}/${GPU_CRASH_LIMIT}:`,
                 details.reason || "", details.exitCode ?? "");
    if (gpuCrashes < GPU_CRASH_LIMIT) return;
    gpuRestarted = true;
    console.warn("[desktop] GPU 不可用 —— 写入判定 + 软件渲染，下次自动进入安全模式");
    // 关掉已拉起的后端，避免残留端口占着
    try { killBackendTree(); } catch { /* 忽略 */ }
    // 把「GPU 不可用」写盘：下次启动读到 ``readGpuVerdict() === false`` ⇒ SAFE_MODE
    writeGpuVerdict(false);
    // 交互模式：弹个简短提示，告知用户再点一次即可
    // （``showMessageBoxSync`` 是阻塞的 —— 非交互会话下必须跳过，见 NO_MODAL 说明）
    if (!NO_MODAL) {
      try {
        const { dialog } = require("electron");
        dialog.showMessageBoxSync({
          type: "warning",
          buttons: ["知道了"],
          defaultId: 0,
          title: "GPU 不可用",
          message: "检测到本机 GPU 不可用，自动切换到软件渲染模式。",
          detail: "请重新启动客户端以进入安全模式。若问题持续，请更新显卡驱动或联系技术支持。",
        });
      } catch { /* headless 时忽略 */ }
    }
    app.exit(0);
  });
}
installGpuCrashGuard();

// ---- GPU 可用性确认：跨过崩溃窗口后写 ok=true，避免下次再探测 ----
//
// ⚠️★ 必须先排除 SAFE_MODE —— 这里曾经漏判，导致**隔次启动失败**（实测 2026-09-21）：
//   安全模式下**压根没用过 GPU**（已 disableHardwareAcceleration + swiftshader），
//   跑满崩溃窗口当然是「不崩」，于是被这里误判成「GPU 好了」并写回 ok=true；
//   下一次启动读回 true ⇒ 回到正常模式 ⇒ 再崩一次。用户的体感是
//   「这客户端有一半概率打不开，而且能打开的那次一重启又坏」。
//   实测三次连续启动的日志证据：
//     第 1 次 repro_a —— 正常模式，GPU 崩 2/2 ⇒ 写 {"ok":false}
//     第 2 次 repro_b —— 读到 false ⇒ 安全模式，**0 条 GPU 崩溃行**，backend ready on 21118
//                        ⇒ 被这段改写成 {"ok":true}
//     第 3 次 repro_c —— 读到 true ⇒ 正常模式 ⇒ 又崩 2/2
//   正确做法：安全模式不写盘，让判定**保持 false**；30 天后自然过期再重探一次，
//   才是「装好驱动后能自己恢复」的闭环。
app.on("ready", () => {
  if (SAFE_MODE) return; // 安全模式没用过 GPU，无权宣称 GPU 可用
  setTimeout(() => {
    if (gpuRestarted) return; // 已判定为坏
    try { writeGpuVerdict(true); } catch { /* 忽略 */ }
  }, GPU_CRASH_WINDOW_MS + 500);
});

const DEFAULT_PORT = 21118; // 与 backend/run.py 的默认起始端口保持一致
// 就绪探针优先用轻量存活端点（/api/v1/live 不做依赖检查、不生成 Swagger 页面），
// 回退 /api/docs 兼容旧版后端；判据是「拿到任何 HTTP 响应」即视为后端已监听。
const HEALTH_PATHS = ["/api/v1/live", "/api/docs"];
let backend = null;
let win = null;
let tray = null;
//: 托盘是否**真的**创建成功。
//:
//: ★★ 为什么必须单独记这个标志（2026-09-22 实测踩到，用户报「右下角没有图标、
//: 无法真实退出」）：`win.on("close")` 的语义是「未退出则隐藏到托盘」，而
//: `quitting` 只由**托盘菜单的「退出」**或 `app.on("quit")` 置位。于是只要托盘没建
//: 起来，「关闭」就变成**单向隐藏**：窗口消失、托盘没有、**再也没有任何退出入口**
//: —— 用户只能去任务管理器杀进程。实测根因就是托盘图标没进 asar（见 trayIcon()）。
//: 因此「关闭即隐藏」**必须以托盘存在为前提**：托盘不可用时，关闭必须是真退出。
let trayAvailable = false;
//: 是否已经提示过「已最小化到托盘」。只提示一次，避免每次关闭都弹。
let trayHintShown = false;
let quitting = false;
let shuttingDown = false; // 异步停机中标志（防止 before-quit 重入）
let activePort = DEFAULT_PORT;
let backendExit = null;   // 后端已退出记录 {code, signal}
// 启动编排状态（见文件末尾 bootSequence）：
//   bootSettled —— 首次启动流程是否已判定（成功/失败）。用于区分「启动期后端退出」
//                  与「运行期后端崩溃」：前者由加载页呈现原因，后者才弹模态框；
//   booting     —— 正在执行启动流程，防止「重试」连点造成并发重启；
//   backendGen  —— 后端进程「代」计数。重试会重启后端，上一代进程的 exit 事件
//                  必须被忽略，否则刚拉起的新进程会被误判成「已退出」而立即失败。
let bootSettled = false;
let booting = false;
let backendGen = 0;
// 后端 stdout/stderr 环形缓冲（最近 40 行）：启动失败时随 startup-error.log 落盘，
// 无头环境下模态框不可见，只能靠这份日志定位根因。
const BACKEND_TAIL = [];
function _tail(line) {
  BACKEND_TAIL.push(line);
  if (BACKEND_TAIL.length > 40) BACKEND_TAIL.shift();
}
// 后端退出上报的幂等守卫与兜底：exit / close / 兜底定时器三路都可能触发，
// 只允许上报一次；换新一代进程时由 startBackend 复位（见 reportBackendExit）。
let exitReported = false;
let pendingExitCode = null;

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
  const gen = ++backendGen;
  exitReported = false; // 新一代进程：重新允许上报（见 reportBackendExit 的幂等守卫）
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
  // ⚠️ `exit` 早于 stdio 排空：在 exit 里读 BACKEND_TAIL 会**丢掉最后几行**，而最后
  //    几行恰恰是根因所在（实测一次启动失败只留下「监听端口」5 行，真正的
  //    `bridge 握手失败: _ping 调用超时（30.0s）` 被吞掉，排查时误判成「后端无声退出」）。
  //    因此这里只记录退出码并挂一个兜底定时器，真正的判定与落盘交给 `close`
  //    —— 它在所有 stdio 流关闭后才触发，此时 BACKEND_TAIL 必然完整。
  backend.on("exit", (code) => {
    if (gen !== backendGen) return; // 上一代进程（重试重启时被 kill）的退出事件，忽略
    backendExit = { code, signal: null };
    pendingExitCode = code;
    // 兜底：万一 close 未触发（stdio 被孙进程持有等）仍要上报；unref 保证这个
    // 定时器不会把 Node 事件循环多留 1.5s、拖慢正常退出。
    const _fallback = setTimeout(() => reportBackendExit(pendingExitCode), 1500);
    if (typeof _fallback.unref === "function") _fallback.unref();
  });
  backend.on("close", (code) => {
    if (gen !== backendGen) return;
    reportBackendExit(pendingExitCode === null ? code : pendingExitCode);
  });
}

// 启动失败统一出口：① 落盘 startup-error.log ② 交互式场景弹模态框
// ③ 测试模式直接以 exit 2 退出，让自动化用例拿到确定性信号。
function writeStartupError(reason) {
  const lines = [
    `[${new Date().toISOString()}] ${reason}`,
    `  execPath=${process.execPath}`,
    `  isPackaged=${app.isPackaged} testMode=${TEST_MODE} noModal=${NO_MODAL} safeMode=${SAFE_MODE}`,
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

// 「被外部强制终止」判据：退出码 1 且后端既无 Traceback 也无 ERROR 行。
// Python 主动 sys.exit(1) 的两条路径（单实例锁被占、远程绑定 + 默认密钥）**都会先
// log.error**，所以「零日志 + code=1」不是 Python 主动退出，而更像被强制终止 ——
// Windows 上 TerminateProcess（taskkill /F、清理脚本、安全软件）的退出码就是 1。
// 写明判据，避免下一次又把「被强杀」当成「后端启动失败」查半天。
function killSuspect(code) {
  if (code !== 1) return "";
  if (/Traceback|ERROR/.test(BACKEND_TAIL.join("\n"))) return "";
  return "（判据：退出码 1 且后端零 Traceback/ERROR —— Python 主动 sys.exit(1) 必先落日志，"
    + "故更像被外部强制终止，例如另一个实例的阶段 0 清理脚本或安全软件）";
}

function reportBackendExit(code) {
  if (exitReported) return; // exit / close / 兜底定时器三路只上报一次
  exitReported = true;
  if (quitting) return;
  console.error("backend exited", code);
  const reason = `后端进程异常退出（code=${code}）${killSuspect(code)}`;
  if (!bootSettled) {
    // 启动期退出：原因由 bootSequence 统一呈现在加载页上（带重试按钮），
    // 这里只落盘诊断信息，不再叠加一个模态框。
    writeStartupError(reason);
    return;
  }
  reportStartupFailure(reason);
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
  // ★★ 收尾必须走 `app.quit()`，**不能**用 `app.exit(0)`（2026-09-25 修的真缺陷）。
  //   Electron 文档明确 `app.exit()` 立即终止且**不发出** before-quit / will-quit /
  //   quit 事件；而 electron-updater 的「退出时自动安装」正挂在 `app.once("quit", …)`
  //   （electron-updater/out/ElectronAppAdapter.onQuit）。原实现用 app.exit 收尾 ⇒
  //   该事件永不触发 ⇒ 更新**下载完成后永远装不上**，用户反复看到「发现新版本」，
  //   而日志里没有任何异常（最难定位的一类断链）。
  //   此刻 quitting / shuttingDown 均已置位：close 处理器与 before-quit 都会放行，
  //   正常退出序列能走完；后端进程**已在上面杀掉**，随后被拉起的 NSIS 安装包不会再
  //   撞上「qmt_work.exe 被占用」（与 build_all.bat 的 running-instance 检查同源）。
  setTimeout(() => { app.quit(); }, 300);
  // 兜底：正常退出序列若被异常阻塞（窗口/更新器句柄），超时强制结束，保留
  // 「关闭即零残留」的原承诺。注意 JS 是单线程：安装包同步拉起期间本定时器
  // 不会插进去打断安装。
  setTimeout(() => { app.exit(0); }, 8000);
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
          refreshTrayTooltip(); // 端口此刻才定下来（可能不是默认值）
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

// ---- 窗口尺寸/位置记忆 ----
// 交易终端用户几乎总是固定一种布局（多屏、并排看盘），每次启动都回到 1440x900
// 居中会反复打断工作流。这里把还原态尺寸落盘，下次启动照原样恢复。
// 两条护栏：
//   ① 测试模式完全不读不写 —— 自动化用例必须每次拿到确定尺寸；
//   ② 恢复前校验坐标与显示器仍有交集 —— 否则拔掉副屏后窗口会「消失在屏幕外」。
function windowStateFile() {
  return path.join(app.getPath("userData"), "window-state.json");
}

function loadWindowState() {
  const fallback = { x: null, y: null, width: 1440, height: 900, maximized: false };
  if (TEST_MODE) return fallback;
  try {
    const s = JSON.parse(fs.readFileSync(windowStateFile(), "utf8"));
    const width = Number(s.width) > 0 ? Math.round(s.width) : fallback.width;
    const height = Number(s.height) > 0 ? Math.round(s.height) : fallback.height;
    let x = Number.isFinite(s.x) ? Math.round(s.x) : null;
    let y = Number.isFinite(s.y) ? Math.round(s.y) : null;
    if (x !== null && y !== null) {
      const onScreen = screen.getAllDisplays().some((d) => {
        const a = d.workArea;
        // 至少露出标题栏可拖拽区域（120x40），否则用户拖不动也看不到
        return x < a.x + a.width && x + 120 > a.x && y < a.y + a.height && y + 40 > a.y;
      });
      if (!onScreen) { x = null; y = null; }
    }
    return { x, y, width, height, maximized: Boolean(s.maximized) };
  } catch {
    return fallback;
  }
}

function saveWindowState() {
  if (TEST_MODE) return;
  try {
    if (!win || win.isDestroyed()) return;
    // 最大化时 getBounds() 返回的是满屏尺寸，直接存会导致下次启动「还原」成满屏；
    // getNormalBounds() 才是还原态尺寸。
    const n = (win.getNormalBounds ? win.getNormalBounds() : win.getBounds());
    fs.writeFileSync(windowStateFile(), JSON.stringify({
      x: n.x, y: n.y, width: n.width, height: n.height,
      maximized: win.isMaximized(),
    }), "utf8");
  } catch { /* 落盘失败不影响运行 */ }
}

let saveStateTimer = null;
function scheduleSaveWindowState() {
  if (saveStateTimer) clearTimeout(saveStateTimer);
  saveStateTimer = setTimeout(saveWindowState, 400); // 拖动/缩放过程中别疯狂写盘
}

// ---- 启动期加载页 ----
// 后端冷启动实测 7.7~10.7s（QMT 客户端探测 + 券商自动连接 + 交易日历加载），
// 慢的时候能到 30s+。旧流程把 createWindow() 放在 waitReady() **之后** ⇒
// 用户双击图标后这 7~30 秒里**完全没有窗口**，观感就是「点了没反应 / 是不是没启动」
// ——这是客户端最差的第一次印象。A/B 实测：旧顺序首窗 7.62s，新顺序 0.53s。
// 改为：窗口立刻建、先显示加载页，后端就绪后再 loadURL 切到真实应用 URL。
//
// 页面本体（booting / failed 两态）抽在 ./loadingPage.cjs —— 纯函数、零 Electron 依赖，
// 因而可以脱离主进程直接渲染验证（见 frontend-next/tests/loadingPage.test.ts）。
const { loadingPageHtml } = require("./loadingPage.cjs");

// ★★ 加载页的**首屏导航目标必须是 about:blank** —— 这就是「客户端窗口不能拖动」的根因。
//
// 实测（output/region_repro 的 REPRO_BOOT 二分；最终页统一为 http://127.0.0.1:21401/，
// 同一页面、同一测量方法；判据 = 标题栏 WM_NCHITTEST，2=HTCAPTION 可拖 / 1=HTCLIENT 拖不动）：
//   直接加载目标页          → 2（可拖）
//   先 data: 再导航到目标   → 恒为 1，**永久不可恢复**
//   先 file:// 再导航到目标 → 恒为 1（同样不可恢复）
//   先 about:blank 再导航   → 2（可拖）
//   导航后强制 resize / reload 均无效；注入全新 -webkit-app-region: drag 元素也无效
//   ⇒ 与页面 CSS/DOM 无关，是**窗口级**状态被写坏：Chromium 不再把 draggable region
//     上报给窗口过程。data: 与 file:// 都会写坏它，about:blank 不会。
//   （注意：早期只测到「file:// 首屏」在**最终页也是 file://** 时正常，据此误判过一次 ——
//     真正的分界是最终页为 http 的跨协议导航。）
//
// 所以：窗口先开 about:blank，再把加载页 HTML 用 document.write 注入进去；
// 后端就绪后 loadURL(appUrl()) 切到应用页 —— 这条路径上拖动区始终正常。
// 加载页里的内联 <style>/<script> 能跑：applySecurityPolicy 只对 isLocalOrigin 下发 CSP，
// about:blank 不匹配；页面里的按钮一律 addEventListener，不依赖内联事件属性。
let loadingGen = 0;

function showLoading(phase, message, error) {
  if (!win || win.isDestroyed()) return;
  const gen = ++loadingGen;
  const html = loadingPageHtml(phase, message, error);
  win.loadURL("about:blank")
    .then(() => {
      // 竞态保护：document.write 若晚于「切到应用页」到达，会把应用页面整个覆盖掉。
      // 因此注入前必须确认①本次仍是最新一次 showLoading ②窗口还停在 about:blank。
      if (!win || win.isDestroyed() || gen !== loadingGen) return undefined;
      if (win.webContents.getURL() !== "about:blank") return undefined;
      // document.open() 只替换文档、**不替换全局对象**，所以 preload 经 contextBridge
      // 暴露的 window.electronAPI 仍然可用（失败页的「重新启动后端」按钮依赖它，
      // 见 output/verify_loading_failed.py 的断言）。
      return win.webContents.executeJavaScript(
        "document.open();document.write(" + JSON.stringify(html) + ");document.close();");
    })
    .catch(() => { /* 窗口已销毁 / 导航被打断（切应用页时必然发生） */ });
}

function appUrl() {
  return `http://127.0.0.1:${activePort}/`;
}

// 启动编排：等待后端就绪 → 切到应用 URL；失败则把中文原因呈现在加载页上（可重试）。
// respawn=true 表示这是「重试」：先杀掉可能已退出的旧后端进程再重新拉起。
async function bootSequence(respawn) {
  if (booting) return;
  booting = true;
  try {
    if (respawn) {
      showLoading("booting", "正在重新启动后端服务…");
      killBackendTree();
      backendExit = null;
      BACKEND_TAIL.length = 0;
      startBackend();
    }
    await waitReady();
    bootSettled = true;
    if (!win || win.isDestroyed()) return;
    console.log("[desktop] backend ready ->", appUrl());
    loadingGen += 1; // 作废未完成的加载页注入（document.write 晚到会覆盖应用页面）
    win.loadURL(appUrl());
  } catch (e) {
    bootSettled = true;
    console.error(e.message);
    writeStartupError(`startup failed: ${e.message}`);
    // 测试模式：以 exit 2 结束，让自动化用例拿到确定性信号（无头环境下模态框不可见）。
    if (TEST_MODE) { quitting = true; app.exit(2); return; }
    // 交互模式：不再弹「启动失败」模态框（那会盖住界面、也没有出路），
    // 改为在加载页里给出中文原因 + 「重新启动后端」按钮。
    showLoading("failed", "后端服务启动失败，无法进入主界面。", e.message);
  } finally {
    booting = false;
  }
}

// ---- 渲染证明（仅 TEST_MODE）：把「页面的 DOM 到底渲染出来没」交给**渲染进程自己**回答 ----
//
// 为什么需要它（2026-09-26 R30 实测）：自动化用例原先截窗口图、数「不同颜色数」来判
// 「是否白屏」。但 PrintWindow(PW_RENDERFULLCONTENT) 取的是 GDI 合成层 —— 窗口被别的
// 窗口**遮挡**时 Chromium 会停止出帧、不再维护合成层，拿到的就是未合成的空白客户区：
// 实测恒为 1 种颜色 / PNG 6406 bytes，而**同一时刻**渲染进程里
// `document.querySelectorAll('*').length` = 200、`body.innerText` 473 字符
// （真实行情：平安银行 000001.SZ 11.30 -0.44% / 贵州茅台 600519.SH）。
// 即：用位图颜色数当判据，会把「页面好着呢」判成「纯色空窗」，构建因此在最后一道闸门
// exit 1。渲染结论必须来自渲染进程本身，而不是一张受遮挡影响的 OS 位图。
// 取样期间反复覆盖写（懒加载分片就绪前后差异很大），由用例侧自行判定阈值；
// 执行失败/超时都不算错误 —— 文件没写出时用例按「未取得渲染证明」处理。
const RENDER_PROOF_MS = 30000;

function startRenderProof() {
  const file = path.join(app.getPath("userData"), "render-proof.json");
  const probe = "JSON.stringify({"
    + "nodes:document.querySelectorAll('*').length,"
    + "textLen:(document.body&&document.body.innerText||'').length,"
    + "rootKids:(document.getElementById('root')||{}).childElementCount||0,"
    + "ready:document.readyState})";
  const t0 = Date.now();
  const tick = () => {
    if (!win || win.isDestroyed()) return;
    win.webContents.executeJavaScript(probe, true)
      .then((raw) => {
        try { fs.writeFileSync(file, `${raw}\n`, "utf8"); } catch { /* 落盘失败不影响运行 */ }
      })
      .catch(() => { /* 渲染进程不可用：保留上一次取样，用例按未取得处理 */ });
    if (Date.now() - t0 < RENDER_PROOF_MS) setTimeout(tick, 700);
  };
  tick();
}

function createWindow() {
  const st = loadWindowState();
  win = new BrowserWindow({
    ...(st.x !== null ? { x: st.x, y: st.y } : {}),
    width: st.width, height: st.height, minWidth: 1024, minHeight: 720,
    backgroundColor: "#0f1420",
    // 无边框自绘标题栏：原生标题栏在深色终端里是一条刺眼的浅色横条，还会带上
    // Electron 默认的英文菜单（File/Edit/View/Window/Help），与行情软件观感严重不符。
    // 改为 frame:false 后由前端 <TitleBar> 自绘（品牌 + 菜单 + 最小化/最大化/关闭）。
    frame: false,
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
  // 自动化测试：把窗口真正置顶。
  // 为什么必须由**本进程**来做：Windows 的原生窗口遮挡检测一旦判定本窗口被别的窗口
  // 完全遮住，Chromium 就停止出帧、不再维护合成层，此时 PrintWindow(PW_RENDERFULLCONTENT)
  // 只能拿到**未合成的空白客户区**（实测 18 色 / PNG 8450 bytes，与页面是否加载成功无关）。
  // 从外部进程调 SetWindowPos(HWND_TOPMOST) 实测不可靠（返回 1 但 WS_EX_TOPMOST 仍为 False），
  // 而窗口所有者自己置顶一定生效。生产环境 TEST_MODE 为假，零影响。
  if (TEST_MODE) win.setAlwaysOnTop(true);

  // 最大化状态同步给渲染进程：自绘标题栏的「最大化/还原」按钮图标要跟着换
  const pushMaximized = () => {
    try { win.webContents.send("window-maximized", win.isMaximized()); } catch { /* 已销毁 */ }
  };
  win.on("maximize", pushMaximized);
  win.on("unmaximize", pushMaximized);
  win.on("resize", scheduleSaveWindowState);
  win.on("move", scheduleSaveWindowState);
  // 恢复上次的最大化状态（尺寸/位置已在 BrowserWindow 构造时恢复）
  if (st.maximized) win.maximize();
  // 去掉应用菜单后，开发者工具快捷键需自行接管（F12 / Ctrl+Shift+I）。
  // 排障入口必须留着：无头/远程会话下这是唯一能看到渲染层报错的地方。
  win.webContents.on("before-input-event", (e, input) => {
    if (input.type !== "keyDown") return;
    const key = (input.key || "").toLowerCase();
    if (key === "f12" || (input.control && input.shift && key === "i")) {
      e.preventDefault();
      win.webContents.toggleDevTools();
    }
  });
  // 站内导航防护：只允许本机后端源；外链转交系统浏览器而非就地跳转
  win.webContents.on("will-navigate", (e, url) => {
    if (isLocalOrigin(url)) return;
    // 加载页（file://）自身的刷新不算外链，放行，免得 Ctrl+R / location.reload 被拦
    if (url === win.webContents.getURL()) return;
    e.preventDefault();
    openExternalSafe(url);
  });
  // window.open / target=_blank 一律拒绝建窗；外链转系统浏览器
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (isSafeExternalUrl(url)) shell.openExternal(url);
    return { action: "deny" };
  });
  // 先显示加载页 —— 后端就绪由 bootSequence 负责切到真实应用 URL。
  // 这样「双击图标 → 见到窗口」是即时的，等待期有明确的中文进度反馈。
  showLoading("booting", "正在启动后端服务，首次启动需加载行情与交易日历，请稍候…");
  // 页面加载结果必须显式留痕：无头/自动化场景下没有控制台可看，
  // 「窗口在但白屏」曾是最难定位的故障形态。这里同时打日志 + 写就绪标记文件，
  // 失败路径写 startup-error.log，供 scripts/client_start_test.py 判定。
  win.webContents.on("did-finish-load", () => {
    // ⚠️ 加载页是 data: URL，它**不是**「应用就绪」。若在这里也写 window-ready.txt，
    // 自动化用例会把加载页当成加载成功并据此截图（拍到的是加载页而非应用）。
    if (!isLocalOrigin(win.webContents.getURL())) return;
    console.log("[desktop] window loaded");
    try {
      fs.writeFileSync(path.join(app.getPath("userData"), "window-ready.txt"),
                       `${Date.now()}\n`, "utf8");
    } catch { /* 标记文件写失败不影响运行 */ }
    // did-finish-load 只代表**文档**加载完；「页面真的渲染出来了」由渲染进程自己证明
    if (TEST_MODE) startRenderProof();
  });
  win.webContents.on("did-fail-load", (_e, code, desc, url, isMainFrame) => {
    // -3 = ERR_ABORTED：loadURL 打断上一次导航时的正常中止（加载页 → 应用页切换、
    // 重试时重载加载页都会命中），不是故障。子框架失败同理不影响主文档。
    // 不加这个过滤会把正常的页面切换写成 startup-error.log，让自动化用例误判。
    if (code === -3 || isMainFrame === false) return;
    console.error(`[desktop] window load FAILED code=${code} desc=${desc} url=${url}`);
    writeStartupError(`前端页面加载失败 code=${code} ${desc} url=${url}`);
  });
  win.webContents.on("render-process-gone", (_e, details) => {
    console.error("[desktop] renderer process gone:", JSON.stringify(details));
    writeStartupError(`渲染进程退出：${JSON.stringify(details)}`);
  });
  win.on("close", (e) => {
    saveWindowState(); // 关闭前落盘尺寸/位置（此刻窗口仍有效）
    if (quitting) return; // 已在退出流程中，放行
    // ★★ 「关闭即隐藏到托盘」**必须以托盘真的存在为前提**（2026-09-22 修）。
    // 托盘不在时，隐藏 = 窗口消失且**再没有任何入口**能打开或退出 ⇒ 客户端变成
    // 退不掉的僵尸进程（用户只能进任务管理器）。这正是用户报的「无法真实退出」。
    // 所以托盘不可用时，「关闭」直接当「退出」处理 —— 宁可少一个后台常驻，
    // 也绝不能让用户退不出去。
    e.preventDefault();
    if (!trayAvailable) { requestQuit(); return; }
    win.hide();
    hintTrayOnce();
  });
  // Ctrl+Q / Cmd+Q 退出：本地快捷键（窗口聚焦即生效），不占用系统级 globalShortcut，
  // 也不会与其他应用的同名快捷键冲突。存在的理由同托盘兜底 —— 退出入口必须有多条。
  win.webContents.on("before-input-event", (_e, input) => {
    if (input.type !== "keyDown") return;
    if (String(input.key || "").toLowerCase() !== "q") return;
    if (input.control || input.meta) requestQuit();
  });
}

//: 内嵌兜底托盘图标（16×16 / 32×32，RGBA PNG 的 base64）。
//:
//: ★★ 为什么必须有内嵌兜底（2026-09-22 实测，用户报「右下角没有图标、无法真实退出」）：
//: 旧 `trayIcon()` 只找 ``__dirname/../build/icon.png``，而 ``electron-builder.yml``
//: 的 ``files`` 只打了 ``electron/**/*`` + ``package.json`` ⇒ **build/icon.png 没进 asar**
//: （实测 ``asar list`` 共 1531 条，``build/`` 与 ``icon.*`` 命中 **0** 条）。
//: 打包态于是 ``existsSync`` 恒 false ⇒ ``createEmpty()`` ⇒ ``isEmpty()`` 为真 ⇒
//: ``createTray()`` 直接 return ⇒ **托盘不存在**。而「关闭」的语义是「隐藏到托盘」、
//: ``quitting`` 只由托盘菜单置位 ⇒ 窗口一关就**再也退不出去**（自锁死，只能进任务管理器）。
//: 内嵌图标保证「任何环境下托盘都建得起来」；同时 electron-builder.yml 已把
//: ``build/icon.png`` 打进 asar 作为首选（内嵌只在候选全失败时用）。
const TRAY_ICON_PNG_B64 = {
  16: "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAAOElEQVR42mNggAL9xu//ScEMyIBUzSiGkKsZbgjVDMAH6OMCmnoBnzeGgBfo54KBM4DizERpdgYAVxKiZezE+pIAAAAASUVORK5CYII=",
  32: "iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAYAAABzenr0AAAAVElEQVR42mNgQAP6jd//0xIz4AK0thivQ+htOYYjBtQBA2U53BGjDhiUDiAHjDpgNBGO5gJy08OoA0YT4dDPBaMOGE2Eow4Y+g4Y7RkNis7pQHbPARgq5Cdj0ifuAAAAAElFTkSuQmCC",
};

function trayIcon() {
  // 依次尝试真实图标文件；**任何一步失败都继续往下**，最后用内嵌图兜底。
  const candidates = [
    path.join(__dirname, "..", "build", "icon.png"),              // asar 内（files 已包含）
    path.join(process.resourcesPath || "", "build", "icon.png"),  // extraResources（若将来加）
    path.join(__dirname, "..", "build", "icon.ico"),
  ];
  for (const p of candidates) {
    try {
      if (p && fs.existsSync(p)) {
        const img = nativeImage.createFromPath(p);
        if (!img.isEmpty()) return img;
      }
    } catch { /* 单条候选失败不影响其余候选 */ }
  }
  for (const size of [32, 16]) {
    try {
      const img = nativeImage.createFromDataURL(
        `data:image/png;base64,${TRAY_ICON_PNG_B64[size]}`);
      if (!img.isEmpty()) return img;
    } catch { /* 继续 */ }
  }
  // 理论上到不了这里；真到了也不能让托盘静默消失（见 trayAvailable 的注释）
  return nativeImage.createEmpty();
}

/** 显示并聚焦主窗口（托盘点击 / 第二实例 / 「显示」菜单共用）。 */
function showMainWindow() {
  if (!win || win.isDestroyed()) return;
  try {
    if (win.isMinimized()) win.restore();
    win.show();
    win.focus();
  } catch { /* 窗口正在销毁时忽略 */ }
}

/** **唯一**的「真退出」入口：置 ``quitting`` 后交给 before-quit → fullShutdown 优雅停机。 */
function requestQuit() {
  quitting = true;
  app.quit();
}

/** 首次隐藏到托盘时提示一次（Windows 用托盘气泡；其余平台跳过，不阻塞）。 */
function hintTrayOnce() {
  if (trayHintShown || !trayAvailable) return;
  trayHintShown = true;
  try {
    if (process.platform === "win32" && tray && typeof tray.displayBalloon === "function") {
      tray.displayBalloon({
        title: "qmt_work 仍在后台运行",
        content: "已最小化到系统托盘。右键托盘图标可选「退出」真正结束程序；"
               + "也可在应用内「菜单栏 → 退出」结束。",
      });
    }
  } catch { /* 气泡失败只影响提示，不影响运行 */ }
}

/** 刷新托盘提示文字。
 *
 * ★ 为什么需要单独一个函数（2026-09-22）：托盘是在 `createTray()` 里建的，而
 * `activePort` 要等 `waitReady()` 健康检查成功才知道（后端端口被占用时会自动 +1）。
 * 旧实现只在建托盘时 `setToolTip` 一次 ⇒ 端口一旦不是默认值，**托盘提示永远是错的**，
 * 用户按提示去开浏览器会打不开。所以端口一变就刷新。
 */
function refreshTrayTooltip() {
  if (!tray || !trayAvailable) return;
  try {
    tray.setToolTip(`qmt_work 量化平台（端口 ${activePort}）`);
  } catch { /* 托盘正在销毁时忽略 */ }
}

function createTray() {
  const icon = trayIcon();
  if (icon.isEmpty()) {
    // 极端兜底：连内嵌图都建不出来 ⇒ 明确放弃托盘，并**保持「关闭=退出」**，
    // 绝不留下「窗口能关、程序退不掉」的状态。
    trayAvailable = false;
    console.log("[desktop] 托盘图标不可用：已禁用「关闭隐藏到托盘」（关闭将直接退出）");
    return;
  }
  try {
    tray = new Tray(icon.resize({ width: 16, height: 16 }));
  } catch (err) {
    trayAvailable = false;
    console.log(`[desktop] 托盘创建失败：${err && err.message}；关闭将直接退出`);
    return;
  }
  const ctx = Menu.buildFromTemplate([
    { label: "显示主窗口", click: () => showMainWindow() },
    { label: "打开浏览器", click: () => shell.openExternal(`http://127.0.0.1:${activePort}/`) },
    { type: "separator" },
    { label: "检查更新", click: () => updater.check() },
    { type: "separator" },
    { label: "退出", click: () => requestQuit() },
  ]);
  refreshTrayTooltip();
  tray.setContextMenu(ctx);
  // 左键单击 = 显示主窗口（Windows 上左键单击默认无行为，必须显式绑）
  tray.on("click", () => showMainWindow());
  tray.on("double-click", () => showMainWindow());
  trayAvailable = true;
  console.log("[desktop] 托盘已创建");
}

app.whenReady().then(async () => {
  // ★ 没拿到单实例锁 ⇒ 本进程只是「用户又点了一次图标」的空壳，**绝不能继续**。
  // 为什么这个 return 是必需的：`app.quit()` 在 ready **之前**调用只是「预约退出」，
  // `whenReady()` 依然会 resolve —— 于是第二个实例照样会 startBackend() +
  // createWindow()，起出第二个后端（端口自动 +1、抢同一份 SQLite）再被退出流程杀掉。
  // 症状是「双击图标后闪一下、日志里多一次后端冷启动、偶尔端口错乱」，很难归因。
  if (!gotLock) return;
  applySecurityPolicy(); // CSP 必须在首次建窗前挂上
  // 去掉 Electron 默认应用菜单（File/Edit/View/Window/Help 一串英文），
  // 一是它与自绘标题栏重复且语言不符，二是深色终端里原生菜单栏观感割裂。
  // 代价：菜单提供的加速器（role: editMenu）一并消失 —— 输入框内的
  // 复制/粘贴/撤销由 Chromium 内建处理，仍然可用；开发者工具改由 F12 显式处理。
  Menu.setApplicationMenu(null);
  startBackend();
  // ★ 先建窗、再等后端。窗口立刻可见（显示加载页），后端就绪后由 bootSequence
  // 切到真实应用 URL。旧顺序（`await waitReady()` 之后才 createWindow）会让用户
  // 面对 7~11 秒的「什么都没有」——最容易被理解成「程序没启动起来」。
  // A/B 实测：旧顺序首窗 7.62s（且首帧已是应用页）；新顺序 0.86s。
  createWindow();
  createTray();
  void bootSequence(false);
  if (TEST_MODE) return; // 自动化测试不触发联网检查更新，保证用例确定性
  // 自动更新：启动后静默检查一次（打包环境生效）
  updater.init(win);
  setTimeout(() => updater.checkSilent(), 5000);
});

app.on("second-instance", () => showMainWindow());
// 窗口全关：托盘可用时正常走不到这里（关闭只是隐藏，窗口并未销毁）；
// 托盘不可用时**必须退出**，否则会以「无窗口」形态残留 —— 那正是「关不掉」。
app.on("window-all-closed", () => {
  if (!trayAvailable) requestQuit();
});
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

// ---- 自绘标题栏的窗口控制 ----
// 无边框窗口（frame:false）后，最小化/最大化/关闭必须由页面按钮经 IPC 触发。
// 这三个 handler 只操作窗口本身，不碰任何业务数据；close 复用主进程既有的
// 「未退出则隐藏到托盘」语义（见 win.on("close")），因此不会误杀后端。
ipcMain.handle("window-minimize", () => {
  if (win && !win.isDestroyed()) win.minimize();
  return true;
});
ipcMain.handle("window-toggle-maximize", () => {
  if (!win || win.isDestroyed()) return false;
  if (win.isMaximized()) win.unmaximize(); else win.maximize();
  return win.isMaximized();
});
ipcMain.handle("window-close", () => {
  if (win && !win.isDestroyed()) win.close();
  return true;
});
// ---- 真退出（页面「退出」按钮走这里）----
// ⚠️ 与 window-close 语义**不同**：window-close = 「隐藏到托盘」（托盘可用时），
// 本接口 = **结束进程**（含后端优雅停机）。
// 为什么页面必须有一个「真退出」：Windows 11 默认把新出现的托盘图标收进
// 「隐藏的图标」溢出层，用户**看不见**它 ⇒ 只靠托盘菜单退出是不可靠的。
// 退出入口必须「不依赖托盘可见性」。
ipcMain.handle("app-quit", () => { requestQuit(); return true; });
ipcMain.handle("window-is-maximized", () => Boolean(win && !win.isDestroyed() && win.isMaximized()));

// ---- 启动失败页的「重新启动后端」----
// 加载页是 data: URL，不能直接驱动主进程重启后端，只能经 IPC 请求。
// 只重跑启动流程，不涉及任何业务数据；bootSequence 内的 booting 标志防连点。
ipcMain.handle("boot-retry", () => { void bootSequence(true); return true; });

// ---- 数据目录设置：系统「选择文件夹」对话框 ----
// 只返回一个路径字符串，**不写任何东西**（真正的落盘由后端 /config/paths 做，
// 并且后端还会再校验一次 —— 这里不做校验，免得两处规则各写一套）。
// canceled ⇒ 返回 null（不是错误）；异常 ⇒ 返回 null 并记日志，让渲染层降级到手输。
ipcMain.handle("select-directory", async (_e, opts) => {
  try {
    if (!win || win.isDestroyed()) return null;
    const res = await dialog.showOpenDialog(win, {
      title: (opts && opts.title) || "选择数据目录",
      defaultPath: (opts && opts.defaultPath) || undefined,
      properties: ["openDirectory", "createDirectory", "dontAddToRecent"],
    });
    if (res.canceled || !res.filePaths || !res.filePaths.length) return null;
    return res.filePaths[0];
  } catch (err) {
    console.error("[select-directory] 打开目录选择框失败：", err);
    return null;
  }
});

"""qmt_work 客户端「本地启动测试」一体化脚本（纯标准库，Windows 专用）。

一次调用完成全流程：
  清理残留 -> 隔离 profile 启动 -> 端口发现 -> 就绪轮询 -> 分级冒烟 ->
  WebSocket 握手 -> 窗口存在性 + 窗口截图 -> 优雅停机 -> 零残留校验 -> 报告

用法（必须用托管 venv 解释器 / 系统 python 亦可，仅依赖标准库）：
  python scripts/client_start_test.py                  # 自动选目标（优先打包桌面客户端）
  python scripts/client_start_test.py --target client  # 强制打包桌面客户端
  python scripts/client_start_test.py --target dev     # 开发态 Electron（秒级，免重新打包）
  python scripts/client_start_test.py --target backend # 只测后端 EXE（无 GUI）
  python scripts/client_start_test.py --no-shot        # 跳过截图

全程**不删除任何文件**（只写自己的产物）：陈旧状态文件用「修改时间是否早于本次运行」
判定，避免误用上次运行的 port.txt / window-ready.txt。因此可随时安全重复运行。

设计约束（血泪经验，勿改）：
  1. 单次调用跑完：本工具环境会回收跨调用的后台进程——分步执行时进程已死，
     端口文件写了但没人监听，冒烟必然假失败。
  2. 净化环境变量：宿主自身是 Electron 应用，会注入 ELECTRON_RUN_AS_NODE=1 /
     NODE_OPTIONS；桌面壳继承后以纯 Node 模式启动 -> 无 TTY、秒退 exit 0、零输出，
     极易被误判成「打包坏了」。这里显式剔除。
  3. 绕过 HTTP 代理：沙箱内访问 127.0.0.1 会被代理拦截，表现为 502/000 假故障。
     所有本地请求走 ProxyHandler({}) 空代理 opener。
  4. 默认用隔离 profile（--user-data-dir 指向 output/client_test/profile）：
     不污染用户真实客户端数据（app.db/日志/自选股），并天然规避历史单实例锁残留。
  5. 退出必须走「优雅停机 + 进程树强杀 + 零残留校验」，否则下一次启动会撞锁。
"""
from __future__ import annotations

import argparse
import base64
import csv
import ctypes
import io
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLIENT_EXE = ROOT / "frontend" / "dist-electron" / "win-unpacked" / "qmt_work.exe"
BACKEND_EXE = ROOT / "backend" / "dist" / "qmt_work" / "qmt_work.exe"
DEV_ELECTRON = ROOT / "frontend" / "node_modules" / "electron" / "dist" / "electron.exe"
DEV_PYTHON = ROOT / "backend" / "runtimes" / "cp311" / "python.exe"
OUT_DIR = ROOT / "output" / "client_test"

# 宿主环境注入的变量：会让 Electron 以纯 Node 模式启动（秒退、零输出），必须剔除
ENV_DROP = ("ELECTRON_RUN_AS_NODE", "NODE_OPTIONS", "PYTHONPATH", "PYTHONHOME",
            "PYTHONSTARTUP", "ELECTRON_ENABLE_LOGGING")

# ---- 冒烟用例：路径 / 期望 ----
# STRICT：核心端点，必须 HTTP 200
SMOKE_CORE = [
    "/api/v1/health", "/api/v1/live", "/api/v1/ready",
    "/api/v1/capabilities", "/api/v1/brokers",
]
# GATED：依赖券商连接的端点，必须 HTTP 200 且业务码 ∈ {0, 503}
#   0   = 已连接券商，正常返回
#   503 = 未连接券商，返回引导（架构铁律：绝不返回假数据、也不 500）
SMOKE_BROKER_GATED = [
    "/api/v1/trade/orders", "/api/v1/trade/positions", "/api/v1/trade/deals",
    "/api/v1/trade/conditions", "/api/v1/account/grid", "/api/v1/algo",
]

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 绕代理
RESULTS: list[dict] = []
RUN_STARTED = 0.0          # 本次运行起始时间戳（判定状态文件是否陈旧）
_FRESH_TOLERANCE = 2.0     # 文件系统时间精度容差（秒）


def fresh(path: Path) -> bool:
    """该文件是否是**本次运行**产生的（而非上次运行残留）。

    用修改时间代替「启动前删掉旧文件」：删除会触发本环境的大范围删除护栏，
    而且完全没必要——只要不把上次的 port.txt / window-ready.txt 当真即可。
    """
    try:
        return path.stat().st_mtime >= RUN_STARTED - _FRESH_TOLERANCE
    except OSError:
        return False


# --------------------------------------------------------------------------- 工具

def record(group: str, name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append({"group": group, "name": name, "ok": bool(ok), "detail": detail})
    flag = "PASS" if ok else "FAIL"
    print(f"  [{flag}] {name}" + (f"  -> {detail}" if detail else ""))
    return bool(ok)


def info(msg: str) -> None:
    print(f"  .. {msg}", flush=True)


def http_get(url: str, timeout: float = 8.0) -> tuple[int | None, bytes]:
    try:
        with _OPENER.open(urllib.request.Request(url), timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:  # 4xx/5xx 也是「服务已响应」
        return exc.code, exc.read()
    except Exception as exc:  # noqa: BLE001
        return None, str(exc).encode("utf-8", "replace")


def http_post(url: str, payload: dict | None = None, timeout: float = 5.0) -> tuple[int | None, bytes]:
    data = json.dumps(payload or {}).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                headers={"Content-Type": "application/json"})
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except Exception as exc:  # noqa: BLE001
        return None, str(exc).encode("utf-8", "replace")


def jbody(raw: bytes) -> dict:
    try:
        obj = json.loads(raw.decode("utf-8", "replace"))
        return obj if isinstance(obj, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def biz_code(raw: bytes) -> int | None:
    """取业务码（本项目统一 code 字段；0 = 成功）。"""
    obj = jbody(raw)
    for key in ("code", "biz_code", "status_code"):
        if isinstance(obj.get(key), int):
            return obj[key]
    return None


def pids_of(image: str) -> set[int]:
    """按镜像名取 PID 集合（tasklist CSV 解析）。

    注意：中文 Windows 的 tasklist 输出是 cp936 字节，若用 text=True 会在读取线程里
    抛 UnicodeDecodeError（表现为一堆 Thread 异常噪声）。这里按字节取回再容错解码——
    PID 是 ASCII，遇到非 ASCII 内容会被替换符吞掉，不影响解析。
    """
    try:
        raw = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {image}", "/FO", "CSV", "/NH"],
                             capture_output=True, timeout=20).stdout
    except Exception:  # noqa: BLE001
        return set()
    out = raw.decode("utf-8", "replace")
    pids: set[int] = set()
    for row in csv.reader(io.StringIO(out)):
        if len(row) > 1 and row[0].strip().lower() == image.lower() and row[1].strip().isdigit():
            pids.add(int(row[1]))
    return pids


def kill_tree(pid: int) -> None:
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                   capture_output=True, timeout=30)


def kill_image(image: str) -> int:
    pids = pids_of(image)
    for pid in pids:
        kill_tree(pid)
    return len(pids)


def clean_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if k not in ENV_DROP and not k.upper().startswith("QMT_")}
    env["PYTHONIOENCODING"] = "utf-8"
    env.update(extra or {})
    return env


def tail(path: Path, lines: int = 12) -> str:
    try:
        content = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(content[-lines:])
    except Exception:  # noqa: BLE001
        return "(无内容)"


# --------------------------------------------------------------- Windows 窗口探测

def _win32():
    """user32 句柄签名（声明一次；不声明会因 64 位句柄带高位而报
    `OverflowError: int too long to convert`，表现为时好时坏的抖动）。"""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    if not getattr(_win32, "_done", False):
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND,
                                                    ctypes.POINTER(wintypes.DWORD)]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        user32.GetWindowTextLengthW.restype = ctypes.c_int
        user32.GetWindowTextW.argtypes = [wintypes.HWND, ctypes.c_void_p,
                                          ctypes.c_int]
        user32.GetWindowTextW.restype = ctypes.c_int
        user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        user32.GetWindowRect.restype = wintypes.BOOL
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.IsWindowVisible.restype = wintypes.BOOL
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        _win32._done = True
    return user32


def find_windows(pid: int) -> list[dict]:
    """列出指定进程的顶层窗口（标题/可见性/矩形），用于验证「窗口真的创建了」。"""
    user32 = _win32()
    found: list[dict] = []
    proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _lparam):
        wpid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
        if wpid.value == pid:
            n = user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            found.append({"hwnd": hwnd, "title": buf.value,
                          "visible": bool(user32.IsWindowVisible(hwnd)),
                          "rect": (rect.left, rect.top, rect.right, rect.bottom)})
        return True

    user32.EnumWindows(proc(cb), 0)
    return found


# Electron/Chromium 进程会顺带创建若干辅助窗口（输入法 IME、MSCTF 等），
# 它们同样属于本进程且往往「可见」，但只有 1x1 像素。若不排除，窗口检查会误判成功
# 并截出一张 1x1 的无效图。
_AUX_WINDOW_TITLES = {"Default IME", "MSCTFIME UI", "Program Manager", "IME"}
_MIN_WINDOW_SIDE = 200


def pick_main_window(pid: int) -> dict | None:
    """挑出真正的应用主窗口：可见 + 尺寸达标 + 非输入法辅助窗口，取面积最大者。"""
    best = None
    for item in find_windows(pid):
        left, top, right, bottom = item["rect"]
        item["width"], item["height"] = right - left, bottom - top
        if not item["visible"]:
            continue
        if item["width"] < _MIN_WINDOW_SIDE or item["height"] < _MIN_WINDOW_SIDE:
            continue
        if item["title"] in _AUX_WINDOW_TITLES:
            continue
        if best is None or item["width"] * item["height"] > best["width"] * best["height"]:
            best = item
    return best


def focus_window(hwnd: int) -> None:
    """把窗口恢复并置前，避免截图被其它窗口遮挡（截到一片空白）。"""
    user32 = _win32()
    try:
        user32.ShowWindow(hwnd, 9)        # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
    except Exception:  # noqa: BLE001
        pass


def ws_probe(port: int, path: str = "/api/v1/ws", timeout: float = 8.0) -> dict:
    """纯标准库 WebSocket 握手 + 读首帧（不依赖 websockets 包）。"""
    key = base64.b64encode(os.urandom(16)).decode()
    handshake = (f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nUpgrade: websocket\r\n"
                 f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                 "Sec-WebSocket-Version: 13\r\n\r\n")
    sock = None
    try:
        sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        sock.sendall(handshake.encode())

        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
        head, _, rest = buf.partition(b"\r\n\r\n")
        status = head.split(b"\r\n")[0].decode("utf-8", "replace")
        if "101" not in status:
            return {"ok": False, "detail": status or "无握手响应"}

        data = rest

        def need(n: int) -> None:
            nonlocal data
            while len(data) < n:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data += chunk

        need(2)
        if len(data) < 2:
            return {"ok": True, "status": status, "first": "(无数据帧)"}
        opcode = data[0] & 0x0F
        length = data[1] & 0x7F
        off = 2
        if length == 126:
            need(4)
            length = int.from_bytes(data[2:4], "big")
            off = 4
        elif length == 127:
            need(10)
            length = int.from_bytes(data[2:10], "big")
            off = 10
        need(off + length)
        payload = data[off:off + length]
        try:  # 礼貌关闭（客户端帧必须掩码）
            mask = os.urandom(4)
            sock.sendall(bytes([0x88, 0x80]) + mask)
        except Exception:  # noqa: BLE001
            pass
        return {"ok": True, "status": status, "opcode": opcode,
                "first": payload[:160].decode("utf-8", "replace")}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:  # noqa: BLE001
                pass


def screenshot_window(hwnd: int, out: Path,
                      rect: tuple[int, int, int, int] | None = None
                      ) -> tuple[str, int, int]:
    """抓客户端窗口自身画面（PrintWindow，不受其它窗口遮挡）。

    返回 (描述, 不同颜色数, 截图字节数)；失败时后两项为 0。
    用窗口句柄而非屏幕坐标：屏幕 DC 只能拿到「看得见的像素」，客户端窗口被工具窗口
    挡住时会截出遮挡者（本地验证时真实踩过）。颜色数用于判定「窗口是否真的渲染了内容」——
    纯色画面说明窗口在、页面却是白/黑屏。
    rect 为兜底矩形：PrintWindow 路径失败时按矩形做屏幕搬运，保证仍能出图并暴露原因。
    """
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import capture_screen  # type: ignore
    except Exception as exc:  # noqa: BLE001
        return f"截图模块不可用: {type(exc).__name__}: {exc}", 0, 0
    note = ""
    try:
        width, height = capture_screen.capture_window(str(out), hwnd)
    except Exception as exc:  # noqa: BLE001
        if not rect:
            return f"截图失败: {type(exc).__name__}: {exc}", 0, 0
        left, top, right, bottom = rect
        try:
            width, height = capture_screen.capture(str(out), max(1, right - left),
                                                   max(1, bottom - top), left, top)
            note = f"（PrintWindow 不可用已回退屏幕搬运: {exc}）"
        except Exception as exc2:  # noqa: BLE001
            return f"截图失败: {type(exc2).__name__}: {exc2}", 0, 0
    colors = int(getattr(capture_screen, "LAST_STATS", {}).get("distinct_colors", 0))
    try:
        size = out.stat().st_size if fresh(out) else 0  # 只认本次运行产出的图
    except OSError:
        size = 0
    return f"{out.name} {width}x{height}{note}", colors, size


# ----------------------------------------------------------------------- 主流程

def pick_target(requested: str) -> str:
    if requested != "auto":
        return requested
    if CLIENT_EXE.exists():
        return "client"
    if BACKEND_EXE.exists():
        return "backend"
    return "dev"


def find_dev_python() -> tuple[str | None, str]:
    """为开发态后端挑一个「装了 fastapi/uvicorn」的解释器。

    仓库内置的 backend/runtimes/cp311 只有 pip/wheel（无后端依赖），所以开发态启动
    必须能切到带依赖的 venv；否则本地启动测试只能等 10 分钟重新打包才能验证主进程改动。
    返回 (解释器路径, 来源说明)。
    """
    candidates = [
        ("$QMT_DEV_PYTHON", os.environ.get("QMT_DEV_PYTHON")),
        ("backend/.venv", str(ROOT / "backend" / ".venv" / "Scripts" / "python.exe")),
        ("backend/venv", str(ROOT / "backend" / "venv" / "Scripts" / "python.exe")),
        ("backend/runtimes/cp311", str(DEV_PYTHON)),
        ("本脚本解释器", sys.executable),
    ]
    for label, path in candidates:
        if not path or not Path(path).exists():
            continue
        try:
            probe = subprocess.run([path, "-c", "import fastapi, uvicorn"],
                                   capture_output=True, timeout=60)
        except Exception:  # noqa: BLE001
            continue
        if probe.returncode == 0:
            return path, f"{label}: {path}"
    return None, "候选解释器均缺少 fastapi/uvicorn"


def main() -> int:
    global RUN_STARTED
    ap = argparse.ArgumentParser(description="qmt_work 客户端本地启动测试")
    ap.add_argument("--target", choices=["auto", "client", "dev", "backend"], default="auto")
    ap.add_argument("--timeout", type=float, default=90.0, help="等待就绪的最长秒数")
    ap.add_argument("--no-shot", action="store_true", help="跳过截图")
    ap.add_argument("--port", type=int, default=0, help="期望端口（默认读端口文件）")
    args = ap.parse_args()

    target = pick_target(args.target)
    # 进程镜像名随目标不同：开发态是 electron.exe，打包态/后端是 qmt_work.exe
    image_name = "electron.exe" if target == "dev" else "qmt_work.exe"
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    profile = OUT_DIR / "profile"
    log_path = OUT_DIR / f"{target}.log"
    report_path = OUT_DIR / "report.json"
    RUN_STARTED = time.time()
    started = RUN_STARTED

    print(f"\n=== qmt_work 客户端启动测试 (target={target}) ===")
    print(f"    输出目录: {OUT_DIR}")

    # ---------- 阶段 0：清理 ----------
    print("\n[0/7] 清理残留")
    pre_client = pids_of("qmt_work.exe")
    pre_electron = pids_of("electron.exe") if target == "dev" else set()
    for pids, name in ((pre_client, "qmt_work.exe"), (pre_electron, "electron.exe")):
        for pid in pids:
            info(f"清理遗留进程 {name} pid={pid}")
            kill_tree(pid)
    # 「清理」阶段只清理**进程**；文件层面一律不删。
    # 原因有二：① 删除隔离 profile 属于大范围删除操作，会触发本环境的安全护栏；
    # ② 其实不需要删——陈旧文件用「修改时间是否早于本次运行」判定即可（见 fresh()）。
    # 这样脚本变成纯只读+只写自己产物的工具，随时可安全运行。
    profile.mkdir(parents=True, exist_ok=True)
    stale_files = [name for name in ("port.txt", "startup-error.log", "window-ready.txt")
                   if (profile / name).exists() and not fresh(profile / name)]
    record("prep", "残留进程已清理，无文件删除", True,
           f"清理 qmt_work={len(pre_client)} electron={len(pre_electron)} "
           f"（忽略陈旧状态文件 {len(stale_files)} 个）")
    # 后端数据目录里的 .qmt_work.lock 也不删：run.py 用的是 msvcrt 字节范围锁，
    # 进程退出即自动释放，残留的锁文件本身无害（旧做法靠删文件兜底，属于过度操作）。

    # ---------- 阶段 1：启动 ----------
    print("\n[1/7] 启动")
    env = clean_env({"QMT_CLIENT_TEST_MODE": "1", "QMT_CLIENT_SAFE_MODE": "1"})
    run_dir: Path | None = None
    if target == "client":
        exe, argv, cwd = CLIENT_EXE, [str(CLIENT_EXE)], CLIENT_EXE.parent
    elif target == "dev":
        if not DEV_ELECTRON.exists():
            record("start", "开发态 Electron 可用", False, f"缺少 {DEV_ELECTRON}")
            return _finish(report_path, started, target)
        exe, argv, cwd = DEV_ELECTRON, [str(DEV_ELECTRON), "."], ROOT / "frontend"
        dev_py, dev_src = find_dev_python()
        record("start", "开发态后端解释器可用（含 fastapi/uvicorn）", dev_py is not None, dev_src)
        if not dev_py:
            info("提示：内置 backend/runtimes/cp311 无后端依赖，请用 --target client 测试打包产物")
            return _finish(report_path, started, target)
        env["QMT_DEV_PYTHON"] = dev_py
    else:  # backend：仅后端 EXE，用于打包产物硬验证
        if not BACKEND_EXE.exists():
            record("start", "后端 EXE 存在", False, str(BACKEND_EXE))
            return _finish(report_path, started, target)
        run_dir = OUT_DIR / "backend_run"
        run_dir.mkdir(parents=True, exist_ok=True)
        env.update({"QMT_DB_PATH": str(run_dir / "app.db"),
                    "QMT_LOG_DIR": str(run_dir),
                    "QMT_PORT_FILE": str(run_dir / "port.txt")})
        exe, argv, cwd = BACKEND_EXE, [str(BACKEND_EXE)], BACKEND_EXE.parent
    if target != "backend":
        # 仅 GUI 目标支持 Electron 的 --user-data-dir 开关（后端 EXE 不识别该参数）
        argv.append(f"--user-data-dir={profile}")
    if args.port:
        env["QMT_PORT"] = str(args.port)

    record("start", f"启动目标存在: {exe.name}", exe.exists(), str(exe))
    if not exe.exists():
        return _finish(report_path, started, target)
    log_file = open(log_path, "wb")  # noqa: SIM115 (句柄随进程生命周期)
    proc = subprocess.Popen(argv, cwd=str(cwd), env=env, stdout=log_file,
                            stderr=subprocess.STDOUT, creationflags=0x00000200)
    info(f"pid={proc.pid}  日志: {log_path}")
    # 「秒退」是本项目最易误判的故障（环境注入 ELECTRON_RUN_AS_NODE 时 exit 0 且零输出），
    # 因此启动后必须观测一小段时间再判定存活，而不是 Popen 完立刻断言。
    time.sleep(3)
    record("start", "启动进程存活（未秒退）", proc.poll() is None,
           f"exit={proc.poll()}" if proc.poll() is not None else "运行中")

    # ---------- 阶段 2：端口发现 + 就绪 ----------
    print("\n[2/7] 等待就绪")
    port_file = (run_dir / "port.txt") if run_dir else (profile / "port.txt")
    port: int | None = None
    deadline = time.time() + args.timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            break  # 进程已退出，端口文件不可能再出现
        try:
            value = int(port_file.read_text().strip())
            # 必须是本次运行写出的端口：上次运行残留的端口文件会把探测引到错误端口
            if value > 0 and fresh(port_file):
                port = value
                break
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.5)
    record("ready", "端口文件已写出", port is not None,
           f"port={port} ({port_file})")

    ready_ok, ready_elapsed = False, 0.0
    if port:
        t0 = time.time()
        while time.time() < deadline:
            code, raw = http_get(f"http://127.0.0.1:{port}/api/v1/ready", timeout=4)
            if code == 200:
                ready_ok = True
                break
            if code is None and proc.poll() is not None:
                break  # 进程已退，不必再等
            time.sleep(0.8)
        ready_elapsed = time.time() - t0
    record("ready", "/api/v1/ready = 200", ready_ok,
           f"启动耗时 {ready_elapsed:.1f}s" if ready_ok else "未就绪")

    if not ready_ok:
        # 失败诊断：优先读主进程落盘的 startup-error.log（含后端输出尾部）
        err_file = profile / "startup-error.log"
        diag = []
        if err_file.exists() and fresh(err_file):
            diag.append("startup-error.log:\n" + tail(err_file, 20))
        else:
            diag.append("startup-error.log: 本次运行未产生（可能未进入主进程启动阶段）")
        diag.append(f"进程退出码: {proc.poll()}")
        diag.append("日志尾部:\n" + tail(log_path, 20))
        record("ready", "启动失败诊断信息", False, "\n".join(diag))
        teardown(proc, port, image_name)
        return _finish(report_path, started, target)

    base = f"http://127.0.0.1:{port}"

    # ---------- 阶段 3：REST 冒烟 ----------
    print("\n[3/7] REST 冒烟")
    for path in SMOKE_CORE:
        code, raw = http_get(base + path, timeout=10)
        ok = code == 200
        body = jbody(raw)
        record("rest", f"{path} = 200", ok,
               f"HTTP {code}" + (f" | {_brief(body, raw)}" if not ok else ""))

    for path in SMOKE_BROKER_GATED:
        code, raw = http_get(base + path, timeout=10)
        bc = biz_code(raw)
        # 200 + 业务码 0/503 都算合格；业务码缺失时容忍 200（老端点无 code 包装）
        ok = code == 200 and bc in (0, 503, None)
        record("rest", f"{path} 业务码 ∈ {{0,503}}", ok,
               f"HTTP {code} code={bc} | {_brief(jbody(raw), raw)}")

    # ---------- 阶段 4：前端资源 ----------
    print("\n[4/7] 前端 SPA 与静态资源")
    code, raw = http_get(base + "/", timeout=10)
    html = raw.decode("utf-8", "replace")
    record("spa", "GET / = 200 且为 SPA 壳", code == 200 and 'id="root"' in html,
           f"HTTP {code} len={len(raw)}")
    asset = _first_asset(html)
    if asset:
        acode, araw = http_get(base + asset, timeout=10)
        record("spa", f"静态资源可加载 {asset}", acode == 200,
               f"HTTP {acode} bytes={len(araw)}")
    else:
        record("spa", "index.html 含 /assets 引用", False, "未找到静态资源引用")

    # ---------- 阶段 5：WebSocket ----------
    print("\n[5/7] WebSocket 实时通道")
    ws = ws_probe(port)
    record("ws", "WS 握手 101 + 首帧", ws.get("ok", False),
           ws.get("first") or ws.get("detail", ""))

    # ---------- 阶段 6：窗口与渲染 ----------
    print("\n[6/7] 窗口渲染")
    if target == "backend":
        info("backend 模式无 GUI，跳过窗口检查")
    else:
        # 页面加载完成信号：主进程 did-finish-load 会写 window-ready.txt。
        # 这是比「窗口标题变了没」更可靠的「页面真的渲染了」判据。
        marker = profile / "window-ready.txt"
        loaded = False
        for _ in range(40):  # 最多 20s
            # 必须判「本次运行写出的」标记，否则上次运行的残留会造成假通过
            if marker.exists() and fresh(marker):
                loaded = True
                break
            if proc.poll() is not None:
                break
            time.sleep(0.5)
        record("window", "前端页面加载完成（did-finish-load）", loaded,
               "window-ready.txt 已写出" if loaded else "20s 内未见加载完成标记")

        log_text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
        # 崩溃特征：渲染进程退出 / 页面加载失败 / GPU 侧 FATAL（本项目实测过
        # gles2_cmd_decoder "Validating command decoder is not supported" 直接带走整个客户端）
        fault_keys = ("load FAILED", "renderer process gone", "FATAL:",
                      "kFatalFailure", "gpu_channel_manager")
        faults = [ln.strip() for ln in log_text.splitlines()
                  if any(k in ln for k in fault_keys)]
        record("window", "无页面加载失败 / 渲染进程崩溃 / GPU FATAL", not faults,
               faults[0][:160] if faults else "无异常")

        main_win = None
        for _ in range(25):  # 窗口创建在就绪之后，给它最多 25s
            main_win = pick_main_window(proc.pid)
            if main_win or proc.poll() is not None:
                break
            time.sleep(1)
        record("window", "客户端主窗口已创建（可见且尺寸达标）", main_win is not None,
               (f"「{main_win['title']}」{main_win['width']}x{main_win['height']}"
                if main_win else "未发现主窗口（进程可能已崩或窗口未渲染）"))
        if main_win and not args.no_shot:
            # PrintWindow 本身不需要窗口在前台；但若它失败会回退到屏幕搬运，
            # 所以仍先尝试置前，提高回退路径的成功率。
            focus_window(main_win["hwnd"])
            time.sleep(0.8)
            shot = OUT_DIR / "client_window.png"
            detail, colors, size = screenshot_window(main_win["hwnd"], shot, main_win["rect"])
            record("window", "窗口截图已产出", size > 5000, f"{detail} / {size} bytes")
            # 纯色画面 = 窗口在但页面没渲染出来（白/黑屏），是本项目历史上真实发生过的
            # 故障形态。以「采样到的不同颜色数」作为「是否真的渲染了内容」的交叉验证。
            record("window", "窗口已实际渲染（非纯色空窗）", colors >= 50,
                   f"采样到的不同颜色数={colors}")

    # ---------- 阶段 7：停机 + 零残留 ----------
    print("\n[7/7] 优雅停机与零残留")
    residue = teardown(proc, port, image_name)
    record("shutdown", "关闭后零残留进程", residue == 0, f"残留 {residue} 个")

    return _finish(report_path, started, target)


def _brief(body: dict, raw: bytes) -> str:
    if not body:
        return raw[:80].decode("utf-8", "replace")
    msg = body.get("message") or body.get("msg") or body.get("detail") or ""
    return (f"code={body.get('code')} msg={str(msg)[:70]}").strip()


def _first_asset(html: str) -> str | None:
    m = re.search(r'(?:src|href)="(/assets/[^"]+\.(?:js|css))"', html)
    return m.group(1) if m else None


def teardown(proc: subprocess.Popen, port: int | None,
             image: str = "qmt_work.exe") -> int:
    """优雅停机（scheduler/shutdown）-> 进程树强杀 -> 返回残留进程数。"""
    if port:
        http_post(f"http://127.0.0.1:{port}/api/v1/scheduler/shutdown", {}, timeout=4)
        info("已发送优雅停机请求")
        time.sleep(2.5)
    if proc.poll() is None:
        kill_tree(proc.pid)
    time.sleep(2)
    leftover = pids_of(image)
    for pid in leftover:  # 兜底再清一轮，保证下次启动不撞锁
        kill_tree(pid)
    time.sleep(1.5)
    return len(pids_of(image))


def _finish(report_path: Path, started: float, target: str) -> int:
    failed = [r for r in RESULTS if not r["ok"]]
    report = {
        "target": target,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(started)),
        "duration_seconds": round(time.time() - started, 1),
        "total": len(RESULTS),
        "passed": len(RESULTS) - len(failed),
        "failed": len(failed),
        "results": RESULTS,
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n" + "=" * 68)
    print(f"结果: {report['passed']}/{report['total']} 通过，耗时 {report['duration_seconds']}s"
          f"  目标={target}")
    if failed:
        print("失败项:")
        for item in failed:
            print(f"  - [{item['group']}] {item['name']}  {item['detail']}")
    print(f"报告: {report_path}")
    print("=" * 68)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

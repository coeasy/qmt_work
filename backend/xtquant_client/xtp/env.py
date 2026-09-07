"""环境探测 / 客户端进程与端口诊断 / launch_client / 版本画像（自原 xtp.py 逐行搬移）。"""

import os
import re
import sys

from dataclasses import dataclass, field

from ._common import (
    _candidate_roots,
    _is_system_dir,
    _load_xtquant_from,
    _normalize,
    _resolve_xtquant_path,
    _shell_attr,
)


def probe_environment(client_path: str, light: bool = False) -> dict:
    """结构化探测客户端环境（不连接券商）：定位 xtquant / 导入可行性 / 目录线索。

    供 /brokers/test、discovery（light=True）与 tools/diag_qmt.py 使用，把
    「为什么探测失败」拆成可读诊断。
    light=True 时仅做轻量定位 + ABI 兼容判定（不 import xtquant、不扫运行时），
    用于 auto-detect 候列表，避免每条候选都触发昂贵的 import / 进程扫描。
    """
    result: dict = {
        "client_path": client_path or "",
        "client_exists": bool(client_path and os.path.isdir(client_path)),
        "candidate_roots": [],
        "xtquant_site": None,
        "xtquant_found": False,
        "xtquant_importable": False,
        "import_error": "",
        "python_version": sys.version.split()[0],
        "has_userdata_mini": False,
        "has_bin_x64": False,
        "hint": "",
    }
    if not client_path:
        result["hint"] = "未填写 client_path"
        return result
    if not os.path.isdir(client_path):
        result["hint"] = f"目录不存在：{client_path}"
        return result
    roots = _candidate_roots(client_path)
    result["candidate_roots"] = roots
    for r in roots:
        if os.path.isdir(os.path.join(r, "userdata_mini")) or os.path.isdir(os.path.join(r, "userdata")):
            result["has_userdata_mini"] = True
        if os.path.isdir(os.path.join(r, "bin.x64")):
            result["has_bin_x64"] = True
    sp = _resolve_xtquant_path(client_path)
    result["xtquant_site"] = sp
    result["xtquant_found"] = bool(sp)
    # 导入 ABI 探测函数（进程内直连 / 桥接 判定用）；容错以避免运行时异常
    try:
        from ..runtime import detect_xtquant_abis, host_python_minor
    except Exception:  # noqa: BLE001
        def host_python_minor() -> int:
            return sys.version_info[0] * 100 + sys.version_info[1]

        def detect_xtquant_abis(_sp) -> list:  # noqa: E741 - _sp 仅占位
            return []
    # 先判定 broker 的 ABI 变体，再决定「进程内直连」还是「桥接」（避免 3.13 上
    # 直接 import xtquant.xtdata 触发 No module named 'xtquant.IPythonApiClient' 误报）
    broker_abis = detect_xtquant_abis(sp) if sp else []
    host = host_python_minor()
    abi_compatible = (host in broker_abis) if broker_abis else True
    result["broker_abis"] = broker_abis
    result["host_abi"] = host
    result["abi_compatible"] = abi_compatible
    if light:
        # 轻量模式（discover 阶段用）：只定位 xtquant 目录 + 判定 ABI 是否兼容，
        # 不真正 import xtquant（避免加载 .pyd 的副作用/耗时），也不触发昂贵的
        # discover_system_runtimes 扫描。完整诊断留给用户点击候选后的 /brokers/test。
        if sp:
            result["xtquant_importable"] = True if abi_compatible else "bridge"
            result["hint"] = (
                "已定位 xtquant 目录"
                + ("（主后端可进程内加载）" if abi_compatible
                   else "（主后端 ABI 不兼容，将经桥接子进程加载；点击候选后可探测）"))
        else:
            result["xtquant_importable"] = False
            result["hint"] = ("未找到 xtquant 目录：请确认 client_path 指向客户端数据目录"
                              "（极速版 MiniQMT 为 userdata_mini，完整版大客户端为 userdata；"
                              "或其上层为客户端根，含 bin.x64）")
        return result
    if sp:
        if abi_compatible:
            try:
                _load_xtquant_from(sp)
                result["xtquant_importable"] = True
                result["hint"] = "xtquant 可用"
            except Exception as exc:  # noqa: BLE001
                result["import_error"] = str(exc)[:500]
                if "IPythonApiClient" in result["import_error"] and not broker_abis:
                    # 进程内可导入但扩展模块缺失 → 客户端目录不完整
                    result["hint"] = (
                        "xtquant 安装不完整：缺少 IPythonApiClient 扩展模块"
                        "（.pyd）。请确认客户端目录完整（含 "
                        "bin.x64\\Lib\\site-packages\\xtquant\\ 下的扩展文件），"
                        "或重装券商客户端。")
                elif "DLL load failed" in result["import_error"] or "ImportError" in result["import_error"]:
                    result["hint"] = (
                        f"xtquant 扩展与当前 Python {sys.version.split()[0]} ABI 不兼容；"
                        f"请改用 ≤3.12 的 Python（或客户端自带 python）运行平台，"
                        f"或 pip install xtquant 到当前环境")
                else:
                    result["hint"] = f"已找到 xtquant 但导入失败：{result['import_error']}"
        else:
            # 主后端 ABI 与券商 xtquant 不兼容：本进程不可导入，但可经桥接子进程加载
            result["xtquant_importable"] = "bridge"
            lo = min(broker_abis); hi = max(broker_abis)
            result["hint"] = (
                f"主后端 Python {sys.version.split()[0]} 与券商 xtquant"
                f"（支持 cp{lo//100}.{lo%100}~cp{hi//100}.{hi%100}）ABI 不兼容，"
                f"将尝试通过桥接子进程加载（优先使用系统已安装的 Python 3.11 等；"
                f"无则需安装 Python {hi//100}.{hi%100} 到 PATH）")
    else:
        result["hint"] = ("未找到 xtquant 目录：请确认 client_path 指向客户端数据目录"
                          "（极速版 MiniQMT 为 userdata_mini，完整版大客户端为 userdata；"
                          "或其上层为客户端根，含 bin.x64）")
    # P0：ABI 运行时方案（进程内直连 / 桥接子进程）+ 可操作提示
    try:
        from ..runtime import discover_system_runtimes, host_python_minor, xtp_runtime_plan
        result["host_python"] = sys.version.split()[0]
        result["host_abi"] = host_python_minor()
        plan = xtp_runtime_plan(client_path)
        if plan is None:
            result["runtime_mode"] = None
            result["bridge_feasible"] = False
            result["suggested_abi"] = None
            # ABI 不兼容却无兼容运行时：给出明确可操作提示（而非笼统「导入失败」）
            if not result.get("abi_compatible", True) and broker_abis:
                lo = min(broker_abis); hi = max(broker_abis)
                need = f"{hi//100}.{hi%100}"
                result["hint"] = (
                    f"主后端 Python {result['host_python']} 与券商 xtquant"
                    f"（支持 cp{lo//100}.{lo%100}~cp{hi//100}.{hi%100}）ABI 不兼容，"
                    f"且当前未找到兼容的桥接运行时（系统仅检测到 Python {result['host_python']}）。"
                    f"请任选其一：① 在 backend/runtimes 放置 cp{hi//100}.{hi%100}"
                    f"/python.exe 嵌入式 Python；② 安装 Python {need} 并将其 python.exe"
                    f" 加入 PATH（平台会自动复用）；③ 升级券商客户端 SDK 至含 cp{host}"
                    f" 的版本。券商客户端需处于登录/可交易状态。")
        else:
            result["runtime_mode"] = plan["mode"]
            result["bridge_feasible"] = True
            result["suggested_abi"] = plan["abi"]
            if plan["mode"] == "bridge":
                # 经桥接加载：明确标记可用，并提示运行时来源
                result["xtquant_importable"] = "bridge"
                try:
                    system = discover_system_runtimes()
                    src = "系统已安装的 Python" if plan["abi"] in system else "捆绑运行时"
                except Exception:  # noqa: BLE001
                    src = "捆绑运行时"
                result["hint"] = (
                    f"主后端 Python {result['host_python']} ABI 不兼容，将自动桥接 "
                    f"cp{plan['abi']//100}.{plan['abi']%100} 子进程加载 xtquant（运行时来源：{src}）")
    except Exception:  # noqa: BLE001
        pass
    return result

def _probe_xtdata(xtdata, client_path: str | None = None) -> tuple[bool, str]:
    """探测行情服务是否可用，返回 (ok, detail)。兼容多版本 xtquant API。

    大/小窗口全兼容：xtdata.connect() 默认走 .xtquant/*/xtdata.cfg 指定端口
    （默认 58610）。若不可达且本机 miniquote 正监听其他端口（多客户端并存 /
    cfg 残留场景），自动逐个回退尝试，绝不因端口配置漂移误判为故障。

    版本差异（踩坑记录）：
    - 部分新版本提供 `xtdata.connect()`；
    - 本机广发 QMT 自带的 2023 版**没有** connect()，只有 `get_client()`——
      成功返回已连接的 RPCClient，失败抛 Exception("无法连接行情服务！")。
      曾误用 `bool(xtdata.connect())` 预检，AttributeError 被吞成「不可用」，
      导致行情模式在该版本下 100% 连不上（即使客户端已登录）。

    设计原则：预检只用于**确定失败**的场景。两个 API 都不存在时返回 True 放行，
    把判断交给后续真实调用，绝不因 SDK 版本差异把可用的连接误判为故障。
    """
    last_detail = ""
    for name in ("connect", "get_client"):
        fn = getattr(xtdata, name, None)
        if not callable(fn):
            continue
        try:
            res = fn()
        except Exception as exc:  # noqa: BLE001
            # SDK 原话（如「无法连接行情服务！」）是有效诊断信息，保留并上抛
            last_detail = str(exc).strip() or type(exc).__name__
            # 端口回退：默认端口连不上时，自动尝试本机 miniquote 实际监听的端口
            if client_path:
                try:
                    probe = _shell_attr("_probe_quote_service")(client_path)  # monkeypatch 兼容：经壳模块动态查找（见 _common._shell_attr）
                except Exception:  # noqa: BLE001
                    probe = {}
                alt_ports = [p for p in (probe.get("quote_ports") or []) if p != 58610]
                if name == "connect":
                    for port in alt_ports:
                        try:
                            res = fn(port=port)
                        except Exception as exc2:  # noqa: BLE001
                            last_detail = str(exc2).strip() or last_detail
                            continue
                        chk = getattr(res, "is_connected", None)
                        if callable(chk):
                            try:
                                if not chk():
                                    continue
                            except Exception:  # noqa: BLE001
                                pass
                        return True, ""
            return False, last_detail
        if res is None or res is False:
            last_detail = f"xtdata.{name}() 未返回可用连接"
            continue
        chk = getattr(res, "is_connected", None)
        if callable(chk):
            try:
                if not chk():
                    last_detail = "行情客户端未处于连接状态"
                    continue
            except Exception:  # noqa: BLE001
                pass  # is_connected 自身异常不作为失败依据
        return True, ""
    if last_detail:
        return False, last_detail
    return True, ""  # 无可用预检 API：放行，由后续真实调用暴露问题

def _scan_qmt_listeners() -> list[dict]:
    """扫描本机 58600-58620 端口段，返回监听端口的进程归属（仅 Windows）。

    返回 [{"port": int, "pid": int, "process": "miniquote.exe"}, ...]，按端口升序。
    端口段依据迅投系客户端惯例：
      - 58610：miniquote.exe 行情服务（小窗口 XtMiniQmt / 大窗口的独立行情子进程）
      - 58600：XtItClient.exe 大窗口交易端口（xttrader 可用；xtdata 行情 RPC 不可用，
        连接会报「未找到处理函数」，因此不能把 58600 当作行情端口）
    任何一步失败都返回已收集到的部分结果，绝不抛异常。
    """
    out: list[dict] = []
    if os.name != "nt":
        return out
    import subprocess
    # 1) netstat 拿 LISTENING 端口 -> PID
    try:
        ns = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True,
            timeout=10, errors="ignore",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout or ""
        listeners: dict[int, int] = {}
        for line in ns.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[3] == "LISTENING":
                try:
                    _addr, port = parts[1].rsplit(":", 1)
                    port = int(port)
                    pid = int(parts[4])
                except ValueError:
                    continue
                if 58600 <= port <= 58620:
                    listeners.setdefault(port, pid)
        if not listeners:
            return out
    except Exception:  # noqa: BLE001
        return out
    # 2) tasklist CSV 拿 PID -> 进程名
    proc_by_pid: dict[int, str] = {}
    try:
        tl = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True,
            timeout=10, errors="ignore",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout or ""
        for line in tl.splitlines():
            cells = [c.strip('"') for c in line.split('","')]
            if len(cells) >= 2:
                try:
                    proc_by_pid[int(cells[1])] = cells[0].lower()
                except ValueError:
                    continue
    except Exception:  # noqa: BLE001
        pass
    for port in sorted(listeners):
        pid = listeners[port]
        out.append({"port": port, "pid": pid,
                    "process": proc_by_pid.get(pid, "")})
    return out


def _probe_quote_service(client_path: str) -> dict:
    """深度诊断行情服务不可达的根因（纯标准库：进程 / 端口 / 配置三路探测）。

    大窗口 / 小窗口全兼容：自动识别本机已运行的迅投系客户端类型——
      - 大窗口 XtItClient（完整客户端）：监听 58600 交易端口，xttrader 交易可用；
        但 xtdata 行情 RPC 需要小窗口（XtMiniQmt / miniquote / 独立行情）提供。
      - 小窗口 XtMiniQmt + miniquote：监听 58610 行情端口，行情 + 交易均可用。
    在 xtdata.connect()/get_client() 失败后调用，主动探测：
      - 58600-58620 端口段的监听与进程归属（miniquote = 行情服务已就绪）
      - xtdata.cfg 配置的客户端根目录是否与 client_path 一致（悬空/错指向检测）
    返回结构化结果供上层生成精确指引；任何探测异常都吞掉，绝不因诊断失败阻断主流程。
    """
    import socket
    res = {
        "client_running": False,
        "listening_ports": [],
        "expected_port": 58610,
        "cfg_port": None,
        "cfg_root": None,
        "cfg_root_exists": None,
        "cfg_matches": None,
        "bin_dir": None,
        # ---- 大/小窗口识别（新增）----
        "port_map": [],          # [{port, pid, process}] 58600-58620 监听明细
        "full_client_running": False,   # 大窗口 XtItClient 在运行
        "mini_client_running": False,   # 小窗口 XtMiniQmt 在运行
        "quote_ports": [],       # miniquote 监听的行情端口（xtdata 可连）
        "trade_ports": [],       # XtItClient 监听的交易端口
        "client_type": "none",   # full / mini / both / none
        "quote_service_ok": False,  # 行情端口已监听（可能仍需登录才出实时数据）
    }
    # 1) 据 client_path 推断客户端根目录 / bin.x64（复用现有候选根逻辑）
    bin_dir = None
    try:
        for r in _candidate_roots(client_path):
            b = os.path.join(r, "bin.x64")
            if os.path.isdir(b):
                bin_dir = b
                break
    except Exception:  # noqa: BLE001
        pass
    res["bin_dir"] = bin_dir

    # 2) 行情端口：默认 58610；扫描 %USERPROFILE%/.xtquant/*/xtdata.cfg 看是否被覆盖
    ports = {58610}
    cfg_root, cfg_port, cfg_matches, cfg_root_exists = None, None, None, None
    try:
        import glob
        import json as _json
        for cfg in glob.glob(os.path.join(
                os.environ.get("USERPROFILE", ""), ".xtquant", "*", "xtdata.cfg")):
            try:
                data = _json.load(open(cfg, "r", encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            p_ = data.get("port")
            if p_:
                ports.add(int(p_))
                cfg_port = int(p_)
            root_dir = data.get("root_dir")
            if root_dir:
                cfg_root = root_dir
                try:
                    cfg_root_exists = os.path.isdir(root_dir)
                except Exception:  # noqa: BLE001
                    cfg_root_exists = None
                try:
                    nc = _normalize(client_path or "")
                    cfg_matches = (
                        nc in (_normalize(cfg_root) + os.sep)
                        or _normalize(os.path.join(cfg_root, "userdata_mini")) == nc
                        or _normalize(cfg_root) == nc)
                except Exception:  # noqa: BLE001
                    cfg_matches = None
    except Exception:  # noqa: BLE001
        pass
    res.update(expected_port=58610, cfg_port=cfg_port, cfg_root=cfg_root,
               cfg_root_exists=cfg_root_exists, cfg_matches=cfg_matches)

    # 3) 端口监听探测 + 进程归属识别（大/小窗口全兼容）
    #    cfg 端口与默认 58610 用 TCP 短超时探测（跨平台兜底）；Windows 下额外用
    #    netstat+tasklist 扫描 58600-58620 段并识别进程归属：
    #    miniquote.exe -> 行情端口（xtdata 可连）；XtItClient.exe -> 大窗口交易端口。
    try:
        for port in sorted(ports):
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(1.0)
            try:
                if s.connect_ex(("127.0.0.1", port)) == 0 \
                        and port not in res["listening_ports"]:
                    res["listening_ports"].append(port)
            finally:
                s.close()
    except Exception:  # noqa: BLE001
        pass
    port_map = _scan_qmt_listeners()
    res["port_map"] = port_map
    for item in port_map:
        proc = (item.get("process") or "").lower()
        port = item.get("port")
        if port not in res["listening_ports"]:
            res["listening_ports"].append(port)
        if "miniquote" in proc:
            res["quote_ports"].append(port)
        elif "xtitclient" in proc or "xtclient" in proc or "itclient" in proc:
            res["trade_ports"].append(port)

    # 4) 客户端进程检测：大窗口 / 小窗口分别识别（含未监听端口的运行中进程），
    #    并记录实际启动的 exe 名（供 auto 模式按「用户启动了哪个 exe」判断）。
    res["running_exes"] = []   # 实际运行的客户端主程序名，如 XtItClient.exe / XtMiniQmt.exe
    try:
        import subprocess
        if os.name == "nt":
            out = subprocess.run(
                ["tasklist", "/FO", "CSV"], capture_output=True, text=True,
                timeout=8, errors="ignore",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout or ""
            low = out.lower()
            res["running_exes"] = sorted({
                ln.split(",")[0].strip('"') for ln in out.splitlines()
                if any(k in ln.lower() for k in (
                    "xtitclient", "xtclient", "xtmini",
                    "xtminiqmt", "miniqmt", "miniquote", "xtminiqt",
                    "xtquant", "xtmonitor"))})
            res["full_client_running"] = any(
                exe in low for exe in ("xtitclient.exe", "xtclient.exe"))
            res["mini_client_running"] = any(
                exe in low for exe in ("xtminiqmt.exe", "miniqmt.exe",
                                       "miniquote.exe", "xtminiqt.exe"))
            res["client_running"] = (res["full_client_running"]
                                     or res["mini_client_running"])
    except Exception:  # noqa: BLE001
        pass
    # miniquote 可能作为大窗口的「独立行情」子进程运行（无 XtMiniQmt 主进程）；
    # 反之 XtItClient 监听了 58600 也可确认大窗口在运行。
    if res["quote_ports"] and not res["mini_client_running"]:
        res["mini_client_running"] = True
    if res["trade_ports"] and not res["full_client_running"]:
        res["full_client_running"] = True
    res["client_type"] = (
        "both" if res["full_client_running"] and res["mini_client_running"]
        else "full" if res["full_client_running"]
        else "mini" if res["mini_client_running"] else "none")
    res["quote_service_ok"] = bool(res["quote_ports"])
    return res

# 各模式客户端主程序 exe（bin.x64 下按优先级匹配；覆盖各券商白标命名）
_FULL_EXE_NAMES = ("XtItClient.exe", "XtClient.exe", "XtMini.exe")
_MINI_EXE_NAMES = ("XtMiniQmt.exe", "MiniQmt.exe", "XtMiniQt.exe")
_QUOTE_EXE_NAMES = ("miniquote.exe",)


def _running_client_exes() -> list:
    """轻量探测本机正在运行的 QMT 相关进程名（best-effort，失败返回 []）。"""
    try:
        import subprocess
        if os.name != "nt":
            return []
        out = subprocess.run(
            ["tasklist", "/FO", "CSV"], capture_output=True, text=True,
            timeout=6, errors="ignore",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout or ""
        return sorted({
            ln.split(",")[0].strip('"') for ln in out.splitlines()
            if any(k in ln.lower() for k in (
                "xtitclient", "xtclient", "xtmini", "xtminiqmt",
                "miniqmt", "miniquote", "xtmonitor"))})
    except Exception:  # noqa: BLE001
        return []


def _latest_login_log(trade_dir: str) -> str:
    """从客户端交易日志中提取最近一次“登录成功”记录（best-effort）。"""
    try:
        log_dir = os.path.join(trade_dir, "log")
        if not os.path.isdir(log_dir):
            return ""
        logs = [f for f in os.listdir(log_dir)
                if f.startswith("XtClient_") and f.endswith(".log")]
        if not logs:
            return ""
        # 优先取“主连接日志”(XtClient_YYYYMMDD.log，纯日期、无附加后缀)，
        # 避免选中 FormulaOutput / Debug / PerformanceFile / Message 等辅助日志。
        import re as _re
        _date = _re.compile(r"^XtClient_\d{8}\.log$").match
        _pool = [f for f in logs if _date(f)] or logs
        newest = max(_pool,
                     key=lambda f: os.path.getmtime(os.path.join(log_dir, f)))
        data = open(os.path.join(log_dir, newest), "rb").read()
        txt = data.decode("gb18030", errors="ignore")
        # 交易登录成功在主日志里的标记词
        hits = [ln.strip() for ln in txt.splitlines()
                if ("LoginSuccess" in ln or "登录成功" in ln)][-1:]
        return f"{newest}: {hits[0][-90:] if hits else '未找到登录成功记录'}"
    except Exception:  # noqa: BLE001
        return ""


def _find_client_exe(root: str, names: tuple[str, ...]) -> str | None:
    """在客户端根（或其 bin.* 子目录）下定位指定 exe（忽略大小写）。"""
    if not root or not os.path.isdir(root):
        return None
    want = {n.lower() for n in names}
    search_dirs = [root]
    try:
        for ent in os.listdir(root):
            if ent.lower() in ("bin.x64", "bin", "bin32", "bin_x64", "bin_x32"):
                search_dirs.append(os.path.join(root, ent))
    except OSError:
        pass
    for d in search_dirs:
        if not os.path.isdir(d):
            continue
        try:
            for ent in os.listdir(d):
                if ent.lower() in want:
                    p = os.path.join(d, ent)
                    if os.path.isfile(p):
                        return p
        except OSError:
            continue
    return None


def launch_client(client_path: str, mode: str = "full") -> dict:
    """按模式启动 QMT 客户端主程序（GUI，保留窗口）。

    mode：
      - "full"  -> 完整版大客户端 XtItClient.exe（交易+行情一体，数据目录 userdata）
      - "mini"  -> 极速版 XtMiniQmt.exe（MiniQMT，数据目录 userdata_mini）
      - "quote" -> 独立行情小窗口 miniquote.exe（为完整版补齐 58610 行情服务）
    已运行则不重复启动。返回结构化结果 {launched, already_running, exe, hint}。
    """
    names = (_FULL_EXE_NAMES if mode == "full"
             else _MINI_EXE_NAMES if mode == "mini"
             else _QUOTE_EXE_NAMES)
    try:
        roots = _candidate_roots(client_path or "")
    except Exception:  # noqa: BLE001
        roots = []
    root = roots[0] if roots else (_normalize(client_path) if client_path else "")
    exe = _find_client_exe(root, names) if root else None
    if not exe:
        return {"launched": False, "already_running": False, "exe": "",
                "hint": f"未在 {root or client_path or '客户端根'} 找到"
                        f"{'/'.join(names)}（请先安装对应模式的 QMT 客户端）"}
    # 已运行：直接返回，避免重复拉起多个实例
    try:
        probe = _shell_attr("_probe_quote_service")(client_path)  # monkeypatch 兼容：经壳模块动态查找（见 _common._shell_attr）
        if mode == "full" and probe.get("full_client_running"):
            return {"launched": False, "already_running": True, "exe": exe,
                    "hint": f"完整版大客户端已在运行（{probe.get('running_exes')}）"}
        if mode in ("mini", "quote") and probe.get("mini_client_running"):
            return {"launched": False, "already_running": True, "exe": exe,
                    "hint": "极速版/独立行情已在运行"}
    except Exception:  # noqa: BLE001
        pass
    try:
        import subprocess
        kwargs = {"cwd": os.path.dirname(exe)}
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        subprocess.Popen([exe], **kwargs)  # noqa: S603  GUI 客户端，保留窗口
    except Exception as exc:  # noqa: BLE001
        return {"launched": False, "already_running": False, "exe": exe,
                "hint": f"启动失败：{exc}"}
    return {"launched": True, "already_running": False, "exe": exe,
            "hint": f"已启动 {os.path.basename(exe)}，请在弹出的窗口中完成登录后重试连接。"}

def _effective_trade_dir(client_path: str, mode: str = "auto") -> tuple[str, str]:
    """解析 XtQuantTrader 实际使用的数据目录（区分极速版 / 完整版大客户端）。

    迅投 QMT 两种启动模式的数据目录不同：
      - 极速版（XtMiniQmt / miniquote，MiniQMT）：userdata_mini
      - 完整版大客户端（XtItClient，普通模式）：userdata
    行情侧（xtdata，端口 58610）两种模式共用、与此无关；但交易侧 XtQuantTrader
    必须指向正确的数据目录——这是「完整版大客户端连接不上」的根因之一（配了
    userdata_mini 而实际跑大客户端，或反之）。

    解析规则：
      - mode="mini"：强制 <根>/userdata_mini（存在才用，否则原样返回交由上层报错）
      - mode="full"：强制 <根>/userdata
      - mode="auto"（默认）：按「实际运行场景 + client_path 后缀 + 目录存在性」推断——
        1) client_path 已明确带 userdata_mini / userdata 后缀 → 直接按后缀；
        2) 否则探测本机运行中的客户端（_probe_quote_service 的 client_type）：
           mini 在跑→极速版、full 在跑→完整版、both→优先极速版（行情+交易一体）；
        3) 仍不确定 → 按目录存在性：userdata_mini 优先，其次 userdata。
    返回 (trade_dir, resolved_mode)；找不到任何存在目录时回退原始 client_path
    （由连接流程给出明确的「目录不存在」错误）。
    """
    if not client_path:
        return client_path, (mode or "auto")
    try:
        roots = _candidate_roots(client_path)
    except Exception:  # noqa: BLE001
        roots = []
    base = roots[0] if roots else _normalize(client_path)
    mini_dir = os.path.join(base, "userdata_mini")
    full_dir = os.path.join(base, "userdata")
    has_mini = mini_dir if os.path.isdir(mini_dir) else ""
    has_full = full_dir if os.path.isdir(full_dir) else ""
    cp_low = _normalize(client_path).lower()
    suffix = ""
    if cp_low.endswith("userdata_mini"):
        suffix = "mini"
    elif cp_low.endswith("userdata"):
        suffix = "full"
    m = (mode or "auto").lower()
    if m == "mini":
        return (has_mini or client_path), "mini"
    if m == "full":
        return (has_full or client_path), "full"
    # auto：后缀优先
    if suffix == "mini" and has_mini:
        return has_mini, "mini"
    if suffix == "full" and has_full:
        return has_full, "full"
    # auto：按运行场景推断（纯诊断探测，无副作用）
    #
    # 关键：大客户端 XtItClient（userdata）+ 独立行情 miniquote（userdata_mini）可**同时运行**
    # （client_type="both"，交易 58600 + 行情 58610 双端口就绪）。此时交易侧必须走完整版
    # userdata 才能读写真实账户/持仓；配成 userdata_mini 会「行情正常但交易取不到数据」。
    # 因此 both / full 一律优先 userdata（完整版），仅 mini 独跑时才选 userdata_mini。
    try:
        probe = _shell_attr("_probe_quote_service")(client_path)  # monkeypatch 兼容：经壳模块动态查找（见 _common._shell_attr）
        ctype = probe.get("client_type")
        if ctype in ("full", "both") and has_full:
            return has_full, "full"
        if ctype == "mini" and has_mini:
            return has_mini, "mini"
        # none：落到目录存在性（下面处理）
    except Exception:  # noqa: BLE001
        pass
    # 目录存在性：完整版 userdata 优先于极速版 userdata_mini
    # （多数券商默认主数据目录是 userdata；仅 userdata_mini 专属时选它）
    if has_mini and not has_full:
        return has_mini, "mini"
    if has_full:
        return has_full, "full"
    return client_path, "auto"

# ---------------- 多版本能力矩阵与版本画像 ----------------
# QMT 存在「完整版大客户端（XtItClient）」、「极速版 MiniQMT（XtMiniQmt + miniquote）」
# 两种启动模式，且各券商/各年代客户端的 xtquant SDK 能力参差。为优雅支持"全功能版本"
# 与"仅部分功能的极简版本"，这里把「版本指纹」与「能力矩阵」显式建模：
#   版本画像（QmtVersionProfile）  = 客户端类型 + 版本号 + SDK 版本 + 交易目录 + 能力矩阵
#   能力矩阵（QmtCapabilities）    = 该客户端实际能提供的功能集合（缺即 False，不臆测）
# 上游（discovery 候选 / /brokers/test / 连接结果 / 前端）统一消费 image_to_dict，
# 前端据此展示"检测到什么版本、支持哪些功能"，并可按能力自动禁用入口。


@dataclass
class QmtCapabilities:
    """按客户端类型 / 账户类型 / 版本推导的运行期能力集。

    唯一事实来源是「本适配器（XTPQuantAdapter）真实实现的 API」+「连接时实际可用」
    两层。这里只标注适配器统一具备的基础能力；极简版（仅行情、无完整交易）或
    特定账户类型下，部分能力由构造参数/运行时状态裁剪，缺则置 False，绝不臆测。
    """
    quote: bool = True            # 实时行情（xtdata get_quote/get_full_tick）
    kline: bool = True            # 历史 K 线（xtdata get_kline）
    stock_list: bool = True       # 板块股票列表
    sector: bool = True           # 板块列表 / 成分
    trading_calendar: bool = True # 交易日历
    trade: bool = True            # 交易（下单/撤单/委托/成交）
    account: bool = True          # 账户 / 资金 / 持仓
    condition_order: bool = False # 条件单 / 多条件单（依赖账号权限与版本，连接成功才置真）
    credit: bool = False          # 融资融券
    option: bool = False          # 期权
    futures: bool = False         # 期货
    l2_tick: bool = False         # Level-2 逐笔（需账号订阅权限）
    financial: bool = True        # 财务数据（xtdata get_stock_financial）
    realtime_push: bool = False   # 实时成交/委托推送（仅配置交易账号且已连接）

    def as_list(self) -> list[str]:
        return [k for k, v in self.__dict__.items() if v]

    def to_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class QmtVersionProfile:
    """一个 QMT 客户端安装的完整版本画像（前端展示 / 排障用）。"""
    client_type: str = "unknown"    # mini / full / both / unknown（本机运行场景）
    client_mode: str = "auto"       # auto / mini / full（解析出的交易目录模式）
    version_str: str = ""           # 客户端主程序版本，如 "7.2.1"（未知留空）
    sdk_version: str = ""           # xtquant SDK 版本，如 "4.1.0"（未知留空）
    trade_dir: str = ""             # XtQuantTrader 实际使用的数据目录
    quote_port: int = 58610         # 行情服务端口
    trade_port: int = 0             # 大客户端交易端口（58600，未监听为 0）
    broker_name: str = ""           # 从 Config.xml 读出的真实券商名（未知留空）
    account_id: str = ""            # 资金账号（未配置留空）
    account_type: str = "STOCK"     # STOCK / CREDIT / OPTION / FUTURES
    capabilities: QmtCapabilities = field(default_factory=QmtCapabilities)
    detail: str = ""                # 人类可读的诊断文案

    def to_dict(self) -> dict:
        return {
            "client_type": self.client_type,
            "client_mode": self.client_mode,
            "version_str": self.version_str,
            "sdk_version": self.sdk_version,
            "trade_dir": self.trade_dir,
            "quote_port": self.quote_port,
            "trade_port": self.trade_port,
            "broker_name": self.broker_name,
            "account_id": self.account_id,
            "account_type": self.account_type,
            "capabilities": self.capabilities.to_dict(),
            "capabilities_list": self.capabilities.as_list(),
            "detail": self.detail,
        }


_VERSION_RE = re.compile(r"(?i)(?:\bversion\b|\bver\b)\s*[:=]\s*([0-9][0-9a-zA-Z._-]*)")


def _detect_version_str(client_path: str) -> str:
    """从客户端目录读取主程序版本（best-effort，未知返回 ''）。

    候选来源：bin.x64/version.txt、bin.x64/version.ini、<根>/version.txt。
    内容多为 "Version=7.23.1" 或 "version: 4.0.0" 之类键值，取其首个版本号。
    """
    if not client_path:
        return ""
    try:
        roots = _candidate_roots(client_path)
    except Exception:  # noqa: BLE001
        roots = []
    base = roots[0] if roots else _normalize(client_path)
    for rel, names in (("", ("version.txt", "version.ini")),
                       ("bin.x64", ("version.txt", "version.ini"))):
        for fname in names:
            p = os.path.join(base, rel, fname) if rel else os.path.join(base, fname)
            try:
                if not os.path.isfile(p):
                    continue
                with open(p, "r", encoding="utf-8", errors="ignore") as f:
                    txt = (f.read() or "")[:4000]
                m = _VERSION_RE.search(txt)
                if m:
                    return m.group(1)
            except Exception:  # noqa: BLE001
                continue
    return ""


def _detect_sdk_version(client_path: str) -> str:
    """读取客户端自带 xtquant 的 __version__（best-effort，未知返回 ''）。"""
    if not client_path:
        return ""
    try:
        sp = _resolve_xtquant_path(client_path)
    except Exception:  # noqa: BLE001
        sp = None
    if not sp:
        return ""
    pkg = os.path.join(sp, "xtquant", "__init__.py")
    try:
        if os.path.isfile(pkg):
            with open(pkg, "r", encoding="utf-8", errors="ignore") as f:
                txt = f.read()
            m = re.search(r"(?m)^\s*__version__\s*=\s*['\"]([^'\"]+)['\"]", txt)
            if m:
                return m.group(1).strip()
    except Exception:  # noqa: BLE001
        pass
    return ""


def _infer_capabilities(client_type: str, account_type: str,
                        account_id: str = "", realtime_push: bool = False) -> QmtCapabilities:
    """由客户端类型 / 账户类型推导能力矩阵（仅裁剪「本就可选/依赖权限」项）。

    基础能力（行情/K线/板块/交易/账户/财务）是本适配器统一实现的，恒为 True。
    依赖账号/权限/运行状态的能力按以下规则收敛，缺则 False，绝不臆测：
      - credit/option/futures：仅当账户类型匹配时置 True
      - condition_order：需交易账号 + 连接期确认真实可用才由外部置 True
      - l2_tick：需 Level-2 订阅权限，默认 False（连接成功后由运行时探测覆盖）
      - realtime_push：仅配置了交易账号时按参数置真
    """
    caps = QmtCapabilities()
    at = (account_type or "STOCK").upper()
    caps.credit = (at == "CREDIT")
    caps.option = (at == "OPTION")
    caps.futures = (at == "FUTURES")
    caps.realtime_push = bool(realtime_push and account_id)
    # 极简行情版（未配交易账号）：交易/账户不可用，行情仍可用
    if not account_id:
        caps.trade = False
        caps.account = False
        caps.condition_order = False
    return caps


def build_version_profile(client_path: str, client_mode: str = "auto",
                          account_id: str = "", account_type: str = "STOCK",
                          realtime_push: bool = False,
                          probe: dict | None = None) -> QmtVersionProfile:
    """构建一个客户端安装的版本画像（纯静态探测，不连接券商、不 import xtquant）。

    这是「支持所有 QMT 版本」的收敛入口：无论完整版 XtItClient 还是只提供部分
    功能的极速 MiniQMT，都归一成一份 {类型 + 版本 + 能力矩阵} 画像，供 discovery /
    /brokers/test / 连接结果 / 前端统一展示与按能力路由入口。
    """
    p = QmtVersionProfile()
    p.client_mode = (client_mode or "auto").lower()
    p.account_id = account_id or ""
    p.account_type = (account_type or "STOCK").upper()
    p.version_str = _detect_version_str(client_path)
    p.sdk_version = _detect_sdk_version(client_path)
    # 交易目录 + 解析出的模式（auto 推断出的 mini/full 回填到 client_mode）
    trade_dir, resolved_mode = _effective_trade_dir(client_path, p.client_mode)
    p.trade_dir = trade_dir
    if resolved_mode in ("mini", "full"):
        p.client_mode = resolved_mode
    # 运行场景（本机在跑 full/mini/both）+ 端口：优先复用调用方已探测结果，避免重复探测
    if probe is None:
        try:
            probe = _shell_attr("_probe_quote_service")(client_path)  # monkeypatch 兼容：经壳模块动态查找（见 _common._shell_attr） if client_path else {}
        except Exception:  # noqa: BLE001
            probe = {}
    probe = probe or {}
    p.client_type = probe.get("client_type") or "unknown"
    qports = probe.get("quote_ports") or []
    tports = probe.get("trade_ports") or []
    p.quote_port = int(qports[0]) if qports else int(probe.get("expected_port") or 58610)
    p.trade_port = int(tports[0]) if tports else 0
    # 真实券商名：优先复用 probe 中的 broker_name（discovery 已从 Config.xml 读出）
    p.broker_name = probe.get("broker_name") or ""
    p.capabilities = _infer_capabilities(p.client_type, p.account_type,
                                         p.account_id, realtime_push)
    # 可读诊断文案：说明识别到哪种客户端、支持到什么程度
    type_label = {
        "mini": "极速版 MiniQMT（仅提供行情 + 极简交易通道）",
        "full": "完整版大客户端（交易 + 行情一体）",
        "both": "完整版 + 极速版 同时运行（交易走完整版）",
        "none": "未检测到运行中的客户端进程",
    }.get(p.client_type, "未知客户端类型")
    mode_label = ("极速版 userdata_mini" if p.client_mode == "mini"
                  else "完整版 userdata")
    caps = p.capabilities
    if caps.trade:
        scope = "行情 + 交易"
    elif caps.quote:
        scope = "仅行情（未配置交易账户）"
    else:
        scope = "未知"
    p.detail = (f"识别到{type_label}（{mode_label}），能力范围：{scope}。"
                f"版本 {p.version_str or '未知'} / SDK {p.sdk_version or '未知'}")
    return p


__all__ = [
    'probe_environment',
    '_probe_xtdata',
    '_scan_qmt_listeners',
    '_probe_quote_service',
    '_FULL_EXE_NAMES',
    '_MINI_EXE_NAMES',
    '_QUOTE_EXE_NAMES',
    '_running_client_exes',
    '_latest_login_log',
    '_find_client_exe',
    'launch_client',
    '_effective_trade_dir',
    'QmtCapabilities',
    'QmtVersionProfile',
    '_VERSION_RE',
    '_detect_version_str',
    '_detect_sdk_version',
    '_infer_capabilities',
    'build_version_profile',
]

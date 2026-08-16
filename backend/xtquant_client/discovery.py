"""本机 QMT / MiniQMT 客户端自动发现（进程 + 安装扫描）。

用途：便携版/安装版启动后，无需用户手填 client_path 即可发现本机已安装/正在运行的
迅投系客户端（XtMiniQmt / miniquote / 各券商 QMT 交易端），自动推导 userdata_mini、
xtquant 定位与疑似券商档案，前端一键接入。

数据源：
1) 进程枚举（优先 psutil；未安装时退化为 PowerShell Get-CimInstance）：
   XtMiniQmt.exe / miniquote.exe 等运行中进程 -> 由 exe 路径推导客户端根
2) 安装目录扫描：常见券商安装位置 + 盘符顶层 *QMT* 目录（≤2 层）
"""
import logging
import os
import re
import subprocess
import sys

from .xtp import probe_environment, _is_system_dir

log = logging.getLogger("qmt_work.discovery")

# Windows：spawn 外部控制台程序（powershell/python）时隐藏其控制台窗口，
# 避免桌面运行时弹出 PowerShell 蓝窗 / 黑窗。其他平台该值为 0（无副作用）。
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

# 运行中的迅投系 QMT 相关进程名（小写）。覆盖各券商主程序 / 行情 / 交易进程：
# - 广发 / 迅投通用 ItClient：XtItClient.exe；部分券商为 XtClient.exe / XtMini.exe
# - 标准 MiniQMT：XtMiniQmt.exe / miniquote.exe / xtquant.exe
# - 交易端：xttrader.exe / quant_trader.exe / stock_trader.exe / xttraderapi.exe
_QMT_PROC_NAMES = {
    "xtminiqmt.exe", "miniqmt.exe", "miniquote.exe", "xtquant.exe",
    "xtmonitor.exe", "xtdaemon.exe", "quant_trader.exe", "stock_trader.exe",
    "xttrader.exe", "xttraderapi.exe",
    "xtitclient.exe", "xtclient.exe", "xtmini.exe",
    "stockclient.exe", "qmtclient.exe", "xtp.exe",
}
_QMT_PROC_KEYWORDS = ("qmt", "xtmini", "miniquote", "xtitclient",
                      "xtclient", "itclient", "xttrader", "stocktrader")

# 需排除的进程（本平台自身进程名 / 路径含 qmt_work / workbuddy 会误命中）
_QMT_EXCLUDE_KEYWORDS = ("qmt_work", "workbuddy")

# 客户端 exe 强信号：进程路径落在这些典型目录片段内，几乎一定是迅投系客户端。
# 注：本平台 qmt_work.exe 路径含 "qmt_work" 已由上面排除关键词提前拦截，不会误命中。
_QMT_CLIENT_DIR_RE = re.compile(
    r"[\\/](bin\.x64|bin\.x32|bin_x64|bin32|userdata_mini|userdata|"
    r"qmt|xtquant|miniqmt|xtmini)[\\/]")

# 客户端内部的辅助进程（CEF 渲染壳 / 浏览器内核 / node 等），虽与主程序同在 bin.x64
# 下，但非交易主程序，排除以避免进程枚举虚高与潜在误判。
_QMT_AUX_EXCLUDE = ("cefviewwing", "chrome", "node", "edge", "electron", "webkit")

# 常见安装位置（一级探测）
_COMMON_ROOTS = [
    r"C:\国金证券QMT交易端", r"C:\国金QMT",
    r"C:\华鑫证券\奇点QMT交易端", r"C:\华鑫证券QMT",
    r"C:\银河证券QMT交易端", r"C:\银河QMT",
    r"C:\中信建投QMT交易端", r"C:\中信建投QMT",
    r"C:\兴业证券QMT交易端", r"C:\兴业QMT",
    r"C:\广发证券QMT交易端", r"C:\广发QMT", r"C:\广发证券\QMT",
    r"C:\QMT", r"C:\迅投QMT", r"C:\MiniQMT", r"D:\QMT", r"D:\MiniQMT",
]

# 根路径关键词 -> 疑似券商档案 id
_ROOT_HINTS = [
    ("广发", "gf"), ("gdzq", "gf"), ("gd_qmt", "gf"), ("国金", "guojin"),
    ("华鑫", "huaxin"), ("奇点", "huaxin"), ("银河", "yinhe"),
    ("建投", "zxjt"), ("兴业", "xy"), ("中信", "zxjt"),
]


def guess_broker_id(root: str) -> str:
    """按客户端根路径关键词猜测券商档案 id（无匹配返回 ''）。"""
    low = (root or "").lower()
    for kw, bid in _ROOT_HINTS:
        if kw.lower() in low:
            return bid
    return ""


def _is_qmt_proc(name: str, exe: str) -> bool:
    """进程是否属于 QMT 系（排除本平台自身进程）。

    判定优先级（越稳越前）：
    1) 排除关键词（qmt_work / workbuddy）直接剔除本平台自身进程；
    2) 显式进程名白名单（XtItClient / XtMiniQmt / xttrader ...）；
    3) exe 路径落在客户端典型目录（bin.x64 / userdata_mini / qmt / xtquant ...）——最强信号，
       覆盖所有「进程名不固定但装在 bin.x64 下」的券商客户端；
    4) 进程名关键词兜底。
    """
    low = (name or "").lower()
    ex = (exe or "").lower()
    if any(k in low or k in ex for k in _QMT_EXCLUDE_KEYWORDS):
        return False
    # 排除客户端内部的辅助进程（CEF 渲染壳等），只认交易主程序
    if any(k in low for k in _QMT_AUX_EXCLUDE):
        return False
    if low in _QMT_PROC_NAMES:
        return True
    if _QMT_CLIENT_DIR_RE.search(ex):
        return True
    return any(k in low for k in _QMT_PROC_KEYWORDS)


def _ps_enum() -> list[dict]:
    """枚举进程 name/pid/exe_path（psutil 优先，退化 PowerShell）。"""
    try:
        import psutil  # type: ignore
        out = []
        for p in psutil.process_iter(["pid", "name", "exe"]):
            try:
                out.append({"pid": p.info["pid"], "name": (p.info["name"] or "").lower(),
                            "exe": p.info["exe"] or ""})
            except Exception:  # noqa: BLE001
                pass
        return out
    except ImportError:
        pass
    try:
        # 列出全部进程（Name|PID|ExecutablePath），由 Python 端 _is_qmt_proc 统一判定，
        # 避免 PowerShell 端用固定关键字过滤（易漏新版券商进程，如 XtItClient.exe）。
        script = (
            "Get-CimInstance Win32_Process | "
            "ForEach-Object { '{0}|{1}|{2}' -f $_.Name, $_.ProcessId, $_.ExecutablePath }")
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                           capture_output=True, text=True, encoding="utf-8", timeout=20,
                           creationflags=CREATE_NO_WINDOW)
        out = []
        for line in (r.stdout or "").splitlines():
            parts = line.split("|", 2)
            if len(parts) == 3:
                out.append({"name": parts[0].strip().lower(),
                            "pid": parts[1].strip(), "exe": parts[2].strip()})
        return out
    except Exception:  # noqa: BLE001
        return []


def _root_from_exe(exe_path: str) -> str | None:
    """由进程 exe 路径推导客户端根（根/bin.x64/XtMiniQmt.exe -> 根）。"""
    if not exe_path:
        return None
    d = os.path.dirname(os.path.abspath(exe_path))
    b = os.path.basename(d).strip().lower()
    if b in ("bin.x64", "bin", "bin32", "bin_x64"):
        return os.path.dirname(d)
    return d


# 疑似客户端根的判定标记（目录或文件存在其一即视为疑似客户端根）。
# 覆盖：标准结构（bin.x64 / userdata_mini）、各券商主程序 exe、xtquant 包。
_CLIENT_MARKERS = (
    "bin.x64", "bin.x32", "bin_x64", "userdata_mini", "userdata",
    "XtItClient.exe", "XtMiniQmt.exe", "miniquote.exe", "xttrader.exe",
    "xtquant.exe", "stock_trader.exe", "quant_trader.exe", "xtmonitor.exe",
)


def _looks_like_client_root(d: str) -> bool:
    """目录是否像客户端根（含 bin.x64 / userdata_mini / 主程序 exe 等标记）。"""
    if not os.path.isdir(d):
        return False
    try:
        entries = os.listdir(d)
    except OSError:
        return False
    return any(m in entries for m in _CLIENT_MARKERS)


def _scan_installed() -> list[str]:
    """扫描常见安装位置 + 全部盘符下 ≤3 层疑似客户端根（去重、保留存在者）。

    相比旧版「仅盘符顶层 *QMT* 目录 / 深度 2 层」，新版扩展：
    1) 盘符从硬编码 C/D/E/P/F 改为遍历 A~Z 全部存在盘符 —— 适配装在 G:/H:/非
       默认盘的客户端（如某些券商客户端默认装到数据盘）；
    2) 深度从 2 提升到 3 层 —— 适配装在 D:\Program Files\Broker\QMT 之类 3 层路径；
    3) 增加 %ProgramFiles% / %ProgramFiles(x86)% / 用户目录下的券商常见子目录
       作为优先探测点（部分券商客户端默认装到 Program Files 下）；
    4) 客户端根判定标记：保留 bin.x64 / userdata_mini / 主程序 exe 等。

    防护：跳过 _SYSTEM_DIR_NAMES（Windows/AppData/Temp 等），避免误命中无关 xtquant
    （如 IDE 的 stub）；跳过 fake/*_test 调试目录。
    """
    import string as _str
    roots: list[str] = []
    seen: set[str] = set()

    def _add_root(r: str):
        r = os.path.abspath(r)
        base = os.path.basename(r).lower()
        if "fake" in base or base.endswith("test") or base.endswith("_test"):
            return
        if _looks_like_client_root(r) and r not in seen:
            seen.add(r)
            roots.append(r)

    # 1) 常见安装位置（一级探测）
    for r in _COMMON_ROOTS:
        _add_root(r)
    # 2) Program Files / 用户目录下券商常见子目录（深度 2 内）
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pfx = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    userprofile = os.environ.get("USERPROFILE", "")
    for base_dir in [pf, pfx, userprofile]:
        if base_dir and os.path.isdir(base_dir):
            try:
                for top in os.listdir(base_dir):
                    d = os.path.join(base_dir, top)
                    if not os.path.isdir(d):
                        continue
                    _add_root(d)
                    try:
                        for sub in os.listdir(d):
                            d2 = os.path.join(d, sub)
                            if os.path.isdir(d2):
                                _add_root(d2)
                    except OSError:
                        continue
            except OSError:
                continue
    # 3) 全部盘符下 ≤3 层扫描
    for drive_letter in _str.ascii_uppercase:
        base = f"{drive_letter}:\\"
        if not os.path.isdir(base):
            continue
        try:
            top_entries = os.listdir(base)
        except OSError:
            continue
        for name in top_entries:
            d1 = os.path.join(base, name)
            if not os.path.isdir(d1):
                continue
            _add_root(d1)  # 第 1 层
            try:
                level1 = os.listdir(d1)
            except OSError:
                continue
            for sub1 in level1:
                d2 = os.path.join(d1, sub1)
                if not os.path.isdir(d2):
                    continue
                _add_root(d2)  # 第 2 层
                try:
                    level2 = os.listdir(d2)
                except OSError:
                    continue
                for sub2 in level2:
                    d3 = os.path.join(d2, sub2)
                    if not os.path.isdir(d3):
                        continue
                    # 第 3 层：跳过系统目录（防 IDE/AppData 误命中无关 xtquant stub）
                    if _is_system_dir(d3):
                        continue
                    _add_root(d3)
    return roots


def _candidate(root: str, running: bool = False, pid: str = "",
               proc: str = "") -> dict | None:
    """由客户端根构造候选（含 userdata_mini / xtquant 定位 / 券商猜测）。"""
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        return None
    ud = os.path.join(root, "userdata_mini")
    client_path = ud if os.path.isdir(ud) else root
    # light=True：auto-detect 阶段只做轻量定位，不 import xtquant、不扫运行时，避免每条
    # 候选触发昂贵扫描导致 auto-detect 慢/超时；完整诊断在用户点击候选后由 /brokers/test 给出。
    probe = probe_environment(client_path, light=True)
    return {
        "root": root,
        "name": os.path.basename(root) or root,
        "broker_id": guess_broker_id(root),
        "running": bool(running),
        "pid": str(pid) if pid else "",
        "process": proc,
        "client_path": client_path,
        "has_userdata_mini": os.path.isdir(ud),
        "has_bin_x64": probe["has_bin_x64"],
        "xtquant_found": probe["xtquant_found"],
        "xtquant_site": probe["xtquant_site"],
        "xtquant_importable": probe["xtquant_importable"],
        "import_error": (probe.get("import_error") or "")[:300],
        "hint": probe.get("hint", ""),
        "runtime_mode": probe.get("runtime_mode"),
        "bridge_feasible": probe.get("bridge_feasible", False),
        "suggested_abi": probe.get("suggested_abi"),
    }


def discover() -> list[dict]:
    """发现本机 QMT 客户端候选（运行中优先，安装扫描兜底；去重）。

    每个候选的探测（probe）异常会被隔离：单个客户端探测失败不影响整体发现，
    避免「某候选抛异常导致整个 auto-detect 失败」的级联故障。
    """
    cands: list[dict] = []
    seen: set[str] = set()

    def _add(root: str, **kw):
        if not root:
            return
        root = os.path.abspath(root)
        if root in seen or not os.path.isdir(root):
            return
        try:
            c = _candidate(root, **kw)
        except Exception as exc:  # noqa: BLE001
            log.warning("discover: 候选 %s 探测失败，已跳过：%s", root, exc)
            return
        if c is None:
            return
        seen.add(root)
        cands.append(c)

    # 1) 运行中的 QMT 进程（排除本平台自身 qmt_work 进程）
    for p in _ps_enum():
        if not _is_qmt_proc(p["name"], p["exe"]):
            continue
        _add(_root_from_exe(p["exe"]) or "", running=True, pid=p["pid"], proc=p["name"])
    # 2) 安装扫描（_scan_installed 已返回疑似客户端根）
    for root in _scan_installed():
        _add(root, running=False)

    # 排序：运行中优先 -> xtquant 可导入优先 -> xtquant 已定位优先
    cands.sort(key=lambda c: (not c["running"], not c["xtquant_importable"],
                              not c["xtquant_found"]))
    return cands

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

from .xtp import _resolve_xtquant_path, probe_environment

log = logging.getLogger("qmt_work.discovery")

# 运行中的迅投系 QMT 相关进程名（小写）
_QMT_PROC_NAMES = {
    "xtminiqmt.exe", "miniqmt.exe", "miniquote.exe", "xtquant.exe",
    "xtmonitor.exe", "xtdaemon.exe", "quant_trader.exe", "stock_trader.exe",
    "xttrader.exe",
}
_QMT_PROC_KEYWORDS = ("qmt", "xtmini", "miniquote")

# 需排除的进程（本平台自身进程名含 qmt 会误命中）
_QMT_EXCLUDE_KEYWORDS = ("qmt_work", "workbuddy")

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
    """进程是否属于 QMT 系（排除本平台自身进程）。"""
    low = (name or "").lower()
    ex = (exe or "").lower()
    if any(k in low or k in ex for k in _QMT_EXCLUDE_KEYWORDS):
        return False
    return low in _QMT_PROC_NAMES or any(k in low for k in _QMT_PROC_KEYWORDS)


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
        script = (
            "Get-CimInstance Win32_Process | "
            "Where-Object { $_.Name -match 'qmt|xtmini|mini' } | "
            "ForEach-Object { '{0}|{1}|{2}' -f $_.Name, $_.ProcessId, $_.ExecutablePath }")
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                           capture_output=True, text=True, timeout=15)
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


def _scan_installed() -> list[str]:
    """扫描常见安装位置 + 盘符顶层 *QMT* 目录（去重、保留存在者）。"""
    roots: list[str] = []
    seen: set[str] = set()
    for r in _COMMON_ROOTS:
        if os.path.isdir(r) and r not in seen:
            seen.add(r)
            roots.append(r)
    # 盘符顶层 *QMT*（一层）
    for drive in ("C:", "D:", "E:", "P:"):
        base = drive + "\\"
        if not os.path.isdir(base):
            continue
        try:
            for name in os.listdir(base):
                if "qmt" not in name.lower():
                    continue
                p = os.path.join(base, name)
                if os.path.isdir(p) and p not in seen:
                    seen.add(p)
                    roots.append(p)
        except OSError:
            continue
    return roots


def _candidate(root: str, running: bool = False, pid: str = "",
               proc: str = "") -> dict | None:
    """由客户端根构造候选（含 userdata_mini / xtquant 定位 / 券商猜测）。"""
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        return None
    ud = os.path.join(root, "userdata_mini")
    client_path = ud if os.path.isdir(ud) else root
    probe = probe_environment(client_path)
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
    """发现本机 QMT 客户端候选（运行中优先，安装扫描兜底；去重）。"""
    cands: list[dict] = []
    seen: set[str] = set()

    def _add(root: str, **kw):
        if not root:
            return
        root = os.path.abspath(root)
        if root in seen or not os.path.isdir(root):
            return
        c = _candidate(root, **kw)
        if c is None:
            return
        seen.add(root)
        cands.append(c)

    # 1) 运行中的 QMT 进程（排除本平台自身 qmt_work 进程）
    for p in _ps_enum():
        if not _is_qmt_proc(p["name"], p["exe"]):
            continue
        _add(_root_from_exe(p["exe"]) or "", running=True, pid=p["pid"], proc=p["name"])
    # 2) 安装扫描（仅保留疑似客户端根：含 bin.x64 / userdata_mini / xtquant）
    for root in _scan_installed():
        if not (os.path.isdir(os.path.join(root, "bin.x64"))
                or os.path.isdir(os.path.join(root, "userdata_mini"))):
            continue
        _add(root, running=False)

    # 排序：运行中优先 -> xtquant 可导入优先 -> xtquant 已定位优先
    cands.sort(key=lambda c: (not c["running"], not c["xtquant_importable"],
                              not c["xtquant_found"]))
    return cands

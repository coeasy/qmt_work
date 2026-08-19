"""ABI 运行时选择 + 多运行时 IPC 桥接的运行时解析（P0 核心）。

背景：迅投 xtquant 的 C 扩展（.pyd）按 CPython 小版本 ABI 编译（cp36 ~ cp312，
目前官方未发布 cp313）。主后端自身可能跑在任意 Python（例如未来用 3.13 打包时，
无法在进程内加载 cp311 的 .pyd）。本模块负责：

- 探测券商 xtquant 目录里实际存在的 ABI 变体；
- 枚举随包附带的极简嵌入式 Python 运行时（backend/runtimes/cp38 ~ cp312）；
- 按「ABI 匹配 + 最高可用」选择桥接子进程解释器；
- 无匹配时给出可操作的报错（ABINotSupportedError）。

若主后端自身的 ABI 就落在券商集合内，则无需桥接（进程内直接 import，最省资源）。
"""
import logging
import os
import re
import shutil
import subprocess
import sys

log = logging.getLogger("qmt_work.runtime")

# Windows：spawn 外部控制台程序（python/pythonw/py 启动器）做 ABI 探测时隐藏其控制台
# 窗口，避免桌面运行时首次连接券商时弹出黑窗。其他平台该值为 0（无副作用）。
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

# 支持的 ABI 小版本（cp36 ~ cp312；cp313 暂未发布 xtquant 变体）
_RUNTIME_DIR_RE = re.compile(r"^cp(\d+)$", re.IGNORECASE)
_PYD_RE = re.compile(r"\.cp(\d+)-win_amd64\.pyd$", re.IGNORECASE)


class ABINotSupportedError(Exception):
    """券商 xtquant 的 ABI 与所有可用运行时（主后端 + 桥接运行时）均不兼容。"""

    def __init__(self, message: str, broker_abis=None, available_runtimes=None):
        self.broker_abis = list(broker_abis or [])
        self.available_runtimes = list(available_runtimes or [])
        super().__init__(message)


def host_python_minor() -> int:
    """当前解释器 ABI 小版本（如 3.11 -> 311）。"""
    return sys.version_info[0] * 100 + sys.version_info[1]


def detect_xtquant_abis(xtquant_site: str | None) -> list[int]:
    """扫描 xtquant 目录里的 .cpXXX-win_amd64.pyd，返回存在的 ABI 小版本（升序）。

    同时检查子目录 `xtquant/`（IPythonApiClient.cp311-win_amd64.pyd 常在此）。

    返回值的编码与 host_python_minor() 一致（MAJOR*100 + MINOR，如 3.11 -> 311，
    3.6 -> 306），以便直接做 `host in broker_abis` 比较。注意 .pyd 文件名 cp36 表示
    CPython 3.6（捕获值 36），这里统一归一为 306；cp311（捕获值 311）保持不动。
    """
    if not xtquant_site or not os.path.isdir(xtquant_site):
        return []
    abis: set[int] = set()

    def _scan(d: str):
        try:
            for fn in os.listdir(d):
                m = _PYD_RE.search(fn)
                if m:
                    v = int(m.group(1))
                    # 归一化：cp36 (raw=36, Python 3.6) -> 306；cp311 (raw=311) 保持。
                    v = _normalize_abi(v)
                    abis.add(v)
        except OSError:
            pass

    _scan(xtquant_site)
    _scan(os.path.join(xtquant_site, "xtquant"))
    return sorted(abis)


def discover_bundled_runtimes(base_dir: str | None = None) -> dict[int, str]:
    """枚举随包附带的嵌入式 Python 运行时：<base>/runtimes/cpXXX/python.exe。

    返回 {minor(如 311 或 308): python_exe_path}。
    返回值编码与 host_python_minor() 一致（MAJOR*100 + MINOR，如 cp311 -> 311，
    cp38 -> 308），便于与 detect_xtquant_abis 的结果直接比较。
    """
    if base_dir is None:
        base_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "runtimes")
    out: dict[int, str] = {}
    if not os.path.isdir(base_dir):
        return out
    for name in os.listdir(base_dir):
        m = _RUNTIME_DIR_RE.match(name.strip().lower())
        if not m:
            continue
        raw = int(m.group(1))
        # 归一化：cp38 目录（raw=38）-> 308；cp311（raw=311）保持
        minor = _normalize_abi(raw)
        exe = os.path.join(base_dir, name, "python.exe")
        if os.path.isfile(exe):
            out[minor] = exe
            continue
        # POSIX 上叫 python / bin/python
        for cand in (os.path.join(base_dir, name, "bin", "python"),
                     os.path.join(base_dir, name, "python")):
            if os.path.isfile(cand):
                out[minor] = cand
                break
    return out


# ---------------- 系统 Python 发现（P0 兜底：无 bundled 运行时也能桥接） ----------------
# 进程内缓存，避免每次连接都 spawn `py` 启动器
_SYSTEM_RUNTIME_CACHE: dict[int, str] | None = None


def _normalize_abi(raw: int) -> int:
    """把 .pyd 文件名或目录名里的 ABI 数字归一化为 MAJOR*100+MINOR 编码。

    cp311（raw=311，已为 3*100+11）-> 311；
    cp38（raw=38，即 Python 3.8，raw=3*10+8）-> 3*100+8=308；
    cp39（raw=39）-> 309。与 host_python_minor() 返回格式一致。
    """
    if raw >= 100:
        return raw
    major = raw // 10
    minor = raw % 10
    return major * 100 + minor


def _sys_py_version(exe: str) -> int | None:
    """返回某 python.exe 的 ABI 小版本（如 311），不在 [3.8, 3.13] 返回 None。"""
    try:
        out = subprocess.run(
            [exe, "-c", "import sys;print(sys.version_info[0]*100+sys.version_info[1])"],
            capture_output=True, text=True, timeout=8,
            creationflags=CREATE_NO_WINDOW)
        if out.returncode != 0:
            return None
        v = int(out.stdout.strip())
        return v if 308 <= v <= 313 else None
    except Exception:  # noqa: BLE001
        return None


def _scan_dir_for_python(dirpath: str | None, out: dict[int, str]) -> None:
    """扫描某目录下的 python.exe / python3*.exe，探测 ABI 版本后收录（首次胜出）。

    覆盖绿色版/嵌入式 Python（不写注册表、不在 PATH），如 WorkBuddy managed
    运行时、conda 根目录、便携 Python 目录等。
    """
    if not dirpath or not os.path.isdir(dirpath):
        return
    try:
        names = os.listdir(dirpath)
    except OSError:
        return
    for fn in names:
        low = fn.lower()
        if low == "python.exe" or (low.startswith("python3") and low.endswith(".exe")):
            exe = os.path.join(dirpath, fn)
            v = _sys_py_version(exe)
            if v is not None:
                out.setdefault(v, exe)


def discover_system_runtimes() -> dict[int, str]:
    """枚举系统中已安装的 CPython，作为 bridge 子进程的候选（无 bundled 运行时时）。

    搜索范围（结果缓存到进程内，仅首次较慢）：
    - 常见安装目录 C:\\Python3xx\\python.exe、AppData/Local/Programs/Python/Python3xx 等；
    - Windows 注册表 Software\\Python\\PythonCore\\3.xx\\InstallPath；
    - `py -3.xx` 启动器；PATH 中的 `python3.xx`；
    - WorkBuddy managed 运行时 ~/.workbuddy/binaries/python/versions/*/python.exe；
    - conda 常见根目录（anaconda3 / miniconda3）；
    - 环境变量显式指定：QMT_PYTHON_DIRS（分号分隔目录）、QMT_PYTHON_<MINOR>（如 QMT_PYTHON_311=D:\\py\\python.exe）。

    仅返回 [3.8, 3.13] 区间版本（迅投官方未发布 cp313，3.13 仅作兜底）。
    """
    global _SYSTEM_RUNTIME_CACHE
    if _SYSTEM_RUNTIME_CACHE is not None:
        return _SYSTEM_RUNTIME_CACHE
    out: dict[int, str] = {}
    candidates: list[str] = []

    # 1) 常见安装目录（注意目录名：Python3.8->Python38，3.10->Python310，无前导零）
    for minor in range(308, 314):
        m = f"{minor // 100}{minor % 100}"  # 308->"38", 311->"311"
        candidates.append(f"C:\\Python{m}\\python.exe")
        candidates.append(os.path.expandvars(
            f"%LOCALAPPDATA%\\Programs\\Python\\Python{m}\\python.exe"))
        candidates.append(f"C:\\Program Files\\Python{m}\\python.exe")
        candidates.append(f"C:\\Program Files (x86)\\Python{m}\\python.exe")
        candidates.append(os.path.expanduser(
            f"~/AppData/Local/Programs/Python/Python{m}\\python.exe"))

    # 2) 注册表（Windows）
    try:
        import winreg
        for hkey in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
                try:
                    root = winreg.OpenKey(
                        hkey, r"Software\Python\PythonCore", 0,
                        winreg.KEY_READ | view)
                except OSError:
                    continue
                for i in range(512):
                    try:
                        keyname = winreg.EnumKey(root, i)
                    except OSError:
                        break
                    if not (keyname[:1].isdigit() and "." in keyname):
                        continue
                    try:
                        sub = winreg.OpenKey(root, keyname + r"\InstallPath")
                        path, _ = winreg.QueryValueEx(sub, None)
                    except OSError:
                        continue
                    exe = os.path.join(path, "python.exe")
                    if os.path.isfile(exe):
                        candidates.append(exe)
    except Exception:  # noqa: BLE001
        pass

    # 3) py 启动器 / PATH 中的 python3.xx
    for minor in range(308, 314):
        m = f"{minor // 100}{minor % 100}"
        ver = f"{minor // 100}.{minor % 100}"  # py -3.8 / python3.8
        try:
            r = subprocess.run(["py", f"-{ver}", "-c", "import sys;print(sys.executable)"],
                               capture_output=True, text=True, timeout=8,
                               creationflags=CREATE_NO_WINDOW)
            if r.returncode == 0:
                exe = r.stdout.strip()
                if exe and os.path.isfile(exe):
                    candidates.append(exe)
        except Exception:  # noqa: BLE001
            pass
        for probe in (f"python{m}", f"python{ver}"):
            p = shutil.which(probe)
            if p:
                candidates.append(p)

    # 4) PATH 目录扫描（绿色版 / 便携 Python / 虚拟环境，覆盖 3.13.12 managed 等）
    #    跳过 Windows 系统目录（含成千上万文件、几乎无 python.exe，listdir 极慢且无意义）；
    #    并限制目录数上限，防止极端长 PATH 下首次扫描卡死（结果有进程内缓存，仅首次较慢）。
    # 注意：不要用宽泛的 "appdata" 跳过——WPS 灵犀、便携 Python 等常装在
    # AppData 下（~\AppData\Roaming\...\python-env），跳过会漏掉当前可用运行时。
    # 仅精确跳过 Temp 与系统目录（这些目录几乎不可能有可用的 python.exe）。
    # low 为反斜杠路径（d.lower()），故 hint 用反斜杠形式。
    _SKIP_DIR_HINTS = ("system32", "syswow64", "servicing", "$recycle.bin",
                       "\\programdata\\", "appdata\\local\\temp")
    _path_dirs = [d.strip() for d in os.environ.get("PATH", "").split(os.pathsep) if d.strip()]
    for d in _path_dirs[:80]:
        low = d.lower()
        if any(h in low for h in _SKIP_DIR_HINTS):
            continue
        _scan_dir_for_python(d, out)

    # 5) WorkBuddy managed 运行时（本机常见：~/.workbuddy/binaries/python/versions/3.11.x/）
    for base in (
        os.path.expanduser("~/.workbuddy/binaries/python/versions"),
        os.path.expandvars("%USERPROFILE%\\.workbuddy\\binaries\\python\\versions"),
    ):
        _scan_dir_for_python(base, out)

    # 6) conda 常见根目录
    for base in (
        os.path.expanduser("~/anaconda3"), os.path.expanduser("~/miniconda3"),
        os.path.expanduser("~/AppData/Local/Continuum/anaconda3"),
        r"C:\ProgramData\Anaconda3", r"C:\ProgramData\miniconda3",
    ):
        _scan_dir_for_python(base, out)

    # 7) 环境变量显式指定
    #    QMT_PYTHON_DIRS=D:\py;E:\tools\python  （分号分隔目录）
    for d in os.environ.get("QMT_PYTHON_DIRS", "").split(os.pathsep):
        _scan_dir_for_python(d.strip() or None, out)
    #    QMT_PYTHON_311=D:\py311\python.exe （按 ABI 小版本指定解释器）
    for key, val in os.environ.items():
        if not key.startswith("QMT_PYTHON_") or key == "QMT_PYTHON_DIRS":
            continue
        try:
            minor = _normalize_abi(int(key[len("QMT_PYTHON_"):]))
        except (TypeError, ValueError):
            continue
        if val and os.path.isfile(val):
            v = _sys_py_version(val)
            if v is not None:
                out.setdefault(v, val)

    for exe in dict.fromkeys(candidates):  # 去重保序
        if exe and os.path.isfile(exe):
            v = _sys_py_version(exe)
            if v is not None:
                out.setdefault(v, exe)
    _SYSTEM_RUNTIME_CACHE = out
    return out


def _merge_runtimes(bundled: dict[int, str] | None) -> dict[int, str]:
    """合并 bundled（优先）+ 系统运行时，作为桥接候选。"""
    merged: dict[int, str] = {}
    if bundled:
        merged.update(bundled)
    try:
        for m, exe in discover_system_runtimes().items():
            merged.setdefault(m, exe)  # bundled 优先
    except Exception:  # noqa: BLE001
        pass
    return merged


def select_runtime(xtquant_site: str | None,
                   bundled: dict[int, str] | None = None,
                   prefer_bridge: bool = False) -> dict | None:
    """选择用于加载该券商 xtquant 的解释器。

    返回 {"python_exe", "abi", "mode": "in_process"|"bridge"} 或 None（无兼容）。

    规则：
    - 若主后端 ABI 在券商集合内且非强制桥接 -> in_process（进程内 import，最省）。
    - 否则从 bundled 里选「ABI ∈ 券商集合且最高」的运行时 -> bridge。
    - 都不行 -> None（调用方抛 ABINotSupportedError）。
    """
    broker_abis = detect_xtquant_abis(xtquant_site)
    if not broker_abis:
        return None
    host = host_python_minor()
    if (not prefer_bridge) and host in broker_abis:
        return {"python_exe": sys.executable, "abi": host, "mode": "in_process"}
    if bundled is None:
        bundled = discover_bundled_runtimes()
    merged = _merge_runtimes(bundled)
    candidates = [m for m in merged if m in broker_abis]
    if candidates:
        best = max(candidates)
        return {"python_exe": merged[best], "abi": best, "mode": "bridge"}
    return None


def require_runtime_or_raise(xtquant_site, bundled=None, prefer_bridge=False) -> dict:
    """select_runtime 的严格版：选不出时抛 ABINotSupportedError（带可操作提示）。"""
    sel = select_runtime(xtquant_site, bundled, prefer_bridge)
    if sel is None:
        broker_abis = detect_xtquant_abis(xtquant_site)
        bundled = bundled or discover_bundled_runtimes()
        system = {}
        try:
            system = discover_system_runtimes()
        except Exception:  # noqa: BLE001
            pass
        available = sorted(set(bundled) | set(system))
        host = host_python_minor()
        msg = (
            f"券商 xtquant 的 ABI 变体为 {broker_abis}（cp36~cp312），"
            f"而当前可用运行时为：主后端 Python {host}（cp{host}）+ "
            f"桥接运行时 {available or '无'}。"
            f"无兼容解释器：请任选其一——"
            f"(1) 在 backend/runtimes 放置匹配的嵌入式 Python"
            f"（如券商支持 cp{min(broker_abis)//100}.{min(broker_abis)%100}"
            f"~cp{max(broker_abis)//100}.{max(broker_abis)%100}，"
            f"放置 runtimes/cp{max(broker_abis)//100}.{max(broker_abis)%100}/python.exe）；"
            f"(2) 安装 Python {max(broker_abis)//100}.{max(broker_abis)%100}"
            f" 并将其 python.exe 加入 PATH（平台会自动复用）；"
            f"(3) 升级券商客户端 SDK 至含 cp{host} 的版本。")
        raise ABINotSupportedError(msg, broker_abis=broker_abis,
                                   available_runtimes=available)
    return sel


def xtp_runtime_plan(client_path: str, prefer_bridge: bool = False) -> dict | None:
    """为某券商 client_path 计算运行时方案（含 mode）。client_path 为空时返回 None。"""
    site = None
    try:
        from .xtp import _resolve_xtquant_path
        site = _resolve_xtquant_path(client_path)
    except Exception:  # noqa: BLE001
        site = None
    return select_runtime(site, prefer_bridge=prefer_bridge)

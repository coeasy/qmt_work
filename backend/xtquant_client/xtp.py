"""迅投 XTQuant 适配器（覆盖所有基于迅投 MiniQMT 的券商客户端）。

支持券商（同一套 xtquant SDK，仅 client_path / account_id / account_type / session_id 不同）：
- 国金证券 QMT、华鑫证券（奇点/华鑫 QMT）、银河证券、中信建投、兴业、广发等所有迅投系 MiniQMT。

xtquant 包自动发现（无需用户手动安装）：
- 若当前 Python 环境已装（pip install 过），直接使用；
- 否则按 client_path（userdata_mini 目录）推断客户端根目录，自动把
  <根>/bin.x64/Lib/site-packages 等目录注入 sys.path 加载客户端自带的 xtquant，
  并缓存结果（版本随客户端升级自动同步，不随程序分发专有包）。
"""
import logging
import os
import re
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime

from .base import BrokerAdapter, BrokerError, BrokerNotConnectedError, BrokerSDKError

log = logging.getLogger("qmt_work")

# 账户类型 -> xt_trader 账户类
_ACCOUNT_CLASS = {}

# 下单操作映射
_ORDER_OP = {}

# 下单价格类型映射
_PRICE_TYPE = {}

# 已解析的 xtquant site-packages 路径缓存（规范化 client_path -> path 或 None）
_XTQUANT_CACHE: dict[str, str | None] = {}

# 客户端内 xtquant 常见相对位置（相对客户端根目录；按出现频率排序）。
# 阶段 5 扩展：增加恒生 UF 定制版与各券商白标常见的 SDK 嵌入位——
# 部分券商把 xtquant 嵌在 client/inner/python 或 app/lib 等非 bin.x64 路径。
_XTQUANT_REL = [
    os.path.join("bin.x64", "Lib", "site-packages"),
    os.path.join("bin.x64", "Python", "Lib", "site-packages"),
    os.path.join("bin.x64", "python", "Lib", "site-packages"),
    os.path.join("bin.x64", "python311", "Lib", "site-packages"),
    os.path.join("bin.x64", "python312", "Lib", "site-packages"),
    os.path.join("bin.x64", "python310", "Lib", "site-packages"),
    # 恒生 UF 定制版：xtquant 经常被复制到 client/python 或 app/python 下
    os.path.join("client", "python", "Lib", "site-packages"),
    os.path.join("client", "python311", "Lib", "site-packages"),
    os.path.join("client", "python312", "Lib", "site-packages"),
    os.path.join("app", "python", "Lib", "site-packages"),
    os.path.join("app", "lib", "site-packages"),
    os.path.join("uf", "python", "Lib", "site-packages"),
    os.path.join("uf", "Lib", "site-packages"),
    os.path.join("inner", "Lib", "site-packages"),
    os.path.join("Lib", "site-packages"),
    os.path.join("python", "Lib", "site-packages"),
    os.path.join("Python", "Lib", "site-packages"),
    ".",  # 极少数结构：根目录本身就是 site-packages
]


def _normalize(p: str) -> str:
    return os.path.normcase(os.path.abspath(os.path.expanduser(p)))


def _candidate_roots(client_path: str) -> list[str]:
    """自底向上收集可能的客户端根目录（含 bin.x64 / userdata_mini 的排前）。

    不假设 client_path 恰好是 <根>/userdata_mini：用户可能填安装根、bin.x64、
    或更深层级，统一从该目录逐级向上收集祖先作为根候选。
    """
    p = _normalize(client_path)
    cands: list[str] = []
    seen: set[str] = set()
    cur = p
    for _ in range(7):
        if cur and cur not in seen:
            seen.add(cur)
            cands.append(cur)
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent

    def _score(d: str) -> int:
        s = 0
        if os.path.isdir(os.path.join(d, "bin.x64")):
            s += 10
        if os.path.isdir(os.path.join(d, "userdata_mini")) or os.path.isdir(os.path.join(d, "userdata")):
            s += 5
        if os.path.isdir(os.path.join(d, "bin.x64", "Lib", "site-packages", "xtquant")):
            s += 20
        # 阶段 5：恒生 UF 定制版常见嵌入位（client/uf/inner/app 下的 python site-packages）
        for sub in ("client", "uf", "inner", "app"):
            if os.path.isdir(os.path.join(d, sub, "python", "Lib", "site-packages", "xtquant")):
                s += 15
            if os.path.isdir(os.path.join(d, sub, "Lib", "site-packages", "xtquant")):
                s += 12
        return s
    return sorted(cands, key=lambda d: -_score(d))


def _is_likely_root(d: str) -> bool:
    """目录是否像客户端根（含 bin.x64 / userdata_mini / userdata / 恒生UF子目录）。"""
    if (os.path.isdir(os.path.join(d, "bin.x64"))
            or os.path.isdir(os.path.join(d, "userdata_mini"))
            or os.path.isdir(os.path.join(d, "userdata"))):
        return True
    # 阶段 5：恒生 UF 定制版典型结构（client/uf/inner 子目录 + userdata 标记）
    for sub in ("client", "uf", "inner", "app"):
        if (os.path.isdir(os.path.join(d, sub))
                and (os.path.isdir(os.path.join(d, sub, "python"))
                     or os.path.isdir(os.path.join(d, sub, "Lib"))
                     or os.path.isdir(os.path.join(d, sub, "userdata_mini")))):
            return True
    return False


# 向上 walk 时禁止进入的系统目录（父级命中则只 walk 自身，防误命中无关 xtquant）
_SYSTEM_DIR_NAMES = {
    "appdata", "programdata", "temp", "tmp", "windows", "users",
    "program files", "program files (x86)", "recovery", "$recycle.bin",
    "system32", "syswow64",
}


def _is_system_dir(d: str) -> bool:
    """路径是否为盘符根或系统目录（不应作为递归搜索的父级）。"""
    parent = os.path.dirname(d)
    if parent == d:  # 盘符根 C:\ 等
        return True
    return os.path.basename(d).strip().lower() in _SYSTEM_DIR_NAMES


def _resolve_xtquant_path(client_path: str) -> str | None:
    """按客户端目录推断 xtquant 的 site-packages 路径；找不到返回 None。

    逻辑：从 client_path 自底向上收集候选根，在候选根下按常见相对位置匹配，
    兜底仅在「疑似客户端根」或 client_path 本身做有限深度（≤4 层）递归搜索，
    避免顺着祖先目录爬进 AppData 等无关区域误命中（如 IDE 生成的 xtquant stub）。
    结果按规范化路径缓存。
    """
    if not client_path:
        return None
    key = _normalize(client_path)
    if key in _XTQUANT_CACHE:
        return _XTQUANT_CACHE[key]

    roots = _candidate_roots(client_path)
    found: str | None = None
    # 1) 常见相对位置（候选根按含 bin.x64/userdata_mini/xtquant 的排前；只查已知子路径，安全）
    for root in roots:
        for rel in _XTQUANT_REL:
            sp = os.path.join(root, rel)
            if os.path.isfile(os.path.join(sp, "xtquant", "__init__.py")):
                found = sp
                break
        if found:
            break
    # 2) 兜底递归：仅对疑似客户端根做 ≤6 层搜索；
    #    若填的是存在但非标准的目录（无 bin.x64/userdata_mini 标记），
    #    再额外 walk 该目录及其最近 1 个父级（父级通常就是客户端根）。
    #    防护：client_path 不存在时绝不向上爬；父级为系统目录/盘符根时只 walk 自身，
    #    避免误入 AppData / Program Files 等命中无关 xtquant（如 IDE stub）。
    if not found:
        likely = [r for r in roots if _is_likely_root(r)]
        if likely:
            walk_roots = likely
        elif os.path.isdir(client_path):
            walk_roots = roots[:2]
            if len(walk_roots) > 1 and _is_system_dir(walk_roots[1]):
                walk_roots = walk_roots[:1]
        else:
            walk_roots = []
        for root in walk_roots:
            try:
                for dirpath, dirnames, _ in os.walk(root):
                    depth = dirpath[len(root):].count(os.sep)
                    if depth > 6:
                        dirnames[:] = []
                        continue
                    if "xtquant" in dirnames and os.path.isfile(
                            os.path.join(dirpath, "xtquant", "__init__.py")):
                        found = dirpath
                        break
            except OSError:
                continue
            if found:
                break
    _XTQUANT_CACHE[key] = found
    return found


def _load_xtquant_from(site_packages: str) -> None:
    """把客户端自带 xtquant 注入 sys.path / PATH，并验证可导入（失败抛原异常）。"""
    # 关键顺序：在注入客户端 site-packages 之前，先从「当前运行时」加载并缓存
    # 科学栈依赖（numpy/pytz/dateutil/pandas...）。
    # 客户端 site-packages 常捆绑按客户端内嵌 Python 编译的旧版本——numpy 1.19.1
    # 与桥接运行时 ABI 不兼容，pytz ~2020 用了 Python3.10 已删的 collections.Mapping，
    # pandas._libs 编译扩展在新解释器上根本加载不了；若让它们抢占 sys.path[0]，
    # xtdata.get_market_data_ex 内部 `import pandas` -> get_kline/get_market_data 直接崩。
    # 提前 import 使其进入 sys.modules 缓存，xtdata 后续 `import X` 复用运行时版本，
    # 客户端捆绑的坏版本被完全屏蔽（桥接跨进程 JSON 序列化，不会把 DataFrame 传回主端，
    # 因此屏蔽是安全且更优的）。运行时未提供某包时静默跳过，回退客户端自带。
    for _mod in ("numpy", "pytz", "dateutil", "pandas"):
        try:
            __import__(_mod)  # noqa: F401
        except ImportError:  # noqa: BLE001  运行时缺失时静默，回退客户端自带
            pass
    if site_packages and site_packages not in sys.path:
        sys.path.insert(0, site_packages)
    # 客户端 bin 目录加入 PATH（xtquant 依赖其下 dll）
    bin_dir = os.path.dirname(os.path.dirname(site_packages))
    if os.path.isdir(bin_dir):
        os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")
    import xtquant.xtdata  # noqa: F401
    _load_trader_api()  # 新旧 xt_trader/xttrader 兼容导入


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
        from .runtime import detect_xtquant_abis, host_python_minor
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
        from .runtime import discover_system_runtimes, host_python_minor, xtp_runtime_plan
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


def _ensure_xtconstant():
    import xtquant.xtconstant as xtc  # noqa: F401
    return xtc


# 新旧 xtquant 交易 API 兼容：新包模块名 xt_trader，旧包 xttrader + 账户类在 xttype
_TRADER_API_CACHE: dict = {}


def _load_trader_api() -> tuple:
    """返回 (XtQuantTrader, {账户类型: 账户类})，兼容新旧 xtquant 客户端。

    旧版客户端（xttrader）账户类可能只提供 STOCK（无信用/期权/期货类），
    因此按类名逐个容错导入；缺失类型在 start() 时给出明确错误。
    """
    if _TRADER_API_CACHE:
        return _TRADER_API_CACHE["class"], _TRADER_API_CACHE["accounts"]
    try:
        import xtquant.xt_trader as _acc_mod
        from xtquant.xt_trader import XtQuantTrader  # 新版
    except ImportError:
        import xtquant.xttype as _acc_mod
        from xtquant.xttrader import XtQuantTrader  # 旧版
    accounts: dict = {}
    for _name, _key in (("StockAccount", "STOCK"), ("CreditAccount", "CREDIT"),
                        ("OptionAccount", "OPTION"), ("FutureAccount", "FUTURES")):
        cls = getattr(_acc_mod, _name, None)
        if cls is not None:
            accounts[_key] = cls
    if "STOCK" not in accounts:
        raise ImportError("xtquant 缺少 StockAccount（客户端 SDK 不完整）")
    _TRADER_API_CACHE["class"] = XtQuantTrader
    _TRADER_API_CACHE["accounts"] = accounts
    return XtQuantTrader, accounts


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
                    probe = _probe_quote_service(client_path)
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
        probe = _probe_quote_service(client_path)
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
        probe = _probe_quote_service(client_path)
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
            probe = _probe_quote_service(client_path) if client_path else {}
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


# ---------------- 多版本 SDK 兼容辅助 ----------------
# 不同 xtquant 版本对象属性命名不一致：旧版 xttrader 全小写（order_id/stock_code/
# order_status/traded_volume…），新版部分用 CamelCase（OrderID/StockCode/OrderStatus/
# DealVolume…）。统一用「候选名逐个取第一个非 None」兼容两套命名，避免某一版本下
# 取到全 0 / None 的失真数据（曾导致旧版客户端订单 volume/dealt/status 全错）。
def _pick(obj, *names, default=None):
    """从 obj 按候选属性名取第一个非 None 的值；全部缺失返回 default。

    用 ``is not None`` 判定（非 falsy），使 ``0``/``""``/``[]`` 这类合法值不被误当缺失。
    """
    for n in names:
        try:
            v = getattr(obj, n, None)
        except Exception:  # noqa: BLE001
            continue
        if v is not None:
            return v
    return default


def _dget(d, *keys, default=None):
    """从 dict 按候选键名取第一个非 None 的值；全部缺失返回 default。

    用于回调载荷（dict）的键名兼容：旧 xttrader 用小写（seq/order_id/order_status），
    新版部分用 CamelCase（Seq/OrderID/OrderStatus）。与 :func:`_pick` 区分——后者读
    对象属性（SDK 实体对象），本函数读本就是 dict 的回调载荷。
    """
    if not isinstance(d, dict):
        return default
    for k in keys:
        v = d.get(k, None)
        if v is not None:
            return v
    return default


def _normalize_kline_period(period: str) -> str:
    """迅投协议周期归一化：平台展示别名 -> xtquant 实际周期名。

    迅投各版本 xtdata 的 K 线周期统一为 1m/5m/15m/30m/1h/1d（+服务端 1w/1mon/tick），
    没有 "60m"——那是平台展示别名。传入 "60m" 时映射为 "1h"，其余原样返回。
    """
    p = (period or "1d").strip().lower()
    return "1h" if p == "60m" else p


# 下单方向操作码（xtconstant）：买类含 担保品买入(23)/买券还券(29)/专项买券还券(42)；
# 卖类含 担保品卖出(24)/卖券还款(31)/专项卖券还款(44)。部分旧版无 33 之类码。
_BUY_ORDER_TYPES = frozenset({23, 29, 42})
_SELL_ORDER_TYPES = frozenset({24, 31, 44})


def _direction_from_order_type(ot: object) -> str:
    """由订单操作类型码推断买卖方向；未知/无法判定返回空串（不臆测）。"""
    try:
        iv = int(ot)
    except (TypeError, ValueError):
        return ""
    if iv in _BUY_ORDER_TYPES:
        return "buy"
    if iv in _SELL_ORDER_TYPES:
        return "sell"
    return ""


class XTPQuantAdapter(BrokerAdapter):
    """迅投 XTQuant 真实适配器。"""

    def __init__(self, client_path: str, account_id: str, account_type: str = "STOCK",
                 session_id: int = 0, min_version: str = "", client_mode: str = "auto"):
        self.client_path = client_path
        # 客户端连接模式：auto（自动推断）/ mini（极速版 MiniQMT，userdata_mini）/
        # full（完整版大客户端，userdata）。决定交易侧 XtQuantTrader 使用的数据目录。
        self._client_mode = (client_mode or "auto").lower()
        self._account_id = account_id
        self._account_type = (account_type or "STOCK").upper()
        self.session_id = int(session_id or 0)
        self.min_version = min_version
        self._xtdata = None
        self._trader = None
        self._acc = None
        self._connected = False
        self._lock = threading.Lock()  # 交易调用串行锁（SDK 侧互斥）
        # 回调锁：专门保护 seq→oid / oid→seq / pending 三张映射（SDK 回调线程与
        # 业务线程并发访问）。与 _lock 分离，避免「锁内调 SDK 方法 + SDK 同步派发
        # 回调再取同一把锁」造成死锁。
        self._map_lock = threading.Lock()
        self._order_status_cache: dict[str, str] = {}
        self._name_cache: dict[str, str] = {}
        # 阶段 0-A：报单回调闭环 —— 本地 seq → 柜台真实 order_id 双向映射，
        # 以及 place_order 等待 response 回调的 pending 表。
        self._seq_to_oid: dict[int, str] = {}
        self._oid_to_seq: dict[str, int] = {}
        self._pending_resp: dict[int, tuple[threading.Event, list]] = {}
        # 外部钩子（由 sync 引擎 / manager 注入，用于把回报直推行情/对账管线）
        self._on_order_cb = None
        self._on_trade_cb = None
        self._on_disconnect_cb = None
        self._probe_cache = None  # 版本画像复用：连接期探测的运行场景结果缓存

    # ---------------- 身份 ----------------
    @property
    def broker_name(self) -> str:
        return "迅投XTQuant"

    @property
    def adapter_id(self) -> str:
        return "xtp"

    @property
    def client_version(self) -> str:
        ver = self.min_version or "xtquant"
        if self._xtdata is not None:
            try:
                return getattr(self._xtdata, "__version__", ver) or ver
            except Exception:  # noqa: BLE001
                return ver
        return ver

    @property
    def account_id(self) -> str:
        return self._account_id

    @property
    def account_type(self) -> str:
        return self._account_type

    @property
    def supported_periods(self) -> list[str]:
        # 展示层用平台命名（"60m"）；真实调用经 get_kline/subscribe_quote 内的
        # _normalize_kline_period 归一化为迅投协议周期（"60m"->"1h"）。
        return ["1m", "5m", "15m", "30m", "60m", "1d", "1w", "1mon"]

    @property
    def supported_account_types(self) -> list[str]:
        return ["STOCK", "CREDIT", "OPTION", "FUTURES"]

    @property
    def sdk_required(self) -> str:
        return "xtquant"

    def capabilities(self) -> list[str]:
        """运行时能力探测：按当前账号 / 目录 / 连接状态推导能力矩阵。

        供 Registry.probe 优先采用（优于基于券商档案的静态推导），前端据此
        展示「当前 QMT 版本实际支持哪些功能」。连接失败/未连接时不臆造能力。
        """
        try:
            return self.version_profile().capabilities.as_list()
        except Exception:  # noqa: BLE001
            return []

    def version_profile(self) -> QmtVersionProfile:
        """返回当前客户端安装的版本画像（类型 + 版本 + 能力矩阵）。

        复用连接期已解析的交易目录 / 模式；运行进程探测优先复用适配器内缓存，
        未缓存时现测（诊断路径，无副作用）。
        """
        probe = getattr(self, "_probe_cache", None)
        acc_filled = bool(self._account_id)
        return build_version_profile(
            self.client_path, self._client_mode,
            account_id=self._account_id, account_type=self._account_type,
            realtime_push=self._connected and acc_filled,
            probe=probe)

    # ---------------- 生命周期 ----------------
    def start(self) -> None:
        # 幂等：已连接则复用，避免重复 start 泄漏第二个交易会话（同账号多会话冲突）
        if self._connected:
            return
        # 自动发现并加载客户端自带的 xtquant（无需用户手动 pip install）
        xtq_sp = _resolve_xtquant_path(self.client_path)
        if xtq_sp:
            try:
                _load_xtquant_from(xtq_sp)
                log.info("xtquant auto-loaded from client dir: %s", xtq_sp)
            except Exception as exc:  # noqa: BLE001
                raise BrokerSDKError(
                    "xtquant",
                    f"客户端目录已发现 xtquant（{xtq_sp}）但导入失败：{exc}") from exc
        try:
            import xtquant.xtdata as xtdata  # noqa: F401
            XtQuantTrader, _acc_classes = _load_trader_api()
        except Exception as exc:  # noqa: BLE001
            if xtq_sp:
                raise BrokerSDKError(
                    "xtquant",
                    f"客户端目录已发现 xtquant（{xtq_sp}）但导入失败：{exc}") from exc
            raise BrokerSDKError(
                "xtquant",
                "未找到 xtquant：已自动在客户端目录（bin.x64\\Lib\\site-packages）搜索失败，"
                "请确认「券商连接」填写的 client_path 指向客户端数据目录"
                "（极速版 userdata_mini / 完整版 userdata）且客户端已安装登录，"
                "或手动 pip install xtquant") from exc

        self._xtdata = xtdata
        xtdata_ok, xtdata_detail = _probe_xtdata(xtdata, self.client_path)

        if not self._account_id:
            # 仅行情模式（无交易账号）：行情可用，交易不可用。
            # 在 start() 阶段就给出明确指引，而不是等到 get_quote 才抛 SDK 原话
            #「无法连接行情服务！」——那种报错会让用户误以为是平台 bug。
            # 阶段 5：恒生UF 等定制版的诊断细化——区分「客户端未登录」「客户端未运行」
            # 「客户端路径错误」等场景，给出可操作指引。
            if not xtdata_ok:
                # 根据 xtdata_detail 关键字推断场景（无需修改 SDK）
                low = (xtdata_detail or "").lower()
                if "未登录" in xtdata_detail or "login" in low or "auth" in low:
                    scene = "客户端未登录（行情/交易服务尚未登录）"
                    steps = (
                        "1) 在 QMT 客户端中完成<b>行情 + 交易</b>登录（极速/普通模式均可）；\n"
                        "2) 确认客户端界面能正常显示行情（否则 SDK 无法连接）；\n"
                        "3) 保持客户端运行，再重试连接。")
                elif "未启动" in xtdata_detail or "not running" in low or "not started" in low:
                    scene = "客户端未启动"
                    steps = (
                        "1) 启动 QMT 客户端（恒生UF：HsUFTrader / UFClient）；\n"
                        "2) 完成登录后保持客户端运行；\n"
                        "3) 再次点击连接。")
                elif "端口" in xtdata_detail or "port" in low or "rpc" in low:
                    scene = "客户端 RPC 端口未就绪"
                    steps = (
                        "1) 重启 QMT 客户端；\n"
                        "2) 确认客户端登录后无防火墙拦截；\n"
                        "3) 再次点击连接。")
                else:
                    # 深度诊断：主动探测进程 / 端口 / 配置一致性，替代笼统的「不可用」
                    probe = _probe_quote_service(self.client_path)
                    self._probe_cache = probe  # 供版本画像复用，避免重复探测
                    _probe_diag = (f"[探测] client_type={probe.get('client_type')} "
                                   f"full={probe.get('full_client_running')} "
                                   f"mini={probe.get('mini_client_running')} "
                                   f"port_map={probe.get('port_map')} "
                                   f"quote_ports={probe.get('quote_ports')} "
                                   f"trade_ports={probe.get('trade_ports')} "
                                   f"cfg_root={probe['cfg_root']} "
                                   f"cfg_root_exists={probe['cfg_root_exists']} "
                                   f"cfg_matches={probe['cfg_matches']}")
                    log.info("%s", _probe_diag)
                    if not probe["client_running"]:
                        scene = "QMT 客户端未运行"
                        steps = (
                            "1) 打开并登录 QMT 客户端（大窗口或小窗口均可），保持客户端运行；\n"
                            "2) 确认客户端界面能正常显示行情；\n"
                            "3) 再重试连接。")
                    elif (probe.get("full_client_running")
                          and not probe.get("quote_service_ok")
                          and not probe.get("mini_client_running")):
                        scene = ("大窗口客户端（XtItClient）已登录，"
                                 "但小窗口行情服务（miniquote）未运行")
                        steps = (
                            "1) xtdata 行情接口由<b>小窗口</b>（miniQmt / 极简模式）提供，"
                            "大窗口的 58600 端口仅支持交易，不支持行情 RPC；\n"
                            "2) 方案A：在大窗口客户端中开启「独立行情 / 极简模式」"
                            "（会自动拉起 miniquote 行情服务）；\n"
                            "3) 方案B：直接运行客户端目录 bin.x64\\XtMiniQmt.exe 并完成登录"
                            "（部分券商首次需输入验证码）；\n"
                            "4) 小窗口就绪后（端口 58610 监听），无需重启平台，直接重试连接。")
                    elif (probe.get("full_client_running")
                          and probe.get("mini_client_running")
                          and not probe.get("quote_service_ok")):
                        scene = "大小窗口均在运行，但行情端口未监听（miniquote 未就绪）"
                        steps = (
                            "1) 小窗口可能停留在登录界面（含验证码）尚未完成登录；\n"
                            "2) 请在弹出的 XtMiniQmt 登录窗口输入账号/密码/验证码；\n"
                            "3) 登录成功后 miniquote 将监听 58610，再重试连接。")
                    elif not probe["listening_ports"]:
                        scene = f"客户端在运行，但行情端口 {probe['expected_port']} 未监听"
                        steps = (
                            "1) 客户端已在运行，但行情服务端口未就绪；\n"
                            "2) 请确认 QMT 已完成<b>行情登录</b>（部分券商需单独登录行情）；\n"
                            "3) 确认客户端行情图正常刷新后，重启客户端再重试连接。")
                    elif probe["cfg_matches"] is False:
                        if probe.get("cfg_root_exists") is False:
                            scene = "行情配置为旧残留，指向已不存在的客户端目录"
                            steps = (
                                f"1) 检测到残留配置 {os.path.join(os.environ.get('USERPROFILE', ''), '.xtquant', '*', 'xtdata.cfg')} 指向不存在的目录：{probe['cfg_root']}；\n"
                                "2) 这是旧 QMT 客户端（如机构版 jigou_qmt）卸载后遗留的配置；\n"
                                "3) 请删除 %USERPROFILE%\\.xtquant\\ 下对应 guid 目录中的 xtdata.cfg，\n"
                                "   或重启当前 QMT 客户端（gd_qmt）使其重新生成正确配置，再重试连接。")
                        else:
                            scene = "行情配置指向了其他 QMT 客户端"
                            steps = (
                                f"1) 检测到行情配置指向：{probe['cfg_root']}，与当前客户端路径不一致；\n"
                                "2) 请确认本机只运行与「客户端路径」匹配的那一个 QMT；\n"
                                "3) 若本机装有多个 QMT（如广发/机构版），请运行路径对应的那个并重新登录。")
                    else:
                        scene = "行情服务不可用"
                        steps = (
                            "1) 打开并登录 QMT 客户端（极速/普通模式均可），保持客户端运行；\n"
                            "2) 确认客户端能正常显示行情；\n"
                            "3) 确认「客户端路径」与「客户端模式」匹配（极速版 userdata_mini / 完整版 userdata）。")
                raise BrokerNotConnectedError(
                    f"行情服务连接失败（{scene}，SDK 返回：{xtdata_detail}）。\n"
                    f"请按顺序排查：\n{steps}\n{_probe_diag}")
            self._connected = True
            return

        # 交易数据目录：按客户端模式解析（极速版 userdata_mini / 完整版 userdata）。
        # 行情侧 xtdata 走固定 58610，与此无关；但 XtQuantTrader 必须指向正确的数据目录，
        # 否则「完整版大客户端配了 userdata_mini」会连不上交易（反之亦然）。
        trade_dir, resolved_mode = _effective_trade_dir(self.client_path, self._client_mode)
        if resolved_mode in ("mini", "full"):
            log.info("XtQuantTrader 交易目录按客户端模式解析：%s -> %s（模式 %s）",
                     self.client_path, trade_dir, resolved_mode)

        try:
            # session 占用规避：连接失败时递增 session_id 重试（0..5），
            # 规避用户已手动打开 QMT 客户端占用默认 session 的场景。
            #
            # 关键兼容性（历史 bug）：xtquant 的 XtQuantTrader.start() 在多数版本里
            # **没有返回值**（源码 `self.async_client.start(); return`），因此 rc 为
            # None。旧实现用 `if rc == 0` 判断启动成功，None == 0 恒为 False，导致
            # 交易模式在这些 SDK 版本下 100% 连不上（无论客户端是否已登录），且报错
            # 指向「session 被占用」误导排查方向。
            # 正确语义：start() 返回 None 视为已启动；真正的连接结果由 connect()
            # 决定（返回 0 成功，非 0 失败——session 冲突在这里暴露）。
            # 候选交易目录：首选按客户端模式解析的目录；失败时自动降级尝试另一模式
            # 目录（完整版 userdata <-> 极速版 userdata_mini 互备）。很多券商同一路径
            # 下既有完整版又有极速版（userdata + userdata_mini 并存），当前解析模式
            # 连不上并不代表另一模式也连不上——例如完整版大客户端未以「极简模式」登录、
            # 而极速版 miniQMT 已登录的情况。故按序尝试，命中即成功。
            candidate_dirs = [trade_dir]
            _alt_dir, _alt_mode = _effective_trade_dir(
                self.client_path, "mini" if resolved_mode != "mini" else "full")
            if _alt_dir and _alt_dir != trade_dir and os.path.isdir(_alt_dir):
                candidate_dirs.append(_alt_dir)
            trader = None
            last_err = ""
            used_dir = trade_dir
            resolved_mode_final = resolved_mode
            for d in candidate_dirs:
                if trader is not None:
                    break
                used_dir = d
                resolved_mode_final = ("mini" if d.endswith("userdata_mini") else "full")
                for attempt in range(6):
                    sid = self.session_id + attempt
                    t = XtQuantTrader(d, sid)
                    rc = t.start()
                    if rc is not None and rc != 0:
                        last_err = f"start rc={rc}"
                        continue
                    crc = t.connect()
                    if crc == 0:
                        trader = t
                        self.session_id = sid
                        break
                    last_err = f"connect rc={crc}"
                    # 释放本次失败的会话，避免连续重试泄漏多个 session
                    try:
                        t.stop()
                    except Exception:  # noqa: BLE001
                        pass
            trade_dir = used_dir
            if trader is None:
                _exe_procs = _running_client_exes()
                _login_log = _latest_login_log(trade_dir)
                # 多因子真实归因（替代旧版「一律归因程序化权限请联系券商」的误导文案）：
                # rc=-1 常见根因按官方排查顺序为 ①登录模式 ②路径 ③session ④权限。
                # 这里把已实测到的事实（运行进程 / 尝试的模式 + 目录互备 / client_mode）
                # 一并列出，让定位不再猜。
                tried = " → ".join(os.path.basename(x) for x in candidate_dirs)
                raise BrokerNotConnectedError(
                    f"交易连接失败（session_id {self.session_id}~{self.session_id + 5} "
                    f"均 {last_err}）。\n"
                    f"### 已实测的排查事实\n"
                    f"  1) 客户端进程：{_exe_procs or '未检测到'}；\n"
                    f"  2) 尝试的数据目录（按顺序）：{tried or (trade_dir or '（无）')}；\n"
                    f"  3) 配置客户端模式：client_mode={self._client_mode}"
                    f"，解析判定 @{resolved_mode_final}。\n"
                    f"### 官方四步排查（迅投 FAQ）\n"
                    f"  ① [极简模式] QMT 登录时是否勾选「极简模式」——完整版大客户端未以"
                    f"极简模式登录时，外部 API 交易连接（XtQuantTrader）会返回 rc=-1；\n"
                    f"  ② [路径/模式匹配] 极速版必须指向 <安装目录>\\userdata_mini，"
                    f"完整版指向 \\userdata；C 盘安装需以管理员权限运行连接端；\n"
                    f"  ③ [session] 换另一个 session_id 再试（同一 session 两次 connect "
                    f"间隔需 >3 秒）；\n"
                    f"  ④ [权限] 若以上均正确仍 rc=-1，才是【资金账号未开通 QMT "
                    f"「程序化交易/策略交易权限」】（仅「基础交易权限」不够），联系券商核实。\n"
                    f"建议优先：运行并登录极速版 bin.x64\\XtMiniQmt.exe（登录时勾选极简"
                    f"模式），或以极简模式重新登录完整版客户端后再连接。"
                    f"（客户端登录日志 {_login_log}）")
            resolved_mode = resolved_mode_final
            if self._account_type not in _acc_classes:
                raise BrokerNotConnectedError(
                    f"该客户端不支持账户类型 {self._account_type}（支持：{sorted(_acc_classes)}）")
            cls = _acc_classes[self._account_type]
            self._acc = cls(self._account_id, self._account_type)
            trader.subscribe(self._acc)
            # 阶段 0-A（C1）：注册回调，否则断线零感知、拿不到柜台 order_id 与成交回报。
            # XtQuantTraderCallback 随 xtquant 版本命名不同，兼容新旧两处导入。
            try:
                from xtquant.xt_trader import XtQuantTraderCallback
            except ImportError:
                from xtquant.xttrader import XtQuantTraderCallback
            try:
                trader.register_callback(self._make_callback(XtQuantTraderCallback))
            except Exception as exc:  # noqa: BLE001
                # 回调注册失败不应阻断连接；但记日志以便排查（影响断线感知/回报）
                log.warning("register_callback 失败（断线感知/成交回报将不可用）：%s", exc)
            self._trader = trader
            self._connected = True
        except BrokerNotConnectedError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise BrokerNotConnectedError(f"交易连接异常：{exc}") from exc

    # ---------------- 阶段 0-A：报单回调闭环 ----------------
    def on_order(self, cb) -> None:
        """注册报单响应回调（order_id 解析 / 状态更新 → 推 sync 引擎）。"""
        self._on_order_cb = cb

    def on_trade(self, cb) -> None:
        """注册成交回报回调（替代纯轮询，直推 sync 引擎）。"""
        self._on_trade_cb = cb

    def on_disconnect(self, cb) -> None:
        """注册断线回调（触发 manager 健康重连）。"""
        self._on_disconnect_cb = cb

    def _make_callback(self, base):
        """动态构造 XtQuantTraderCallback 子类，把 SDK 对象转 dict 后交 adapter 方法处理。

        动态子类（而非静态子类）是因为 XtQuantTraderCallback 仅在 xtquant 导入后存在；
        adapter 方法接收**纯 dict**，便于在无 SDK 环境下单测。

        多版本兼容：新旧 SDK 的回调方法名与签名都不一致，这里**同时覆盖两套**：
        - 新 SDK：``on_order_stock_response(order)`` + ``on_cancel_error(order_id, error_id, error_msg)``
        - 旧 SDK (xttrader)：``on_order_stock_async_response(response)`` +
          ``on_cancel_error(cancel_error)``（1 个对象参数）+ ``on_order_error(order_error)``
        只要基类定义了对应方法，覆盖后即生效；基类未定义的方法名覆盖无副作用。
        """
        adapter = self

        def _order_as_dict(order) -> dict:
            # 兼容新旧 SDK：旧 xttrader 用全小写（seq/order_id/order_status/error_msg），
            # 新版部分用 CamelCase（Seq/OrderID/OrderStatus/ErrorMsg）。
            return {
                "seq": _pick(order, "Seq", "seq"),
                "order_id": _pick(order, "OrderID", "order_id"),
                "order_status": _pick(order, "OrderStatus", "order_status"),
                "error_id": _pick(order, "ErrorID", "error_id"),
                "error_msg": _pick(order, "ErrorMsg", "error_msg"),
                "stock_code": _pick(order, "StockCode", "stock_code"),
                "order_type": _pick(order, "OrderType", "order_type"),
                "price": _pick(order, "Price", "price"),
                "volume": _pick(order, "Volume", "order_volume", "volume", default=0),
                "traded_volume": _pick(order, "TradedVolume", "traded_volume", default=0),
            }

        def _trade_as_dict(trade) -> dict:
            # 真实成交（XtTrade）用 traded_price/traded_time；旧 SDK 退化路径（见
            # _query_stock_deals）以「已成交订单」近似，订单对象用 price/order_time，
            # 故两者都列入候选（真实成交的 traded_price 优先命中，不会被 price 覆盖）。
            return {
                "order_id": _pick(trade, "OrderID", "order_id"),
                "stock_code": _pick(trade, "StockCode", "stock_code"),
                "trade_type": _pick(trade, "TradeType", "trade_type"),
                "price": _pick(trade, "traded_price", "deal_price", "DealPrice", "price"),
                "volume": _pick(trade, "Volume", "traded_volume", "deal_volume", default=0),
                "trade_time": _pick(trade, "TradeTime", "traded_time", "deal_time", "order_time"),
                "trade_id": _pick(trade, "TradeID", "traded_id", "deal_id"),
            }

        class _Cb(base):
            def on_disconnected(self):
                adapter._handle_disconnected()

            # 新 SDK: on_order_stock_response(order)
            def on_order_stock_response(self, order):
                adapter._handle_order_response(_order_as_dict(order))

            # 旧 SDK (xttrader): on_order_stock_async_response(response)
            def on_order_stock_async_response(self, response):
                adapter._handle_order_response(_order_as_dict(response))

            # 旧 SDK 报单失败回调（XtOrderError 对象）
            def on_order_error(self, order_error):
                adapter._handle_order_error(order_error)

            # 兼容两种签名：旧 SDK on_cancel_error(cancel_error: 对象) /
            # 新 SDK on_cancel_error(order_id, error_id, error_msg)
            def on_cancel_error(self, *args):
                adapter._handle_cancel_error(*args)

            def on_stock_trade(self, trade):
                adapter._handle_stock_trade(_trade_as_dict(trade))

        return _Cb()

    def _handle_disconnected(self) -> None:
        """断线：翻转连接态并通知 manager 触发健康重连。"""
        was = self._connected
        self._connected = False
        with self._map_lock:
            self._seq_to_oid.clear()
            self._oid_to_seq.clear()
            self._pending_resp.clear()
        log.warning("XtQuantTrader 断线（adapter=%s account=%s）",
                    self._account_id, self._account_type)
        if was and self._on_disconnect_cb is not None:
            try:
                self._on_disconnect_cb()
            except Exception:  # noqa: BLE001
                pass

    def _handle_order_response(self, o: dict) -> None:
        """报单响应：建立 seq→柜台 order_id 映射，唤醒等待中的 place_order，并外推。

        键名兼容新旧两套：旧 xttrader 用全小写（seq/order_id/order_status/error_msg），
        新版部分用 CamelCase（Seq/OrderID/OrderStatus/ErrorMsg）。
        """
        try:
            seq = int(_dget(o, "seq", "Seq") or -1)
        except (ValueError, TypeError):
            seq = -1
        oid = _dget(o, "order_id", "OrderID")
        with self._map_lock:
            if oid is not None:
                oid = str(oid)
                self._seq_to_oid[seq] = oid
                self._oid_to_seq[oid] = seq
            pend = self._pending_resp.pop(seq, None)
        log.debug("order response seq=%s order_id=%s status=%s err=%s",
                  seq, oid, _dget(o, "order_status", "OrderStatus"),
                  _dget(o, "error_msg", "ErrorMsg"))
        if pend is not None:
            ev, bucket = pend
            bucket.append(o)
            ev.set()
        if self._on_order_cb is not None:
            try:
                self._on_order_cb(o)
            except Exception:  # noqa: BLE001
                pass

    def _handle_order_error(self, order_error) -> None:
        """旧 SDK 报单失败回调（XtOrderError）：记录以便审计与诊断。"""
        oid = getattr(order_error, "order_id", None)
        eid = getattr(order_error, "error_id", None)
        emsg = getattr(order_error, "error_msg", None)
        log.warning("order error order_id=%s error_id=%s msg=%s", oid, eid, emsg)

    def _handle_cancel_error(self, *args) -> None:
        """撤单失败回调，兼容两种 SDK 签名：

        - 旧 SDK：``on_cancel_error(cancel_error: XtCancelError)`` —— 1 个对象参数，
          从中取 ``order_id`` / ``error_id`` / ``error_msg``；
        - 新 SDK：``on_cancel_error(order_id, error_id, error_msg)`` —— 3 个标量参数。
        """
        oid = eid = emsg = None
        if len(args) == 1:
            ce = args[0]
            if ce is not None:
                oid = getattr(ce, "order_id", None)
                eid = getattr(ce, "error_id", None)
                emsg = getattr(ce, "error_msg", None)
                if oid is None and eid is None and emsg is None and not isinstance(ce, (list, dict)):
                    # 个别版本直接传错误字符串/整型
                    emsg = str(ce)
        elif len(args) >= 3:
            oid, eid, emsg = args[0], args[1], args[2]
        log.warning("cancel error order_id=%s error_id=%s msg=%s", oid, eid, emsg)

    def _handle_stock_trade(self, t: dict) -> None:
        """成交回报：直推 sync 引擎（替代纯轮询）。"""
        if self._on_trade_cb is not None:
            try:
                self._on_trade_cb(t)
            except Exception:  # noqa: BLE001
                pass

    def _wait_order_response(self, seq: int, timeout: float = 10.0) -> str | None:
        """等待 on_order_stock_response 回调解析出柜台真实 order_id。

        返回柜台 order_id（str）；超时未到返回 None（调用方应记作 status=unknown）。
        回调已提前到达时（映射已建立）直接返回，不阻塞。

        并发安全：注册 pending 与回调侧写映射/弹 pending 都在 _map_lock 内，
        消除「回调落在 get(seq) 之后、Event 注册之前」的竞态窗口；等待结束后
        再查一次映射，覆盖「回调已建映射但 event 未触发」的边界。
        """
        with self._map_lock:
            oid = self._seq_to_oid.get(seq)
            if oid is not None:
                return oid
            ev = threading.Event()
            bucket: list = []
            self._pending_resp[seq] = (ev, bucket)
        if ev.wait(timeout):
            with self._map_lock:
                oid = self._seq_to_oid.get(seq)
            if oid is not None:
                return oid
            for o in bucket:
                got = _dget(o, "order_id", "OrderID")
                if got is not None:
                    return str(got)
        return None

    def probe(self) -> dict:
        """结构化环境诊断（不依赖连接）：供 /brokers/test 与 tools/diag_qmt.py 展示。"""
        diag = probe_environment(self.client_path)
        diag["session_id"] = self.session_id
        diag["account_id"] = self._account_id
        return diag

    def close(self) -> None:
        had_trader = self._trader is not None
        trader = self._trader
        # 阶段 0-A（C10）：先标记断开，再释放 trader 与映射，避免回调/竞态复用已停用句柄。
        self._connected = False
        if trader is not None:
            try:
                # 释放交易会话与端口。原实现仅把指针置 None，不调 stop()——
                # 反复连接/断开会累计残留 session/监听端口，最终同账号会话被占满，
                # 后续连接报「session 被占用」，表现为「连接不上 QMT」。
                trader.stop()
            except Exception:  # noqa: BLE001
                pass
        with self._map_lock:
            self._seq_to_oid.clear()
            self._oid_to_seq.clear()
            self._pending_resp.clear()
        self._trader = None
        self._acc = None
        if had_trader:
            log.info("XTPQuantAdapter closed (session_id=%s)", self.session_id)

    def is_connected(self) -> bool:
        return self._connected

    # ---------------- 行情 ----------------
    def get_quote(self, code: str) -> dict:
        if self._xtdata is None:
            raise BrokerSDKError("xtquant", "pip install xtquant")
        tick = self._xtdata.get_full_tick([code]).get(code)
        if not tick:
            raise BrokerNotConnectedError(f"未获取到 {code} 行情（客户端未运行或代码无效）")
        return self._norm_quote(code, tick)

    def get_full_tick(self, codes: list[str]) -> dict:
        if self._xtdata is None:
            raise BrokerSDKError("xtquant", "pip install xtquant")
        try:
            raw = self._xtdata.get_full_tick(list(codes)) or {}
        except Exception as exc:  # noqa: BLE001
            # 行情服务未认证 / 非交易时段 / 客户端未订阅代码时，xtquant 的
            # get_full_tick 可能抛 JSONDecodeError（内部把错误当响应解析）或
            # 其它 SDK 异常。此时不应向上抛协议级错误（会被桥接包装成含糊的
            # "Expecting value: 连接异常"），而应返回空行情，由调用方给出
            # 「客户端已连但行情未就绪」的明确诊断。
            log.warning("get_full_tick 异常（cast 未认证/非交易时段?）：%s: %s",
                        type(exc).__name__, exc)
            return {}
        if not isinstance(raw, dict):
            return {}
        out = {}
        for c in list(codes):
            t = raw.get(c)
            if t and isinstance(t, dict):
                out[c] = self._norm_quote(c, t)
        return out

    def _norm_quote(self, code: str, tick: dict) -> dict:
        def _lst(v, i):
            return v[i] if isinstance(v, (list, tuple)) and len(v) > i else (v if not isinstance(v, (list, tuple)) else None)
        # 五档买卖盘：xtquant get_full_tick 的 bidPrice/askPrice/bidVolume/askVolume
        # 均为长度 5 的数组（买一~买五 / 卖一~卖五）。归一化为 bids/asks 数组供前端展示。
        bid_prices = _dget(tick, "bidPrice", "bid_price") or []
        ask_prices = _dget(tick, "askPrice", "ask_price") or []
        bid_vols = _dget(tick, "bidVolume", "bid_volume") or []
        ask_vols = _dget(tick, "askVolume", "ask_volume") or []
        bids = [{"price": _lst(bid_prices, i), "volume": _lst(bid_vols, i)}
                for i in range(5)]
        asks = [{"price": _lst(ask_prices, i), "volume": _lst(ask_vols, i)}
                for i in range(5)]
        # 键名兼容：tick 快照用 CamelCase（lastPrice/lastClose/bidPrice），K 线 bar 用
        # 小写（close/open/high/low/volume/amount）。last 缺失用 close 兜底（K 线 bar
        # 无最新价字段）、lastClose 缺失用 preClose/pre_close 兜底，避免旧版 K 线订阅
        # 推送的行情 last/lastClose 为 None。
        return {
            "code": code,
            "last": _dget(tick, "lastPrice", "close"),
            "open": _dget(tick, "open", "Open"),
            "high": _dget(tick, "high", "High"),
            "low": _dget(tick, "low", "Low"),
            "lastClose": _dget(tick, "lastClose", "preClose", "pre_close"),
            "volume": _dget(tick, "volume", "Volume"),
            "amount": _dget(tick, "amount", "Amount"),
            "bid": _lst(_dget(tick, "bidPrice", "bid_price"), 0),
            "ask": _lst(_dget(tick, "askPrice", "ask_price"), 0),
            "bid_vol": _lst(_dget(tick, "bidVolume", "bid_volume"), 0),
            "ask_vol": _lst(_dget(tick, "askVolume", "ask_volume"), 0),
            "bids": bids,
            "asks": asks,
            "ts": datetime.now().isoformat(timespec="seconds"),
        }

    def get_instrument_detail(self, code: str) -> dict:
        """合约详情：名称 / 涨停价 / 跌停价 / 昨收（用于涨停板精确涨停价与名称）。"""
        if self._xtdata is None:
            raise BrokerSDKError("xtquant", "pip install xtquant")
        try:
            d = self._xtdata.get_instrument_detail(code) or {}
        except Exception as exc:  # noqa: BLE001
            raise BrokerNotConnectedError(f"合约详情获取失败：{exc}") from exc
        return {
            "code": code,
            "name": d.get("instrument_name") or code,
            "up_limit_price": d.get("up_limit_price"),
            "down_limit_price": d.get("down_limit_price"),
            "pre_close": d.get("pre_close_price"),
            "exchange": d.get("exchange_id"),
        }

    def _kline_lookback_days(self, period: str, count: int) -> int:
        """K 线 count 换算成回看日历天数，供下载预热窗口使用（含缓冲）。"""
        need = {
            "1m": count // 240, "5m": count // 48, "15m": count // 16,
            "30m": count // 8, "1h": count // 4, "1d": count,
            "1w": count * 7, "1mon": count * 30,
        }.get(period, count * 2)
        return max(need + 2, 1)

    def _warm_kline_cache(self, code: str, period: str, count: int,
                          start: str, end: str) -> bool:
        """本地缓存无数据（行情服务离线/未预热）时用 download_history_data 预热。

        仅在 get_kline 空结果时触发一次：先下载历史数据到本地缓存，主进程随后
        重查 get_market_data 即可读到。download 每次都会快速增量（本地已有则复用），
        典型耗时 ~1s，远低于 RPC 超时（30s），不会阻塞。多版本签名容错。
        """
        try:
            from datetime import timedelta
            fn = self._xtdata.download_history_data
            if not start:
                try:
                    from datetime import datetime
                    d0 = datetime.now() - timedelta(days=self._kline_lookback_days(period, count))
                    start = d0.strftime("%Y%m%d")
                except Exception:  # noqa: BLE001
                    start = ""
            # 多签名兼容：download_history_data(stock_code, period, start, end)
            # 或 download_history_data(stock_list, period, start, end)
            try:
                fn(code, period, start or "", end or "")
            except TypeError:
                fn([code], period, start or "", end or "")
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("get_kline 预热下载失败（忽略）：%s", exc)
            return False

    def get_kline(self, code: str, period: str, count: int,
                  start: str = "", end: str = "") -> list[dict]:
        if self._xtdata is None:
            raise BrokerSDKError("xtquant", "pip install xtquant")
        # 迅投协议周期：各版本 xtdata 均用 "1h"（本地缓存集合 {1m,5m,15m,30m,1h,1d}），
        # 平台展示用 "60m" 只是别名——必须归一化，否则旧版 get_market_data("60m") 失败。
        period = _normalize_kline_period(period)
        field_list = ["open", "high", "low", "close", "volume", "amount"]

        def _fetch() -> dict:
            try:
                return self._xtdata.get_market_data(
                    field_list=field_list, stock_list=[code], period=period,
                    start_time=start or "", end_time=end or "", count=int(count),
                    dividend_type="none", fill_data=True)
            except Exception as exc:  # noqa: BLE001
                raise BrokerNotConnectedError(f"K 线获取失败：{exc}") from exc

        data = _fetch()
        # 空结果兜底：本地缓存未就绪（行情服务离线 / 从未下载过该标的）时，首次
        # get_market_data 恒返回空。自动下载预热并重查一次，避免「非交易时段
        # 查看历史 K 线恒为空」。download 已就绪时增量很快，重查命中缓存。
        if not isinstance(data, dict) or not data:
            if self._warm_kline_cache(code, period, int(count), start, end):
                data = _fetch()
        if not isinstance(data, dict) or not data:
            return []
        # 迅投 get_market_data(field_list=..., stock_list=[...]) 返回
        # {字段名: DataFrame}——每个 DataFrame 的 index=股票代码、columns=日期。
        # 注意不是 {code: DataFrame}！旧实现按 data.get(code) 解析永远取不到，
        # 导致 get_kline 恒返回空条（K 线为空的根因，见阶段排查）。
        df0 = next(iter(data.values()))
        if df0 is None or len(df0) == 0:
            return []
        dates = list(df0.columns)
        out = []
        for dt in dates:
            bar = {"time": str(dt)[:19]}
            for fld in field_list:
                sub = data.get(fld)
                val = None
                if sub is not None and code in sub.index and dt in sub.columns:
                    val = self._f(sub.loc[code, dt])
                    # NaN 归一为 None，避免 JSON 序列化 nan
                    if val is not None and val != val:
                        val = None
                bar[fld] = val
            out.append(bar)
        return out

    @staticmethod
    def _f(v):
        try:
            return None if v is None else round(float(v), 4)
        except Exception:  # noqa: BLE001
            return None

    def get_tick(self, code: str) -> dict:
        return self.get_quote(code)

    def get_stock_list(self, sector: str = "沪深A股") -> list[dict]:
        if self._xtdata is None:
            raise BrokerSDKError("xtquant", "pip install xtquant")
        codes = self._xtdata.get_stock_list_in_sector(sector) or []
        out = []
        for c in codes[:3000]:  # 名称逐个查询较耗时，限量保证响应
            name = self._name(c)
            out.append({"code": c, "name": name})
        return out

    def _name(self, code: str) -> str:
        if code in self._name_cache:
            return self._name_cache[code]
        name = code
        try:
            detail = self._xtdata.get_instrument_detail(code)
            if detail and detail.get("instrument_name"):
                name = detail["instrument_name"]
        except Exception:  # noqa: BLE001
            pass
        self._name_cache[code] = name
        return name

    def search_stocks(self, keyword: str, limit: int = 20) -> list[dict]:
        kw = (keyword or "").strip().upper()
        if not kw:
            return []
        # 代码匹配优先（快）
        codes = self._xtdata.get_stock_list_in_sector("沪深A股") if self._xtdata else []
        hits = []
        for c in codes:
            if kw in c.upper():
                hits.append({"code": c, "name": self._name(c)})
                if len(hits) >= limit:
                    return hits
        # 名称匹配
        for c in codes:
            if len(hits) >= limit:
                break
            name = self._name(c)
            if kw in name.upper() and c not in {h["code"] for h in hits}:
                hits.append({"code": c, "name": name})
        return hits

    def subscribe_quote(self, codes: list[str], on_tick, period: str = "1m") -> None:
        """订阅实时行情。

        阶段 0-A（C8）：
        - period 可指定 "tick"（真实逐笔/快照）或 "1m"（1 分钟 K 线）；默认 1m 保持兼容。
        - 去重订阅（重复 symbols 不重复调 SDK，避免回调累积）。
        - 回调异常不再 ``except: pass`` 静默吞掉，改为记日志便于排查。
        - 回调内**不**同步重调 get_full_tick（避免阻塞推送线程），优先使用回调带入的 bar。
        """
        if self._xtdata is None:
            raise BrokerSDKError("xtquant", "pip install xtquant")
        period = _normalize_kline_period(period)  # "60m" -> "1h"（迅投协议）
        # 去重并保持顺序
        codes = list(dict.fromkeys(codes))

        def _cb(datas):
            try:
                for code, per in (datas or {}).items():
                    bars = None
                    if isinstance(per, dict):
                        # 新版 xtdata：{code: {period: [bars]}} —— 取任一有数据的周期最后一根
                        for _p, _bars in per.items():
                            if isinstance(_bars, list) and _bars:
                                bars = _bars
                                break
                    elif isinstance(per, list) and per:
                        # 旧版 xtdata：{code: [data1, data2, ...]} —— 值直接是 bar 列表
                        bars = per
                    if not bars:
                        continue
                    bar = bars[-1]
                    if not isinstance(bar, dict):
                        continue
                    on_tick({"type": "quote", "data": self._norm_quote(code, bar)})
            except Exception as exc:  # noqa: BLE001
                log.warning("subscribe_quote callback error: %s", exc)

        for c in codes:
            try:
                self._xtdata.subscribe_quote(c, period=period, count=0, callback=_cb)
            except Exception as exc:  # noqa: BLE001
                log.warning("subscribe_quote failed for %s: %s", c, exc)

    # ---------------- 账户 ----------------
    def _require_trader(self):
        # 阶段 0-A（C10）：不仅检查对象存在，还要确认连接态——否则对已 stop 的
        # trader 继续调用 order_stock/query_asset 会拿到错误结果或崩溃。
        if not self._connected or self._trader is None or self._acc is None:
            raise BrokerNotConnectedError("交易未连接：未配置 account_id 或客户端未登录")
        return self._trader, self._acc

    def get_account(self) -> dict:
        trader, acc = self._require_trader()
        with self._lock:
            a = self._query_asset(trader, acc)
        if a is None:
            raise BrokerNotConnectedError("账户查询返回空（客户端未就绪）")
        return {"account_id": self._account_id, "account_type": self._account_type,
                "cash": self._f(getattr(a, "cash", None)),
                "frozen": self._f(getattr(a, "frozen_cash", None)),
                "market_value": self._f(getattr(a, "market_value", None)),
                "assets": self._f(getattr(a, "total_asset", None))}

    def _query_asset(self, trader, acc):
        """查资产，兼容 ``query_asset``（新 SDK）与 ``query_stock_asset``（旧 SDK）。

        旧版 xttrader 只有 ``query_stock_asset(account)``，无 ``query_asset``；
        直接调 ``query_asset`` 会在该版本 AttributeError。两者返回的资产对象属性
        都是小写（cash/frozen_cash/market_value/total_asset），上层取出一致。
        """
        fn = getattr(trader, "query_asset", None) or getattr(trader, "query_stock_asset", None)
        if fn is None:
            return None
        try:
            return fn(acc)
        except Exception:  # noqa: BLE001
            try:
                return trader.query_stock_asset(acc)
            except Exception:  # noqa: BLE001
                return None

    def get_positions(self, symbol: str | None = None) -> list[dict]:
        trader, acc = self._require_trader()
        with self._lock:
            pos = trader.query_stock_positions(acc) or []
        out = []
        for p in pos:
            # 多版本属性兼容：旧版全小写，新版部分 CamelCase。
            code = _pick(p, "stock_code", "StockCode")
            if symbol and code != symbol:
                continue
            vol = _pick(p, "volume", "Volume", default=0)
            avail = _pick(p, "can_use_volume", "CanUseVolume", default=0)
            cost = self._f(_pick(p, "open_price", "OpenPrice", "cost_price", "CostPrice"))
            mv = self._f(_pick(p, "market_value", "MarketValue"))
            out.append({"code": code, "name": self._name(code), "volume": vol,
                        "avail": avail, "cost": cost, "market_value": mv})
        return out

    def get_cash(self) -> dict:
        acc = self.get_account()
        return {"cash": acc["cash"], "frozen": acc["frozen"],
                "assets": acc["assets"], "market_value": acc["market_value"]}

    def get_orders(self) -> list[dict]:
        trader, acc = self._require_trader()
        with self._lock:
            orders = trader.query_stock_orders(acc) or []
        out = []
        for o in orders:
            # 多版本属性兼容：旧版全小写（order_id/stock_code/order_volume/
            # traded_volume/order_status），新版部分 CamelCase。
            oid = str(_pick(o, "order_id", "OrderID") or "")
            code = _pick(o, "stock_code", "StockCode")
            otype = _pick(o, "order_type", "OrderType")
            out.append({
                "order_id": oid,
                "code": code,
                "direction": _direction_from_order_type(otype),
                "price": self._f(_pick(o, "price", "Price")),
                "volume": _pick(o, "order_volume", "ordered_volume", "Volume", default=0),
                "dealt": _pick(o, "traded_volume", "deal_volume", "DealVolume", default=0),
                "status": self._order_status(_pick(o, "order_status", "Status", default=-1), oid),
            })
        return out

    def get_deals(self) -> list[dict]:
        trader, acc = self._require_trader()
        with self._lock:
            deals = self._query_stock_deals(trader, acc)
        if not deals:
            return []
        out = []
        for d in deals:
            # 多版本属性兼容（同 get_orders / _trade_as_dict）。
            # 注意退化路径（旧 SDK 无 query_stock_deals）用「已成交订单」近似，
            # 订单对象用 price/order_time，故一并列入候选（真实成交的
            # traded_price/traded_time 优先命中，不会被覆盖）。
            oid = str(_pick(d, "order_id", "OrderID") or "")
            code = _pick(d, "stock_code", "StockCode")
            otype = _pick(d, "order_type", "OrderType")
            out.append({
                "order_id": oid,
                "code": code,
                "direction": _direction_from_order_type(otype),
                "price": self._f(_pick(d, "traded_price", "deal_price", "DealPrice", "price")),
                "volume": _pick(d, "traded_volume", "deal_volume", "DealVolume", default=0),
                "time": _pick(d, "traded_time", "deal_time", "DealTime", "order_time") or "",
                "seq": _pick(d, "seq", "Seq"),
            })
        return out

    def _query_stock_deals(self, trader, acc) -> list:
        """查询成交记录，兼容有无 ``query_stock_deals`` 的 SDK 版本。

        - 新 SDK：``trader.query_stock_deals(acc)``；
        - 旧 SDK（xttrader）：无此方法，退化为「已成交订单」近似（仅用于成交展示，
          真实成交回报仍以 on_stock_trade 回调为准，不会漏单 / 不会重复计）。
        """
        fn = getattr(trader, "query_stock_deals", None)
        if fn is not None:
            try:
                return fn(acc) or []
            except Exception:  # noqa: BLE001
                pass
        try:
            orders = trader.query_stock_orders(acc) or []
            return [o for o in orders
                    if int(_pick(o, "traded_volume", "deal_volume", default=0) or 0) > 0]
        except Exception:  # noqa: BLE001
            return []

    # ---------------- 交易 ----------------
    def place_order(self, code: str, direction: str, price_type: str,
                    price: float, volume: int, strategy_name: str = "",
                    remark: str = "") -> dict:
        trader, acc = self._require_trader()
        # 参数防线（纵深防御，风控层可能被旁路/直调 bridge）：
        # 限价单 price<=0 会以「价格=0」送出成交灾难，必须先拦。
        if (price_type or "limit") == "limit" and price <= 0:
            raise BrokerError("限价单必须提供 >0 的委托价")
        if not (isinstance(volume, int) or float(volume).is_integer()) or volume <= 0:
            raise BrokerError("委托数量必须为正整数")
        xtc = _ensure_xtconstant()
        # 阶段 0-A（C9）：信用账户裸常量 CREDIT_BUY/CREDIT_SELL 在部分 xtquant 版本
        # 不存在（AttributeError）。用 getattr 兜底，缺失时回退标准买卖。
        if self._account_type == "CREDIT" and direction == "buy":
            op = getattr(xtc, "CREDIT_BUY", xtc.STOCK_BUY)
        elif self._account_type == "CREDIT" and direction == "sell":
            op = getattr(xtc, "CREDIT_SELL", xtc.STOCK_SELL)
        elif direction == "buy":
            op = xtc.STOCK_BUY
        else:
            op = xtc.STOCK_SELL
        # 价格类型常量：部分旧版可能无 LATEST_PRICE / 用 MARKET_PRICE，getattr 兜底避免
        # AttributeError；最终数字码与新版一致（LATEST_PRICE=5, FIX_PRICE=11）。
        _mkt = getattr(xtc, "LATEST_PRICE", getattr(xtc, "MARKET_PRICE", 5))
        _fix = getattr(xtc, "FIX_PRICE", 11)
        pt = _mkt if (price_type or "limit") == "market" else _fix
        # 注意参数顺序：真实旧版 order_stock(account, stock_code, order_type, order_volume,
        # price_type, price, strategy_name, order_remark)。原代码把 pt 放在 order_volume
        # 位置会导致真实下单时「量=价格类型、价格类型=价格、价格=量」——交易灾难。
        with self._lock:
            seq = trader.order_stock(acc, code, op, int(volume), pt, float(price),
                                     strategy_name or "", remark or "")
        # 阶段 0-A（C2/F2）：order_stock 失败返回 -1（truthy，不能 `if not` 判断），
        # 必须把 -1 显式判为失败并抛错，否则「下单失败被报成功」→ 审计记 ok、WAL 记 pending。
        if seq is None or seq == -1:
            raise BrokerSDKError(
                "xtquant",
                "下单失败：柜台返回 -1（资金不足/标的不在交易时段/无交易权限/风控拦截）")
        seq = int(seq)
        # 阶段 0-A：真实柜台 order_id 仅经 on_order_stock_response 回调下发，
        # 提交后等待回调（带超时）——超时未到则记作 status=unknown（绝不伪报 submitted）。
        oid = self._wait_order_response(seq, timeout=10.0)
        order_id = oid or str(seq)
        status = "unknown" if oid is None else "submitted"
        return {"order_id": order_id, "seq": seq, "code": code, "direction": direction,
                "price_type": price_type, "price": price, "volume": volume,
                "status": status, "ts": datetime.now().isoformat(timespec="seconds")}

    def cancel_order(self, order_id: str) -> dict:
        trader, acc = self._require_trader()
        # order_id 可能是柜台真实 order_id（place_order 返回），直接传入；
        # seq 已通过 _seq_to_oid 映射为真实 id，无需再转换。
        with self._lock:
            trader.cancel_order_stock(acc, int(order_id))
        return {"order_id": str(order_id), "status": "cancel_submitted"}

    def _order_status(self, st: int, oid: str) -> str:
        # 阶段 0-A（F12）：统一走共享词汇表，消除三处各说各话。
        from .order_status import normalize_order_status
        return normalize_order_status(st)

    # ---------------- 参考数据 / L2（真实 xtdata 调用） ----------------
    def get_sector_list(self) -> list[str]:
        if self._xtdata is None:
            raise BrokerSDKError("xtquant", "pip install xtquant")
        try:
            return [s for s in (self._xtdata.get_sector_list() or []) if s]
        except Exception as exc:  # noqa: BLE001
            raise BrokerNotConnectedError(f"板块列表获取失败：{exc}") from exc

    def get_sector_stocks(self, sector: str = "沪深A股") -> list[str]:
        if self._xtdata is None:
            raise BrokerSDKError("xtquant", "pip install xtquant")
        try:
            return [c for c in (self._xtdata.get_stock_list_in_sector(sector) or []) if c]
        except Exception as exc:  # noqa: BLE001
            raise BrokerNotConnectedError(f"板块成分获取失败：{exc}") from exc

    def get_trading_calendar(self, start: str = "", end: str = "") -> list[str]:
        if self._xtdata is None:
            raise BrokerSDKError("xtquant", "pip install xtquant")
        try:
            # 多版本签名兼容：
            # - 旧 SDK：get_trading_calendar(market, start_time='', end_time='', tradetimes=False)
            #   首个位置参数必填为 market，乱填 start 会查错市场；
            # - 新 SDK：可能 get_trading_calendar(start_time, end_time, ...) 无 market。
            # 用 inspect 探测首参名决定调用方式（SH/SZ 交易日一致，取 SH 即可）。
            import inspect
            sig = inspect.signature(self._xtdata.get_trading_calendar)
            params = [p for p in sig.parameters]
            if params and params[0].lower().startswith("market"):
                cal = self._xtdata.get_trading_calendar("SH", start or "", end or "")
            else:
                cal = self._xtdata.get_trading_calendar(start or "", end or "")
            return [str(d) for d in (cal or [])]
        except Exception as exc:  # noqa: BLE001
            raise BrokerNotConnectedError(f"交易日历获取失败：{exc}") from exc

    def get_financial(self, code: str) -> dict:
        if self._xtdata is None:
            raise BrokerSDKError("xtquant", "pip install xtquant")
        fields = ["EPS", "BPS", "OPERATE_INCOME", "TOTAL_OPERATE_INCOME",
                  "PARENT_NETPROFIT", "TOTAL_OPERATE_EXPENSE", "ROE", "CAPITAL",
                  "TOTAL_OPERATE_INCOME_YOY", "PARENT_NETPROFIT_YOY"]
        try:
            df = self._xtdata.get_stock_financial([code], fields, "", "",
                                                  report_type="report_time")
        except Exception as exc:  # noqa: BLE001
            raise BrokerNotConnectedError(f"财务数据获取失败：{exc}") from exc
        frame = (df or {}).get(code)
        if frame is None or len(frame) == 0:
            return {"code": code, "detail": "无财务数据（数据权限或代码无效）"}
        row = frame.iloc[-1]
        out = {"code": code, "report_time": str(frame.index[-1])[:10]}
        for f in fields:
            try:
                v = row.get(f)
                out[f] = None if v is None else round(float(v), 4)
            except Exception:  # noqa: BLE001
                out[f] = None
        return out

    def get_l2_transactions(self, code: str, count: int = 100) -> list[dict]:
        if self._xtdata is None:
            raise BrokerSDKError("xtquant", "pip install xtquant")
        try:
            # 多版本参数顺序兼容：旧 SDK 为
            # get_l2_transaction(field_list=[], stock_code='', start_time='', end_time='',
            #                   count=-1)，首参是 field_list。用关键字 stock_code=/count=
            # 调用最稳；个别版本若关键字不接收则退化为位置参数。
            try:
                data = self._xtdata.get_l2_transaction(stock_code=code, count=int(count))
            except TypeError:
                data = self._xtdata.get_l2_transaction([], code, "", "", int(count))
        except Exception as exc:  # noqa: BLE001
            raise BrokerNotConnectedError(f"L2 逐笔获取失败：{exc}") from exc
        df = (data or {}).get(code)
        if df is None or len(df) == 0:
            return []
        out = []
        for idx, row in df.iterrows():
            out.append({
                "time": str(idx)[11:19],
                "price": self._f(row.get("price")),
                "volume": int(row.get("volume") or 0),
                "type": "buy" if int(row.get("buyorsell") or 0) == 0 else "sell",
            })
        return out

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
import sys
import threading
import time
from datetime import datetime

from .base import BrokerAdapter, BrokerNotConnectedError, BrokerSDKError

log = logging.getLogger("qmt_work")

# 账户类型 -> xt_trader 账户类
_ACCOUNT_CLASS = {}

# 下单操作映射
_ORDER_OP = {}

# 下单价格类型映射
_PRICE_TYPE = {}

# 已解析的 xtquant site-packages 路径缓存（规范化 client_path -> path 或 None）
_XTQUANT_CACHE: dict[str, str | None] = {}

# 客户端内 xtquant 常见相对位置（相对客户端根目录；按出现频率排序）
_XTQUANT_REL = [
    os.path.join("bin.x64", "Lib", "site-packages"),
    os.path.join("bin.x64", "Python", "Lib", "site-packages"),
    os.path.join("bin.x64", "python", "Lib", "site-packages"),
    os.path.join("bin.x64", "python311", "Lib", "site-packages"),
    os.path.join("bin.x64", "python312", "Lib", "site-packages"),
    os.path.join("bin.x64", "python310", "Lib", "site-packages"),
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
        return s
    return sorted(cands, key=lambda d: -_score(d))


def _is_likely_root(d: str) -> bool:
    """目录是否像客户端根（含 bin.x64 / userdata_mini / userdata 标记）。"""
    return (os.path.isdir(os.path.join(d, "bin.x64"))
            or os.path.isdir(os.path.join(d, "userdata_mini"))
            or os.path.isdir(os.path.join(d, "userdata")))


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
        from .runtime import host_python_minor, detect_xtquant_abis
    except Exception:  # noqa: BLE001
        host_python_minor = lambda: sys.version_info[0] * 100 + sys.version_info[1]
        detect_xtquant_abis = lambda sp: []
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
            result["hint"] = ("未找到 xtquant 目录：请确认 client_path 指向 userdata_mini 目录"
                              "（或其上层为客户端根，含 bin.x64）")
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
        result["hint"] = ("未找到 xtquant 目录：请确认 client_path 指向 userdata_mini 目录"
                          "（或其上层为客户端根，含 bin.x64）")
    # P0：ABI 运行时方案（进程内直连 / 桥接子进程）+ 可操作提示
    try:
        from .runtime import host_python_minor, xtp_runtime_plan, discover_system_runtimes
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
        from xtquant.xt_trader import XtQuantTrader  # 新版
        import xtquant.xt_trader as _acc_mod
    except ImportError:
        from xtquant.xttrader import XtQuantTrader  # 旧版
        import xtquant.xttype as _acc_mod
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


class XTPQuantAdapter(BrokerAdapter):
    """迅投 XTQuant 真实适配器。"""

    def __init__(self, client_path: str, account_id: str, account_type: str = "STOCK",
                 session_id: int = 0, min_version: str = ""):
        self.client_path = client_path
        self._account_id = account_id
        self._account_type = (account_type or "STOCK").upper()
        self.session_id = int(session_id or 0)
        self.min_version = min_version
        self._xtdata = None
        self._trader = None
        self._acc = None
        self._connected = False
        self._lock = threading.Lock()
        self._order_status_cache: dict[str, str] = {}
        self._name_cache: dict[str, str] = {}

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
        return ["1m", "5m", "15m", "30m", "60m", "1d", "1w", "1mon"]

    @property
    def supported_account_types(self) -> list[str]:
        return ["STOCK", "CREDIT", "OPTION", "FUTURES"]

    @property
    def sdk_required(self) -> str:
        return "xtquant"

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
                "请确认「券商连接」填写的 client_path 是 userdata_mini 目录且客户端已安装登录，"
                "或手动 pip install xtquant") from exc

        self._xtdata = xtdata
        try:
            xtdata.connect()
        except Exception:  # noqa: BLE001
            pass  # xtdata 在未启动客户端时会失败；后续行情查询会明确报错

        if not self._account_id:
            # 仅行情模式（无交易账号）：行情可用，交易不可用
            self._connected = True
            return

        try:
            # session 占用规避：start()!=0 时递增 session_id 重试（0..5），
            # 规避用户已手动打开 QMT 客户端占用默认 session 的场景
            trader = None
            last_err = ""
            for attempt in range(6):
                sid = self.session_id + attempt
                t = XtQuantTrader(self.client_path, sid)
                rc = t.start()
                if rc == 0:
                    trader = t
                    self.session_id = sid
                    break
                last_err = f"start rc={rc}"
            if trader is None:
                raise BrokerNotConnectedError(
                    f"XtQuantTrader 启动失败（session_id {self.session_id}~"
                    f"{self.session_id + 5} 均失败，最后 {last_err}）："
                    f"请确认 client_path 是 userdata_mini 目录、未被其他进程占用"
                    f"（如已开 QMT 客户端），或手动指定其他 session_id")
            if trader.connect() != 0:
                raise BrokerNotConnectedError("XtQuantTrader 连接失败（QMT 客户端未登录或未运行）")
            if self._account_type not in _acc_classes:
                raise BrokerNotConnectedError(
                    f"该客户端不支持账户类型 {self._account_type}（支持：{sorted(_acc_classes)}）")
            cls = _acc_classes[self._account_type]
            self._acc = cls(self._account_id, self._account_type)
            trader.subscribe(self._acc)
            self._trader = trader
            self._connected = True
        except BrokerNotConnectedError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise BrokerNotConnectedError(f"交易连接异常：{exc}") from exc

    def probe(self) -> dict:
        """结构化环境诊断（不依赖连接）：供 /brokers/test 与 tools/diag_qmt.py 展示。"""
        diag = probe_environment(self.client_path)
        diag["session_id"] = self.session_id
        diag["account_id"] = self._account_id
        return diag

    def close(self) -> None:
        self._connected = False
        if self._trader is not None:
            try:
                self._trader.stop()
            except Exception:  # noqa: BLE001
                pass

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
        raw = self._xtdata.get_full_tick(list(codes)) or {}
        return {c: self._norm_quote(c, t) for c, t in raw.items()}

    def _norm_quote(self, code: str, tick: dict) -> dict:
        def _lst(v, i):
            return v[i] if isinstance(v, (list, tuple)) and len(v) > i else (v if not isinstance(v, (list, tuple)) else None)
        return {
            "code": code,
            "last": tick.get("lastPrice"),
            "open": tick.get("open"),
            "high": tick.get("high"),
            "low": tick.get("low"),
            "lastClose": tick.get("lastClose"),
            "volume": tick.get("volume"),
            "amount": tick.get("amount"),
            "bid": _lst(tick.get("bidPrice"), 0),
            "ask": _lst(tick.get("askPrice"), 0),
            "bid_vol": _lst(tick.get("bidVolume"), 0),
            "ask_vol": _lst(tick.get("askVolume"), 0),
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

    def get_kline(self, code: str, period: str, count: int,
                  start: str = "", end: str = "") -> list[dict]:
        if self._xtdata is None:
            raise BrokerSDKError("xtquant", "pip install xtquant")
        field_list = ["open", "high", "low", "close", "volume", "amount"]
        try:
            data = self._xtdata.get_market_data(
                field_list=field_list, stock_list=[code], period=period,
                start_time=start or "", end_time=end or "", count=int(count),
                dividend_type="none", fill_data=True)
        except Exception as exc:  # noqa: BLE001
            raise BrokerNotConnectedError(f"K 线获取失败：{exc}") from exc
        df = (data or {}).get(code)
        if df is None or len(df) == 0:
            return []
        out = []
        for idx, row in df.iterrows():
            out.append({
                "time": str(idx)[:19],
                "open": self._f(row.get("open")),
                "high": self._f(row.get("high")),
                "low": self._f(row.get("low")),
                "close": self._f(row.get("close")),
                "volume": self._f(row.get("volume")),
                "amount": self._f(row.get("amount")),
            })
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

    def subscribe_quote(self, codes: list[str], on_tick) -> None:
        if self._xtdata is None:
            raise BrokerSDKError("xtquant", "pip install xtquant")
        codes = list(codes)

        def _cb(datas):
            try:
                for code, per in (datas or {}).items():
                    if not isinstance(per, dict):
                        continue
                    bar = None
                    for _p, bars in per.items():
                        if isinstance(bars, list) and bars:
                            bar = bars[-1]
                    tick = self._xtdata.get_full_tick([code]).get(code) if bar is None else bar
                    if tick:
                        on_tick({"type": "quote", "data": self._norm_quote(code, tick)})
            except Exception:  # noqa: BLE001
                pass

        for c in codes:
            try:
                self._xtdata.subscribe_quote(c, period="1m", count=0, callback=_cb)
            except Exception:  # noqa: BLE001
                pass

    # ---------------- 账户 ----------------
    def _require_trader(self):
        if self._trader is None or self._acc is None:
            raise BrokerNotConnectedError("交易未连接：未配置 account_id 或客户端未登录")
        return self._trader, self._acc

    def get_account(self) -> dict:
        trader, acc = self._require_trader()
        with self._lock:
            a = trader.query_asset(acc)
        if a is None:
            raise BrokerNotConnectedError("账户查询返回空（客户端未就绪）")
        return {"account_id": self._account_id, "account_type": self._account_type,
                "cash": self._f(a.cash), "frozen": self._f(a.frozen_cash),
                "market_value": self._f(a.market_value), "assets": self._f(a.total_asset)}

    def get_positions(self, symbol: str | None = None) -> list[dict]:
        trader, acc = self._require_trader()
        with self._lock:
            pos = trader.query_stock_positions(acc) or []
        out = []
        for p in pos:
            code = getattr(p, "stock_code", None)
            if symbol and code != symbol:
                continue
            vol = getattr(p, "volume", 0)
            avail = getattr(p, "can_use_volume", 0)
            cost = self._f(getattr(p, "open_price", None))
            mv = self._f(getattr(p, "market_value", None))
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
            oid = str(getattr(o, "order_id", ""))
            out.append({
                "order_id": oid, "code": getattr(o, "stock_code", ""),
                "direction": "buy" if getattr(o, "order_type", 0) in (23, 33) else "sell",
                "price": self._f(getattr(o, "price", None)),
                "volume": getattr(o, "ordered_volume", 0),
                "dealt": getattr(o, "deal_volume", 0),
                "status": self._order_status(getattr(o, "status", -1), oid),
            })
        return out

    def get_deals(self) -> list[dict]:
        trader, acc = self._require_trader()
        with self._lock:
            deals = trader.query_stock_deals(acc) or []
        out = []
        for d in deals:
            out.append({
                "order_id": str(getattr(d, "order_id", "")), "code": getattr(d, "stock_code", ""),
                "direction": "buy" if getattr(d, "order_type", 0) in (23, 33) else "sell",
                "price": self._f(getattr(d, "deal_price", None)),
                "volume": getattr(d, "deal_volume", 0),
                "time": getattr(d, "deal_time", ""),
            })
        return out

    # ---------------- 交易 ----------------
    def place_order(self, code: str, direction: str, price_type: str,
                    price: float, volume: int, strategy_name: str = "",
                    remark: str = "") -> dict:
        trader, acc = self._require_trader()
        xtc = _ensure_xtconstant()
        op = (xtc.CREDIT_BUY if self._account_type == "CREDIT" and direction == "buy"
              else xtc.CREDIT_SELL if self._account_type == "CREDIT" and direction == "sell"
              else xtc.STOCK_BUY if direction == "buy" else xtc.STOCK_SELL)
        pt = xtc.LATEST_PRICE if (price_type or "limit") == "market" else xtc.FIX_PRICE
        with self._lock:
            oid = trader.order_stock(acc, code, op, pt, float(price), int(volume),
                                     strategy_name or "", remark or "")
        if not oid:
            raise BrokerNotConnectedError("下单失败：客户端返回空委托号（检查交易权限/资金/标的可交易）")
        return {"order_id": str(oid), "code": code, "direction": direction,
                "price_type": price_type, "price": price, "volume": volume,
                "status": "submitted", "ts": datetime.now().isoformat(timespec="seconds")}

    def cancel_order(self, order_id: str) -> dict:
        trader, acc = self._require_trader()
        with self._lock:
            trader.cancel_order_stock(acc, int(order_id))
        return {"order_id": str(order_id), "status": "cancel_submitted"}

    def _order_status(self, st: int, oid: str) -> str:
        mapping = {48: "unreported", 49: "wait_report", 50: "reported",
                   51: "reported_cancel_pending", 52: "part_deal_cancel_pending",
                   53: "part_cancel", 54: "canceled", 55: "part_deal",
                   56: "fully_dealt", 57: "rejected", 86: "confirmed", 255: "unknown"}
        return mapping.get(int(st), "unknown")

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
            cal = self._xtdata.get_trading_calendar(start or "", end or "") or []
            return [str(d) for d in cal]
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
            data = self._xtdata.get_l2_transaction([code], "", int(count))
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

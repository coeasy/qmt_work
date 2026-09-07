"""共享常量 / 工具 / 内部辅助（自原 xtp.py 逐行搬移）。"""

import logging
import os
import re
import sys
import threading
from datetime import datetime


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

def _shell_attr(name: str):
    """经壳模块 xtquant_client.xtp 动态取属性（monkeypatch 兼容层）。

    拆分后 `_probe_quote_service` / `_ensure_xtconstant` 等符号定义在本包子模块，
    但测试通过 ``monkeypatch.setattr(xtquant_client.xtp, name, ...)``（或直接对壳
    模块属性赋值）打补丁。若调用方按名静态引用（``from ._common import X``），
    补丁不会生效。因此所有「测试会替换」的内部符号的调用点一律经本函数在壳模块
    上动态查找：未打补丁时壳模块属性即原始函数，行为不变；打补丁后取到替换对象。
    （这是本次机械拆分中唯一允许的行为兼容性改动，其余代码逐行原样搬移。）
    """
    import xtquant_client.xtp as _shell
    return getattr(_shell, name)



__all__ = [
    'log',
    '_ACCOUNT_CLASS',
    '_ORDER_OP',
    '_PRICE_TYPE',
    '_XTQUANT_CACHE',
    '_XTQUANT_REL',
    '_normalize',
    '_candidate_roots',
    '_is_likely_root',
    '_SYSTEM_DIR_NAMES',
    '_is_system_dir',
    '_resolve_xtquant_path',
    '_load_xtquant_from',
    '_ensure_xtconstant',
    '_TRADER_API_CACHE',
    '_load_trader_api',
    '_pick',
    '_dget',
    '_normalize_kline_period',
    '_BUY_ORDER_TYPES',
    '_SELL_ORDER_TYPES',
    '_direction_from_order_type',
    '_shell_attr',
]

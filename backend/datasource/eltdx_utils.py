"""eltdx 公共工具函数：代码转换、缓存读写、周期映射等。

从 eltdx_source.py 拆出，降低主文件复杂度（1143 → ~950 行）。
这些函数无副作用，可独立测试。
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from datasource.periods import to_eltdx_period
from core.clock import local_now

log = logging.getLogger("qmt_work.datasource.eltdx")

# 交易所前缀映射
_EXCH_PFX = {"SH": "sh", "SZ": "sz", "BJ": "bj"}

# 本地缓存文件名
_NAME_CACHE = "stock_names.json"
_INDUSTRY_CACHE = "stock_industry.json"

# 全角字母/数字 → 半角
_FW2HW = str.maketrans(
    "ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ0１２3456789",
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789",
)

# 主要宽基指数兜底名称
_INDEX_FALLBACK_NAMES = {
    "000001.SH": "上证指数", "000300.SH": "沪深300", "000016.SH": "上证50",
    "000905.SH": "中证500", "000852.SH": "中证1000", "000688.SH": "科创50",
    "000699.SH": "科创100", "399001.SZ": "深证成指", "399006.SZ": "创业板指",
    "399005.SZ": "中小100", "399300.SZ": "沪深300", "399903.SZ": "中证100",
    "899050.BJ": "北证50",
}


def is_index_code(code: str) -> bool:
    """该标的是不是**指数 / 板块指数**（决定 eltdx 取 K 线时必须传 ``kind``）。

    ★★ 为什么必须是「按交易所分别判定」而不是一串前缀（2026-09-21 实测踩到）：
    代码前缀在不同交易所含义**完全不同** ——
      - 沪市 ``000xxx`` = **指数**（000001 上证指数 / 000300 沪深300 / 000905 中证500）；
      - 深市 ``000xxx`` = **个股**（000001.SZ 平安银行、000002.SZ 万科A）！
    所以「``c6.startswith("000")`` 就当指数」这种写法在**沪市对、深市错**，
    而错的后果极不对称：指数被当个股取 ⇒ 报 ProtocolError（可感知）；
    **个股被当指数取 ⇒ 取到指数的 K 线，或静默返回空** —— 界面出图但标的是错的，
    或干脆一片空白，两种都极难发现。

    （本文件此前已有一份同样的判定写在 ``_get_index_name`` 里，那份只在**查名称**
    时用：误判无非是多查一次名称表、查不到返回 None，无害。因此它可以宽松；
    而 ``kind`` 判定直接决定取到谁的数据，必须严格 —— 故独立成此函数，二者不再共用。）

    各交易所的真实代码段（已按 2026-09-21 实测的注册清单核对）：
      - 沪市：``000xxx``（指数）/ ``999xxx``（指数）/ ``880xxx``·``881xxx``（通达信板块指数）。
        沪市个股在 600/601/603/605/688/689 段，基金在 5xx 段，**均不落在 000 段**。
      - 深市：**仅** ``399xxx``（深证成指 399001 / 创业板指 399006 / 中小100 399005）。
        深市 000/001/002/003/300/301 段**全部是个股**，绝不能当指数。
      - 北交所：``899xxx``（北证50 899050）。43/83/87/92 段是个股。
    """
    c = (code or "").upper().strip()
    if not c:
        return False
    num, _, exch = c.partition(".")
    exch = exch or "SH"
    if exch == "SH":
        return num.startswith(("000", "999", "880", "881"))
    if exch == "SZ":
        # ★ 深市只有 399 段是指数；000 段是平安银行/万科这类**个股**
        return num.startswith(("399", "880", "881"))
    if exch == "BJ":
        return num.startswith("899")
    return False


def _f(v) -> Optional[float]:
    """宽松转 float：F10 返回的涨跌幅/价格可能是 str 或 None，失败返回 None（不伪造 0）。"""
    if v is None or v == "":
        return None
    try:
        return round(float(v), 4)
    except (TypeError, ValueError):
        return None


def _normalize_name(nm: str) -> str:
    """规整证券简称：全角转半角，并去掉 TDX 名称里的填充空格（「万 科Ａ」→「万科A」）。"""
    nm = nm.translate(_FW2HW)
    return re.sub(r"\s+", "", nm)


def _cache_dir() -> Path:
    """返回运行时数据目录（与 app.db 同目录）。"""
    try:
        from core.config import settings
        d = Path(str(settings.db_path)).parent
    except Exception:  # noqa: BLE001
        d = Path("data")
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return d


def _name_cache_path() -> Path:
    return _cache_dir() / _NAME_CACHE


# ---- 本地名称表查询（单一真相来源） -----------------------------------------
#
# ★★ 为什么需要它（2026-09-20 实测发现两处同源缺陷）：
#
# 运行时数据目录（与 app.db 同目录）下的 `stock_names.json` 由 eltdx 源维护，
# 真实安装里 215 KB / 7175 条（`688837.SH -> 信诺维`）。但此前**没人去读它**，
# 于是「名称」在两条链路上各自静默失效：
#
# 1. 选股：股票池退化到「券商板块成分 / 本地日线」兜底时 names 为空 ⇒ 结果
#    「名称」列整列空白，且 `_is_st("")` 恒 False ⇒ **「排除 ST」一只都排不掉**。
# 2. 涨停监控池：`LimitUpMonitor.add(code)` 在未传 name 时 `name or code`
#    ⇒ 接口返回 `{"code":"600000.SH","name":"600000.SH"}`，界面把**代码当名称**显示。
#
# 所以把「按代码查本地名称」收敛到**这里唯一一份实现**，供各业务域共用。
# 查不到就**留空**（零 mock：绝不拿代码冒充名称、也不编造）。
_NAME_MEMO: Optional[dict] = None


def _load_name_cache() -> dict:
    """惰性加载运行时名称表（进程内缓存）；失败返回空表，绝不抛。"""
    global _NAME_MEMO
    if _NAME_MEMO is not None:
        return _NAME_MEMO
    memo: dict = {}
    try:
        raw = _load_json_cache(_name_cache_path()) or {}
        memo = {str(k): _normalize_name(str(v or "")) for k, v in raw.items() if v}
    except Exception as exc:  # noqa: BLE001
        log.warning("股票名称表加载失败（名称将留空，不伪造）：%s", exc)
        memo = {}
    _NAME_MEMO = memo
    return memo


def lookup_name(code: str) -> str:
    """按代码查名称；查不到返回 ``""``（调用方须显式渲染成占位符）。

    ★ 2026-09-20 实测修复：**指数名称此前恒退化成代码**。
    本地名称表 `stock_names.json` 只由 eltdx 维护**个股**（实测 7175 条，无一条指数），
    而 `_INDEX_FALLBACK_NAMES`（上证指数 / 深证成指 / 沪深300 …）虽在本文件里定义，
    却只被 `eltdx_source.py` 自己那份 `lookup_name` 使用 —— 于是走
    「单一真相来源」这条路的调用方（行情合并 `registry._merge_quote`、
    基本信息 `market.py`）拿到的是 `""`，再被 `or code` 兜成**代码当名称**，
    界面上市值指数只能显示 `000300.SH` 而不是「沪深300」。
    指数名单是**稳定且有限**的，内置兜底既不联网也不会过时，故在此接上。
    """
    c = (code or "").strip().upper()
    if not c:
        return ""
    nm = str(_load_name_cache().get(c) or "")
    if nm:
        return nm
    return str(_INDEX_FALLBACK_NAMES.get(c) or "")


def lookup_names(codes) -> dict:
    """批量查本地名称表 → ``{code: name}``，**只含查到的**（查不到的键不出现）。"""
    out: dict = {}
    for c in codes or []:
        key = str(c or "").strip().upper()
        if not key or key in out:
            continue
        nm = lookup_name(key)
        if nm:
            out[key] = nm
    return out


def reset_name_cache() -> None:
    """清空进程内名称表缓存（测试 / 名称表被重新同步后调用）。"""
    global _NAME_MEMO
    _NAME_MEMO = None


def _industry_cache_path() -> Path:
    return _cache_dir() / _INDUSTRY_CACHE


def _num(code: str) -> str:
    """qmt 代码 600519.SH -> 6 位数字 600519（F10/题材接口用纯数字）。"""
    return (code or "").split(".")[0].strip()


def _to_eltdx(code: str) -> str:
    """qmt 代码 600519.SH -> eltdx 代码 sh600519。"""
    c = (code or "").upper().strip()
    if "." in c:
        num, exch = c.split(".", 1)
    else:
        num, exch = c, "SH"
    pfx = _EXCH_PFX.get(exch, "sh")
    return f"{pfx}{num}"


def _to_qmt(eltdx_code: str) -> str:
    """eltdx 代码 sh600519 -> qmt 代码 600519.SH。"""
    pfx = eltdx_code[:2].lower()
    num = eltdx_code[2:]
    exch = {"sh": "SH", "sz": "SZ", "bj": "BJ"}.get(pfx, "SH")
    return f"{num}.{exch}"


def _map_period(p: str) -> str:
    """qmt 周期 -> eltdx 周期（委托契约表，见 app/datasource/periods.py）。

    ⚠️ 历史坑（2026-08-29 修复）：此处曾为 `return m.get(p, "day")`，
    未知周期（含前端实际使用的 "1mo"/"1q"）会**静默降级为日线**，
    导致用户点月线/季线看到日线数据，且错误数据以 period 为键写入 K 线缓存。
    现改为查契约表：未知周期抛 UnknownPeriodError，不支持的周期（季线）抛
    UnsupportedPeriodError —— 宁可报错，也不返回错误数据。
    """
    return to_eltdx_period(p)


def _map_adjust(a: Optional[str]) -> Optional[str]:
    """qmt 复权 -> eltdx 复权（qfq/hfq，其余视为不复权）。"""
    if not a:
        return None
    a = a.lower()
    if a in ("qfq", "hfq"):
        return a
    return None


def _load_json_cache(path: Path, max_age_days: Optional[float] = None) -> Optional[dict]:
    """读取 JSON 缓存；max_age_days 不为 None 时超期返回 None。"""
    try:
        if not path.exists():
            return None
        if max_age_days is not None:
            age = (local_now() - datetime.fromtimestamp(path.stat().st_mtime)).days
            if age > max_age_days:
                return None
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        log.warning("读取本地缓存失败 %s: %s", path.name, exc)
        return None


def _save_json_cache(path: Path, data: dict) -> None:
    try:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except Exception as exc:  # noqa: BLE001
        log.warning("写入本地缓存失败 %s: %s", path.name, exc)
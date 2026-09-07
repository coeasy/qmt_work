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

from app.datasource.periods import to_eltdx_period

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
            age = (datetime.now() - datetime.fromtimestamp(path.stat().st_mtime)).days
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
"""Phase 4：多源股票池（universe）解析（D-J §J.5）。

``resolve_universe`` 支持 ``all / sector / index / custom / saved_board / screen_result /
holdings`` 六类股票池，**每类都按能力链多源降级**并返回
``provider_used / as_of / degraded``。引擎与业务代码不得出现 provider 名字（§J.1）。

本函数为 async（sector/index 需在线取成分股），调用方须在 async 上下文 await。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

log = logging.getLogger("qmt_work.screener.universe")

_UNIVERSE_KINDS = (
    "all", "sector", "index", "custom", "saved_board", "screen_result", "holdings",
)


@dataclass
class UniverseSpec:
    kind: str = "all"
    value: Optional[str] = None                 # sector/index 的取值（板块名 / 指数代码）
    codes: Optional[List[str]] = None           # custom 模式的代码列表
    board: Optional[str] = None                 # saved_board 的板块名

    @classmethod
    def parse(cls, raw: Any) -> "UniverseSpec":
        if raw is None:
            return cls(kind="all")
        if isinstance(raw, UniverseSpec):
            return raw
        if isinstance(raw, str):
            # 兼容 "sector:医药" / "index:000300" 简写
            if ":" in raw:
                k, _, v = raw.partition(":")
                return cls(kind=k.strip() or "all", value=v.strip() or None)
            return cls(kind=raw.strip() or "all")
        if isinstance(raw, dict):
            return cls(
                kind=raw.get("kind", "all"),
                value=raw.get("value"),
                codes=raw.get("codes"),
                board=raw.get("board"),
            )
        raise ValueError(f"非法 universe 规格：{raw!r}")


def _store(store=None):
    if store is not None:
        return store
    from datasource.local_store import get_store
    return get_store()


def _hub(hub=None):
    if hub is not None:
        return hub
    from datasource.registry import get_manager
    return get_manager()


def _empty(reason: str) -> dict:
    return {"codes": [], "names": {}, "provider_used": None, "as_of": None,
            "degraded": True, "degraded_reason": reason}


# ---- 本地名称表（运行时缓存） ------------------------------------------------
#
# ★★ 为什么股票池必须补名称（2026-09-20 实测发现）：
#
# 股票池一旦走到「券商板块成分」或「本地日线」兜底，`names` 就是空字典。此前
# 这个空字典**同时造成两个后果**，且都不报错、静默发生：
#
# 1. **展示**：`engine.evaluate_scan` 里 `"name": names.get(code, "")` ⇒ 选股结果
#    每一行的 `name` 都是 `""`，界面「名称」列整列空白（不是 `--`，是空白 —— 空串
#    绕过了前端 `?? "--"` 的兜底）。实测：条件树与公式 DSL 返回
#    `{"code":"000333.SZ","name":"","close":84.4}`；`screen_picks.name` 同样全空。
#
# 2. **功能**：`_prefilter_codes` 用 `_is_st(names.get(c, ""))` 判 ST/退市
#    ⇒ 名称全空时 `_is_st("")` 恒为 False ⇒ **勾选「排除 ST」一只都排不掉**，
#    而且 `meta["excluded_st"]` 报 0，看起来像「本来就没有 ST」。
#    退市股识别（`"退市" in name`）同样失效。
#
# 而名称表**本来就在**：运行时数据目录（与 app.db 同目录）下的 `stock_names.json`，
# 真实安装里 215 KB / 7175 条（`688837.SH -> 信诺维`），由 eltdx 源维护、与之共用
# 同一份缓存。此前只是**没人去读它**。
#
# ★ 实现已上提到 `datasource/eltdx_utils.py::lookup_names`（单一真相来源）——
# 涨停监控池（`engines/limitup.py` 未传 name 时 `name or code` ⇒ 界面把**代码当名称**）
# 有同一个缺口，两处各写一套必然再次漂移。本模块只做「保留在线名 + 补缺失」的编排。


def _load_name_cache() -> Dict[str, str]:
    """惰性加载运行时名称表（委托给唯一实现）；失败返回空表，绝不伪造。"""
    from datasource.eltdx_utils import _load_name_cache as _impl
    return _impl()


def _fill_names(codes: List[str],
                names: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """补全股票池名称：先保留已有（在线源）名称，缺失的查本地名称表。

    查不到仍留空 —— 前端须把空名称显式渲染成占位符，不得静默空白。
    """
    out: Dict[str, str] = {k: v for k, v in (names or {}).items() if v}
    missing = [c for c in codes if not out.get(c)]
    if not missing:
        return out
    from datasource.eltdx_utils import lookup_names
    for code, nm in lookup_names(missing).items():
        out[code] = nm
    return out


async def resolve_universe(spec: UniverseSpec, *, policy_str: str = "auto",
                           hub=None, store=None) -> dict:
    """解析股票池 → {codes, names, provider_used, as_of, degraded, degraded_reason}。

    解析失败（源不可用 / 空池）一律返回 **空 codes + degraded + 原因**，绝不抛 500 也不伪造。
    """
    spec = UniverseSpec.parse(spec)
    if spec.kind not in _UNIVERSE_KINDS:
        return _empty(f"unknown_universe:{spec.kind}")

    # 仅本地类（all/custom/saved_board）才惰性取本地仓；纯在线类（sector/index/
    # holdings）不触发 get_store()，避免在线场景无 DB 时误报。
    st = _store(store) if (store is not None or spec.kind in ("all", "custom", "saved_board")) else None
    hb = _hub(hub) if spec.kind in ("sector", "index", "holdings") else None

    # —— all / custom / saved_board：本地仓直接满足 ——
    if spec.kind == "all":
        rows = st.get_stock_list()
        codes = [r["code"] for r in rows]
        names = {r["code"]: r.get("name", "") or "" for r in rows}
        if codes:
            return {"codes": codes, "names": names, "provider_used": "local",
                    "as_of": None, "degraded": False, "degraded_reason": None}
        # 本地股票列表为空 ⇒ 退化到券商「沪深A股」成分（实测 5224 只）。
        # 此前直接返回空池，使**定时自动选股在纯券商环境下恒不可用**
        # （system_jobs 默认就是 all 池）—— 明明连着券商却永远选不出票。
        hb_all = _hub(hub)
        for sec in ("沪深A股", "沪A", "深A"):
            try:
                codes_b, src_b = await hb_all.get_sector_stocks(sec)
            except Exception as exc:  # noqa: BLE001
                log.warning("券商全市场兜底失败 %s: %s", sec, exc)
                continue
            if codes_b:
                return {"codes": codes_b, "names": _fill_names(codes_b),
                        "provider_used": src_b or "broker",
                        "as_of": None, "degraded": False,
                        "degraded_reason": f"local_empty_fallback:{sec}"}
        # ★ 最后一层兜底：本地**有日线**的标的集合。
        #
        # 为什么必须有这一层：券商适配器**没有 get_stock_list 能力**（只有
        # get_sector_stocks），所以 ``local_stock_list`` 在纯券商环境下没有任何
        # 东西会去写它 —— 它恒为空。而「本地日线」恰恰是同步任务真正落库的东西。
        #
        # 实测（2026-09-20 真实库，未连券商）：``local_stock_list`` = 0 行，
        # 但 ``local_bars`` 有 5209 只 / 62 万根日线（截至 20260918）。
        # 此时选股直接回「股票池为空」⇒ **有日线却选不了股，整条选股链路等于没生效**。
        #
        # 而「有日线的标的集合」本身就是合法且更可靠的股票池：选股要算的就是这些
        # 日线，没有日线的标的根本进不了计算。名称留空（不伪造），as_of 如实给出
        # 本地最新交易日。
        try:
            latest = st.latest_bar_dt() or ""
            since = ""
            if latest and len(latest) == 8:
                from datetime import datetime as _dt
                from datetime import timedelta as _td
                # 45 天窗口：全量回补会把**早已退市**的标的留在 local_bars 里，
                # 不加时间窗就会把它们当成当前股票池。
                since = (_dt.strptime(latest, "%Y%m%d") - _td(days=45)).strftime("%Y%m%d")
            codes_l = (st.codes_with_bars(since=since)
                       if hasattr(st, "codes_with_bars") else [])
        except Exception as exc:  # noqa: BLE001
            log.warning("本地日线股票池兜底失败：%s", exc)
            codes_l = []
        if codes_l:
            # 名称由本地名称表补全（见 `_fill_names`）——此前留空导致界面「名称」列
            # 整列空白，且 ST/退市过滤静默失效。
            names_l = _fill_names(codes_l)
            return {"codes": codes_l, "names": names_l, "provider_used": "local_bars",
                    "as_of": latest or None, "degraded": True,
                    "degraded_reason": (
                        f"local_empty_fallback:local_bars（股票池由本地日线标的推导，"
                        f"{len(codes_l)} 只，截至 {latest or '未知'}，"
                        f"名称覆盖 {len(names_l)}/{len(codes_l)}；"
                        f"如需完整全市场清单请连接券商或同步股票列表）")}
        return {"codes": [], "names": {}, "provider_used": "local",
                "as_of": None, "degraded": True,
                "degraded_reason": "local_empty_and_no_broker_sector"}

    if spec.kind == "custom":
        codes = [str(c) for c in (spec.codes or [])]
        names: Dict[str, str] = {}
        try:
            rows = st.get_stock_list()
            names = {r["code"]: r.get("name", "") or "" for r in rows}
        except Exception:  # noqa: BLE001
            pass
        return {"codes": codes, "names": _fill_names(codes, names),
                "provider_used": "local",
                "as_of": None, "degraded": False, "degraded_reason": None}

    if spec.kind == "saved_board":
        name = spec.board or spec.value
        if not name:
            return _empty("saved_board:missing_name")
        rows = []
        for kind in (f"screen:{name}", name):
            try:
                rows = st.get_boards(kind)
                if rows:
                    break
            except Exception:  # noqa: BLE001
                continue
        if not rows:
            return {"codes": [], "names": {}, "provider_used": "local",
                    "as_of": None, "degraded": True,
                    "degraded_reason": f"saved_board_not_found:{name}"}
        codes = [r["code"] for r in rows]
        names = _fill_names(codes, {r["code"]: r.get("name", "") or "" for r in rows})
        return {"codes": codes, "names": names, "provider_used": "local",
                "as_of": None, "degraded": False, "degraded_reason": None}

    if spec.kind == "screen_result":
        # 需要先前选股结果 id；本期与 saved_board 等价（结果存为板块后引用）。
        return _empty("screen_result:unsupported_in_this_build")

    # —— sector / index / holdings：在线源（按链降级）——
    if spec.kind == "sector":
        value = spec.value
        if not value:
            return _empty("sector:missing_value")
        try:
            res, src = await hb.get_board_constituents(value)
        except Exception as exc:  # noqa: BLE001
            log.warning("板块成分获取失败 %s: %s", value, exc)
            return _empty("sector:source_error")
        if not res or not res.get("items"):
            # 券商兜底：在线板块源不可用时，**券商其实能提供成分股代码**
            # （实测「沪深A股」返回 5224 只，见 _BoundBrokerSource.get_sector_stocks）。
            # 缺了这一步，纯券商环境（无网络补充源）下选股池恒为空 ——
            # 而同一份能力在「K 线同步」那条路径上是可用的。
            try:
                codes_b, src_b = await hb.get_sector_stocks(value)
            except Exception as exc:  # noqa: BLE001
                log.warning("券商板块成分兜底失败 %s: %s", value, exc)
                codes_b, src_b = None, None
            if codes_b:
                return {"codes": codes_b, "names": _fill_names(codes_b),
                        "provider_used": src_b or "broker",
                        "as_of": None, "degraded": False, "degraded_reason": None}
            return _empty("sector:empty")
        items = res["items"]
        codes = [it["code"] for it in items]
        names = {it["code"]: it.get("name", "") or "" for it in items}
        return {"codes": codes, "names": names, "provider_used": src or "online",
                "as_of": None, "degraded": False, "degraded_reason": None}

    if spec.kind == "index":
        value = spec.value
        if not value:
            return _empty("index:missing_value")
        try:
            res, src = await hb.get_index_constituents(value)
        except Exception as exc:  # noqa: BLE001
            log.warning("指数成分获取失败 %s: %s", value, exc)
            return _empty("index:source_error")
        if not res:
            return _empty("index:empty")
        if isinstance(res, dict) and "items" in res:
            items = res["items"]
            codes = [it["code"] for it in items]
            names = {it["code"]: it.get("name", "") or "" for it in items}
        elif isinstance(res, dict):
            codes = list(res.keys())
            names = {k: (v if isinstance(v, str) else "") for k, v in res.items()}
        elif isinstance(res, list):
            codes = [x["code"] if isinstance(x, dict) else str(x) for x in res]
            names = _fill_names(codes)
        else:
            return _empty("index:bad_format")
        if not codes:
            return _empty("index:empty")
        return {"codes": codes, "names": names, "provider_used": src or "online",
                "as_of": None, "degraded": False, "degraded_reason": None}

    if spec.kind == "holdings":
        # 持仓需券商账户态；本期无 broker 时明确降级，绝不伪造持仓。
        return _empty("holdings:no_broker_connection")

    return _empty(f"unknown:{spec.kind}")


__all__ = ["UniverseSpec", "resolve_universe", "UNIVERSE_KINDS"]

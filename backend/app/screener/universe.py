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
        return {"codes": codes, "names": names, "provider_used": "local",
                "as_of": None, "degraded": len(codes) == 0,
                "degraded_reason": ("local_empty" if not codes else None)}

    if spec.kind == "custom":
        codes = [str(c) for c in (spec.codes or [])]
        names: Dict[str, str] = {}
        try:
            rows = st.get_stock_list()
            names = {r["code"]: r.get("name", "") or "" for r in rows}
        except Exception:  # noqa: BLE001
            pass
        return {"codes": codes, "names": names, "provider_used": "local",
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
        names = {r["code"]: r.get("name", "") or "" for r in rows}
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
            names = {c: "" for c in codes}
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

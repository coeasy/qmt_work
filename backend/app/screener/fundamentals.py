"""Phase 4：基本面因子（字段级溯源，D-J §J.6 / F11）。

支持字段：``pe / pb / ps / roe / revenue_yoy / profit_yoy / turnover / mktcap /
float_mktcap / dividend_yield``。

铁律（与 D-J 一致）：
- 字段不可得 → 写 ``None`` + 记入 ``missing_codes``，**绝不填 0**（修 ``nl_screen.py`` 的
  unsupported 误填 0 问题）；
- 同源一致性（J-4）：一次请求只用一个源供给全部字段；
- 许可证过滤（J-5）：商用模式下跳过 ``commercial_ok=False`` 的源。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

log = logging.getLogger("qmt_work.screener.fundamentals")

FUNDAMENTAL_FIELDS: tuple[str, ...] = (
    "pe", "pb", "ps", "roe", "revenue_yoy", "profit_yoy",
    "turnover", "mktcap", "float_mktcap", "dividend_yield",
)


def _commercial_mode() -> bool:
    try:
        from datasource.registry import get_manager
        return get_manager()._commercial_mode
    except Exception:  # noqa: BLE001
        import os
        return os.environ.get("QMT_COMMERCIAL") == "1"


async def fetch_fundamentals(
    codes: List[str],
    *,
    policy_str: str = "auto",
    fields: Optional[List[str]] = None,
    hub=None,
) -> dict:
    """取多标的多字段基本面，返回字段级溯源结构。

    无可用源 / 源无实现 → 所有字段 ``None`` + ``provenance`` 标 None，绝不报错、绝不填 0。
    """
    codes = [str(c) for c in (codes or [])]
    wanted = [f for f in (fields or list(FUNDAMENTAL_FIELDS)) if f in FUNDAMENTAL_FIELDS]
    if not wanted:
        wanted = list(FUNDAMENTAL_FIELDS)

    out_fields: Dict[str, Dict[str, Optional[float]]] = {f: {} for f in wanted}
    provenance: Dict[str, Optional[str]] = {f: None for f in wanted}
    missing: Dict[str, List[str]] = {f: list(codes) for f in wanted}

    if not codes:
        return {"fields": out_fields, "provenance": provenance, "missing_codes": missing}

    manager = hub if hub is not None else _get_manager()
    if manager is None:
        return {"fields": out_fields, "provenance": provenance, "missing_codes": missing}

    # 解析 fundamental 能力链（含商用过滤）
    chain = _resolve_fundamental_chain(policy_str, manager)
    for src_name in chain:
        src = manager._plugins.get(src_name) if hasattr(manager, "_plugins") else None
        if src is None or not hasattr(src, "get_fundamentals"):
            continue
        try:
            rows = await src.get_fundamentals(codes)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            log.warning("基本面源 %s 取数失败: %s", src_name, exc)
            continue
        if not rows:
            continue
        # 同源一次性映射全部字段
        for field_name in wanted:
            prov = False
            for c in codes:
                row = rows.get(c) if isinstance(rows, dict) else None
                if row is None and isinstance(rows, list):
                    row = next((r for r in rows if r.get("code") == c), None)
                if not isinstance(row, dict):
                    continue
                val = row.get(field_name)
                if val is None:
                    continue
                try:
                    out_fields[field_name][c] = float(val)
                    if c in missing[field_name]:
                        missing[field_name].remove(c)
                    prov = True
                except (TypeError, ValueError):
                    continue
            if prov:
                provenance[field_name] = src_name
        # 任一字段拿到数据即可停止（同源一致性）
        if any(provenance.values()):
            break

    return {"fields": out_fields, "provenance": provenance, "missing_codes": missing}


def _get_manager():
    try:
        from datasource.registry import get_manager
        return get_manager()
    except Exception:  # noqa: BLE001
        return None


def _resolve_fundamental_chain(policy_str: str, manager) -> List[str]:
    try:
        from datasource.providers import provider_catalog
        registered = set(manager.list_sources())
        return list(provider_catalog.resolve_chain(
            "fundamental", commercial_mode=_commercial_mode(), registered=registered))
    except Exception:  # noqa: BLE001
        return []


__all__ = ["fetch_fundamentals", "FUNDAMENTAL_FIELDS"]

"""G7 条件选股 · 条件表达式模型与求值。

条件表达为**可序列化嵌套 JSON**（经 REST `conditions` 字符串参数传入，签名
auto-safe，自动暴露 MCP）：

- 叶子：
  - ``{"indicator": {name, params, output, op, value, window}}``
    指标条件：经统一指标引擎（G2）计算后取 ``output`` 列在 ``window`` 处的值比较。
    ``params`` 用路由口径：``win``（ma/ema/rsi 等窗口，映射到 period）/ ``n`` / ``m``。
  - ``{"field": {name, op, value, window}}``
    原始字段条件：name ∈ close/open/high/low/volume。
  - ``window``：bar 偏移，-1 = 最新一根（默认），-2 = 次新…
- 组合：``{"and": [..]}`` / ``{"or": [..]}`` 可嵌套。
- ``op`` ∈ gt/gte/lt/lte/eq/ne。

铁律：窗口处为 null（数据不足/源缺字段）→ 该叶子**不命中**（绝不估算填充）；
整个条件树空 / 非法结构 → ValueError（路由 400）。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from app.indicators import calc, get_indicator

log = logging.getLogger("qmt_work.screener.conditions")

_OPS = {"gt", "gte", "lt", "lte", "eq", "ne"}
_FIELD_NAMES = {"close", "open", "high", "low", "volume"}


def _cmp(a: float, op: str, b: float) -> bool:
    if op == "gt":
        return a > b
    if op == "gte":
        return a >= b
    if op == "lt":
        return a < b
    if op == "lte":
        return a <= b
    if op == "eq":
        return abs(a - b) < 1e-9
    if op == "ne":
        return abs(a - b) >= 1e-9
    raise ValueError(f"非法操作符：{op}（可选 {sorted(_OPS)}）")


def _windowed(arr: List, window: int) -> Optional[float]:
    """取数组 window 偏移处的值；越界 / null 一律 None。"""
    n = len(arr)
    idx = window if window >= 0 else n + window
    if idx < 0 or idx >= n:
        return None
    v = arr[idx]
    return float(v) if v is not None else None


def _resolve_ind_params(spec, params: Dict[str, Any]) -> Dict[str, Any]:
    """路由口径参数（win/n/m）→ 指标 spec 参数（period/n/m...）。"""
    resolved: Dict[str, Any] = {}
    for p in spec.params:
        if p.name == "period" and "win" in params:
            resolved["period"] = params["win"]
        elif p.name in params:
            resolved[p.name] = params[p.name]
    return resolved


def _ind_cache_key(name: str, params: Dict[str, Any]) -> tuple:
    """指标计算的去重键：同名同参 → 单次计算可复用（D-J §J.10 指标需求去重）。"""
    return (name, tuple(sorted((k, str(v)) for k, v in (params or {}).items())))


def _eval_indicator(cond: dict, bars: list, cache: dict) -> Optional[float]:
    ind = cond["indicator"]
    name = ind.get("name")
    spec = get_indicator(name)                       # KeyError → 路由 400
    params = _resolve_ind_params(spec, ind.get("params") or {})
    key = _ind_cache_key(name, params)
    res = cache.get(key)
    if res is None:
        res = calc(name, bars, **params)
        cache[key] = res
    out_key = ind.get("output") or (spec.outputs[0] if spec.outputs else None)
    arr = res["outputs"].get(out_key) if out_key else None
    if arr is None:
        raise ValueError(f"指标 {name} 无输出列 {out_key}（可选 {spec.outputs}）")
    return _windowed(arr, int(ind.get("window", -1)))


def _operand_value(operand: dict, bars: list, cache: dict) -> Optional[float]:
    """取操作数在 window 处的值（compare 叶子用）。kind ∈ field/indicator。"""
    kind = operand.get("kind")
    if kind == "field":
        name = operand.get("name")
        if name not in _FIELD_NAMES:
            raise ValueError(f"未知字段条件：{name}（可选 {sorted(_FIELD_NAMES)}）")
        arr = [float(getattr(b, name)) if getattr(b, name, None) is not None else None
               for b in bars]
        return _windowed(arr, int(operand.get("window", -1)))
    if kind == "indicator":
        spec = get_indicator(operand["name"])
        params = _resolve_ind_params(spec, operand.get("params") or {})
        key = _ind_cache_key(operand["name"], params)
        res = cache.get(key)
        if res is None:
            res = calc(operand["name"], bars, **params)
            cache[key] = res
        out = operand.get("output") or (spec.outputs[0] if spec.outputs else None)
        arr = res["outputs"].get(out) if out else None
        if arr is None:
            raise ValueError(f"指标 {operand['name']} 无输出列 {out}（可选 {spec.outputs}）")
        return _windowed(arr, int(operand.get("window", -1)))
    raise ValueError(f"未知操作数类型：{kind}")


def _eval_leaf(cond: dict, bars: list, cache: dict) -> bool:
    if "compare" in cond:
        c = cond["compare"]
        op = c.get("op")
        if op not in _OPS:
            raise ValueError(f"非法操作符：{op}（可选 {sorted(_OPS)}）")
        lv = _operand_value(c["left"], bars, cache)
        rv = _operand_value(c["right"], bars, cache)
        if lv is None or rv is None:
            return False
        return _cmp(lv, op, rv)
    if "indicator" in cond:
        val = _eval_indicator(cond, bars, cache)
        leaf: dict = cond["indicator"]
    elif "field" in cond:
        f = cond["field"]
        name = f.get("name")
        if name not in _FIELD_NAMES:
            raise ValueError(f"未知字段条件：{name}（可选 {sorted(_FIELD_NAMES)}）")
        arr = [float(getattr(b, name)) if getattr(b, name, None) is not None else None
               for b in bars]
        val = _windowed(arr, int(f.get("window", -1)))
        leaf = f
    else:
        raise ValueError("条件叶子须为 {indicator:...} / {field:...} / {compare:...}")
    op = leaf.get("op")
    if op not in _OPS:
        raise ValueError(f"非法操作符：{op}（可选 {sorted(_OPS)}）")
    value = leaf.get("value")
    if val is None or value is None:
        return False                                  # 窗口无值 → 不命中（不估算）
    return _cmp(val, op, float(value))


def evaluate(conditions: dict, bars: list) -> Tuple[bool, int, int]:
    """求值条件树 → (是否命中, 命中的叶子数, 叶子总数)。

    内部共享一个指标计算缓存：同一 (指标, 参数) 在一次求值内只计算一次（D-J §J.10 去重）。
    """
    def _rec(node: dict, cache: dict):
        if "and" in node:
            subs = [_rec(c, cache) for c in node["and"]]
            hits = sum(s[0] for s in subs)
            return all(s[0] for s in subs), hits, sum(s[2] for s in subs)
        if "or" in node:
            subs = [_rec(c, cache) for c in node["or"]]
            hits = sum(s[0] for s in subs)
            return any(s[0] for s in subs), hits, sum(s[2] for s in subs)
        if "not" in node:                       # G2-4 DSL 支持 NOT（叶子计数取反）
            hit, score, total = _rec(node["not"], cache)
            return (not hit), (total - score), total
        matched = _eval_leaf(node, bars, cache)
        return matched, (1 if matched else 0), 1

    if not isinstance(conditions, dict) or not conditions:
        raise ValueError("conditions 为空：须为 {and:[..]}/{or:[..]} 或叶子条件对象")
    hit, score, total = _rec(conditions, {})
    return hit, score, total


__all__ = ["evaluate", "_OPS", "_FIELD_NAMES"]

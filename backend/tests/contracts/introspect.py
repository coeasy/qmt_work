"""Tier-0 契约内省（Phase 1 · 先锁后改）。

从**真实代码**提取契约面，供 ``scripts/gen_contracts.py`` 落盘快照、
``tests/test_contracts_baseline.py`` 做基线回归。全部实时内省，不手抄：

- REST 端点   ：FastAPI app.routes（/api/v1/*）→ {"<METHOD> <path>": tag}
- MCP 工具    ：build_mcp(risk) 实际注册表 → 排序工具名清单
- QMT 契约    ：order_status SSOT 映射表 + place_order 签名/响应键（AST 解析源码）
- 选股契约    ：/market/screen(/expr) 查询参数（路由 dependant 内省）+ §J.10 响应/溯源键
- WS 事件词汇 ：AST 扫描 broadcast/_notify 调用点 → 频道 → "type" 字面量集合
"""
from __future__ import annotations

import ast
import functools
import os
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]

# 无头内省环境兜底：settings 要求主密钥存在（仅内省用，不参与真实鉴权）
os.environ.setdefault("QMT_API_KEY", "contract-introspect-key")

_WS_SCAN_ROOTS = ("sync", "app", "gateway", "engines", "xtquant_client")
_EMIT_FUNC_NAMES = {"broadcast", "_notify"}


# ---------------------------------------------------------------------------
# REST 端点
# ---------------------------------------------------------------------------
def _iter_routes(fastapi_app):
    """展平 app.routes 并拼接前缀链：本 FastAPI 版本把 include_router 收进
    _IncludedRouter（真实路由在其 original_router.routes，prefix 存于
    include_context.prefix），需递归下钻并逐层拼接前缀。yield (full_path, route)。"""
    stack = [("", r) for r in fastapi_app.routes]
    while stack:
        prefix, r = stack.pop(0)
        sub = getattr(r, "original_router", None)
        if sub is not None:
            ctx = getattr(r, "include_context", None)
            p = getattr(ctx, "prefix", "") or ""
            stack.extend((prefix + p, x) for x in (getattr(sub, "routes", []) or []))
            continue
        yield prefix + getattr(r, "path", ""), r


@functools.lru_cache(maxsize=1)
def rest_endpoints() -> dict:
    from app.main import app as fastapi_app  # create_app()：完整装配（含全部路由）

    out: dict = {}
    for path, route in _iter_routes(fastapi_app):
        methods = getattr(route, "methods", None)
        if not path.startswith("/api/v1") or not methods:
            continue
        tag = (getattr(route, "tags", None) or ["ops"])[0]
        for m in sorted(methods):
            if m in ("HEAD", "OPTIONS"):
                continue
            out[f"{m} {path}"] = tag
    return dict(sorted(out.items()))


# ---------------------------------------------------------------------------
# MCP 工具
# ---------------------------------------------------------------------------
@functools.lru_cache(maxsize=1)
def mcp_tools() -> list:
    from core.config import settings
    from gateway.risk import RiskManager
    from mcp_server import build_mcp

    risk = RiskManager(
        max_amount=settings.risk_max_amount, min_qty=settings.risk_min_qty,
        max_position_ratio=settings.risk_max_position_ratio,
        max_single_position_ratio=settings.risk_max_single_position_ratio,
        max_orders_per_min=settings.risk_max_orders_per_min,
        daily_amount_limit=settings.risk_daily_amount_limit,
        daily_loss_limit=settings.risk_daily_loss_limit,
        per_code_daily_orders=settings.risk_per_code_daily_orders,
        price_deviation_pct=settings.risk_price_deviation_pct,
        symbol_allow=settings.risk_symbol_allow, symbol_deny=settings.risk_symbol_deny,
    )
    mcp = build_mcp(risk)
    return sorted(mcp._tool_manager._tools.keys())


# ---------------------------------------------------------------------------
# QMT 契约：状态码 SSOT + 下单签名/响应 + 价格类型
# ---------------------------------------------------------------------------
def _place_order_ast_facts() -> dict:
    """AST 解析 xtp/trading.py 的 place_order：参数名 + return 字典常量键。"""
    import xtquant_client.xtp.trading as t

    src = Path(t.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    params: list = []
    ret_keys: list = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "place_order":
            params = [a.arg for a in node.args.args]
            for sub in ast.walk(node):
                if (isinstance(sub, ast.Return) and isinstance(sub.value, ast.Dict)
                        and sub.value.keys):
                    keys = [k.value for k in sub.value.keys
                            if isinstance(k, ast.Constant) and isinstance(k.value, str)]
                    if keys:
                        ret_keys = keys
            break
    return {"params": params, "response_keys": sorted(ret_keys)}


@functools.lru_cache(maxsize=1)
def qmt_contracts() -> dict:
    from xtquant_client import order_status as osm

    po = _place_order_ast_facts()
    return {
        # 订单状态词汇表（唯一真相来源，改动必须显式更新快照）
        "std_states": sorted({osm.PENDING, osm.PARTIAL, osm.FILLED,
                              osm.CANCELLED, osm.REJECTED, osm.UNKNOWN}),
        "xtp_int_status": {str(k): v for k, v in sorted(osm._XTP_INT_STATUS.items())},
        "raw_to_std": dict(sorted(osm._RAW_TO_STD.items())),
        "terminal_states": sorted(osm._TERMINAL),
        "active_states": sorted(osm._OPEN),
        # 下单接口契约
        "place_order_params": po["params"],
        "place_order_response_keys": po["response_keys"],
        "place_order_submit_status": ["submitted", "unknown"],
        # 价格类型契约（现状：二元；扩展为多档时须更新快照 + test_price_type_matrix）
        "price_type_accepted": ["limit", "market"],
        "price_type_default": "limit",
        "price_type_xtp_mapping": {"limit": "FIX_PRICE(11)", "market": "LATEST_PRICE(5)"},
        "limit_requires_positive_price": True,
        "market_requires_protect_price": True,
        # 撤单判定契约（P0-12：返回码 0=成功 / -1=失败，杜绝假成功）
        "cancel_verdict": {"ok_on_return_code": 0, "fail_on_return_code": -1},
    }


# ---------------------------------------------------------------------------
# 选股契约（§J.10）
# ---------------------------------------------------------------------------
_SCREEN_RESPONSE_KEYS = (
    "count", "total_scanned", "elapsed_ms", "sort_by", "conditions", "results",
    "provenance", "degraded", "degraded_reason", "fallback_tried",
    "provider_policy_version", "dataset_snapshot_id", "fundamentals",
)
_SCREEN_PROVENANCE_KEYS = (
    # BarsBatchReport.to_provenance()
    "provider_used", "source_policy", "degraded", "degraded_reason",
    "fallback_tried", "as_of", "local_fallback", "provider_policy_version",
    "count_online", "count_local", "count_total",
    # engine.scan_async 补充
    "universe", "universe_provider", "universe_degraded", "prefilter",
    "screening_mode",
)


@functools.lru_cache(maxsize=1)
def screen_contract() -> dict:
    from app.main import app as fastapi_app

    params: dict = {}
    for path, route in _iter_routes(fastapi_app):
        if path in ("/api/v1/market/screen", "/api/v1/market/screen/expr"):
            qp = sorted(p.name for p in route.dependant.query_params)
            params["screen" if path.endswith("/screen") else "expr"] = qp
    return {
        "screen_query_params": params.get("screen", []),
        "expr_query_params": params.get("expr", []),
        "response_keys": list(_SCREEN_RESPONSE_KEYS),
        "provenance_keys": list(_SCREEN_PROVENANCE_KEYS),
        "source_policy_values": ["auto", "prefer_qmt", "qmt_only", "local_only", "explicit:<id>"],
        "universe_kinds": ["all", "sector", "index", "custom", "saved_board",
                           "screen_result", "holdings"],
        "empty_semantics": "source_all_failed=503; zero_hit=200&count=0",
    }


# ---------------------------------------------------------------------------
# WS 事件词汇（AST 扫描 emit 调用点）
# ---------------------------------------------------------------------------
def _emit_calls(tree: ast.AST):
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = getattr(fn, "attr", None) or getattr(fn, "id", None)
        if name in _EMIT_FUNC_NAMES:
            yield node


def ws_events() -> dict:
    channels: dict = {}
    for root in _WS_SCAN_ROOTS:
        base = BACKEND_ROOT / root
        if not base.exists():
            continue
        for py in base.rglob("*.py"):
            try:
                tree = ast.parse(py.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):
                continue
            for call in _emit_calls(tree):
                if not call.args:
                    continue
                a0 = call.args[0]
                if not (isinstance(a0, ast.Constant) and isinstance(a0.value, str)):
                    continue  # 频道为变量（如 quote_bus.publish(code,…)）不属事件词汇
                ch = a0.value
                types = channels.setdefault(ch, set())
                for sub in ast.walk(call):
                    if (isinstance(sub, ast.Dict) and sub.keys
                            and isinstance(sub.keys[0], ast.Constant)
                            and sub.keys[0].value == "type"
                            and isinstance(sub.values[0], ast.Constant)
                            and isinstance(sub.values[0].value, str)):
                        types.add(sub.values[0].value)
    return {ch: sorted(ts) for ch, ts in sorted(channels.items())}


def collect_all() -> dict:
    return {
        "rest_endpoints": rest_endpoints(),
        "mcp_tools": mcp_tools(),
        "qmt_contracts": qmt_contracts(),
        "screen_contract": screen_contract(),
        "ws_events": ws_events(),
    }


__all__ = ["rest_endpoints", "mcp_tools", "qmt_contracts", "screen_contract",
           "ws_events", "collect_all", "BACKEND_ROOT"]

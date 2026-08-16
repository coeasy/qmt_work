"""阶段 5 默认工具集：Agent 与 MCP 共用同一注册表（tool schema 解耦）。

全部走真实券商 / 运行期状态，无 mock：
- read 类：运行时配置、券商档案、连接状态、账户、行情快照；
- trade 类：下单（经统一风控，与「手动交易」页同源路径，绝不绕过风控）。

未连接券商时工具返回明确错误（不编造、不回退假数据）。
"""
from __future__ import annotations

from app.config import settings
from app.state import state
from xtquant_client.base import BrokerError
from xtquant_client.registry import registry


def _active_bridge(conn_id=None):
    """返回指定/活跃 bridge；无连接抛 BrokerError（core 会转为工具错误文本）。"""
    b = state.broker_manager.bridge(conn_id)
    if b is None:
        raise BrokerError("未连接任何券商客户端：请到「券商连接」页添加并连接券商。")
    return b


async def _runtime_config(args: dict) -> dict:
    rc = state.runtime_config
    if rc is None:
        return {"error": "运行时配置中心未初始化"}
    return {"config": rc.all()}


async def _broker_profiles(args: dict) -> dict:
    return {"profiles": registry.list_profiles_v2()}


async def _broker_status(args: dict) -> dict:
    bm = state.broker_manager
    return {"connections": bm.status_list(), "active": bm.active_bridge() is not None}


async def _query_account(args: dict) -> dict:
    b = _active_bridge(args.get("conn_id") or None)
    cash = await b.call(b.gateway.get_cash)
    pos = await b.call(b.gateway.get_positions)
    return {"connected": b.gateway.is_connected(), "cash": cash, "positions": pos}


async def _get_quote(args: dict) -> dict:
    code = str(args.get("code", "")).strip().upper()
    if not code:
        return {"error": "code 必填"}
    b = _active_bridge(args.get("conn_id") or None)
    q = await b.call(b.gateway.get_quote, code)
    return {"code": code, "quote": q}


async def _submit_order(args: dict) -> dict:
    """经统一风控下单（与「手动交易」页同源路径，绝不绕过风控）。"""
    code = str(args.get("code", "")).strip().upper()
    direction = (args.get("direction") or "buy").lower()
    volume = int(args.get("volume", 0) or 0)
    price = float(args.get("price", 0) or 0)
    price_type = args.get("price_type", "limit")
    if not code or direction not in ("buy", "sell") or volume <= 0:
        return {"error": "参数非法：需 code / buy|sell / volume>0"}
    b = _active_bridge(args.get("conn_id") or None)
    okc, reason = state.risk.check_order(
        code, price if price > 0 else 100.0, volume, direction)
    if not okc:
        return {"error": f"风控拒绝：{reason}"}
    res = await b.call(b.gateway.place_order, code, direction, price_type,
                       price, volume, "agent", args.get("remark", ""))
    if isinstance(res, dict) and res.get("code", 0) != 0:
        return {"error": res.get("message", "下单失败"), "detail": res}
    return {"order": res}


def build_default_registry():
    """构建默认工具注册表（真实数据访问，无 mock）。"""
    from .tools import ToolRegistry

    reg = ToolRegistry()
    reg.register(
        "get_runtime_config",
        "读取运行时配置中心的全部引擎参数（热更新值，如同步批处理窗口、风控巡检间隔等）。",
        {"type": "object", "properties": {}},
        _runtime_config, permission="read")
    reg.register(
        "list_broker_profiles",
        "列出平台支持的券商档案（国金/华鑫/银河/同花顺/恒生PTrade/掘金…），含适配器与能力标记。",
        {"type": "object", "properties": {}},
        _broker_profiles, permission="read")
    reg.register(
        "broker_status",
        "查询当前券商连接状态：各连接是否在线、活跃连接是哪个。未连接时返回 connected=false。",
        {"type": "object", "properties": {}},
        _broker_status, permission="read")
    reg.register(
        "query_account",
        "查询指定/活跃连接的账户资金与持仓（需连接券商）。",
        {"type": "object", "properties": {
            "conn_id": {"type": "string", "description": "可选，省略用活跃连接"}}},
        _query_account, permission="read")
    reg.register(
        "get_quote",
        "查询单只标的实时报价（需连接券商，真实行情，非缓存假数据）。",
        {"type": "object", "properties": {
            "code": {"type": "string", "description": "标的代码，如 600519.SH"},
            "conn_id": {"type": "string", "description": "可选，省略用活跃连接"}}},
        _get_quote, permission="read")
    reg.register(
        "submit_order",
        "经统一风控下单（与「手动交易」页同源路径，绝不绕过风控）。下单前请先与用户确认方向、数量、价格。",
        {"type": "object", "properties": {
            "code": {"type": "string", "description": "标的代码，如 600519.SH"},
            "direction": {"type": "string", "enum": ["buy", "sell"]},
            "volume": {"type": "integer", "description": "股数（100 的整数倍）"},
            "price": {"type": "number", "description": "限价单价格（市价单可填 0）"},
            "price_type": {"type": "string", "description": "limit（限价）/ market（市价）"},
            "conn_id": {"type": "string", "description": "可选，省略用活跃连接"}}},
        _submit_order, permission="trade")
    return reg

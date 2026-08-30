#!/usr/bin/env python
"""G3-5 · 能力暴露漂移门禁（CI 用）。

比对「agent_visible 的 GET 端点」与「已注册 MCP tool」：
若任一应自动暴露的端点没有对应 tool，则视为漂移（新增路由后自动暴露失效），
进程以非 0 退出，阻断 CI。

用法：
    python scripts/check_capability_drift.py
"""
from __future__ import annotations

import os
import sys

# 确保 backend/ 在导入路径中（脚本位于 backend/scripts/ 下）。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.capabilities import agent_visible_reads, tool_name_for
from app.config import settings
from gateway.risk import RiskManager
from mcp_server import build_mcp


def main() -> int:
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
    registered = set(mcp._tool_manager._tools.keys())

    missing = []
    for cap in agent_visible_reads():
        name = tool_name_for(cap)
        if name not in registered:
            missing.append((cap.path, name))

    total = len(registered)
    print(f"[capability-drift] MCP tools 总数: {total}")
    print(f"[capability-drift] agent_visible GET 端点: {len(agent_visible_reads())}")
    if missing:
        print(f"[capability-drift] FAIL: {len(missing)} 个应暴露端点缺少 MCP tool:")
        for path, name in missing:
            print(f"    {path} -> 期望 tool: {name}")
        return 1
    print("[capability-drift] PASS: 所有 agent_visible 端点均已暴露为 MCP tool")
    return 0


if __name__ == "__main__":
    sys.exit(main())

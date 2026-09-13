"""Tier-0 契约基线回归（Phase 1 DoD：silent removal = 0）。

语义：
- **基线 ⊆ 现网**：删除/改名任何已锁定的 REST 端点、MCP 工具、WS 频道/事件、
  选股参数、QMT 状态词汇 → 红灯；
- QMT 状态 SSOT（order_status.py 映射表）用**严格相等**——改词汇必须显式刷新基线；
- 新增契约不阻塞（打印提示，运行 ``scripts/gen_contracts.py`` 刷新基线后提交）。

基线缺失时 skip（而非 fail）：新环境先运行 ``python scripts/gen_contracts.py``。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from contracts.introspect import (
    mcp_tools,
    qmt_contracts,
    rest_endpoints,
    screen_contract,
    ws_events,
)

C_DIR = Path(__file__).parent / "contracts"


def _load(name: str):
    p = C_DIR / f"{name}.json"
    if not p.exists():
        pytest.skip(f"契约基线缺失：{name}.json（先运行 scripts/gen_contracts.py）")
    return json.loads(p.read_text(encoding="utf-8"))


def test_rest_endpoints_no_silent_removal():
    base = _load("rest_endpoints")
    live = rest_endpoints()
    missing = sorted(set(base) - set(live))
    assert not missing, f"REST 端点被静默删除/改名：{missing}"
    added = sorted(set(live) - set(base))
    assert not added or True  # 新增放行，但保证测试输出可见
    if added:
        print(f"[contracts] REST 新增 {len(added)} 个（请刷新基线）：{added[:8]}…")


def test_mcp_tools_no_silent_removal():
    base = _load("mcp_tools")
    live = mcp_tools()
    missing = sorted(set(base) - set(live))
    assert not missing, f"MCP 工具被静默删除/改名：{missing}"
    added = sorted(set(live) - set(base))
    if added:
        print(f"[contracts] MCP 新增 {len(added)} 个（请刷新基线）：{added[:8]}…")


def test_qmt_status_ssot_strict():
    """状态码词汇表是唯一真相来源：任何变化都必须显式刷新快照（严格相等）。"""
    base = _load("qmt_contracts")
    live = qmt_contracts()
    for section in ("std_states", "xtp_int_status", "raw_to_std", "terminal_states",
                    "active_states", "place_order_params", "place_order_response_keys",
                    "price_type_accepted", "price_type_xtp_mapping", "cancel_verdict"):
        assert base.get(section) == live.get(section), (
            f"QMT 契约段「{section}」发生变化——如是刻意变更，"
            "请运行 scripts/gen_contracts.py 刷新基线并同步全部消费方")


def test_screen_contract_no_silent_removal():
    base = _load("screen_contract")
    live = screen_contract()
    for key in ("screen_query_params", "expr_query_params",
                "response_keys", "provenance_keys"):
        removed = set(base.get(key, [])) - set(live.get(key, []))
        assert not removed, f"选股契约「{key}」被静默删除：{sorted(removed)}"


def test_ws_events_no_silent_removal():
    base = _load("ws_events")
    live = ws_events()
    removed_channels = sorted(set(base) - set(live))
    assert not removed_channels, f"WS 频道被静默删除：{removed_channels}"
    for ch, types in base.items():
        removed_types = set(types) - set(live.get(ch, []))
        assert not removed_types, f"WS 频道「{ch}」事件类型被静默删除：{sorted(removed_types)}"

"""``GET /account/grid`` 的「跨账户资产汇总」契约测试。

为什么需要它
------------
仪表盘原先吃 ``GET /account/status``（只看**第一个可用连接**的账户），然后用
``positions.reduce(...)`` 自己把持仓市值与盈亏加出来；而「多账户网格」页吃
``GET /account/grid``（跨账户汇总）。两处口径不同 ⇒ **同一时刻两页给出两个总资产**。

锁定五条不变量（错了会出真事）：

1. **持仓行必须富化**（现价/成本/浮动盈亏/中文名）：券商 ``get_positions`` 只回
   ``code/name/volume/avail/cost/market_value``，不富化则看板的「浮动盈亏」恒为 0，
   而持仓页却有值 —— 同一份数据两个结论；
2. **``assets`` 为 0 时退化为 ``cash + 持仓市值``**：实测某账户 ``get_account().assets``
   恒为 0 但持仓市值 179.9，不退化就显示「总资产 0.00 元」而旁边列着一只持仓；
3. **每个账户的持仓只取一次**：原先 ``_account_row`` 与汇总循环各取一次，同一份数据
   打了两遍券商 IPC；
4. **0 与「不知道」必须可区分**：``profit`` / ``total_profit`` 无数据时是 ``None``
   （前端显示「—」），有数据且恰好为 0 时是 ``0.0``（显示 0.00）；
5. **不伪造**：取数失败只写 ``error``，不塞假数字。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.routes.account import _account_row, _sum_opt
from xtquant_client.base import BrokerError
from xtquant_client.manager import Connection, ConnectionConfig


class _CountingAdapter:
    """记录调用次数的桩适配器（用于断言「持仓只取一次」）。"""

    adapter_id = "stub"
    broker_name = "桩券商"
    client_version = ""
    supported_periods: list = []
    supported_account_types: list = []

    def __init__(self, account=None, positions=None, orders=None, deals=None, fail=False):
        self._account = account or {}
        self._positions = positions or []
        self._orders = orders or []
        self._deals = deals or []
        self._fail = fail
        self.calls: dict[str, int] = {"account": 0, "positions": 0, "orders": 0, "deals": 0}

    def get_account(self):
        self.calls["account"] += 1
        if self._fail:
            raise BrokerError("桩：账户不可用")
        return self._account

    def get_positions(self):
        self.calls["positions"] += 1
        if self._fail:
            raise BrokerError("桩：持仓不可用")
        return self._positions

    def get_orders(self):
        self.calls["orders"] += 1
        return self._orders

    def get_deals(self):
        self.calls["deals"] += 1
        return self._deals


def _conn(adapter, *, connected=True, last_error="") -> Connection:
    cfg = ConnectionConfig(
        conn_id="c1", name="测试账户", broker_id="stub",
        client_path="", client_mode="auto", account_id="000001",
        account_type="STOCK", session_id=0, min_version="", active=True,
    )
    return Connection(cfg=cfg, adapter=adapter, bridge=None,
                      connected=connected, last_error=last_error)


def _ctx() -> SimpleNamespace:
    """最小 ctx：enrich_positions 只在「持仓行无现价」时才会去打源。"""
    return SimpleNamespace(sync_engine=None)


def _pos(code="600519.SH", name="贵州茅台", volume=100, cost=1400.0, price=1500.0, **over):
    p = {"code": code, "name": name, "volume": volume, "cost": cost, "price": price}
    p.update(over)
    return p


# ---------------- 1. 富化（现价 / 成本 / 盈亏） ----------------


def test_positions_are_enriched_with_profit():
    """券商只给 cost/market_value，盈亏必须由富化补算出来（而不是恒 0）。"""
    adapter = _CountingAdapter(
        account={"assets": 0, "cash": 0, "market_value": 0},
        positions=[{"code": "600519.SH", "name": "贵州茅台", "volume": 100,
                    "cost": 1400.0, "market_value": 150000.0, "price": 1500.0}],
    )
    row, pos = asyncio.run(_account_row(_conn(adapter), _ctx()))
    assert row["error"] == ""
    assert len(pos) == 1
    # (1500 - 1400) × 100 = 10000
    assert pos[0]["profit"] == 10000.0
    assert pos[0]["profit_pct"] == round(100 / 1400 * 100, 2)
    assert row["profit"] == 10000.0


def test_missing_cost_yields_none_not_zero():
    """没有成本价 ⇒ profit 必须是 None（前端显示「—」），不能是 0。

    0.00 会被读成「真的不赚不亏」，而事实是「算不出来」——本项目的「假成功」家族。
    """
    adapter = _CountingAdapter(
        account={"assets": 0, "cash": 0, "market_value": 0},
        positions=[{"code": "600519.SH", "name": "贵州茅台", "volume": 100,
                    "market_value": 150000.0, "price": 1500.0}],
    )
    row, pos = asyncio.run(_account_row(_conn(adapter), _ctx()))
    assert pos[0].get("profit") is None
    assert row["profit"] is None


# ---------------- 2. assets 为 0 时退化为 cash + 持仓市值 ----------------


def test_assets_falls_back_to_cash_plus_position_value():
    """实测场景：``get_account().assets`` 恒为 0，但持仓市值 179.9。"""
    adapter = _CountingAdapter(
        account={"assets": 0.0, "cash": 20.1, "market_value": 0.0},
        positions=[{"code": "513090.SH", "name": "证券ETF", "volume": 100,
                    "cost": 1.5, "market_value": 179.9, "price": 1.799}],
    )
    row, _ = asyncio.run(_account_row(_conn(adapter), _ctx()))
    assert row["assets"] == round(20.1 + 179.9, 2)
    assert row["assets"] >= row["market_value"] > 0


def test_real_assets_is_not_overridden():
    """券商给了非 0 的 assets 就尊重它，不做任何「修正」。"""
    adapter = _CountingAdapter(
        account={"assets": 12345.67, "cash": 100.0, "market_value": 12245.67},
        positions=[{"code": "600519.SH", "name": "贵州茅台", "volume": 100,
                    "cost": 1400.0, "market_value": 150000.0, "price": 1500.0}],
    )
    row, _ = asyncio.run(_account_row(_conn(adapter), _ctx()))
    assert row["assets"] == 12345.67


# ---------------- 3. 每个账户的持仓只取一次 ----------------


def test_positions_fetched_exactly_once_per_account():
    """``_account_row`` 现在同时返回行与持仓，调用方不得再取一遍。"""
    adapter = _CountingAdapter(
        account={"assets": 1000, "cash": 1000, "market_value": 0},
        positions=[_pos()],
    )
    asyncio.run(_account_row(_conn(adapter), _ctx()))
    assert adapter.calls["positions"] == 1
    assert adapter.calls["account"] == 1


# ---------------- 4. 取数失败只写 error，不伪造 ----------------


def test_broker_error_marks_row_and_returns_no_positions():
    adapter = _CountingAdapter(fail=True)
    row, pos = asyncio.run(_account_row(_conn(adapter), _ctx()))
    assert row["error"]
    assert "账户不可用" in row["error"]
    assert pos == []
    assert row["assets"] == 0.0 and row["profit"] is None


def test_disconnected_account_reports_reason():
    adapter = _CountingAdapter()
    row, pos = asyncio.run(_account_row(_conn(adapter, connected=False, last_error="桥接已退出"), _ctx()))
    assert row["connected"] is False
    assert row["error"] == "桥接已退出"
    assert pos == []
    # 未连接时不该去打券商
    assert adapter.calls["positions"] == 0


# ---------------- 5. 0 与「不知道」的区分 ----------------


def test_sum_opt_distinguishes_zero_from_unknown():
    assert _sum_opt([]) is None
    assert _sum_opt([None, None]) is None
    # 已知的 0 必须保留为 0.0（不是 None）
    assert _sum_opt([0, 0]) == 0.0
    assert _sum_opt([0, 0]) is not None
    assert _sum_opt([1.5, None, -0.5]) == 1.0


def test_sum_opt_tolerates_unparsable_values():
    """券商偶尔回 ``""`` / ``"--"``：按「没有值」处理，不能让看板 500。"""
    assert _sum_opt([None]) is None
    assert _sum_opt([""]) is None
    assert _sum_opt(["--"]) is None
    assert _sum_opt([0, ""]) == 0.0
    assert _sum_opt([2.5, "--", None, "1.5"]) == 4.0

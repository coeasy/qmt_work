# -*- coding: utf-8 -*-
"""「连上了」≠「能用」：券商连接健康度必须并入**交易能力**。

背景（2026-10-14 真机取证）
--------------------------
大 QMT 文件桥的存活判据只回答「agent 还在应答吗」。本机实测存在这样一种状态：

  * ``agent_status.json`` 心跳新鲜、``PROBE`` 通过、连接页显示**已连接**；
  * 但 agent 自报 ``injected=[]``、``trade_surface.can_submit=false``
    —— 因为它被 QMT 以**独立进程**形态跑（``pythonw -u <策略.py> <userdata>``），
      终端只把 ``passorder`` / ``get_trade_detail_data`` 注入**进程内**被公式引擎
      挂载的那个文件；
  * 于是界面是一条**绿灯**，用户每次下单/查账户都撞 503。

这是本项目反复出现的「假绿灯」家族：**存活 ≠ 可用**。旧实现里
``gateway.health._check`` 只看 ``adapter.is_connected()``，因此这条连接永远是
``health_status="connected"``，``last_error`` 为空，用户没有任何线索。

本文件锁住新的口径：
  * 适配器可选实现 ``capability_gap() -> str``（**必须 O(1) 零 IO**）；
  * 报出缺口 ⇒ ``health_status`` 降级为 ``needs_action`` 且 ``last_error`` 带处置文案；
  * 缺口消失 ⇒ 恢复 ``connected``；
  * **未实现该方法的适配器行为完全不变**（本改动只增不减，不改既有语义）。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from connectors.bigqmt_bridge import BigQmtBridge  # noqa: E402
from gateway.health import BrokerHealthMonitor  # noqa: E402

# 直接用产线文案：断言「处置里含 内置Python」才有意义（自造短串会空洞通过）。
_REMEDY = BigQmtBridge._NO_TRADE_REMEDY


# active=False：这些用例只测「连通 / 能力」两个分支，不需要调度重连任务 ——
# 重连任务会把 health_status 立刻改成 connecting，污染断言。
class _Cfg:
    def __init__(self, conn_id="c1", active=False):
        self.conn_id = conn_id
        self.active = active
        self.name = conn_id


class _Adapter:
    """最小适配器替身：可切换连接状态与能力缺口。"""

    def __init__(self, connected=True, gap=""):
        self._connected = connected
        self._gap = gap
        self.gap_calls = 0

    def is_connected(self):
        return self._connected

    def capability_gap(self):
        self.gap_calls += 1
        return self._gap


class _PlainAdapter:
    """**不实现** capability_gap 的适配器（模拟 xtquant 直连等既有实现）。"""

    def __init__(self, connected=True):
        self._connected = connected

    def is_connected(self):
        return self._connected


class _Conn:
    def __init__(self, adapter, conn_id="c1"):
        self.cfg = _Cfg(conn_id)
        self.adapter = adapter
        self.connected = False
        self.health_status = "disconnected"
        self.last_error = ""
        self.reconnect_attempts = 0
        self.reconnect_task = None


class _Manager:
    def __init__(self, conns):
        self._conns = {c.cfg.conn_id: c for c in conns}

    def all_connections(self):
        return list(self._conns.values())


def _check(conn, **kw):
    mon = BrokerHealthMonitor(_Manager([conn]), on_event=None, **kw)
    asyncio.run(mon._check(conn))
    return conn


def test_capability_gap_degrades_connected_to_needs_action():
    """★ 桥可用但无下单能力 ⇒ 必须降级 + 带可执行处置，而不是继续挂绿灯。"""
    conn = _Conn(_Adapter(connected=True, gap=_REMEDY))
    _check(conn)
    assert conn.connected is True, "连接本身是通的（available=True），不能谎报掉线"
    assert conn.health_status == "needs_action", conn.health_status
    assert conn.last_error == _REMEDY
    assert "内置Python" in conn.last_error, "处置文案必须指向真正能修好的动作"


def test_capability_gap_clearing_restores_connected():
    """缺口消失（改对运行形态后）⇒ 恢复 connected 并清空 last_error 语义。"""
    ad = _Adapter(connected=True, gap=_REMEDY)
    conn = _Conn(ad)
    _check(conn)
    assert conn.health_status == "needs_action"

    ad._gap = ""
    _check(conn)
    assert conn.health_status == "connected", conn.health_status


def test_plain_adapter_behaviour_unchanged():
    """★ 只增不减：不实现 capability_gap 的适配器健康语义必须与改动前完全一致。"""
    conn = _Conn(_PlainAdapter(connected=True))
    _check(conn)
    assert conn.health_status == "connected"
    assert conn.last_error == ""

    conn2 = _Conn(_PlainAdapter(connected=False))
    _check(conn2)
    assert conn2.health_status == "disconnected"
    assert conn2.connected is False


def test_capability_gap_exception_does_not_break_health_loop():
    """诊断方法抛异常 ⇒ 视作「无缺口」，绝不能炸掉整轮健康巡检。"""
    class _Boom(_Adapter):
        def capability_gap(self):
            raise RuntimeError("probe exploded")

    conn = _Conn(_Boom(connected=True))
    _check(conn)
    assert conn.health_status == "connected", "诊断失败不得影响健康判定"


def test_capability_gap_not_consulted_when_disconnected():
    """未连通时不查能力缺口（避免在掉线状态下给出误导性的能力结论）。"""
    ad = _Adapter(connected=False, gap=_REMEDY)
    conn = _Conn(ad)
    _check(conn)
    assert conn.health_status == "disconnected"
    assert ad.gap_calls == 0, "掉线时不该去问能力缺口"


# --------------------------------------------------------------------------
# BigQmtBridge.capability_gap：三种语义必须区分（未知 / 有能力 / 确知无能力）
# --------------------------------------------------------------------------

class _Transport:
    def __init__(self, meta):
        self._meta = meta

    @property
    def agent_meta(self):
        return dict(self._meta)


class _Connector:
    def __init__(self, meta):
        self.transport = _Transport(meta)

    def supported_ops(self):
        return ["PLACE_ORDER", "QUERY_ASSET"]


def _bridge(meta):
    return BigQmtBridge(_Connector(meta), "c-gap",
                        connector_key="qmt.big.bridge.file")


def test_capability_gap_empty_when_agent_meta_unknown():
    """agent 尚未回报 meta ⇒ 返回 ""（**未知**，不妄断）。

    否则刚连上的一瞬间会把一切正常的连接误报成「不可下单」—— 比假绿灯更糟。
    """
    assert _bridge({}).capability_gap() == ""


def test_capability_gap_empty_when_passorder_captured():
    b = _bridge({"funcs": ["passorder", "cancel", "get_trade_detail_data"],
                 "trading_enabled": True})
    assert b.capability_gap() == ""


def test_capability_gap_reports_independent_process_mode():
    """独立进程模式（只转发行情、无 passorder）⇒ 必须给出可执行处置。"""
    b = _bridge({"funcs": ["get_full_tick", "get_market_data"],
                 "forwarded_funcs": 76, "trading_enabled": False,
                 "runtime_mode": "standalone_process"})
    gap = b.capability_gap()
    assert gap, "确知无 passorder 时必须报缺口"
    assert "passorder" in gap
    assert "独立进程" in gap, "必须点明运行形态是根因"
    assert "内置Python" in gap, "必须给出真正能修好的动作"
    assert "standalone_process" in gap, "agent 自报的运行形态要带出来"


def test_capability_gap_is_zero_io():
    """健康巡检每 5s 跑一次 ⇒ capability_gap 必须是 O(1) 零 IO。

    用「不实现任何 IO 的 connector」构造：若它触发了网络/文件往返，本测试会因为
    AttributeError 直接失败（_Connector 只有 supported_ops）。
    """
    import time as _t
    b = _bridge({"funcs": ["get_full_tick"], "trading_enabled": False})
    t0 = _t.perf_counter()
    for _ in range(200):
        b.capability_gap()
    assert _t.perf_counter() - t0 < 0.5, "capability_gap 不得有耗时 IO"


def test_capability_gap_never_raises_on_broken_connector():
    """connector 形态意外（connector_probe 抛）⇒ 返回 "" 而不是把异常漏出去。"""
    class _Boom:
        @property
        def transport(self):
            raise RuntimeError("no transport")

    b = BigQmtBridge(_Boom(), "c-boom", connector_key="qmt.big.bridge.file")
    assert b.capability_gap() == ""

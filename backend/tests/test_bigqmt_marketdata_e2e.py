"""M2：大 QMT agent 行情/交易能力补全的端到端与单元回归。

覆盖计划 §6 M2 的验收点：
* M2.1 QUERY_STOCK_LIST / QUERY_SECTOR_LIST / QUERY_INSTRUMENT /
  QUERY_CALENDAR / SUB_QUOTE 解除拒绝（fake agent + 真 Executor 双层）；
* M2.2 D2 方向仲裁表（optName→offsetFlag→direction→下单记忆→unknown）；
* M2.4 D3 ttl_ms 过期回 Expired（幽灵单防护）；
* M2.5 D6 回调双轨：note_callback 写基线防 diff 二次发事件；
* M2.6 D5 ascii_only 开关（响应/事件全转义仍可解析）；
* D7 契约：dialect prepare 白名单丢弃未知键，绝不透传到 agent。
"""
import asyncio
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from connectors.dialects import BigQmtV1  # noqa: E402
from connectors.registry import resolve  # noqa: E402
from connectors.transport import WireRequest  # noqa: E402

from fake_bigqmt_agent import FakeBigQmtAgent  # noqa: E402

# 按文件路径加载 py3.6 agent 模块（它不属于后端包，禁止 import 副作用）
_spec = importlib.util.spec_from_file_location(
    "qmt_api_under_test",
    str(Path(__file__).resolve().parent.parent / "agent_bigqmt" / "qmt_api.py"))
qmt_api = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(qmt_api)


# ---------------------------------------------------------------------------
# 测试替身：模拟 QMT 注入命名空间 + ContextInfo
# ---------------------------------------------------------------------------
class _Ctx:
    """ContextInfo 替身：get_full_tick / get_market_data / 板块函数。"""

    def __init__(self, ticks=None, market_data=None):
        self._ticks = ticks or {}
        self._md = market_data
        self.downloaded = []

    def get_full_tick(self, codes):
        return {c: self._ticks[c] for c in codes if c in self._ticks}

    def get_market_data(self, fields, codes, period, count):
        return self._md

    def download_history_data(self, code, period, start="", end=""):
        self.downloaded.append((code, period))


def _mk_executor(cfg=None, injected=None, ctx=None):
    return qmt_api.Executor(cfg or {"bridge_dir": "", "trading_enabled": True},
                            injected or {}, ctx or _Ctx())


# ---------------------------------------------------------------------------
# M2.1 Executor 层：行情/合约扩展 action
# ---------------------------------------------------------------------------
def test_stock_list_sector_calendar_instrument_routed():
    calls = {}

    def get_stock_list_in_sector(sector):
        calls["sector"] = sector
        return ["600036.SH", "000001.SZ"]

    def get_instrument_detail(code, field="InstrumentName"):
        return {"InstrumentName": "名称-" + code}

    def get_trading_dates(market, start, end):
        return ["20260928", "20260929"]

    ex = _mk_executor(injected={
        "get_stock_list_in_sector": get_stock_list_in_sector,
        "get_instrument_detail": get_instrument_detail,
        "get_trading_dates": get_trading_dates,
        "get_sector_list": lambda: ["沪深A股", "ETF"],
    })
    assert ex.do_stock_list({"sector": "沪深A股"}) == [
        {"code": "600036.SH", "name": "名称-600036.SH"},
        {"code": "000001.SZ", "name": "名称-000001.SZ"}]
    assert calls["sector"] == "沪深A股"
    assert ex.do_sector_list({}) == ["沪深A股", "ETF"]
    assert ex.do_calendar({"start": "2026-09-28", "end": "2026/09/30"}) == \
        ["20260928", "20260929"]
    assert ex.do_instrument({"code": "600036.SH"})["InstrumentName"] == "名称-600036.SH"


def test_uncaptured_function_reports_not_captured_not_absent():
    """未捕获 ⇒ error_type 只说「未捕获」，绝不断言「终端没有该接口」。"""
    ex = _mk_executor()
    with pytest.raises(qmt_api.ActionError) as exc:
        ex.do_stock_list({})
    assert exc.value.error_type == "BrokerSDKError"
    assert "未捕获" in str(exc.value)
    assert "没有该接口" not in str(exc.value)


def test_subscribe_and_quote_events_diff_forward():
    ctx = _Ctx(ticks={"600036.SH": {"lastPrice": 35.5, "volume": 100,
                                    "lastTime": "14:30:00"}})
    ex = _mk_executor(ctx=ctx)
    state = {"quote_seen": {}, "order_seen": {}, "trade_seen": {}, "primed": True}
    events = []
    emit = lambda kind, data: events.append((kind, data))  # noqa: E731
    assert ex.quote_events(state, emit) == 0            # 未订阅 ⇒ 无事件
    r = ex.do_subscribe({"codes": ["600036.SH"]})
    assert r["mode"] == "poll_forward" and r["subscribed"] == ["600036.SH"]
    assert ex.quote_events(state, emit) == 1            # 首见即发布一帧快照
    assert events[-1][0] == "quote" and events[-1][1]["price"] == 35.5
    assert ex.quote_events(state, emit) == 0            # tick 未变 ⇒ 不重复发
    ctx._ticks["600036.SH"]["lastPrice"] = 35.6
    assert ex.quote_events(state, emit) == 1            # 变化 ⇒ 再发一帧


# ---------------------------------------------------------------------------
# M2.2 D2 方向仲裁表
# ---------------------------------------------------------------------------
class _Row:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


@pytest.mark.parametrize("row,expected", [
    # 一级：文案最可信
    (_Row(m_strOptName="卖", m_nOffsetFlag=48, m_nDirection=48), "sell"),
    (_Row(m_strOptName="买入"), "buy"),
    # 二级：offsetFlag
    (_Row(m_nOffsetFlag=49), "sell"),
    (_Row(m_nOffsetFlag=48), "buy"),
    # 三级：direction 只有非恒 48 的 49 可信；48 单独出现**不判买**
    (_Row(m_nDirection=49), "sell"),
    (_Row(m_nDirection=48), "unknown"),
    # 全缺失 ⇒ unknown（不许猜）
    (_Row(), "unknown"),
])
def test_direction_arbitration_table(row, expected):
    ex = _mk_executor()
    assert ex._row_to_dict(qmt_api._DT_ORDER, row)["direction"] == expected


def test_direction_falls_back_to_place_memory():
    """opType 记忆级：本进程下过的单按备注（client_order_id）回查方向。"""
    ex = _mk_executor(injected={"passorder": lambda *a: 7160})
    r = ex.do_place({"stock_code": "600036.SH", "side": "sell", "price": 35.0,
                     "volume": 100, "price_type": "limit",
                     "client_order_id": "cid-1"})
    assert r["order_id"] == "7160"
    row = _Row(m_strRemark="cid-1", m_nDirection=48)
    out = ex._row_to_dict(qmt_api._DT_ORDER, row)
    assert out["direction"] == "sell"


def test_generated_client_order_id_is_declared_not_faked():
    ex = _mk_executor(injected={"passorder": lambda *a: 7161})
    r = ex.do_place({"stock_code": "600036.SH", "side": "buy", "price": 3.0,
                     "volume": 100})
    assert r["client_order_id"].startswith("ag")
    assert r.get("generated_client_order_id") is True   # 生成关联号必须如实声明


# ---------------------------------------------------------------------------
# M2.4 D3 ttl 过期 / M2.5 D6 回调双轨 / M2.6 D5 ascii
# ---------------------------------------------------------------------------
def test_ttl_expired_write_rejected_reads_unaffected():
    ex = _mk_executor(injected={"get_trade_detail_data":
                                lambda *a: []})
    env = {"op": "PLACE", "ts": int(time.time() * 1000) - 5000,
           "params": {"ttl_ms": 1000}}
    res = ex.execute(env)
    assert res["ok"] is False and res["error_type"] == "Expired"
    # 缺省（无 ttl）不过期：向后兼容旧信封
    res2 = ex.execute({"op": "QUERY_ORDER", "ts": int(time.time() * 1000) - 10 ** 7,
                       "params": {}})
    assert res2["ok"] is True


def test_callback_note_writes_baseline_so_diff_does_not_double_emit():
    """D6 双轨去重：回调发过的事件写入差分基线，diff_events 不再对同一状态重发。"""
    order_row = lambda: _Row(m_strOrderSysID="7160", m_nOrderStatus=48,  # noqa: E731
                             m_nVolumeTraded=0, m_dTradedPrice=0.0,
                             m_strInsertTime="14:31:00", m_strOptName="买")
    ex = _mk_executor(injected={"get_trade_detail_data":
                                lambda *a: [order_row()]})
    state = {"order_seen": {}, "trade_seen": {}, "primed": True}
    events = []
    # 模拟 BIGQMT_AGENT._on_broker_callback：note → 写基线 → emit
    row, key, fp = ex.note_callback("order", order_row())
    state["order_seen"][key] = fp
    events.append(("order", row))
    ex.diff_events(state, lambda k, d: events.append((k, d)))
    assert len(events) == 1                       # diff 没有二次发同一事件
    assert ex.meta()["callback_bound"] is True
    # 状态变化后 diff 仍正常增量发事件
    events.clear()
    ex.injected["get_trade_detail_data"] = lambda *a: [
        _Row(m_strOrderSysID="7160", m_nOrderStatus=48, m_nVolumeTraded=100,
             m_dTradedPrice=35.5, m_strInsertTime="14:31:00",
             m_strOptName="买")]
    ex.diff_events(state, lambda k, d: events.append((k, d)))
    assert events and events[0][0] == "order"


def test_ascii_only_response_is_translatable_json(tmp_path):
    ex = qmt_api.Executor({"bridge_dir": str(tmp_path), "ascii_only": True,
                           "trading_enabled": True}, {}, _Ctx())
    os.makedirs(os.path.join(tmp_path, "resp"), exist_ok=True)
    ex._write_response("sig1", {"ok": False, "error": "中文错误原因",
                                "error_type": "BrokerError"})
    raw = Path(tmp_path, "resp", "sig1.json").read_text("utf-8")
    assert "\\u4e2d" in raw                      # 全转义，无裸中文
    assert json.loads(raw)["error"] == "中文错误原因"


# ---------------------------------------------------------------------------
# 端口级端到端（真实 wire + fake agent）：新 action 贯通 & Expired 语义
# ---------------------------------------------------------------------------
@pytest.fixture()
def bridge(tmp_path):
    root = str(tmp_path / "bridge")
    agent = FakeBigQmtAgent(root, token="secret", trading_enabled=True)
    agent.start()
    conn = resolve("qmt.big.bridge.file", bridge_dir=root, auth_token="secret")
    yield conn, agent, root
    agent.stop()


def test_port_level_market_data_actions_flow(bridge):
    conn, agent, root = bridge

    async def run():
        return (await conn.get_stock_list("沪深A股"),
                await conn.get_trading_calendar("20260928", "20260930"),
                await conn._call("SUBSCRIBE_QUOTE", {"codes": ["600036.SH"]}))

    stocks, calendar, sub = asyncio.run(run())
    assert stocks and stocks[0]["code"] == "600036.SH"
    assert calendar == ["20260928", "20260929", "20260930"]
    assert sub["mode"] == "poll_forward"
    # 查询回报方向已归一化（fake 给 buy ⇒ canonical side=buy）
    orders = asyncio.run(conn.get_orders())
    assert orders[0].side == "buy"


def test_expired_ttl_maps_to_error_with_cause(bridge):
    """过期写请求：Expired 必须**炸出且保留真因**，不许被吞成超时/空结果。"""
    conn, agent, root = bridge
    payload = {"code": "600036.SH", "side": "buy", "order_type": "limit",
               "price": 35.5, "quantity": 100, "ttl_ms": 1}
    time.sleep(0.05)
    with pytest.raises(Exception) as exc:
        asyncio.run(conn._call("PLACE_ORDER", payload))
    assert "过期" in str(exc.value)


def test_quote_event_stream_translates_canonical(bridge):
    conn, agent, root = bridge
    agent.emit("quote", {"code": "600036.SH", "time": "14:30:00",
                         "price": 35.5, "tick": {"lastPrice": 35.5}})
    evs = conn.stream_events()
    assert evs and evs[0].kind == "quote"
    assert evs[0].data["price"] == 35.5


def test_transport_clock_offset_observable(bridge):
    """M2.7 health 面数据源：响应信封 ts 与本端时钟偏差必须可读且非异常。"""
    conn, agent, root = bridge
    asyncio.run(conn.test_connection())
    offset = conn.transport.clock_offset_ms
    assert isinstance(offset, int) and abs(offset) < 5000


# ---------------------------------------------------------------------------
# D7 契约：方言 prepare 是白名单 —— 未知/多余键必须被丢弃，绝不抵达 agent
# ---------------------------------------------------------------------------
def test_dialect_prepare_drops_unknown_keys():
    d = BigQmtV1()
    p = d.prepare("PLACE_ORDER", {
        "code": "600036.SH", "side": "buy", "order_type": "limit",
        "price": 35.5, "quantity": 100,
        "entrust_prop": "bgra", "some_future_key": 1, "on_tick": lambda x: x})
    assert "entrust_prop" not in p and "some_future_key" not in p
    assert "on_tick" not in p
    sub = d.prepare("SUBSCRIBE_QUOTE", {"codes": ["600036.SH"],
                                        "on_tick": lambda x: x, "junk": 2})
    assert sub == {"codes": ["600036.SH"]}


def test_dialect_maps_all_new_ops():
    d = BigQmtV1()
    assert d.op_of("GET_STOCK_LIST") == "QUERY_STOCK_LIST"
    assert d.op_of("GET_SECTOR_LIST") == "QUERY_SECTOR_LIST"
    assert d.op_of("GET_INSTRUMENT_DETAIL") == "QUERY_INSTRUMENT"
    assert d.op_of("GET_TRADING_CALENDAR") == "QUERY_CALENDAR"
    assert d.op_of("SUBSCRIBE_QUOTE") == "SUB_QUOTE"
    assert set(qmt_api._ACTIONS) >= {
        "QUERY_STOCK_LIST", "QUERY_SECTOR_LIST", "QUERY_INSTRUMENT",
        "QUERY_CALENDAR", "SUB_QUOTE"}


def test_wire_request_carries_ttl_and_signal_id():
    req = WireRequest(op="PLACE", params={"ttl_ms": 3000})
    assert req.params["ttl_ms"] == 3000

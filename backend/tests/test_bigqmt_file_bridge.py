"""V4 Phase 1+2：Dialect×Transport 组合 + 大 QMT 文件桥端到端。

盯的是**不变量**，不是「能不能调通」：

* 业务失败（柜台拒单）必须走 400 语义的异常类型，不得被吞成空结果；
* 传输故障必须走 503 语义的 TransportError，不得伪装成空列表；
* 拿不到柜台委托号 ⇒ ``place_order`` 必须报错（零 mock）；
* POLL_DIFF 必须声明正的延迟上界；
* 同一 dialect 换 transport 不需改一行方言代码。
"""
import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from connectors.canonicalize import OrderSnapshot  # noqa: E402
from connectors.dialects import BigQmtV1, XtQuantV1, get_dialect  # noqa: E402
from connectors.ports import (  # noqa: E402
    AccountSnapshot, CapabilityLevel, EventSemantics, InstrumentId, OrderRequest,
    PositionSnapshot,
)
from connectors.registry import available_keys, describe, resolve  # noqa: E402
from connectors.transports import FileSignalTransport  # noqa: E402
from connectors.transports.semantics import max_latency_ms, spec_for  # noqa: E402
from connectors.transport import TransportError, WireRequest  # noqa: E402

from fake_bigqmt_agent import FakeBigQmtAgent  # noqa: E402


# ---------------------------------------------------------------------------
# Phase 1：组合装配
# ---------------------------------------------------------------------------
def test_registry_lists_six_free_combinations():
    keys = available_keys()
    assert "qmt.mini" in keys
    assert {"qmt.big.direct", "qmt.big.bridge.file",
            "qmt.big.bridge.redis", "qmt.big.bridge.zmq"} <= set(keys)


def test_resolve_requires_transport_specific_args():
    with pytest.raises(ValueError):
        resolve("qmt.big.bridge.file")            # 缺 bridge_dir
    with pytest.raises(ValueError):
        resolve("qmt.big.bridge.redis")           # 缺 redis_url
    with pytest.raises(ValueError):
        resolve("qmt.big.bridge.zmq")
    with pytest.raises(KeyError):
        resolve("qmt.notexist")


def test_describe_is_introspectable_without_side_effects():
    info = describe("qmt.big.bridge.file")
    assert info["dialect"] == "bigqmt.v1"
    assert info["transport"] == "file"


def test_same_dialect_different_transport_shares_code():
    """同一 dialect 可配不同 transport —— 方言代码零改动。"""
    d = get_dialect("bigqmt.v1")
    assert d.dialect_id == "bigqmt.v1"
    assert get_dialect("xtquant.v1").__class__ is XtQuantV1
    assert isinstance(d, BigQmtV1)


def test_event_semantics_upper_bounds_are_positive():
    """POLL_DIFF / PUSH_WITH_GAP 必须声明上界，否则超时守护会误撤单。"""
    assert spec_for("file").semantics is EventSemantics.POLL_DIFF
    assert max_latency_ms("file") > 0
    assert max_latency_ms("redis") > 0
    assert max_latency_ms("zmq") > 0
    assert spec_for("inprocess").semantics is EventSemantics.PUSH


# ---------------------------------------------------------------------------
# Phase 2：大 QMT 文件桥端到端
# ---------------------------------------------------------------------------
@pytest.fixture()
def bridge(tmp_path):
    root = str(tmp_path / "bridge")
    agent = FakeBigQmtAgent(root, token="secret", trading_enabled=True)
    agent.start()
    conn = resolve("qmt.big.bridge.file", bridge_dir=root,
                   auth_token="secret")
    yield conn, agent, root
    agent.stop()


def test_probe_returns_agent_metadata(bridge):
    conn, agent, root = bridge

    async def run():
        reply = await conn.test_connection()
        return reply

    reply = asyncio.run(run())
    # FakeQMT 的 PROBE 返回 {captured: [...]}；连接层原样透传，不做粉饰。
    assert reply["captured"]


def test_place_order_returns_canonical_snapshot(bridge):
    conn, agent, root = bridge
    req = OrderRequest(instrument=InstrumentId(code="600036", exchange="SH"),
                       side="buy", order_type="limit", price=35.5, quantity=100,
                       client_order_id="abc-123")

    async def run():
        return await conn.place_order(req)

    snap = asyncio.run(run())
    assert isinstance(snap, OrderSnapshot)
    assert snap.broker_order_id == "7160"
    assert snap.accepted is True
    assert snap.client_order_id == "abc-123"
    # 幂等键确实走到了对端 wire（否则对账缺锚点）
    assert agent.received[-1]["params"]["client_order_id"] == "abc-123"


def test_counter_rejection_becomes_broker_error(bridge):
    """柜台拒单 ⇒ BrokerError（400 + 真因），不是空结果也不是 TransportError。"""
    conn, agent, root = bridge
    req = OrderRequest(instrument=InstrumentId(code="600036", exchange="SH"),
                       side="buy", order_type="limit", price=0.0, quantity=100)

    async def run():
        await conn.place_order(req)

    # OrderRequest.validate 先拦：限价单价格必须为正（更上游的防线）
    with pytest.raises(ValueError):
        asyncio.run(run())

    req2 = OrderRequest(instrument=InstrumentId(code="600036", exchange="SH"),
                        side="buy", order_type="limit", price=35.5, quantity=100)
    agent.trading_enabled = False

    async def run2():
        await conn.place_order(req2)

    with pytest.raises(Exception) as exc:
        asyncio.run(run2())
    assert "trading_enabled" in str(exc.value)


def test_missing_broker_order_id_is_not_painted_as_accepted(tmp_path):
    """零 mock：回执无委托号 ⇒ place_order 必须炸，不许返回 accepted 的快照。"""
    root = str(tmp_path / "b2")

    class _NoOrderId(FakeBigQmtAgent):
        def _dispatch(self, op, params):
            if op == "PLACE":
                return {"order_id": "", "status": "unknown"}
            return FakeBigQmtAgent._dispatch(self, op, params)

    agent = _NoOrderId(root)
    agent.start()
    conn = resolve("qmt.big.bridge.file", bridge_dir=root)
    try:
        req = OrderRequest(instrument=InstrumentId(code="600036", exchange="SH"),
                           side="buy", order_type="limit", price=1.0, quantity=100)

        async def run():
            await conn.place_order(req)

        with pytest.raises(Exception) as exc:
            asyncio.run(run())
        assert "委托号" in str(exc.value) or "order" in str(exc.value).lower()
    finally:
        agent.stop()


def test_unreachable_agent_raises_transport_error(tmp_path):
    """对端不在 ⇒ TransportError（503 语义），绝不返回空数据糊过去。"""
    root = str(tmp_path / "b3")
    conn = resolve("qmt.big.bridge.file", bridge_dir=root, agent_timeout=0.5)

    async def run():
        await conn.get_positions()

    with pytest.raises(TransportError):
        asyncio.run(run())


def test_wrong_token_is_rejected(bridge):
    conn, agent, root = bridge
    bad = FileSignalTransport(root, auth_token="WRONG", poll_interval=0.02)
    from connectors.generic import GenericConnector

    c2 = GenericConnector(dialect=get_dialect("bigqmt.v1"), transport=bad)

    async def run():
        await c2.get_orders()

    with pytest.raises(Exception) as exc:
        asyncio.run(run())
    assert "token" in str(exc.value).lower() or "Rejected" in str(exc.value)


def test_wrong_signal_id_response_is_protocol_error(tmp_path):
    """signal_id 不匹配必须报错 —— 串行/envThread 下错配会造成成交归属错乱。"""
    root = str(tmp_path / "b4")
    os.makedirs(os.path.join(root, "resp"), exist_ok=True)

    class _Mismatch(FileSignalTransport):
        async def _await_response(self, request):
            return {"v": 1, "signal_id": "WRONG-ID", "ok": True, "result": {}}

    from connectors.transports.file_signal import FileSignalProtocolError

    t = _Mismatch(root, poll_interval=0.02)

    async def run():
        await t.invoke(WireRequest(op="PROBE", timeout=1.0))

    with pytest.raises(FileSignalProtocolError):
        asyncio.run(run())


def test_canonical_queries(bridge):
    conn, agent, root = bridge

    async def run():
        return (await conn.get_orders(), await conn.get_deals(),
                await conn.get_positions(), await conn.get_account())

    orders, deals, positions, account = asyncio.run(run())
    assert orders and isinstance(orders[0], OrderSnapshot)
    assert orders[0].status == "filled"        # 56 → SSOT filled
    assert deals and deals[0].status == "filled"
    assert positions and isinstance(positions[0], PositionSnapshot)
    assert positions[0].quantity == 200 and positions[0].available_quantity == 100
    assert isinstance(account, AccountSnapshot)
    assert account.cash == 10000.0 and account.assets == 45200.0


def test_capability_probe_marks_unverified_as_unknown(bridge):
    conn, agent, root = bridge

    async def run():
        return await conn.probe_capabilities()

    caps = asyncio.run(run())
    # 真正探测：捕获到 passorder ⇒ trade SUPPORTED；捕获到 get_full_tick ⇒ quote SUPPORTED。
    assert caps.is_supported("quote")
    assert caps.is_supported("trade")
    # 文件桥是 POLL_DIFF ⇒ realtime 必须是 DEGRADED（不是 SUPPORTED，也不是 UNKNOWN）。
    assert caps.level_of("realtime") is CapabilityLevel.DEGRADED
    # 任何查不到的项必须是 UNKNOWN 而非 UNSUPPORTED（禁止据单点探测断言终端能力）。
    assert caps.level_of("bigqmt.credit") is CapabilityLevel.UNKNOWN
    assert caps.is_definitely_unsupported("bigqmt.credit") is False


def test_capability_vocabulary_is_closed_and_contradiction_free(bridge):
    """probe 输出的能力名必须与能力词表**同名**，且不得自相矛盾。

    两个真实缺陷（都已修，此处钉死）：

    1. ``_FUNC_TO_CAPS`` 曾写 ``"get_trade_detail_data" → ("account", "position",
       …)`` 用**单数** ``position``，而词表（``registry._DEFAULT_CAPS`` /
       ``QmtConnector.descriptor``）用的是**复数** ``positions``。
       后果：probe 同时输出 ``position``(SUPPORTED) 与 ``positions``(UNKNOWN)
       两条互相矛盾的能力 —— 前端按声明名查 ``positions`` 看到「不支持」，
       而持仓明明是通的。

    2. 大 QMT agent 注入的日历函数叫 ``get_trading_dates``（ContextInfo 的命名），
       而 ``_FUNC_TO_CAPS`` 只列了 ``get_trading_calendar``（miniQMT 的命名）。
       后果：agent 明明捕获到了日历函数，``calendar`` 仍被报成 UNKNOWN
       —— 正是本模块注释警告的「反向假阴性」。
    """
    conn, agent, root = bridge
    caps = asyncio.run(conn.probe_capabilities())
    names = {c.name for c in caps.capabilities}

    # 1) 单数/复数只能有一个，且必须是词表里的复数形式
    assert "positions" in names, "持仓能力必须按词表的复数名上报"
    assert "position" not in names, (
        "probe 输出了词表之外的单数 position ⇒ 与 positions 形成矛盾能力项")
    assert caps.is_supported("positions"), (
        "agent 捕获到 get_trade_detail_data ⇒ 持仓必须判为 SUPPORTED")

    # 2) 两个日历函数名任一被捕获 ⇒ calendar 必须 SUPPORTED
    captured = set(agent.meta()["funcs"])
    assert captured & {"get_trading_calendar", "get_trading_dates"}, (
        "fake agent 自称支持日历却没上报对应函数名，本断言失去意义")
    assert caps.is_supported("calendar"), (
        "捕获到日历注入函数，但 calendar 仍被判为 UNKNOWN（反向假阴性）")

    # 3) 输出的能力名必须全部落在已知词表内（不得凭空造名）
    known = {"quote", "kline", "trade", "trade_submit", "trade_cancel",
             "account", "positions", "order", "deal", "realtime",
             "instrument", "calendar"}
    assert names <= known, f"probe 造出了词表外的能力名：{sorted(names - known)}"


def test_event_semantics_through_connector(bridge):
    conn, agent, root = bridge
    spec = conn.event_semantics()
    assert spec.semantics is EventSemantics.POLL_DIFF
    assert spec.max_latency_ms >= 1000


def test_file_bridge_stream_events_emits_canonical(bridge):
    """大 QMT 文件桥：agent 写入 events.ndjson → EventPort 翻成 canonical 事件。"""
    conn, agent, root = bridge
    agent.emit("order", {"order_id": "7160", "code": "600036.SH",
                         "order_status": 56, "dealt": 100, "traded_price": 35.5})
    evs = conn.stream_events()
    assert evs and evs[0].kind == "order"
    assert evs[0].synthetic is True        # 桥接合成的，非柜台原生推送


def test_subscribe_quote_rejected_on_wire_transport(bridge):
    """大小 QMT 接口对齐：跨进程桥接不支持回调订阅，给出清晰 UnsupportedOp。"""
    conn, agent, root = bridge
    from connectors.dialects import UnsupportedOp

    with pytest.raises(UnsupportedOp):
        asyncio.run(conn.subscribe_quote(["600036.SH"], lambda d: None))


def test_signal_id_is_unique_per_request():
    a = WireRequest(op="PROBE")
    b = WireRequest(op="PROBE")
    assert a.signal_id and a.signal_id != b.signal_id
    assert a.timeout <= 30.0


# ---------------------------------------------------------------------------
# A13：事件游标必须是**字节偏移** —— agent 重启会让 seq 回卷
# ---------------------------------------------------------------------------
def _write_raw_events(root, rows, *, append=True):
    """直接往 events.ndjson 写行。

    ★ 为什么不走 ``agent.emit``：它自带递增计数器，构造不出**回卷**（seq 从 1 重来）
    这个关键场景 —— 而回卷正是 A13 缺陷的触发条件。
    """
    path = os.path.join(root, "events.ndjson")
    with open(path, "a" if append else "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def test_events_survive_agent_restart_seq_rewind(bridge):
    """★★ A13：agent 重启后 ``seq`` 从 1 重来，新会话的事件**不得被吞掉**。

    缺陷现场：agent 的 ``seq`` 是**进程内**计数器（``BIGQMT_AGENT._STATE["seq"]``
    从 0 起），策略重启后从 1 重来；而 ``events.ndjson`` 是 **append** 的 ⇒ 文件里
    出现「旧会话 1..3」紧跟「新会话 1..」这种**回卷**。旧实现用 ``max(last, seq)``
    当水位、并 ``seq > since_seq`` 过滤 ⇒ 新会话里 ``seq ≤ 3`` 的事件**全部消失**；
    而且只要新会话还没跑过 3 条，整条事件通道就是**死的**（前端委托/成交永不刷新），
    同时健康指标与日志**全绿** —— 与 SUB_QUOTE 未下发是同一类「安静的断链」。
    """
    conn, agent, root = bridge
    for i in range(3):
        agent.emit("order", {"order_id": f"OLD-{i}", "order_status": 48})
    assert len(conn.stream_events()) == 3
    assert conn.stream_events() == []              # 同一批不二次投递

    agent._seq = 0                                 # ← 模拟策略重启：seq 回到 1
    agent.emit("trade", {"trade_id": "NEW-1", "price": 35.5, "volume": 100})

    evs = conn.stream_events()
    assert [e.kind for e in evs] == ["trade"], (
        "agent 重启导致 seq 回卷，新会话事件被静默吞掉（事件通道变死而指标全绿）")
    assert evs[0].data["trade_id"] == "NEW-1"
    assert conn.stream_events() == []


def test_cursor_survives_rotation_to_a_bigger_file(bridge):
    """★ 轮转后**即使新文件比旧游标更大**也必须重读。

    这是「体积回缩」信号会漏掉的那一半：agent 的 ``_rotate_if_huge`` 是
    ``os.rename(path, path + ".1")``，随后 append 到**新文件**。若新文件在两次
    poll 之间就长得比旧游标还大（高频行情下很容易），``size < offset`` 不成立 ——
    只有**文件身份**（``st_ino``）能发现「换了文件」。
    """
    conn, agent, root = bridge
    agent.emit("order", {"order_id": "OLD", "order_status": 48})
    assert len(conn.stream_events()) == 1

    path = os.path.join(root, "events.ndjson")
    os.replace(path, path + ".1")                  # ← agent 的轮转动作
    _write_raw_events(root, [{"seq": 1, "type": "quote", "data": {
        "code": "600036.SH", "time": "14:30:00", "price": 35.5,
        "pad": "x" * 4096}}], append=False)        # 新文件**比旧游标大得多**

    evs = conn.stream_events()
    assert [e.kind for e in evs] == ["quote"], (
        "轮转后的新文件比旧游标大 ⇒ 只靠体积回缩发现不了，行情会被永久静默丢失")


def test_missing_then_recreated_event_file_is_reread(bridge):
    """轮转中间态（文件暂时不存在）后重建，也要从头读，不得沿用旧游标。"""
    conn, agent, root = bridge
    agent.emit("order", {"order_id": "A", "order_status": 48})
    assert len(conn.stream_events()) == 1

    os.remove(os.path.join(root, "events.ndjson"))  # rename 与下次 append 之间的空档
    assert conn.stream_events() == []
    _write_raw_events(root, [{"seq": 1, "type": "trade",
                              "data": {"trade_id": "B", "price": 1.0}}], append=False)
    assert [e.kind for e in conn.stream_events()] == ["trade"]


def test_partial_trailing_line_is_not_consumed_then_delivered_once(bridge):
    """写到一半的行不能被提前消费 —— 消费了那条记录就**永久**丢失。"""
    conn, agent, root = bridge
    half = json.dumps({"seq": 7, "type": "order",
                       "data": {"order_id": "HALF", "order_status": 48}})
    _write_raw_events(root, [])
    path = os.path.join(root, "events.ndjson")
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(half[:20])                        # 半行，且**没有换行**
    assert conn.stream_events() == [], (
        "半行被消费 ⇒ 游标越过它，那条记录永远不会再被读到")

    with open(path, "a", encoding="utf-8") as fh:
        fh.write(half[20:] + "\n")                 # 补完
    evs = conn.stream_events()
    assert [e.data.get("order_id") for e in evs] == ["HALF"]
    assert conn.stream_events() == []              # 只投一次，不重复


def test_burst_between_two_drains_loses_nothing(bridge):
    """两轮 poll 之间来一大波（>500 条）时**一条都不能丢**。

    旧实现按条数截断 ``out[-limit:]``（limit=500）却把水位推到末尾 ⇒ 超出的部分
    **永久**消失。开盘集合竞价 / 行情突发时这不是理论问题。
    """
    conn, agent, root = bridge
    rows = [{"seq": i, "type": "quote",
             "data": {"code": "600036.SH", "time": "09:25:00", "i": i}}
            for i in range(1, 701)]
    _write_raw_events(root, rows)
    evs = conn.stream_events()
    assert len(evs) == 700, f"突发 700 条只收到 {len(evs)} 条（多余部分被静默丢弃）"
    assert [e.data["i"] for e in evs] == list(range(1, 701)), "顺序也不能乱"

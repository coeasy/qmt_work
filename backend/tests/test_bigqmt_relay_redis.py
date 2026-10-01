"""M3：redis 中继（bigqmt_relay）+ RedisTransport 事件流端到端。

用 fakeredis 顶替 Redis 服务（协议替身，与 fake_bigqmt_agent 同一性质）：
验证的是「中继把 wire 三方（req/resp/events）搬对了没有」，不是 redis 本身。
"""
import asyncio
import importlib.util
import json
import os
import sys
import threading
import time
from pathlib import Path

import fakeredis
import pytest

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from connectors.transport import WireRequest  # noqa: E402
from fake_bigqmt_agent import FakeBigQmtAgent  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "bigqmt_relay_under_test", str(BACKEND.parent / "scripts" / "bigqmt_relay.py"))
relay = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(relay)

NS = "bigqmt-test"


@pytest.fixture()
def env(tmp_path):
    root = str(tmp_path / "bridge")
    agent = FakeBigQmtAgent(root, token="tok", trading_enabled=True)
    agent.start()
    client = fakeredis.FakeRedis(decode_responses=True)
    tailer = relay.EventTailer(os.path.join(root, "events.ndjson"))
    yield client, agent, root, tailer
    agent.stop()


def _relay_loop(client, root, tailer, stop):
    while not stop.is_set():
        relay.pump_once(root, client, NS, tailer)
        stop.wait(0.02)


def test_relay_moves_request_to_file_bridge(env):
    client, agent, root, tailer = env
    payload = {"v": 1, "signal_id": "s1", "op": "PROBE",
               "ts": int(time.time() * 1000), "auth": "tok", "params": {}}
    client.rpush(f"{NS}:req", json.dumps(payload))
    relay.pump_once(root, client, NS, tailer)
    # 请求已落成文件桥 req 文件，fake agent 处理后写 resp
    deadline = time.time() + 2.0
    while not os.listdir(os.path.join(root, "resp")) and time.time() < deadline:
        time.sleep(0.02)
    assert os.listdir(os.path.join(root, "resp"))
    relay.pump_once(root, client, NS, tailer)
    got = client.blpop(f"{NS}:resp:s1", timeout=1)
    assert got and json.loads(got[1])["ok"] is True
    # resp 转发后即删，不重复投递
    assert os.listdir(os.path.join(root, "resp")) == []


def test_relay_event_tailer_incremental_and_rotatable(env):
    client, agent, root, tailer = env
    agent.emit("order", {"order_id": "7160", "order_status": 48})
    assert tailer.pump(client, NS) == 1
    assert tailer.pump(client, NS) == 0            # 偏移游标：不重放
    agent.emit("trade", {"trade_id": "t1", "price": 35.5, "volume": 100})
    assert tailer.pump(client, NS) == 1
    rows = [json.loads(x) for x in client.lrange(f"{NS}:events", 0, -1)]
    assert [r["seq"] for r in rows] == [1, 2]
    # LTRIM 上界生效
    for i in range(300):
        agent.emit("quote", {"code": "600000.SH", "time": "t", "price": 1.0, "i": i})
    tailer.pump(client, NS, keep=50)
    assert client.llen(f"{NS}:events") <= 50


def test_redis_transport_stream_events_dedup_by_seq(env):
    from connectors.transports.lowlatency import RedisTransport
    client, agent, root, tailer = env
    agent.emit("order", {"order_id": "7160", "code": "600036.SH",
                         "order_status": 56, "dealt": 100, "traded_price": 35.5})
    agent.emit("quote", {"code": "600036.SH", "time": "14:30:00", "price": 35.5,
                         "tick": {"lastPrice": 35.5}})
    tailer.pump(client, NS)
    t = RedisTransport("redis://x", namespace=NS, auth_token="tok")
    t._client = client                              # 注入 fakeredis（协议替身）
    evs = t.stream_events()
    kinds = [e.kind for e in evs]
    assert "order" in kinds and "quote" in kinds
    assert t.stream_events() == []                  # seq 游标：不二次投递
    spec = t.event_semantics()
    assert spec.max_latency_ms > 0                  # PUSH_WITH_GAP 也必须声明上界


def test_redis_transport_survives_agent_seq_rewind(env):
    """★★ A13：agent 重启让 ``seq`` 回卷时，新会话的事件**不得**被水位吞掉。

    队列里出现「旧会话 1..3」紧跟「新会话 1..」时，旧水位 3 会把新会话里
    ``seq ≤ 3`` 的记录全部过滤 ⇒ 事件通道事实上**变死**而指标全绿（与文件桥
    那个缺陷同源；中继那句「旧行重放无害」的注释正是把它掩盖掉的原因）。
    """
    from connectors.transports.lowlatency import RedisTransport
    client, agent, root, tailer = env
    for i in range(3):
        agent.emit("order", {"order_id": f"OLD-{i}", "order_status": 48})
    tailer.pump(client, NS)
    t = RedisTransport("redis://x", namespace=NS, auth_token="tok")
    t._client = client
    assert len(t.stream_events()) == 3
    assert t.stream_events() == []                  # 水位内：不二次投递

    agent._seq = 0                                  # ← 模拟策略重启：seq 回到 1
    agent.emit("trade", {"trade_id": "NEW-1", "price": 35.5, "volume": 100})
    tailer.pump(client, NS)

    evs = t.stream_events()
    assert [e.kind for e in evs] == ["trade"], (
        "agent 重启导致 seq 回卷，新会话事件被 seq 水位静默吞掉")
    assert evs[0].data["trade_id"] == "NEW-1"
    # 拐点只处理一次：不得反复重放整个新会话（重复投递比漏投递更难查）
    assert t.stream_events() == []


def test_redis_rewind_replays_from_the_boundary_not_from_zero(env):
    """★ 归零后必须**从拐点**重放，不能从 0 重放 —— 否则上个会话已投递过的事件会再发一遍。"""
    from connectors.transports.lowlatency import RedisTransport
    client, agent, root, tailer = env
    for i in range(3):
        agent.emit("order", {"order_id": f"OLD-{i}", "order_status": 48})
    tailer.pump(client, NS)
    t = RedisTransport("redis://x", namespace=NS, auth_token="tok")
    t._client = client
    assert len(t.stream_events()) == 3

    agent._seq = 0
    for i in range(2):
        agent.emit("trade", {"trade_id": f"NEW-{i}", "price": 1.0, "volume": 1})
    tailer.pump(client, NS)

    evs = t.stream_events()
    assert [e.data.get("trade_id") for e in evs] == ["NEW-0", "NEW-1"], (
        f"重放起点错了（应只重放新会话）：{[e.data for e in evs]}")
    assert all(e.kind == "trade" for e in evs), "上个会话的 order 被重复投递了"


def test_redis_transport_invoke_through_relay(env):
    """完整回路：后端 RedisTransport.invoke → 中继 → fake agent → 响应原路返回。"""
    from connectors.transports.lowlatency import RedisTransport
    client, agent, root, tailer = env
    t = RedisTransport("redis://x", namespace=NS, auth_token="tok",
                       poll_interval=0.01)
    t._client = client
    stop = threading.Event()
    th = threading.Thread(target=_relay_loop, args=(client, root, tailer, stop),
                          daemon=True)
    th.start()
    try:
        res = asyncio.run(t.invoke(WireRequest(op="PROBE", timeout=5.0)))
        assert res.ok and res.result["captured"]
        assert res.meta["agent"]["ver"] == "fake-1.0"
        assert abs(t.clock_offset_ms) < 5000
    finally:
        stop.set()
        th.join(timeout=2.0)


def test_relay_bad_request_is_dropped_not_crashing(env):
    client, agent, root, tailer = env
    client.rpush(f"{NS}:req", "{ 半份 JSON")
    assert relay.pump_once(root, client, NS, tailer) == 0

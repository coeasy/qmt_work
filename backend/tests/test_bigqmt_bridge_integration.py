"""大 QMT 桥的**端到端贯通**测试（真 transport + 真桥 + fake agent 端）。

与被测对象的关系
----------------
``fake_bigqmt_agent`` 站在 wire 协议**另一侧**（读 ``req/``、写 ``resp/``、
追加 ``events.ndjson``），替换的是「大 QMT 内置 Python」那一端。
``FileSignalTransport`` / ``BigQmtV1`` / ``GenericConnector`` / ``BigQmtBridge``
**全部是产线真家伙**。所以本文件证明的是：

    产线那条链路（REST → SignalRouter → ExecutionService → bridge.call(_locked)
    → gateway/adapter → connector → dialect → transport → agent）在**真协议**上
    从连接一直跑到事件回前端，没有断链。

为什么不能用「只测 transport」代替
----------------------------------
`test_bigqmt_marketdata_e2e.py` 已经覆盖了 transport 层。但真正造成线上
「大 QMT 接不进来」的四条断链**全都发生在桥这一层**（``await bridge.start()``
形态、``stop()`` 缺失、``start_pump_on`` 缺失、``conn.adapter.get_account()``
AttributeError）—— 只测 transport 一个都发现不了。
"""
import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from connectors.bigqmt_bridge import BigQmtBridge  # noqa: E402
from connectors.registry import resolve  # noqa: E402
from tests.fake_bigqmt_agent import FakeBigQmtAgent  # noqa: E402

#: 桥目录 / 探活预算都调小：本文件测的是「链路通不通」，不是超时行为。
_AGENT_TIMEOUT = 5.0


@pytest.fixture()
def wired(tmp_path):
    """起一个 fake agent + 一条**真实装配**的 BigQmtBridge（= Connection.adapter/bridge）。"""
    bridge_dir = str(tmp_path / "bridge")
    agent = FakeBigQmtAgent(bridge_dir, trading_enabled=True)
    agent.start()
    connector = resolve("qmt.big.bridge.file", bridge_dir=bridge_dir,
                        agent_timeout=_AGENT_TIMEOUT)
    bridge = BigQmtBridge(connector, "c-int", connector_key="qmt.big.bridge.file")
    try:
        yield agent, bridge
    finally:
        try:
            bridge.close()
        except Exception:  # noqa: BLE001
            pass
        agent.stop()


def _run(coro, timeout=_AGENT_TIMEOUT + 5):
    async def _main():
        return await asyncio.wait_for(coro, timeout=timeout)
    return asyncio.run(_main())


# ---------------------------------------------------------------------------
# 1) 生命周期：连上 / 探活 / 停机（两条停机路径都要能走）
# ---------------------------------------------------------------------------

def test_connect_sync_marks_bridge_connected(wired):
    """``connect_sync`` 是 BrokerManager 在同步线程里唯一的拉起入口。"""
    _agent, bridge = wired
    assert bridge.is_connected() is False
    bridge.connect_sync()
    assert bridge.is_connected() is True
    probe = bridge.test_connection()
    assert probe["connected"] is True


def test_async_start_matches_sync_start(wired):
    """``await bridge.start()``（phase_broker / health 的路径）必须同样可用。

    ★ 旧实现 ``start()`` 是同步的：``await`` 一个非协程只会抛 TypeError，
      在 ``_start_one`` 里被 ``except Exception`` 吞成「连接启动失败」。
    """
    _agent, bridge = wired

    async def _main():
        info = await bridge.start()
        assert bridge.is_connected() is True
        assert bridge.pump_running() is True      # start() 必须把泵拉起来
        return info

    info = asyncio.run(_main())
    assert isinstance(info, dict)


def test_graceful_shutdown_paths(wired):
    """优雅停机 ``await bridge.stop()`` 与手动断开 ``bridge.close()`` 都要能走。

    ★ ``shutdown.py`` 停机时统一 ``await conn.bridge.stop()``；缺这个方法会让
      整轮优雅停机抛 AttributeError。
    """
    _agent, bridge = wired
    bridge.connect_sync()

    async def _main():
        await bridge.start()
        await bridge.stop()
        assert bridge.pump_running() is False

    asyncio.run(_main())
    assert bridge.is_connected() is False
    bridge.close()          # 再走一次同步关闭，必须幂等
    bridge.close()


# ---------------------------------------------------------------------------
# 2) gateway 面：产线实际调用的每一个方法都要真的拿到数据
# ---------------------------------------------------------------------------

def test_gateway_surface_returns_real_shapes(wired):
    """逐个走一遍产线调用点，断言**容器类型与关键字段**都符合旧适配器契约。

    容器类型断言不是洁癖：``get_kline`` 曾写成 ``or {}``，空结果变 dict，
    下游 ``isinstance(bars, dict) and bars.get("code")`` 分支与 KlineCache
    落库会拿到错误的类型。
    """
    _agent, bridge = wired
    bridge.connect_sync()
    gw = bridge.gateway

    async def _main():
        orders = await bridge.call(gw.get_orders)
        assert isinstance(orders, list) and orders[0]["order_id"] == "7160"

        deals = await bridge.call(gw.get_deals)
        assert isinstance(deals, list) and deals[0]["trade_id"] == "50016562"

        acc = await bridge.call(gw.get_account)
        assert isinstance(acc, dict) and float(acc["cash"]) == 10000.0

        cash = await bridge.call(gw.get_cash)
        assert isinstance(cash, dict) and "cash" in cash

        pos = await bridge.call(gw.get_positions)
        assert isinstance(pos, list) and pos[0]["code"] == "600036.SH"

        kline = await bridge.call(gw.get_kline, "600036.SH", "1d", 3)
        assert isinstance(kline, list) and kline, (
            "空 K 线不足以证明链路通 —— 必须真的拿到行数据")
        assert {"open", "high", "low", "close", "volume"} <= set(kline[0])

        quote = await bridge.call(gw.get_quote, "600036.SH")
        assert isinstance(quote, dict) and quote

        tick = await bridge.call(gw.get_full_tick, ["600036.SH"])
        assert isinstance(tick, dict) and "600036.SH" in tick

        sectors = await bridge.call(gw.get_sector_list)
        assert sectors == ["沪深A股", "ETF", "指数"]

        stocks = await bridge.call(gw.get_stock_list, "沪深A股")
        assert [s["code"] for s in stocks] == ["600036.SH", "000001.SZ"]

        codes = await bridge.call(gw.get_sector_stocks, "沪深A股")
        assert codes == ["600036.SH", "000001.SZ"]

        cal = await bridge.call(gw.get_trading_calendar, "", "")
        assert cal == ["20260928", "20260929", "20260930"]

        det = await bridge.call(gw.get_instrument_detail, "600036.SH")
        assert det["InstrumentName"] == "测试标的"

        found = await bridge.call(gw.search_stocks, "平安")
        assert [s["code"] for s in found] == ["000001.SZ"]

        assert await bridge.call(gw.test_connection) is not None
        assert gw.is_connected() is True

    asyncio.run(_main())


def test_adapter_surface_returns_legacy_dicts(wired):
    """``conn.adapter.*`` 面（账户看板 / 同步引擎 / 订单守护走的路）必须可用。

    产线调用形态统一是 ``await bridge.call(conn.adapter.<m>)``（大 QMT 时
    ``adapter is bridge``，方法是 async；miniQMT 时是真适配器，方法同步、由
    bridge.call 丢线程池）。这里按同一形态跑，确保**两个槽位都对得上**。
    """
    _agent, bridge = wired
    bridge.connect_sync()

    async def _main():
        acc = await bridge.call(bridge.get_account)
        assert isinstance(acc, dict) and acc["account_id"] == "8888"
        pos = await bridge.call(bridge.get_positions)
        assert isinstance(pos, list) and pos[0]["avail"] == 100
        orders = await bridge.call(bridge.get_orders)
        assert isinstance(orders, list) and orders[0]["order_id"] == "7160"
        deals = await bridge.call(bridge.get_deals)
        assert isinstance(deals, list) and deals[0]["volume"] == 100

    asyncio.run(_main())


# ---------------------------------------------------------------------------
# 3) 交易链路：下单 / 撤单（走 call_locked，串行写）
# ---------------------------------------------------------------------------

def test_place_and_cancel_through_write_path(wired):
    """下单/撤单必须经 ``call_locked`` 并拿到柜台委托号（零 mock）。"""
    agent, bridge = wired
    bridge.connect_sync()

    async def _main():
        snap = await bridge.call_locked(
            bridge.gateway.place_order,
            "600036.SH", "buy", "limit", 35.5, 100, "qmt_work", "remark")
        assert snap["order_id"] == "7160"
        assert snap["code"] == 0
        assert snap["ok"] is True

        cancel = await bridge.call_locked(bridge.gateway.cancel_order, "7160")
        assert cancel["ok"] is True and cancel["order_id"] == "7160"

    asyncio.run(_main())
    ops = [e.get("op") for e in agent.received]
    assert "PLACE" in ops and "CANCEL_ORDER" in ops


def test_rejected_order_becomes_broker_error_not_silent_success(wired):
    """trading_enabled=false ⇒ 拒单必须以异常语义到达上层（400 而非 503/假成功）。

    零 mock 契约：柜台拒单绝不能返回一个「看起来像成功」的快照。

    ★ 断言的是**归因契约**而不是某个具体类名：真正决定 HTTP 状态的是
      ``gateway.signal_router._live`` 的
      ``isinstance(exc, (BrokerNotConnectedError, BrokerSDKError))`` 分流。
      所以这里同时断言「必须抛」+「必须落在 BrokerError 层级、且不被判为券商不可用」，
      这样换基类/加子类都不会悄悄把拒单变成 503（用户去重连，白折腾）。
    """
    from xtquant_client.base import BrokerError, BrokerNotConnectedError, BrokerSDKError

    agent, bridge = wired
    agent.trading_enabled = False
    bridge.connect_sync()

    async def _main():
        with pytest.raises(BrokerError) as ei:
            await bridge.call_locked(
                bridge.gateway.place_order,
                "600036.SH", "buy", "limit", 35.5, 100)
        return ei.value

    exc = asyncio.run(_main())
    # 真因必须保留（不能只剩「服务器内部错误」）
    assert "trading_enabled" in str(exc)
    # 拒单 ≠ 券商不可用 ⇒ 路由应给 400 + 真因，而不是 503 + 「去连接券商」
    assert not isinstance(exc, (BrokerNotConnectedError, BrokerSDKError)), (
        f"拒单被归因成「券商不可用」（{type(exc).__name__}）→ 前端会引导用户去重连，"
        "而真实原因是 agent 侧 trading_enabled=false")


# ---------------------------------------------------------------------------
# 4) 事件链路：agent 事件 → 事件泵 → WS 订阅者
# ---------------------------------------------------------------------------

def test_event_pump_delivers_agent_events_to_subscribers(wired):
    """agent 写出的委托事件必须经事件泵送到 ``on``/``ensure_handler`` 订阅者。

    ★ 这是「大 QMT 成交回报到不了前端」那条断链的回归判据：``start_pump_on``
      缺失 + ``pump_running()`` 谎报 ⇒ 泵从未启动，事件全部滞留在
      ``events.ndjson`` 里无人消费。
    """
    agent, bridge = wired
    bridge.connect_sync()
    got: list[dict] = []
    bridge.ensure_handler("order", lambda ev: got.append(ev))

    async def _main():
        await bridge.start()
        agent.emit("order", {"order_id": "7160", "order_status": 56,
                             "code": "600036.SH", "dealt": 100,
                             "traded_price": 35.5, "direction": "buy"})
        for _ in range(60):                 # 泵 1s 一轮，最多等 6s
            if got:
                break
            await asyncio.sleep(0.1)

    asyncio.run(_main())
    assert got, "事件泵没有把 agent 事件送达订阅者（大 QMT 事件链路断链）"
    assert got[0]["type"] == "order"
    assert got[0]["data"]["order_id"] == "7160"


def test_order_error_event_is_classified_by_ssot(wired):
    """废单/拒单必须经 ``order_status`` SSOT 判成 ``order_error``，不得硬编码整数。

    ★ 56 在 xtquant 是「全部成交」，在大 QMT 文档里被写成「已拒」——
      在这里硬编码数字会把真实成交判成废单，反之亦然。
    """
    agent, bridge = wired
    bridge.connect_sync()
    got: list[dict] = []
    bridge.ensure_handler("order_error", lambda ev: got.append(ev))

    async def _main():
        await bridge.start()
        # 57 = 废单（SSOT 里 REJECTED），与上面 56（已成）形成对照
        agent.emit("order", {"order_id": "9999", "order_status": 57,
                             "code": "600036.SH"})
        for _ in range(60):
            if got:
                break
            await asyncio.sleep(0.1)

    asyncio.run(_main())
    assert got, "废单未被识别为 order_error"
    assert got[0]["data"]["order_id"] == "9999"


# ---------------------------------------------------------------------------
# 5) 诊断面：四类根因必须可区分（读缓存、零 IO）
# ---------------------------------------------------------------------------

def test_connector_probe_exposes_root_cause_fields(wired):
    """``connector_probe`` 必须能区分「agent 未运行 / 函数缺失 / 路径不一致 / token 错」."""
    agent, bridge = wired
    bridge.connect_sync()

    async def _main():
        # 探针本身不做 IO；先跑一次真调用让 transport 缓存下 agent 元数据
        await bridge.call(bridge.gateway.test_connection)

    asyncio.run(_main())

    t0 = time.perf_counter()
    probe = bridge.connector_probe()
    assert time.perf_counter() - t0 < 0.2, "connector_probe 不得有耗时 IO"
    assert probe["transport"] == "file"
    assert probe["connector_key"] == "qmt.big.bridge.file"
    assert "supported_ops" in probe and probe["supported_ops"]
    assert "pump_running" in probe
    agent_meta = probe.get("agent") or {}
    assert agent_meta.get("bridge_dir") == agent.bridge_dir
    assert "actions" in probe or "funcs" in agent_meta

    # ★ 四类根因判定必须在服务端算好（人眼比对长路径极易看漏大小写/斜杠差异）
    assert probe["local_bridge_dir"] == agent.bridge_dir
    assert probe["bridge_dir_match"] is True
    assert probe["root_cause"] == "", (
        f"一切正常时 root_cause 必须为空串，却报了：{probe['root_cause']!r}")
    # 真实连接的 actions 面必须上浮（排障要看「两端 op 数对不对得上」）
    assert set(probe["actions"]) >= {"QUERY_KLINE", "QUERY_QUOTE", "PLACE"}
    assert probe["agent"]["funcs"], "funcs 清单是「注入函数缺失」判据，不能为空"
    # root_cause 只返回一个结论串（不给多值让 UI 猜）
    assert isinstance(probe["root_cause"], str)


def test_probe_on_a_wrong_bridge_dir_reports_agent_not_running(tmp_path):
    """bridge_dir 两端不一致 ⇒ 必须**如实超时**，不得返回空数据冒充成功。"""
    from connectors.transport import TransportError

    bridge_dir = str(tmp_path / "empty")
    connector = resolve("qmt.big.bridge.file", bridge_dir=bridge_dir,
                        agent_timeout=0.5)
    bridge = BigQmtBridge(connector, "c-nobody")
    try:
        with pytest.raises(TransportError) as ei:
            bridge.test_connection()
        msg = str(ei.value)
        assert "超时" in msg
        # 排查引导必须带上「桥目录两端一致 / 策略是否运行 / token」
        assert "bridge_dir" in msg
        assert bridge.is_connected() is False
        # ★ 未连上时探针仍必须给出**可读的根因**，而不是一片空白
        probe = bridge.connector_probe()
        assert probe["local_bridge_dir"] == bridge_dir
        assert probe["bridge_dir_match"] is None, (
            "agent 从未回报过 ⇒ 只能判「未知」，不能武断说不一致"
            "（把未知显示成不一致会让用户白改一次配置）")
        assert "未运行" in probe["root_cause"]
    finally:
        bridge.close()


def test_probe_json_written_for_diagnostics(wired):
    """探针面必须可 JSON 序列化（要进 /brokers/diagnostics 的响应体）。"""
    _agent, bridge = wired
    bridge.connect_sync()
    json.dumps(bridge.connector_probe(), ensure_ascii=False)
    json.dumps(bridge.version_profile(), ensure_ascii=False)


def test_fake_agent_declared_actions_match_its_implementations(tmp_path):
    """诊断面自检：fake agent 声明的 ``_ACTIONS`` 必须**逐条真的能跑**。

    这曾是一个真问题：``_ACTIONS`` 宣称 14 个 action，但 ``QUERY_KLINE`` /
    ``QUERY_QUOTE`` 没有实现 ⇒ 任何走 K 线/报价的链路在测试里都会撞上
    ``UnsupportedOp: 未知 action``。声明面与实现面不闭合时，测试其实是在一个
    **假能力面**上跑绿（真实 agent 有实现，fake 没有，或反之）。
    """
    from tests.fake_bigqmt_agent import _ACTIONS

    bridge_dir = str(tmp_path / "bridge")
    agent = FakeBigQmtAgent(bridge_dir, trading_enabled=True)
    agent.start()
    try:
        probe = agent._reply({"signal_id": "s", "op": "PROBE"})
        assert probe["ok"] is True
        assert set(probe["result"]["captured"]), "PROBE 必须如实上报捕获到的函数"
        assert set(agent.meta()["actions"]) == set(_ACTIONS)

        # 每个声明的 action 都必须有实现（不能落到「未知 action」兜底）
        payloads = {
            "PLACE": {"stock_code": "600036.SH", "volume": 100, "price": 35.5,
                      "side": "buy", "price_type": "limit"},
            "CANCEL_ORDER": {"order_id": "7160"},
            "QUERY_ASSET": {},
            "QUERY_POSITION": {},
            "QUERY_ORDER": {},
            "QUERY_TRADE": {},
            "QUERY_QUOTE": {"codes": ["600036.SH"]},
            "QUERY_KLINE": {"stock_code": "600036.SH", "period": "1d", "count": 3},
            "QUERY_STOCK_LIST": {},
            "QUERY_SECTOR_LIST": {},
            "QUERY_INSTRUMENT": {"code": "600036.SH"},
            "QUERY_CALENDAR": {},
            "SUB_QUOTE": {"codes": ["600036.SH"]},
        }
        assert set(payloads) | {"PROBE"} == set(_ACTIONS), (
            "本测试的 payload 表与 _ACTIONS 不同步 —— 新增 action 时要一起补")
        for action in _ACTIONS:
            if action == "PROBE":
                continue
            reply = agent._reply({"signal_id": "s", "op": action,
                                  "params": payloads[action]})
            assert reply["ok"] is True, (
                f"fake agent 声明支持 {action} 但实现缺失/报错：{reply.get('error')}")
    finally:
        agent.stop()


# ---------------------------------------------------------------------------
# 5) 行情订阅链：意图 → SUB_QUOTE → agent 转发 → 事件泵 → WS
# ---------------------------------------------------------------------------

def test_quote_subscription_reaches_agent_and_ticks_flow(wired):
    """产线唯一的订阅形态必须真的把 ``SUB_QUOTE`` 送到 agent。

    ★ 这是「大 QMT 连上了、利润/价格却一直不动」那条断链的回归判据。

      曾经的写法：``_BigQmtGateway.subscribe_quote`` 只写一句 ``log.debug``
      就当订阅完成。于是 ``sync._subscribe_to_qmt`` 顺利返回并
      ``_subscribed_codes.update(codes)``、日志打出「subscribed to broker」，
      而 **``SUB_QUOTE`` 从未发出**：

        agent.state.subscribed 恒为空 ⇒ ``quote_events`` 恒返回 0
        ⇒ events.ndjson 没有任何 type=quote 帧 ⇒ 事件泵每秒空转
        ⇒ ``sync.latest_quotes`` 永远只有订阅那一刻的种子值。

      连接状态、``/brokers/diagnostics``、health 检查、订阅日志**全是绿的**
      ——「绿灯是另一个 bug 遮出来的」的又一例。

      本测试用产线真实调用形态（``b.gateway.subscribe_quote(codes, cb)``，
      同步、不 await）驱动，然后只靠**事件泵自己的 1s 对账心跳**完成下发，
      以此保证「意图登记 → 泵内对账」这条链路本身是通的（而不是靠测试手动踢一脚）。
    """
    agent, bridge = wired
    bridge.connect_sync()
    got: list[dict] = []

    async def _main():
        await bridge.start()
        bridge.ensure_handler("quote", got.append)
        # ★ 产线唯一形态：sync._subscribe_to_qmt 就是这么调的（同步、不 await）。
        bridge.gateway.subscribe_quote(["600036.SH", "600519.SH"], lambda e: None)
        for _ in range(40):                 # 泵 1s 一轮，最多等 4s
            if agent.subscribed:
                break
            await asyncio.sleep(0.1)
        assert sorted(agent.subscribed) == ["600036.SH", "600519.SH"], (
            "SUB_QUOTE 从未送达 agent ⇒ agent 永远不会转发任何行情，"
            "界面会永远停在种子价，而订阅日志是绿的")
        assert agent.meta()["subscribed"] == 2, "agent 必须诚实上报已订阅数量"

        # agent 现在开始轮询转发：写一帧 quote，事件泵必须送到 WS 订阅者。
        agent.emit("quote", {"code": "600036.SH", "last": 35.5, "volume": 1000})
        for _ in range(40):
            if got:
                break
            await asyncio.sleep(0.1)

    asyncio.run(_main())
    assert got, "订阅生效后 agent 的行情帧没有经事件泵送达订阅者"
    assert got[0]["type"] == "quote"
    assert got[0]["data"]["code"] == "600036.SH"
    assert got[0]["data"]["last"] == 35.5

    # 诊断面必须能区分「本端要了什么」与「已经下发成功什么」。
    probe = bridge.connector_probe()
    assert probe["quote_wanted"] == 2
    assert probe["quote_applied"] == 2
    assert probe["quote_sync_error"] == ""
    assert probe["subscribed"] == 2


def test_subscribe_quote_records_intent_without_any_io(wired):
    """``subscribe_quote`` 是**同步契约** ⇒ 必须零 IO、立即返回。

    ★ 反例（本项目的实际诱惑）：在这里 ``_run_sync`` 把 ``SUB_QUOTE`` 发下去。
      ``sync._subscribe_to_qmt`` 跑在**事件循环线程**上，而 ``_run_sync`` 检测到
      运行中的 loop 时会换成临时线程并 ``fut.result(timeout=_SYNC_TIMEOUT)``
      —— ``_SYNC_TIMEOUT=30``。agent 一旦没起来，一次订阅就把整个后端冻 30 秒。

    所以本测试刻意**不连接**：此刻 agent 不可达，任何真 IO 都会失败或阻塞。
    """
    _agent, bridge = wired
    assert bridge.is_connected() is False
    t0 = time.monotonic()
    bridge.gateway.subscribe_quote(["600036.SH"], lambda e: None)   # 不得抛、不得等
    elapsed = time.monotonic() - t0
    assert elapsed < 0.5, f"subscribe_quote 阻塞了 {elapsed:.2f}s，同步契约被破坏"
    assert bridge._want_quotes == {"600036.SH"}
    assert bridge.connector_probe()["quote_wanted"] == 1


def test_quote_reconcile_failure_is_visible_not_silently_green(tmp_path):
    """下发失败必须留痕（``quote_sync_error``），不能悄悄当成功。

    ``_applied_quotes`` 不推进 ⇒ 诊断面的 ``quote_applied`` 停在 0；
    同时必须**退避**（否则每轮心跳都重试、每轮都要等满 ``agent_timeout``，
    会把事件泵的循环周期一起拖长）。
    """
    bridge_dir = str(tmp_path / "nobody-home")      # 没有 agent 在读这个目录
    connector = resolve("qmt.big.bridge.file", bridge_dir=bridge_dir,
                        agent_timeout=0.5)
    bridge = BigQmtBridge(connector, "c-noagent",
                          connector_key="qmt.big.bridge.file")
    try:
        bridge.gateway.subscribe_quote(["600036.SH", "600519.SH"], None)
        # 泵会把这个异常记进 _quote_sync_error 并继续跑；这里直接驱动一轮对账。
        with pytest.raises(Exception):
            asyncio.run(bridge._reconcile_quote_subscriptions())
        assert bridge._applied_quotes == set(), "下发失败却标记成已下发"
        probe = bridge.connector_probe()
        assert probe["quote_wanted"] == 2
        assert probe["quote_applied"] == 0
        assert probe["quote_sync_error"], (
            "下发失败必须留下可读原因，否则诊断面只能看到 0 == 0 的静默绿灯")
        # 失败后必须退避：紧接着的下一轮心跳不得再打一次（否则每轮都要等满
        # agent_timeout 才返回，会把事件泵的循环周期一起拖长）。
        asyncio.run(bridge._reconcile_quote_subscriptions())   # 不得再次抛/再次打
        assert bridge._applied_quotes == set()
    finally:
        bridge.close()


def test_quote_reconcile_is_idempotent_but_immediate_on_new_codes():
    """对账幂等：窗口内不重复下发；但**新增标的必须立即下发**（不等窗口）。

    ★ 刻意用计数壳而不是真 agent：真 agent 那一版要跟事件泵（1s 心跳）抢时序，
      断言「没有第二次下发」会随机失败 —— 测试自身不稳比没有测试更坏。
    """

    class _CountingConnector:
        def __init__(self):
            self.calls: list[list[str]] = []
            self.transport = None

        async def subscribe_quote_polling(self, codes):
            self.calls.append(sorted(codes))
            return {"subscribed": sorted(codes), "mode": "poll_forward"}

    conn = _CountingConnector()
    bridge = BigQmtBridge(conn, "c-count", connector_key="qmt.big.bridge.file")

    async def _main():
        bridge.gateway.subscribe_quote(["600036.SH", "600519.SH"], None)
        await bridge._reconcile_quote_subscriptions()
        assert conn.calls == [["600036.SH", "600519.SH"]], "首次订阅必须下发"
        await bridge._reconcile_quote_subscriptions()
        assert len(conn.calls) == 1, "窗口内重复对账不得再打一次（幂等）"
        # 意图变了 ⇒ 必须**立即**下发，且整集下发（含此前的 code）
        bridge.gateway.subscribe_quote(["000001.SZ"], None)
        await bridge._reconcile_quote_subscriptions()
        assert conn.calls[-1] == ["000001.SZ", "600036.SH", "600519.SH"], (
            "新增标的必须立即下发，且按整集（agent 侧是 set.update）")
        assert bridge.connector_probe()["quote_applied"] == 3

    asyncio.run(_main())
    bridge.close()


def test_quote_reconcile_self_heals_after_agent_restart(wired, monkeypatch):
    """agent 重启（自身订阅集清零）后必须**自动补发**，否则行情静默断流。

    ★ 只比 ``_applied_quotes != _want_quotes`` 是不够的：agent 是独立进程，
      重启后它自己的状态清零，而本端集合还在 —— 那会在「永不重发」里静默失联。
      也**不能**靠 agent 自报的 ``meta().subscribed``：``transport.agent_meta``
      是最近一次响应信封的缓存，而事件泵只读 events.ndjson、一个请求都不发，
      缓存根本没有机会刷新。所以兜底必须是**时间**（盲重发一次，幂等）。
    """
    # 把 60s 的盲重发窗口压到 0.2s，让本测试不用真的等一分钟。
    monkeypatch.setattr("connectors.bigqmt_bridge._QUOTE_RESYNC_SECONDS", 0.2)
    agent, bridge = wired
    bridge.connect_sync()

    async def _main():
        await bridge.start()
        bridge.gateway.subscribe_quote(["600036.SH"], lambda e: None)
        for _ in range(40):
            if agent.subscribed:
                break
            await asyncio.sleep(0.1)
        assert sorted(agent.subscribed) == ["600036.SH"]

        # 模拟 agent 重启：它自己的订阅集清零，本端不做任何事，等盲重发兜底。
        agent.subscribed.clear()
        for _ in range(40):
            if agent.subscribed:
                break
            await asyncio.sleep(0.1)

    asyncio.run(_main())
    assert sorted(agent.subscribed) == ["600036.SH"], (
        "agent 重启后订阅没有自动补发 ⇒ 行情会静默断流且毫无提示")
    assert bridge._applied_quotes == {"600036.SH"}


def test_bigqmt_gateway_has_no_lying_unsubscribe_quote(wired):
    """``unsubscribe_quote`` 必须**保持不存在**：agent 没有 UNSUB op。

    ``sync._unsubscribe_from_qmt`` 用 ``getattr(b.gateway, "unsubscribe_quote", None)``
    探测。若为了「接口完整」加一个只改本端集合的空实现，它就会在日志里打出
    「unsubscribed from broker (refcount=0)」而 agent 仍在转发 —— 又是谎报。
    """
    _agent, bridge = wired
    assert not hasattr(bridge.gateway, "unsubscribe_quote")


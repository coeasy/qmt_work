"""连接器异常层级 × HTTP 归因 的对账测试（跨层，纯静态 + 纯函数）。

为什么需要这个文件
------------------
``connectors`` 包（方言 × 传输组合）引入了自己的一套异常：
``ConnectorError`` / ``TransportError`` / ``UnsupportedOp``。
而 HTTP 层只认 ``xtquant_client.base`` 那一套（``BrokerError`` 及其派生）。

这两套层级曾经**完全没有接缝**：

    TransportError(RuntimeError)          ← 与 BrokerError 无关
    app/routes/_common._call              ← 只 except BrokerError / TimeoutError
    gateway/signal_router._live           ← 只按 BrokerNotConnected/BrokerSDKError 分流
    app/routes/account.py                 ← 只 except BrokerError

后果：大 QMT 桥的任何传输故障（agent 未运行 / bridge_dir 两端不一致 / 超时）
都会穿透到 FastAPI 兜底 → **HTTP 500「服务器内部错误」**，
而 ``connectors/transport.py`` 的头部注释白纸黑字写着「上层映射为
BrokerNotConnectedError → HTTP 503 + 引导」—— 契约写了，实现没接。

本文件把那个接缝钉住：任何人把基类改回 ``RuntimeError``，这里立刻红。
"""
import inspect
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from connectors.dialects import Ops, UnsupportedOp  # noqa: E402
from connectors.ports import ConnectorError  # noqa: E402
from connectors.transport import TransportError  # noqa: E402
from xtquant_client.base import (  # noqa: E402
    BrokerError,
    BrokerNotConnectedError,
    BrokerSDKError,
)


# ---------------------------------------------------------------------------
# 1) 层级本身：连接器异常必须是券商异常的后代
# ---------------------------------------------------------------------------

def test_connector_error_is_a_broker_error():
    """``ConnectorError`` 必须继承 ``BrokerError``，否则路由层看不见它。

    路由层的归类语句全是 ``except BrokerError`` / ``isinstance(..., BrokerError)``。
    本类一旦独立，异常直接落到框架兜底 → 500（真因丢失）。
    """
    assert issubclass(ConnectorError, BrokerError), (
        "ConnectorError 脱离了 BrokerError 层级 ⇒ 从 bridge 冒出的连接器异常"
        "会变成 HTTP 500，真实原因（bridge_dir 不一致 / agent 未运行）被吞掉")


def test_transport_error_is_classified_as_broker_unavailable():
    """``TransportError`` 必须同时是 ConnectorError 与 BrokerNotConnectedError。

    两个语义都要成立：
      * 是连接器错误（同根，统一捕获能命中）；
      * 是「未连接」错误 —— ``signal_router._live`` 据此置
        ``broker_unavailable=True`` → 路由返 503 + 「去连接券商」引导。
        若只当普通 BrokerError，传输故障会被报成 400「请求非法」。
    """
    assert issubclass(TransportError, ConnectorError)
    assert issubclass(TransportError, BrokerNotConnectedError), (
        "TransportError 不再被归为「券商不可用」⇒ agent 没运行时前端会看到 400，"
        "提示用户去改参数，而正确动作是去启动/连接券商客户端")


def test_unsupported_op_is_a_broker_error():
    """``UnsupportedOp`` 走真实业务路径（如 ``subscribe_quote`` 回调不可跨线），
    独立于券商层级时同样会变成 500。"""
    assert issubclass(UnsupportedOp, BrokerError)


def test_all_connector_errors_are_constructible_with_a_single_message():
    """重建路径 ``_rebuild`` 用 ``cls(message)`` 单参构造；签名不兼容就会静默降级。"""
    for cls in (ConnectorError, TransportError, UnsupportedOp):
        exc = cls("真因文案")
        assert "真因文案" in str(exc)
        assert isinstance(exc, BrokerError)


def test_broker_error_extras_survive_on_connector_errors():
    """``BrokerError`` 的 ``needs_action`` / ``brief`` 派生属性必须同样可用
    （健康监控按它决定退避节奏；缺失会让无权限类故障被无限快速重试）。"""
    exc = TransportError("严格连接校验未通过\n第二行诊断")
    assert exc.needs_action is True
    assert exc.brief == "严格连接校验未通过"
    plain = TransportError("对端超时")
    assert plain.needs_action is False
    assert plain.brief == "对端超时"


# ---------------------------------------------------------------------------
# 2) 端到端归因：模拟路由层的两条归类语句，断言不会落到 500
# ---------------------------------------------------------------------------

def _classify_like_call_helper(exc: Exception) -> int:
    """复刻 ``app/routes/_common.py::_call`` 的归类（源码级一致，不靠人肉同步）。"""
    import asyncio

    if isinstance(exc, asyncio.TimeoutError):
        return 503
    if isinstance(exc, BrokerError):
        return 503
    return 500


def _classify_like_signal_router(exc: Exception) -> dict:
    """复刻 ``gateway/signal_router.py::_live`` 的失败信封。"""
    return {"ok": False, "reason": str(exc), "mode": "live",
            "error_type": type(exc).__name__,
            "broker_unavailable": isinstance(
                exc, (BrokerNotConnectedError, BrokerSDKError))}


@pytest.mark.parametrize("exc", [
    TransportError("agent 未运行（bridge_dir 超时）"),
    ConnectorError("QUERY_POSITION 方言解析失败: missing key"),
    UnsupportedOp("本传输（file）不支持回调式实时订阅"),
])
def test_no_connector_error_ever_becomes_http_500(exc):
    """任何连接器异常都必须被 ``_call`` 归类成业务信封，绝不是 500。"""
    assert _classify_like_call_helper(exc) == 503, (
        f"{type(exc).__name__} 会穿透到 FastAPI 兜底 → 500「服务器内部错误」")


def test_transport_failure_is_503_signal_but_rejection_is_400_signal():
    """signal_router 的分流：连不上 → 503 引导；连上了但被拒 → 400 真因。"""
    transport = TransportError("agent 未运行")
    assert _classify_like_signal_router(transport)["broker_unavailable"] is True

    # 业务失败（_rebuild('Rejected', ...) 的产物）必须**不是** broker_unavailable
    rejected = ConnectorError("agent 侧 trading_enabled=false，拒绝下单")
    env = _classify_like_signal_router(rejected)
    assert env["broker_unavailable"] is False, (
        "拒单被标成「券商不可用」⇒ 路由返回 503 引导重连，"
        "而用户真正需要做的是去 agent_config.json 打开 trading_enabled")


# ---------------------------------------------------------------------------
# 3) error_type 词表对账：生产端产出的每个字面量都要能在 _rebuild 里找到归宿
# ---------------------------------------------------------------------------

def test_rebuild_maps_every_error_type_the_agents_can_emit():
    """两端 error_type 词表必须闭合。

    生产端可能的取值：
      * ``XtQuantV1.classify_error`` → BrokerNotConnected / BrokerSDKError /
        BrokerError / Timeout / ConnectorError
      * ``BigQmtV1.classify_error``  → BrokerNotConnected / BrokerSDKError /
        Timeout / BrokerError
      * ``agent_bigqmt/qmt_api.py`` 的 ActionError → Rejected / BrokerError /
        BrokerSDKError / Unsupported / Expired
    漏映射 ⇒ 静默降级为 ConnectorError（能兜住），但**归因会失真**：
    Timeout 应当 503，映射成 ConnectorError 后 signal_router 会报 400。
    """
    from connectors.generic import _rebuild

    # (error_type, 期望的 HTTP 归因) —— 400 只留给「券商可用但拒绝 / 请求无法按现状服务」
    cases = {
        "BrokerError": "reject",
        "Rejected": "reject",
        "Expired": "reject",
        # 能力缺口：请求本身无法按当前传输/配置服务。真因文案自带出路指引
        # （「请改用 EventPort / 切到 xtquant 直连」），归 400 + 真因更利于排障。
        "Unsupported": "reject",
        "BrokerNotConnected": "unavailable",
        "BrokerSDKError": "unavailable",
        "Timeout": "unavailable",
    }
    for error_type, expect in cases.items():
        exc = _rebuild(error_type, "真因文案")
        assert isinstance(exc, BrokerError), (
            f"{error_type} 重建出的 {type(exc).__name__} 不是 BrokerError ⇒ 会 500")
        assert "真因文案" in str(exc), f"{error_type} 重建丢失了原始消息"
        unavailable = isinstance(exc, (BrokerNotConnectedError, BrokerSDKError))
        if expect == "reject":
            assert not unavailable, (
                f"{error_type} 是「券商可用但拒绝」，却被归成「券商不可用」→ 前端误导")
        else:
            assert unavailable, (
                f"{error_type} 是「连不上/能力缺失」，却按 400 真因返回 → 前端误导")


def test_rebuild_defaults_to_connector_error_for_unknown_types():
    """未知 error_type 必须向前兼容：仍返回 BrokerError 后代，绝不 500。"""
    from connectors.generic import _rebuild

    exc = _rebuild("SomeFutureType", "未来新增的类型")
    assert isinstance(exc, BrokerError)
    assert "未来新增的类型" in str(exc)


def test_dialect_classify_error_outputs_are_all_in_the_table():
    """两个方言的 ``classify_error`` 产出的字面量必须都在 ``_rebuild`` 表里有归宿。

    这条是**反向**对账：只用上面那张期望表会漏掉「方言新增了一个 error_type
    但没同步到 _rebuild」。

    ★ 只取 ``return ("X", msg)`` 元组的首元素 —— 不能抓函数里所有字符串字面量：
      ``classify_error`` 里 ``if name == "BrokerNotConnectedError":`` 之类的
      **入参比较**文案与出参同名但语义相反，一起抓会把断言变成噪声
      （噪声规则迟早被人关掉，比没有规则更坏）。
    """
    import ast
    import textwrap

    from connectors.dialects import get_dialect

    known = {"BrokerError", "Rejected", "Expired", "BrokerNotConnected",
             "BrokerSDKError", "Timeout", "Unsupported", "ConnectorError"}
    checked = 0
    for name in ("xtquant.v1", "bigqmt.v1", "ptrade.v1"):
        cls = type(get_dialect(name))
        fn = getattr(cls, "classify_error", None)
        if fn is None:
            continue
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
        produced = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Return) or node.value is None:
                continue
            value = node.value
            first = value.elts[0] if isinstance(value, ast.Tuple) and value.elts else value
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                produced.add(first.value)
        assert produced, f"{name}.classify_error 没有任何字面量返回，测试失效"
        checked += 1
        unknown = produced - known
        assert not unknown, (
            f"{name}.classify_error 产出了 {sorted(unknown)}，"
            "但 connectors.generic._rebuild 的映射表里没有 —— 归因会失真")
    assert checked >= 2, "至少 xtquant.v1 与 bigqmt.v1 都要被扫到（否则测试是空转）"


# ---------------------------------------------------------------------------
# 4) 容器类型与 raw 保真：canonicalize 不得丢字段
# ---------------------------------------------------------------------------

def test_position_snapshot_preserves_raw_dict():
    """``position_snapshot`` 必须保留 ``raw``。

    大 QMT 桥的 ``_raw_of()`` 返回 ``snap.raw`` 给旧适配器面；下游按 dict 键取值
    （``p["code"]`` / ``p["avail"]`` / ``p["cost"]``）。
    曾漏掉 ``raw=dict(raw)`` ⇒ 持仓恒为 ``[{}]``，**不抛异常**，
    账户看板显示空仓、``enrich_positions`` 拿不到代码，排查时看不到任何报错。
    """
    from connectors.canonicalize import position_snapshot

    row = {"code": "600036.SH", "name": "招商银行", "volume": 200,
           "avail": 100, "cost": 34.2, "market_value": 7100.0}
    snap = position_snapshot(row)
    assert snap.raw == row, "持仓 raw 丢失 ⇒ 旧适配器面会拿到空 dict"
    assert snap.name == "招商银行"
    assert snap.instrument.code == "600036"
    assert snap.quantity == 200
    assert snap.available_quantity == 100
    assert snap.market_value == 7100.0


def test_every_snapshot_type_preserves_raw():
    """四种 canonical 快照都必须保真 raw —— 一张表钉住，防再漏。"""
    from connectors.canonicalize import (
        account_snapshot,
        order_snapshot,
        position_snapshot,
        trade_snapshot,
    )

    cases = [
        (account_snapshot, {"account_id": "8888", "cash": 1.0}),
        (order_snapshot, {"order_id": "1", "code": "600036.SH", "status": "已报"}),
        (trade_snapshot, {"trade_id": "2", "code": "600036.SH", "volume": 100}),
        (position_snapshot, {"code": "600036.SH", "volume": 200}),
    ]
    for fn, row in cases:
        snap = fn(row)
        assert getattr(snap, "raw", None) == row, (
            f"{fn.__name__} 丢失了 raw —— 下游按 dict 键取值会拿到空对象")


def test_position_snapshot_tolerates_non_mapping():
    """非 Mapping 输入（某家券商返回 None）不得抛异常，也不得产生脏 raw。"""
    from connectors.canonicalize import position_snapshot

    snap = position_snapshot(None)
    assert snap.raw == {}
    assert snap.quantity == 0


def test_ops_and_unsupported_op_are_importable_from_the_dialects_package():
    """``UnsupportedOp`` 的公开导入路径不能因改基类而断（多处 import 依赖它）。"""
    from connectors.dialects import UnsupportedOp as U1
    from connectors.dialects.base import UnsupportedOp as U2

    assert U1 is U2
    assert isinstance(Ops.PLACE_ORDER, str)


# ---------------------------------------------------------------------------
# 5) QmtConnector：不得把券商异常的**子类身份**压平（大小 QMT 同一套契约）
# ---------------------------------------------------------------------------

class _StubAdapter:
    """结构化鸭子类型：QmtConnector 只用它的若干同步方法。"""

    broker_name = "stub"
    client_version = "test"

    def is_connected(self):
        return False

    def place_order(self, *a, **kw):
        return {}

    def cancel_order(self, *a, **kw):
        return {}


class _RaisingBridge:
    def __init__(self, exc: BaseException):
        self._exc = exc

    async def call(self, fn, *a, **kw):
        raise self._exc

    async def call_locked(self, fn, *a, **kw):
        raise self._exc

    async def start(self):
        raise self._exc

    async def stop(self):
        return None


def _order_request():
    from connectors.ports import InstrumentId, OrderRequest

    return OrderRequest(instrument=InstrumentId(code="600036", exchange="SH"),
                        side="buy", order_type="limit", price=35.5, quantity=100)


@pytest.mark.parametrize("exc", [
    BrokerNotConnectedError("券商客户端未登录"),
    BrokerSDKError("xtquant", "未安装"),
])
def test_qmt_connector_does_not_flatten_broker_error_subclasses(exc):
    """``QmtConnector`` 必须保留券商异常的**子类身份**（不能压成通用 ConnectorError）。

    曾用 ``raise ConnectorError(str(exc)) from exc`` 归一化，那是**有损**的：
    ``signal_router._live`` 判 503 的依据就是
    ``isinstance(exc, (BrokerNotConnectedError, BrokerSDKError))``。
    压平之后「客户端没连上」会被报成 **400「风控/执行拒绝」**，
    用户被引导去改下单参数，而正确动作是去连接/登录券商客户端。
    """
    import asyncio

    from connectors.qmt import QmtConnector

    conn = QmtConnector(_StubAdapter())
    conn.bridge = _RaisingBridge(exc)

    with pytest.raises(type(exc)) as ei:
        asyncio.run(conn.place_order(_order_request()))
    assert str(ei.value) == str(exc), "重抛时真因文案必须原样保留"
    assert _classify_like_signal_router(ei.value)["broker_unavailable"] is True


def test_qmt_connector_wraps_non_broker_errors_once():
    """非券商层异常（OSError）才需要包裹，且必须带 ``__cause__`` 以便追溯。"""
    import asyncio

    from connectors.qmt import QmtConnector

    conn = QmtConnector(_StubAdapter())
    conn.bridge = _RaisingBridge(OSError("管道断裂"))
    with pytest.raises(ConnectorError) as ei:
        asyncio.run(conn.place_order(_order_request()))
    assert isinstance(ei.value.__cause__, OSError)


def test_qmt_connector_start_keeps_taxonomy_and_context():
    """启动失败同样保留层级；包裹时保留 ``QMT connector start failed`` 上下文。"""
    import asyncio

    from connectors.qmt import QmtConnector

    conn = QmtConnector(_StubAdapter())
    conn.bridge = _RaisingBridge(BrokerNotConnectedError("客户端未登录"))
    with pytest.raises(BrokerNotConnectedError):
        asyncio.run(conn.start())

    conn.bridge = _RaisingBridge(RuntimeError("奇怪的失败"))
    with pytest.raises(ConnectorError) as ei:
        asyncio.run(conn.start())
    assert "QMT connector start failed" in str(ei.value)


def test_qmt_connector_get_kline_passes_by_keyword():
    """``QmtConnector.get_kline`` 必须按**关键字**传参。

    端口与 adapter 的两个签名同名但顺序不同：
      端口   ``(code, period, count, adjust, start, end)``
      adapter ``(code, period, count, start, end, adjust)``
    按位置传时第 4 个实参含义不同 —— 一旦任一签名调整顺序，
    ``adjust`` 会被静默当成起始日期送进取数接口，返回错区间数据且**不报错**。
    """
    import asyncio

    from connectors.qmt import QmtConnector

    seen: dict = {}

    class _RecorderAdapter(_StubAdapter):
        def get_kline(self, code, period, count, start="", end="", adjust=None):
            seen.update(code=code, period=period, count=count,
                        start=start, end=end, adjust=adjust)
            return []

    class _DirectBridge:
        async def call(self, fn, *a, **kw):
            return fn(*a, **kw)

        async def call_locked(self, fn, *a, **kw):
            return fn(*a, **kw)

    conn = QmtConnector(_RecorderAdapter())
    conn.bridge = _DirectBridge()
    out = asyncio.run(conn.get_kline(
        "600036.SH", "1d", 5, adjust="qfq",
        start="20260101", end="20260131"))
    assert out == []
    assert seen == {"code": "600036.SH", "period": "1d", "count": 5,
                    "start": "20260101", "end": "20260131", "adjust": "qfq"}, (
        "get_kline 的参数映射错位 ⇒ 复权口径/日期区间会被静默互换")


def test_gateway_get_kline_signature_matches_adapter_shape():
    """大小 QMT 两条路径的 ``get_kline`` 关键字名必须一致，否则调用方无法统一。"""
    import inspect

    from connectors.bigqmt_bridge import BigQmtBridge
    from xtquant_client.gateway import XTQuantGateway

    gw_params = list(inspect.signature(XTQuantGateway.get_kline).parameters)
    bridge_params = list(inspect.signature(BigQmtBridge.get_kline).parameters)
    assert gw_params == bridge_params, (
        f"真适配器 {gw_params} 与桥 {bridge_params} 的关键字名/顺序不一致 ⇒ "
        "同一段调用代码在两个券商上行为不同")
    assert bridge_params[:4] == ["self", "code", "period", "count"]

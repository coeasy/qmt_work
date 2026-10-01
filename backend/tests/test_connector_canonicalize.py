"""P0 接线：方言 raw dict → canonical 快照的转换不变量（docs/UNIFIED_TRADING_ABSTRACTION.md §5.2）。

这里盯的是三条铁律，任何一条被破坏都会直接导致产线事故：
 1. 状态只走 SSOT、原始值仅落 raw_status 供诊断；
 2. 拿不到柜台委托号/状态不成立时绝不置 accepted=True（零 mock）；
 3. 畸形输入返回诚实快照而不是抛异常或伪造内容。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from connectors.canonicalize import (  # noqa: E402
    account_snapshot,
    order_snapshot,
    position_snapshot,
    trade_snapshot,
)
from connectors.ports import (  # noqa: E402
    CapabilityLevel,
    CapabilitySet,
    EventSemantics,
    EventSemanticsSpec,
    InstrumentId,
    OrderSnapshot,
)
from xtquant_client.order_status import UNKNOWN  # noqa: E402

XT_ORDER = {
    "order_id": "7160", "code": "600036.SH", "direction": "BUY",
    "price": 35.5, "volume": 100, "dealt": 100, "status": 56,
}


def test_status_normalized_via_ssot():
    """56 → SSOT 标准词表；原始值留在 raw_status 仅供诊断。"""
    snap = order_snapshot(XT_ORDER)
    assert snap.status == "filled"
    assert snap.raw_status == "56"
    assert snap.is_terminal is True


def test_rejected_status_is_not_accepted():
    """废单（57）即使带委托号也算未受理 —— 判据是「有号 且 未被拒」。"""
    raw = dict(XT_ORDER, status=57)
    snap = order_snapshot(raw)
    assert snap.status == "rejected"
    assert snap.accepted is False


def test_missing_broker_order_id_is_not_accepted():
    """零 mock：拿不到柜台委托号就绝不置 True，由上层决定重查还是报错。"""
    snap = order_snapshot({"code": "600036.SH", "status": 50})
    assert snap.broker_order_id == ""
    assert snap.accepted is False


def test_unknown_status_stays_unknown_not_silent_success():
    snap = order_snapshot({"order_id": "7160", "status": "WEIRD_NEW_CODE"})
    assert snap.status == UNKNOWN
    assert snap.accepted is False


def test_non_mapping_input_yields_honest_snapshot():
    """方言返回 None/对象时不能把服务打崩，也不能编造字段。"""
    for bad in (None, [], "x", 42):
        snap = order_snapshot(bad)
        assert snap.status == UNKNOWN
        assert snap.accepted is False
        assert snap.raw == {}


def test_multi_dialect_field_names():
    """easytrader 风格键名也必须落到同一组 canonical 字段。"""
    snap = order_snapshot({
        "stock_code": "600036.SH",
        "order_sysid": "9001",
        "order_status": 50,
        "order_volume": 200,
        "traded_volume": 0,
    })
    assert snap.instrument == InstrumentId(code="600036", exchange="SH")
    assert snap.broker_order_id == "9001"
    assert snap.requested_quantity == 200
    assert snap.filled_quantity == 0
    assert snap.accepted is True


def test_code_without_exchange_is_not_guessed():
    """无交易所后缀时不猜市场：猜错会拼出看似合法实则错误的代码。"""
    snap = order_snapshot({"order_id": "1", "code": "600036"})
    assert snap.instrument == InstrumentId(code="600036")
    assert snap.instrument.canonical == "600036"


def test_trade_snapshot_is_filled_by_definition():
    """成交行本身就是「已发生成交」的证据，此处填 FILLED 不算臆造。"""
    snap = trade_snapshot({"order_id": "7160", "code": "600036.SH",
                           "volume": 100, "price": 35.5})
    assert snap.status == "filled"
    assert snap.accepted is True
    assert snap.avg_price == 35.5


def test_position_and_account_snapshots_keep_raw_for_forensics():
    pos = position_snapshot({"code": "600036.SH", "volume": 100,
                             "can_use_volume": 100, "open_price": 35.2,
                             "market_value": 3520.0})
    assert pos.available_quantity == 100
    assert pos.average_cost == 35.2

    acc = account_snapshot({"account_id": "8888", "cash": 1000.0,
                            "assets": 4520.0})
    assert acc.account_id == "8888"
    assert acc.assets == 4520.0
    # raw 保留全量：各家券商资金口径不一，排查时上层要回看原始值。
    assert acc.raw["cash"] == 1000.0


def test_order_snapshot_default_is_honest_unknown():
    snap = OrderSnapshot()
    assert snap.status == UNKNOWN
    assert snap.accepted is False
    assert snap.is_terminal is False


def test_capability_set_treats_unknown_as_not_supported():
    """UNKNOWN 是一等公民：只有显式 SUPPORTED 才可用（大 QMT 单点探测教训）。"""
    caps = CapabilitySet(capabilities=())
    assert caps.level_of("bigqmt.credit") is CapabilityLevel.UNKNOWN
    assert caps.is_supported("bigqmt.credit") is False
    assert caps.is_definitely_unsupported("bigqmt.credit") is False


def test_event_semantics_declares_latency_bound():
    """POLL_DIFF 必须声明延迟上界：上层据此设置超时守护，否则会误撤未回报的单。"""
    spec = EventSemanticsSpec(semantics=EventSemantics.POLL_DIFF, max_latency_ms=1000,
                              poll_interval_ms=500)
    assert spec.semantics is EventSemantics.POLL_DIFF
    assert spec.max_latency_ms > 0

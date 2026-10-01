"""V4 Phase 0-c：``QMT_USE_PORTS`` 灰度接线的双跑一致性。

这是接线能否摘掉旧路径的判据：**同一个 adapter、同一笔委托，两条路径必须产出
同形的回执**。默认端口关闭 ⇒ 产线行为与接线前**逐字节一致**（回归保护）。
"""
import asyncio
import importlib
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from xtquant_client.gateway import XTQuantBridge  # noqa: E402


class _FakeGateway:
    """最小可用适配器：实现 XTQuantGateway 的下单/撤单/行情面。"""

    client_version = "fake-1.0"
    sdk_required = "xtquant"
    supported_account_types = ["STOCK"]
    capabilities = ["quote", "kline", "trade"]

    def __init__(self):
        self.calls = []

    def start(self) -> None:
        return None

    def close(self) -> None:
        return None

    def is_connected(self) -> bool:
        return True

    def get_quote(self, code: str) -> dict:
        return {"code": code, "last": 35.6, "ask1": 35.7, "bid1": 35.5}

    def get_kline(self, code, period, count, start="", end="", adjust=None):
        return []

    def place_order(self, code, direction, price_type, price, volume,
                    strategy_name="", remark=""):
        self.calls.append(("place_order", code, direction, price_type, price,
                           volume, strategy_name, remark))
        return {"order_id": "7160", "status": 50}

    def cancel_order(self, order_id: str) -> dict:
        self.calls.append(("cancel_order", order_id))
        return {"order_id": order_id, "status": 54}

    def query_position(self):
        return []

    def query_cash(self):
        return {}

    def subscribe_quote(self, codes, on_tick) -> None:
        return None


class _FakeRisk:
    def check_order(self, code, price, volume, direction, price_type,
                    require_account=False):
        return True, ""


def _reload_execution(flag: str):
    """按开关值重新加载 execution 模块（``QMT_USE_PORTS`` 在导入期读取）。"""
    os.environ["QMT_USE_PORTS"] = flag
    import gateway.execution as ex

    return importlib.reload(ex)


@pytest.fixture(autouse=True)
def _restore_env():
    old = os.environ.get("QMT_USE_PORTS")
    yield
    if old is None:
        os.environ.pop("QMT_USE_PORTS", None)
    else:
        os.environ["QMT_USE_PORTS"] = old


def _place(ex_module, bridge):
    svc = ex_module.ExecutionService(risk=_FakeRisk(), db=None)
    return asyncio.run(svc.place_order(
        bridge, "600036.SH", "buy", 100, price=35.6, price_type="limit",
        strategy_name="unit", remark="test", risk_checked=True))


def test_default_path_unchanged():
    """默认关闭 ⇒ 不经端口层。这是对既有行为的回归保护。"""
    ex = _reload_execution("0")
    assert ex._QMT_USE_PORTS is False
    gw = _FakeGateway()
    bridge = XTQuantBridge(gw)
    out = _place(ex, bridge)
    assert out["ok"] is True
    assert out["order_id"] == "7160"
    # 旧路径用的是位置参数调用
    assert gw.calls[0][0] == "place_order"
    assert gw.calls[0][1] == "600036.SH"


def test_port_path_produces_same_shape():
    """开启端口 ⇒ 走 Dialect×Transport，但**回执形状与旧路径一致**。"""
    ex = _reload_execution("1")
    assert ex._QMT_USE_PORTS is True
    gw = _FakeGateway()
    bridge = XTQuantBridge(gw)
    out = _place(ex, bridge)
    assert out["ok"] is True
    assert out["order_id"] == "7160"
    assert out["status"] == "pending"       # 50 → SSOT pending（不再裸传整数）
    assert gw.calls[0][0] == "place_order"  # 同一个 adapter 的同一个方法


def test_port_path_rejects_unaccepted_receipt():
    """零 mock：回执拿不到委托号 ⇒ 端口路径报错，不许粉饰成成功。"""
    ex = _reload_execution("1")

    class _NoId(_FakeGateway):
        def place_order(self, code, direction, price_type, price, volume,
                        strategy_name="", remark=""):
            return {"status": "unknown"}

    bridge = XTQuantBridge(_NoId())
    with pytest.raises(Exception) as exc:
        _place(ex, bridge)
    assert "委托号" in str(exc.value) or "受理" in str(exc.value)


def test_cancel_order_via_port():
    ex = _reload_execution("1")
    gw = _FakeGateway()
    bridge = XTQuantBridge(gw)
    svc = ex.ExecutionService(risk=_FakeRisk(), db=None)
    out = asyncio.run(svc.cancel_order(bridge, "7160"))
    assert out["order_id"] == "7160"
    assert out["ok"] is True


def test_port_assembly_failure_is_not_silently_downgraded():
    """没有 gateway 的 bridge ⇒ 端口装配失败必须显式抛出（不许悄悄走旧路径）。"""
    ex = _reload_execution("1")

    class _NoGateway:
        def call_locked(self, fn, *a, **kw):
            raise AssertionError("不应回落到旧路径")

    with pytest.raises(Exception):
        ex._port_for_bridge(_NoGateway())


def test_instrument_splitting():
    ex = _reload_execution("0")
    inst = ex._split_instrument("600036.sh")
    assert inst.code == "600036"
    assert inst.exchange == "SH"
    assert inst.canonical == "600036.SH"

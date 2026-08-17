"""阶段 0-B 回归测试：幂等唯一入口（F1）+ 风控唯一闸门（F5）+ 统一下单入口（F6/F13）。

不依赖真实 xtquant SDK（CI/托管 venv 均无 xtquant）：
- 用内存级 fake manager/bridge 让 SignalRouter 走通 _live 真实下单路径；
- 引擎收敛测试直接 patch `state.signal_router` 为 spy，验证引擎不再直连 gateway.place_order。
"""
import asyncio
import time

import pytest

from app.state import state
from gateway.idempotency import single_flight
from gateway.risk import RiskManager, normalize_direction
from gateway.signal_router import SignalRouter
from tools.algo import AlgoEngine


# ---------------- fakes ----------------
class FakeBridge:
    def __init__(self):
        self.calls = []
        # 引擎 _confirm_fill 通过 hasattr(b.gateway, "get_orders") 判断适配器能力，
        # 故需提供 gateway 属性（与真实 bridge 接口一致）。
        from types import SimpleNamespace
        self.gateway = SimpleNamespace(
            get_quote=get_quote, get_orders=get_orders, place_order=place_order)

    async def call(self, fn, *args, **kwargs):
        self.calls.append((fn, args, kwargs))
        name = getattr(fn, "__name__", "")
        if name == "get_quote":
            return {"last": 10.0, "volume": 1000}
        if name == "get_orders":
            return []
        return {"order_id": "FAKE", "status": "submitted"}

    async def call_locked(self, fn, *args, **kwargs):
        self.calls.append((fn, args, kwargs))
        return {"order_id": "FAKE", "status": "submitted"}


def get_quote(*a, **k):
    return {"last": 10.0, "volume": 1000}


def get_orders(*a, **k):
    return []


def place_order(*a, **k):
    return {"order_id": "FAKE", "status": "submitted"}


class FakeManager:
    def __init__(self):
        self.bridge_ = FakeBridge()

    def bridge(self, conn_id=None):
        return self.bridge_

    def active_bridge(self, conn_id=None):
        return self.bridge_


class FakeManagerDisconnected:
    def bridge(self, conn_id=None):
        return None

    def active_bridge(self, conn_id=None):
        return None


@pytest.fixture(autouse=True)
def _clear_idempotency():
    """单飞缓存/进行中任务为模块级全局态，测试间必须隔离。"""
    import gateway.idempotency as ide
    ide._cache.clear()
    ide._inflight.clear()
    yield
    ide._cache.clear()
    ide._inflight.clear()


# ---------------- F1 单飞幂等 ----------------
def test_single_flight_concurrent_only_once():
    """并发同 key 只执行一次真实逻辑，其余复用结果并标记 duplicated（杜绝双击双单）。"""
    counter = {"n": 0}

    async def factory():
        counter["n"] += 1
        await asyncio.sleep(0.05)
        return {"order_id": "X"}

    async def run():
        tasks = [single_flight("k-same", factory) for _ in range(5)]
        return await asyncio.gather(*tasks)

    results = asyncio.run(run())
    assert counter["n"] == 1, "并发同 key 不应执行多次"
    # 仅真实执行那次无 duplicated 标记；其余 4 次应标记 duplicated（复用同一执行）
    assert sum(1 for r in results if r.get("duplicated") is True) == 4


def test_single_flight_window_cache_marks_dup():
    """窗口内已完成同 key 直接命中缓存（标记 duplicated），不二次下发。"""
    async def factory():
        return {"order_id": "Y"}

    async def run():
        first = await single_flight("k-window", factory)
        second = await single_flight("k-window", factory)
        return first, second

    first, second = asyncio.run(run())
    assert first.get("duplicated") is None  # 首次真实执行
    assert second.get("duplicated") is True  # 窗口内命中缓存


# ---------------- F5 风控唯一闸门 ----------------
def test_direction_normalize_boundary():
    """方向归一化：中英/多同义词识别；未知方向显式返回 None（绝不默认卖出）。"""
    assert normalize_direction("BUY") == "buy"
    assert normalize_direction("卖出") == "sell"
    assert normalize_direction("long") == "buy"
    assert normalize_direction("short") == "sell"
    assert normalize_direction("") is None
    assert normalize_direction("mystery_xyz") is None


def test_risk_rejects_unknown_direction():
    risk = RiskManager(max_amount=1_000_000, min_qty=100)
    ok, reason = risk.check_order("600000", 10.0, 100, "BUY")
    assert ok is True  # 同义词应被接受
    ok2, reason2 = risk.check_order("600000", 10.0, 100, "mystery_xyz")
    assert ok2 is False
    assert "未知交易方向" in reason2


def test_circuit_blocks_submit_all_engines():
    """熔断期间 submit 必拒（所有引擎经 submit 入场，故熔断对全部引擎生效）。"""
    risk = RiskManager(max_amount=1_000_000, min_qty=100)
    risk.trip("测试一键熔断")
    sr = SignalRouter(FakeManager(), risk, None, None, None, None)
    res = asyncio.run(sr.submit("600000", "buy", 100, 10.0, "limit",
                                source="test", auto_confirm=True))
    assert res["ok"] is False
    assert "熔断" in res["reason"]


def test_submit_risk_reject_returns_false_not_whitewashed():
    """单笔金额超限被风控拒单 → submit 返回 ok=False，绝不粉饰成成功。"""
    risk = RiskManager(max_amount=1000, min_qty=100)  # 单笔上限 1000
    sr = SignalRouter(FakeManager(), risk, None, None, None, None)
    # 200 股 * 10 元 = 2000 > 1000 → 拒
    res = asyncio.run(sr.submit("600000", "buy", 200, 10.0, "limit",
                                source="test", auto_confirm=True))
    assert res["ok"] is False
    assert "amount" in res["reason"]


# ---------------- F13 失败不粉饰 ----------------
def test_submit_unconnected_returns_false():
    """未连接券商 → submit 返回 ok=False（reason 引导去连接页），而非伪装成功。"""
    risk = RiskManager(max_amount=1_000_000, min_qty=100)
    sr = SignalRouter(FakeManagerDisconnected(), risk, None, None, None, None)
    res = asyncio.run(sr.submit("600000", "buy", 100, 10.0, "limit",
                                source="test", auto_confirm=True))
    assert res["ok"] is False
    assert "未连接" in res["reason"]


# ---------------- F6 引擎收敛到统一入口 ----------------
def test_algo_slice_routes_through_signal_router():
    """算法单切片经 state.signal_router.submit()（含 auto_confirm），且不直接调 gateway.place_order。"""
    calls = []

    async def fake_submit(code, side, volume, price=0.0, price_type="limit",
                          source="manual", broker_id="", remark="",
                          idempotency_key="", auto_confirm=False):
        calls.append({"code": code, "side": side, "volume": volume,
                      "price": price, "price_type": price_type,
                      "source": source, "auto_confirm": auto_confirm})
        return {"ok": True, "order_id": "FAKE", "status": "submitted"}

    class FakeRouter:
        submit = staticmethod(fake_submit)

    saved = state.signal_router
    state.signal_router = FakeRouter()
    mgr = FakeManager()
    risk = RiskManager(max_amount=1_000_000, min_qty=100)
    eng = AlgoEngine(mgr, risk)
    aid = "algo-1"
    eng._jobs[aid] = {
        "code": "600000", "direction": "buy", "algo": "twap",
        "price_type": "limit", "limit_price": 10.0, "remark": "",
        "done": 0, "children": [], "slices": 5, "slices_done": 0,
    }
    try:
        asyncio.run(eng._place_slice(aid, 1, 100))
    finally:
        state.signal_router = saved

    assert len(calls) == 1, "切片应恰好经一次 submit"
    c = calls[0]
    assert c["code"] == "600000" and c["side"] == "buy" and c["volume"] == 100
    assert c["auto_confirm"] is True and c["source"] == "algo_twap"
    # 关键断言：引擎不得绕过下单入口直接调用 gateway.place_order
    place_calls = [c for c in mgr.bridge_.calls
                   if getattr(c[0], "__name__", "") == "place_order"]
    assert place_calls == [], "引擎不得直接调用 gateway.place_order（应统一经 SignalRouter.submit）"

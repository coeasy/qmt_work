"""P0-4 回归：**手动单必须走大额 TOTP 二次确认**，不得跳过。

## 缺陷回顾

`app/routes/trade.py` 曾对**手动单**传 `auto_confirm=True`。

`auto_confirm` 的语义是「跳过人工 TOTP 挂起」，它只为**已授权的自动化引擎**而设
（algo / condition / limitup / strategy / rebalance —— 用户在启动引擎前已配置并授权，
无需逐单确认）。手动单跟着传 True 的后果是 `SignalRouter.route()` 里

    amount >= threshold and mode in ("live", "paper") and not auto_confirm

这一条件**恒为假**，于是人工大额单的二次确认从不触发，TOTP 形同虚设，
前端 `Trade.tsx` 里整套 `pending_confirmation` / TOTP 交互沦为死代码。

## 本文件锁死什么

1. **路由层**：`POST /trade/order` 必须以 `auto_confirm=False` 调 `signal_router`；
2. **语义层**：用真实 `SignalRouter` 证明「金额 ≥ 阈值 → 挂起等确认」这条路径
   在 `auto_confirm=False` 时确实可达，且 `auto_confirm=True`（引擎单）行为不变。

两者缺一不可：只测路由会漏掉「挂起逻辑本身坏了」，只测语义会漏掉「路由传错参数」。
"""
import asyncio

from core.context import AppContext, set_active_context
from gateway.signal_router import SignalRouter

# 与 core/config.py 的 signal_confirm_threshold 默认值一致
THRESHOLD = 100_000.0


# ---- 桩：路由层 ------------------------------------------------------------

class _SpyRouter:
    """记录 submit 实参的假统一入口（只记录，不做业务判断）。"""

    def __init__(self):
        self.calls: list[dict] = []

    async def submit(self, code, side, volume, price=0.0, price_type="limit",
                     source="", broker_id="", remark="", idempotency_key="",
                     auto_confirm=False, payload=None):
        self.calls.append({
            "code": code, "side": side, "volume": volume, "price": price,
            "price_type": price_type, "source": source, "broker_id": broker_id,
            "idempotency_key": idempotency_key, "auto_confirm": auto_confirm,
        })
        return {"ok": True, "pending_confirmation": True, "confirm_token": "tok-1",
                "amount": price * volume, "requires_totp": True, "mode": "live"}


class _StubManager:
    """让 _need() 拿得到一个 bridge（否则路由直接 503，测不到参数）。"""

    def bridge(self, conn_id=None):
        return object()


def _call_trade_order(body: dict) -> dict:
    """在桩上下文下直接调用路由函数，返回业务信封。"""
    from app.routes.trade import trade_order

    router = _SpyRouter()
    ctx = AppContext()
    ctx.signal_router = router
    ctx.broker_manager = _StubManager()
    set_active_context(ctx)
    try:
        return asyncio.run(trade_order(body, ctx=ctx)), router
    finally:
        set_active_context(AppContext())


def test_manual_order_passes_auto_confirm_false():
    """核心断言：手动单不得跳过 TOTP 挂起。"""
    body = {"code": "600519.SH", "direction": "buy", "volume": 20000,
            "price": 10.0, "price_type": "limit"}
    env, router = _call_trade_order(body)

    assert len(router.calls) == 1
    call = router.calls[0]
    assert call["auto_confirm"] is False, (
        "手动单传了 auto_confirm=True —— 大额 TOTP 二次确认会被整体跳过")
    assert call["source"] == "manual"
    assert env["code"] == 0


def test_manual_large_order_surfaces_pending_confirmation():
    """大额手动单的信封必须携带 pending_confirmation + confirm_token（前端据此弹 TOTP）。"""
    body = {"code": "600519.SH", "direction": "buy", "volume": 20000,
            "price": 10.0, "price_type": "limit"}
    env, _ = _call_trade_order(body)

    data = env["data"]
    assert data["pending_confirmation"] is True
    assert data["confirm_token"] == "tok-1"
    assert data["requires_totp"] is True


def test_manual_small_order_also_goes_through_router():
    """小额单同样走统一链路（参数一致），是否挂起交给 SignalRouter 判金额。"""
    body = {"code": "000001.SZ", "direction": "buy", "volume": 100,
            "price": 11.0, "price_type": "limit"}
    env, router = _call_trade_order(body)

    assert router.calls[0]["auto_confirm"] is False
    assert router.calls[0]["volume"] == 100
    assert env["code"] == 0


# ---- 语义层：真实 SignalRouter ---------------------------------------------

class _Bridge:
    def __init__(self):
        self.gateway = self
        self.orders: list = []

    def get_quote(self, code):
        return {"last": 10.0}

    def place_order(self, *args, **kwargs):
        self.orders.append((args, kwargs))
        return {"order_id": f"OID-{len(self.orders)}", "ok": True}

    async def call(self, fn, *args):
        return fn(*args)

    async def call_locked(self, fn, *args):
        return fn(*args)


class _Mgr:
    def __init__(self, bridge):
        self._bridge = bridge

    def bridge(self, conn_id=None):
        return self._bridge


class _PermissiveRisk:
    def check_order(self, code, price, volume, side, price_type="limit",
                    require_account=False, **kwargs):
        return True, ""


def _router():
    bridge = _Bridge()
    r = SignalRouter(_Mgr(bridge), risk=_PermissiveRisk())
    r.mode = "live"
    r.threshold = THRESHOLD
    return r, bridge


def test_large_manual_order_is_suspended_for_totp():
    """auto_confirm=False + 金额 ≥ 阈值 → 挂起，且**不产生真实委托**。"""
    r, bridge = _router()
    out = asyncio.run(r.submit("600519.SH", "buy", 20000, 10.0, "limit",
                               source="manual", auto_confirm=False))
    assert out["ok"] is True
    assert out["pending_confirmation"] is True
    assert out["confirm_token"]
    assert out["amount"] == 200_000.0
    assert bridge.orders == [], "挂起态不得已经报单"


def test_small_manual_order_executes_without_suspension():
    """金额 < 阈值 → 直接执行，不打扰用户（小额单 UX 不变）。"""
    r, bridge = _router()
    out = asyncio.run(r.submit("000001.SZ", "buy", 100, 11.0, "limit",
                               source="manual", auto_confirm=False))
    assert out.get("pending_confirmation") is not True
    assert len(bridge.orders) == 1


def test_engine_order_still_skips_suspension():
    """引擎单（auto_confirm=True）行为必须不变：已授权自动化不再逐单确认。"""
    r, bridge = _router()
    out = asyncio.run(r.submit("600519.SH", "buy", 20000, 10.0, "limit",
                               source="algo", auto_confirm=True))
    assert out.get("pending_confirmation") is not True
    assert len(bridge.orders) == 1

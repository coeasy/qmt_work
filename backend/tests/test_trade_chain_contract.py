"""交易主链路契约回归：**模式分流** + **二次确认字段名/参数完整性**。

## 背景（2026-09-20 在本机真实库 + 独立后端实例上实测发现的三处缺陷）

### 缺陷 1：`POST /trade/order` 的券商前置门不分模式

`app/routes/trade.py` 曾无条件 `b = _need(); if b is None: return no_broker()`。
于是把信号模式切到 **paper（模拟盘）/ dry_run（预演）** 后，交易页下单仍被
「未连接券商」挡死 —— 可这两个模式**根本不需要券商**：

- `paper`   ：成交由 `PaperEngine` 本地撮合（现金 / 持仓 / T+1 / 涨跌停全在本地维护）；
- `dry_run` ：`_route_inner` 只返回计划，在碰券商之前就 `return` 了。

实测（mode=paper、未连券商）：`POST /trade/order` →
`503 未连接任何券商客户端：请到「券商连接」页添加并连接券商。`
即**模拟盘这条链路从界面完全不可达**。更矛盾的是 `/trade/precheck` 已经按
`require_account=_live` 做了模式分流：预检说「可以下单」，下单却说「没连券商」。

修法：只有 `live` 才要求券商；live 无券商时交给 `SignalRouter._live()` 返回
`broker_unavailable=True`，路由照旧映射成 503 + 券商引导 —— **归因不变**。

### 缺陷 2：TOTP 字段名两侧不一致 ⇒ 启用 TOTP 后大额单永远确认不了

前端 `signalApi.confirm()`（`services/api/trade.ts`）发的是 **`totp`**，
后端 `routes/signal.py` 只读 **`totp_code`**，中间无任何归一化。
后果不是「少个参数」，而是**一旦启用 TOTP，大额单二次确认永远失败**，且报错
把责任推给用户（「TOTP 校验失败，请重新发起信号」），重试多少次都不会成功。

实测对照（同一合法 TOTP 码、同一挂起令牌、mode=paper）：

    {"confirm_token": T, "totp":      "231417"} → 400 TOTP 校验失败，请重新发起信号
    {"confirm_token": T, "totp_code": "231417"} → TOTP 通过，继续走到下一道真实校验

修法：后端归一化，两种写法都收（已分发的 Electron 客户端无需升级即可生效）。

### 缺陷 3：`confirm()` 重跑风控漏传 `price_type`

`SignalRouter.confirm()` 重跑风控时未传 `price_type`，`check_order` 取默认值
`"limit"` ⇒ **市价单被当限价单评估**：

- `_validate_order` 里 `if not is_market and price <= 0` 会以「限价单必须提供 >0 的
  委托价」拒掉一笔**市价单**；而「取不到最新价」恰恰正是市价单进入挂起分支的原因
  ⇒ **这类单永远确认不了**（实测复现：市价单挂起后确认 →
  `400 限价单必须提供 >0 的委托价，当前 price=0.0`）。
- 价格偏离闸门（`price_deviation_pct`）本应只作用于限价单，却会拿 `est_price`
  当委托价去比对。

修法：透传 `price_type=sig.price_type`，与 `_route_inner` 口径一致。

## 本文件锁死什么

上述三条各自的**行为**（而非实现细节）：模式分流、字段名兼容、重跑风控参数一致。
"""
import asyncio

from core.context import AppContext, set_active_context
from core.state import MSG_NO_BROKER
from gateway.risk import RiskManager
from gateway.signal_router import SignalRouter

THRESHOLD = 100_000.0


# ---- 桩：路由层 ------------------------------------------------------------

class _NoBridge:
    """没有任何连接的券商管理器：`bridge()` 恒 None（= 未连券商）。"""

    def bridge(self, conn_id=None):
        return None


class _SpyRouter:
    """记录调用的假统一入口（只记录，不做业务判断）。"""

    def __init__(self, mode="paper"):
        self.mode = mode
        self.submits: list[dict] = []
        self.confirms: list[dict] = []

    async def submit(self, code, side, volume, price=0.0, price_type="limit",
                     source="", broker_id="", remark="", idempotency_key="",
                     auto_confirm=False, payload=None):
        self.submits.append({
            "code": code, "side": side, "volume": volume, "price": price,
            "price_type": price_type, "source": source, "broker_id": broker_id,
            "idempotency_key": idempotency_key, "auto_confirm": auto_confirm,
        })
        return {"ok": True, "mode": self.mode, "order_id": "PAPER-1", "status": "filled"}

    async def confirm(self, token, totp_code=""):
        self.confirms.append({"token": token, "totp_code": totp_code})
        return {"ok": True, "mode": self.mode, "confirmed": True, "order_id": "PAPER-2"}


def _ctx(router, manager=None) -> AppContext:
    ctx = AppContext()
    ctx.signal_router = router
    ctx.broker_manager = manager or _NoBridge()
    set_active_context(ctx)
    return ctx


def _order(body: dict, router, manager=None) -> dict:
    from app.routes.trade import trade_order
    ctx = _ctx(router, manager)
    return asyncio.run(trade_order(body, ctx=ctx))


def _confirm(body: dict, router) -> dict:
    from app.routes.signal import signal_confirm
    ctx = _ctx(router)
    return asyncio.run(signal_confirm(body, ctx=ctx))


# ---- 缺陷 1：模式分流 ------------------------------------------------------

def test_live_mode_without_broker_still_returns_no_broker():
    """live 模式无券商：仍然 503 + 券商引导（归因不能变松）。"""
    r = _SpyRouter(mode="live")
    env = _order({"code": "600519.SH", "direction": "buy", "volume": 300,
                  "price": 1680.0}, r)
    assert env["code"] == 503
    assert MSG_NO_BROKER in env["message"]
    assert r.submits == [], "未连券商时 live 单不得进入信号路由"


def test_paper_mode_without_broker_reaches_router():
    """★ 核心回归：paper 模式无券商也必须能下单（成交由 PaperEngine 本地撮合）。"""
    r = _SpyRouter(mode="paper")
    env = _order({"code": "600000.SH", "direction": "buy", "volume": 100,
                  "price": 9.07, "price_type": "limit"}, r)
    assert env["code"] == 0, f"模拟盘不该被券商前置门拦下：{env}"
    assert len(r.submits) == 1
    assert r.submits[0]["code"] == "600000.SH"
    assert r.submits[0]["price_type"] == "limit"


def test_dry_run_mode_without_broker_reaches_router():
    """dry_run 只返回计划，同样不需要券商。

    ⚠️ 每条用例必须用**不同的委托内容**：`/trade/order` 末尾的 `single_flight` 幂等键是
    `manual:{code}:{direction}:{volume}:{price}:{price_type}`，且缓存是**进程级**的
    （跨用例共享，窗口 5s）。同参数的第二条用例会命中上一条的缓存、根本不进
    `_SpyRouter`，表现为「单独跑通过、整个文件跑失败」。
    """
    r = _SpyRouter(mode="dry_run")
    env = _order({"code": "600004.SH", "direction": "buy", "volume": 200,
                  "price": 7.63}, r)
    assert env["code"] == 0, f"预演模式不该被券商前置门拦下：{env}"
    assert len(r.submits) == 1


def test_order_without_signal_router_is_503_not_crash():
    """signal_router 未初始化：必须是明确的 503，而不是 getattr(None) 崩掉。"""
    from app.routes.trade import trade_order
    ctx = AppContext()
    ctx.signal_router = None
    ctx.broker_manager = _NoBridge()
    set_active_context(ctx)
    env = asyncio.run(trade_order({"code": "600000.SH", "direction": "buy",
                                   "volume": 100, "price": 9.07}, ctx=ctx))
    assert env["code"] == 503
    assert "信号" in env["message"]


# ---- 缺陷 2：TOTP 字段名兼容 -----------------------------------------------

def test_confirm_accepts_frontend_totp_field():
    """★ 核心回归：前端契约用的 `totp` 必须被接受（否则启用 TOTP 后永远失败）。"""
    r = _SpyRouter()
    env = _confirm({"confirm_token": "tok-1", "totp": "231417"}, r)
    assert env["code"] == 0, f"前端字段名 `totp` 未被接受：{env}"
    assert r.confirms[0]["totp_code"] == "231417"


def test_confirm_accepts_canonical_totp_code_field():
    """规范名 `totp_code` 同样接受（前端已同步为发这个名字）。"""
    r = _SpyRouter()
    env = _confirm({"confirm_token": "tok-2", "totp_code": "654321"}, r)
    assert env["code"] == 0
    assert r.confirms[0]["totp_code"] == "654321"


def test_confirm_prefers_totp_code_when_both_present():
    """两个都传时以规范名 `totp_code` 为准（避免歧义）。"""
    r = _SpyRouter()
    _confirm({"confirm_token": "tok-3", "totp_code": "111111", "totp": "999999"}, r)
    assert r.confirms[0]["totp_code"] == "111111"


def test_confirm_without_token_is_400():
    r = _SpyRouter()
    env = _confirm({"totp": "231417"}, r)
    assert env["code"] == 400
    assert "confirm_token" in env["message"]


# ---- 缺陷 3：confirm 重跑风控必须带 price_type -----------------------------

class _SpyRisk:
    def __init__(self):
        self.calls: list[dict] = []

    def check_order(self, code, price, volume, side, price_type="limit",
                    require_account=False, **kwargs):
        self.calls.append({"code": code, "price": price, "volume": volume,
                           "side": side, "price_type": price_type,
                           "require_account": require_account})
        return True, ""


class _RecorderPaper:
    """记录 paper 撮合实参的假引擎。"""

    def __init__(self):
        self.calls: list[dict] = []

    def submit_order(self, code, side, price, volume, price_type="limit",
                     remark="", name=""):
        self.calls.append({"code": code, "side": side, "price": price,
                           "volume": volume, "price_type": price_type})
        return {"order_id": "PAPER-X", "code": code, "side": side,
                "price": price, "volume": volume, "status": "filled"}


def _router_with(risk, paper=None, mode="paper"):
    r = SignalRouter(_NoBridge(), risk=risk)
    r.mode = mode
    r.threshold = THRESHOLD
    if paper is not None:
        r._paper_engine = paper
    return r


def test_confirm_reruns_risk_with_original_price_type():
    """★ 核心回归：确认时重跑风控必须透传原委托的 price_type。

    此前漏传 ⇒ 默认 "limit" ⇒ 市价单被按限价单评估。
    """
    risk = _SpyRisk()
    r = _router_with(risk, _RecorderPaper())
    # 市价单、无行情 ⇒ 挂起等确认
    out = asyncio.run(r.submit("600004.SH", "buy", 100, 0.0, "market",
                               source="manual", auto_confirm=False))
    assert out.get("pending_confirmation") is True, out
    token = out["confirm_token"]

    risk.calls.clear()
    asyncio.run(r.confirm(token, ""))
    assert risk.calls, "确认路径必须重跑风控"
    assert risk.calls[0]["price_type"] == "market", (
        f"重跑风控丢了 price_type，市价单被当限价单：{risk.calls[0]}")


def test_market_order_confirm_is_not_rejected_as_limit_order():
    """★ 核心回归（用真实 RiskManager）：市价单确认不得被「限价单必须提供 >0 的委托价」拒掉。

    这是缺陷 3 的用户可见后果：市价单之所以进入挂起分支，正是因为取不到最新价
    （`est_price=0`）；若重跑风控把它当限价单，就必然以「限价单无委托价」被拒
    ⇒ 这类单永远确认不了。
    """
    risk = RiskManager()          # 真实风控，默认 max_amount=100_000
    paper = _RecorderPaper()
    r = _router_with(risk, paper)
    out = asyncio.run(r.submit("600004.SH", "buy", 100, 0.0, "market",
                               source="manual", auto_confirm=False))
    assert out.get("pending_confirmation") is True, out

    res = asyncio.run(r.confirm(out["confirm_token"], ""))
    reason = str(res.get("reason") or "")
    assert "限价单必须提供" not in reason, (
        f"市价单被错误地按限价单拒绝：{res}")
    # 市价单拿不到价时，模拟盘必须如实拒绝（零 mock：绝不臆造价格成交），
    # 且文案要点明「市价单需要最新行情」这一真实成因。
    assert res.get("ok") is False, res
    assert "市价" in reason and "行情" in reason, res


def test_limit_order_confirm_still_enforces_price():
    """反向保护：限价单缺价仍必须被风控拒（修复不能把闸门放松成永真）。"""
    risk = RiskManager()
    r = _router_with(risk, _RecorderPaper())
    # 直接构造一笔「限价单 + price=0」的挂起项，模拟异常输入
    from gateway.signal_router import Signal
    import time as _time
    sig = Signal(source="manual", code="600004.SH", side="buy", volume=100,
                 price=0.0, price_type="limit")
    r._pending["tok-limit"] = {"sig": sig.__dict__, "ts": _time.time(),
                               "mode": "paper"}
    res = asyncio.run(r.confirm("tok-limit", ""))
    assert res.get("ok") is False, res
    assert "限价单必须提供" in str(res.get("reason") or ""), res


# ---- 缺陷 4/5：撤单与再平衡的「券商门顺序 + 模式归因」 ---------------------
#
# 2026-09-20 第二轮实测（独立后端 + 用户真实库副本）发现：
#
#   1) `POST /trade/cancel` 的**参数校验在券商门之后** ⇒ 传 `{}` 得到
#      「未连接任何券商客户端：请到「券商连接」页添加并连接券商。」
#      把一个「参数没传」的请求归因成「环境不可用」，排查方向被彻底带偏。
#   2) `POST /trade/cancel` / `POST /rebalance` 的券商门**不分模式** ⇒ 在
#      paper 模式下撤模拟盘委托 / 做模拟盘再平衡，同样被推去「连接券商」。
#      paper 委托即时撮合，本来就不存在可撤的挂单；说成「没连券商」是错误归因。
#
# 修法：参数校验前置；券商门按 `signal_router.mode` 分流，非 live 时给出
# 「当前模式不支持 + 替代路径」的诚实说明，live 时**归因保持不变**。

def _cancel(body: dict, router, manager=None) -> dict:
    from app.routes.trade import trade_cancel
    ctx = _ctx(router, manager)
    return asyncio.run(trade_cancel(body, ctx=ctx))


def _rebalance(body: dict, router, manager=None) -> dict:
    from app.routes.rebalance import rebalance
    ctx = _ctx(router, manager)
    return asyncio.run(rebalance(body, ctx=ctx))


def test_cancel_missing_order_id_is_400_not_503():
    """★ 核心回归：没传 order_id 是**参数错**，不是「没连券商」。"""
    env = _cancel({}, _SpyRouter(mode="paper"))
    assert env["code"] == 400, f"参数校验被券商门抢先，归因错误：{env}"
    assert "order_id" in env["message"]


def test_cancel_live_without_broker_keeps_no_broker_attribution():
    """反向保护：live 模式无券商，撤单仍必须是券商引导（归因不能变松）。"""
    env = _cancel({"order_id": "1001"}, _SpyRouter(mode="live"))
    assert env["code"] == 503
    assert MSG_NO_BROKER in env["message"]


def test_cancel_paper_without_broker_explains_mode_not_broker():
    """★ 核心回归：paper 模式撤单不得说「请去连接券商」。

    真实成因是「模拟盘委托即时撮合，没有挂单可撤」——文案必须点明模式，
    且不得出现券商引导（否则用户会去连一个根本用不上的券商）。
    """
    env = _cancel({"order_id": "PAPER-1"}, _SpyRouter(mode="paper"))
    assert env["code"] == 503, env
    assert "模拟盘" in env["message"], f"未点明真实成因：{env}"
    assert MSG_NO_BROKER not in env["message"], f"paper 模式不该引导去连券商：{env}"


def test_cancel_dry_run_without_broker_explains_mode():
    env = _cancel({"order_id": "1002"}, _SpyRouter(mode="dry_run"))
    assert env["code"] == 503
    assert "预演" in env["message"] or "模拟盘" in env["message"], env
    assert MSG_NO_BROKER not in env["message"], env


def test_cancel_unknown_conn_id_still_404_and_never_falls_back():
    """★ 账户安全保险不能因为这次改动被放松：指定了 conn_id 就必须 404。"""
    env = _cancel({"order_id": "1003", "conn_id": "nope"}, _SpyRouter(mode="paper"))
    assert env["code"] == 404, env
    assert "已阻止回退" in env["message"], env


def test_rebalance_live_without_broker_keeps_no_broker_attribution():
    env = _rebalance({"targets": [{"code": "600000.SH", "target_ratio": 0.5}]},
                     _SpyRouter(mode="live"))
    assert env["code"] == 503
    assert MSG_NO_BROKER in env["message"]


def test_rebalance_paper_without_broker_explains_mode_not_broker():
    """★ 核心回归：再平衡按券商账户算差额，paper 下要说「当前模式不支持」并给替代路径。"""
    env = _rebalance({"targets": [{"code": "600000.SH", "target_ratio": 0.5}]},
                     _SpyRouter(mode="paper"))
    assert env["code"] == 503, env
    assert "模拟盘" in env["message"], f"未点明真实成因：{env}"
    assert MSG_NO_BROKER not in env["message"], f"paper 模式不该引导去连券商：{env}"


def test_rebalance_empty_targets_is_400_regardless_of_mode():
    """反向保护：参数校验依旧在最前（不能因为加了模式分支而改变优先级）。"""
    for mode in ("live", "paper", "dry_run"):
        env = _rebalance({"targets": []}, _SpyRouter(mode=mode))
        assert env["code"] == 400, f"mode={mode} 应仍是参数错：{env}"
        assert "targets" in env["message"]


# ---- 缺陷 4：模拟盘委托号被递给券商柜台 ⇒ 500「服务器内部错误」 -------------
#
# 2026-09-20 在本机独立后端实例上实测（QMT **已连接**、signal mode=paper）：
#
#     POST /trade/cancel {"order_id": "PAPER-1"}
#     → 500 {"code":500,"message":"服务器内部错误"}
#
# 真实链路：`_need()` 拿到了券商（**因为券商是连着的**）⇒ 跳过模式分支 ⇒
# 一路走到 `xtquant_client/xtp/trading.py` 的 `int(order_id)` ⇒
# `ValueError: invalid literal for int() with base 10: 'PAPER-1'` ⇒ 全局兜底 500。
#
# 两个错误归因叠在一起：
#   ① 用户看到「服务坏了」，真相是「这笔委托根本不在券商那儿」；
#   ② 原模式的判断被写在 `b is None` 分支里 ⇒ **只在没连券商时才生效**，
#      连上券商后 paper 模式撤单反而被放行到柜台。
#
# 修法：先按**委托号前缀**识别模拟盘单（不按模式一刀切 —— 用户可能刚从 live
# 切到 paper，券商那边还有真实挂单要撤，硬拦会把真实委托搁死）；再把柜台的
# ValueError 映射成 400 参数错，而不是 500 服务故障。

class _ConnectedBridge:
    """有连接的券商管理器：`bridge()` 返回非 None（= 券商是连着的）。

    ★ 这是本组用例的关键 —— 旧代码只在**没连券商**时才走模式分支，
      所以只有「连上券商」才能复现出那个 500。
    """

    def __init__(self):
        self.bridge_calls = 0

    def bridge(self, conn_id=None):
        self.bridge_calls += 1
        return object()          # 非 None 即视为已连接


class _SpyExec:
    """假执行服务：记录被撤的委托号，可配置抛错。"""

    def __init__(self, exc=None):
        self.calls: list[str] = []
        self.exc = exc

    async def cancel_order(self, b, oid):
        self.calls.append(oid)
        if self.exc is not None:
            raise self.exc
        return {"order_id": oid, "status": "cancelled"}


def _cancel_with_exec(body: dict, router, manager, exec_svc, monkeypatch) -> dict:
    from app.routes import trade as trade_routes
    monkeypatch.setattr(trade_routes, "get_execution_service", lambda: exec_svc)
    return _cancel(body, router, manager)


def test_cancel_paper_order_id_never_reaches_broker(monkeypatch):
    """★ 核心回归：模拟盘委托号即使**券商已连接**也不得递给柜台。

    修前：券商连着 ⇒ 走到 `int("PAPER-1")` ⇒ 500「服务器内部错误」。
    修后：503 + 点明「模拟盘」，且**一次都没碰券商**。
    """
    mgr = _ConnectedBridge()
    ex = _SpyExec()
    env = _cancel_with_exec({"order_id": "PAPER-1"}, _SpyRouter(mode="paper"),
                            mgr, ex, monkeypatch)
    assert env["code"] == 503, f"模拟盘单号应被拦下，实际：{env}"
    assert "模拟盘" in env["message"], f"未点明真实成因：{env}"
    assert ex.calls == [], f"模拟盘单号被递给了券商执行服务：{ex.calls}"
    assert mgr.bridge_calls == 0, "前缀判定应在取券商之前完成（不该白取一次 bridge）"


def test_cancel_malformed_order_id_is_400_not_500(monkeypatch):
    """★ 核心回归：柜台因委托号形态拒绝 ⇒ 400 参数错，绝不是 500 服务故障。

    这里直接让执行服务抛 `ValueError`（等价于 `int("abc")`），
    断言路由把它翻译成「参数被拒绝」而不是让它冒泡成 500。
    """
    mgr = _ConnectedBridge()
    ex = _SpyExec(exc=ValueError("invalid literal for int() with base 10: 'abc'"))
    env = _cancel_with_exec({"order_id": "abc"}, _SpyRouter(mode="live"),
                            mgr, ex, monkeypatch)
    assert env["code"] == 400, f"应归因为参数错，实际：{env}"
    assert "撤单被拒绝" in env["message"], env
    assert "服务器内部错误" not in env["message"], env
    assert ex.calls == ["abc"], "合法的券商撤单请求仍应真的发出去"


def test_cancel_live_order_id_still_reaches_broker(monkeypatch):
    """反向保护：普通（券商）委托号必须照旧真的撤 —— 前缀门不得误伤。

    ★ 为什么不能按模式一刀切拦下所有撤单：用户可能刚从 live 切到 paper，
      券商那边还有真实挂单需要撤，硬拦会把真实委托搁死。
    """
    for mode in ("live", "paper"):
        mgr = _ConnectedBridge()
        ex = _SpyExec()
        env = _cancel_with_exec({"order_id": "1001"}, _SpyRouter(mode=mode),
                                mgr, ex, monkeypatch)
        assert env["code"] == 0, f"mode={mode} 普通委托号应照常撤：{env}"
        assert ex.calls == ["1001"], f"mode={mode} 未真正调用撤单：{ex.calls}"
        assert mgr.bridge_calls == 1, f"mode={mode} 应恰好取一次券商"

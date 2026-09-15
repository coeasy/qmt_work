"""Phase F (F3)：AppContext 注入测试 —— 路由经 Depends(get_ctx) 拿到非 None 上下文。

对应 V10 方案 Phase A DoD ⑤ / Phase F F3：
- lifespan 启动后 ``app.state.ctx`` 存在且核心槽位非 None；
- ``core.context.active_context()`` 与 ``app.state.ctx`` 同一实例（进程级持有）；
- 关键路由 handler 签名含 ``ctx: AppContext``（由 Depends(get_ctx) 注入）；
- 走一次真实请求：/api/v1/health 200、/config/runtime 200、/ready 200（Required 全 ready；
  broker 属 Optional，未连券商不影响就绪）。
"""
from __future__ import annotations

import inspect


def test_ctx_slots_filled(app_client):
    from app.main import app
    from core.context import AppContext

    ctx = getattr(app.state, "ctx", None)
    assert isinstance(ctx, AppContext)
    assert ctx.db is not None
    assert ctx.broker_manager is not None
    assert ctx.job_runtime is not None
    assert ctx.data_router is not None
    assert ctx.execution is not None


def test_bootstrap_slots_reach_routes_via_ctx(app_client):
    """**生产级回归**：bootstrap 装配的槽位必须到达路由上下文。

    这两个槽位曾是静默失效的受害者（2026-09-15 实测）：
    - ``alert_engine``：``bootstrap/phase_db`` 写 ``state.alert_engine``，路由
      ``POST /alerts/test`` 读 ``ctx.alert_engine`` —— 快照时代恒为 None，
      端点永远返回 503「告警引擎未初始化」；
    - ``_market_sync_last``：``gateway.market_sync`` 写，``GET /market/kline/sync-status``
      读 —— 快照时代 ``last_run`` 恒为 null。

    容器合一（ctx is state）后两者都可见；本用例在**真实 app** 上钉住。
    """
    from core.context import active_context

    ctx = active_context()
    assert ctx.alert_engine is not None, \
        "alert_engine 未到达路由上下文 → POST /alerts/test 会恒 503"
    assert ctx.order_watchdog is not None
    # 该属性由 gateway.market_sync 在运行时写入；未跑同步时值为 None 属正常，
    # 关键是**属性可读**（声明存在），不再走静默兜底。
    assert ctx._market_sync_last is None or isinstance(ctx._market_sync_last, dict)


def test_active_context_is_process_singleton(app_client):
    from app.main import app
    from core.context import active_context

    assert active_context() is app.state.ctx
    # V11 R5 容器合一：生产路径注册的就是 core.state 单例本身（**不是快照拷贝**）。
    # 若 main.lifespan 退回「拷贝一份 AppContext」，这条会红 —— 那正是
    # `alert_engine` / `_market_sync_last` 静默失效的根因。
    from core.state import state
    assert active_context() is state


def test_slots_written_after_startup_are_visible_via_ctx(app_client):
    """回归：启动后写入单例的槽位，路由读到的 ctx 必须立即可见（快照时代看不到）。"""
    from app.main import app
    from core.context import active_context
    from core.state import state

    ctx = active_context()
    assert ctx is app.state.ctx
    saved = state._market_sync_last
    try:
        state._market_sync_last = {"date": "2026-09-15 15:00:00", "codes": 1,
                                   "ok": 1, "fail": 0}
        assert ctx._market_sync_last == state._market_sync_last
    finally:
        state._market_sync_last = saved


def test_route_handlers_inject_ctx():
    """核心交易/账户/同步路由的 handler 都声明 ctx 参数（Depends(get_ctx)）。"""
    from app.routes import account, sync, trade
    from core.context import get_ctx

    def has_ctx(fn):
        params = inspect.signature(fn).parameters
        p = params.get("ctx")
        return p is not None and p.default is not inspect.Parameter.empty \
            and getattr(p.default, "dependency", None) is get_ctx

    # 直接 handler：account.account_status / trade.trade_order / sync.sync_subscribe
    assert has_ctx(account.account_status)
    assert has_ctx(trade.trade_order)
    assert has_ctx(sync.sync_subscribe)


def test_health_endpoints_via_ctx(app_client):
    """ctx 注入后端到端可用：health 200 / config 200 / ready 200。

    回归（P1-18 配套修正）：`/ready` 的就绪语义只看 Required 阶段
    （db/engines/watchdogs/replay/misc），broker 属 Optional —— **未连券商不应影响就绪**
    （见 tests/test_lifecycle_levels.py::test_qmt_broker_failure_does_not_break_ready）。

    历史（P1-18）：lifespan 曾在 `build_from_state(state)` 之后才 `state.mark_ready()`，
    而当时的 `AppContext.lifecycle_ready` 是**快照字段** → 进程级 ctx 永久
    lifecycle_ready=False，`/ready` 恒 503（假未就绪）。
    V11 R5 容器合一后 `ctx is state`（同一对象、无快照），该类问题从结构上消失；
    本用例继续钉死「就绪即 200」。
    """
    r1 = app_client.get("/api/v1/health")
    assert r1.status_code == 200 and r1.json().get("code") == 0
    r2 = app_client.get("/api/v1/config/runtime")
    assert r2.status_code == 200
    r3 = app_client.get("/api/v1/ready")
    body = r3.json()
    assert r3.status_code == 200, body
    assert body.get("code") == 0, body
    d = body.get("data") or {}
    assert d.get("ready") is True, d
    assert d.get("db") is True, d
    assert all((d.get("engines") or {}).values()), d
    assert d.get("lifecycle", {}).get("ready") is True, d
    assert all(v == "ready" for v in (d.get("required_phases") or {}).values()), d

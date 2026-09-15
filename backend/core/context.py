"""AppContext（V10 Phase A：core 分层纠偏；V11 R5：容器合一）。

本模块是**服务槽位的唯一定义处**：``AppContext`` 声明全部槽位，
``core.state.AppState`` 继承它、只补生命周期方法与单例。于是「新增一个服务槽位」
只需改一处 —— 不会再出现「改了 A 没改 B」。

- ``app.main`` 的 lifespan（装配出口，唯一允许 import 全栈的位置）在启动完成后把
  ``core.state.state``（AppState 单例，本身即 AppContext）注册为当前上下文：
  ``set_active_context(state)``。**没有快照拷贝** —— 路由与引擎读的是**同一个对象**，
  故「启动后写入的槽位路由看不到」这一整类问题从结构上消失。
  （2026-09-15 实测：``/market/kline/sync-status`` 的 ``last_run`` 因快照而恒为
  ``null`` —— ``gateway.market_sync`` 写 ``state._market_sync_last``，路由却读快照。）
- ``get_ctx()`` 作为 FastAPI ``Depends`` 注入点；``active_context()`` 供非请求上下文
  （ws / 后台协程 / 共享 helper）取当前进程上下文。测试可
  ``set_active_context(自建 AppContext())`` 注入隔离上下文（不影响单例）。
- 未注入字段默认 None（Degraded），调用方按「有则用之」处理，绝不阻断就绪。
- **不提供「未知属性返回 None」兜底**：那会把 ``ctx.typo`` 变成静默 None，
  掩盖拼写错误。取可选槽位请写 ``getattr(ctx, name, None)``。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class AppContext:
    """应用上下文：全部服务槽位的**唯一**定义（``core.state.AppState`` 继承本类）。

    槽位分三组：
    - **核心服务槽位**：由 ``app/bootstrap`` 各阶段装配；
    - **运行期状态**：由服务在运行时写入、路由读取展示（必须与写入侧同一对象，
      故不能只靠启动时装配 —— 见模块 docstring 里的 ``last_run`` 实例）；
    - **app 层补充槽位**：core 不 import，由 ``app.main`` 显式注入。
    """

    # ---- 核心服务槽位（bootstrap 各阶段装配）----
    db: Any = None
    broker_manager: Any = None
    bridge: Any = None
    gateway: Any = None
    risk: Any = None
    mcp: Any = None
    sync_engine: Any = None
    ws_manager: Any = None
    backtest_queue: Any = None
    limitup_monitor: Any = None
    algo_engine: Any = None
    condition_engine: Any = None
    apikey_store: Any = None
    rate_limiter: Any = None
    notifier: Any = None
    # 告警引擎：bootstrap/phase_db 装配，routes/alerts 经 ctx 消费。
    # 2026-09-15：此前**未声明**，快照时代 ctx 读它经 __getattr__ 兜底恒为 None，
    # 导致 POST /alerts/test 永远返回 503「告警引擎未初始化」（静默失效）。
    alert_engine: Any = None
    order_watchdog: Any = None   # 委托超时看门狗：bootstrap 装配，shutdown 停止
    signal_router: Any = None
    wal: Any = None
    health_monitor: Any = None
    quote_bus: Any = None
    metrics: Any = None
    reconciler: Any = None
    kline_cache: Any = None
    webhook_out: Any = None
    runtime_config: Any = None
    paper_engine: Any = None
    strategy_runtime: Any = None
    market_sync: Any = None
    schedule_runner: Any = None

    # ---- 生命周期状态（由 core.state.AppState 的方法维护）----
    started_at: float = 0.0
    latest_quotes: dict = field(default_factory=dict)
    phase_status: dict = field(default_factory=dict)
    lifecycle_ready: bool = False
    lifecycle_stopping: bool = False

    # ---- 运行期状态（服务运行时写入；路由读取展示）----
    # 这些**必须**与写入侧共享同一对象，否则读到的是过期/空值。
    _market_sync_last: Any = None    # gateway.market_sync 写入最近一次刷新结果
    _eod_last_run_date: Any = None   # gateway.market_sync 写入 EOD 已跑日期
    _quote_bound_conns: Any = None   # bootstrap/phase_engines 写入已绑行情的 conn_id

    # ---- app 层装配补充槽位（core 不 import，由 main 显式注入）----
    job_runtime: Any = None
    execution: Any = None
    data_router: Any = None
    connector_registry: Any = None
    connector_supervisor: Any = None
    permission: Any = None
    secrets: Any = None
    calendar: Any = None
    scheduler: Any = None

    extras: dict = field(default_factory=dict)

    def update(self, **kwargs: Any) -> "AppContext":
        """批量写入槽位；未声明的键落 ``extras``（不静默丢弃）。"""
        for k, v in kwargs.items():
            if hasattr(self, k):
                setattr(self, k, v)
            else:
                self.extras[k] = v
        return self


_ACTIVE: "AppContext | None" = None


def set_active_context(ctx: AppContext) -> None:
    """装配出口（main.lifespan）注入当前进程上下文。

    生产路径传的是 ``core.state.state`` 单例本身 —— 与 bootstrap 写入侧同一对象。
    测试可传自建实例以隔离。
    """
    global _ACTIVE
    _ACTIVE = ctx


def active_context() -> AppContext:
    """非请求上下文取当前进程上下文（未注入则返回空上下文，不抛错）。"""
    return _ACTIVE if _ACTIVE is not None else AppContext()


def get_ctx() -> AppContext:
    """FastAPI 依赖：``Depends(get_ctx)`` 获取 AppContext。

    设计为无参依赖：避免 FastAPI 将 ``request`` 误判为查询参数（typed=Any 时会触发
    422）。当前平台为单进程单上下文，进程级持有即等价于请求级。
    """
    return active_context()


__all__ = [
    "AppContext", "get_ctx", "set_active_context", "active_context",
]

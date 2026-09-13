"""AppContext（V10 Phase A：core 分层纠偏 + 彻底迁移）。

V9 此处的 ``from_state()`` 通过**反向 import app/datasource/gateway** 来聚合服务，
违反了 core 无依赖的分层约定（P2）。V10 改为：

- ``AppContext`` 成为**显式构造**的上下文容器，字段名与 ``core.state.AppState``
  的服务槽位一一对应，路由层迁移只需将 ``state.X`` 重命名为 ``ctx.X``；
- 由 ``app.main`` 的 lifespan（装配出口，唯一允许 import 全栈的位置）显式注入所有
  依赖后调用 ``set_active_context(ctx)``，core 不再反向依赖任何外层模块；
- ``get_ctx(request)`` 仍作为 FastAPI ``Depends`` 注入点；``active_context()``
  供非请求上下文（ws/后台协程/共享 helper）取当前进程上下文；
- 未注入字段默认 None（Degraded），调用方按「有则用之」处理，绝不阻断就绪。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class AppContext:
    """应用上下文：与 AppState 服务槽位对齐的显式容器。

    字段命名与 ``core.state.AppState`` 保持一致（db / broker_manager / risk ...），
    以便路由层 ``state.X`` → ``ctx.X`` 机械迁移零语义落差。
    新增的 app 层装配依赖（job_runtime / data_router / execution / calendar 等）
    作为补充槽位，默认 None。
    """

    # 与 AppState 对齐的核心服务槽位
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
    started_at: float = 0.0
    latest_quotes: dict = field(default_factory=dict)
    phase_status: dict = field(default_factory=dict)
    lifecycle_ready: bool = False
    lifecycle_stopping: bool = False

    # app 层装配补充槽位（core 不 import，由 main 显式注入）
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
        for k, v in kwargs.items():
            if hasattr(self, k):
                setattr(self, k, v)
            else:
                self.extras[k] = v
        return self

    def __getattr__(self, name: str) -> Any:
        # 迁移期兜底：未知槽位返回 None，避免遗漏字段引发 AttributeError
        return None


_ACTIVE: "AppContext | None" = None


def set_active_context(ctx: AppContext) -> None:
    """装配出口（main.lifespan）注入当前进程上下文。"""
    global _ACTIVE
    _ACTIVE = ctx


def active_context() -> AppContext:
    """非请求上下文取当前进程上下文（无则空上下文）。"""
    return _ACTIVE if _ACTIVE is not None else AppContext()


def build_from_state(state: Any) -> AppContext:
    """显式从 AppState 拷贝对齐槽位（core 内零反向 import）。"""
    ctx = AppContext()
    for f in _STATE_FIELDS:
        ctx.update(**{f: getattr(state, f, None)})
    return ctx


# 与 AppState 对齐、可经 copy 直接迁移的字段（不含补充槽位与 extras）
_STATE_FIELDS = (
    "db", "broker_manager", "bridge", "gateway", "risk", "mcp",
    "sync_engine", "ws_manager", "backtest_queue", "limitup_monitor",
    "algo_engine", "condition_engine", "apikey_store", "rate_limiter",
    "notifier", "signal_router", "wal", "health_monitor", "quote_bus",
    "metrics", "reconciler", "kline_cache", "webhook_out",
    "runtime_config", "paper_engine", "strategy_runtime", "market_sync",
    "schedule_runner", "started_at", "latest_quotes", "phase_status",
    "lifecycle_ready", "lifecycle_stopping",
)


def get_ctx() -> AppContext:
    """FastAPI 依赖：``Depends(get_ctx)`` 获取 AppContext。

    解析顺序：
    - 进程级 ``active_context()``（由 ``app.main`` 的 lifespan 在启动完成时
      ``set_active_context(ctx)`` 注入；同进程内 HTTP/WS 均一致）；
    - 未构建/测试环境返回空上下文（调用方按 Degraded 处理，不抛 500）。

    设计为无参依赖：避免 FastAPI 将 ``request`` 误判为查询参数（typed=Any 时会触发
    422）。当前平台为单进程单 AppContext，进程级持有即等价于请求级。
    """
    return active_context()


__all__ = [
    "AppContext", "get_ctx", "set_active_context", "active_context",
    "build_from_state",
]

"""运行期单例容器：由 main.create_app 初始化，routes/tools 消费（避免循环 import）。

V11 R5 容器合一：``AppState`` 继承 :class:`core.context.AppContext`，
**服务槽位只在 AppContext 定义一处**，本模块只补生命周期状态机与券商连接访问器。
``state`` 本身即 AppContext，故 ``app.main`` 直接 ``set_active_context(state)``
—— 路由与引擎读写**同一对象**，不再有启动快照，也就没有「快照陈旧」这一类问题。

重点：券商连接由 `broker_manager` 统一管理（多券商 / 多账户 / 多客户端版本）。
`bridge` / `gateway` 保持为「当前活跃连接」的引用以便单连接调用点兼容；
多连接场景下请通过 `broker_manager.bridge(conn_id)` 指定。
"""
from __future__ import annotations

from dataclasses import dataclass

from core.context import AppContext

# V10 Phase A2：core 不得反向依赖 xtquant_client，BrokerManager 延迟实例化。
# 由 app/main.py 或 bootstrap/phase_broker 在装配期显式绑定（见 init_broker_manager）。

# 无券商连接的统一文案（503 响应与异常共用；app/routes/_common.no_broker 引用此处）。
MSG_NO_BROKER = "未连接任何券商客户端：请到「券商连接」页添加并连接券商。"

# 异常语境变体（require_bridge/get_bridge 抛出用；含客户端形态提示，与 503 响应文案区分）。
MSG_NO_BROKER_EXC = ("当前未连接任何券商客户端：请到「券商连接」页添加并连接券商"
                     "（国金/华鑫/银河等 MiniQMT）。")


# V9 Phase 5：Required 阶段唯一真源（core 不依赖 app，供 bootstrap/lifecycle 映射）。
# broker 为 Optional：QMT/券商失败 → Degraded，绝不阻断 READY。
REQUIRED_PHASES: tuple[str, ...] = ("db", "engines", "watchdogs", "replay", "misc")


@dataclass
class AppState(AppContext):
    """AppContext + 生命周期状态机 + 券商连接访问器。

    槽位（db / broker_manager / risk / ...）**全部继承自** :class:`AppContext`，
    本类不再重复声明 —— 新增槽位只改 ``core/context.py`` 一处。
    """

    def begin_startup(self) -> None:
        self.phase_status = {}
        self.lifecycle_ready = False
        self.lifecycle_stopping = False

    def mark_phase(self, name: str, status: str) -> None:
        self.phase_status[name] = status

    def mark_ready(self) -> bool:
        # P1-18：READY 判定基于 lifecycle 分层信号（Required 阶段全部 ready），
        # 不再依赖 started_at 时间戳（启动瞬间即被赋值，属恒真）。
        self.lifecycle_ready = all(self.phase_status.get(n) == "ready"
                                   for n in REQUIRED_PHASES)
        return self.lifecycle_ready

    def begin_shutdown(self) -> None:
        self.lifecycle_stopping = True
        self.lifecycle_ready = False

    def require_bridge(self, conn_id: str | None = None):
        """返回指定/活跃 bridge；无可用连接时抛 BrokerNotConnectedError。

        2026-09-15：``broker_manager`` 为 None（未装配 / 未 init）时**也**视为
        「未连接」—— 此前直接 ``self.broker_manager.bridge(...)`` 会抛
        ``AttributeError: 'NoneType' object has no attribute 'bridge'``，
        调用方（如 ``POST /factors/from-kline``）拿到 500 而非契约声明的 503。
        """
        from xtquant_client.base import BrokerNotConnectedError
        mgr = self.broker_manager
        b = mgr.bridge(conn_id) if mgr is not None else None
        if b is None:
            raise BrokerNotConnectedError(MSG_NO_BROKER_EXC)
        return b


state = AppState()


def init_broker_manager():
    """延迟创建 BrokerManager（core 不 import xtquant_client）。

    由 app 层（main.py / bootstrap/phase_broker）在装配期调用；
    多券商/多版本适配器的真实实现位于 xtquant_client.manager。
    """
    from xtquant_client.manager import BrokerManager
    if state.broker_manager is None:
        state.broker_manager = BrokerManager()
    return state.broker_manager

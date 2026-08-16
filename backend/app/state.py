"""运行期单例容器：由 main.create_app 初始化，routes/tools 消费（避免循环 import）。

重点：券商连接由 `broker_manager` 统一管理（多券商 / 多账户 / 多客户端版本）。
`bridge` / `gateway` 保持为「当前活跃连接」的引用以便单连接调用点兼容；
多连接场景下请通过 `broker_manager.bridge(conn_id)` 指定。
"""
from xtquant_client.manager import BrokerManager


class AppState:
    db = None
    broker_manager = BrokerManager()
    bridge = None          # 活跃连接 bridge（可能为 None）
    gateway = None         # 活跃连接 gateway（可能为 None）
    risk = None
    mcp = None
    sync_engine = None
    ws_manager = None
    backtest_queue = None
    limitup_monitor = None   # 涨停监控/打板助手
    algo_engine = None       # 算法单引擎（TWAP/VWAP）
    condition_engine = None  # 条件单/止损单引擎
    apikey_store = None      # 多组 API Key 存储（供其他服务调用）
    rate_limiter = None      # 限流器（每密钥独立配额）
    notifier = None          # 通知推送中心（钉钉/企微/飞书/邮件）
    signal_router = None     # 统一信号入口 + 物理旁路
    wal = None               # WAL 与启动恢复
    health_monitor = None    # 券商连接健康状态机 + 自动重连
    quote_bus = None         # 行情共享总线（内存/Redis）
    metrics = None           # Prometheus 指标收集器
    reconciler = None        # 委托对账核销器（A2）
    kline_cache = None       # 历史 K 线本地缓存（C1）
    webhook_out = None       # 委托/成交/告警事件出站 webhook（B2）
    runtime_config = None    # 运行时配置中心（热更新）
    paper_engine = None      # 模拟盘引擎（P1）
    strategy_runtime = None  # 策略运行容器：在平台内把策略当作实盘/模拟机器人运行（P0）
    started_at: float = 0.0  # 进程启动时间戳（健康检查用）
    latest_quotes: dict = {}

    def require_bridge(self, conn_id: str | None = None):
        """返回指定/活跃 bridge；无可用连接时抛 BrokerNotConnectedError。"""
        from xtquant_client.base import BrokerNotConnectedError
        b = self.broker_manager.bridge(conn_id)
        if b is None:
            raise BrokerNotConnectedError(
                "当前未连接任何券商客户端：请到「券商连接」页添加并连接券商（国金/华鑫/银河等 MiniQMT）。")
        return b


state = AppState()

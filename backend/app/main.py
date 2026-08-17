"""FastAPI 统一后端入口。

承载：REST 网关 + MCP（Streamable HTTP）+ Agent + 数据同步引擎(WebSocket) + 回测任务队列 + 静态托管。

券商客户端通过 BrokerManager 统一管理（多券商 / 多账户 / 多客户端版本），全部为真实 SDK 调用，无 mock。
"""
import asyncio
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.config import BASE_DIR, settings
from app.db import init_db
from app.logging_setup import setup_logging
from app.middleware.request_id import request_id_middleware
from app.routes import router
from app.state import state
from app.version import __version__
from backtest import BacktestQueue
from gateway.auth import make_auth_middleware
from gateway.rate_limit import RateLimiter, make_rate_limit_middleware
from gateway.risk import RiskManager
from mcp_server import build_mcp
from sync import SyncEngine, WSManager
from xtquant_client.manager import ConnectionConfig

setup_logging()
log = logging.getLogger("qmt_work")

risk = RiskManager(
    max_amount=settings.risk_max_amount,
    min_qty=settings.risk_min_qty,
    max_position_ratio=settings.risk_max_position_ratio,
    max_single_position_ratio=settings.risk_max_single_position_ratio,
    max_orders_per_min=settings.risk_max_orders_per_min,
    daily_amount_limit=settings.risk_daily_amount_limit,
    daily_loss_limit=settings.risk_daily_loss_limit,
    per_code_daily_orders=settings.risk_per_code_daily_orders,
    price_deviation_pct=settings.risk_price_deviation_pct,
    symbol_allow=settings.risk_symbol_allow,
    symbol_deny=settings.risk_symbol_deny,
)
# 价格偏离校验用的最新价提供者：从同步引擎行情缓存取（无行情时跳过校验）
def _latest_price(code: str):
    se = state.sync_engine
    if se is None:
        return None
    d = se.latest_quotes.get(code) or {}
    for k in ("price", "last", "lastPrice", "close"):
        v = d.get(k)
        if v:
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return None

risk.set_price_provider(_latest_price)
mcp = build_mcp(risk)
mcp_app = mcp.http_app(path="/", transport="streamable-http")


OPENAPI_TAGS = [
    {"name": "brokers", "description": "券商连接管理：多券商 / 多账户 / 多客户端版本，连接、切换、健康检查。"},
    {"name": "account", "description": "账户与资产：净值、可用资金、持仓、盈亏、多账户聚合、滑点统计。"},
    {"name": "trade", "description": "交易执行：下单、撤单、目标仓位、条件单/止损单、委托与成交查询。"},
    {"name": "signal", "description": "统一信号入口：live / paper / dry_run 物理旁路，大额二次确认（TOTP），外部 webhook 入站。"},
    {"name": "algo", "description": "算法单：TWAP / VWAP / Iceberg / POV 智能拆单，暂停恢复撤销。"},
    {"name": "limitup", "description": "涨停监控与打板助手：股票池、三因子触发、自动下单。"},
    {"name": "target-portfolio", "description": "目标持仓同步：差量计算、批量调仓、方案持久化。"},
    {"name": "backtest", "description": "回测任务队列：提交、查询、取消，含成本模型与真实历史 K 线。"},
    {"name": "market", "description": "行情数据：快照、K 线、L2 逐笔、板块与资讯抓取。"},
    {"name": "reference", "description": "参考数据：交易日历、板块列表与成分、财务摘要。"},
    {"name": "strategies", "description": "策略模板生成与落盘到 QMT 客户端 mpython 目录。"},
    {"name": "api-keys", "description": "多组 API Key：scope 分级、每密钥限流、IP 白名单、过期与轮换（供外部服务调用）。"},
    {"name": "alerts", "description": "告警规则引擎：事件型 / 指标型规则、冷却、历史记录。"},
    {"name": "notifications", "description": "通知渠道：钉钉 / 企业微信 / 飞书 / 邮件，含测试与发送日志。"},
    {"name": "webhooks", "description": "出站 webhook：订阅委托/成交/告警/风控事件，HMAC-SHA256 签名 + 指数退避重试，供外部系统消费。"},
    {"name": "reconcile", "description": "委托对账核销与 WAL 轮转：崩溃恢复后与券商当日委托/成交比对。"},
    {"name": "config", "description": "运行期配置：风控参数、信号模式。"},
    {"name": "ops", "description": "运维：健康检查、Prometheus 指标、审计日志、行情总线状态、WS 订阅。"},
    {"name": "factors", "description": "技术指标/因子库（pandas 向量化）：单/多因子计算、基于真实行情。"},
    {"name": "paper", "description": "模拟盘：基于实时行情的虚拟成交、持仓与盈亏（独立于真实券商）。"},
    {"name": "strategy-market", "description": "策略市场：模板目录、发布、导入导出（zip/json）、安装到 QMT 客户端。"},
    {"name": "agent", "description": "智能助手（LLM Agent）：基于真实券商/运行期数据的对话与工具调用；缺 LLM 配置即 503 降级。"},
]

_TAG_PREFIX = [
    ("/api/v1/brokers", "brokers"),
    ("/api/v1/account", "account"),
    ("/api/v1/trade", "trade"),
    ("/api/v1/signal", "signal"),
    ("/api/v1/algo", "algo"),
    ("/api/v1/limitup", "limitup"),
    ("/api/v1/target-portfolio", "target-portfolio"),
    ("/api/v1/rebalance", "target-portfolio"),
    ("/api/v1/backtest", "backtest"),
    ("/api/v1/market", "market"),
    ("/api/v1/reference", "reference"),
    ("/api/v1/strategies", "strategies"),
    ("/api/v1/strategies/run", "strategies"),
    ("/api/v1/api-keys", "api-keys"),
    ("/api/v1/alerts", "alerts"),
    ("/api/v1/notifications", "notifications"),
    ("/api/v1/webhooks", "webhooks"),
    ("/api/v1/reconcile", "reconcile"),
    ("/api/v1/wal", "reconcile"),
    ("/api/v1/config", "config"),
    ("/api/v1/factors", "factors"),
    ("/api/v1/paper", "paper"),
    ("/api/v1/strategy-market", "strategy-market"),
    ("/api/v1/agent", "agent"),
]


def _apply_openapi_meta(app: FastAPI) -> None:
    """G1：按路径前缀自动补 tags，并用 docstring 首行补 summary（避免逐个端点手改）。"""
    for route in app.routes:
        path = getattr(route, "path", "")
        if not path.startswith("/api/v1"):
            continue
        tag = "ops"
        for prefix, name in _TAG_PREFIX:
            if path.startswith(prefix):
                tag = name
                break
        if not getattr(route, "tags", None) or route.tags == ["api"]:
            route.tags = [tag]
        endpoint = getattr(route, "endpoint", None)
        if endpoint is not None and not getattr(route, "summary", None):
            doc = (endpoint.__doc__ or "").strip()
            if doc:
                route.summary = doc.splitlines()[0].strip()


def _bootstrap_from_env():
    """若提供了引导连接环境变量，则建立一个默认券商连接（不自动连接，待 lifespan 启动）。"""
    if settings.account_id or settings.client_path:
        cfg = ConnectionConfig(
            conn_id="bootstrap", name="引导连接",
            broker_id=settings.broker_id or "guojin",
            client_path=settings.client_path, account_id=settings.account_id,
            account_type=settings.account_type, session_id=settings.session_id,
            active=True)
        state.broker_manager.add_connection(cfg, autoconnect=False)


def create_app() -> FastAPI:
    async def _system_broadcast_loop():
        """周期向 WS 客户端广播系统状态（uptime/连接/交易时段/版本），供前端全局 Dashboard。"""
        while True:
            await asyncio.sleep(5.0)
            if state.ws_manager is None:
                continue
            try:
                from gateway.trading_session import default_session
                payload = {
                    "uptime_seconds": int(time.time() - state.started_at) if state.started_at else 0,
                    "version": __version__,
                    "brokers_total": len(state.broker_manager.all_connections()),
                    "brokers_connected": sum(1 for c in state.broker_manager.all_connections() if c.connected),
                    "clients": state.ws_manager.client_count(),
                    "trading_session": default_session.stats(),
                    "started_at": state.started_at,
                }
                await state.ws_manager.broadcast("system", {"event": "tick", **payload})
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                log.warning("system broadcast failed: %s", exc)

    @asynccontextmanager
    async def app_lifespan(app: FastAPI):
        db_backup = None
        _system_task = None
        state.db = init_db(settings.db_path)
        # 阶段 2.4：券商档案注册表挂接 DB（热插拔档案落库 + 加载已持久化档案）
        from xtquant_client.registry import registry as broker_registry
        broker_registry.attach_db(state.db)
        state.mcp = mcp
        # 多密钥存储绑定 DB 并加载
        if state.apikey_store is not None:
            state.apikey_store.bind(state.db)
            state.apikey_store.reload()
        # 通知中心初始化
        from gateway.notifier import Notifier
        state.notifier = Notifier(state.db, dedup_seconds=settings.notify_dedup_seconds)
        log.info("notifier ready")
        # 告警规则引擎：订阅通知事件流
        from gateway.alert_engine import AlertEngine
        state.alert_engine = AlertEngine(state.db, state.notifier)
        state.notifier.on_event = state.alert_engine.evaluate_event
        log.info("alert engine ready")
        # 运行时配置中心（引擎级参数热更新，配置灵活化）
        from gateway.runtime_config import RuntimeConfig
        state.runtime_config = RuntimeConfig(state.db)
        log.info("runtime config ready: %d keys", len(state.runtime_config.all()))
        # C1 历史 K 线本地缓存（回测/图表命中后不穿透券商）
        from gateway.kline_cache import KlineCache
        state.kline_cache = KlineCache(
            state.db, ttl_daily=settings.kline_cache_ttl_daily,
            ttl_intraday=settings.kline_cache_ttl_intraday)
        log.info("kline cache ready: %s", state.kline_cache.stats().get("rows"))
        # WAL 初始化（用于启动恢复）
        from gateway.wal import WAL
        wal_path = settings.db_path.parent / "wal.jsonl"
        state.wal = WAL(str(wal_path))
        log.info("wal ready: %s", wal_path)
        # 1. 加载持久化的券商连接 + 引导连接
        state.broker_manager.load_persisted()
        _bootstrap_from_env()
        # 2. 启动各连接 bridge（异步泵，使用应用事件循环）
        # 注意：行情管道（quote handler）的注册在 SyncEngine 构造之后统一进行（C3），
        # 原实现在此处注册时 state.sync_engine 尚为 None → AttributeError 被吞，
        # 每次启动所有 active 连接行情管道全断（latest_quotes 恒空/风控最新价恒 None）。
        for conn in state.broker_manager.all_connections():
            if conn.cfg.active:
                try:
                    await conn.bridge.start()
                    conn.connected = conn.adapter.is_connected()
                    log.info("broker connection started: %s (%s) connected=%s",
                             conn.cfg.name, conn.cfg.conn_id, conn.connected)
                except Exception as exc:  # noqa: BLE001
                    log.warning("broker start failed %s: %s", conn.cfg.conn_id, exc)
        state.bridge = state.broker_manager.active_bridge()
        state.gateway = state.bridge.gateway if state.bridge else None
        # 2.5 交易日历感知调度：用券商真实日历刷新，失败回退周末规则
        from gateway.trading_session import default_session
        try:
            if state.bridge is not None:
                cal = await state.bridge.call(state.bridge.gateway.get_trading_calendar)
                if not default_session.refresh_from_calendar(cal or []):
                    default_session.use_fallback()
        except Exception as exc:  # noqa: BLE001
            default_session.use_fallback()
            log.warning("trading calendar unavailable, fallback weekday rule: %s", exc)
        log.info("trading session: %s", default_session.stats())
        # 3. 同步引擎 + WS 管理 + 风控 + MCP
        # 阶段 0-B（F4）：消灭双 RiskManager。模块级 `risk` 为唯一实例并挂载 MCP；
        # 运行时把持久化风控参数载入同一实例（原地 update），state.risk 指向它，
        # REST 路由 / 各引擎 / MCP 工具因此共享同一个风控闸门，配置变更对全部入口即时生效。
        state.risk = risk
        if state.db is not None:
            try:
                row = state.db.query_one(
                    "SELECT params_json FROM risk_config WHERE scope='global'")
                if row and row.get("params_json"):
                    import json
                    state.risk.update_from(json.loads(row["params_json"]))
            except Exception as exc:  # noqa: BLE001
                log.warning("load risk config failed, use defaults: %s", exc)
        state.started_at = time.time()
        # 数据库自动备份：启动时一次 + 后台周期（关闭前再备份一次）
        if settings.db_backup_enabled:
            from gateway.db_backup import DBBackup
            db_backup = DBBackup(
                settings.db_path, keep=settings.db_backup_keep,
                interval=settings.db_backup_interval, db=state.db)
            db_backup.backup_once("startup")
            await db_backup.start()
            log.info("db backup enabled: interval=%.0fs keep=%d",
                     settings.db_backup_interval, settings.db_backup_keep)
        # 系统状态广播：周期向 WS 客户端推送（前端全局 Dashboard 用）
        _system_task = asyncio.create_task(_system_broadcast_loop())
        # 行情共享总线（默认内存；配置 redis 时跨进程共享）
        from gateway.quote_bus import create_quote_bus
        state.quote_bus = create_quote_bus(
            redis_url=settings.quote_bus_redis_url,
            enabled=(settings.quote_bus_backend == "redis"))
        log.info("quote bus: %s", state.quote_bus.stats().get("mode"))
        # B2 出站 webhook（事件投递给外部服务，HMAC 签名 + 重试）
        from gateway.webhook_out import WebhookOut
        state.webhook_out = WebhookOut(
            state.db, base_delay=settings.webhook_out_retry_backoff)
        log.info("webhook out ready: %d subs", len(state.webhook_out._configs()))
        state.sync_engine = SyncEngine(state.broker_manager, state.db,
                                       quote_bus=state.quote_bus,
                                       risk=state.risk, notifier=state.notifier,
                                       webhook_out=state.webhook_out,
                                       runtime_config=state.runtime_config)
        state.ws_manager = WSManager(state.sync_engine)
        state.sync_engine.on_notify(state.ws_manager.broadcast)
        # C3 修复：行情管道统一在此（SyncEngine 构造之后）为所有连接注册。
        # 运行期新增连接由下方 _pump_guard 兜底补注册，保证行情管道永不缺失。
        _register_quote_handlers = state.sync_engine.on_event
        for conn in state.broker_manager.all_connections():
            if conn.bridge is not None:
                conn.bridge.on("quote", _register_quote_handlers)
        state.sync_engine.start_batch()
        await state.sync_engine.start_account_snapshots(interval=5.0)
        # 3.5 券商连接健康状态机 + 自动重连
        from gateway.health import BrokerHealthMonitor
        state.health_monitor = BrokerHealthMonitor(
            state.broker_manager, state.ws_manager.broadcast, check_interval=5.0)
        await state.health_monitor.start()
        # 3.5.1 行情泵托管：确保所有已连接连接的行情泵跑在应用主事件循环上（幂等自愈）。
        # 覆盖运行期新增/手动 connect/健康重连等路径——泵一旦因异常退出会自动补建。
        _pump_task = None

        async def _pump_guard():
            while True:
                await asyncio.sleep(2.0)
                loop = asyncio.get_running_loop()
                for conn in state.broker_manager.all_connections():
                    b = conn.bridge
                    if b is None or not (conn.cfg.active or conn.connected):
                        continue
                    # C3：运行期新增/手动 connect 的连接补注册行情 handler（幂等去重）。
                    # 否则新增连接的行情管道永远缺失（原实现只在启动循环注册一次）。
                    if state.sync_engine is not None:
                        try:
                            b.ensure_handler("quote", state.sync_engine.on_event)
                        except Exception as exc:  # noqa: BLE001
                            log.warning("quote handler guard %s: %s", conn.cfg.conn_id, exc)
                    if not b.pump_running():
                        try:
                            b.start_pump_on(loop)
                        except Exception as exc:  # noqa: BLE001
                            log.warning("pump guard %s: %s", conn.cfg.conn_id, exc)

        _pump_task = asyncio.create_task(_pump_guard())
        # 4. 涨停监控 + 算法单引擎（事件推送到 WS）
        from tools.limitup import LimitUpMonitor
        from tools.algo import AlgoEngine
        from tools.condition_order import ConditionOrderEngine
        state.limitup_monitor = LimitUpMonitor(state.broker_manager, state.risk,
                                               state.ws_manager.broadcast, wal=state.wal)
        state.algo_engine = AlgoEngine(state.broker_manager, state.risk,
                                       state.ws_manager.broadcast, wal=state.wal)
        state.condition_engine = ConditionOrderEngine(
            state.broker_manager, state.risk, state.db, state.ws_manager.broadcast,
            wal=state.wal, notifier=state.notifier)
        state.condition_engine.load_from_db()
        await state.condition_engine.start(interval=2.0)
        # 4.3 订单超时守护：pending 委托超时自动撤单 + 告警（P1）
        from gateway.order_watchdog import OrderWatchdog
        state.order_watchdog = OrderWatchdog(
            state.broker_manager, timeout=settings.order_watchdog_timeout,
            interval=settings.order_watchdog_interval,
            enabled=settings.order_watchdog_enabled,
            on_event=state.ws_manager.broadcast, notifier=state.notifier)
        await state.order_watchdog.start()
        # 4.6 统一信号入口 + 物理旁路
        from gateway.signal_router import SignalRouter
        state.signal_router = SignalRouter(
            state.broker_manager, state.risk, state.db, state.wal,
            state.notifier, state.ws_manager.broadcast)
        # 4.5 WAL 启动重放：恢复未完成的算法单（F3：按 algo_id 聚合终态，仅重放最终态
        # pending/running 者——原实现逐个 create 记录重放、不查其后是否有 final/cancel，
        # 每次重启都会把历史每个算法单（含已取消/已完成）重新 _run → 大规模重复拆单下单）
        def _replay_algos() -> None:
            if not state.wal:
                return
            try:
                records = state.wal.all_records()
            except Exception as exc:  # noqa: BLE001
                log.warning("wal replay read failed: %s", exc)
                return
            # 按 algo_id 聚合：create 之后若出现 final/cancel（含 pause 后 final），
            # 该单最终态不是运行中，跳过；仅保留「最后动作仍是 create 且状态 pending/running」者。
            pending_jobs: dict[str, dict] = {}
            for rec in records:
                if rec.get("entity") != "algo":
                    continue
                aid = str(rec.get("entity_id") or "")
                op = rec.get("op", "")
                if not aid or op in ("pause", "resume"):
                    continue
                if op == "create":
                    pending_jobs[aid] = rec.get("payload", {})
                elif op in ("final", "cancel"):
                    pending_jobs.pop(aid, None)
            replayed = 0
            for aid, payload in pending_jobs.items():
                if payload.get("status", "") not in ("pending", "running"):
                    continue
                try:
                    asyncio.create_task(state.algo_engine.submit(
                        payload.get("code", ""), payload.get("direction", "buy"),
                        int(payload.get("volume", 0)), payload.get("algo", "twap"),
                        int(payload.get("duration", 300)), int(payload.get("slices", 5)),
                        payload.get("price_type", "market"),
                        float(payload.get("limit_price", 0) or 0), payload.get("remark", "")))
                    replayed += 1
                except Exception as exc:  # noqa: BLE001
                    log.warning("wal replay algo failed: %s", exc)
            log.info("wal algo replay: %d pending job(s) restarted", replayed)

        _replay_algos()
        # 4.7 A2 委托对账核销：启动后立即对账一次，并定时巡检
        from gateway.reconcile import OrderReconciler
        state.reconciler = OrderReconciler(
            state.broker_manager, wal=state.wal, db=state.db,
            on_event=state.ws_manager.broadcast, notifier=state.notifier)
        try:
            res = await state.reconciler.reconcile()
            log.info("startup reconcile: %s", {k: v for k, v in res.items() if k != "details"})
        except Exception as exc:  # noqa: BLE001
            log.warning("startup reconcile failed: %s", exc)
        await state.reconciler.start(interval=settings.reconcile_interval)
        # 5. 回测任务队列
        state.backtest_queue = BacktestQueue(max_workers=2)
        state.backtest_queue.on_event(state.ws_manager.broadcast)

        # 6. P1 模拟盘引擎（基于实时行情盯市，独立于真实券商）
        from paper.paper_engine import PaperEngine
        state.paper_engine = PaperEngine().init(state.db)
        log.info("paper trading engine ready")

        # 6.5 P0 策略运行容器：把生成的策略当作实盘/模拟机器人运行（进程内异步循环）
        from tools.strategy_runtime import StrategyRuntime
        state.strategy_runtime = StrategyRuntime(state)
        restored = state.strategy_runtime.restore()
        log.info("strategy runtime ready: restored %d running instance(s)", restored)

        log.info("qmt_work started (real broker mode)")
        try:
            yield
        finally:
            # 优雅停机：先广播关闭事件，前端据此进入离线态并停止重连风暴
            if state.ws_manager is not None:
                try:
                    await state.ws_manager.broadcast(
                        "system", {"event": "shutdown",
                                   "message": "服务正在关闭，稍后将自动重连"})
                except Exception:  # noqa: BLE001
                    pass
            if _system_task is not None:
                _system_task.cancel()
                try:
                    await _system_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            if db_backup is not None and settings.db_backup_enabled:
                await db_backup.stop()
            if state.reconciler:
                await state.reconciler.stop()
            if state.health_monitor:
                await state.health_monitor.stop()
            if _pump_task is not None:
                _pump_task.cancel()
                try:
                    await _pump_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            if state.limitup_monitor:
                await state.limitup_monitor.stop()
            if state.condition_engine:
                await state.condition_engine.stop()
            if state.order_watchdog:
                await state.order_watchdog.stop()
            if state.notifier:
                await state.notifier.close()
            if state.webhook_out:
                await state.webhook_out.close()
            if state.wal:
                state.wal.close()
            await state.backtest_queue.close()
            await state.sync_engine.stop()
            for conn in state.broker_manager.all_connections():
                try:
                    await conn.bridge.stop()
                except Exception:  # noqa: BLE001
                    pass
            log.info("qmt_work stopped")

    @asynccontextmanager
    async def combined_lifespan(app: FastAPI):
        async with mcp_app.router.lifespan_context(mcp_app):
            async with app_lifespan(app):
                yield

    app = FastAPI(
        title="qmt_work 量化交易网关",
        version=__version__,
        description=(
            "**qmt_work** —— 面向真实券商（迅投 MiniQMT 系）的量化交易统一网关。\n\n"
            "### 鉴权\n"
            "所有 `/api/v1/**` 请求需携带密钥，二者任选其一：\n"
            "- `Authorization: Bearer <key>`\n"
            "- `X-API-Key: <key>`\n\n"
            "主密钥（`QMT_API_KEY`）拥有全部权限；`/api/v1/api-keys` 可签发多组子密钥，"
            "支持 scope 分级（`read` / `trade` / `admin`）、每密钥独立限流、IP 白名单、过期时间与轮换宽限期。\n"
            "本机回环（127.0.0.1）请求免鉴权，便于桌面客户端直连。\n\n"
            "### 统一响应包\n"
            "```json\n{\"code\": 0, \"message\": \"ok\", \"data\": {}}\n```\n"
            "业务异常返回 HTTP 200 + `code != 0`；`code=503` 表示尚未连接券商客户端，"
            "请先到「券商连接」页添加并连接（**不会返回任何模拟数据**）。\n\n"
            "### 实时推送\n"
            "`WS /ws?token=<key>`：行情微批帧（`quotes`）、委托/成交、算法单、条件单、"
            "涨停触发、连接健康、对账结果。断线重连后自动补发最近 30 秒事件窗口。\n\n"
            "### 其他入口\n"
            "- `GET /api/v1/metrics`：Prometheus 文本格式指标\n"
            "- `POST /mcp`：MCP Streamable HTTP（供 Agent / Claude 等 MCP 客户端接入）\n"
        ),
        openapi_tags=OPENAPI_TAGS,
        lifespan=combined_lifespan,
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
    )

    limiter = RateLimiter(settings.rate_limit_window, settings.rate_limit_max)
    state.rate_limiter = limiter
    # 多密钥鉴权：主密钥（settings.api_key 全权限）+ api_keys 表（按 scope/限流）
    from gateway.apikey import ApiKeyStore
    state.apikey_store = ApiKeyStore()
    from gateway.metrics import get_metrics
    state.metrics = get_metrics()
    app.middleware("http")(make_auth_middleware(
        settings.api_key,
        lambda: state.apikey_store,
        lambda: state.rate_limiter))
    app.middleware("http")(make_rate_limit_middleware(limiter))
    # X-Request-ID 链路追踪（最外层，确保 auth/rate_limit 及路由日志都带请求号）
    app.middleware("http")(request_id_middleware)
    # CORS（P1）：可配置跨域来源（QMT_CORS_ORIGINS 逗号分隔；空=不启用，仅同源）。
    # 置于最后添加 = 最外层，确保 OPTIONS 预检请求不被鉴权/限流拦截。
    if settings.cors_origins.strip():
        from fastapi.middleware.cors import CORSMiddleware
        origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
        app.add_middleware(CORSMiddleware, allow_origins=origins,
                           allow_credentials=True,
                           allow_methods=["*"], allow_headers=["*"])

    app.include_router(router)
    _apply_openapi_meta(app)
    app.mount("/mcp", mcp_app, name="mcp")

    static_dir = BASE_DIR / "static"
    if static_dir.exists():
        app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")

    return app


app = create_app()

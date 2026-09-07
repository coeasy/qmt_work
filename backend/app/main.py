"""FastAPI 统一后端入口。

承载：REST 网关 + MCP（Streamable HTTP）+ 数据同步引擎(WebSocket) + 回测任务队列 + 静态托管。

券商客户端通过 BrokerManager 统一管理（多券商 / 多账户 / 多客户端版本），全部为真实 SDK 调用，无 mock。

启动编排（2026-09-07 R1 重构）：
  lifespan 拆分为 6 个阶段模块 + shutdown 逆序关闭（见 app/bootstrap/）。
  本文件仅负责 FastAPI 构建、include_router、中间件挂接；不再承担 lifespan 业务逻辑。
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.config import BASE_DIR, settings
from app.logging_setup import setup_logging
from app.middleware.envelope import EnvelopeMiddleware
from app.middleware.request_id import request_id_middleware
from app.routes import router
from app.state import state
from app.version import __version__
from gateway.auth import make_auth_middleware
from gateway.rate_limit import RateLimiter, make_rate_limit_middleware
from gateway.risk import RiskManager
from mcp_server import build_mcp

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
# 模块级 risk 单例 → 挂到 state 供 lifespan 阶段使用
# （REST/各引擎/MCP 工具都通过 state.risk 共享同一个闸门）
state.risk = risk
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


def create_app() -> FastAPI:
    """FastAPI 工厂。

    Lifespan 编排已拆分为 6 个阶段模块（app/bootstrap/），本函数只负责：
    1. 创建 FastAPI 实例
    2. 注册全局错误处理 + openapi 元信息
    3. 挂中间件（envelope 兜底 → request_id → auth/rate_limit → cors）
    4. 挂静态资源 + include_router

    阶段启动顺序：phase_db → phase_broker → phase_engines →
                   phase_watchdogs → phase_replay → phase_misc
    停机逆序：见 app/bootstrap/shutdown.py
    """
    from app.bootstrap import phase_db, phase_broker, phase_engines
    from app.bootstrap import phase_watchdogs, phase_replay, phase_misc
    from app.bootstrap.shutdown import shutdown as _shutdown

    @asynccontextmanager
    async def app_lifespan(app: FastAPI):
        # 6 阶段顺序启动
        phases = [
            ("db",        phase_db.setup),
            ("broker",    phase_broker.setup),
            ("engines",   phase_engines.setup),
            ("watchdogs", phase_watchdogs.setup),
            ("replay",    phase_replay.setup),
            ("misc",      phase_misc.setup),
        ]
        for name, fn in phases:
            try:
                await fn(app)
                log.info("bootstrap phase %s done", name)
            except Exception as exc:  # noqa: BLE001
                log.exception("bootstrap phase %s failed: %s", name, exc)
        try:
            yield
        finally:
            # 优雅停机（逆序关闭所有引擎/服务）
            await _shutdown(app)

    @asynccontextmanager
    async def combined_lifespan(app: FastAPI):
        async with mcp_app.router.lifespan_context(mcp_app):
            async with app_lifespan(app):
                yield


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
            "- `POST /mcp`：MCP Streamable HTTP（供 MCP 客户端接入）\n"
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
    # 响应信封兜底：包在 request_id 之外（内层），所有 /api/v1/* 端点自动符合 {code:0,data} 契约
    from app.middleware.envelope import EnvelopeMiddleware
    app.add_middleware(EnvelopeMiddleware)
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

    @app.post("/api/v1/scheduler/shutdown")
    async def _desktop_shutdown():
        # 桌面壳优雅停机：先停行情同步引擎并断开券商连接；
        # 失败不影响 Electron 的进程树强杀兜底（main.cjs killBackendTree）。
        try:
            se = getattr(state, "sync_engine", None)
            if se and hasattr(se, "stop"):
                await se.stop()
        except Exception as exc:  # noqa: BLE001
            log.warning("sync stop on shutdown failed: %s", exc)
        try:
            bm = getattr(state, "broker_manager", None)
            if bm and hasattr(bm, "disconnect_all"):
                bm.disconnect_all()
        except Exception as exc:  # noqa: BLE001
            log.warning("broker disconnect on shutdown failed: %s", exc)
        return {"ok": True, "shutting_down": True}

    app.mount("/mcp", mcp_app, name="mcp")

    # UTF-8 强制声明中间件（P1 修复）：StaticFiles 默认给 .js/.css 返回的
    # Content-Type 不含 charset，中文 Windows 下浏览器可能以 GBK 解码导致
    # 全站中文乱码。此处对所有 text/* 响应补上 charset=utf-8。
    @app.middleware("http")
    async def utf8_charset_middleware(request, call_next):
        resp = await call_next(request)
        ct = resp.headers.get("content-type", "")
        if ct.startswith("text/") and "charset" not in ct.lower():
            resp.headers["content-type"] = ct + "; charset=utf-8"
        return resp

    static_dir = BASE_DIR / "static"
    if static_dir.exists():
        # index.html 必须走 no-cache：StaticFiles(html=True) 默认不发缓存头，浏览器会缓存
        # 旧 index.html → 引用旧构建的 JS hash → 前端重新构建后用户刷新仍加载旧代码，
        # 表现为「修复不生效 / 问题依然存在」。带 hash 的 /assets/* 资源可长缓存。
        from fastapi.responses import FileResponse

        @app.get("/", include_in_schema=False)
        async def _index():
            return FileResponse(
                str(static_dir / "index.html"),
                headers={"Cache-Control": "no-cache, must-revalidate"},
            )

        app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")

    return app


app = create_app()

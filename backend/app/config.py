"""全局配置：优先级 环境变量(QMT_*) > exe 同目录 qmt_work_config.json > .env > 默认值。

不再有 mock 模式：所有行情/交易/账户均走真实券商客户端（XTQuant 等）。
可选引导连接（仅当提供了 QMT_ACCOUNT_ID 等时，启动时自动建立默认券商连接）：
- QMT_BROKER_ID：券商档案 id（默认 guojin）
- QMT_CLIENT_PATH：券商客户端 userdata_mini 目录
- QMT_ACCOUNT_ID：资金账号
- QMT_ACCOUNT_TYPE：STOCK/CREDIT/OPTION/FUTURES
- QMT_SESSION_ID：迅投会话 id（整数）

打包运行（PyInstaller EXE）时：
- 配置文件：<exe 同目录>/qmt_work_config.json，首次启动**自动生成**（含全部可配项 + 中文说明）
- 数据库：默认 <exe 同目录>/data/app.db；日志：默认 <exe 同目录>/logs
  （可在配置文件中以相对路径（相对 exe 同目录）或绝对路径覆盖）
- 桌面壳通过 QMT_DB_PATH / QMT_PORT_FILE 环境变量覆盖（env 优先级最高，行为不变）
"""
import json
import sys
from pathlib import Path
from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


def _is_frozen() -> bool:
    """PyInstaller 打包运行时为 True。"""
    return bool(getattr(sys, "frozen", False))


def exe_dir() -> Path:
    """运行根目录：打包时为 exe 所在目录；开发时为后端根目录。"""
    if _is_frozen():
        return Path(sys.executable).resolve().parent
    return BASE_DIR


def config_file() -> Path:
    """配置文件路径：<exe 同目录>/qmt_work_config.json。"""
    return exe_dir() / "qmt_work_config.json"


def _default_config_payload() -> dict[str, Any]:
    """默认配置内容（首次启动自动生成，带中文说明；`_` 开头键为注释，解析时忽略）。"""
    return {
        "_readme": "qmt_work 配置文件（UTF-8）。修改后重启生效。"
                   "优先级：环境变量(QMT_*) > 本文件 > .env > 默认值。"
                   "`_` 开头的键是说明，不会被解析。"
                   "db_path / log_dir 填相对路径时以本文件所在目录（exe 同目录）为基准。",
        "app_name": "qmt_work",
        # 默认仅本机可访问（0-E 安全基线）；需远程访问请改为 0.0.0.0
        # 并务必修改 api_key，否则启动自检会拒绝在远程监听下使用默认密钥
        "host": "127.0.0.1",
        "port": 21117,
        # ---- 存储与日志 ----
        "db_path": "data/app.db",
        "log_dir": "logs",
        # ---- 日志聚合与告警（工业级可观测性）----
        # log_json=true 时输出结构化 JSON（逐行），便于接入 Loki/ELK 等日志聚合系统
        "log_json": False,
        # 日志告警：ERROR 及以上级别日志推送到该 webhook（HMAC-SHA256 可选），
        # 空=关闭。格式：{url}|{secret} 或纯 url；用于对接钉钉/企业微信/自定义监控
        "log_alert_webhook": "",
        "log_alert_level": "ERROR",
        # ---- 数据库自动备份（防单点损坏）----
        "db_backup_enabled": True,
        "db_backup_interval": 3600.0,
        "db_backup_keep": 10,
        # ---- 网关鉴权（生产环境务必修改）----
        "api_key": "qmt-dev-key",
        # ---- 可选引导连接（不填则需在「券商连接」页添加）----
        "broker_id": "",
        "client_path": "",
        "account_id": "",
        "account_type": "STOCK",
        "session_id": 0,
        # ---- 交易二次确认 ----
        "signal_confirm_threshold": 100_000.0,
        "totp_secret": "",
        "totp_digits": 6,
        # ---- 外部信号 webhook 签名密钥 ----
        "webhook_secret": "",
        # ---- 风控默认值（运行期可在「设置 → 风控」调整）----
        "risk_max_amount": 100_000.0,
        "risk_min_qty": 100,
        "risk_max_position_ratio": 0.3,
        "risk_max_single_position_ratio": 0.2,
        "risk_max_orders_per_min": 30,
        "risk_daily_amount_limit": 0.0,
        "risk_daily_loss_limit": 0.0,
        "risk_per_code_daily_orders": 0,
        # ---- 预交易风控扩展（0/空 = 关闭）----
        "risk_price_deviation_pct": 0.0,
        "risk_symbol_allow": "",
        "risk_symbol_deny": "",
        # ---- 订单超时守护（pending 超时自动撤单）----
        "order_watchdog_enabled": True,
        "order_watchdog_timeout": 60.0,
        "order_watchdog_interval": 5.0,
        # ---- 通知去重静默期（秒；0=关闭）----
        "notify_dedup_seconds": 0.0,
        # ---- CORS（逗号分隔来源；空=不启用，仅同源）----
        "cors_origins": "",
        # ---- 限流 ----
        "rate_limit_window": 60,
        "rate_limit_max": 120,
        # ---- 行情共享总线（memory 或 redis）----
        "quote_bus_backend": "memory",
        "quote_bus_redis_url": "redis://127.0.0.1:6379/0",
        # ---- 引擎轮询/巡检间隔 ----
        "reconcile_interval": 300.0,
        "kline_cache_ttl_daily": 21600.0,
        "kline_cache_ttl_intraday": 60.0,
        "webhook_out_retry_backoff": 2.0,
    }


def ensure_config_file() -> Path:
    """若配置文件不存在则自动生成（首次启动）；返回配置路径。"""
    f = config_file()
    if f.exists():
        return f
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(_default_config_payload(), ensure_ascii=False, indent=2),
                     encoding="utf-8")
    except OSError:
        pass  # 目录不可写时静默（仍可用默认值运行）
    return f


def _json_config_source() -> dict[str, Any]:
    """从 exe 同目录 qmt_work_config.json 读取配置（忽略 `_` 开头键，相对路径解析为 exe 同目录）。"""
    f = config_file()
    if not f.exists():
        return {}
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    out: dict[str, Any] = {}
    for k, v in data.items():
        if not isinstance(k, str) or k.startswith("_"):
            continue
        if v is None:
            continue
        out[k.replace("-", "_").lower()] = v
    for pkey in ("db_path", "log_dir"):
        if pkey in out and isinstance(out[pkey], str) and not Path(out[pkey]).is_absolute():
            out[pkey] = str((exe_dir() / out[pkey]).resolve())
    return out


def _default_db_path() -> Path:
    return exe_dir() / "data" / "app.db"


def _default_log_dir() -> Path:
    return exe_dir() / "logs"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="QMT_", extra="ignore")

    app_name: str = "qmt_work"
    # 0-E 安全基线：默认仅本机可访问；远程监听(0.0.0.0)且未改默认 api_key 时启动自检拒绝启动
    host: str = "127.0.0.1"
    port: int = 21117

    # 引导连接（可选；不提供则需在 UI 中配置券商连接）
    broker_id: str = ""
    client_path: str = ""
    account_id: str = ""
    account_type: str = "STOCK"
    session_id: int = 0

    # 网关鉴权：远程调用必须携带的 API Key（开发默认值，生产必须修改）
    api_key: str = "qmt-dev-key"

    # 交易二次确认（大额下单需确认）
    signal_confirm_threshold: float = 100_000.0
    totp_secret: str = ""          # 非空时启用 TOTP 二次确认（RFC 6238）
    totp_digits: int = 6

    # 外部信号 webhook 签名密钥（HMAC-SHA256）；非空时校验 X-Signature 头
    webhook_secret: str = ""

    # 风控默认值（运行期可在「设置 → 风控」页调整，持久化到 risk_config 表）
    risk_max_amount: float = 100_000.0
    risk_min_qty: int = 100
    risk_max_position_ratio: float = 0.3
    risk_max_single_position_ratio: float = 0.2
    risk_max_orders_per_min: int = 30
    # 日级风控（B4；0 = 不启用，跨自然日自动重置）
    risk_daily_amount_limit: float = 0.0        # 日累计下单金额上限
    risk_daily_loss_limit: float = 0.0          # 日亏损熔断阈值（净值较日初回撤）
    risk_per_code_daily_orders: int = 0         # 单标的日下单次数上限
    # 预交易风控扩展（P1；0/空 = 关闭）：
    risk_price_deviation_pct: float = 0.0       # 下单价相对最新价允许偏离上限（0.05=±5%）
    risk_symbol_allow: str = ""                 # 标的白名单（逗号分隔；空=全部允许）
    risk_symbol_deny: str = ""                  # 标的黑名单（逗号分隔；命中即拒）

    # 存储与日志（默认跟随运行根目录：打包=exe 同目录 data/、logs/）
    db_path: Path = Field(default_factory=_default_db_path)
    log_dir: Path = Field(default_factory=_default_log_dir)

    # 日志聚合与告警（工业级可观测性）
    log_json: bool = False                 # true=结构化 JSON 输出（接入 Loki/ELK 等）
    log_alert_webhook: str = ""            # ERROR+ 日志推送 webhook（{url}|{secret} 或纯 url）
    log_alert_level: str = "ERROR"         # 日志告警最低级别

    # 数据库自动备份：启动时 + 周期性（秒）+ 关闭前各一次，保留最近 keep 份
    db_backup_enabled: bool = True
    db_backup_interval: float = 3600.0
    db_backup_keep: int = 10

    # 限流：窗口秒数 / 窗口内最大请求数（按 token/ip）
    rate_limit_window: int = 60
    rate_limit_max: int = 120

    # 行情共享总线：默认内存模式；redis 时跨进程共享行情（多实例部署用）
    quote_bus_backend: str = "memory"
    quote_bus_redis_url: str = "redis://127.0.0.1:6379/0"

    # 委托对账核销巡检间隔（秒）；WAL 中未核销委托与券商当日委托/成交比对
    reconcile_interval: float = 300.0

    # 历史 K 线本地缓存 TTL（C1）：日线 6h / 分钟线 60s
    kline_cache_ttl_daily: float = 6 * 3600.0
    kline_cache_ttl_intraday: float = 60.0

    # 出站 webhook（B2）：并发投递与重试退避基数（秒）
    webhook_out_retry_backoff: float = 2.0

    # 订单超时守护（P1）：pending/submitted 委托超过 timeout 秒未成交自动撤单（0=关闭）
    order_watchdog_enabled: bool = True
    order_watchdog_timeout: float = 60.0
    order_watchdog_interval: float = 5.0

    # 通知去重静默期（秒；0=关闭）：同一通知渠道 + 事件 + 标题在窗口内只发送一次
    notify_dedup_seconds: float = 0.0

    # CORS：允许的跨域来源（逗号分隔；空=不启用跨域，仅同源访问）
    cors_origins: str = ""

    # 阶段 5 Agent：LLM Provider 配置（缺 key 即 503 降级，绝不造假）
    agent_enabled: bool = False
    agent_provider: str = "openai"           # openai | anthropic
    agent_api_key: str = ""
    agent_model: str = ""                    # 空=使用 provider 默认模型
    agent_base_url: str = ""                 # 空=使用 provider 默认端点（支持自建/代理）

    @classmethod
    def settings_customise_sources(cls, settings_cls, init_settings, env_settings,
                                   dotenv_settings, file_secret_settings):
        """配置源优先级：初始化参数 > 环境变量(QMT_*) > exe 同目录 JSON 配置 > .env > 默认值。"""
        return (init_settings, env_settings, _json_config_source,
                dotenv_settings, file_secret_settings)


settings = Settings()
# 打包运行时：首次启动自动生成默认配置文件（开发模式不生成，避免污染仓库）
if _is_frozen():
    ensure_config_file()

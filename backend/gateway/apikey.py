"""多组 API Key 管理（工业级）：供其他服务调用 QMT 接口的密钥体系。

- api_keys 表持久化（key_hash/scopes/rate_limit/status/name）
- ApiKeyStore 内存缓存（启动加载 + 增删改刷新），verify(token) 返回密钥行
- scope 分级：market / trade / account / backtest / admin / *（通配）
- 兼容：settings.api_key 作为"主密钥"拥有全部权限（向后兼容 + loopback 免鉴权）
"""
import datetime
import hashlib
import logging
import threading
import time

log = logging.getLogger("qmt_work")

# 端点路径前缀 -> 所需 scope（用于鉴权时校验）
# 顺序敏感：更具体的前缀优先匹配
PATH_SCOPES = [
    ("/api/v1/market", "market"),
    ("/api/v1/quote", "market"),
    ("/api/v1/reference", "market"),
    ("/api/v1/factors", "market"),
    ("/api/v1/sync", "market"),
    ("/api/v1/trade", "trade"),
    ("/api/v1/algo", "trade"),
    ("/api/v1/limitup", "trade"),
    ("/api/v1/rebalance", "trade"),
    ("/api/v1/signal", "trade"),
    ("/api/v1/target-portfolio", "trade"),
    ("/api/v1/paper", "trade"),
    # 阶段 0-E：strategies/run 必须独立 scope——它启动策略代码（可真实下单），
    # 若沿用 strategies 的 backtest scope，backtest 权限子密钥即可启动实盘策略。
    # 必须置于 /api/v1/strategies 之前（前缀匹配顺序敏感）。
    ("/api/v1/strategies/run", "trade"),
    ("/api/v1/strategies", "backtest"),
    ("/api/v1/strategy-market", "backtest"),
    ("/api/v1/account", "account"),
    ("/api/v1/backtest", "backtest"),
    ("/api/v1/research", "backtest"),
    ("/api/v1/config", "admin"),
    ("/api/v1/api-keys", "admin"),
    ("/api/v1/notifiers", "admin"),
    ("/api/v1/audit", "admin"),
    ("/api/v1/brokers", "admin"),
    ("/api/v1/webhooks", "admin"),
    ("/api/v1/alerts", "admin"),
    ("/api/v1/notifications", "admin"),
    ("/api/v1/reconcile", "admin"),
    ("/api/v1/wal", "admin"),
    ("/api/v1/metrics", "admin"),
    ("/api/v1/quote-bus", "admin"),
    ("/api/v1/agent", "admin"),
]
# 公共路径（免鉴权，仅只读信息）：健康检查 / 就绪 / API 文档 / 前端静态页
PUBLIC_PREFIXES = (
    "/api/v1/health", "/api/v1/ready",
    "/api/docs", "/api/redoc", "/api/openapi.json",
)

# 未匹配（非公共）路径的兜底 scope：default-deny——任何未显式映射的端点
# 至少需要 admin scope 才能访问，杜绝「新端点/前缀漏配 → 默认放行」的越权面。
UNMATCHED_SCOPE = "admin"


def _is_public_path(path: str) -> bool:
    """静态页面与文档免鉴权（浏览器直开 UI / 查看 API 文档）；API 与 MCP 保持鉴权。"""
    if path in ("/", "/index.html"):
        return True
    if path.startswith("/assets/") or path.startswith("/favicon"):
        return True
    return any(path.startswith(p) for p in PUBLIC_PREFIXES)


def scope_for_path(path: str) -> str | None:
    """根据请求路径返回所需 scope；public 端点返回 None（免鉴权）。

    阶段 0-E：未匹配的端点返回 UNMATCHED_SCOPE（default-deny）。原实现返回 None，
    而 scope_match(None) 恒 True → 任意有效密钥（如 market 子密钥）即可访问
    未映射端点（/mcp、/agent 等），存在越权调用下单/管理接口的通道。
    """
    if _is_public_path(path):
        return None
    for prefix, scope in PATH_SCOPES:
        if path.startswith(prefix):
            return scope
    return UNMATCHED_SCOPE


def scope_match(required: str | None, scopes: str) -> bool:
    """scopes 是逗号分隔字符串（如 'trade,market'）；'*' 通配全部。"""
    if required is None:
        return True
    parts = {s.strip() for s in (scopes or "").split(",") if s.strip()}
    return "*" in parts or required in parts


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class ApiKeyStore:
    """API Key 内存缓存（线程安全，启动加载 + 增删改刷新）。"""

    def __init__(self, db=None):
        self._db = db
        self._by_hash: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._loaded = False

    def bind(self, db) -> None:
        self._db = db

    def reload(self) -> None:
        if self._db is None:
            return
        try:
            rows = self._db.query(
                "SELECT id, key_hash, name, scopes, rate_limit, status, created_at, "
                "ip_allow, expires_at, grace_until FROM api_keys")
            with self._lock:
                self._by_hash = {r["key_hash"]: dict(r) for r in rows}
            self._loaded = True
            log.info("api_keys loaded: %d", len(self._by_hash))
        except Exception as exc:  # noqa: BLE001
            log.warning("api_keys load failed: %s", exc)

    @staticmethod
    def _is_expired(row: dict) -> bool:
        """过期判断：expires_at 之后失效；grace_until 提供轮换宽限期。

        阶段 0-E：解析失败按「已过期」处理（返回 True）。原实现解析失败返回 False，
        意味着格式损坏/非法日期的 expires_at 会让密钥永不过期。
        """
        exp = (row.get("expires_at") or "").strip()
        if not exp:
            return False
        try:
            exp_ts = datetime.datetime.fromisoformat(exp).timestamp()
        except Exception:
            return True
        cutoff = exp_ts
        grace = (row.get("grace_until") or "").strip()
        if grace:
            try:
                cutoff = max(cutoff, datetime.datetime.fromisoformat(grace).timestamp())
            except Exception:
                pass
        return time.time() > cutoff

    @staticmethod
    def _ip_allowed(row: dict, client_ip: str) -> bool:
        """IP 白名单：空=不限；支持逗号与 CIDR 前缀（如 10.0.0.*）。"""
        allow = (row.get("ip_allow") or "").strip()
        if not allow:
            return True
        rules = {a.strip() for a in allow.split(",") if a.strip()}
        if not rules:
            return True
        for r in rules:
            if r == client_ip:
                return True
            if r.endswith("*") and client_ip.startswith(r.rstrip("*")):
                return True
        return False

    def verify(self, token: str, client_ip: str = "") -> dict | None:
        """校验 token，返回密钥行（含 scopes/rate_limit/status）或 None。

        额外检查：状态 active、未过期（grace_until 宽限）、来源 IP 在白名单内。
        """
        if not token or not self._loaded:
            return None
        h = hash_token(token)
        with self._lock:
            row = self._by_hash.get(h)
        if not row or row.get("status") != "active":
            return None
        if self._is_expired(row):
            return None
        if not self._ip_allowed(row, client_ip):
            return None
        return row

    def invalidate(self) -> None:
        """增删改密钥后调用，触发重新加载。"""
        self.reload()

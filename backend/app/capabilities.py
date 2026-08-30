"""G3 · 能力自描述层（Capability Registry）。

设计要点（借鉴 OpenBB 入口点自省 + Fincept 统一数据面思想，独立实现零代码复制）：
- 在 **模块导入期** 即可构建：直接扫描聚合后的 `app.routes.router.routes`，
  不依赖 FastAPI `app` 实例（build_mcp 在 create_app 之前运行，拿不到 app）。
- 每个 REST 端点被归一为一个 `Capability`：id / path / method / category /
  summary / risk_level / confirm_required / agent_visible / param_schema。
- 暴露策略（安全优先）：
    * GET（只读）端点 → `agent_visible=True`（让 Agent 看见上一轮升级的全部行情能力）
    * 写入型（POST/PUT/DELETE）端点 → `agent_visible=False`，保持人工确认护栏，
      不自动暴露，避免放大交易风险面（G8 风控铁律）
    * 基础设施/密钥类 GET（health/metrics/capabilities/scheduler/webhooks/api-keys/ws）
      → 列入 DENY 名单，不自动生成 tool，但仍出现在注册表供审计
- 与 G3-5 漂移门禁配合：CI/测试比对「agent_visible 的 GET 端点」与「已注册 MCP tool」，
  缺失即失败。

所有实现均为 qmt_work 独立编写，不复制任何 AGPL 项目的源码。
"""
from __future__ import annotations

import inspect
import re
from dataclasses import asdict, dataclass, field
from typing import Any

# --- 自动暴露为 MCP tool 的排除名单（基础设施 / 密钥管理，跳过自动生成） ---
_DENY_TOOL_PATHS = (
    "/api/v1/health",
    "/api/v1/metrics",
    "/api/v1/capabilities",
    "/api/v1/scheduler",
    "/api/v1/webhooks",
    "/api/v1/ws",
    "/api/v1/api-keys",
)

# 执行 / 密钥 / 基础设施域：一律不自动暴露（保持人工确认护栏，不放大交易风险面）。
# 这些域已有手工注册的 MCP tool（见 tools/*.py 与 mcp_server/__init__.py）。
_EXEC_DENY_DOMAINS = {
    "trade", "target-portfolio", "rebalance", "algo",
    "condition", "limitup", "signal", "brokers",
    "api-keys", "webhooks", "notifications", "alerts",
    "scheduler", "ws", "health", "metrics", "capabilities",
}

# 需「人工确认护栏」的写入型域（交易/资金执行类）。
_CONFIRM_DOMAINS = {
    "trade", "target-portfolio", "rebalance", "algo",
    "condition", "limitup", "signal",
}

# 已存在手工 MCP tool 的域（除 market 外）：自动暴露会与其产生重复，故整体排除。
# market 域仅手工覆盖了 6 个端点（quote/kline/search/stock_list/full_tick/tick），
# 其余（boards/etfs/rotation/overview/indices…）零覆盖 —— 通过 _MARKET_NAME_OVERRIDES
# 把已覆盖端点映射回手工 tool 名触发碰撞跳过，仅自动补齐缺口。
_MANUAL_TOOL_DOMAINS = {
    "account", "analysis", "backtest", "condition", "factors",
    "limitup", "position", "rebalance", "reference", "strategy",
    "strategy-market", "target-portfolio", "trade",
}

# market 域：已覆盖端点的路径 → 手工 tool 名（碰撞即跳过，不重复）。
_MARKET_NAME_OVERRIDES = {
    "/api/v1/market/quote": "get_quote",
    "/api/v1/market/kline": "get_kline",
    "/api/v1/market/search": "search_stocks",
    "/api/v1/market/stock_list": "get_stock_list",
    "/api/v1/market/full_tick": "get_full_tick",
    "/api/v1/market/tick": "get_tick",
}

# 参数类型若含以下关键字，说明 handler 依赖请求对象/依赖注入/文件等，
# 无法直接以 kwargs 调用 → 该端点不自动暴露（保持手动注册或后续专门适配）。
_UNSAFE_PARAM_HINTS = (
    "Request", "Response", "BackgroundTasks", "Depends", "WebSocket",
    "HTTPException", "Session", "File", "UploadFile", "Form", "Header",
    "Cookie", "Security", "starlette",
)


@dataclass
class ParamSpec:
    name: str
    type: str            # string | integer | number | boolean | array
    required: bool
    default: Any = None


@dataclass
class Capability:
    id: str
    path: str
    method: str
    category: str
    summary: str
    risk_level: str          # low | medium | high
    confirm_required: bool
    agent_visible: bool
    tool_name: str = ""
    param_schema: list[ParamSpec] = field(default_factory=list)
    endpoint: Any = None     # 路由 handler（仅用于自动暴露调用，不序列化）

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("endpoint", None)   # 函数对象不可序列化，剔除
        return d


def _category_of(path: str) -> str:
    """从 /api/v1/<cat>/... 提第一路径段作为分类。"""
    rest = path
    if rest.startswith("/api/v1/"):
        rest = rest[len("/api/v1/"):]
    elif rest.startswith("/api/v1"):
        rest = rest[len("/api/v1"):]
    seg = rest.split("/")[0] if rest else ""
    return seg or "root"


def _is_unsafe_param(annotation: Any) -> bool:
    if annotation is inspect.Parameter.empty:
        return False
    text = str(annotation)
    return any(h in text for h in _UNSAFE_PARAM_HINTS)


def _simple_type(annotation: Any) -> str:
    text = str(annotation)
    if "int" in text:
        return "integer"
    if "float" in text:
        return "number"
    if "bool" in text:
        return "boolean"
    if "list" in text or "List" in text:
        return "array"
    return "string"


def _tool_name_of(path: str, method: str) -> str:
    """由路径生成 MCP tool 名：/api/v1/market/board/constituents → market_board_constituents。

    路径参数 {param} 转为 by_param，保证生成的函数名是合法 Python 标识符。
    """
    seg = path
    for p in ("/api/v1", "/api"):
        if seg.startswith(p):
            seg = seg[len(p):]
            break
    seg = re.sub(r"\{(\w+)\}", r"by_\1", seg)   # /x/{id} → /x/by_id
    slug = seg.strip("/").replace("/", "_").replace("-", "_")
    return f"{method.lower()}_{slug}" if slug else f"{method.lower()}_root"


def _classify(cap: "Capability") -> None:
    """就地填充 risk_level / confirm_required / agent_visible。

    自动暴露策略（安全优先，对齐 G8 风控铁律）：
    - 基础设施/密钥路径（_DENY_TOOL_PATHS）→ 不暴露 tool
    - 执行/密钥/基础设施域（_EXEC_DENY_DOMAINS）→ 不暴露 tool（保持人工确认护栏）
    - 已有手工 tool 的域（_MANUAL_TOOL_DOMAINS，market 除外）→ 不暴露（避免重复）
    - 其余 GET 只读端点 → agent_visible=True，交给自动生成器
    - 写入型端点 → 不自动暴露，confirm_required 视域而定
    """
    if cap.path in _DENY_TOOL_PATHS or cap.category in _EXEC_DENY_DOMAINS:
        cap.agent_visible = False
        cap.risk_level = "low"
        cap.confirm_required = False
        return
    if cap.method == "GET":
        # market 域仅补齐手工未覆盖的缺口（其余靠 _MARKET_NAME_OVERRIDES 碰撞跳过）
        in_manual_domain = cap.category in _MANUAL_TOOL_DOMAINS and cap.category != "market"
        cap.agent_visible = not in_manual_domain
        cap.risk_level = "low"
        cap.confirm_required = False
    else:
        cap.agent_visible = False
        cap.confirm_required = cap.category in _CONFIRM_DOMAINS
        cap.risk_level = "high" if cap.confirm_required else "medium"


def _iter_routes(obj, prefix: str = ""):
    """递归展开聚合路由，产出 (full_path, route)。

    新版 FastAPI 把 `include_router` 的子路由包成 `_IncludedRouter`
    （真实路由挂在 `.original_router.routes`，且自身无 `.endpoint`）；旧版则
    直接平铺 `APIRoute`。统一处理：遇到 `_IncludedRouter` 就下钻到
    `original_router`；其余「含 routes 但无 endpoint」的对象同样下钻；叶子
    `APIRoute` 则按其相对 path 拼上前缀得到完整路径。
    """
    for route in obj.routes:
        orig = getattr(route, "original_router", None)
        if orig is not None:
            yield from _iter_routes(orig, prefix)
        elif getattr(route, "routes", None) and not hasattr(route, "endpoint"):
            yield from _iter_routes(route, prefix)
        else:
            rp = getattr(route, "path", "") or ""
            # newer FastAPI 将 include_router 的子路由扁平化为纯 APIRoute，
            # 其 path 已含完整前缀（如 /api/v1/brokers/auto-detect），
            # 此时不可再叠加聚合前缀，否则会产生 /api/v1/api/v1/ 的双前缀。
            if prefix and rp.startswith(prefix):
                p = rp
            else:
                p = prefix + rp
            yield p.replace("//", "/"), route


def _route_endpoints():
    """惰性导入聚合路由，返回 (path, methods:set, endpoint_func, summary)。"""
    from app.routes import router as aggregated

    out = []
    for path, route in _iter_routes(aggregated, aggregated.prefix or ""):
        if not path or not path.startswith("/api/v1"):
            continue
        methods = getattr(route, "methods", None) or set()
        endpoint = getattr(route, "endpoint", None)
        if endpoint is None or not callable(endpoint):
            continue
        doc = (endpoint.__doc__ or "").strip()
        summary = doc.splitlines()[0].strip() if doc else ""
        out.append((path, methods, endpoint, summary))
    return out


def build_capabilities() -> list[Capability]:
    """扫描全部 REST 路由，产出统一能力注册表（幂等，可多次调用）。"""
    caps: list[Capability] = []
    seen_names: dict[str, int] = {}
    for path, methods, endpoint, summary in _route_endpoints():
        for method in sorted(methods):
            method = method.upper()
            # 跳过非标准方法（HEAD/OPTIONS 由框架自动产生）
            if method not in ("GET", "POST", "PUT", "DELETE", "PATCH"):
                continue
            tool_name = _tool_name_of(path, method)
            # 同名去重（极少见）：追加序号
            if tool_name in seen_names:
                seen_names[tool_name] += 1
                tool_name = f"{tool_name}_{seen_names[tool_name]}"
            else:
                seen_names[tool_name] = 0

            # 解析参数 schema；若存在无法安全直接调用的参数，则该端点不自动暴露
            params: list[ParamSpec] = []
            auto_safe = True
            try:
                sig = inspect.signature(endpoint)
            except (ValueError, TypeError):
                auto_safe = False
                sig = None
            if sig is not None:
                for pname, p in sig.parameters.items():
                    if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
                        auto_safe = False
                        break
                    if _is_unsafe_param(p.annotation):
                        auto_safe = False
                        break
                    has_default = p.default is not inspect.Parameter.empty
                    params.append(ParamSpec(
                        name=pname,
                        type=_simple_type(p.annotation),
                        required=not has_default,
                        default=p.default if has_default else None,
                    ))

            cap = Capability(
                id=f"{method}:{path}",
                path=path,
                method=method,
                category=_category_of(path),
                summary=summary,
                risk_level="low",
                confirm_required=False,
                agent_visible=False,
                tool_name=tool_name if auto_safe else "",
                param_schema=params if auto_safe else [],
                endpoint=endpoint if auto_safe else None,
            )
            _classify(cap)
            # 参数不安全的端点：即便 GET 也降为 agent_visible=False（无法安全直接调用）
            if not auto_safe:
                cap.agent_visible = False
            caps.append(cap)
    return caps


def agent_visible_reads() -> list[Capability]:
    """返回应自动暴露为 MCP tool 的能力（GET 且 agent_visible 且具备可调用参数签名）。"""
    return [c for c in build_capabilities()
            if c.agent_visible and c.method == "GET" and c.tool_name]


def tool_name_for(cap: Capability) -> str:
    """解析自动暴露用的 tool 名：market 已覆盖端点回退到手工 tool 名（碰撞即跳过）。"""
    return _MARKET_NAME_OVERRIDES.get(cap.path, cap.tool_name)

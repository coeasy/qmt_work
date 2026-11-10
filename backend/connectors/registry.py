"""ConnectorRegistry：``<broker>.<mode>[.<transport>]`` → 已装配的连接器（V4 §3.1）。

组合表一眼看全：

| key                      | Dialect      | Transport  | 说明                            |
|--------------------------|--------------|------------|---------------------------------|
| ``qmt.mini``             | xtquant.v1   | inprocess  | 现状主通道（含 ABI 子进程桥形态）      |
| ``qmt.big.direct``       | xtquant.v1   | inprocess  | 路径 A：xtquant 直连大 QMT       |
| ``qmt.big.bridge.file``  | bigqmt.v1    | file       | 路径 B 默认：零部署             |
| ``qmt.big.bridge.redis`` | bigqmt.v1    | redis      | 低延迟                          |
| ``qmt.big.bridge.zmq``   | bigqmt.v1    | zmq        | 同机极低延迟                    |
| ``qmt.big.closed``       | bigqmt.v1    | embedded   | **闭环：盘前计划 + 日志回传**（2026-10-23 后唯一可用） |
| ``ptrade.http``          | ptrade.v1    | http       | 未来客户端（接入模板）          |

★ 新增一个客户端 = 在 ``_PROFILES`` 加一行（外加一个 dialect 文件）。
  组合表里**没有一个格子是手写类** —— 这就是把 Dialect/Transport 拆开的意义。

失败语义：未知 key / 缺失必填参数一律抛错（零 mock：不给默认胡编一个连接器）。
"""
from __future__ import annotations

from typing import Any

from .dialects import get_dialect
from .generic import GenericConnector
from .transports import get_transport

#: 默认能力声明（未经实调验证的项在 probe 时会降级为 UNKNOWN）
_DEFAULT_CAPS = {
    "xtquant.v1": ("quote", "kline", "trade", "account", "positions", "realtime"),
    "bigqmt.v1": ("quote", "kline", "trade", "account", "positions"),
}

_PROFILES: dict[str, dict[str, Any]] = {
    "qmt.mini": {"dialect": "xtquant.v1", "transport": "inprocess"},
    "qmt.big.direct": {"dialect": "xtquant.v1", "transport": "inprocess"},
    "qmt.big.bridge.file": {"dialect": "bigqmt.v1", "transport": "file"},
    "qmt.big.bridge.redis": {"dialect": "bigqmt.v1", "transport": "redis"},
    "qmt.big.bridge.zmq": {"dialect": "bigqmt.v1", "transport": "zmq"},
    "qmt.big.closed": {"dialect": "bigqmt.v1", "transport": "embedded"},
    "ptrade.http": {"dialect": "ptrade.v1", "transport": "http"},
}

#: 展示元数据（REST /brokers/connectors 用）。
#: 与 ``_PROFILES`` 分开维护的原因：``describe()`` 是装配契约，测试断言其
#: 精确形状；把 UI 文案混进去会让「加个中文名」变成破坏性变更。
_DISPLAY: dict[str, dict[str, Any]] = {
    "qmt.mini": {"label": "MiniQMT（进程内）", "recommended": False},
    "qmt.big.direct": {"label": "大 QMT 直连（xtquant）", "recommended": False},
    "qmt.big.bridge.file": {"label": "大 QMT 文件桥", "recommended": False},
    "qmt.big.bridge.redis": {"label": "大 QMT Redis 桥", "recommended": False},
    "qmt.big.bridge.zmq": {"label": "大 QMT ZMQ 桥", "recommended": False},
    "qmt.big.closed": {"label": "大 QMT 闭环（嵌入式）", "recommended": True},
    "ptrade.http": {"label": "PTrade HTTP", "recommended": True},
}

#: 2026-10-23 起券商收紧后的失效清单（**保留 profile 不删**：不同券商生效时间不同，
#: 且回滚成本必须接近零）。这里只提供「为什么不建议用」的一句话，不改 ``resolve``
#: 的行为 —— 让调用方自己决定是否接受，而不是替它抛错把老部署打死。
RETIRED_PROFILES: dict[str, str] = {
    "qmt.mini": "miniQMT/xtquant 外接已被关停（2026-10-23 起）",
    "qmt.big.direct": "xtquant 直连受授权串限制且 miniQMT 已关停",
    "qmt.big.bridge.file": "策略进程禁文件读写（PermissionError: Forbidden FileIO）",
    "qmt.big.bridge.redis": "策略进程禁外网，redis 连接不可达",
    "qmt.big.bridge.zmq": "策略进程禁外网（含 127.0.0.1），zmq 连接触发停模",
}

_TRANSPORT_REQUIRED = {
    "inprocess": ("adapter",),
    "http": ("base_url",),
    "file": ("bridge_dir",),
    "redis": ("redis_url",),
    "zmq": ("zmq_addr",),
    "embedded": ("bundle_id",),
}


def retirement_notice(key: str) -> str:
    """返回该 profile 的失效说明；可用 profile 返回空串。

    ★ 刻意不在 ``resolve`` 里直接抛错：本地模拟盘/历史回放仍需要这些 transport
    来复现旧行为，硬拦会把「能跑的老测试」一起打死。是否拒绝由调用方按场景决定。
    """
    return RETIRED_PROFILES.get(key, "")


def is_retired(key: str) -> bool:
    return key in RETIRED_PROFILES


def available_keys() -> tuple[str, ...]:
    return tuple(sorted(_PROFILES))


def describe(key: str) -> dict[str, Any]:
    try:
        return dict(_PROFILES[key])
    except KeyError as exc:
        raise KeyError(
            f"未知连接器 key: {key}（可用: {', '.join(available_keys())}）") from exc


def display_meta(key: str) -> dict[str, Any]:
    """返回展示用元数据（label / recommended）；未知 key 回退到 key 本身。"""
    meta = _DISPLAY.get(key) or {}
    return {"label": meta.get("label") or key,
            "recommended": bool(meta.get("recommended"))}

def resolve(key: str, **options: Any) -> GenericConnector:
    """装配一个连接器。

    :param key: 形如 ``qmt.big.bridge.file``
    :param options: 由 transport 决定必填项 —— ``adapter`` / ``bridge_dir``
                    / ``redis_url`` / ``zmq_addr``；另有 ``auth_token``、
                    ``poll_interval_ms``、``agent_timeout`` 等可选项。
    """
    profile = describe(key)
    dialect = get_dialect(profile["dialect"])
    transport_id = profile["transport"]

    missing = [p for p in _TRANSPORT_REQUIRED.get(transport_id, ())
               if options.get(p) in (None, "")]
    if missing:
        raise ValueError(
            f"连接器 {key} 缺少必需参数: {', '.join(missing)}"
            f"（transport={transport_id}）")

    transport = _build_transport(transport_id, dialect, options)
    return GenericConnector(
        dialect=dialect,
        transport=transport,
        connector_id=key,
        name=f"{key}",
        capabilities=_DEFAULT_CAPS.get(profile["dialect"], ()),
    )


def _build_transport(transport_id: str, dialect: Any, options: dict[str, Any]):
    common: dict[str, Any] = {}
    if "auth_token" in options:
        common["auth_token"] = options["auth_token"]
    if "agent_timeout" in options:
        common["agent_timeout"] = options["agent_timeout"]

    if transport_id == "inprocess":
        # 写指令名单由 dialect 提供（若有），否则走 transport 默认。
        write_ops = getattr(dialect, "write_ops", None)
        return get_transport(transport_id, adapter=options["adapter"],
                             write_ops=frozenset(write_ops) if write_ops else None,
                             **common)
    if transport_id == "http":
        kwargs = {"base_url": options["base_url"]}
        if "api_key" in options:
            kwargs["api_key"] = options["api_key"]
        if "timeout" in options:
            kwargs["timeout"] = options["timeout"]
        return get_transport(transport_id, **kwargs)
    if transport_id == "file":
        kwargs = dict(bridge_dir=options["bridge_dir"])
        if "poll_interval" in options:
            kwargs["poll_interval"] = options["poll_interval"]
        return get_transport(transport_id, **kwargs, **common)
    if transport_id == "redis":
        return get_transport(transport_id, url=options["redis_url"], **common)
    if transport_id == "zmq":
        return get_transport(transport_id, addr=options["zmq_addr"], **common)
    if transport_id == "embedded":
        kwargs = {"bundle_id": options["bundle_id"]}
        if options.get("plan_ops"):
            kwargs["plan_ops"] = frozenset(options["plan_ops"])
        return get_transport(transport_id, **kwargs, **common)
    raise ValueError(f"未支持的传输: {transport_id}")


__all__ = ["available_keys", "describe", "is_retired", "resolve",
           "retirement_notice", "RETIRED_PROFILES"]

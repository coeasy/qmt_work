"""ConnectorRegistry：``<broker>.<mode>[.<transport>]`` → 已装配的连接器（V4 §3.1）。

组合表一眼看全：

| key                      | Dialect      | Transport  | 说明                            |
|--------------------------|--------------|------------|---------------------------------|
| ``qmt.mini``             | xtquant.v1   | inprocess  | 现状主通道（含 ABI 子进程桥形态）      |
| ``qmt.big.direct``       | xtquant.v1   | inprocess  | 路径 A：xtquant 直连大 QMT       |
| ``qmt.big.bridge.file``  | bigqmt.v1    | file       | 路径 B 默认：零部署             |
| ``qmt.big.bridge.redis`` | bigqmt.v1    | redis      | 低延迟                          |
| ``qmt.big.bridge.zmq``   | bigqmt.v1    | zmq        | 同机极低延迟                    |
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
    "ptrade.http": {"dialect": "ptrade.v1", "transport": "http"},
}

_TRANSPORT_REQUIRED = {
    "inprocess": ("adapter",),
    "http": ("base_url",),
    "file": ("bridge_dir",),
    "redis": ("redis_url",),
    "zmq": ("zmq_addr",),
}


def available_keys() -> tuple[str, ...]:
    return tuple(sorted(_PROFILES))


def describe(key: str) -> dict[str, Any]:
    try:
        return dict(_PROFILES[key])
    except KeyError as exc:
        raise KeyError(
            f"未知连接器 key: {key}（可用: {', '.join(available_keys())}）") from exc


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
    raise ValueError(f"未支持的传输: {transport_id}")


__all__ = ["available_keys", "describe", "resolve"]

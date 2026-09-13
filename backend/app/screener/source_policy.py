"""选股数据源 Source Policy 与能力链求值（D-J §J.1–J.2 / D9 v1.3 锁定）。

设计原则（不可违反）：
- J-1 选股引擎不得绑定任何 Provider：引擎只依赖能力端口，来源由 Source Policy 决定；
- J-2 能力优先，名称兜底：路由只按「已声明且已实现」的能力选源；
- J-3 显式降级，禁止静默换源：不支持该能力要么报错要么带 degraded 返回；
- J-4 同源一致性：同一次扫描内同一能力必须来自同一 Provider；
- J-5 许可证合规（链求值期过滤）：commercial_mode=true 时链自动跳过 commercial_ok=false 的源；
- J-6 默认顺序即契约：QMT → eltdx → baostock → akshare（由 tests 钉死）。

本模块不依赖任何具体 Provider 实现，只消费 ``datasource.providers`` 的目录与链路求解。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class SourcePolicy(str, Enum):
    AUTO = "auto"                 # 默认：QMT 优先，否则按链降级 eltdx → baostock → akshare
    PREFER_QMT = "prefer_qmt"    # 与 auto 同链，但明确标注「QMT 优先」
    QMT_ONLY = "qmt_only"        # 钉死 QMT；未连接/能力缺失 → 明确 503，不降级
    LOCAL_ONLY = "local_only"    # 只用本地 canonical 层，完全离线可复现
    EXPLICIT = "explicit"        # explicit:<id>：钉死某 provider，不支持即报错不降级


@dataclass(frozen=True)
class ResolvedPolicy:
    policy: SourcePolicy
    explicit_provider: Optional[str] = None   # explicit:<id> 时的目标源
    # 链路求值后的实际源序列（已过滤：注册态 / 依赖 / 许可证 / 能力）
    chain: tuple[str, ...] = ()
    degraded: bool = False
    qmt_unavailable_reason: Optional[str] = None  # prefer_qmt 下说明为何没走 QMT


def parse_source_policy(raw: Optional[str]) -> SourcePolicy:
    """解析请求级 source_policy；非法值回退 auto（不报错，避免击穿选股）。"""
    if not raw:
        return SourcePolicy.AUTO
    s = str(raw).strip().lower()
    if s.startswith("explicit:") or s.startswith("explicit="):
        return SourcePolicy.EXPLICIT
    for p in SourcePolicy:
        if p.value == s:
            return p
    return SourcePolicy.AUTO


def explicit_provider_of(raw: Optional[str]) -> Optional[str]:
    """从 ``explicit:akshare`` 中提取目标源 id（无则返回 None）。"""
    if not raw:
        return None
    s = str(raw).strip()
    if s.startswith("explicit:"):
        return s[len("explicit:"):].strip() or None
    if s.startswith("explicit="):
        return s[len("explicit="):].strip() or None
    return None


def resolve_policy(raw: Optional[str], capability: str,
                   *, commercial_mode: bool = False,
                   registered: Optional[set[str]] = None,
                   qmt_connected: bool = False) -> ResolvedPolicy:
    """按能力求值 Source Policy，产出实际候选源链（D-J §J.2 求值顺序 ①→⑦）。

    - auto / prefer_qmt：取该能力默认链并按链求值；
    - qmt_only：仅 broker（未连接则空链 → 调用方 503）；
    - local_only：空链（由 canonical 层处理）；
    - explicit:<id>：链缩为单元素且不降级。
    """
    policy = parse_source_policy(raw)
    explicit = explicit_provider_of(raw) if policy == SourcePolicy.EXPLICIT else None

    if policy == SourcePolicy.QMT_ONLY:
        chain = ("broker",) if qmt_connected else ()
        return ResolvedPolicy(policy, chain=chain,
                              qmt_unavailable_reason=None if qmt_connected else "no_broker_connected")
    if policy == SourcePolicy.LOCAL_ONLY:
        return ResolvedPolicy(policy, chain=())

    from datasource.providers import provider_catalog
    if policy == SourcePolicy.EXPLICIT and explicit:
        # 显式源：直接成链，不降级；若该源不满足能力/许可证，由调用方负责报错。
        return ResolvedPolicy(policy, explicit_provider=explicit, chain=(explicit,))

    # auto / prefer_qmt → 能力链求值
    chain = list(provider_catalog.resolve_chain(
        capability, commercial_mode=commercial_mode, registered=registered))
    degraded = False
    qmt_reason = None
    if not qmt_connected:
        # QMT 不可用：从有效链中剔除 broker（它实际不可用），实际从下一顺位开始降级。
        if "broker" in chain:
            chain = [c for c in chain if c != "broker"]
            degraded = True
            qmt_reason = "no_broker_connected"
    return ResolvedPolicy(policy, chain=tuple(chain), degraded=degraded,
                          qmt_unavailable_reason=qmt_reason)


__all__ = ["SourcePolicy", "ResolvedPolicy", "parse_source_policy",
           "explicit_provider_of", "resolve_policy"]

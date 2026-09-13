"""Phase 4：统一 K 线取数层（本地 canonical + 在线 CapabilityChain）。

把「选股引擎读什么 K 线」完全收敛到本模块：调用方只传
``(codes, period, adjust, policy_str)``，既可读本地 canonical 仓，也可按能力链在线取数；
**引擎代码内不得出现任何 provider 名字**（D-J §J.1）。

铁律（D-J §J.2–J.4）：
- J-2 能力优先，名称兜底：仅按「已声明且已实现」的能力选源。
- J-3 显式降级，禁止静默换源：不支持该能力要么报错要么带 ``degraded`` 返回。
- J-4 同源一致性：一次扫描整批优先使用链上第一个可用源；该源整批无数据才整体降级
  到下一源；所有在线源失败再回退本地 canonical（``degraded=True``，绝不伪造）。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.screener.source_policy import (
    SourcePolicy,
    parse_source_policy,
    resolve_policy,
)

log = logging.getLogger("qmt_work.data.bars_provider")

_BAR_COUNT_DEFAULT = 250
_CHAIN_VERSION = "chain.v1"


@dataclass
class BarsBatchReport:
    """一次批量取数的溯源 / 降级报告（§J.10 provenance 契约来源）。"""

    provider_used: Optional[str] = None
    source_policy: str = "auto"
    degraded: bool = False
    degraded_reason: Optional[str] = None
    fallback_tried: List[str] = field(default_factory=list)
    as_of: Optional[str] = None
    local_fallback: bool = False
    count_online: int = 0
    count_local: int = 0
    count_total: int = 0

    def to_provenance(self) -> dict:
        return {
            "provider_used": self.provider_used,
            "source_policy": self.source_policy,
            "degraded": self.degraded,
            "degraded_reason": self.degraded_reason,
            "fallback_tried": list(self.fallback_tried),
            "as_of": self.as_of,
            "local_fallback": self.local_fallback,
            "provider_policy_version": _CHAIN_VERSION,
            "count_online": self.count_online,
            "count_local": self.count_local,
            "count_total": self.count_total,
        }


class BarsProvider:
    """统一 K 线批量取数（本地 canonical + 在线能力链）。

    设计为无状态、可注入 hub/store（测试友好）；不传则惰性取全局单例。
    """

    def __init__(self, hub=None, store=None):
        self._hub = hub
        self._store = store

    # ------------------------------------------------------------------
    # 运行态探针（惰性，避免模块加载期重依赖）
    # ------------------------------------------------------------------
    @staticmethod
    def _commercial_mode() -> bool:
        try:
            from datasource.registry import get_manager
            return get_manager()._commercial_mode
        except Exception:  # noqa: BLE001
            import os
            return os.environ.get("QMT_COMMERCIAL") == "1"

    @staticmethod
    def _qmt_connected() -> bool:
        try:
            from core.state import state
            mgr = getattr(state, "broker_manager", None)
            if mgr is None:
                return False
            return any(getattr(c, "connected", False) for c in mgr.all_connections())
        except Exception:  # noqa: BLE001
            return False

    def _get_hub(self):
        if self._hub is not None:
            return self._hub
        from datasource.registry import get_manager
        return get_manager()

    def _get_store(self):
        if self._store is not None:
            return self._store
        from datasource.local_store import get_store
        return get_store()

    def _local_batch(self, codes: List[str], period: str, adjust: str,
                     count: int) -> Dict[str, list]:
        if not codes:
            return {}
        st = self._get_store()
        try:
            # Phase B：单条（分块）SQL 批量取数，替代逐只循环
            return st.get_bars_batch(codes, period=period, adjust=adjust, limit=count)
        except Exception as exc:  # noqa: BLE001
            log.debug("本地仓批量取数失败: %s", exc)
            return {c: [] for c in codes}

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------
    async def get_bars_batch(
        self,
        codes: List[str],
        *,
        period: str = "1d",
        adjust: str = "qfq",
        policy_str: str = "auto",
        offline: bool = False,
        bar_count: int = _BAR_COUNT_DEFAULT,
    ) -> tuple[Dict[str, list], BarsBatchReport]:
        """一次性取回多标的 K 线（本地 canonical 或在线能力链）。

        返回 ``(bars_map, report)``，``bars_map`` 为 ``{code: [Bar,...]}``（无数据为空列表）。
        """
        report = BarsBatchReport(source_policy=policy_str or "auto")
        codes = [str(c) for c in codes]
        report.count_total = len(codes)

        # —— LOCAL_ONLY / offline：纯本地，绝不触网 ——
        if offline or parse_source_policy(policy_str) == SourcePolicy.LOCAL_ONLY:
            batch = self._local_batch(codes, period, adjust, bar_count)
            n = sum(1 for v in batch.values() if v)
            report.provider_used = "local"
            report.count_local = n
            report.as_of = self._store_as_of()
            if n == 0 and not offline:
                report.degraded = True
                report.degraded_reason = "local_empty"
            return batch, report

        # —— 解析能力链 ——
        cap = "kline_qfq" if adjust in ("qfq", "hfq") else "kline"
        try:
            from datasource.registry import get_manager
            registered = set(get_manager().list_sources())
        except Exception:  # noqa: BLE001
            registered = None
        resolved = resolve_policy(
            policy_str, cap,
            commercial_mode=self._commercial_mode(),
            registered=registered,
            qmt_connected=self._qmt_connected(),
        )
        chain = list(resolved.chain)
        report.degraded = resolved.degraded
        if resolved.qmt_unavailable_reason:
            report.fallback_tried.append(f"qmt:{resolved.qmt_unavailable_reason}")

        # —— 无在线源（如 qmt_only 但未连接）→ 全回退本地 ——
        if not chain:
            report.degraded = True
            report.degraded_reason = resolved.qmt_unavailable_reason or "no_online_source"
            return self._fallback_local(codes, period, adjust, bar_count, report), report

        # —— 按链逐源尝试（J-4 同源一致性：整批用同一源）——
        hub = self._get_hub()
        for src in chain:
            try:
                batch = await self._fetch_online(hub, codes, src, period, adjust, bar_count)
            except Exception as exc:  # noqa: BLE001
                log.warning("在线源 %s 批量取数异常: %s", src, exc)
                report.fallback_tried.append(f"{src}:error")
                continue
            n_online = sum(1 for v in batch.values() if v)
            if n_online > 0:
                report.provider_used = src
                report.count_online = n_online
                # 缺失标的用本地补（不破坏同源：仍记 fallback）
                missing = [c for c, v in batch.items() if not v]
                if missing:
                    local = self._local_batch(missing, period, adjust, bar_count)
                    filled = 0
                    for c in missing:
                        if local.get(c):
                            batch[c] = local[c]
                            filled += 1
                    if filled:
                        report.local_fallback = True
                        report.count_local = filled
                        report.fallback_tried.append(
                            f"{src}:partial_local_fallback({filled})")
                report.as_of = self._online_as_of(src)
                return batch, report
            report.fallback_tried.append(f"{src}:empty")

        # —— 全在线失败 → 本地（degraded）——
        report.degraded = True
        report.degraded_reason = report.degraded_reason or "all_online_unavailable"
        return self._fallback_local(codes, period, adjust, bar_count, report), report

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    async def _fetch_online(self, hub, codes, src, period, adjust, count) -> Dict[str, list]:
        # Phase B-3：在线源并发批量（信号量限流 4），避免逐只串行阻塞；
        # 源原生批量（QMT get_market_data_ex / eltdx 并发）为后续优化项。
        sem = asyncio.Semaphore(4)

        async def _one(c: str):
            async with sem:
                try:
                    bars, _ = await hub.get_kline(c, period, count,
                                                 source=src, adjust=adjust)
                except Exception as exc:  # noqa: BLE001
                    log.debug("get_kline(%s, %s) 失败: %s", c, src, exc)
                    return c, []
                return c, list(bars) if bars else []

        results = await asyncio.gather(*[_one(c) for c in codes])
        return {c: bl for c, bl in results}

    def _fallback_local(self, codes, period, adjust, count, report) -> Dict[str, list]:
        batch = self._local_batch(codes, period, adjust, count)
        report.provider_used = "local"
        report.local_fallback = True
        report.count_local = sum(1 for v in batch.values() if v)
        report.as_of = self._store_as_of()
        return batch

    @staticmethod
    def _store_as_of() -> Optional[str]:
        try:
            from datasource.local_store import get_store
            st = get_store()
            return st.latest_bar_dt()
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _online_as_of(src: str) -> Optional[str]:
        # 在线源为实时数据，不提供统一 as_of（由下游标「截至最新」）。
        return None


__all__ = ["BarsProvider", "BarsBatchReport"]

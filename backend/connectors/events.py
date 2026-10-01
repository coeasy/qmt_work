"""事件合成：把「轮询差分」伪装成和「原生推送」完全同形的事件流（V4 §4.4）。

为什么必须存在
--------------
大 QMT 的普通账户**没有稳定的委托/成交推送**（部分券商仅两融有回调），
而 miniQMT 有 ``XtQuantTraderCallback``。若让上层各自处理「这个客户端有没有回调」，
每条消费路径都要写两遍分支 —— 这正是 CCXT/easytrader 式抽象最容易崩的地方（E6）。

本模块让两类来源**对上层同形**，差异只体现在 ``EventSemanticsSpec``：

* ``PUSH``      —— 无条件信任，延迟 ≈0；
* ``POLL_DIFF`` —— 带 ``max_latency_ms`` 上界，超时守护必须留出它。

★ **延迟上界不是文档修辞**：`order_watchdog` 若不把 POLL_DIFF 的上界算进容忍窗口，
会把「还没被轮询到的成交」误判为「未成交」而触发撤单 —— 那是真实的资金损失。

首轮打底纪律
------------
第一次轮询拿到的全量快照只用来**建立基线**，绝不补发成事件：程序启动时把昨天
（或更早）的成交通知给策略层，会造成重复撮合/重复记账。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Iterable, Sequence

from xtquant_client.order_status import REJECTED, normalize_order_status

from .ports import CanonicalEvent, EventSemantics, EventSemanticsSpec

log = logging.getLogger("qmt_work.connectors.events")


def _order_fp(row: Any) -> tuple[str, str]:
    """委托指纹：委托号 + 关联对象 + 序号。变了才发事件。"""
    return (
        str(row.get("order_id") or row.get("order_sysid") or ""),
        str(row.get("seq") or ""),
    )


def _fingerprint(row: Any) -> str:
    """一条记录的完整指纹（状态 + 成交量 + 价格），用于判定「有没有变化」。

    刻意**不包含**时间戳类字段：那些每次查询都会变，会让 diff 永远有增量。
    """
    parts = [
        str(row.get("order_id") or row.get("order_sysid") or row.get("trade_id") or ""),
        str(row.get("status") or row.get("order_status") or ""),
        str(row.get("dealt") or row.get("traded_volume") or row.get("volume") or ""),
        str(row.get("traded_price") or row.get("price") or ""),
    ]
    return "|".join(parts)


class PollDiffEventSource:
    """轮询差分 → canonical 事件流。

    :param fetch_orders: ``() -> list[dict]``（方言形态即可，本类只做结构性 diff）
    :param fetch_deals:  ``() -> list[dict]``
    :param max_latency_ms: **必须**声明的延迟上界（= 轮询间隔 + 单次 IO 上界）
    """

    def __init__(self, fetch_orders, fetch_deals, *, poll_interval_ms: int = 1000,
                 transport_id: str = "poll"):
        self._fetch_orders = fetch_orders
        self._fetch_deals = fetch_deals
        self._poll_interval_ms = max(50, int(poll_interval_ms))
        self._transport_id = transport_id
        self._seq = 0
        self._primed = False
        self._order_seen: dict[tuple[str, str], str] = {}
        self._trade_seen: dict[str, str] = {}

    # ------------------------------------------------------------------
    def event_semantics(self) -> EventSemanticsSpec:
        return EventSemanticsSpec(
            semantics=EventSemantics.POLL_DIFF,
            # 上界 = 轮询间隔 + 单次查询往返预算（保守给 2 倍）
            max_latency_ms=int(self._poll_interval_ms * 2),
            poll_interval_ms=self._poll_interval_ms,
        )

    # ------------------------------------------------------------------
    def poll_once(self) -> list[CanonicalEvent]:
        """做一次差分。可在线程池里调用（纯 CPU/同步 IO-free）。"""
        events: list[CanonicalEvent] = []
        events.extend(self._diff(self._safe_orders(), self._order_seen, "order"))
        events.extend(self._diff(self._safe_deals(), self._trade_seen, "trade"))
        if not self._primed:
            # 首轮打底：只建立基线，不补发历史。
            self._primed = True
            return []
        return events

    async def stream_events(self) -> list[CanonicalEvent]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.poll_once)

    # ------------------------------------------------------------------
    def _safe_orders(self) -> Sequence[dict]:
        try:
            return list(self._fetch_orders() or [])
        except Exception as exc:  # noqa: BLE001
            # 单次查询失败不能让事件流死掉 —— 记录后返回空，下一轮再来。
            log.warning("[events] 查询委托失败: %s", exc)
            return []

    def _safe_deals(self) -> Sequence[dict]:
        try:
            return list(self._fetch_deals() or [])
        except Exception as exc:  # noqa: BLE001
            log.warning("[events] 查询成交失败: %s", exc)
            return []

    def _diff(self, rows: Iterable[dict], seen: dict, kind: str) -> list[CanonicalEvent]:
        out: list[CanonicalEvent] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            key_val = str(row.get("order_id") or row.get("order_sysid")
                          or row.get("trade_id") or "")
            if not key_val:
                continue
            key = _order_fp(row) if kind == "order" else key_val
            fp = _fingerprint(row)
            if key in seen and seen[key] == fp:
                continue
            seen[key] = fp
            if not self._primed:
                continue  # 基线建立期不发事件
            self._seq += 1
            data = dict(row)
            # ★ 废单/拒单判定**必须过 SSOT**，绝不能在这里硬编码整数。
            #   血的教训：56 在 xtquant 是「全部成交」，在大 QMT 文档里被写成「已拒」；
            #   若在这里写死数字，会把一批真实成交判成拒单（反之亦然）。
            #   交给 order_status.normalize_order_status 唯一裁决（INV-3）。
            status = normalize_order_status(
                row.get("status") or row.get("order_status") or "")
            if status == REJECTED:
                out.append(CanonicalEvent(
                    kind="order_error",
                    occurred_at=str(row.get("time") or row.get("order_time") or ""),
                    data={**data, "error_id": key_val,
                          "error_msg": row.get("status_msg") or "柜台废单/拒单",
                          "status": status},
                    seq=self._seq,
                    synthetic=True,
                ))
                continue
            out.append(CanonicalEvent(
                kind=kind,
                occurred_at=str(row.get("time") or row.get("order_time")
                                or row.get("traded_time") or ""),
                data=data,
                seq=self._seq,
                synthetic=True,
            ))
        return out


class PushEventSource:
    """原生推送来源（miniQMT 回调）：原样包成 canonical 事件，语义为 PUSH。"""

    def __init__(self, buffer: list[CanonicalEvent] | None = None):
        self._buffer: list[CanonicalEvent] = buffer if buffer is not None else []
        self._clock = time.perf_counter

    def push(self, kind: str, data: dict[str, Any]) -> CanonicalEvent:
        ev = CanonicalEvent(kind=kind, data=dict(data or {}),
                            seq=len(self._buffer) + 1, synthetic=False)
        self._buffer.append(ev)
        return ev

    def event_semantics(self) -> EventSemanticsSpec:
        return EventSemanticsSpec(semantics=EventSemantics.PUSH)

    def stream_events(self) -> list[CanonicalEvent]:
        out = list(self._buffer)
        self._buffer.clear()
        return out

    async def drain(self) -> list[CanonicalEvent]:
        return self.stream_events()


def canonical_from_agent_event(type_str: str, data: dict[str, Any],
                                seq: int = 0) -> CanonicalEvent | None:
    """把大 QMT agent 已差分过的行（events.ndjson）翻译成 canonical 事件。

    agent 已经做过「首轮建基线、只发变化」的差分（见 ``qmt_api.Executor.diff_events``），
    所以 qmt_work 侧**只翻译、不再二次 diff**，避免重复计数。

    ★ 废单/拒单判定**必须过 SSOT**（``normalize_order_status``）：56 在 xtquant 是
    已成、在大 QMT 文档里被写成已拒，硬编码整数会错判（详见 ``events.PollDiffEventSource``）。
    """
    if not isinstance(data, dict):
        return None
    kind = str(type_str or "").lower()
    if kind in ("order", "order_error"):
        status = normalize_order_status(
            data.get("order_status") or data.get("status") or "")
        if status == REJECTED:
            return CanonicalEvent(
                kind="order_error", occurred_at=str(data.get("time") or ""),
                data={**data, "error_id": str(data.get("order_id") or ""),
                      "error_msg": data.get("status_msg") or "柜台废单/拒单",
                      "status": status},
                seq=seq, synthetic=True)
        return CanonicalEvent(
            kind="order", occurred_at=str(data.get("time") or ""),
            data=dict(data), seq=seq, synthetic=True)
    if kind == "trade":
        return CanonicalEvent(
            kind="trade", occurred_at=str(data.get("time") or ""),
            data=dict(data), seq=seq, synthetic=True)
    if kind == "quote":
        # 订阅行情（SUB_QUOTE 轮询转发）：保持 type=quote 与 bridge_server 推送帧同形。
        return CanonicalEvent(
            kind="quote", occurred_at=str(data.get("time") or ""),
            data=dict(data), seq=seq, synthetic=True)
    # 未知类型：保留为通用事件，不丢。
    return CanonicalEvent(kind=kind or "unknown", data=dict(data),
                          seq=seq, synthetic=True)


__all__ = ["PollDiffEventSource", "PushEventSource", "canonical_from_agent_event"]

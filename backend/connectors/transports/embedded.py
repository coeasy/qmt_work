"""闭环传输（``embedded``）：只登记「意图」，不假装能实时下单。

为什么需要它
------------
2026-10-23 起策略进程禁文件读写、禁网络（含回环）。``file`` / ``redis`` / ``zmq`` /
``http`` / ``inprocess`` 五种 transport 全部失效。剩下的合法形态是：

    后端盘前产计划  →  烘焙进策略源码  →  策略在沙箱内自判自执行  →  日志回传

所以本 transport **不是**「另一种把请求送进去的办法」，而是把语义诚实化：

* 它接受 **计划类** op（默认 ``SUBMIT_PLAN`` / ``PLAN`` / ``PING``），登记为
  ``PENDING`` 意图并返回受理结果；
* 其余 op（下单/撤单/查询）一律返回 ``ok=False`` + ``error_type=ClosedLoopRealtimeUnsupported``
  —— **绝不允许**返回 ``ok=True`` 让上层以为单子已经发出去了（那正是零 mock 要防的假绿灯）。

意图的终态由日志回传补齐（``LogTail`` 解析到 ``RESULT`` 帧后调 ``resolve``）。
因此在闭环模式下，正确的心智模型是「**提交计划 → 等日志**」，不是「调用 → 返回」。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from ..transport import MAX_TIMEOUT, WireRequest, WireResponse
from .base import BaseTransport

#: 闭环下唯一被接受的 op（可由调用方覆盖，便于方言演进）。
DEFAULT_PLAN_OPS = frozenset({"SUBMIT_PLAN", "PLAN", "PING"})

#: 意图状态机。``PENDING`` 之外都是终态，``resolve`` 只允许迁移一次。
STATE_PENDING = "PENDING"
STATE_DONE = "DONE"
STATE_FAILED = "FAILED"
STATE_EXPIRED = "EXPIRED"


@dataclass
class Intent:
    """一次计划提交。``signal_id`` 同时是对账锚点（柜台 ``remark`` 里的 ``cid=``）。"""

    signal_id: str
    op: str
    params: dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0
    state: str = STATE_PENDING
    result: Any = None
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "signal_id": self.signal_id,
            "op": self.op,
            "state": self.state,
            "created_at": self.created_at,
            "error": self.error,
        }


class EmbeddedTransport(BaseTransport):
    """闭环传输：登记意图 + 由日志补终态。"""

    transport_id = "embedded"
    supports_callback = False

    #: ★ 上层可据此**提前**拒绝「实时下单」类交互，而不是等到超时。
    realtime_supported = False

    def __init__(self, bundle_id: str = "", *, plan_ops: frozenset[str] | None = None,
                 auth_token: str = "", agent_timeout: float = MAX_TIMEOUT,
                 clock=time.time) -> None:
        if not bundle_id:
            # 没有 bundle_id 就无从判断「部署的是哪一版计划」⇒ 响亮失败，不猜。
            raise ValueError("embedded transport 需要 bundle_id（部署的策略包标识）")
        self.bundle_id = bundle_id
        self.plan_ops = frozenset(plan_ops or DEFAULT_PLAN_OPS)
        self.auth_token = auth_token
        self.agent_timeout = min(float(agent_timeout), MAX_TIMEOUT)
        self._clock = clock
        self._intents: dict[str, Intent] = {}
        self._closed = False
        self.submitted = 0
        self.rejected = 0

    # ------------------------------------------------------------------
    async def _dispatch(self, request: WireRequest, started: float) -> WireResponse:
        if self._closed:
            return self._fail("闭环传输已关闭", "TransportClosed", request, started)
        if request.op not in self.plan_ops:
            self.rejected += 1
            return self._fail(
                "闭环模式不支持实时 %s：新规下策略进程禁文件读写与外网（含 127.0.0.1），"
                "指令无法在交易时段送达。请改为「盘前生成计划 → 部署策略 → 查看执行」。" % request.op,
                "ClosedLoopRealtimeUnsupported", request, started,
                realtime_supported=False)
        intent = Intent(signal_id=request.signal_id, op=request.op,
                        params=dict(request.params), created_at=self._clock())
        self._intents[intent.signal_id] = intent
        self.submitted += 1
        return self._ok({"plan_id": intent.signal_id, "state": STATE_PENDING},
                        request, started, pending=True, bundle_id=self.bundle_id)

    # ------------------------------------------------------------------
    # 终态补全（由日志回传驱动）
    # ------------------------------------------------------------------
    def resolve(self, signal_id: str, *, ok: bool, result: Any = None,
                error: str = "", error_type: str = "") -> bool:
        """把意图推进到终态。返回 ``False`` 表示「没有这个意图/已是终态」。

        ★ 幂等：同一 ``signal_id`` 重复回放（日志重读、策略重发）只生效一次，
        与 ``LogTail`` 的 ``(boot_id, seq)`` 去重互为双保险。
        """
        intent = self._intents.get(signal_id)
        if intent is None or intent.state != STATE_PENDING:
            return False
        intent.state = STATE_DONE if ok else STATE_FAILED
        intent.result = result
        intent.error = error or ("" if ok else error_type)
        return True

    def expire(self, ttl: float, *, now: float | None = None) -> list[str]:
        """把超过 ``ttl`` 仍未回终态的意图标 ``EXPIRED``，返回其 id。

        ★ 过期**不等于失败**：闭环下「没回日志」可能是策略没触发、也可能是日志被
        轮转掉了。调用方必须按「先查柜台再决定」处理（见 ``bigqmt_reconcile``），
        禁止拿 ``EXPIRED`` 直接重发。
        """
        moment = self._clock() if now is None else now
        expired: list[str] = []
        for sid, intent in self._intents.items():
            if intent.state == STATE_PENDING and moment - intent.created_at > ttl:
                intent.state = STATE_EXPIRED
                expired.append(sid)
        return expired

    def pending(self) -> tuple[str, ...]:
        return tuple(sid for sid, it in self._intents.items() if it.state == STATE_PENDING)

    def intent(self, signal_id: str) -> Intent | None:
        return self._intents.get(signal_id)

    def snapshot(self) -> list[dict[str, Any]]:
        return [it.as_dict() for it in self._intents.values()]

    # ------------------------------------------------------------------
    def is_alive(self) -> bool:
        """O(1) 探活：只看本地状态（HealthPort 契约，禁止 IO）。"""
        return not self._closed

    async def close(self) -> None:
        self._closed = True

    @property
    def stats(self) -> dict[str, int]:
        counts = {STATE_PENDING: 0, STATE_DONE: 0, STATE_FAILED: 0, STATE_EXPIRED: 0}
        for intent in self._intents.values():
            counts[intent.state] = counts.get(intent.state, 0) + 1
        return {"submitted": self.submitted, "rejected": self.rejected, **counts}


__all__ = ["DEFAULT_PLAN_OPS", "EmbeddedTransport", "Intent", "STATE_DONE",
           "STATE_EXPIRED", "STATE_FAILED", "STATE_PENDING"]

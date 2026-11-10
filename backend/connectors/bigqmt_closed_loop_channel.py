"""闭环通道胶水：把「日志回传」接到「意图终态」上。

三个模块各自独立（``embedded`` / ``bigqmt_log_tail`` / ``bigqmt_reconcile``），
但只有把它们连起来，闭环才真的转得动：

    后端登记意图 → 策略执行 → print("@@QMTW|…") → 日志落盘 → 本模块 poll()
        ├─ RESULT  → transport.resolve()   （意图从 PENDING 推进到终态）
        └─ ORDER/TRADE → CanonicalEvent    （喂给事件总线 / 前端）

为什么单独一层而不是塞进 transport：
transport 的契约是「一次 invoke 一次响应」，它不该知道日志文件在哪；
而日志跟随有游标、轮转、半行这些文件语义，塞进去会让 transport 变成四不像。

★ 幂等：同一条 RESULT 被重复读到（日志重读/进程重启）只会 ``resolve`` 一次 ——
  ``LogTail`` 按 ``(boot_id, seq)`` 去重，``resolve`` 自己也会拒绝二次迁移，双保险。
"""
from __future__ import annotations

from typing import Any, Callable

from .bigqmt_log_tail import AgentRecord, LogTail
from .ports import CanonicalEvent


class ClosedLoopChannel:
    """日志驱动的闭环通道。调用方按自己的节奏调 ``poll()``。"""

    def __init__(self, transport: Any, tail: LogTail | None = None,
                 log_path: str = "", *,
                 on_event: Callable[[CanonicalEvent], None] | None = None) -> None:
        self.transport = transport
        self.tail = tail or LogTail(log_path)
        self.log_path = log_path or getattr(self.tail, "path", "")
        self.on_event = on_event
        self.resolved = 0
        self.unmatched = 0

    # ------------------------------------------------------------------
    def poll(self) -> list[CanonicalEvent]:
        """读一段日志，推进意图终态，返回本轮产生的事件（可能为空）。"""
        records = self.tail.read(self.log_path) if self.log_path else []
        return self.consume(records)

    def consume(self, records: list[AgentRecord]) -> list[CanonicalEvent]:
        """处理一批已解析的记录（便于测试直接喂，不必造文件）。"""
        events: list[CanonicalEvent] = []
        for record in records:
            if str(record.kind).upper() == "RESULT":
                self._apply_result(record)
                continue
            event = record.to_canonical()
            if event is None:
                continue
            events.append(event)
            if self.on_event is not None:
                self.on_event(event)
        return events

    # ------------------------------------------------------------------
    def _apply_result(self, record: AgentRecord) -> bool:
        data = record.data or {}
        signal_id = str(data.get("signal_id") or data.get("cid") or "").strip()
        if not signal_id:
            # 没有锚点的回执无法归属 ⇒ 计数留痕，不猜是哪个意图。
            self.unmatched += 1
            return False
        ok = bool(data.get("ok", True))
        result = data.get("result")
        error = str(data.get("error") or data.get("msg") or "")
        changed = self.transport.resolve(signal_id, ok=ok, result=result, error=error)
        if changed:
            self.resolved += 1
        else:
            # 已终态 / 未知意图：重复回执是正常现象（日志重读），不算错误。
            self.unmatched += 0
        return changed

    # ------------------------------------------------------------------
    @property
    def stats(self) -> dict[str, int]:
        base = self.tail.stats.as_dict()
        base.update({"resolved": self.resolved, "unmatched_results": self.unmatched})
        return base


__all__ = ["ClosedLoopChannel"]

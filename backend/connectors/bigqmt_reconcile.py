"""闭环三方对账：**意图 × 日志事件 × 柜台委托**。

为什么必须三方而不是两方
------------------------
闭环模式下没有「请求-响应」，只有三条互相独立、都可能缺的证据链：

1. **意图**（``embedded`` transport 登记的 ``PENDING``）：只证明「后端想让它做」；
2. **日志**（``@@QMTW|`` 回传）：证明「策略说它做了」，但日志会轮转、会被截断；
3. **柜台**（客户端原生导出/查询）：证明「柜台真的收到了」，是唯一有资金后果的事实。

只对两方会得到两种致命误判：

* 只看意图+日志 ⇒ 日志说发了、柜台其实拒了，被当成成交；
* 只看意图+柜台 ⇒ 柜台有一笔同标的同量的单，未必是我们这笔（手工单/上一会话）。

因此每条意图都要三方各自定位，**缺哪一方就如实标缺哪一方**，不做「大概率一致」的推断。

★ 铁律：``INTENT_ONLY``（有意图、无日志、无柜台）是 ``UNKNOWN`` 而不是失败。
处理顺序永远是「先查柜台 → 查到认账 → 查不到才允许重发」，**绝不超时重发**。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

#: 委托备注里的对账锚点。``passorder`` 的 remark 位写 ``cid=<signal_id>``，
#: 这是唯一能跨进程、跨重启、跨日志轮转定位同一笔委托的字段。
CID_PREFIX = "cid="

_CID_RE = re.compile(re.escape(CID_PREFIX) + r"([A-Za-z0-9_.:\-]{1,64})")

#: 对账结论。前两个阻断自动部署，其余只记录。
STATE_MATCHED = "MATCHED"
STATE_INTENT_ONLY = "INTENT_ONLY"
STATE_LOG_ONLY = "LOG_ONLY"
STATE_COUNTER_ONLY = "COUNTER_ONLY"
STATE_VOLUME_MISMATCH = "VOLUME_MISMATCH"

#: 阻断态：出现任一即不允许自动部署新计划（差异未清零）。
BLOCKING_STATES = frozenset({STATE_INTENT_ONLY, STATE_VOLUME_MISMATCH})


@dataclass(frozen=True)
class ReconcileRow:
    signal_id: str
    state: str
    code: str = ""
    intent_volume: int = 0
    counter_volume: int = 0
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "signal_id": self.signal_id,
            "state": self.state,
            "code": self.code,
            "intent_volume": self.intent_volume,
            "counter_volume": self.counter_volume,
            "detail": self.detail,
        }


@dataclass
class ReconcileReport:
    rows: list[ReconcileRow] = field(default_factory=list)
    intents: int = 0
    log_records: int = 0
    counter_rows: int = 0

    @property
    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for row in self.rows:
            out[row.state] = out.get(row.state, 0) + 1
        return out

    @property
    def blocking(self) -> list[ReconcileRow]:
        return [r for r in self.rows if r.state in BLOCKING_STATES]

    @property
    def clean(self) -> bool:
        """无阻断差异。★ 只回答「能不能自动部署下一版计划」，不回答「交易对不对」。"""
        return not self.blocking

    @property
    def unknown_intents(self) -> list[str]:
        """``INTENT_ONLY`` 的 signal_id —— 必须「先查柜台再决定」的那批。"""
        return [r.signal_id for r in self.rows if r.state == STATE_INTENT_ONLY]

    def as_dict(self) -> dict[str, Any]:
        return {
            "clean": self.clean,
            "counts": self.counts,
            "intents": self.intents,
            "log_records": self.log_records,
            "counter_rows": self.counter_rows,
            "blocking": [r.signal_id for r in self.blocking],
            "unknown_intents": self.unknown_intents,
            "rows": [r.as_dict() for r in self.rows],
        }


def extract_cid(row: dict[str, Any]) -> str:
    """从一行柜台/日志数据里取对账锚点。

    优先级：显式 ``signal_id`` > ``cid`` > ``remark`` 里的 ``cid=``。
    取不到返回空串（调用方据此归入「无法对账」而不是硬配一笔）。
    """
    if not isinstance(row, dict):
        return ""
    for key in ("signal_id", "cid", "client_order_id"):
        value = row.get(key)
        if value:
            return str(value).strip()
    for key in ("remark", "order_remark", "m_strRemark"):
        text = row.get(key)
        if not text:
            continue
        found = _CID_RE.search(str(text))
        if found:
            return found.group(1)
    return ""


def _int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


class ReconcileService:
    """纯函数式对账：不读文件、不连柜台，只吃三份已归一的数据。"""

    def __init__(self, *, cid_prefix: str = CID_PREFIX) -> None:
        self.cid_prefix = cid_prefix

    def reconcile(self, intents: Iterable[dict[str, Any]],
                  log_records: Iterable[Any] = (),
                  counter_rows: Iterable[dict[str, Any]] = ()) -> ReconcileReport:
        intent_list = [dict(i) for i in intents if isinstance(i, dict)]
        counter_list = [dict(c) for c in counter_rows if isinstance(c, dict)]
        logs = list(log_records)

        log_index: dict[str, dict[str, Any]] = {}
        for rec in logs:
            data = getattr(rec, "data", None)
            if not isinstance(data, dict):
                data = rec if isinstance(rec, dict) else {}
            cid = extract_cid(data)
            if cid:
                log_index.setdefault(cid, data)

        counter_index: dict[str, dict[str, Any]] = {}
        for row in counter_list:
            cid = extract_cid(row)
            if cid:
                counter_index.setdefault(cid, row)

        rows: list[ReconcileRow] = []
        seen: set[str] = set()
        for intent in intent_list:
            sid = extract_cid(intent)
            if not sid:
                # 没有锚点的意图无法对账 ⇒ 显式列出，不猜。
                rows.append(ReconcileRow(signal_id="", state=STATE_INTENT_ONLY,
                                         code=str(intent.get("code") or ""),
                                         detail="意图缺少 signal_id/cid，无法对账"))
                continue
            seen.add(sid)
            code = str(intent.get("code") or "")
            volume = _int(intent.get("volume"))
            in_log = sid in log_index
            in_counter = sid in counter_index
            counter_volume = _int(counter_index.get(sid, {}).get("volume"))

            if in_counter and counter_volume and volume and counter_volume != volume:
                rows.append(ReconcileRow(
                    signal_id=sid, state=STATE_VOLUME_MISMATCH, code=code,
                    intent_volume=volume, counter_volume=counter_volume,
                    detail="柜台数量与意图不一致（部分成交/改单/他人操作）"))
                continue
            if in_log and in_counter:
                rows.append(ReconcileRow(sid, STATE_MATCHED, code, volume,
                                         counter_volume, "三方一致"))
            elif in_counter:
                rows.append(ReconcileRow(sid, STATE_MATCHED, code, volume,
                                         counter_volume, "柜台有单，日志缺失（按柜台认账）"))
            elif in_log:
                rows.append(ReconcileRow(sid, STATE_LOG_ONLY, code, volume, 0,
                                         "日志称已发，柜台无此单（可能被拒/未报）"))
            else:
                rows.append(ReconcileRow(sid, STATE_INTENT_ONLY, code, volume, 0,
                                         "无日志无柜台 ⇒ UNKNOWN，先查后发，禁止重发"))

        for cid, row in counter_index.items():
            if cid in seen:
                continue
            rows.append(ReconcileRow(cid, STATE_COUNTER_ONLY,
                                     str(row.get("code") or ""), 0,
                                     _int(row.get("volume")),
                                     "柜台有单但无对应意图（手工单/上一会话/他策略）"))

        rows.sort(key=lambda r: (r.state, r.signal_id))
        return ReconcileReport(rows=rows, intents=len(intent_list),
                               log_records=len(logs), counter_rows=len(counter_list))


__all__ = ["BLOCKING_STATES", "CID_PREFIX", "ReconcileReport", "ReconcileRow",
           "ReconcileService", "STATE_COUNTER_ONLY", "STATE_INTENT_ONLY",
           "STATE_LOG_ONLY", "STATE_MATCHED", "STATE_VOLUME_MISMATCH", "extract_cid"]

"""大 QMT 客户端日志的结构化回传通道（``@@QMTW|`` 行协议）。

背景
----
2026-10-23 起，券商对大 QMT 内置策略收紧三条：禁文件读写、禁网络（**含
``127.0.0.1`` 回环**，且触发「停模」而非抛异常）、关停 miniQMT。策略进程内唯一
还剩的表达能力是 ``print`` —— 于是「内部 → 外部」的合法通路变成：

    策略 print("@@QMTW|{json}")  →  客户端日志  →  本模块只读 tail

为什么不能照搬 ``events.ndjson`` 的字节游标
-------------------------------------------
* **轮转**：客户端日志按日切 ``XtClient_<date>.log``，跨日必须换文件；
* **清空/压缩**：文件变小即视为从头读，否则游标永远越界、静默读空；
* **``seq`` 回卷**：策略重启后 ``seq`` 从 1 重来 ⇒ 去重键必须是 ``(boot_id, seq)``；
  只拿 ``seq`` 当水位会吞掉新会话的低序号事件（通道变死而指标全绿）。
* **噪音**：日志里混着终端自身大量输出，只认前缀，其余计 ``ignored`` 不报错；
* **半行**：读取可能截断在行中间，必须回退游标而不是把半行当坏行丢掉。

失败语义：**解析失败绝不影响采集**。坏行计数后跳过（``bad``），不抛异常 ——
一条畸形日志不该让整个回传通道停摆。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

from .events import canonical_from_agent_event
from .ports import CanonicalEvent

#: 结构化行前缀。选它是因为 QMT 自身日志不会出现 ``@@`` 开头的行，且便于 grep。
PREFIX = "@@QMTW|"

#: 单行上限。超长直接丢弃并计数 —— 防一条畸形日志把内存打爆。
MAX_LINE = 8192

#: 去重窗口。策略一次会话的发单量远小于此；超窗后按 FIFO 淘汰最老的键。
MAX_DEDUPE = 4096

#: 参与 canonical 事件翻译的类型（其余类型只做记录，不翻译）。
_EVENT_KINDS = {"ORDER", "TRADE", "ORDER_ERROR", "QUOTE"}


@dataclass(frozen=True)
class AgentRecord:
    """一条 ``@@QMTW|`` 记录。字段全部来自策略侧 ``emit()``，不做二次推断。"""

    kind: str
    seq: int = 0
    boot_id: str = ""
    ts: float = 0.0
    data: dict[str, Any] = field(default_factory=dict)

    def to_canonical(self) -> CanonicalEvent | None:
        """翻译成统一事件；非事件类（RESULT/STATUS/PROBE）返回 ``None``。

        ★ 废单判定走 ``events.canonical_from_agent_event`` 的 SSOT（56 的语义在
        大小 QMT 相反），此处**不再**自己判断状态码。
        """
        kind = str(self.kind or "").upper()
        if kind not in _EVENT_KINDS:
            return None
        return canonical_from_agent_event(kind.lower(), self.data, self.seq)


@dataclass
class TailStats:
    """采集计数器。★ 全部是「可证伪」的原始计数，不做「健康度」之类的合成指标。"""

    parsed: int = 0
    ignored: int = 0
    bad: int = 0
    duplicates: int = 0
    oversized: int = 0
    boot_switches: int = 0
    rotations: int = 0
    bytes_read: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "parsed": self.parsed,
            "ignored": self.ignored,
            "bad": self.bad,
            "duplicates": self.duplicates,
            "oversized": self.oversized,
            "boot_switches": self.boot_switches,
            "rotations": self.rotations,
            "bytes_read": self.bytes_read,
        }


class LogTail:
    """增量解析器。

    刻意**不内置轮询循环**：调用方（后端作业/测试）决定节奏，本类只做
    「给我一段文本/一个文件，还我若干记录」。这样单测不需要 sleep。
    """

    def __init__(self, path: str = "", *, prefix: str = PREFIX,
                 encoding: str = "utf-8", errors: str = "replace",
                 max_dedupe: int = MAX_DEDUPE, max_line: int = MAX_LINE) -> None:
        self.path = path
        self.prefix = prefix
        self.encoding = encoding
        self.errors = errors
        self.max_dedupe = max_dedupe
        self.max_line = max_line
        self.stats = TailStats()
        self._seen: dict[tuple[str, int], None] = {}
        self._boot_id = ""
        self._offset = 0
        self._inode: int | None = None

    # ------------------------------------------------------------------
    # 解析（纯内存，无 IO）
    # ------------------------------------------------------------------
    def feed_line(self, line: str) -> AgentRecord | None:
        """解析单行；不匹配/坏行/重复一律返回 ``None`` 并计数。"""
        text = line.rstrip("\r\n")
        if not text.startswith(self.prefix):
            self.stats.ignored += 1
            return None
        if len(text) > self.max_line:
            self.stats.oversized += 1
            return None
        payload = text[len(self.prefix):]
        try:
            raw = json.loads(payload)
        except (ValueError, TypeError):
            self.stats.bad += 1
            return None
        if not isinstance(raw, dict):
            self.stats.bad += 1
            return None
        kind = str(raw.get("k") or raw.get("kind") or "").strip().upper()
        if not kind:
            self.stats.bad += 1
            return None
        try:
            seq = int(raw.get("s") or raw.get("seq") or 0)
        except (TypeError, ValueError):
            seq = 0
        boot_id = str(raw.get("b") or raw.get("boot_id") or "")
        try:
            ts = float(raw.get("t") or raw.get("ts") or 0.0)
        except (TypeError, ValueError):
            ts = 0.0
        data = raw.get("d") if isinstance(raw.get("d"), dict) else raw.get("data")
        if not isinstance(data, dict):
            data = {}

        # ★ 去重键必须带 boot_id：重启后 seq 归零，只用 seq 会把新会话全判成重复。
        #   首次见到 boot_id 不算「切换」（那是初始化，不是重启），否则每次启动都虚报一次。
        if boot_id and boot_id != self._boot_id:
            if self._boot_id:
                self.stats.boot_switches += 1
            self._seen.clear()
            self._boot_id = boot_id
        if seq > 0:
            key = (boot_id, seq)
            if key in self._seen:
                self.stats.duplicates += 1
                return None
            self._seen[key] = None
            if len(self._seen) > self.max_dedupe:
                oldest = next(iter(self._seen))
                self._seen.pop(oldest, None)

        self.stats.parsed += 1
        return AgentRecord(kind=kind, seq=seq, boot_id=boot_id, ts=ts, data=data)

    def feed(self, text: str) -> list[AgentRecord]:
        """解析一段文本（按 ``\\n`` 切分）。末尾不完整行请用 ``read`` 的游标语义处理。"""
        out: list[AgentRecord] = []
        for line in str(text).splitlines():
            rec = self.feed_line(line)
            if rec is not None:
                out.append(rec)
        return out

    # ------------------------------------------------------------------
    # 文件跟随（后端侧，不受沙箱限制）
    # ------------------------------------------------------------------
    def read(self, path: str = "", *, max_bytes: int = 4 * 1024 * 1024) -> list[AgentRecord]:
        """从游标读到尾。文件缺失返回空（**不是**错误：策略还没打印过）。

        ``max_bytes`` 只做内存上界：读不完的部分留在游标之后，下次接着读，
        **绝不丢弃尾部**（丢尾等于静默丢事件）。
        """
        target = path or self.path
        if not target:
            raise ValueError("LogTail.read 需要 path")
        try:
            st = os.stat(target)
        except OSError:
            return []

        inode = getattr(st, "st_ino", 0)
        if (self._inode is not None and inode and inode != self._inode) or \
                st.st_size < self._offset:
            # 轮转 / 清空 / 压缩：游标失效，必须从头读。
            self._offset = 0
            self.stats.rotations += 1
        self._inode = inode
        if st.st_size <= self._offset:
            return []

        with open(target, "rb") as fh:
            fh.seek(self._offset)
            chunk = fh.read(max_bytes)
            consumed = len(chunk)
            # 末尾半行：回退游标，等下次补齐（当作坏行丢掉会永久丢这一条）。
            head, sep, tail = chunk.rpartition(b"\n")
            if sep and tail:
                consumed -= len(tail)
                chunk = head + sep
            elif not sep and chunk:
                # 整块都没有换行 ⇒ 一个超长半行，先不消费，等后续数据。
                return []
            self._offset += consumed
        self.stats.bytes_read += consumed
        return self.feed(chunk.decode(self.encoding, self.errors))

    # ------------------------------------------------------------------
    def reset(self) -> None:
        """清空游标与去重窗口（换客户端实例/换日志目录时用）。"""
        self._offset = 0
        self._inode = None
        self._boot_id = ""
        self._seen.clear()

    @property
    def offset(self) -> int:
        return self._offset

    @property
    def boot_id(self) -> str:
        return self._boot_id


__all__ = ["AgentRecord", "LogTail", "MAX_DEDUPE", "MAX_LINE", "PREFIX", "TailStats"]

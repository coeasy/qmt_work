"""文件信号传输：signal/result 文件桥（大 QMT 兜底通道，零部署）。

为什么还需要它：券商把外部进程直连收紧（PID 白名单）之后，
唯一能合法摸到交易的，是**跑在大 QMT 内置 Python 里的策略脚本**。两端之间最稳、
依赖最少的通道就是文件系统：不装 Redis、不开端口、不需要 pyzmq。

协议（每条一行 JSON，行分隔，便于 tail 与崩溃后续读）
-------------------------------------------------
请求  ``<dir>/req/<signal_id>.json``          （先写 .tmp 再 os.replace 原子落盘）
响应  ``<dir>/resp/<signal_id>.json``
事件  ``<dir>/events.ndjson``                 （agent 单写追加，本端 tail 增量读）

★ 事件游标是**字节偏移**，不是 agent 的 ``seq`` —— agent 重启后 ``seq`` 从 1 重来，
  而文件是 append 的 ⇒ ``seq`` 会**回卷**。拿它当水位会静默吞掉新会话的低序号事件
  （整条通道变死而指标全绿）。理由与边界条件详见 ``_drain_events`` 的 docstring。

延迟：受 agent 轮询间隔限制（默认 0.3~1s），因此 EventSemantics 必须是
``POLL_DIFF`` 并声明上界 —— 上层超时守护要据此放宽（否则会误撤未回报的单）。

★ INV-8：所有 IO 都有界（``poll_interval`` + ``timeout``），绝不无限等待。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..events import canonical_from_agent_event
from ..ports import CanonicalEvent, EventSemanticsSpec
from ..transport import MAX_TIMEOUT, TransportError, WireRequest, WireResponse
from .base import BaseTransport
from .semantics import spec_for

log = logging.getLogger("qmt_work.connectors.transports.file_signal")

#: 单轮最多从 events.ndjson 消费的字节数。**只做内存上界，绝不丢事件** ——
#: 读不完的部分留在游标之后，下一轮接着读（「读不完就丢掉后半段」是本文件修过的
#: 老毛病：旧实现按条数 ``out[-limit:]`` 截断，等于每轮静默丢弃超出部分）。
_MAX_DRAIN_BYTES = 4 * 1024 * 1024


class FileSignalProtocolError(TransportError):
    """对端写出的内容不符合 wire 协议（不是「连不上」，是「说不清」）。"""


@dataclass(frozen=True)
class BridgeDirs:
    root: str
    req: str
    resp: str
    events: str


def _mkdirs(root: str) -> BridgeDirs:
    req = os.path.join(root, "req")
    resp = os.path.join(root, "resp")
    for d in (req, resp):
        os.makedirs(d, exist_ok=True)
    return BridgeDirs(
        root=root,
        req=req,
        resp=resp,
        events=os.path.join(root, "events.ndjson"),
    )


class FileSignalTransport(BaseTransport):
    """写请求文件 → 轮询响应文件。

    ``poll_interval`` 默认 0.05s：agent 侧有自己的轮询周期，这里频繁一点能让
    「agent 已写完」到「本端读到」的额外延迟降到可忽略，代价只是每秒 20 次 stat。
    """

    transport_id = "file"

    def __init__(self, bridge_dir: str, *, poll_interval: float = 0.05,
                 auth_token: str = "", agent_timeout: float = MAX_TIMEOUT):
        if not bridge_dir:
            raise ValueError("bridge_dir is required")
        self.bridge_dir = bridge_dir
        self.dirs = _mkdirs(bridge_dir)
        self.poll_interval = max(0.01, float(poll_interval))
        self.auth_token = auth_token or ""
        self.agent_timeout = min(float(agent_timeout), MAX_TIMEOUT)
        self._agent_meta: dict[str, Any] = {}
        #: 事件游标 = **已消费的字节数**（恒落在行边界上）。见 ``_drain_events``：
        #: 为什么不能用 agent 的 ``seq`` 当游标（重启回卷 ⇒ 静默吞事件）。
        self._events_offset: int = 0
        #: events.ndjson 的**文件身份**（``st_dev``/``st_ino``）。轮转（改名 .1、
        #: 新建同名文件）只靠它才能发现 —— 体积可能不缩反而更大。
        self._events_file_id: tuple[int, int] | None = None
        self._agent_ts_ms: int = 0
        self._clock_offset_ms: int = 0

    # ------------------------------------------------------------------
    async def invoke(self, request: WireRequest) -> WireResponse:
        """（重写基类：文件桥不需要把一切异常都归成传输故障。）

        * 文件路径/权限问题 ⇒ transport 故障（503）；
        * agent 返回 ok=False ⇒ 业务失败（400），**不得**吞掉或改写成空结果。
        """
        started = time.perf_counter()
        self._put_request(request)
        try:
            envelope = await self._await_response(request)
        except TransportError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise TransportError(f"[file] {request.op} 等待响应失败: {exc}") from exc
        return self._to_response(envelope, request, started)

    # ------------------------------------------------------------------
    def _put_request(self, request: WireRequest) -> None:
        payload = {
            "v": 1,
            "signal_id": request.signal_id,
            "op": request.op,
            "ts": int(time.time() * 1000),
            "auth": self.auth_token,
            "params": dict(request.params or {}),
        }
        target = Path(self.dirs.req) / f"{request.signal_id}.json"
        tmp = target.with_suffix(".json.tmp")
        # 原子写：agent 侧轮询可能在本端写一半时读文件（半份 JSON 会让它整轮报错）。
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, target)

    async def _await_response(self, request: WireRequest) -> dict[str, Any]:
        target = Path(self.dirs.resp) / f"{request.signal_id}.json"
        deadline = time.perf_counter() + min(request.timeout, self.agent_timeout)
        loop = asyncio.get_running_loop()
        while time.perf_counter() < deadline:
            if target.exists():
                # agent 侧同样先写 tmp 再 replace，故存在即可读。
                raw = await loop.run_in_executor(
                    None, target.read_text, "utf-8")
                try:
                    envelope = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise FileSignalProtocolError(
                        f"响应文件不是合法 JSON: {target} ({exc})") from exc
                if not isinstance(envelope, dict) or "signal_id" not in envelope:
                    raise FileSignalProtocolError(f"响应信封缺少 signal_id: {target}")
                return envelope
            await asyncio.sleep(self.poll_interval)
        raise TransportError(
            f"[file] {request.op} 超时 {request.timeout:.1f}s 未收到响应 "
            f"(signal_id={request.signal_id})；请检查大 QMT 策略是否已运行、"
            f"bridge_dir 两端是否一致、token 是否匹配。"
            f"★ 写操作超时后不得盲目重发（防幽灵单）：先经 QUERY_ORDER 对账确认柜台状态")

    def _to_response(self, envelope: dict[str, Any], request: WireRequest,
                     started: float) -> WireResponse:
        if envelope.get("signal_id") != request.signal_id:
            raise FileSignalProtocolError(
                f"响应 signal_id 不匹配: 期望 {request.signal_id}, "
                f"实际 {envelope.get('signal_id')}")
        self._agent_meta = envelope.get("agent") or self._agent_meta
        # M2.7：时钟偏差诊断（agent 信封 ts - 本端收到时刻）。持续为大负值说明
        # 终端机器时钟落后或轮询极慢 —— 排障时先看它，再看业务错误。
        try:
            self._agent_ts_ms = int(envelope.get("ts") or 0)
            self._clock_offset_ms = self._agent_ts_ms - int(time.time() * 1000)
        except (TypeError, ValueError):
            pass
        if envelope.get("ok"):
            return self._ok(envelope.get("result"), request, started,
                            agent=self._agent_meta)
        # 业务失败：error_type 决定上层映射到 400 还是别的。
        return self._fail(envelope.get("error") or "agent 未给出失败原因",
                          envelope.get("error_type") or "BrokerError",
                          request, started, agent=self._agent_meta)

    # ------------------------------------------------------------------
    def _drain_events(self, max_bytes: int = _MAX_DRAIN_BYTES
                      ) -> list[dict[str, Any]]:
        """按**字节游标**读 agent 新追加的事件（只消费**完整行**）。

        ★★ 游标为什么必须是字节偏移、而不是 ``seq`` 水位（2026-10-01，A13 实测）：
        agent 的 ``seq`` 是**进程内**计数器（``BIGQMT_AGENT._STATE["seq"]`` 从 0 起），
        策略重启后**从 1 重新开始**；而 ``events.ndjson`` 是 **append** 模式 —— 于是
        文件里会出现「上一会话 ``seq`` 1..42」紧跟「新会话 ``seq`` 1..N」这种**回卷**。

        旧实现用 ``last = max(last, seq)`` 维护水位、并 ``seq > since_seq`` 过滤 ⇒
        新会话里 ``seq ≤ 42`` 的事件被**静默吞掉**；更糟的是只要新会话还没跑到 42 条，
        整条事件通道就等于**死的**（前端委托/成交永不刷新、行情停在种子值），
        而所有健康指标与日志**全绿** —— 与 SUB_QUOTE 那个缺陷同一类。

        字节偏移对回卷天然免疫：文件只增不减 ⇒ 新字节里的内容必定没被消费过。
        只有三种情况会让游标**失效**（统称「换了一个文件」），两种信号都识别：
          * **文件身份变化**（``st_ino``）：轮转（``_rotate_if_huge`` 改名 ``.1``）
            后新建的文件与旧文件 id 不同 —— 即使新文件**比旧游标还大**，也只有这条
            能发现（此时 ``size < offset`` 不成立，体积回缩信号会漏）；
          * **体积回缩**（``size < offset``）：截断/重建，兜住 ``st_ino`` 恒为 0 的平台。
        命中即从头重读。三种情形下新文件的内容都是全新的，**不会重复投递**
        （且 ``_canonical_to_ws`` 不消费 ``seq``，重复序号对上层无副作用）。

        只消费到**最后一个换行符**为止：agent 单行 ``write`` 的可见性不是原子承诺，
        末行可能正被写一半 —— 把半行留在游标之后，下一轮再读（半行也会被 JSON
        解析失败跳过，但那会**永久**丢掉那条记录，所以不能提前消费）。
        """
        path = Path(self.dirs.events)
        try:
            st = path.stat()
        except OSError:
            # 不存在 = agent 还没写过 / 正在轮转的空档。游标作废：下次出现时从头读
            # （agent 轮转是 rename + 后续 append，中间确实存在「文件不存在」的窗口）。
            self._events_offset = 0
            self._events_file_id = None
            return []
        ident = (int(getattr(st, "st_dev", 0) or 0), int(getattr(st, "st_ino", 0) or 0))
        size = int(st.st_size)
        if self._events_file_id is not None and (
                ident != self._events_file_id or size < self._events_offset):
            log.info("事件文件已更换（轮转/截断/重建）：游标 %d → 0（%s）",
                     self._events_offset, path)
            self._events_offset = 0
        self._events_file_id = ident
        base = self._events_offset
        if size <= base:
            return []
        try:
            with open(path, "rb") as fh:
                fh.seek(base)
                chunk = fh.read(min(size - base, max(1, int(max_bytes))))
                if len(chunk) < size - base and b"\n" not in chunk:
                    # 单条记录就超过预算（实际不可能：一条记录只有几百字节）。
                    # 放宽到整个剩余区间，否则游标会永远停在半行上、**卡死通道**。
                    chunk = fh.read(size - base)
        except OSError as exc:
            log.warning("读取事件文件失败（下一轮重试）：%s", exc)
            return []
        cut = chunk.rfind(b"\n")
        if cut < 0:
            return []                      # 还没有完整行 ⇒ 不动游标
        self._events_offset = base + cut + 1
        out: list[dict[str, Any]] = []
        for line in chunk[:cut].decode("utf-8", "replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue                   # 损坏行：跳过而非整轮失败
            if isinstance(ev, dict):
                out.append(ev)
        return out

    def stream_events(self) -> list[CanonicalEvent]:
        """EventPort 取流：读增量事件并翻译成 canonical 事件。

        agent 已做过差分（只发变化、首轮不补发），此处**只翻译不二次 diff**；
        废单/拒单判定过 SSOT（``canonical_from_agent_event``），与 miniQMT 推送
        对上层**同形**。游标语义（为什么是字节偏移）见 ``_drain_events``。
        """
        out: list[CanonicalEvent] = []
        for ev in self._drain_events():
            ce = canonical_from_agent_event(
                ev.get("type", ""), ev.get("data", {}), int(ev.get("seq") or 0))
            if ce is not None:
                out.append(ce)
        return out

    def event_semantics(self) -> EventSemanticsSpec:
        """文件桥的事件来自轮询差分，**必须**声明延迟上界（V4 §4.4）。"""
        return spec_for(self.transport_id)

    @property
    def agent_meta(self) -> dict[str, Any]:
        return dict(self._agent_meta)

    @property
    def clock_offset_ms(self) -> int:
        return self._clock_offset_ms


__all__ = ["FileSignalTransport", "FileSignalProtocolError", "BridgeDirs"]

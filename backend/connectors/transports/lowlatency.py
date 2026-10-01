"""低延迟传输：Redis 队列（<50ms）与 ZMQ ROUTER/DEALER（同机极低延迟）。

两者共用**同一套 wire 信封**（``WireRequest``/``WireResponse``），因此与
``FileSignalTransport`` 可以互换而不动一行 dialect 代码 —— 这正是 V4 §3
「Transport × Dialect 正交」要兑现的收益。

零 mock 纪律：第三方库缺失/连接不可达**一律抛错**，不静默降级。
"降级"只能发生在**上层显式选择**时（并且要在能力面标注 DEGRADED），
传输层悄悄换个通道会让「为什么这么慢」变成永远查不出来的问题。
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from ..ports import EventSemanticsSpec
from ..transport import MAX_TIMEOUT, TransportError, WireRequest, WireResponse
from .base import BaseTransport
from .semantics import spec_for

log = logging.getLogger("qmt_work.connectors.transports.lowlatency")


class MissingDependency(TransportError):
    """必需的第三方库未安装。明确告知安装什么，而不是抛 ImportError 堆栈。"""


def _require(module: str, pip_name: str):
    try:
        return __import__(module)
    except ImportError as exc:
        raise MissingDependency(
            f"传输依赖缺失: pip install {pip_name}（随后重启后端）。"
            f"如暂不使用该传输，请改用 transport=file（零依赖兜底通道）。") from exc


class RedisTransport(BaseTransport):
    """Redis 队列传输：rpush 请求 / blpop 响应 / pub-sub 事件。

    队列键带 ``namespace``：同一台 Redis 可被多个 qmt_work 实例共用而不互相串扰。
    """

    transport_id = "redis"

    def __init__(self, url: str, *, namespace: str = "bigqmt",
                 auth_token: str = "", poll_interval: float = 0.01,
                 agent_timeout: float = MAX_TIMEOUT):
        if not url:
            raise ValueError("redis url is required")
        self.url = url
        self.namespace = namespace
        self.auth_token = auth_token or ""
        self.poll_interval = max(0.005, float(poll_interval))
        self.agent_timeout = min(float(agent_timeout), MAX_TIMEOUT)
        self._client: Any = None
        self._pubsub: Any = None
        self._agent_meta: dict[str, Any] = {}
        self._last_event_seq: int = 0
        #: 已处理过的「回卷拐点」= (上一会话末条 seq, 新会话首条 seq)。
        #: 队列里同一批记录会被反复读到，没有它就每轮都归零水位 ⇒ 反复重放旧事件。
        self._rewind_mark: tuple[int, int] | None = None
        self._clock_offset_ms: int = 0

    # ------------------------------------------------------------------
    def _ensure(self):
        if self._client is not None:
            return self._client
        redis = _require("redis", "redis")
        try:
            self._client = redis.Redis.from_url(
                self.url, decode_responses=True, socket_connect_timeout=2.0,
                socket_timeout=2.0)
            self._client.ping()
        except Exception as exc:  # noqa: BLE001  连接不上必须显式报错（503 语义）
            self._client = None
            raise TransportError(f"Redis 不可达({self.url}): {exc}") from exc
        return self._client

    # ------------------------------------------------------------------
    async def invoke(self, request: WireRequest) -> WireResponse:
        client = self._ensure()
        started = time.perf_counter()
        payload = {
            "v": 1, "signal_id": request.signal_id, "op": request.op,
            "ts": int(time.time() * 1000), "auth": self.auth_token,
            "params": dict(request.params or {}),
        }
        req_key = f"{self.namespace}:req"
        resp_key = f"{self.namespace}:resp:{request.signal_id}"
        try:
            # 清理上一条同名残留（进程重启动 + 相同 signal_id 的极端情况）
            client.delete(resp_key)
            client.rpush(req_key, json.dumps(payload, ensure_ascii=False))
            deadline = time.perf_counter() + min(request.timeout, self.agent_timeout)
            envelope = None
            while time.perf_counter() < deadline:
                item = client.blpop(resp_key, timeout=1)
                if item:
                    envelope = json.loads(item[1])
                    break
        except TransportError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise TransportError(f"[redis] {request.op} 传输失败: {exc}") from exc
        if envelope is None:
            raise TransportError(
                f"[redis] {request.op} 超时 {request.timeout:.1f}s 未收到响应 "
                f"(signal_id={request.signal_id})；检查大 QMT 端 agent 是否在运行、"
                f"以及中继 scripts/bigqmt_relay.py 是否启动（redis 模式必须由中继转文件桥）")
        if not envelope.get("ok"):
            return self._fail(envelope.get("error") or "agent 未给出失败原因",
                              envelope.get("error_type") or "BrokerError",
                              request, started)
        self._agent_meta = envelope.get("agent") or self._agent_meta
        try:
            self._clock_offset_ms = int(envelope.get("ts") or 0) - int(time.time() * 1000)
        except (TypeError, ValueError):
            pass
        return self._ok(envelope.get("result"), request, started,
                        agent=self._agent_meta)

    def event_semantics(self) -> EventSemanticsSpec:
        """事件语义 + 延迟上界（集中定义在 transports/semantics.py）。"""
        return spec_for(self.transport_id)

    def stream_events(self) -> list:
        """消费中继写入的 ``{ns}:events`` 队列（M3.2，PUSH_WITH_GAP）。

        数据来自 ``scripts/bigqmt_relay.py`` 对 events.ndjson 的 tail 转发：
        * 按 seq 去重 + 游标增量 —— 中继重启/队列重放不会二次投递；
        * 队列被 LTRIM 截断时产生**缺口**（seq 跳号）：上层 order_watchdog 以
          QUERY_ORDER 对账兜底，本端不假装「读到的就是全部」。
        redis 不可达时抛 TransportError（零 mock），绝不静默返回空列表 ——
        「查不到」和「没有」是两回事。

        ★★ 「按 seq 去重」有一个前提，2026-10-01（A13）才补上 —— 与文件桥同源的缺陷：
        agent 的 ``seq`` 是**进程内**计数器（``BIGQMT_AGENT._STATE["seq"]`` 从 0 起），
        **策略重启后从 1 重来**；而中继是把 events.ndjson 的新行**原样** rpush 上来，
        于是队列里会出现「旧会话 ``seq`` 1..42」紧跟「新会话 ``seq`` 1..」的**回卷**。
        旧水位 42 会把新会话里 ``seq ≤ 42`` 的记录**静默过滤掉**：前端委托/成交不再
        刷新、行情停在上一笔，而健康指标与日志全绿。中继那句「旧行重放无害（消费端
        按 seq 去重）」的注释也正因此**失效** —— 是它掩盖了这个回卷。

        ⇒ 读到队列里**最后一个 down 拐点**即认定「新会话开始」，水位归零并**从该点**
          起重放（不是从 0 重放，否则会把上个会话已投递过的事件再发一遍）。
          ``_rewind_mark`` 记住已处理过的拐点，避免同一批记录被反复重放。

        ★ 拐点之前的记录**永远不再扫描**（哪怕不是新拐点）—— 它们属于更早的会话，
        ``seq`` 与当前水位**不可比**：例如旧会话 seq 2/3 大于新会话的水位 1，按水位
        过滤就会把旧事件当新事件再投一遍（实测踩到，由
        ``test_redis_transport_survives_agent_seq_rewind`` 的收尾断言抓住）。

        ⚠️ 残留缺口（诚实记录，非本端能消除）：若旧会话的记录在新会话开始前就被
        ``LTRIM`` 全部挤掉，拐点不可见 ⇒ 无法发现回卷。这正是 ``PUSH_WITH_GAP``
        的语义边界，由 order_watchdog 的 QUERY_ORDER 对账兜底（``--keep-events``
        默认 5000 条 + 20ms 轮询，实践中拐点几乎不可能漏过）。
        """
        client = self._ensure()
        from ..events import canonical_from_agent_event
        try:
            rows = client.lrange(f"{self.namespace}:events", 0, -1)
        except Exception as exc:  # noqa: BLE001
            raise TransportError(f"[redis] 事件队列读取失败: {exc}") from exc
        parsed: list[tuple[int, dict]] = []
        for raw in rows:
            try:
                ev = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(ev, dict):
                parsed.append((int(ev.get("seq") or 0), ev))
        start = self._resync_on_rewind(parsed)
        out = []
        for seq, ev in parsed[start:]:
            if seq <= self._last_event_seq:
                continue
            self._last_event_seq = seq
            ce = canonical_from_agent_event(ev.get("type", ""),
                                            ev.get("data", {}), seq)
            if ce is not None:
                out.append(ce)
        return out

    def _resync_on_rewind(self, parsed: list[tuple[int, dict]]) -> int:
        """队列里出现 ``seq`` 回卷 ⇒ 新会话开始：水位归零，返回**有效扫描起点下标**。

        回卷 = 后一条的 ``seq`` **不增**（agent 侧 seq 严格递增，唯一能让它变小的
        原因就是策略重启、计数器从头开始）。

        两条纪律：
        * 只在**新的**拐点上归零（``_rewind_mark`` 去重）—— 队列会被反复整读，
          无条件归零会把整个新会话每轮重放一遍（重复投递比漏投递更难查）；
        * 拐点之前的记录**永远不再扫描**（无论是否新拐点）—— 它们属于更早的会话，
          ``seq`` 与当前水位**不可比**：旧会话 seq 2/3 大于新会话水位 1，按水位过滤
          就会把旧事件当新事件再投一遍。
        """
        mark: tuple[int, int] | None = None
        start = 0
        prev: int | None = None
        for i, (seq, _) in enumerate(parsed):
            if prev is not None and seq <= prev:
                mark, start = (prev, seq), i    # 取**最后**一个拐点 = 最新会话起点
            prev = seq
        if mark is None:
            self._rewind_mark = None
            return 0
        if mark != self._rewind_mark:
            self._rewind_mark = mark
            self._last_event_seq = 0
            log.info("[redis] 检测到 agent 会话回卷（seq %s → %s）："
                     "事件水位归零并从 #%d 重放", mark[0], mark[1], start)
        # ★ 返回 start（而非 0）：非新拐点时也必须跳过上一会话的记录
        return start

    @property
    def agent_meta(self) -> dict[str, Any]:
        return dict(self._agent_meta)

    @property
    def clock_offset_ms(self) -> int:
        return self._clock_offset_ms


    async def close(self) -> None:
        try:
            if self._pubsub is not None:
                self._pubsub.close()
            if self._client is not None:
                self._client.close()
        except Exception:  # noqa: BLE001  —— 关闭失败不得影响停机路径（TD-25）
            pass
        self._client = None


class ZmqTransport(BaseTransport):
    """ZMQ ROUTER/DEALER 传输（同机最低延迟）。

    DEALER 本端 + agent 侧 ROUTER：回包由 identity 原生路由，无需显式 signal_id 队列。
    仍然保留 signal_id 以便做请求-响应配对校验（跨 N 并发不错位）。
    """

    transport_id = "zmq"

    def __init__(self, addr: str, *, auth_token: str = "",
                 recv_timeout_ms: int = 2000, agent_timeout: float = MAX_TIMEOUT):
        if not addr:
            raise ValueError("zmq addr is required (e.g. tcp://127.0.0.1:5555)")
        self.addr = addr
        self.auth_token = auth_token or ""
        self.recv_timeout_ms = max(100, int(recv_timeout_ms))
        self.agent_timeout = min(float(agent_timeout), MAX_TIMEOUT)
        self._ctx: Any = None
        self._sock: Any = None

    # ------------------------------------------------------------------
    def _ensure(self):
        if self._sock is not None:
            return self._sock
        zmq = _require("zmq", "pyzmq")
        try:
            self._ctx = zmq.Context.instance()
            self._sock = self._ctx.socket(zmq.DEALER)
            self._sock.setsockopt(zmq.RCVTIMEO, self.recv_timeout_ms)
            self._sock.setsockopt(zmq.LINGER, 0)   # 关闭不阻塞（TD-25）
            self._sock.connect(self.addr)
        except Exception as exc:  # noqa: BLE001
            self._sock = None
            raise TransportError(f"ZMQ 连接失败({self.addr}): {exc}") from exc
        return self._sock

    # ------------------------------------------------------------------
    async def invoke(self, request: WireRequest) -> WireResponse:
        sock = self._ensure()
        started = time.perf_counter()
        payload = {
            "v": 1, "signal_id": request.signal_id, "op": request.op,
            "ts": int(time.time() * 1000), "auth": self.auth_token,
            "params": dict(request.params or {}),
        }
        try:
            sock.send_json(payload)
            deadline = time.perf_counter() + min(request.timeout, self.agent_timeout)
            envelope = None
            while time.perf_counter() < deadline:
                try:
                    envelope = sock.recv_json()
                except Exception:  # noqa: BLE001  recv 超时（RCVTIMEO）
                    continue
                if envelope and envelope.get("signal_id") == request.signal_id:
                    break
                envelope = None
        except TransportError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise TransportError(f"[zmq] {request.op} 传输失败: {exc}") from exc
        if envelope is None:
            raise TransportError(
                f"[zmq] {request.op} 超时 {request.timeout:.1f}s 未收到响应 "
                f"(signal_id={request.signal_id})")
        if not envelope.get("ok"):
            return self._fail(envelope.get("error") or "agent 未给出失败原因",
                              envelope.get("error_type") or "BrokerError",
                              request, started)
        return self._ok(envelope.get("result"), request, started)

    def event_semantics(self) -> EventSemanticsSpec:
        """事件语义 + 延迟上界（集中定义在 transports/semantics.py）。"""
        return spec_for(self.transport_id)

    def stream_events(self) -> list:
        """ZMQ 只做请求-响应；事件流尚未接（没有对应的 relay 实现）→ 当前无流。

        诚实返回空：低延迟事件流目前由 redis 中继提供
        （scripts/bigqmt_relay.py，M3.1/M3.2）；需要事件流的部署请选
        qmt.big.bridge.redis，上层 order_watchdog 另有 QUERY_ORDER 对账兜底。
        """
        return []


    @property
    def agent_meta(self) -> dict[str, Any]:
        return {}


    async def close(self) -> None:
        try:
            if self._sock is not None:
                self._sock.close(linger=0)
            if self._ctx is not None:
                self._ctx.term()
        except Exception:  # noqa: BLE001
            pass
        self._sock = None
        self._ctx = None


__all__ = ["RedisTransport", "ZmqTransport", "MissingDependency"]

"""BigQMT 桥：把 connectors 层的 ``GenericConnector``（bigqmt.v1 + file/redis/zmq 传输）
包装成 xtquant_client 兼容的 Bridge **与** Adapter 接口，使 ``SignalRouter`` /
``ExecutionService`` / 账户页 / 同步引擎 / 事件泵**无需改动**即可驱动大 QMT
（路径 B：agent 隔离 + 文件/redis/zmq 桥）。

★ 这是「全量接通 Big QMT」到生产交易链路的接入点：``BrokerManager`` 在
  ``ConnectionConfig`` 携带 ``connector_key``（如 ``qmt.big.bridge.file``）时，
  用它替代 ``XTQuantBridge``（且 ``Connection.adapter`` 与 ``Connection.bridge``
  **是同一个对象**）。

为什么必须实现「两套面」
------------------------
``BrokerManager`` 的 ``Connection`` 有两个槽位，二者的调用形态不同：

| 槽位        | 谁在用                                   | 期望形态                       |
|-------------|------------------------------------------|--------------------------------|
| ``bridge``  | phase_broker / health / watchdog / pump   | ``async start/stop``、``call``、``pump_running``、``start_pump_on`` |
| ``adapter`` | 账户页 / 同步引擎 / status_list / 诊断     | 同步属性 + 可 ``await bridge.call(adapter.X)`` 的方法 |

小 QMT 侧这两个槽位是**两个对象**（真 adapter + ``XTQuantBridge``）；大 QMT 侧
只有桥一个对象，因此它必须**同时满足两套契约**。历史上这里只实现了一部分，
导致：`await bridge.start()` 拿到 None 抛 TypeError、``bridge.stop()`` 不存在
（停机 AttributeError）、``start_pump_on`` 缺失（事件泵永不启动 → 大 QMT 的
委托/成交/行情事件到不了前端）、账户页 ``conn.adapter.get_account()`` 直接
AttributeError。本文件现已把这些面全部补齐，并由
``backend/tests/test_bigqmt_bridge_face.py`` 逐条锁死。

线程/事件循环纪律（每一条都有事故来源）
--------------------------------------
1. **``call_locked`` 必须用 ``asyncio.Lock``，绝不能用 ``threading.Lock`` 包 await。**
   本桥的网关方法是 async；若在事件循环线程上用 ``threading.Lock`` 包住 ``await``，
   第二个并发写调用会把**事件循环线程**阻塞在 ``acquire`` 上，而第一个协程正因为
   循环被阻塞而无法恢复 ⇒ **整个后端永久卡死**。``XTQuantBridge`` 之所以能用
   ``threading.Lock``，是因为它把调用 offload 到了线程池（锁持在工作线程上）。
2. **同步方法（``connect_sync``/``test_connection``）不在运行中的 loop 上 ``asyncio.run``。**
   被事件循环线程调用时改走工作线程执行，否则 ``asyncio.run`` 直接 RuntimeError。
3. **``close()`` 可从任意线程调用**：取消泵任务要走 ``call_soon_threadsafe``。
4. 大 QMT 文件桥不支持回调式订阅（A2 已落地：跨线 ``UnsupportedOp``），故
   ``subscribe_quote`` 只**记录意图**（纯内存、零 IO、绝不在同步语境里等 IO），
   真正的下发由事件泵每秒对账完成（``_reconcile_quote_subscriptions``）：
   把 codes 经 ``SUB_QUOTE`` 交给 agent → agent 轮询差分 →
   ``events.ndjson`` → 事件泵 → WS。
   ★ 曾经这里只写了一句 ``log.debug`` 就当订阅成功了 —— ``sync`` 于是
   ``_subscribed_codes.update(codes)`` + 日志「subscribed to broker」，
   而 **``SUB_QUOTE`` 从未发出**：agent 的订阅集恒为空，``quote_events``
   恒返回 0，事件泵每秒空转，界面价格永远停在订阅那一刻的种子值。
   连接状态、健康检查、订阅日志全绿 —— 教科书式的「绿灯是另一个 bug 遮出来的」。

★ 职责拆分（2026-10-01）：本模块原同时承载 **gateway 面**（canonical op →
  旧 ``XTQuantGateway`` shape 的适配），加了存活判据后越过单文件 50KB 上限
  （``check_execution_architecture.py`` Gate 4）。该面连同它的三个 shape 助手
  （``_split_instrument`` / ``_snapshot_to_legacy`` / ``_raw_of``）已迁到
  ``connectors/bigqmt_gateway.py``（``_BigQmtGateway``）。本模块只保留
  **双槽位本体 / 事件泵 / 行情订阅对账 / 存活判据**。两个模块的模块级
  docstring **各自只讲自己那一半**，避免同一份设计说明两处漂移。
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

# gateway 面（canonical op → 旧 XTQuantGateway shape）住在独立模块；这里导入供
# ``BigQmtBridge.__init__`` 构造 ``self.gateway`` 使用。单向依赖：gateway 模块
# 对本模块只做鸭子类型调用，不 import（否则成环）。
from connectors.bigqmt_gateway import _BigQmtGateway

log = logging.getLogger("qmt_work.bigqmt_bridge")

#: 单次同步包装的硬上界（对齐 transport.MAX_TIMEOUT 的量级）：
#: 停机/健康检查路径禁止无界等待（TD-25）。
_SYNC_TIMEOUT = 30.0

#: 行情订阅**成功**后多久盲重发一次 ``SUB_QUOTE``（秒）。
#: 用途：覆盖「agent 重启导致它自己的订阅集清零」这类本端看不见的状态丢失。
#: 一次文件桥小请求/分钟，代价可忽略；agent 侧是 ``set.update``，幂等。
_QUOTE_RESYNC_SECONDS = 60.0

#: 行情订阅**失败**后的重试间隔（秒）。取值远大于心跳周期（1s）：
#: 否则每轮心跳都重试、且每轮都要等满 ``agent_timeout`` 才返回，会把泵的
#: 循环周期拖长（泵除了对账还要取事件）。
_QUOTE_RETRY_SECONDS = 5.0

#: 距上次**成功**的 agent 往返多久之后开始主动探活（秒）。取值略大于
#: ``_QUOTE_RESYNC_SECONDS``(60)：正常有订阅时，那次 60s 盲重发本身就是一次
#: 往返证据，于是正常路径**永远不会**多发探活请求。
_LIVENESS_IDLE_SECONDS = 90.0

#: 探活请求的**短**超时（秒）。必须远小于 ``MAX_TIMEOUT``(30s)：agent 真的死掉时
#: 每次探活都要等满超时，用 30s 会把同一轮的事件派发一起拖住。
_LIVENESS_PROBE_TIMEOUT = 5.0

#: 探活失败后的重试间隔（秒）；与 ``_QUOTE_RETRY_SECONDS`` 同思路的退避。
_LIVENESS_RETRY_SECONDS = 30.0

#: 连续多少次探活无应答才判「不可用」。1 次就判死，会把「agent 正在写一份很大的
#: 响应 / 文件系统瞬时抖动」误报成掉线，进而触发无谓的重连。
_LIVENESS_FAILURES_TO_DIE = 2


async def _call_with_optional_timeout(fn, timeout: float):
    """调 ``fn(timeout=...)``；签名不接受该关键字时退回 ``fn()``。

    为什么要容错：产线的 ``GenericConnector.test_connection`` 支持短超时预算，
    但测试替身/旧实现只写 ``async def test_connection(self)``。为了探活这点小事
    让替身全部改签名，是把测试的负担加给生产代码 —— 不如在这里兼容一次。
    """
    try:
        ret = fn(timeout=timeout)
    except TypeError:
        ret = fn()
    return await ret if inspect.isawaitable(ret) else ret


def _canonical_to_ws(event: Any) -> dict:
    """CanonicalEvent → WS 事件帧（与 wsEvents.ts 消费契约对齐）。"""
    kind = getattr(event, "kind", None) or getattr(event, "type", "system")
    data = getattr(event, "data", None) or {}
    return {"type": str(kind), "data": dict(data)}


class BigQmtBridge:
    """xtquant 兼容的大 QMT 桥（路径 B：agent 隔离 + 文件/redis/zmq 桥）。

    同时充当 ``Connection.adapter`` 与 ``Connection.bridge``（``BrokerManager`` 同一对象），
    因此需同时提供 ``XTQuantBridge`` 在被二者调用时所需的全部方法。
    """

    def __init__(self, connector: Any, conn_id: str, name: str = "",
                 connector_key: str = ""):
        self.connector = connector
        self.conn_id = conn_id
        self.connector_key = connector_key
        if name:
            self.broker_name = name
        self.gateway = _BigQmtGateway(connector, self)
        self._connected = False
        self._handlers: dict[str, list] = {}
        self._pump_task: Any = None
        self._pump_on_loop: Any = None
        # 写操作串行化：按「运行中的 loop」缓存 asyncio.Lock（见模块 docstring 第 1 条）。
        self._write_locks: dict[int, asyncio.Lock] = {}
        self._loop_guard = threading.Lock()
        # 同步方法 offload 专用线程池：**自建**而非用默认 executor ——
        # 默认池的线程在解释器退出时会被 atexit join，把可能无界阻塞的活丢进去
        # 会让进程退不出去（TD-25）。停机时显式 shutdown(wait=False)。
        self._pool: ThreadPoolExecutor | None = None
        #: 适配器回调注册位（大 QMT 的真实事件经 EventPort 泵送出，见 ``on_order``）。
        self._order_cbs: list = []
        self._trade_cbs: list = []
        self._disconnect_cbs: list = []
        # ---- 行情订阅对账状态（见 ``want_quotes`` / ``_reconcile_quote_subscriptions``）----
        #: 本地**意图**：期望 agent 转发哪些标的。只增不减 —— agent 没有 UNSUB op，
        #: 谎报「已退订」比留着一个多余的转发更坏（与 ``subscribe_positions`` 同口径）。
        self._want_quotes: set[str] = set()
        #: 已**确认下发**的集合（``SUB_QUOTE`` 收到成功应答那一刻的 ``_want_quotes``）。
        self._applied_quotes: set[str] = set()
        #: 最近一次下发的失败原因（诊断面用；空串 = 未失败）。
        self._quote_sync_error: str = ""
        #: **上次尝试**下发的那份意图（失败也记）。退避闸门比的是它 —— 比
        #: ``_applied_quotes`` 会让失败路径每轮心跳都重试（见对账方法说明）。
        self._quote_last_attempt: set[str] | None = None
        #: 下一次**允许**下发订阅的时刻（``time.monotonic()``）。成功→长周期重发；
        #: 失败→短退避重试。见 ``_reconcile_quote_subscriptions``。
        self._quote_next_attempt_at: float = 0.0
        # ---- 存活判据（见 ``_liveness_probe`` / ``is_connected``）----
        #: 上一次**成功完成 agent 往返**的时刻（``time.monotonic()``；0 = 从未）。
        self._last_agent_ok: float = 0.0
        #: 连续探活无应答次数（任何一次成功往返即清零）。
        self._liveness_failures: int = 0
        #: 「agent 连续无应答」——``is_connected()`` 据此如实返回 False。
        self._agent_unresponsive: bool = False
        #: 下一次**允许**探活的时刻（退避闸门）。
        self._liveness_next_at: float = 0.0

    # ---------------- adapter 身份元数据（status_list / registry 直读属性） ----------------
    # ``manager.status_list`` 对 adapter 无 getattr 兜底，缺一个属性就会把
    # /brokers 整个打挂 —— 下列四项与 BrokerAdapter 契约同名同义。
    adapter_id = "bigqmt"
    #: 类级默认值（实例可覆盖）：``_version_profile`` / ``status_list`` 会按
    #: **类**取属性，只在 __init__ 里赋实例属性会让结构性检查看不到它。
    broker_name = "Big QMT (Agent)"
    sdk_required = "bigqmt_agent"
    supported_periods = ("1m", "5m", "15m", "30m", "1h", "1d", "1w", "1mo", "1q", "1hy", "1s")
    supported_account_types = ("STOCK", "CREDIT")

    @property
    def account_id(self) -> str:
        return ""

    @property
    def client_version(self) -> str:
        meta = getattr(self.connector.transport, "agent_meta", None)
        meta = meta() if callable(meta) else (meta or {})
        return "agent " + str((meta or {}).get("ver") or "?")

    # ---------------- 工作线程池（自建 + 有界停机） ----------------
    def _ensure_pool(self) -> ThreadPoolExecutor:
        with self._loop_guard:
            if self._pool is None:
                self._pool = ThreadPoolExecutor(max_workers=2,
                                                thread_name_prefix="bigqmt")
            return self._pool

    def _shutdown_pool(self) -> None:
        with self._loop_guard:
            pool, self._pool = self._pool, None
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)

    # ---------------- 同步语境里跑协程（唯一的 asyncio.run 入口） ----------------
    def _run_sync(self, coro):
        """在同步方法里跑完一个协程。

        · 当前线程**没有**运行中的循环（``BrokerManager`` 的同步线程 / 线程池 worker）
          ⇒ ``asyncio.run``；
        · 当前线程**有**运行中的循环（被事件循环直接同步调用）⇒ 不能 ``asyncio.run``
          （RuntimeError），换到临时工作线程执行 —— 这条路径会短暂阻塞调用线程，
          因此产线调用点一律走 ``await bridge.call(...)``，此处只是最后一道防线。
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)
        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="bigqmt-sync")
        try:
            fut = pool.submit(lambda: asyncio.run(coro))
            return fut.result(timeout=_SYNC_TIMEOUT)
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    # ---------------- 同步生命周期（供 BrokerManager 线程调用） ----------------
    async def _probe_agent(self) -> dict:
        """探活 agent 并把桥标记为已连接（唯一的「连上」判定点）。"""
        info = await self.connector.test_connection()
        self._connected = True
        # 握手成功就是一次货真价实的往返证据：刷新存活戳并解除「无应答」标记
        # （重连成功路径据此自愈，不需要额外的复位逻辑）。
        self._stamp_agent_ok()
        return info if isinstance(info, dict) else {}

    async def start(self) -> dict:
        """验证 agent 可达性（PROBE）并启动事件泵：失败即连接失败。

        ★ 必须是 **async**：``phase_broker._start_one`` / ``health._reconnect``
          都写 ``await conn.bridge.start()`` 并配 ``asyncio.wait_for`` 超时。
          曾经这里是同步方法 —— ``await None`` ⇒ TypeError，大 QMT 连接在启动
          阶段就永远被判失败，且异常被 ``except`` 吞成「连不上」，无从排查。
        """
        info = await self._probe_agent()
        try:
            self.start_pump_on(asyncio.get_running_loop())
        except RuntimeError:  # pragma: no cover - 防御：不在事件循环里
            pass
        return info

    def connect_sync(self) -> None:
        """``start()`` 的同步别名 —— ``BrokerManager._safe_start`` / ``connect``
        对大 QMT 连接统一按这个名字调用（旧实现缺失该方法，启动即
        AttributeError 被 ``except`` 吞成「永远连不上」）。

        ★ 刻意**不**在这里建事件泵：本方法跑在 BrokerManager 的一次性线程里，
          那个 loop 用完即关，在它上面建的泵会立刻被取消（还会产生
          「Task was destroyed」噪声）。真正的泵建在应用主事件循环上 ——
          ``phase_broker._start_one``（连接时）或 ``phase_watchdogs._pump_guard``
          （每 2s 兜底）。
        """
        self._run_sync(self._probe_agent())

    async def stop(self) -> None:
        """``XTQuantBridge.stop`` 的同形异步停机口。

        ``app/bootstrap/shutdown.py`` 停机时统一 ``await conn.bridge.stop()``：
        大 QMT 桥缺这个方法会让**优雅停机直接抛 AttributeError**（停机路径禁止
        无界等待，也禁止因一个连接炸掉整轮停机）。
        """
        self._cancel_pump()
        self._connected = False
        try:
            await self.connector.close()
        except Exception as exc:  # noqa: BLE001  停机不得因单条连接失败而中断
            log.warning("BigQmtBridge.stop: connector.close 失败: %s", exc)
        self._shutdown_pool()

    def close(self) -> None:
        """同步关闭（``BrokerManager.disconnect`` / ``remove`` 走这里）。"""
        self._cancel_pump()
        self._connected = False
        self._shutdown_pool()

    def _cancel_pump(self) -> None:
        """取消事件泵（可从任意线程调用）。"""
        task = self._pump_task
        self._pump_task = None
        if task is None:
            return
        loop = self._pump_on_loop
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        try:
            if loop is not None and loop is not running and not loop.is_closed():
                loop.call_soon_threadsafe(task.cancel)
            else:
                task.cancel()
        except Exception:  # noqa: BLE001  已结束/已关闭的循环：无需再取消
            pass

    def is_connected(self) -> bool:
        """桥**当前是否可用**（不是「曾经连上过」）。

        ★ 判据必须包含「agent 没有连续无应答」（2026-10-01 修正）
        ------------------------------------------------------
        旧实现只返回 ``self._connected``，而那个标志只在 ``start()`` 握手成功时
        置真、只在显式 ``stop()/close()`` 置假 —— **agent 进程死掉（QMT 被关掉 /
        策略被停止）之后它永远是真**。后果是整条链路一起说谎：

        * 前端一直显示「已连接」；
        * ``gateway.health._check`` 永远判 ``ok=True`` ⇒ ``health_status`` 恒为
          ``connected``，``_reconnect`` **永远不会被调度**，自愈能力形同不存在；
        * 用户的每一次操作都要自己撞上 503 才发现「其实早就断了」。

        这是本项目反复出现的「假绿灯」家族（同 A12/A13）。文件桥没有任何
        「连接」可言（它只是两个目录），所以唯一诚实的判据是**一次真实的
        请求/应答往返**：泵每分钟做一次有界探活（``_liveness_probe``），
        连续 ``_LIVENESS_FAILURES_TO_DIE`` 次无应答即置 ``_agent_unresponsive``。

        本方法仍满足 HealthPort 的 O(1)/零 IO 契约：只读内存里的布尔量。
        """
        return self._connected and not self._agent_unresponsive

    def _stamp_agent_ok(self) -> None:
        """记一次**成功的 agent 往返**（唯一改变存活结论的正向证据）。"""
        self._last_agent_ok = time.monotonic()
        self._liveness_failures = 0
        self._agent_unresponsive = False

    async def _liveness_probe(self) -> None:
        """空闲超限时主动探一次活；失败累计到阈值即如实标记 agent 不可用。

        为什么要它、以及为什么不用 ``agent_status.json`` 心跳做判据，见
        ``is_connected`` 与下面的 ★ 段。

        ★ 为什么不用 ``agent_status.json`` 心跳新鲜度：``handlebar`` 只在
          **行情推进**时被 QMT 调用，收盘后心跳自然过期 —— 拿它当判据会在非交易
          时段把一切正常的连接误报成掉线（比假绿灯更糟，会引发重连风暴）。
          往返判据与时段时间无关：只要 agent 进程活着，它就必须能在几百毫秒内
          回一个 PROBE。
        """
        now = time.monotonic()
        if now < self._liveness_next_at:
            return
        if self._last_agent_ok and now - self._last_agent_ok < _LIVENESS_IDLE_SECONDS:
            return      # 近期有真实往返（业务调用/订阅对账/握手）⇒ 无需额外请求
        self._liveness_next_at = now + _LIVENESS_RETRY_SECONDS
        fn = getattr(self.connector, "test_connection", None)
        if not callable(fn):
            return
        try:
            await _call_with_optional_timeout(fn, _LIVENESS_PROBE_TIMEOUT)
        except Exception as exc:  # noqa: BLE001  探活失败只记账，绝不向上抛
            self._liveness_failures += 1
            if self._liveness_failures >= _LIVENESS_FAILURES_TO_DIE:
                if not self._agent_unresponsive:
                    log.warning(
                        "bigqmt[%s]：agent 连续 %d 次探活无应答（%s）"
                        "⇒ 如实标记为不可用，等待自动重连",
                        self.conn_id, self._liveness_failures, exc)
                    # 2026-10-03：这里是「可用性由可用变不可用」的**边沿**。
                    # 只在翻真的那一刻发一次，避免每次探活失败都刷屏。
                    # 与 xtquant 侧 SDK 的 on_disconnected 回调语义对齐——同一条
                    # broker.disconnected 链路，两条连接形态都能走通。
                    self.enqueue({"type": "disconnected", "detail": str(exc)[:200]})
                self._agent_unresponsive = True
            return
        self._stamp_agent_ok()

    def test_connection(self) -> dict:
        """同步包装（供 BrokerManager.connect / probe_transient 调用，返回结构化 dict）。"""
        info = self._run_sync(self.connector.test_connection())
        self._stamp_agent_ok()      # 这次同步往返同样是存活证据
        base = {"connected": True, "detail": "bigqmt agent reachable"}
        if isinstance(info, dict):
            base.update({k: v for k, v in info.items() if k not in base})
        return base

    def version_profile(self) -> dict:
        """对齐 ``XTQuantBridge`` 画像接口，前端据此展示连接形态。"""
        return {
            "client_type": "bigqmt",
            "broker_name": self.broker_name,
            "sdk_version": "agent",
            "connector_key": self.connector_key,
            "transport": getattr(self.connector, "transport_id", "") or "",
        }

    # ---------------- 事件泵（与 XTQuantBridge 同名同形） ----------------
    def pump_running(self) -> bool:
        """泵是否真的在跑。

        ★ 曾经这里返回 ``self._connected`` —— 那不是「泵在跑」而是「桥连上了」，
          于是 ``phase_watchdogs._pump_guard`` 的 ``if not b.pump_running()`` 永远
          为假，``start_pump_on`` 永不调用 ⇒ 大 QMT 的委托/成交/行情事件**从未
          被泵出去过**。判据只能是任务本身。
        """
        task = self._pump_task
        return task is not None and not task.done()

    def start_pump_on(self, loop: asyncio.AbstractEventLoop) -> None:
        """在指定（主）事件循环上确保泵已启动（幂等），对齐 ``XTQuantBridge``。"""
        if self.pump_running():
            return
        self._pump_on_loop = loop
        self._pump_task = loop.create_task(self._pump_loop_run())

    async def _pump_loop_run(self) -> None:
        """周期拉取 agent 增量事件（events.ndjson）并派发给 WS 订阅者。

        ★ 有界纪律：单轮失败只记日志继续；``_connected`` 为假即退出，不空转。
        """
        stream = getattr(self.connector, "stream_events", None)
        if stream is None:
            return
        while self._connected:
            # 先对账行情订阅，再取事件：同一个 1s 心跳里「告诉 agent 要什么」与
            # 「收 agent 给了什么」，订阅生效的延迟因此只有一轮心跳。
            try:
                await self._reconcile_quote_subscriptions()
            except Exception:  # noqa: BLE001  订阅对账失败不得影响事件派发
                # 失败原因已由 reconcile 自己写进 ``_quote_sync_error``（单一来源），
                # 这里只记日志；下一轮心跳（1s 后）会自然重试。
                log.debug("bigqmt 行情订阅对账失败", exc_info=True)
            # 存活探活：只在「近期没有任何真实往返」时才真的发请求（见 _liveness_probe），
            # 因此正常路径零额外开销；agent 死掉时它会如实把 is_connected 翻成 False。
            try:
                await self._liveness_probe()
            except Exception:  # noqa: BLE001  探活失败绝不影响事件派发
                log.debug("bigqmt 存活探活失败", exc_info=True)
            try:
                for ev in (stream() or []):
                    self.enqueue(_canonical_to_ws(ev))
            except Exception:  # noqa: BLE001  单轮失败不影响后续
                log.debug("bigqmt event pump 单轮失败", exc_info=True)
            await asyncio.sleep(1.0)

    # ---------------- 行情订阅：意图登记 + 泵内对账 ----------------
    def want_quotes(self, codes: list[str]) -> None:
        """登记期望订阅的标的（由 ``subscribe_quote`` 调用；纯内存、线程安全区外无 IO）。

        线程安全说明：只做一次 ``set.update``。CPython 的 ``set.update`` 在 GIL 下
        不会让读到中间态——即便与泵的 ``set(...)`` 快照交错，最坏结果是「这一轮少下发
        一个 code、下一轮补上」，不会丢订阅（对账是幂等的整集下发）。
        """
        fresh = {str(c) for c in (codes or []) if c}
        if fresh:
            self._want_quotes |= fresh

    def _agent_quote_count(self) -> int:
        """agent 诚实上报的已订阅**数量**（``meta().subscribed``；无 meta 时 0）。

        用途：**诊断面**（``connector_probe``）让人一眼看出「agent 到底收到了几个」；
        不参与下发判定 —— 它是最近一次响应信封的缓存，事件泵只读文件、不发请求，
        缓存可能长期不刷新，拿它当重发依据会漏掉 agent 重启（见
        ``_reconcile_quote_subscriptions`` 的说明）。
        """
        tp = getattr(self.connector, "transport", None)
        meta = getattr(tp, "agent_meta", None)
        meta = meta() if callable(meta) else (meta or {})
        if not isinstance(meta, dict):
            return 0
        try:
            return int(meta.get("subscribed") or 0)
        except (TypeError, ValueError):
            return 0

    async def _reconcile_quote_subscriptions(self) -> None:
        """把本地意图收敛到 agent 侧（幂等整集下发 + 有界重试 + 自愈）。

        下发时机（任一成立即下发）：
          1. 意图集合变了（``_applied_quotes != _want_quotes``）—— 新订阅，**立即**下发；
          2. 距上次**成功**下发已过 ``_QUOTE_RESYNC_SECONDS`` —— 盲重发一次。

        为什么需要第 2 条（而不是只做条件 1）
        ------------------------------------
        agent 是独立进程，重启后它自己的 ``state.subscribed`` 清零，而本端
        ``_applied_quotes`` 还在 ⇒ 只比集合会**永久漏掉**这条：行情静默断流、
        毫无提示。想靠 agent 自报的 ``meta().subscribed`` 发现重启也不行 ——
        ``transport.agent_meta`` 是**最近一次响应信封**的缓存，而事件泵只读
        ``events.ndjson``、一个请求都不发，缓存根本没机会刷新（依赖「别的模块
        恰好有流量」是隐式耦合，不可测）。所以这里退一步用**时间**兜底：
        盲重发一次 ``SUB_QUOTE``，agent 侧是 ``set.update``，幂等无副作用。

        为什么整集下发而不是增量：``SUB_QUOTE`` 在 agent 侧就是
        ``state.subscribed.update(codes)``，整集下发天然幂等，且能在 agent
        丢状态后一次性补齐，本端不需要维护增量游标。

        为什么重试要退避：失败时若每个心跳（1s）都重试，而每轮失败都要等满
        ``agent_timeout`` 才返回，泵的循环周期会被拖长。失败后在
        ``_QUOTE_RETRY_SECONDS`` 内不再尝试 —— 既不风暴，又能在 agent 恢复后
        自动接上（成功即回到 ``_QUOTE_RESYNC_SECONDS`` 的长周期）。

        ★ 闸门比的是「**上次尝试**下发的那份意图」（``_quote_last_attempt``），
          不是「已成功」的 ``_applied_quotes``：失败时后者恒为空，拿它比会得出
          「意图变了」⇒ 每轮心跳都重试，退避形同虚设。
        """
        want = set(self._want_quotes)
        if not want:
            return
        now = time.monotonic()
        if self._quote_last_attempt == want and now < self._quote_next_attempt_at:
            return
        fn = getattr(self.connector, "subscribe_quote_polling", None)
        if not callable(fn):
            # 进程内直连（miniQMT）没有这条 op：真实订阅走适配器回调，此处无需对账。
            self._quote_last_attempt = want
            self._applied_quotes = set(want)
            return
        self._quote_last_attempt = want
        try:
            await fn(sorted(want))
        except Exception as exc:  # noqa: BLE001  如实记录后向上抛，由泵决定是否继续
            self._quote_sync_error = f"{type(exc).__name__}: {exc}"
            self._quote_next_attempt_at = now + _QUOTE_RETRY_SECONDS
            raise
        self._applied_quotes = set(want)
        self._quote_sync_error = ""
        self._quote_next_attempt_at = now + _QUOTE_RESYNC_SECONDS
        self._stamp_agent_ok()      # 订阅下发成功也是一次真实往返
        log.info("bigqmt 行情订阅已下发 agent: %d 个标的", len(want))

    def connector_probe(self) -> dict:
        """桥接连接的诊断探针面（供 /brokers/diagnostics 与 health 消费）。

        只读本地缓存（transport.agent_meta 来自最近一次响应信封），**不做 IO** ——
        health 面禁止阻塞。agent 未回报过时诚实给空 dict。
        """
        tp = getattr(self.connector, "transport", None)
        out: dict = {
            "connector_key": self.connector_key,
            "transport": getattr(tp, "transport_id", "") or "",
        }
        # 本连接器**能翻译**的 canonical op 全量清单（由 dialect 自己报，不手抄）：
        # 排障时先看它，再看 agent 的 actions —— 两端数量对不上就是契约漂移。
        ops = getattr(self.connector, "supported_ops", None)
        if callable(ops):
            try:
                out["supported_ops"] = list(ops() or [])
            except Exception:  # noqa: BLE001  诊断面失败不得影响主流程
                pass
        meta = getattr(tp, "agent_meta", None)
        if callable(meta):
            out["agent"] = dict(meta() or {})
        elif isinstance(meta, dict):
            out["agent"] = dict(meta)
        sem = getattr(self.connector, "event_semantics", None)
        if callable(sem):
            try:
                spec = sem()
                out["event_semantics"] = getattr(getattr(spec, "semantics", None), "value", "") or ""
                out["max_latency_ms"] = int(getattr(spec, "max_latency_ms", 0) or 0)
            except Exception:  # noqa: BLE001  诊断面失败不得影响主流程
                pass
        # M2.7 排障四分法的关键字段上浮：路径不一致（agent 回报的 bridge_dir 与
        # 本地比对）、token 错（能收到响应即已排除）、函数缺失（funcs 清单）、
        # agent 未运行（meta 为空）。其余原始细节保留在 out["agent"] 里。
        agent = out.get("agent") or {}
        if agent:
            out["agent_bridge_dir"] = agent.get("bridge_dir", "")
            out["callback_bound"] = bool(agent.get("callback_bound"))
            out["direction_unknown"] = int(agent.get("direction_unknown") or 0)
            out["actions"] = list(agent.get("actions") or [])
            out["subscribed"] = int(agent.get("subscribed") or 0)
            out["ascii_only"] = bool(agent.get("ascii_only"))
        out["clock_offset_ms"] = int(getattr(tp, "clock_offset_ms", 0) or 0)
        out["pump_running"] = self.pump_running()
        # ---- 存活面：让「曾经连上」与「现在可用」在诊断里可区分 ----
        # 光看 connected 会误判（旧 bug 正是如此），因此把两个原始量都上浮：
        #   * agent_unresponsive：探活连续无应答（is_connected 已据此转 False）；
        #   * last_agent_ok_age_s：距上一次**成功往返**多少秒（None = 从未）。
        out["agent_unresponsive"] = bool(self._agent_unresponsive)
        out["liveness_failures"] = int(self._liveness_failures)
        out["last_agent_ok_age_s"] = (
            round(time.monotonic() - self._last_agent_ok, 1)
            if self._last_agent_ok else None)
        out["available"] = bool(self.is_connected())

        # ---- 行情订阅对账面：区分「本端要了什么」「agent 收到了什么」----
        # 这两者不一致 = 界面会停在种子价，而连接一切正常。不给出来就只能靠
        # 人盯盘口猜（``sync`` 的订阅日志永远是成功的）。
        out["quote_wanted"] = len(self._want_quotes)
        out["quote_applied"] = len(self._applied_quotes)
        out["quote_agent_subscribed"] = self._agent_quote_count()
        out["quote_sync_error"] = self._quote_sync_error

        # ---- 四类根因判定：**在服务端算好**，不让用户/前端自己比路径 ----
        # 「bridge_dir 两端不一致」是最高频的一类，而人眼比对两个长路径极易看漏
        # （大小写、正/反斜杠、末尾分隔符、\\\\ 长路径前缀都会造成「看着一样但不同」）。
        # 这里给出 normcase+normpath 后的布尔结论 + 一条可直接照做的引导。
        local_dir = str(getattr(tp, "bridge_dir", "") or "")
        out["local_bridge_dir"] = local_dir
        out["bridge_dir_match"] = self._bridge_dir_match(local_dir, agent)
        out["root_cause"] = self._root_cause(agent, out["bridge_dir_match"])
        return out

    @staticmethod
    def _bridge_dir_match(local_dir: str, agent: dict) -> bool | None:
        """本地 bridge_dir 与 agent 回报值是否同一目录；无法判定时返回 None。

        None 的语义是「**还不知道**」（agent 尚未回报 meta），与 False
        （「确实不一致」）必须区分 —— 把「未知」显示成「不一致」会让用户
        白折腾一次改配置。
        """
        if not agent:
            return None
        remote = str(agent.get("bridge_dir") or "")
        if not local_dir or not remote:
            return None
        try:
            import os
            return os.path.normcase(os.path.normpath(local_dir)) == \
                os.path.normcase(os.path.normpath(remote))
        except Exception:  # noqa: BLE001  路径非法不该让诊断面崩
            return local_dir == remote

    @staticmethod
    def _root_cause(agent: dict, dir_match: bool | None) -> str:
        """把排障四分法压成**一个**可展示的结论串（空串 = 未发现问题）。

        四分法：agent 未运行 / bridge_dir 两端不一致 / 注入函数缺失 / token 不匹配。
        顺序按「先看有没有连上，再看配置对不对」的排查习惯排列。
        """
        if not agent:
            return "agent 未运行或从未应答（桥目录里没有任何响应信封）"
        if dir_match is False:
            return ("bridge_dir 两端不一致：agent 读的是另一个目录，"
                    "请求永远等不到响应。请让 agent_config.json 与连接配置指向同一目录")
        funcs = agent.get("funcs") or []
        if not funcs:
            return ("agent 已就绪但**一个注入函数都没捕获到**："
                    "说明策略运行在错误的位置，或终端版本不提供这些函数")
        if not agent.get("trading_enabled"):
            return ("agent 已连通但 trading_enabled=false：只读可用，"
                    "下单会被 agent 拒绝（确认无误后在 agent_config.json 置 true）")
        return ""

    # ---------------- adapter 面：账户 / 持仓 / 委托 / 成交（async，与网关同形） ----------------
    # ★ 全部 async：调用点统一写 ``await bridge.call(conn.adapter.<m>)``（与
    #   ``sync/__init__.py`` / ``order_watchdog.py`` 既有写法一致）。**禁止**直接从
    #   事件循环同步调用（会拿到未 await 的协程）。
    async def get_account(self) -> dict:
        return await self.gateway.get_account()

    async def get_cash(self) -> dict:
        return await self.gateway.get_cash()

    async def get_positions(self, symbol: str | None = None) -> list[dict]:
        return await self.gateway.get_positions(symbol)

    async def get_orders(self) -> list[dict]:
        return await self.gateway.get_orders()

    async def get_deals(self) -> list[dict]:
        return await self.gateway.get_deals()

    async def get_quote(self, code: str) -> dict:
        return await self.gateway.get_quote(code)

    async def get_kline(self, code: str, period: str, count: int,
                        start: str = "", end: str = "", adjust: str = "") -> list[dict]:
        return await self.gateway.get_kline(code, period, count, start, end, adjust)

    async def get_full_tick(self, codes: list[str]) -> dict:
        return await self.gateway.get_full_tick(codes)

    async def get_tick(self, code: str) -> dict:
        return await self.gateway.get_tick(code)

    async def get_instrument_detail(self, code: str) -> dict:
        return await self.gateway.get_instrument_detail(code)

    async def get_stock_list(self, sector: str = "沪深A股") -> list[dict]:
        return await self.gateway.get_stock_list(sector)

    async def get_sector_list(self) -> list[str]:
        return await self.gateway.get_sector_list()

    async def get_sector_stocks(self, sector: str = "沪深A股") -> list[str]:
        return await self.gateway.get_sector_stocks(sector)

    async def get_trading_calendar(self, start: str = "", end: str = "") -> list[str]:
        return await self.gateway.get_trading_calendar(start, end)

    async def search_stocks(self, keyword: str, limit: int = 20) -> list[dict]:
        return await self.gateway.search_stocks(keyword, limit)

    async def get_financial(self, code: str) -> dict:
        return await self.gateway.get_financial(code)

    async def get_l2_transactions(self, code: str, count: int = 100) -> list[dict]:
        return await self.gateway.get_l2_transactions(code, count)

    async def place_order(self, code: str, direction: str, price_type: str,
                          price: float, volume: int, strategy_name: str = "",
                          remark: str = "", account_type: str = "") -> dict:
        return await self.gateway.place_order(code, direction, price_type, price,
                                              volume, strategy_name, remark,
                                              account_type)

    async def cancel_order(self, order_id: str) -> dict:
        return await self.gateway.cancel_order(order_id)

    async def cancel_order_price(self, order_id: str, deviation: float = 0.01) -> dict:
        return await self.gateway.cancel_order_price(order_id, deviation)

    # ---------------- 适配器回调注册位（大 QMT 真实事件走事件泵，见 on_order 注释） ----------------
    def on_order(self, cb) -> None:
        """注册报单回报回调。

        ★ 大 QMT 的委托事件来源是 **EventPort 轮询差分**（agent 写 events.ndjson →
          事件泵 → ``enqueue({"type": "order", ...})``），不是原生回调。这里保存
          callback 只为接口完整（``sync_engine.register_realtime_trade_handlers``
          要求该方法存在）；真正的派发由 ``ensure_handler("order", ...)`` 承担，
          两者在调用点都会注册，故不会漏事件、也不会重复（enqueue 里按类型派发）。
        """
        if cb is not None:
            self._order_cbs.append(cb)

    def on_trade(self, cb) -> None:
        """注册成交回报回调（理由同 ``on_order``）。"""
        if cb is not None:
            self._trade_cbs.append(cb)

    def on_disconnect(self, cb) -> None:
        """注册断线回调（理由同 ``on_order``）。

        2026-10-03：补齐此方法。此前 ``sync_engine.register_realtime_trade_handlers``
        对 xtquant 适配器调 ``conn.adapter.on_disconnect(...)`` 能生效，但本桥**没有**
        这个方法 —— 而 ``BrokerManager.Connection`` 里大 QMT 连接只有一个对象同时占
        ``bridge`` 与 ``adapter`` 两个槽位，于是大 QMT 侧的断线回调注册会直接
        ``AttributeError``（被 ``register_realtime_trade_handlers`` 的 try/except 吞掉，
        只留一条 warning），大 QMT 连接的掉线事件永远到不了前端。

        真正的派发由 :meth:`enqueue` 承担：见 :meth:`_liveness_probe` 在
        ``_agent_unresponsive`` 由假翻真那一步发出的 ``{"type": "disconnected"}``。
        """
        if cb is not None:
            self._disconnect_cbs.append(cb)

    def subscribe_quote(self, codes: list[str], on_tick) -> None:
        self.gateway.subscribe_quote(codes, on_tick)

    # ---------------- 异步调用适配（在事件循环内被 await 驱动） ----------------
    def _write_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        key = id(loop)
        with self._loop_guard:
            lock = self._write_locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._write_locks[key] = lock
            return lock

    async def call(self, fn, *args, **kwargs):
        """读调用：async 方法直接 await；同步方法 offload 到工作线程。

        ★ 绝不把同步方法丢在事件循环线程上直接执行 —— 文件桥的一次往返可达
          30s，那会让整个后端在这一次查询期间失去响应。
        ★ 成功即刷新存活戳（``_stamp_agent_ok``）：这是存活判据的**主要**证据源，
          于是有业务流量时泵根本不需要额外探活。
        """
        if inspect.iscoroutinefunction(fn):
            out = await fn(*args, **kwargs)
        else:
            loop = asyncio.get_running_loop()
            pool = self._ensure_pool()
            out = await loop.run_in_executor(pool, lambda: fn(*args, **kwargs))
        self._stamp_agent_ok()
        return out

    async def call_locked(self, fn, *args, **kwargs):
        """写调用：串行化 + 不阻塞事件循环。

        ★ 必须用 ``asyncio.Lock``：本桥的写方法是 **async**，用 ``threading.Lock``
          包住 ``await`` 会把事件循环线程阻塞在 acquire 上，而持锁的协程正因为
          循环被阻塞而无法恢复 ⇒ 整个后端永久卡死（详见模块 docstring 第 1 条）。
        """
        async with self._write_lock():
            if inspect.iscoroutinefunction(fn):
                out = await fn(*args, **kwargs)
            else:
                loop = asyncio.get_running_loop()
                pool = self._ensure_pool()
                out = await loop.run_in_executor(pool, lambda: fn(*args, **kwargs))
        self._stamp_agent_ok()
        return out

    # ---------------- WS 事件 ----------------
    def enqueue(self, event: dict) -> None:
        for h in self._handlers.get(event.get("type"), []):
            try:
                h(event)
            except Exception:  # noqa: BLE001  事件派发失败不得影响业务
                pass

    def on(self, event_type: str, handler) -> None:
        self._handlers.setdefault(event_type, []).append(handler)

    def ensure_handler(self, event_type: str, handler) -> None:
        """注册事件 handler（**幂等去重**）。

        2026-10-03 正确性修复：本实现原先直接 ``self.on()``（无条件 append），
        而调用方 ``app.bootstrap.phase_watchdogs._pump_guard`` 每 **2 秒**无条件调用
        一次 → 大 QMT 连接保持活跃期间 ``_handlers["quote"]`` 每天追加 ≈43,200 个
        重复引用，且 ``enqueue`` 遍历全表 ⇒ 一条行情帧被派发 N 次（K 线重复落库、
        下游指标重复计算），进程越跑越慢而**无任何报错**。

        这与同一仓库 ``xtquant_client/gateway.py::ensure_handler`` 的幂等实现语义相同
        （同一调用方、同一语义，此前两个实现行为相反）。此处一并收敛：
        bound method 用 ``==`` 比较（函数 + 实例），故 ``if handler not in hs`` 可靠。
        """
        hs = self._handlers.setdefault(event_type, [])
        if handler not in hs:
            hs.append(handler)


__all__ = ["BigQmtBridge"]

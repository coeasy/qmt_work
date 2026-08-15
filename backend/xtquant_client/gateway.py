"""XTQuant 网关抽象 + 线程模型（§4.14）。

关键约束：
- XTQuant 是同步阻塞调用，且订阅回调来自 QMT 自己的工作线程。
- 因此所有同步调用经 `run_in_executor` 线程池执行，绝不阻塞 asyncio 事件循环；
- 回调线程经线程安全队列投递到事件循环（队列泵）；
- 下单类操作加锁串行化，防竞态/乱序。

`bridge.call(fn, ...)` 是唯一入口：async 侧调用，底层在 worker 线程执行同步函数。
"""
import asyncio
import queue
import threading
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor


class XTQuantGateway(ABC):
    """与 QMT/XTQuant 交互的同步接口（真实实现为 xtquant_client 包装）。"""

    @abstractmethod
    def start(self) -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    @abstractmethod
    def is_connected(self) -> bool: ...

    @abstractmethod
    def get_quote(self, code: str) -> dict: ...

    @abstractmethod
    def get_kline(self, code: str, period: str, count: int) -> list[dict]: ...

    @abstractmethod
    def place_order(self, code: str, direction: str, price_type: str, price: float, volume: int) -> dict: ...

    @abstractmethod
    def cancel_order(self, order_id: str) -> dict: ...

    @abstractmethod
    def query_position(self) -> list[dict]: ...

    @abstractmethod
    def query_cash(self) -> dict: ...

    @abstractmethod
    def subscribe_quote(self, codes: list[str], on_tick) -> None: ...


class XTQuantBridge:
    """线程模型桥：线程池执行同步调用 + 回调队列泵 + 下单锁。

    生命周期幂等：start() 可安全重复调用（先停旧泵再重建，避免重连时泵/子进程堆积），
    stop() 后 start() 可重新拉起（线程池按需重建）。
    """

    def __init__(self, gateway: XTQuantGateway, max_workers: int = 4):
        self.gateway = gateway
        self._max_workers = max_workers
        self._pool: ThreadPoolExecutor | None = ThreadPoolExecutor(max_workers=max_workers)
        # 泵专用线程池（1 线程）：与业务同步调用池解耦。
        # 业务调用（get_quote/get_kline 等）可能长时间挂起（QMT 客户端卡顿），
        # 若与泵共用线程池，池被占满时泵的阻塞取队列也会排队，行情投递随之停更；
        # 独立线程池保证泵永远能及时排空回调队列。
        self._pump_pool: ThreadPoolExecutor | None = ThreadPoolExecutor(max_workers=1)
        self._lock = threading.Lock()
        self._q: queue.Queue[dict] = queue.Queue()
        self._pump: asyncio.Task | None = None
        self._handlers: dict[str, list] = {}
        self._started = False

    # ---- 生命周期（幂等）----
    async def start(self) -> None:
        if self._pool is None:
            self._pool = ThreadPoolExecutor(max_workers=self._max_workers)
        if self._pump_pool is None:
            self._pump_pool = ThreadPoolExecutor(max_workers=1)
        loop = asyncio.get_running_loop()
        # 幂等：网关已启动且仍连接（子进程存活）时不再重复拉起；
        # 一旦断开/子进程退出，is_connected()=False 会在此触发重新 start 自愈。
        if not self._started or not self._gateway_connected():
            await loop.run_in_executor(self._pool, self.gateway.start)
            self._started = True
        self.start_pump_on(loop)

    async def stop(self) -> None:
        if self._pump is not None and not self._pump.done():
            self._pump.cancel()
            try:
                # 泵内队列带 0.5s 超时，取消能在有界时间内生效
                await self._pump
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._pump = None
        if self._pump_pool is not None:
            self._pump_pool.shutdown(wait=False, cancel_futures=True)
            self._pump_pool = None
        loop = asyncio.get_running_loop()
        if self._pool is not None:
            try:
                await loop.run_in_executor(self._pool, self.gateway.close)
            except Exception:  # noqa: BLE001
                pass
            self._pool.shutdown(wait=False, cancel_futures=True)
            self._pool = None
        self._started = False

    def _gateway_connected(self) -> bool:
        try:
            return bool(self.gateway.is_connected())
        except Exception:  # noqa: BLE001
            return False

    def pump_running(self) -> bool:
        return self._pump is not None and not self._pump.done()

    def start_pump_on(self, loop: asyncio.AbstractEventLoop) -> None:
        """在指定（主）事件循环上确保泵已启动（幂等）。

        泵（重新）启动时换新回调队列：上一轮被取消的泵，其 `q.get` 线程可能仍在
        旧队列上阻塞（≤0.5s 超时）；若沿用同一队列，僵尸 waiter 会抢走新泵的首个
        事件（重启丢事件）。换新队列后新泵只在新队列上等待，僵尸线程在旧队列超时退出。
        """
        if self.pump_running():
            return
        if self._pump_pool is None:
            self._pump_pool = ThreadPoolExecutor(max_workers=1)
        self._q = queue.Queue()
        self._pump = loop.create_task(self._pump_loop())

    # ---- 同步调用隔离（§4.14 关键）----
    async def call(self, fn, *args, **kwargs):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._pool, lambda: fn(*args, **kwargs))

    # ---- 下单串行化（§4.14 关键）----
    async def call_locked(self, fn, *args, **kwargs):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._pool, self._locked_call, fn, args, kwargs)

    def _locked_call(self, fn, args, kwargs):
        with self._lock:
            return fn(*args, **kwargs)

    # ---- 回调投递（QMT 工作线程 -> 事件循环）----
    def enqueue(self, event: dict) -> None:
        self._q.put(event)

    def on(self, event_type: str, handler) -> None:
        self._handlers.setdefault(event_type, []).append(handler)

    async def _pump_loop(self) -> None:
        loop = asyncio.get_running_loop()
        # 泵专用线程池快照：泵的阻塞取队列只走该池（与业务池解耦）。
        # stop() 会先取消泵再关池，重启时 start_pump_on 重建新池并新建泵任务，
        # 故此处快照始终对应当前泵的池，不跨生命周期错用。
        pool = self._pump_pool
        while True:
            try:
                # 带超时取队列：线程池关闭 / 泵被取消时能在有界时间内返回。
                # 关键：在泵专用池上取队列——即使业务调用（get_quote/get_kline 等）
                # 长时间挂起占满业务池，泵也能及时排空回调队列，行情不丢更。
                event = await loop.run_in_executor(pool, self._q.get, True, 0.5)
            except queue.Empty:
                continue
            except Exception:  # noqa: BLE001  池已关闭等
                break
            for handler in self._handlers.get(event.get("type", ""), []):
                try:
                    # 兼容 async 与同步 handler
                    res = handler(event)
                    if asyncio.iscoroutine(res):
                        await res
                except Exception as exc:  # 泵内异常不得中断
                    print(f"[sync] handler error: {exc}")

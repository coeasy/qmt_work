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
    """线程模型桥：线程池执行同步调用 + 回调队列泵 + 下单锁。"""

    def __init__(self, gateway: XTQuantGateway, max_workers: int = 4):
        self.gateway = gateway
        self._pool = ThreadPoolExecutor(max_workers=max_workers)
        self._lock = threading.Lock()
        self._q: queue.Queue[dict] = queue.Queue()
        self._pump: asyncio.Task | None = None
        self._handlers: dict[str, list] = {}

    # ---- 生命周期 ----
    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self._pool, self.gateway.start)
        self._pump = asyncio.create_task(self._pump_loop())

    async def stop(self) -> None:
        if self._pump:
            self._pump.cancel()
        await asyncio.get_running_loop().run_in_executor(self._pool, self.gateway.close)
        self._pool.shutdown(wait=False, cancel_futures=True)

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
        while True:
            event = await loop.run_in_executor(self._pool, self._q.get)
            for handler in self._handlers.get(event.get("type", ""), []):
                try:
                    await handler(event)
                except Exception as exc:  # 泵内异常不得中断
                    print(f"[sync] handler error: {exc}")

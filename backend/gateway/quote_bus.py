"""行情共享总线：默认内存模式，可选 Redis 模式跨进程/多实例共享。

内存模式（默认）：
- QuoteBus InMemory：每个 code 维护引用计数，零引用时通知 SyncEngine 退订券商

Redis 模式（QMT_QUOTE_BUS=redis）：
- 跨进程共享：多个 qmt_work 实例订阅同一 Redis channel，行情只向券商订阅一次
- 引用计数存 Redis（key: qmt:quote:ref:<code>），过期自动清理
- 需要 redis-py 或 fakeredis（开发期）

⚠️ **当前消费情况**（R25 核实，避免误判为「总线在分发行情」）：

- ``add_ref`` / ``dec_ref`` **确实在用** —— ``sync/__init__.py`` 据此决定
  何时向券商退订（零引用退订），这是本模块目前唯一的真实功能。
- ``publish`` / ``subscribe`` 这条**事件分发**路径当前**没有订阅者**：
  内存模式下 ``publish`` 遍历空的 ``_subs``（无效果），Redis 模式下发到
  channel 但无人订阅。**行情分发实际走 ``sync/__init__.py`` 的
  ``WSManager.broadcast``**（WS 单一路径），前端不从这里拿行情。

  保留 ``publish`` / ``subscribe`` 是**有意的预留**（多实例共享场景），
  不是漏接。改动行情链路时请以 ``WSManager.broadcast`` 为准，别以为改了
  这里就会影响前端。
"""
from __future__ import annotations

import json
import logging
import threading
from typing import Callable

log = logging.getLogger("qmt_work.quotebus")


class InMemoryQuoteBus:
    """内存引用计数总线（默认，零依赖）。"""

    def __init__(self):
        self._refs: dict[str, int] = {}
        self._subs: dict[str, list[Callable[[dict], None]]] = {}
        self._lock = threading.Lock()

    def add_ref(self, code: str) -> int:
        with self._lock:
            self._refs[code] = self._refs.get(code, 0) + 1
            return self._refs[code]

    def dec_ref(self, code: str) -> int:
        with self._lock:
            n = self._refs.get(code, 0) - 1
            if n <= 0:
                self._refs.pop(code, None)
                self._subs.pop(code, None)
                return 0
            self._refs[code] = n
            return n

    def ref_count(self, code: str) -> int:
        with self._lock:
            return self._refs.get(code, 0)

    def subscribe(self, code: str, handler: Callable[[dict], None]) -> None:
        with self._lock:
            self._subs.setdefault(code, []).append(handler)

    def unsubscribe(self, code: str, handler: Callable[[dict], None]) -> None:
        with self._lock:
            lst = self._subs.get(code)
            if lst and handler in lst:
                lst.remove(handler)

    def publish(self, code: str, data: dict) -> None:
        with self._lock:
            handlers = list(self._subs.get(code, []))
        for h in handlers:
            try:
                h(data)
            except Exception as exc:  # noqa: BLE001
                log.debug("inmem bus handler error: %s", exc)

    def stats(self) -> dict:
        with self._lock:
            return {"mode": "memory", "codes": len(self._refs),
                    "refs": dict(self._refs), "subscribers": {k: len(v) for k, v in self._subs.items()}}


class RedisQuoteBus:
    """Redis 引用计数 + pub/sub 总线（可选，跨进程共享行情）。"""

    def __init__(self, redis_url: str = "redis://127.0.0.1:6379/0",
                 channel: str = "qmt:quote", prefix: str = "qmt:quote:ref:"):
        self._url = redis_url
        self._channel = channel
        self._prefix = prefix
        self._redis = None
        self._pubsub = None
        self._listeners: dict[str, list[Callable[[dict], None]]] = {}
        self._lock = threading.Lock()
        self._listener_thread = None
        self._connected = False

    def connect(self) -> bool:
        try:
            import redis
            self._redis = redis.from_url(self._url, decode_responses=True)
            self._redis.ping()
            self._connected = True
            log.info("redis quote bus connected: %s", self._url)
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("redis quote bus connect failed: %s (fallback to memory)", exc)
            self._connected = False
            return False

    def add_ref(self, code: str) -> int:
        if not self._connected:
            return 0
        key = self._prefix + code
        n = self._redis.incr(key)
        self._redis.expire(key, 3600)
        return int(n)

    def dec_ref(self, code: str) -> int:
        if not self._connected:
            return 0
        key = self._prefix + code
        n = self._redis.decr(key)
        if n <= 0:
            self._redis.delete(key)
            return 0
        return int(n)

    def ref_count(self, code: str) -> int:
        if not self._connected:
            return 0
        v = self._redis.get(self._prefix + code)
        return int(v or 0)

    def subscribe(self, code: str, handler: Callable[[dict], None]) -> None:
        with self._lock:
            self._listeners.setdefault(code, []).append(handler)
        self._ensure_listener()

    def unsubscribe(self, code: str, handler: Callable[[dict], None]) -> None:
        with self._lock:
            lst = self._listeners.get(code)
            if lst and handler in lst:
                lst.remove(handler)

    def publish(self, code: str, data: dict) -> None:
        if not self._connected:
            return
        try:
            self._redis.publish(self._channel, json.dumps({"code": code, "data": data},
                                                          ensure_ascii=False, default=str))
        except Exception as exc:  # noqa: BLE001
            log.debug("redis publish failed: %s", exc)

    def _ensure_listener(self):
        if self._listener_thread or not self._connected:
            return
        import threading

        def _run():
            try:
                self._pubsub = self._redis.pubsub()
                self._pubsub.subscribe(self._channel)
                for msg in self._pubsub.listen():
                    if msg.get("type") != "message":
                        continue
                    try:
                        payload = json.loads(msg.get("data", ""))
                        code = payload.get("code")
                        data = payload.get("data", {})
                        with self._lock:
                            handlers = list(self._listeners.get(code, []))
                        for h in handlers:
                            try:
                                h(data)
                            except Exception:  # noqa: BLE001
                                pass
                    except Exception:  # noqa: BLE001
                        continue
            except Exception as exc:  # noqa: BLE001
                log.warning("redis listener stopped: %s", exc)

        self._listener_thread = threading.Thread(target=_run, daemon=True, name="quotebus-listen")
        self._listener_thread.start()

    def stats(self) -> dict:
        return {"mode": "redis", "connected": self._connected, "url": self._url,
                "channel": self._channel}


def create_quote_bus(redis_url: str = "", enabled: bool = False) -> InMemoryQuoteBus | RedisQuoteBus:
    """工厂：enabled=True 且 redis 可达时用 Redis，否则内存。"""
    if enabled and redis_url:
        bus = RedisQuoteBus(redis_url)
        if bus.connect():
            return bus
        log.warning("redis unavailable, fallback to in-memory quote bus")
    return InMemoryQuoteBus()

"""事件回调派发的**唯一实现**（同步/异步回调统一入口）。

## 为什么需要它

引擎的 `on_event` 回调是**同步**调用的（`self._on_event(event)`），但接线时传进来的
是 `ws_manager.broadcast` —— 一个 `async def`：

```python
state.signal_router = SignalRouter(..., on_event=state.ws_manager.broadcast)
...
def _emit(self, event):
    self._on_event(event)        # ← 只创建了协程对象，永不 await
```

后果有两层，且**都不报错**：

1. 事件**静默丢失** —— 下单/成交/风控/对账事件永远推不到前端（前端只能靠轮询）；
2. 每次 GC 冒一条 `RuntimeWarning: coroutine 'WSManager.broadcast' was never awaited`。

实测（2026-09-17）：`POST /trade/order` 后端日志出现该 RuntimeWarning，
`qmt_work.signal` 的事件从未到达 WS 客户端。

`gateway/health.py` 曾单独修过这一处（注释：「原实现未 await 且把 dict 当 event_type
传，导致 broker.connected/disconnected 事件从未真正推送到前端」），但
`signal_router` / `reconcile` / `alert_engine` 三处仍是坏的 —— 同一缺陷散落多份手写
补丁，正是「唯一实现」纪律要消除的形态。

## 语义

- 回调返回**非协程** → 已同步完成，原样返回；
- 回调返回**协程** → 有运行中的事件循环则 `create_task` 调度（fire-and-forget）；
  没有循环（同步单测 / 脚本直调）则**显式 close()** —— 否则又是一条 never-awaited 警告；
- 回调抛异常 / 参数不匹配 → 只记 warning，**绝不向上抛**（事件推送失败不得影响业务）。
"""
from __future__ import annotations

import asyncio
import inspect
import logging

log = logging.getLogger("qmt_work.emit")


def emit_event(cb, *args, **kwargs):
    """调用事件回调，自动兼容同步/异步两种实现。返回值仅用于测试断言。"""
    if cb is None:
        return None
    try:
        res = cb(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001  事件推送失败不得影响业务主流程
        log.warning("事件回调 %r 调用失败（已忽略）：%s",
                    getattr(cb, "__qualname__", cb), exc)
        return None
    if not inspect.iscoroutine(res):
        return res
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        res.close()  # 无事件循环：关掉协程，避免 "coroutine was never awaited"
        return None
    try:
        return loop.create_task(res)
    except Exception as exc:  # noqa: BLE001  循环正在关闭等极端情况
        log.warning("事件回调 %r 调度失败（已忽略）：%s",
                    getattr(cb, "__qualname__", cb), exc)
        res.close()
        return None


__all__ = ["emit_event"]

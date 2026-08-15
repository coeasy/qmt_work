"""XTQuantBridge 线程模型桥生命周期测试：泵/线程池幂等启停 + 事件投递。

覆盖修复：健康重连 / 多次 start() 时不应堆积泵或线程池、不应重复拉起 gateway；
stop() 后可干净重启（池重建、泵重建、事件恢复投递）。
"""
import asyncio

from xtquant_client.gateway import XTQuantGateway, XTQuantBridge


class _G(XTQuantGateway):
    """最小内存 gateway（无子进程），仅统计 start/close 次数。"""

    def __init__(self):
        self.started = 0
        self.closed = 0

    def start(self):
        self.started += 1

    def close(self):
        self.closed += 1

    def is_connected(self):
        return True

    def get_quote(self, code):
        return {"code": code, "last": 1.0}

    def get_kline(self, code, period, count):
        return []

    def place_order(self, *a, **k):
        return {"order_id": "X"}

    def cancel_order(self, order_id):
        return {"status": "ok"}

    def query_position(self):
        return []

    def query_cash(self):
        return {}

    def subscribe_quote(self, codes, on_tick):
        pass


async def _wait_for(pred, timeout: float = 3.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if pred():
            return True
        await asyncio.sleep(0.02)
    return False


def test_bridge_pump_lifecycle_idempotent_start_stop():
    g = _G()
    b = XTQuantBridge(g, max_workers=2)
    got = []

    async def run():
        await b.start()
        assert b.pump_running()
        b.on("quote", lambda e: got.append(e))
        b.enqueue({"type": "quote", "data": 1})
        assert await _wait_for(lambda: bool(got)), "首次启动泵未投递事件"
        assert g.started == 1

        # 幂等：重复 start 不重复拉起 gateway、不重复建泵
        await b.start()
        await b.start()
        assert g.started == 1
        assert b.pump_running()

        # stop：泵取消、gateway 关闭、线程池释放
        await b.stop()
        assert not b.pump_running()
        assert g.closed == 1

        # 重启：池重建、泵重建、事件恢复投递
        await b.start()
        assert g.started == 2
        assert b.pump_running()
        b.enqueue({"type": "quote", "data": 2})
        assert await _wait_for(lambda: len(got) >= 2), "重启后泵未恢复事件投递"
        await b.stop()
        assert not b.pump_running()

    asyncio.run(run())

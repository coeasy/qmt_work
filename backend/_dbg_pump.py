import asyncio
import sys
sys.path.insert(0, ".")
from xtquant_client.gateway import XTQuantGateway, XTQuantBridge


class _G(XTQuantGateway):
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


async def _wait_for(pred, timeout=3.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if pred():
            return True
        await asyncio.sleep(0.02)
    return False


async def run():
    g = _G()
    b = XTQuantBridge(g, max_workers=2)
    got = []
    await b.start()
    b.on("quote", lambda e: got.append(e))
    b.enqueue({"type": "quote", "data": 1})
    print("first:", await _wait_for(lambda: bool(got)), flush=True)

    await b.stop()
    print("after stop: pump_task.done =", b._pump, flush=True)

    await b.start()
    t = b._pump
    print("after restart: pump_task =", t, "done =", t.done(), flush=True)
    b.enqueue({"type": "quote", "data": 2})
    ok = await _wait_for(lambda: len(got) >= 2)
    print("second:", ok, "got:", got, flush=True)
    print("pump task exception:", t.exception(), flush=True)
    print("queue qsize:", b._q.qsize(), flush=True)
    await b.stop()


asyncio.run(run())

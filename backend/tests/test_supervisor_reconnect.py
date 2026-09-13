"""V9 Phase 6 DoD：Supervisor 健康探测 + 自动重连 + 人工停机尊重。"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from connectors.ports import ConnectorDescriptor, ConnectorState  # noqa: E402
from connectors.supervisor import ConnectionSupervisor  # noqa: E402


class FakeConnector:
    def __init__(self, fail_times: int = 0):
        self.descriptor = ConnectorDescriptor(id="fake", name="fake", version="1")
        self.started = 0
        self.closed = 0
        self.alive = True
        self.fail_times = fail_times   # 前 N 次 start 失败，之后成功

    async def start(self):
        self.started += 1
        if self.started <= self.fail_times:
            raise RuntimeError("sdk down")
        self.alive = True

    async def close(self):
        self.closed += 1
        self.alive = False

    def is_connected(self):
        return self.alive


def _run(coro):
    return asyncio.run(coro)


def test_start_failure_marks_failed():
    async def _run():
        sup = ConnectionSupervisor()
        c = FakeConnector(fail_times=1)
        sup.register("a", c)
        st = await sup.start("a")
        assert st.state == ConnectorState.FAILED
        assert st.last_error
        st2 = await sup.start("a")   # 重试成功
        assert st2.state == ConnectorState.CONNECTED
        assert c.started == 2

    asyncio.run(_run())


def test_stop_disables_auto_reconnect():
    async def _run():
        sup = ConnectionSupervisor(max_backoff=0.05)
        c = FakeConnector()
        sup.register("a", c)
        await sup.start("a")
        await sup.stop("a")
        assert sup.status("a").state == ConnectorState.DISCONNECTED
        # 人工停机后健康探测不得拉起（DISCONNECTED 被跳过）
        await sup._health_check()
        assert c.started == 1

    asyncio.run(_run())


def test_health_check_reconnects_lost_connection():
    async def _run():
        sup = ConnectionSupervisor(max_backoff=0.01)
        c = FakeConnector()
        sup.register("a", c)
        await sup.start("a")
        c.alive = False              # 模拟掉线（非人工 stop）
        sup.status("a").state = ConnectorState.CONNECTED
        await sup._health_check()    # 探测 → 掉线 → 自动重连
        assert c.started == 2
        assert sup.status("a").state == ConnectorState.CONNECTED

    asyncio.run(_run())


def test_one_connector_failure_isolated():
    async def _run():
        sup = ConnectionSupervisor()
        bad = FakeConnector(fail_times=99)
        good = FakeConnector()
        sup.register("bad", bad)
        sup.register("good", good)
        results = await sup.start_all()
        by_id = {r.connector_id: r for r in results}
        assert by_id["bad"].state == ConnectorState.FAILED
        assert by_id["good"].state == ConnectorState.CONNECTED

    asyncio.run(_run())

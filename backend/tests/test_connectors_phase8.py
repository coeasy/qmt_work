import asyncio

import pytest

from connectors.ports import ConnectorDescriptor, ConnectorState, InstrumentId, OrderRequest
from connectors.supervisor import ConnectionSupervisor


class _Connector:
    def __init__(self, connected=True, fail=False):
        self.descriptor = ConnectorDescriptor("test", "test", "1")
        self.connected = connected
        self.fail = fail
        self.started = 0

    async def start(self):
        self.started += 1
        if self.fail:
            raise RuntimeError("SDK unavailable")
        self.connected = True

    async def close(self):
        self.connected = False

    def is_connected(self):
        return self.connected


def test_order_request_rejects_invalid_real_command():
    request = OrderRequest(InstrumentId("600000", "SH"), "buy", quantity=0)
    with pytest.raises(ValueError):
        request.validate()


def test_supervisor_isolates_failed_connector():
    supervisor = ConnectionSupervisor()
    good = _Connector(connected=False)
    bad = _Connector(connected=False, fail=True)
    supervisor.register("good", good)
    supervisor.register("bad", bad)
    statuses = asyncio.run(supervisor.start_all())
    assert [s.state for s in statuses] == [ConnectorState.CONNECTED, ConnectorState.FAILED]
    assert supervisor.status("bad").last_error == "SDK unavailable"

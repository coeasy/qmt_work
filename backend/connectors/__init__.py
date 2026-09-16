"""Platform-neutral connector ports and lifecycle supervision (V9 Phase 8).

⚠️ **当前未接线（NOT WIRED）** —— 详见 ``docs/2026-09-15_项目全景梳理与V11重构方案.md``
§7.2 附。本包是 V9 Phase 8 设计的「连接器端口层」，**产线尚未接入**：

* 全仓**零产线引用**（`app/` `core/` `datasource/` `engines/` `gateway/` `tools/`
  `main.py` `run.py` 均不 import 本包）；
* 仅 3 个测试文件引用：`tests/test_connector_ports.py`、`test_connectors_phase8.py`、
  `test_supervisor_reconnect.py`；
* `build_exe.py` 只在 PyInstaller `hiddenimports` 里列出，纯粹为「打包不丢文件」，
  **不代表运行时使用**。

实际在用的券商客户端是 ``xtquant_client/``（22 py）；本包的 ``qmt.py`` 只是**包在它
之上的适配层**（``from xtquant_client.base import BrokerAdapter``），**不构成重复实现**。
V9 审计（``docs/2026-09-09_V9总纲实施核对审计报告.md:50``）已记录
``ConnectorPort.place_order`` 未被 ``ExecutionService`` 接线 ——「端口形同虚设」。

**保留而非删除**：这是「已设计、待接线」的扩展点（多券商接入），删掉会丢失设计资产；
但请知悉 **改本包不会影响任何产线行为**。若要让其生效，需先按 §8.6 排期立项接线。
"""

from .http import HttpTradingConnector
from .ports import (
    AccountSnapshot,
    ConnectorDescriptor,
    ConnectorError,
    ConnectorPort,
    ConnectorState,
    InstrumentId,
    OrderRequest,
    PositionSnapshot,
)
from .qmt import QmtConnector
from .supervisor import ConnectionSupervisor

__all__ = [
    "AccountSnapshot", "ConnectorDescriptor", "ConnectorError", "ConnectorPort",
    "ConnectorState", "ConnectionSupervisor", "InstrumentId", "OrderRequest",
    "PositionSnapshot", "QmtConnector", "HttpTradingConnector",
]

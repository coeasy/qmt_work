"""Permissioned plugin kernel (V9 Phase 9).

Plugins extend declared ports and capabilities; they do not receive a raw DB,
broker adapter, process environment or secret store.

⚠️ **当前未接线（NOT WIRED）—— 声明式骨架，非疏漏** —— 详见
``docs/2026-09-15_项目全景梳理与V11重构方案.md`` §7.2 附：

* V9 审计（``docs/2026-09-09_V9总纲实施核对审计报告.md:79``）：`kernel.py`
  （install/activate/list）、`manifest.py`（禁 core.replace / db.raw / broker.raw /
  secrets.raw）、`permissions.py`（SecretProxy）**齐全，但无加载运行时、无进程隔离，
  业务代码零调用（仅测试引用）**；
* V9 决策 D6（``docs/2026-09-10_项目功能架构实现全景审计与V9全量重构实施方案.md:32``）：
  **插件平台「本期不做」→ Backlog B，保留目录但标注 roadmap** —— 本包即该 backlog 项；
* 全仓仅 ``tests/test_plugin_kernel.py`` 引用，`app/` `core/` 等零引用；前端无插件页，
  README 亦未承诺插件能力（仅目录树中性列出）。

**注意**：`datasource/registry.py` 里的 ``self._plugins`` 是**字典属性名**，与本包无关，
不要误判为「插件已被使用」。**改本包不会影响任何产线行为。**
"""

from .kernel import PluginManager
from .manifest import PluginManifest, PluginManifestError
from .permissions import PluginPermissionError, PluginPermissionPolicy, SecretProxy

__all__ = [
    "PluginManager", "PluginManifest", "PluginManifestError",
    "PluginPermissionError", "PluginPermissionPolicy", "SecretProxy",
]

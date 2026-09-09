"""Plugin catalog and activation kernel.

Activation is intentionally declarative in this phase. The kernel validates a
manifest and permissions; execution is delegated to a future subprocess/RPC host,
so an extension can never be handed the application's DB or broker object.
"""
from __future__ import annotations

from dataclasses import dataclass

from .manifest import PluginManifest
from .permissions import PluginPermissionPolicy


@dataclass
class PluginRecord:
    manifest: PluginManifest
    state: str = "installed"


class PluginManager:
    def __init__(self, policy: PluginPermissionPolicy | None = None):
        self.policy = policy or PluginPermissionPolicy()
        self._records: dict[str, PluginRecord] = {}

    def install(self, manifest: PluginManifest | dict) -> PluginRecord:
        if isinstance(manifest, dict):
            manifest = PluginManifest.from_dict(manifest)
        manifest.validate()
        self.policy.check(manifest.permissions)
        if manifest.id in self._records:
            raise ValueError(f"plugin already installed: {manifest.id}")
        record = PluginRecord(manifest)
        self._records[manifest.id] = record
        return record

    def activate(self, plugin_id: str) -> PluginRecord:
        record = self._get(plugin_id)
        record.state = "active"
        return record

    def deactivate(self, plugin_id: str) -> PluginRecord:
        record = self._get(plugin_id)
        record.state = "installed"
        return record

    def remove(self, plugin_id: str) -> None:
        record = self._get(plugin_id)
        if record.state == "active":
            raise ValueError("deactivate plugin before removal")
        del self._records[plugin_id]

    def list(self) -> list[PluginRecord]:
        return list(self._records.values())

    def _get(self, plugin_id: str) -> PluginRecord:
        try:
            return self._records[plugin_id]
        except KeyError as exc:
            raise KeyError(f"unknown plugin: {plugin_id}") from exc


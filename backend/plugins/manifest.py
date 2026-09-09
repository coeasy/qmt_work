"""Signed/declarative plugin manifest validation."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_PLUGIN_ID = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")
_RUNTIMES = {"subprocess", "rpc"}
_FORBIDDEN_CAPABILITIES = {"core.replace", "db.raw", "broker.raw", "secrets.raw"}


class PluginManifestError(ValueError):
    """Manifest is unsafe or incomplete."""


@dataclass(frozen=True)
class PluginManifest:
    id: str
    version: str
    name: str
    entrypoint: str
    runtime: str = "subprocess"
    capabilities: tuple[str, ...] = ()
    permissions: tuple[str, ...] = ()
    secret_scopes: tuple[str, ...] = ()
    api_version: str = "v1"
    checksum: str = ""
    signature: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "PluginManifest":
        values = dict(payload)
        for key in ("capabilities", "permissions", "secret_scopes"):
            values[key] = tuple(str(v) for v in values.get(key, ()))
        manifest = cls(**values)
        manifest.validate()
        return manifest

    def validate(self) -> None:
        if not _PLUGIN_ID.fullmatch(self.id):
            raise PluginManifestError("plugin id must be a lowercase stable identifier")
        if not self.version or not self.name or not self.entrypoint:
            raise PluginManifestError("plugin version/name/entrypoint are required")
        if self.runtime not in _RUNTIMES:
            raise PluginManifestError(f"unsupported plugin runtime: {self.runtime}")
        forbidden = _FORBIDDEN_CAPABILITIES.intersection(self.capabilities)
        if forbidden:
            raise PluginManifestError(f"forbidden plugin capabilities: {sorted(forbidden)}")
        if self.id in {"core", "platform", "execution"}:
            raise PluginManifestError("reserved core plugin id")
        if self.entrypoint.startswith(("core.", "app.", "gateway.")):
            raise PluginManifestError("plugin entrypoint cannot replace trusted core modules")


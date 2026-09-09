"""Permissioned plugin kernel (V9 Phase 9).

Plugins extend declared ports and capabilities; they do not receive a raw DB,
broker adapter, process environment or secret store.
"""

from .kernel import PluginManager
from .manifest import PluginManifest, PluginManifestError
from .permissions import PluginPermissionError, PluginPermissionPolicy, SecretProxy

__all__ = [
    "PluginManager", "PluginManifest", "PluginManifestError",
    "PluginPermissionError", "PluginPermissionPolicy", "SecretProxy",
]

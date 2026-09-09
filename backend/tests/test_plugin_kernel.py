import pytest

from plugins import PluginManager, PluginManifest, PluginManifestError
from plugins.permissions import PluginPermissionError, PluginPermissionPolicy, SecretProxy


def test_plugin_manifest_rejects_core_replacement():
    with pytest.raises(PluginManifestError):
        PluginManifest.from_dict({
            "id": "research-ext", "version": "1", "name": "x",
            "entrypoint": "gateway.execution.run",
        })


def test_plugin_permissions_and_secret_scope_are_explicit():
    manager = PluginManager(PluginPermissionPolicy({"market.read"}))
    with pytest.raises(PluginPermissionError):
        manager.install({
            "id": "research-ext", "version": "1", "name": "x",
            "entrypoint": "extension.main", "permissions": ["trade.execute"],
        })
    proxy = SecretProxy({"provider.demo": "token"}, {"provider.demo"})
    assert proxy.get("provider.demo") == "token"
    with pytest.raises(PluginPermissionError):
        proxy.get("broker.live")


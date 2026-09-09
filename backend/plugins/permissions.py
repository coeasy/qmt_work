"""Capability and secret-scope enforcement for plugin boundaries."""
from __future__ import annotations


class PluginPermissionError(PermissionError):
    pass


class PluginPermissionPolicy:
    def __init__(self, allowed: set[str] | None = None):
        self.allowed = set(allowed or ())

    def check(self, requested: tuple[str, ...] | list[str]) -> None:
        denied = set(requested) - self.allowed
        if denied:
            raise PluginPermissionError(f"plugin permissions denied: {sorted(denied)}")


class SecretProxy:
    """Scoped secret reader; the plugin never receives the raw secret store."""

    def __init__(self, values: dict[str, str], allowed_scopes: set[str]):
        self._values = dict(values)
        self._allowed = set(allowed_scopes)

    def get(self, scope: str) -> str:
        if scope not in self._allowed:
            raise PluginPermissionError(f"secret scope denied: {scope}")
        if scope not in self._values:
            raise KeyError(scope)
        return self._values[scope]

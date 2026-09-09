"""Provider capability catalog and strict resolution metadata.

The catalog is not a data generator: a provider is ``active`` only when a real
DataSource implementation is registered. Optional SDK/public HTTP entries remain
visible with an explicit status so UI and schedulers cannot silently fall back.
"""
from __future__ import annotations

import importlib.util
from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderDescriptor:
    id: str
    name: str
    transport: str
    capabilities: tuple[str, ...]
    optional_dependency: str = ""
    license_note: str = ""


PROVIDER_CATALOG = (
    ProviderDescriptor("tstdx", "TDX standard", "pytdx", ("quote", "kline", "stock_list"), "pytdx"),
    ProviderDescriptor("eltdx", "ELTDX public market", "eltdx", ("quote", "kline", "instrument_detail", "stock_list"), "eltdx", "research-only"),
    ProviderDescriptor("baostock", "BaoStock", "baostock", ("kline", "stock_list", "instrument_detail"), "baostock"),
    ProviderDescriptor("sina", "Sina public quote", "http", ("quote",), license_note="public endpoint; availability is runtime checked"),
    ProviderDescriptor("tencent", "Tencent public quote", "http", ("quote",), license_note="public endpoint; availability is runtime checked"),
)


class ProviderCatalog:
    def __init__(self, descriptors=PROVIDER_CATALOG):
        self._descriptors = {item.id: item for item in descriptors}
        self._active: dict[str, dict] = {}

    def register(self, provider, *, descriptor: ProviderDescriptor | None = None) -> None:
        provider_id = getattr(provider, "name", "")
        if not provider_id:
            raise ValueError("provider must declare a stable name")
        if descriptor is not None and descriptor.id != provider_id:
            raise ValueError("provider descriptor id does not match implementation")
        self._active[provider_id] = (
            provider.capability_manifest() if hasattr(provider, "capability_manifest")
            else {"provider": provider_id, "capabilities": []})

    def describe(self) -> list[dict]:
        out = []
        for item in self._descriptors.values():
            active = self._active.get(item.id)
            dependency_available = (not item.optional_dependency or
                                    importlib.util.find_spec(item.optional_dependency) is not None)
            out.append({
                "provider": item.id, "name": item.name, "transport": item.transport,
                "capabilities": list(active.get("capabilities", item.capabilities) if active else item.capabilities),
                "active": active is not None,
                "dependency_available": dependency_available,
                "status": "active" if active else ("dependency-ready" if dependency_available else "unavailable"),
                "optional_dependency": item.optional_dependency,
                "license_note": item.license_note,
            })
        return out

    def require_active(self, provider_id: str) -> dict:
        if provider_id not in self._active:
            raise LookupError(f"provider is not registered and active: {provider_id}")
        return self._active[provider_id]


provider_catalog = ProviderCatalog()

__all__ = ["ProviderCatalog", "ProviderDescriptor", "PROVIDER_CATALOG", "provider_catalog"]

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
    commercial_ok: bool = True
    requires: str = ""


# 默认能力链（v1.3 锁定契约）：QMT（链首恒为 broker） → eltdx → baostock → akshare，
# 其余（pytdx / tencent / sina）仅用于显式指定或个别能力的末位兜底。改此顺序必须同步
# 改文档 + 测试（test_default_chain_order.py），三处一致才允许合并。
DEFAULT_CAPABILITY_CHAINS: dict[str, tuple[str, ...]] = {
    "kline": ("broker", "eltdx", "baostock", "akshare", "pytdx", "tencent", "sina"),
    "kline_qfq": ("broker", "eltdx", "baostock", "akshare", "tencent"),
    "kline_hfq": ("broker", "eltdx", "baostock", "akshare", "tencent"),
    "stock_list": ("broker", "eltdx", "baostock", "akshare", "pytdx"),
    "instrument_detail": ("broker", "eltdx", "baostock", "akshare", "pytdx"),
    "sector": ("broker", "eltdx", "akshare", "baostock"),
    "index_constituent": ("broker", "eltdx", "akshare", "baostock"),
    "fundamental": ("broker", "eltdx", "baostock", "akshare"),
    "capital": ("broker", "eltdx", "baostock", "akshare"),
    "suspend": ("broker", "eltdx", "baostock", "akshare"),
    "price_limit": ("broker", "eltdx", "akshare", "baostock"),
    "corporate_action": ("broker", "eltdx", "baostock", "akshare", "pytdx"),
    "calendar": ("broker", "eltdx", "baostock", "akshare", "local"),
    "quote": ("broker", "eltdx", "tencent", "sina", "akshare"),
    "moneyflow": ("broker", "eltdx", "akshare"),
}


PROVIDER_CATALOG = (
    ProviderDescriptor("broker", "QMT Broker", "xtquant", (
        "quote", "kline", "kline_qfq", "kline_hfq", "stock_list", "instrument_detail",
        "fundamental", "capital", "suspend", "price_limit", "index_constituent",
        "corporate_action", "calendar", "moneyflow"),
        commercial_ok=True, license_note="券商授权终端，授权即合规"),
    ProviderDescriptor("eltdx", "ELTDX public market", "eltdx", (
        "quote", "kline", "kline_qfq", "kline_hfq", "instrument_detail", "stock_list",
        "sector", "index_constituent", "capital", "suspend", "price_limit", "moneyflow",
        "calendar"), "eltdx", "ELTDX Research-Only（禁止商用）", commercial_ok=False),
    ProviderDescriptor("baostock", "BaoStock", "baostock", (
        "kline", "kline_qfq", "kline_hfq", "stock_list", "instrument_detail",
        "fundamental", "suspend", "index_constituent", "dividend", "calendar"),
        "baostock", "BSD-3-Clause", commercial_ok=True),
    ProviderDescriptor("akshare", "Akshare", "http", (
        "quote", "kline", "kline_qfq", "kline_hfq", "stock_list", "sector",
        "index_constituent", "fundamental", "capital", "suspend", "moneyflow",
        "calendar", "dividend"), "akshare", "MIT", commercial_ok=True),
    ProviderDescriptor("tstdx", "TDX standard", "pytdx", ("quote", "kline", "stock_list"),
        "pytdx", "MIT", commercial_ok=True),
    ProviderDescriptor("sina", "Sina public quote", "http", ("quote",),
        license_note="public endpoint; availability is runtime checked", commercial_ok=True),
    ProviderDescriptor("tencent", "Tencent public quote", "http", ("quote", "kline_qfq"),
        license_note="public endpoint; availability is runtime checked", commercial_ok=True),
)


class ProviderCatalog:
    def __init__(self, descriptors=PROVIDER_CATALOG):
        self._descriptors = {item.id: item for item in descriptors}
        self._active: dict[str, dict] = {}
        self._overrides: dict[str, tuple[str, ...]] = {}

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
                "commercial_ok": item.commercial_ok,
                "requires": item.optional_dependency,
                "status": "active" if active else ("dependency-ready" if dependency_available else "unavailable"),
                "optional_dependency": item.optional_dependency,
                "license_note": item.license_note,
            })
        return out

    def is_commercial_ok(self, provider_id: str) -> bool:
        item = self._descriptors.get(provider_id)
        return item.commercial_ok if item else True

    def default_chain(self, capability: str) -> list[str]:
        """返回某能力默认降级链（v1.3 契约顺序，未注册过滤前）。

        若存在经 ``set_override`` 设定的运行时覆盖，则优先返回覆盖链（仍受注册/依赖/
        许可证过滤约束）。默认契约链本身不可被修改（改顺序须三处同步）。
        """
        if capability in self._overrides:
            return list(self._overrides[capability])
        chain = DEFAULT_CAPABILITY_CHAINS.get(capability)
        if chain is None:
            chain = DEFAULT_CAPABILITY_CHAINS["kline"]
        return list(chain)

    def set_override(self, capability: str, chain: list[str]) -> None:
        """设定某能力的运行时降级链覆盖（内存态，不持久化、不修改契约默认链）。

        仅接受已知 provider id；未知 id 立即抛 ValueError（不静默丢弃，便于调用方定位）。
        """
        if capability not in DEFAULT_CAPABILITY_CHAINS:
            raise ValueError(f"未知能力：{capability}（可选 {sorted(DEFAULT_CAPABILITY_CHAINS)}）")
        chain = [str(c).strip() for c in (chain or [])]
        for pid in chain:
            if pid not in self._descriptors:
                raise ValueError(f"链含未知 provider：{pid}（可选 {sorted(self._descriptors)}）")
        self._overrides[capability] = tuple(chain)

    def clear_override(self, capability: str | None = None) -> None:
        """清除覆盖；capability 为 None 时清除全部。"""
        if capability is None:
            self._overrides.clear()
        else:
            self._overrides.pop(capability, None)

    def resolve_chain(self, capability: str, *,
                      commercial_mode: bool = False,
                      registered: set[str] | None = None) -> list[str]:
        """按能力求值降级链（D-J §J.2 求值顺序 ①→④）。

        - ① 默认链；② 过滤未注册源；③ 过滤依赖缺失源；
          ④ 商用模式跳过 ``commercial_ok=False`` 的源（如 eltdx Research-Only）。
        - 许可证过滤是「跳过」而非「报错」，业务代码无需写例外分支（v1.3 关键修正）。
        """
        chain = self.default_chain(capability)
        out: list[str] = []
        for pid in chain:
            desc = self._descriptors.get(pid)
            if desc is None:
                continue
            # 注册态过滤：broker 由连接态决定；补充源必须在 registered 中才算可用
            if pid == "broker":
                if registered is not None and "broker" not in registered:
                    continue
            elif registered is not None and pid not in registered:
                continue
            # 依赖可用性过滤（可选依赖未安装则跳过，保留目录条目但链路跳过）
            if desc.optional_dependency and importlib.util.find_spec(desc.optional_dependency) is None:
                continue
            # 商用模式：跳过 Research-Only 等禁止商用的源（静默跳过，不报错）
            if commercial_mode and not desc.commercial_ok:
                continue
            out.append(pid)
        return out

    def require_active(self, provider_id: str) -> dict:
        if provider_id not in self._active:
            raise LookupError(f"provider is not registered and active: {provider_id}")
        return self._active[provider_id]


provider_catalog = ProviderCatalog()

__all__ = ["ProviderCatalog", "ProviderDescriptor", "PROVIDER_CATALOG", "provider_catalog"]

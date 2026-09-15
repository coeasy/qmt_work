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
# 其余（tencent / sina）仅用于显式指定或个别能力的末位兜底。改此顺序必须同步
# 改文档 + 测试（test_default_chain_order.py），三处一致才允许合并。
#
# ★ 2026-09-13 移除 "pytdx" 链项与 tstdx provider（方案 §6.1）：
#   该源注册名为 "tstdx"（optional_sources.TstdxSource.name），而链里写的是
#   "pytdx"，标识错配导致**它从来没有被真正选中过**——移除不损失任何实际能力，
#   只是消除无效冗余与「看起来还有一层兜底」的误导。
#
# ★ 2026-09-15（V11 R6）**链成员按「实现类自述的能力」逐条对账**（见 §8.12）：
#   此前链成员是「期望」写下的，与实现类 `capabilities` 双向不一致 ——
#   既存在「声明了却不在链里」（sina/tencent 的 instrument_detail 永远轮不到），
#   也存在「在链里却没声明/没实现」（baostock 在 sector/capital/price_limit 链里
#   但从未实现这些方法，只能靠 `hasattr` 事后跳过、白白计入熔断）。
#   逐源核对**真实方法集**后的修正：
#   - `instrument_detail` 补 tencent/sina（两者都实现且都声明）；
#   - `sector` 去掉 akshare；`index_constituent` 去掉 baostock；
#     `capital` / `moneyflow` 去掉 akshare；`suspend` / `calendar` 去掉 eltdx、
#     baostock、akshare；`fundamental` 去掉 eltdx、baostock、akshare；
#     `price_limit` 去掉 akshare、baostock —— 这些源均未实现对应方法；
#   - `fundamental` / `suspend` / `corporate_action` 目前**无任何补充源实现**，
#     只保留 broker 占位（不再摆伪兜底）；`calendar` 保留 "local"（本地交易日历）；
#   - **删除 `dividend` 能力**：baostock/akshare 的旧声明声称支持，但两者都没有
#     `get_dividend` 方法，属纯声明幻觉；
#   - 新增 `minutes` / `etf_list` / `search` 三个能力 id，使「补充源专有方法」也有
#     能力归属（此前只能借道 kline 链，属能力错配）。
#   护栏 `test_capability_chain_unity.py` 同时锁死两侧：
#   「链成员必声明该能力」+「声明必落在链里」+「声明必有方法实现」。
#   注意 broker 是**唯一豁免**：它是券商授权终端，能力由 BridgeAdapter 提供，
#   `_BoundBrokerSource` 只声明 DataSource 抽象方法里真正实现的那几个；
#   链首恒为 broker 是**声明偏好**，实际是否入选由 resolve_chain 的能力校验决定。
DEFAULT_CAPABILITY_CHAINS: dict[str, tuple[str, ...]] = {
    "kline": ("broker", "eltdx", "baostock", "akshare", "tencent", "sina"),
    "kline_qfq": ("broker", "eltdx", "baostock", "akshare", "tencent"),
    "kline_hfq": ("broker", "eltdx", "baostock", "akshare", "tencent"),
    "stock_list": ("broker", "eltdx", "baostock", "akshare"),
    "instrument_detail": ("broker", "eltdx", "baostock", "akshare", "tencent", "sina"),
    "sector": ("broker", "eltdx"),
    "index_constituent": ("broker", "eltdx", "akshare"),
    "fundamental": ("broker",),
    "capital": ("broker", "eltdx"),
    "suspend": ("broker",),
    "price_limit": ("broker", "eltdx"),
    "corporate_action": ("broker",),
    "calendar": ("broker", "local"),
    "quote": ("broker", "eltdx", "tencent", "sina", "akshare"),
    "moneyflow": ("broker", "eltdx"),
    "minutes": ("eltdx",),
    "etf_list": ("eltdx",),
    "search": ("eltdx",),
}


#: 静态 provider 目录。**capabilities 必须与实现类的 `DataSource.capabilities` 一致**
#: （由 `test_capability_chain_unity.py` 断言）—— 它是「未安装/未注册时」向 UI 展示
#: 与做筛选就绪判断的依据，写「想当然」的能力会让前端显示一个其实不存在的兜底。
#: V11 R6 之前这里与实现严重不符（如 broker 声明 14 个、akshare 声明 13 个）。
PROVIDER_CATALOG = (
    ProviderDescriptor("broker", "QMT Broker", "xtquant", (
        "quote", "kline", "kline_qfq", "kline_hfq", "instrument_detail"),
        commercial_ok=True, license_note="券商授权终端，授权即合规"),
    ProviderDescriptor("eltdx", "ELTDX public market", "eltdx", (
        "quote", "kline", "kline_qfq", "kline_hfq", "instrument_detail", "stock_list",
        "sector", "index_constituent", "capital", "price_limit", "moneyflow",
        "minutes", "etf_list", "search"),
        "eltdx", "ELTDX Research-Only（禁止商用）", commercial_ok=False),
    ProviderDescriptor("baostock", "BaoStock", "baostock", (
        "kline", "kline_qfq", "kline_hfq", "instrument_detail", "stock_list"),
        "baostock", "BSD-3-Clause", commercial_ok=True),
    ProviderDescriptor("akshare", "Akshare", "http", (
        "quote", "kline", "kline_qfq", "kline_hfq", "stock_list", "instrument_detail",
        "index_constituent"), "akshare", "MIT", commercial_ok=True),
    ProviderDescriptor("sina", "Sina public quote", "http", (
        "quote", "kline", "instrument_detail"),
        license_note="public endpoint; availability is runtime checked", commercial_ok=True),
    ProviderDescriptor("tencent", "Tencent public quote", "http", (
        "quote", "kline", "kline_qfq", "kline_hfq", "instrument_detail"),
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
                      registered: set[str] | None = None,
                      declared: dict[str, frozenset[str] | set[str]] | None = None) -> list[str]:
        """按能力求值降级链（D-J §J.2 求值顺序 ①→⑤）。

        - ① 默认链；② 过滤未注册源；③ 过滤依赖缺失源；
          ④ 商用模式跳过 ``commercial_ok=False`` 的源（如 eltdx Research-Only）；
          ⑤ **（V11 R6 新增）能力校验**：``declared`` 中登记了该 provider 时，
             要求它声明了 ``capability`` 才可入选。
        - 许可证过滤是「跳过」而非「报错」，业务代码无需写例外分支（v1.3 关键修正）。

        ``declared``（provider_id → 声明能力集）由调用方（``DataSourceManager``）提供，
        取自**实现类自己的 ``capabilities``** —— 这是能力的唯一真源。
        未在 ``declared`` 中登记的 provider（如 broker：它由 ``_BoundBrokerSource``
        动态构造、不经 ``register``）沿用静态描述符的声明，避免「登记口径不一」误杀。

        为何需要 ⑤：此前链成员是手写「期望」，与实现自述双向不一致 ——
        既可能把不支持的源排进链里（只能靠 ``hasattr`` 事后跳过、白白计入熔断），
        也可能让已声明的源永远轮不到（sina/tencent 的 instrument_detail）。
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
            # 能力校验（V11 R6）：以实现类的自述为准
            if declared is not None and pid in declared:
                if capability not in declared[pid]:
                    continue
            out.append(pid)
        return out

    def require_active(self, provider_id: str) -> dict:
        if provider_id not in self._active:
            raise LookupError(f"provider is not registered and active: {provider_id}")
        return self._active[provider_id]


provider_catalog = ProviderCatalog()

__all__ = ["ProviderCatalog", "ProviderDescriptor", "PROVIDER_CATALOG", "provider_catalog"]

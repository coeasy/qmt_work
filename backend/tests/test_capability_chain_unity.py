"""V11 R6 能力链统一护栏（P1-3 双数据源链）。

回归背景（2026-09-15 实测）：仓库里并存**三套**链解析机制，且两套改链机制互不可见：

| 机制 | 规则 | 谁在用 |
|------|------|--------|
| ``_auto_candidates()`` | 扁平 `_auto_chain`（注册序）+ 仅许可证过滤 | get_quote / get_instrument_detail / get_minutes / get_stock_list / search_stocks |
| ``resolve_chain(cap)`` | 能力契约链 + 注册 + 依赖 + 许可证 | 只有 get_kline |
| ``_sup_chain()`` | ``resolve_chain("kline")`` − broker | 8 个补充源方法（boards/etf_list/moneyflow/…）——**用 kline 链解析非 kline 能力** |

实测出的硬事实：
1. ``provider_catalog.set_override("quote", ["tencent"])`` 后 API 回显 ``['tencent']``，
   而 ``get_quote`` 仍取 ``sina`` —— **改链 API 对 5 条读路径静默无效**；
2. 扁平链给 ``sina`` 优先、契约链给 ``tencent`` 优先 —— 实际取 ``sina``，
   而 ``app/platform.py`` 向 UI 宣告的是 ``tencent``；
3. ``_sup_chain`` 给 ``get_moneyflow`` 的候选是 kline 链 ``[tencent, sina]``，
   而 moneyflow 的真实链是 ``(broker, eltdx, akshare)`` —— **源集合不相交**；
4. ``resolve_chain`` **从不校验「该 provider 是否声明了该能力」**，而链成员是手写
   「期望」，与实现类自述双向不一致。

本文件锁死「统一到能力契约链」后的四条不变量。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from datasource.akshare_source import AkshareSource  # noqa: E402
from datasource.base import DataSource  # noqa: E402
from datasource.eltdx_source import EltdxSource  # noqa: E402
from datasource.optional_sources import BaoStockSource  # noqa: E402
from datasource.providers import (  # noqa: E402
    DEFAULT_CAPABILITY_CHAINS,
    PROVIDER_CATALOG,
    provider_catalog,
)
from datasource.public_sources import SinaSource, TencentSource  # noqa: E402
from datasource.registry import DataSourceManager, _BoundBrokerSource  # noqa: E402

BACKEND = Path(__file__).resolve().parent.parent
REGISTRY_SRC = (BACKEND / "datasource" / "registry.py").read_text(encoding="utf-8")

#: 能力 -> 承载方法（用于「声明必有实现」检查）。
#: 注意 `index_constituent`：eltdx 用 get_board_kline，baostock/akshare 用
#: get_index_constituents（复数），两者都算实现。
CAP_METHODS: dict[str, tuple[str, ...]] = {
    "quote": ("get_quote",),
    "kline": ("get_kline",),
    "kline_qfq": ("get_kline",),
    "kline_hfq": ("get_kline",),
    "stock_list": ("get_stock_list",),
    "instrument_detail": ("get_instrument_detail",),
    "sector": ("get_boards",),
    "index_constituent": ("get_index_constituents", "get_board_kline"),
    "capital": ("get_share_capital",),
    "price_limit": ("get_price_limits",),
    "moneyflow": ("get_moneyflow",),
    "minutes": ("get_minutes",),
    "etf_list": ("get_etf_list",),
    "search": ("search",),
    # 以下四个能力目前**无任何补充源实现**，链里只有 broker 占位；
    # 登记方法名是为了让「声明 → 实现」检查能覆盖它们（一旦有源声明就必须真有方法）。
    "fundamental": ("get_fundamental",),
    "suspend": ("get_suspend",),
    "corporate_action": ("get_corporate_action",),
    "calendar": ("get_calendar",),
}

#: 实现类（按 provider_id）。全部可**无 SDK 导入**（依赖都在方法内惰性 import）。
IMPLS: dict[str, type] = {
    "broker": _BoundBrokerSource,
    "eltdx": EltdxSource,
    "baostock": BaoStockSource,
    "akshare": AkshareSource,
    "sina": SinaSource,
    "tencent": TencentSource,
}


# ---------------------------------------------------------------- 不变量 ① 链 → 声明
def test_every_chain_member_declares_the_capability():
    """链成员必须声明该能力（否则它会占着位置、只能靠 hasattr 事后跳过）。

    broker 是**唯一豁免**：它是券商授权终端，能力由 BridgeAdapter 提供，
    ``_BoundBrokerSource`` 只声明 DataSource 抽象方法里真正实现的那几个；
    「链首恒为 broker」是声明偏好，实际是否入选由 ``resolve_chain`` 的能力校验决定。
    """
    violations = []
    for cap, chain in DEFAULT_CAPABILITY_CHAINS.items():
        for pid in chain:
            if pid == "broker" or pid == "local":
                continue
            cls = IMPLS.get(pid)
            if cls is None:
                continue
            if cap not in cls.capabilities:
                violations.append(f"{cap}: {pid} 在链里但未声明该能力")
    assert not violations, "链成员未声明能力：\n" + "\n".join(violations)


# ---------------------------------------------------------------- 不变量 ② 声明 → 链
def test_every_declared_capability_is_reachable_via_a_chain():
    """任何源声明的能力都必须落在对应契约链里，否则该能力**永远轮不到**它。

    实测回归：sina/tencent 声明了 ``instrument_detail``，而该链原本只有
    ``(broker, eltdx, baostock, akshare)`` —— 两个源永远不可能被选中。
    """
    violations = []
    for pid, cls in IMPLS.items():
        for cap in cls.capabilities:
            if cap not in DEFAULT_CAPABILITY_CHAINS:
                violations.append(f"{pid} 声明了 {cap}，但没有任何契约链包含它")
            elif pid not in DEFAULT_CAPABILITY_CHAINS[cap]:
                violations.append(f"{pid} 声明了 {cap}，但不在链 "
                                  f"{DEFAULT_CAPABILITY_CHAINS[cap]} 中")
    assert not violations, "声明了但链里够不到：\n" + "\n".join(violations)


# ---------------------------------------------------------------- 不变量 ③ 声明 → 实现
def test_every_declared_capability_has_an_implementation():
    """声明的能力必须有对应方法 —— 禁止「声明幻觉」。

    实测回归：baostock 曾声明 suspend/fundamental/index_constituent/dividend，
    akshare 曾声明 sector/fundamental/capital/suspend/moneyflow/calendar/dividend，
    而两个类**一个都没实现** —— 链路求值会把它们排进候选，再靠 hasattr 事后跳过。
    """
    violations = []
    for pid, cls in IMPLS.items():
        for cap in cls.capabilities:
            methods = CAP_METHODS.get(cap)
            if methods is None:
                violations.append(f"{pid} 声明了未知能力 {cap}（CAP_METHODS 未登记）")
                continue
            if not any(hasattr(cls, m) for m in methods):
                violations.append(f"{pid} 声明了 {cap}，但没有实现 {methods}")
    assert not violations, "声明了但没有实现：\n" + "\n".join(violations)


# ---------------------------------------------------------------- 不变量 ④ 静态目录 == 实现
def test_static_catalog_capabilities_match_implementation():
    """静态 ``PROVIDER_CATALOG`` 的 capabilities 必须等于实现类的自述。

    静态目录是「未安装/未注册时」向 UI 展示与判筛选就绪的依据；写「想当然」的能力
    会让前端显示一个其实不存在的兜底。实测回归：broker 静态声明 14 个、实现只 3 个；
    akshare 静态声明 13 个、实现只 5 个。
    """
    by_id = {d.id: d for d in PROVIDER_CATALOG}
    violations = []
    for pid, cls in IMPLS.items():
        desc = by_id.get(pid)
        if desc is None:
            violations.append(f"{pid} 不在 PROVIDER_CATALOG 中")
            continue
        static, impl = set(desc.capabilities), set(cls.capabilities)
        if static != impl:
            violations.append(
                f"{pid}: 静态 {sorted(static)} != 实现 {sorted(impl)}"
                f"（仅静态有 {sorted(static - impl)} / 仅实现有 {sorted(impl - static)}）")
    assert not violations, "静态目录与实现不一致：\n" + "\n".join(violations)


# ---------------------------------------------------------------- resolve_chain 能力校验
def _manager_with(*names: str) -> DataSourceManager:
    m = DataSourceManager()
    m.register_broker(lambda cid: None)
    for n in names:
        cls = IMPLS[n]
        src = cls.__new__(cls)
        DataSource.__init__(src) if hasattr(DataSource, "__init__") else None
        src.name = n
        m.register(src)
    return m


def test_resolve_chain_filters_providers_not_declaring_capability():
    """``resolve_chain`` 的能力校验：未声明该能力的源必须被剔除。"""
    m = _manager_with("sina", "tencent")
    declared = m._declared_map()
    registered = m._registered_set()

    # 两者都声明 quote → 都在
    quote = provider_catalog.resolve_chain("quote", registered=registered, declared=declared)
    assert "sina" in quote and "tencent" in quote

    # 两者都不声明 stock_list → 都不在（get_stock_list 是显式 raise）
    stock = provider_catalog.resolve_chain("stock_list", registered=registered, declared=declared)
    assert "sina" not in stock and "tencent" not in stock, stock

    # sina 不声明复权变体 → 只有 tencent 在
    qfq = provider_catalog.resolve_chain("kline_qfq", registered=registered, declared=declared)
    assert "tencent" in qfq and "sina" not in qfq, qfq


def test_resolve_chain_without_declared_keeps_legacy_behaviour():
    """不传 ``declared`` 时保持旧语义（只按注册/依赖/许可过滤），便于外部调用点渐进迁移。

    用 ``set_override`` 临时构造「链里有、声明无」的场景（sina 忽略 adjust，不声明
    kline_qfq）—— 修正后的真实契约链已不存在这种成员，故必须临时造一个。
    """
    m = _manager_with("sina", "tencent")
    registered = m._registered_set()
    try:
        provider_catalog.set_override("kline_qfq", ["sina", "tencent"])
        with_declared = provider_catalog.resolve_chain(
            "kline_qfq", registered=registered, declared=m._declared_map())
        without = provider_catalog.resolve_chain("kline_qfq", registered=registered)
        assert "sina" not in with_declared, with_declared
        assert "sina" in without, "不传 declared 时不应做能力过滤"
        assert "tencent" in with_declared and "tencent" in without
    finally:
        provider_catalog.clear_override()


# ---------------------------------------------------------------- override 对所有路径生效
def test_override_affects_every_read_path():
    """``set_override`` 必须对所有读路径生效（回归：此前只对 K 线生效）。

    实测：``set_override("quote", ["tencent"])`` 后 API 回显 ``['tencent']``，
    而 ``get_quote`` 仍取 ``sina`` —— 「API 说谎」。
    """
    m = _manager_with("sina", "tencent")
    registered = m._registered_set()
    try:
        provider_catalog.set_override("quote", ["sina"])
        resolved = provider_catalog.resolve_chain(
            "quote", registered=registered, declared=m._declared_map())
        assert m._resolve_sources("auto", "quote") == resolved == ["sina"]
    finally:
        provider_catalog.clear_override()


def test_all_read_paths_go_through_the_single_resolver():
    """源码级：读路径不得绕过 ``_resolve_sources``。"""
    assert "def _auto_candidates" not in REGISTRY_SRC, \
        "扁平链 _auto_candidates 应已删除（统一到 _resolve_sources）"
    assert "self._auto_candidates(" not in REGISTRY_SRC, \
        "不得再有调用点绕过 _resolve_sources"
    # 读路径的循环头必须用 _resolve_sources
    for cap in ("quote", "instrument_detail", "minutes", "stock_list", "search"):
        assert f'_resolve_sources(source, "{cap}")' in REGISTRY_SRC \
            or f'_resolve_sources("auto", "{cap}")' in REGISTRY_SRC, \
            f"读路径 {cap} 未走统一解析入口"


def test_sup_chain_uses_real_capability_not_kline():
    """``_sup_chain`` 必须按**真实能力**解析，而不是一律借道 kline 链。

    实测回归：``get_moneyflow`` 拿到的是 kline 链 ``[tencent, sina]``，
    而 moneyflow 的真实链是 ``(broker, eltdx, akshare)``。
    """
    m = _manager_with("sina", "tencent")
    kline = m._sup_chain("auto", "kline")
    moneyflow = m._sup_chain("auto", "moneyflow")
    assert moneyflow != kline, (
        f"_sup_chain 仍在使用 kline 链（moneyflow={moneyflow} kline={kline}）")
    # sina/tencent 都不声明 moneyflow → 空链
    assert moneyflow == [], moneyflow


def test_sup_chain_signature_requires_capability():
    """``_sup_chain`` 的 capability 参数不得被移除（防止退回「一律 kline」）。"""
    import inspect
    sig = inspect.signature(DataSourceManager._sup_chain)
    assert "capability" in sig.parameters


def test_no_dividend_capability_left_behind():
    """``dividend`` 能力已删除 —— baostock/akshare 都没有 get_dividend，属纯声明幻觉。"""
    assert "dividend" not in DEFAULT_CAPABILITY_CHAINS
    for pid, cls in IMPLS.items():
        assert "dividend" not in cls.capabilities, pid


def test_chain_keys_are_known_capabilities():
    """链表里不得出现 CAP_METHODS 之外的能力（防止拼写错误静默产生空链）。"""
    unknown = [c for c in DEFAULT_CAPABILITY_CHAINS if c not in CAP_METHODS]
    assert not unknown, f"未知能力（拼写错误会静默变空链）：{unknown}"


def test_registry_has_no_module_level_auto_chain_literal():
    """``_auto_chain`` 不应再作为「注册序」可变列表存在（已改为只读 property）。"""
    assert re.search(r"self\._auto_chain\s*[:=]\s*\[", REGISTRY_SRC) is None, \
        "self._auto_chain 不应再被赋值为注册序列表"
    assert "_auto_chain_override" in REGISTRY_SRC, \
        "覆盖链应存在（set_auto_chain 的显式逃生口）"

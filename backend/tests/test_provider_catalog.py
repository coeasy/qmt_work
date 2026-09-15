from datasource.optional_sources import BaoStockSource
from datasource.providers import ProviderCatalog
from datasource.public_sources import SinaSource, TencentSource


def test_provider_catalog_exposes_optional_sources_without_claiming_active():
    catalog = ProviderCatalog()
    rows = {row["provider"]: row for row in catalog.describe()}
    # tstdx（pytdx）已于 2026-09-13 按方案 §6.1 移除
    assert {"eltdx", "baostock", "sina", "tencent"} <= rows.keys()
    assert "tstdx" not in rows
    assert all(row["active"] is False for row in rows.values())


def test_provider_catalog_requires_explicit_registration():
    catalog = ProviderCatalog()
    try:
        catalog.require_active("sina")
    except LookupError as exc:
        assert "sina" in str(exc)
    else:
        raise AssertionError("inactive provider must not resolve")


def test_public_sources_have_real_capability_contracts():
    """公共源的能力声明必须与**真实实现**一致（V11 R6 修正）。

    此前这里断言 ``SinaSource().capabilities == TencentSource().capabilities`` ——
    那是「想当然的相等」，恰好掩盖了真实差异：``TencentSource.get_kline`` 真的处理
    ``adjust``（映射到 fqkline 的 adj 参数），``SinaSource.get_kline`` 则**完全忽略**
    ``adjust``（只回不复权日线）。故 tencent 声明复权变体、sina 不声明 —— 与
    ``DEFAULT_CAPABILITY_CHAINS`` 的 kline_qfq/kline_hfq 成员（含 tencent、不含 sina）
    两处一致。
    """
    sina, tencent = SinaSource().capabilities, TencentSource().capabilities
    base = {"quote", "kline", "instrument_detail"}
    assert base <= sina and base <= tencent
    assert "kline_qfq" not in sina and "kline_hfq" not in sina   # 忽略 adjust
    assert {"kline_qfq", "kline_hfq"} <= tencent                 # 真正支持复权
    # 两者都不提供全市场列表（get_stock_list 是显式 raise，不是待实现桩）
    assert "stock_list" not in sina and "stock_list" not in tencent
    assert "quote" in sina


def test_optional_provider_contracts_fail_closed_without_sdk():
    assert "kline" in BaoStockSource.capabilities

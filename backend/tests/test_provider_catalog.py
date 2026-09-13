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
    assert SinaSource().capabilities == TencentSource().capabilities
    assert "quote" in SinaSource().capabilities


def test_optional_provider_contracts_fail_closed_without_sdk():
    assert "kline" in BaoStockSource.capabilities

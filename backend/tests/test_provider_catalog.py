from datasource.providers import ProviderCatalog
from datasource.public_sources import SinaSource, TencentSource


def test_provider_catalog_exposes_optional_sources_without_claiming_active():
    catalog = ProviderCatalog()
    rows = {row["provider"]: row for row in catalog.describe()}
    assert {"tstdx", "eltdx", "baostock", "sina", "tencent"} <= rows.keys()
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

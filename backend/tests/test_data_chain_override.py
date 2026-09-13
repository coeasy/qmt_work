"""运行时链路覆盖端点逻辑（POST /data/chain 后端）：set_override / clear / 校验。

默认契约链不可被修改（改顺序须三处同步），override 仅覆盖、不持久化。
"""
from datasource.providers import provider_catalog, DEFAULT_CAPABILITY_CHAINS


def test_override_applies_and_resolves():
    orig = dict(provider_catalog._overrides)
    try:
        provider_catalog.set_override("kline", ["broker", "akshare"])
        assert provider_catalog.default_chain("kline") == ["broker", "akshare"]
    finally:
        provider_catalog._overrides = orig


def test_override_unknown_provider_raises():
    orig = dict(provider_catalog._overrides)
    try:
        raised = False
        try:
            provider_catalog.set_override("kline", ["nope"])
        except ValueError:
            raised = True
        assert raised
    finally:
        provider_catalog._overrides = orig


def test_clear_restores_default():
    orig = dict(provider_catalog._overrides)
    try:
        provider_catalog.set_override("kline", ["broker", "akshare"])
        provider_catalog.clear_override("kline")
        assert (provider_catalog.default_chain("kline") ==
                list(DEFAULT_CAPABILITY_CHAINS["kline"]))
    finally:
        provider_catalog._overrides = orig


def test_default_contract_chain_unchanged_by_override():
    orig = dict(provider_catalog._overrides)
    try:
        provider_catalog.set_override("kline", ["broker", "akshare"])
        # 默认契约链本身未被改
        assert list(DEFAULT_CAPABILITY_CHAINS["kline"])[1] == "eltdx"
    finally:
        provider_catalog._overrides = orig

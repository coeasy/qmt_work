"""多源行情 DataSourceManager 单元测试（测试替身源，无 eltdx / 无网络）。

覆盖：auto 回退、显式源、复权链（v1.3：QMT 参与复权链，故障再降级补充源）、
全失败返回 (None, None)、熔断、超时、搜索索引。
需在装有 pytest 的环境运行（pytest 未装时可用 `python -m tests.test_datasource` 自查）。
"""
import asyncio
import time

from datasource.base import DataSource
from datasource.board import classify_board, limit_ratio
from datasource.registry import DataSourceManager, UnsupportedDataSource

from _phase4_support import force_deps


class FakeBroker:
    def __init__(self, fail: bool = False):
        self.fail = fail

    async def get_quote(self, code):
        if self.fail:
            raise RuntimeError("boom")
        return {"code": code, "last": 1.0}

    async def get_kline(self, code, period="1d", count=250, adjust=None):
        if self.fail:
            raise RuntimeError("boom")
        return [{"close": 1}]

    async def get_instrument_detail(self, code):
        if self.fail:
            raise RuntimeError("boom")
        return {"name": "B", "up_limit_price": 1.1,
                "down_limit_price": 0.9, "pre_close": 1.0}

    async def get_stock_list(self):
        return [{"code": "B.SH", "name": "B"}]


class FakeTDX(DataSource):
    name = "eltdx"

    def __init__(self, fail: bool = False):
        self.fail = fail

    async def get_quote(self, code):
        if self.fail:
            raise RuntimeError("tdx down")
        return {"code": code, "last": 2.0}

    async def get_kline(self, code, period="1d", count=250, adjust=None):
        if self.fail:
            raise RuntimeError("tdx down")
        return [{"close": 2}]

    async def get_instrument_detail(self, code):
        return {"name": "E", "high_limit": 2.2, "low_limit": 1.8,
                "pre_close": 2.0, "industry": "", "concepts": []}

    async def get_stock_list(self):
        return [{"code": "E.SH", "name": "E"}]

    async def search(self, q, limit=20):
        return [{"code": "E.SH", "name": "E"}]


class Slow(DataSource):
    name = "slow"

    async def get_quote(self, code):
        await asyncio.sleep(10)
        return {"code": code, "last": 9}

    async def get_kline(self, *a, **k):
        return []

    async def get_instrument_detail(self, code):
        return {}

    async def get_stock_list(self):
        return []


def _m(broker_fail=False, tdx_fail=False, slow=False):
    m = DataSourceManager()
    m.register_broker(lambda cid: FakeBroker(fail=broker_fail))
    if slow:
        m.register(Slow())
    else:
        m.register(FakeTDX(fail=tdx_fail))
    m.set_auto_chain(["broker", "eltdx"])
    return m


def test_auto_prefers_broker():
    async def c():
        q = await _m().get_quote("X.SH")
        assert q["source"] == "broker" and q["last"] == 1.0
    asyncio.run(c())


def test_auto_falls_back_to_eltdx():
    async def c():
        q = await _m(broker_fail=True).get_quote("X.SH")
        assert q["source"] == "eltdx" and q["last"] == 2.0
    asyncio.run(c())


def test_explicit_source():
    async def c():
        assert (await _m().get_quote("X.SH", source="eltdx"))["source"] == "eltdx"
    asyncio.run(c())


def test_provider_capability_manifest():
    m = _m()
    details = m.describe_sources()
    assert details["eltdx"]["active"] is True
    assert "quote" in details["eltdx"]["capabilities"]
    assert "broker" in details


def test_unknown_explicit_source_does_not_fallback():
    async def c():
        try:
            await _m().get_quote("X.SH", source="missing-provider")
        except UnsupportedDataSource as exc:
            assert exc.source == "missing-provider"
        else:  # pragma: no cover - guard against accidental auto fallback
            raise AssertionError("unknown explicit source must not enter auto chain")
    asyncio.run(c())


def test_explicit_broker_failure_does_not_fallback():
    async def c():
        assert await _m(broker_fail=True).get_quote("X.SH", source="broker") is None
    asyncio.run(c())


def test_explicit_kline_broker_failure_does_not_fallback():
    async def c():
        bars, src = await _m(broker_fail=True).get_kline("X.SH", source="broker")
        assert bars is None and src is None
    asyncio.run(c())


def test_kline_adjusted_chain_prefers_broker_then_falls_back():
    """v1.3 契约（D9）：复权经 dividend_type 参数化后 **QMT 参与复权链**（不再跳过 broker）；
    broker 不可用时按能力链真实降级到 eltdx（绝不外传伪造数据）。

    ★ 必须用 force_deps()：get_kline 走 provider_catalog.resolve_chain，其中会按
    ``importlib.util.find_spec(optional_dependency)`` 过滤「依赖未安装」的源。本用例
    注入的是**测试替身** FakeTDX（name="eltdx"），而真实 eltdx 包并未安装，不做
    find_spec 替身时 eltdx 会被链路过滤掉，降级断言就会假失败。
    （对照：get_quote 走 _auto_candidates，只做许可证过滤、不做依赖过滤，故无需 force_deps。）
    """
    async def c():
        bars, src = await _m().get_kline("X.SH")
        assert src == "broker" and bars[0]["close"] == 1
        # qfq 链首恒为 broker（链序 QMT→eltdx→baostock→akshare）
        bars2, src2 = await _m().get_kline("X.SH", adjust="qfq")
        assert src2 == "broker" and bars2[0]["close"] == 1
        # broker 故障 → 真实降级到 eltdx（而非报错或返回空）
        bars3, src3 = await _m(broker_fail=True).get_kline("X.SH", adjust="qfq")
        assert src3 == "eltdx" and bars3[0]["close"] == 2
        # 全链失败 → (None, None)，不冒充「无符合标的」
        bars4, src4 = await _m(broker_fail=True, tdx_fail=True).get_kline("X.SH", adjust="qfq")
        assert bars4 is None and src4 is None
    with force_deps():
        asyncio.run(c())


def test_all_fail_returns_none():
    async def c():
        assert await _m(broker_fail=True, tdx_fail=True).get_quote("X.SH") is None
    asyncio.run(c())


def test_breaker_trips_and_skips():
    async def c():
        m = _m(broker_fail=True)
        for _ in range(3):
            await m.get_quote("X.SH")
        h = await m.health()
        assert "熔断" in h["broker"]["note"]
        q = await m.get_quote("X.SH")
        assert q["source"] == "eltdx"
    asyncio.run(c())


def test_search_stocks_indexed():
    async def c():
        s = await _m().search_stocks("E", 5)
        assert s and s[0]["code"] == "E.SH"
    asyncio.run(c())


def test_slow_source_times_out():
    async def c():
        assert await _m(broker_fail=True, slow=True).get_quote("X.SH") is None
    asyncio.run(c())


def test_classify_and_limit():
    assert classify_board("600519.SH")["board"] == "沪市主板"
    assert classify_board("300750.SZ")["board"] == "创业板"
    assert abs(limit_ratio("600519.SH") - 0.10) < 1e-9
    assert abs(limit_ratio("300750.SZ") - 0.20) < 1e-9
    assert limit_ratio("113050.SH") is None          # 可转债无涨跌幅
    assert abs(limit_ratio("600519.SH", "ST 某某") - 0.05) < 1e-9


def test_merge_quote_derives_change_pct():
    """broker 原始快照不带 change/change_pct（eltdx 已在源内算）——
    _merge_quote 必须从 last/昨收统一推导，否则券商连接后指数条全空涨跌幅。"""
    from datasource.registry import DataSourceManager as _DSM
    merged = _DSM._merge_quote(
        {"code": "000001.SH", "last": 10.0, "lastClose": 8.0}, "000001.SH",
        classify_board("000001.SH"), {}, "broker")
    assert merged["change"] == 2.0
    assert merged["change_pct"] == 25.0
    # 已带 change/change_pct 的源（eltdx）：保持原值不重算
    keep = _DSM._merge_quote(
        {"code": "X.SH", "last": 10.0, "lastClose": 8.0,
         "change": 1.5, "change_pct": 15.0}, "X.SH",
        classify_board("600519.SH"), {}, "eltdx")
    assert keep["change"] == 1.5 and keep["change_pct"] == 15.0
    # 缺昨收：置 None（不伪造）
    none_c = _DSM._merge_quote({"code": "X.SH", "last": 10.0}, "X.SH",
                               classify_board("600519.SH"), {}, "broker")
    assert none_c["change"] is None and none_c["change_pct"] is None


def test_commercial_mode_blocks_eltdx_on_all_paths():
    """许可证合规（D-J §J.5）：商用模式下 **所有** 取数路径都必须跳过 eltdx。

    eltdx 是 ELTDX Research-Only 许可（禁止一切商业使用），其 ProviderDescriptor
    的 ``commercial_ok=False``。回归背景（2026-09-13 修复）：

    此前只有 ``get_kline`` 经 ``resolve_chain`` 应用了商用过滤，而 ``get_quote`` /
    ``get_instrument_detail`` / ``get_minutes`` / ``get_stock_list`` /
    ``search_stocks`` 直接遍历 ``_auto_chain``，**绕过了许可证过滤**。后果是商用
    部署里 K 线已正确跳过 eltdx，实时行情却仍在用 eltdx —— 等于把禁止商用的数据源
    用在了商业部署中。修复后五条路径统一走 ``_auto_candidates()``。

    本用例同时覆盖「非商用必须仍可用」，避免用「一律禁用 eltdx」的粗暴修法蒙混过关。
    """
    with force_deps():
        m = _m(broker_fail=True)  # broker 故障，迫使走补充源

        async def c():
            # --- 非商用（个人研究）：eltdx 全路径可用 ---
            m.set_commercial_mode(False)
            assert (await m.get_quote("X.SH"))["source"] == "eltdx"
            assert (await m.get_kline("X.SH"))[1] == "eltdx"
            assert (await m.get_instrument_detail("X.SH"))["name"] == "E"
            assert await m.search_stocks("E") == [{"code": "E.SH", "name": "E"}]

            # --- 商用：eltdx 必须被全路径跳过（不报错、静默降级）---
            m.set_commercial_mode(True)
            assert await m.get_quote("X.SH") is None
            assert await m.get_kline("X.SH") == (None, None)
            assert await m.get_instrument_detail("X.SH") is None
            assert await m.search_stocks("E") == []
        asyncio.run(c())


def test_license_gate_keeps_broker_and_public_sources():
    """许可证过滤只针对 commercial_ok=False 的源，不得误伤 broker 与 MIT/公共源。"""
    m = _m()
    m.set_commercial_mode(True)
    assert m._license_ok("broker") is True   # 券商授权终端，授权即合规
    assert m._license_ok("eltdx") is False   # Research-Only
    assert m._license_ok("baostock") is True  # BSD-3-Clause
    assert m._license_ok("akshare") is True   # MIT
    # broker 恒在候选链中，即使商用模式
    assert "broker" in m._auto_candidates()


# ---------------- 券商详情为空壳时的画像富化（ETF / 指数真实形态） ----------------
#
# 回归背景（2026-09-14 实测）：券商对 ETF/指数常回**空壳详情**（无名称、无涨跌停），
# 原实现会顺序遍历全部插件源且不设总时限。一旦某源不可达（实测 sina 经本机代理
# 403，单次约 5.5s），整次 get_quote 被拖到 5.6s —— 实测同一接口
# 股票 0.02s / ETF·指数 5.6s。而实测这 5.6s 只换来一个名称。

class ShellBroker:
    """详情为空壳的券商（ETF/指数的真实形态）：有价、无名称、无涨跌停。"""

    async def get_quote(self, code):
        return {"code": code, "last": 1.0}

    async def get_kline(self, *a, **k):
        return []

    async def get_instrument_detail(self, code):
        return {}

    async def get_stock_list(self):
        return []


def test_shell_detail_falls_back_to_local_name_when_network_fails():
    """网络富化全失败时，须用**本地名称表**兜底，不得让界面退化成「只有代码」。

    这正是原 docstring 描述的坏体验：空壳详情 + 富化失败 → 个股名显示为一串代码。
    """
    class Dead(DataSource):
        name = "eltdx"

        @classmethod
        def lookup_name(cls, code):
            return "本地名称" if code == "X.SH" else None

        async def get_quote(self, code):
            return {"code": code, "last": 2.0}

        async def get_kline(self, *a, **k):
            return []

        async def get_instrument_detail(self, code):
            raise RuntimeError("source down")

        async def get_stock_list(self):
            return []

    async def c():
        m = DataSourceManager()
        m.register_broker(lambda cid: ShellBroker())
        m.register(Dead())
        m.set_auto_chain(["broker", "eltdx"])
        q = await m.get_quote("X.SH")
        assert q["source"] == "broker" and q["last"] == 1.0, q
        assert q["name"] == "本地名称", f"富化失败时未用本地名称兜底：{q}"

    asyncio.run(c())


def test_detail_enrichment_result_is_cached():
    """富化结果须缓存：合约画像日内不变，同一代码不得反复付网络代价。

    回归背景：不可达源会让单次富化耗时秒级；无缓存则每次 get_quote 都重付。
    """
    calls: list = []

    class Good(DataSource):
        name = "eltdx"

        async def get_quote(self, code):
            return {"code": code, "last": 2.0}

        async def get_kline(self, *a, **k):
            return []

        async def get_instrument_detail(self, code):
            calls.append(code)
            return {"name": "创业板指", "industry": "指数"}

        async def get_stock_list(self):
            return []

    async def c():
        m = DataSourceManager()
        m.register_broker(lambda cid: ShellBroker())
        m.register(Good())
        m.set_auto_chain(["broker", "eltdx"])
        q1 = await m.get_quote("X.SH")
        assert q1["name"] == "创业板指", q1
        assert calls == ["X.SH"], calls
        q2 = await m.get_quote("X.SH")
        assert q2["name"] == "创业板指", q2
        assert calls == ["X.SH"], f"富化结果未缓存，重复打源：{calls}"

    asyncio.run(c())


def test_shell_detail_network_enrichment_is_time_bounded():
    """本地名称表未命中时（指数/板块），网络富化必须有总时限。

    不可达源不得把行情拖到秒级——详情只是增强项，拿不到也要让行情照常返回。
    """
    class Hanging(DataSource):
        name = "eltdx"

        async def get_quote(self, code):
            return {"code": code, "last": 2.0}

        async def get_kline(self, *a, **k):
            return []

        async def get_instrument_detail(self, code):
            await asyncio.sleep(30)          # 不可达源：永不返回
            return {}

        async def get_stock_list(self):
            return []

    async def c():
        m = DataSourceManager()
        m.register_broker(lambda cid: ShellBroker())
        m.register(Hanging())
        m.set_auto_chain(["broker", "eltdx"])
        t0 = time.monotonic()
        q = await m.get_quote("X.SH")
        dt = time.monotonic() - t0
        assert q["source"] == "broker" and q["last"] == 1.0, q
        assert dt < 2.5, f"详情富化未限时，耗时 {dt:.2f}s（行情被增强项拖死）"

    asyncio.run(c())


def test_shell_detail_enrichment_still_yields_name_when_a_source_works():
    """限预算不能把富化能力砍掉：有可用源时仍须补出名称（指数场景）。"""
    class Hanging(DataSource):
        name = "eltdx"

        async def get_quote(self, code):
            return {"code": code, "last": 2.0}

        async def get_kline(self, *a, **k):
            return []

        async def get_instrument_detail(self, code):
            await asyncio.sleep(30)          # 排在前面的不可达源
            return {}

        async def get_stock_list(self):
            return []

    class Good(DataSource):
        name = "tencent"

        async def get_quote(self, code):
            return {"code": code, "last": 3.0}

        async def get_kline(self, *a, **k):
            return []

        async def get_instrument_detail(self, code):
            # 注意：只回 name 会被 _contentless_detail 判为空壳（名称之外还需
            # 涨跌停/行业/概念之一），故这里带上 industry —— 与真实 tencent 形态一致。
            return {"name": "创业板指", "industry": "指数"}

        async def get_stock_list(self):
            return []

    async def c():
        m = DataSourceManager()
        m.register_broker(lambda cid: ShellBroker())
        m.register(Hanging())
        m.register(Good())
        m.set_auto_chain(["broker", "eltdx", "tencent"])
        t0 = time.monotonic()
        q = await m.get_quote("X.SH")
        dt = time.monotonic() - t0
        assert q["name"] == "创业板指", f"排在后方的可用源被误放弃：{q}"
        assert dt < 2.5, f"耗时 {dt:.2f}s"

    asyncio.run(c())


if __name__ == "__main__":
    for fn in (test_auto_prefers_broker, test_auto_falls_back_to_eltdx,
               test_explicit_source, test_kline_adjusted_chain_prefers_broker_then_falls_back,
               test_all_fail_returns_none, test_breaker_trips_and_skips,
               test_search_stocks_indexed, test_slow_source_times_out,
               test_classify_and_limit, test_commercial_mode_blocks_eltdx_on_all_paths,
               test_license_gate_keeps_broker_and_public_sources,
               test_shell_detail_falls_back_to_local_name_when_network_fails,
               test_detail_enrichment_result_is_cached,
               test_shell_detail_network_enrichment_is_time_bounded,
               test_shell_detail_enrichment_still_yields_name_when_a_source_works):
        fn()
    print("ALL DATASOURCE TESTS PASSED")

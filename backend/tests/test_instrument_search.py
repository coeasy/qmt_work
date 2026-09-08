"""标的检索与分析体系单元测试（纯函数 + 路由层 monkeypatch，零网络零 mock 数据）。

覆盖（对应 docs/标的检索与分析体系（2026-08-31）.md）：
- 检索层：pinyin GBK 首字母（含金融多音字）/ instrument 双因子分类 /
  /market/search 富信息 / /market/resolve 归一
- 分析层：/market/analysis 六维聚合 + availability + _perf_from_bars

路由层测试用 monkeypatch 替换 get_hub()/_need()（假源返回确定形状的真实结构，
不伪造行情数值——只验证聚合与字段契约，不验证行情本身）。
"""
import asyncio

import pytest

from datasource.instrument import classify_instrument
from datasource.pinyin import matches_initials, pinyin_initials
from app.routes import market as mk


# ============================ 拼音首字母（GBK 区间法） ============================
class TestPinyin:
    def test_common_stock_names(self):
        assert pinyin_initials("贵州茅台") == "gzmt"
        # 「行」主音 xíng → payx（háng 经多音字表补召回，见 polyphonic 用例）
        assert pinyin_initials("平安银行") == "payx"
        assert pinyin_initials("工商银行") == "gsyx"
        assert pinyin_initials("易方达香港证券ETF") == "yfdxgzqetf"

    def test_mixed_alnum(self):
        # 万科A：汉字 + ASCII 字母混排，字母按原样小写参与
        assert pinyin_initials("万科A") == "wka"

    def test_empty_and_non_chinese(self):
        assert pinyin_initials("") == ""
        assert pinyin_initials("ETF") == "etf"
        assert pinyin_initials("  ") == ""

    def test_matches_initials_exact(self):
        assert matches_initials("贵州茅台", "gzmt") is True
        assert matches_initials("贵州茅台", "gz") is True   # 前缀（q 更短）
        assert matches_initials("贵州茅台", "gzmx") is False

    def test_matches_initials_polyphonic(self):
        # 「行」xíng(主音)/háng 双候选：payh / payx 均命中平安银行
        assert matches_initials("平安银行", "payh") is True
        assert matches_initials("平安银行", "payx") is True
        # 「长」cháng/zhǎng 双候选：长安 ca / za 均命中
        assert matches_initials("长安", "ca") is True
        assert matches_initials("长安", "za") is True

    def test_matches_initials_guard(self):
        assert matches_initials("贵州茅台", "") is False
        assert matches_initials("贵州茅台", "123") is False
        assert matches_initials(None, "gz") is False


# ============================ 标的分类画像（双因子） ============================
class TestInstrument:
    def test_etf_by_exchange_dual_factor(self):
        assert classify_instrument("513090.SH")["type"] == "etf"      # 沪 51 段
        assert classify_instrument("159915.SZ")["type"] == "etf"      # 深 15 段
        assert classify_instrument("510300.SH")["label"] == "ETF"

    def test_index_vs_stock_same_prefix(self):
        # 双因子核心场景：000001 沪=指数 / 深=股票
        assert classify_instrument("000001.SH")["type"] == "index"
        assert classify_instrument("000001.SZ")["type"] == "stock"
        assert classify_instrument("399001.SZ")["type"] == "index"
        assert classify_instrument("899050.BJ")["type"] == "index"

    def test_board_prefix(self):
        ind = classify_instrument("881305.SH")
        assert ind["type"] == "board" and ind["exchange"] == "板块"
        assert ind["board"] == "行业板块"
        assert classify_instrument("880001.SH")["board"] == "概念/统计板块"

    def test_bond(self):
        assert classify_instrument("113050.SH")["type"] == "bond"
        assert classify_instrument("123456.SZ")["board"] == "可转债"

    def test_stock_boards(self):
        assert classify_instrument("600519.SH")["type"] == "stock"
        assert classify_instrument("300750.SZ")["type"] == "stock"
        assert classify_instrument("688981.SH")["type"] == "stock"

    def test_unknown_never_raises(self):
        out = classify_instrument("")
        assert out["type"] == "unknown"
        assert classify_instrument("XYZ")["type"] == "unknown"


# ============================ 路由层：/market/search 富信息 ============================
class _FakeHub:
    """路由层假源：返回确定形状的结构（不伪造行情数值，仅搜索/详情契约字段）。"""

    async def search_stocks(self, q, limit=20):
        if "513090" in q or "香港" in q:
            return [{"code": "513090.SH", "name": "易方达香港证券ETF"}]
        if "gshy" == q:
            return [{"code": "601398.SH", "name": "工商银行"}]
        return []

    async def search_boards(self, q, limit=8, source="auto"):
        if "香港" in q:
            return ([{"code": "881305.SH", "name": "香港证券", "kind": "industry"}], "eltdx")
        return [], None

    async def get_instrument_detail(self, code):
        return {"code": code, "name": "易方达香港证券ETF"} if code == "513090.SH" else {}

    async def get_quote(self, code, source="auto", conn_id=None):
        return None

    async def get_share_capital(self, codes, source="auto"):
        return {}, None

    async def get_kline(self, code, period="1d", count=250, source="auto",
                        conn_id=None, adjust=None):
        return [], None

    async def get_moneyflow(self, code, source="auto"):
        return None, None


@pytest.fixture()
def fake_hub(monkeypatch):
    hub = _FakeHub()
    monkeypatch.setattr(mk, "get_hub", lambda: hub)
    return hub


def _run(coro):
    return asyncio.run(coro)


class TestMarketSearch:
    def test_enrich_fields(self, fake_hub):
        # 信封契约：{code:0, data:[...]}（前端 _req 按 code!==0 抛错，裸数组会静默失败）
        resp = _run(mk.market_search("513090", include_boards=False))
        assert resp["code"] == 0
        rows = resp["data"]
        assert len(rows) == 1
        r = rows[0]
        assert r["code"] == "513090.SH" and r["type"] == "etf"
        assert r["match"] == "code"
        assert r["pinyin"] == pinyin_initials("易方达香港证券ETF")

    def test_board_joint_search(self, fake_hub):
        rows = _run(mk.market_search("香港", limit=10))["data"]
        codes = [r["code"] for r in rows]
        assert "513090.SH" in codes and "881305.SH" in codes
        board_row = next(r for r in rows if r["code"] == "881305.SH")
        assert board_row["type"] == "board" and board_row["board"] == "行业板块"

    def test_empty_q(self, fake_hub):
        r = _run(mk.market_search("", include_boards=True))
        assert r["code"] == 0 and r["data"] == []


# ============================ 路由层：/market/resolve 归一 ============================
class TestMarketResolve:
    def test_code_normalized_resolved(self, fake_hub):
        r = _run(mk.market_resolve("513090"))
        assert r["code"] == 0
        d = r["data"]
        assert d["resolved"] is True
        assert d["code"] == "513090.SH"
        assert d["type"] == "etf" and d["name"]

    def test_prefixed_form(self, fake_hub):
        d = _run(mk.market_resolve("sh513090"))["data"]
        assert d["resolved"] is True and d["code"] == "513090.SH"

    def test_missing_q(self, fake_hub):
        r = _run(mk.market_resolve(""))
        assert r["code"] == 400

    def test_ambiguous_dual_candidates(self, fake_hub):
        # 000001 双候选：沪指数 / 深股票（详情无名 → 走搜索补候选，resolved 看搜索结果）
        d = _run(mk.market_resolve("000001"))["data"]
        codes = [c["code"] for c in d["candidates"]]
        assert codes[:2] == ["000001.SH", "000001.SZ"], codes
        assert d["candidates"][0]["type"] == "index"
        assert d["candidates"][1]["type"] == "stock"

    def test_normalize_code_segments(self):
        assert mk._normalize_code("600519") == ["600519.SH"]
        assert mk._normalize_code("159915") == ["159915.SZ"]
        assert mk._normalize_code("899050") == ["899050.BJ"]
        assert mk._normalize_code("sh600519") == ["600519.SH"]
        assert mk._normalize_code("600519.SZ") == ["600519.SZ"]
        # 非 000 开头的深市段不歧义
        assert mk._normalize_code("002594") == ["002594.SZ"]
        assert mk._normalize_code("300750") == ["300750.SZ"]


# ============================ 分析层：_perf_from_bars ============================
class TestPerfFromBars:
    def _bars(self, closes, hi=None, lo=None):
        hi = hi if hi is not None else max(closes)
        lo = lo if lo is not None else min(closes)
        return [{"close": c, "high": max(hi, c), "low": min(lo, c)} for c in closes]

    def test_chg_and_52w(self):
        # 70 根：5 日涨跌 = 110/100-1 = 10%
        bars = self._bars([100] * 65 + [110], hi=120, lo=90)
        p = mk._perf_from_bars(bars)
        assert p["chg_5d"] == 10.0
        assert p["chg_20d"] == 10.0
        assert p["high_52w"] == 120 and p["low_52w"] == 90
        assert p["pct_in_52w"] == round((110 - 90) / (120 - 90) * 100, 1)

    def test_insufficient_bars_no_fabrication(self):
        # 不足 60 根：chg_60d 为 None，不外推
        p = mk._perf_from_bars(self._bars([1, 2, 3]))
        assert p["chg_5d"] is None and p["chg_20d"] is None and p["chg_60d"] is None

    def test_empty(self):
        assert mk._perf_from_bars([]) is None
        assert mk._perf_from_bars(None) is None


# ============================ 路由层：/market/analysis 聚合 ============================
class TestMarketAnalysis:
    def test_missing_code(self, fake_hub):
        assert _run(mk.market_analysis(""))["code"] == 400

    def test_dimensions_and_availability(self, fake_hub, monkeypatch):
        # 估值维度：无券商连接 → unavailable（前端据此提示「估值需券商连接」）
        monkeypatch.setattr(mk, "_need", lambda conn_id=None: None)
        # K 线走 fetch_kline_cached（tools）——这里直接替换为假数据
        async def _fake_kline_cached(code, period="1d", count=250,
                                     broker_id=None, force=False,
                                     source="auto", adjust=None):
            bars = [{"close": 10.0, "high": 11.0, "low": 9.0}] * 70
            return {"bars": bars, "source": "cache", "cached_at": None}
        import tools
        monkeypatch.setattr(tools, "fetch_kline_cached", _fake_kline_cached)

        r = _run(mk.market_analysis("513090.SH"))
        assert r["code"] == 0
        d = r["data"]
        assert d["code"] == "513090.SH" and d["type"] == "etf"
        # 六维 availability 键齐备
        for k in ("snapshot", "profile", "capital", "performance", "moneyflow", "valuation"):
            assert k in d["availability"]
        assert d["availability"]["valuation"] == "unavailable"
        # 表现维度来自 K 线缓存（70 根）
        assert d["performance"]["bars_used"] == 70
        assert d["performance"]["high_52w"] == 11.0

    def test_performance_stale_eltdx_fallback(self, fake_hub, monkeypatch):
        # 券商缓存 K 线停在一年前（QMT 未同步该标的）→ TDX 公共源补最新重算
        from datetime import date, timedelta
        fresh_day = (date.today() - timedelta(days=2)).isoformat()

        async def _stale_kline_cached(code, period="1d", count=250,
                                      broker_id=None, force=False,
                                      source="auto", adjust=None):
            bars = [{"time": "20250418", "close": 1.4, "high": 1.5, "low": 1.3}] * 70
            return {"bars": bars, "source": "cache"}
        import tools
        monkeypatch.setattr(tools, "fetch_kline_cached", _stale_kline_cached)

        async def _get_kline(code, period="1d", count=250, source="auto",
                             conn_id=None, adjust=None):
            assert source == "eltdx"
            bars = [{"time": fresh_day, "close": 1.9, "high": 2.0, "low": 1.8}] * 70
            return bars, "eltdx"
        monkeypatch.setattr(fake_hub, "get_kline", _get_kline)

        d = _run(mk.market_analysis("513090.SH"))["data"]
        assert d["performance"]["as_of"] == fresh_day
        assert d["performance"].get("stale") is not True
        assert d["availability"]["performance"] == "ok"

    def test_performance_stale_marked_when_fallback_fails(self, fake_hub, monkeypatch):
        # TDX 补数也失败 → 保留陈旧表现 + 显式 stale（前端展示数据截至日，不静默旧数据）
        async def _stale_kline_cached(code, period="1d", count=250,
                                      broker_id=None, force=False,
                                      source="auto", adjust=None):
            bars = [{"time": "20250418", "close": 1.4, "high": 1.5, "low": 1.3}] * 70
            return {"bars": bars, "source": "cache"}
        import tools
        monkeypatch.setattr(tools, "fetch_kline_cached", _stale_kline_cached)

        async def _get_kline(code, period="1d", count=250, source="auto",
                             conn_id=None, adjust=None):
            raise RuntimeError("tdx down")

        monkeypatch.setattr(fake_hub, "get_kline", _get_kline)
        d = _run(mk.market_analysis("513090.SH"))["data"]
        assert d["performance"]["stale"] is True
        assert d["performance"]["as_of"] == "20250418"
        assert d["availability"]["performance"] == "stale"

    def test_perf_stale_helper(self):
        # YYYYMMDD 与 YYYY-MM-DD 两种 bar 时间形态均可判（动态日期，永不过期）
        from datetime import date, timedelta
        today = date.today()
        old = (today - timedelta(days=30)).strftime("%Y%m%d")
        recent = (today - timedelta(days=2)).strftime("%Y-%m-%d")
        assert mk._perf_stale(old) is True
        assert mk._perf_stale(recent) is False
        assert mk._perf_stale(None) is False
        assert mk._perf_stale("garbage") is False

    def test_valuation_pe_pb(self, fake_hub, monkeypatch):
        # 快照给现价 4.0，财务给 EPS 2.0 / BPS 1.0 → PE=2.0 / PB=4.0（现算不伪造）
        async def _quote(code, source="auto", conn_id=None):
            return {"code": code, "last": 4.0}

        async def _capital(codes, source="auto"):
            return {"513090.SH": {"total_shares": 1000, "circulating_shares": 500}}, None

        monkeypatch.setattr(fake_hub, "get_quote", _quote)
        monkeypatch.setattr(fake_hub, "get_share_capital", _capital)

        class _Fin:
            class gateway:
                @staticmethod
                def get_financial(code):
                    return {"EPS": 2.0, "BPS": 1.0, "ROE": 0.15, "report_time": "2026-06-30"}

            async def call(self, fn, *args):
                return fn(*args)

        monkeypatch.setattr(mk, "_need", lambda conn_id=None: _Fin())
        async def _fake_kline_cached(code, period="1d", count=250,
                                     broker_id=None, force=False,
                                     source="auto", adjust=None):
            return {"bars": [], "source": None}
        import tools
        monkeypatch.setattr(tools, "fetch_kline_cached", _fake_kline_cached)

        d = _run(mk.market_analysis("513090.SH"))["data"]
        assert d["valuation"]["pe"] == 2.0
        assert d["valuation"]["pb"] == 4.0
        assert d["availability"]["valuation"] == "ok"
        # 资本派生：换手率（vol 缺失→None）/ 市值 = 现价 × 股本
        assert d["capital"]["total_mktcap"] == 4000.0
        assert d["capital"]["float_mktcap"] == 2000.0
        assert d["capital"]["turnover_rate"] is None

    def test_negative_eps_no_fake_pe(self, fake_hub, monkeypatch):
        # 亏损股 EPS<0：PE 为 None（负 PE 无意义，不伪造）
        async def _quote(code, source="auto", conn_id=None):
            return {"code": code, "last": 4.0}
        monkeypatch.setattr(fake_hub, "get_quote", _quote)

        class _Fin:
            class gateway:
                @staticmethod
                def get_financial(code):
                    return {"EPS": -0.5, "BPS": 1.0}

            async def call(self, fn, *args):
                return fn(*args)
        monkeypatch.setattr(mk, "_need", lambda conn_id=None: _Fin())
        async def _fake_kline_cached(code, period="1d", count=250,
                                     broker_id=None, force=False,
                                     source="auto", adjust=None):
            return {"bars": [], "source": None}
        import tools
        monkeypatch.setattr(tools, "fetch_kline_cached", _fake_kline_cached)

        d = _run(mk.market_analysis("513090.SH"))["data"]
        assert d["valuation"]["pe"] is None
        # PB 仍可用（净资产正常）
        assert d["valuation"]["pb"] == 4.0
        assert d["availability"]["valuation"] == "ok"


# ============================ eltdx：ETF 并入名称表 / 惰性检索 ============================
class TestEltdxEtfMerge:
    """A 股名称表不含 ETF（51/56/58/15/16 段）→ 并入 + 惰性拉取重试的契约。"""

    ETFS = [{"code": "513090.SH", "name": "香港证券ETF"},
            {"code": "159915.SZ", "name": "创业板ETF"}]

    @pytest.fixture()
    def src(self, monkeypatch):
        """隔离类级状态的 EltdxSource 实例（保存/恢复，避免污染其他用例）。"""
        from datasource import eltdx_source as es
        saved = (dict(es.EltdxSource._name_map), dict(es.EltdxSource._search_index))
        es.EltdxSource._name_map.clear()
        es.EltdxSource._search_index.clear()
        es.EltdxSource._name_map.update({"600519.SH": "贵州茅台"})
        es.EltdxSource._rebuild_search_index()
        # 名称表持久化打桩（不写盘）
        monkeypatch.setattr(es, "_save_json_cache", lambda path, data: None)
        yield es.EltdxSource()
        es.EltdxSource._name_map.clear()
        es.EltdxSource._search_index.clear()
        es.EltdxSource._name_map.update(saved[0])
        es.EltdxSource._search_index.update(saved[1])

    def test_merge_idempotent_no_overwrite(self, src, monkeypatch):
        src._merge_into_name_map(self.ETFS)
        assert src._name_map["513090.SH"] == "香港证券ETF"
        assert src._index_match("513090", 10)[0]["code"] == "513090.SH"
        # 幂等：已有中文名不覆盖
        src._merge_into_name_map([{"code": "513090.SH", "name": "X"}])
        assert src._name_map["513090.SH"] == "香港证券ETF"
        # 无新增项时不触发持久化
        calls = []
        from datasource import eltdx_source as es
        monkeypatch.setattr(es, "_save_json_cache",
                            lambda path, data: calls.append(path))
        src._merge_into_name_map(self.ETFS)
        assert calls == []

    def test_search_lazy_etf_fallback(self, src, monkeypatch):
        async def _no_op(self=None):
            return None
        monkeypatch.setattr(src, "_ensure_name_map", _no_op)
        calls = []

        async def _fake_etf_list(limit=0):
            calls.append(limit)
            src._merge_into_name_map(self.ETFS)
            return self.ETFS

        monkeypatch.setattr(src, "get_etf_list", _fake_etf_list)
        # 513090 不在 A 股名称表 → 惰性拉取后命中
        rows = _run(src.search("513090"))
        assert rows and rows[0]["code"] == "513090.SH"
        assert rows[0]["name"] == "香港证券ETF"
        assert len(calls) == 1
        # 二次检索直接命中索引（不再触发网络）
        rows2 = _run(src.search("513090"))
        assert rows2[0]["code"] == "513090.SH" and len(calls) == 1

    def test_search_non_etf_digits_no_fetch(self, src, monkeypatch):
        async def _no_op(self=None):
            return None
        monkeypatch.setattr(src, "_ensure_name_map", _no_op)

        async def _fail_etf_list(limit=0):
            raise AssertionError("非 ETF 代码段不应触发 ETF 清单拉取")

        monkeypatch.setattr(src, "get_etf_list", _fail_etf_list)
        # 债券段 113 开头：无命中也不打网络
        assert _run(src.search("113050")) == []
        # 中文名仍可搜（A 股名称表）
        assert _run(src.search("贵州茅台"))[0]["code"] == "600519.SH"

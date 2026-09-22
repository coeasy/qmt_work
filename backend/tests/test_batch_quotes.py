"""批量行情 + 价格归一化的回归测试（2026-09-20 第 17 轮补丁）。

锁住三件事，都是**错了会让用户看到假象**的：

① ``DataSource.get_quotes`` 默认实现：并发调单只 ``get_quote``，返回
   ``{code: dict | None}`` —— 单只失败**不得**抹掉整批；

② ``TencentSource.get_quotes`` 是**真正的批量**（1 次 HTTP 拉多只），不是
   N 次单只 —— 这是报价牌冷启动时「4 只全空」的根因修复；

③ ``/market/quotes`` 的**价格归一化**：两条来源键名不一致（券商快照给
   ``price``、eltdx/tencent 给 ``last``），出口必须统一成 ``price``；
   取不到价就**删掉**该键（绝不写 0 —— ``0.00`` 会被读成「跌到 0」）。
"""
from __future__ import annotations

import asyncio
import sys
import types

import pytest

sys.path.insert(0, ".")

from datasource.base import DataSource  # noqa: E402  ④ 节要用到基类（必须先插路径）


# ---------------------------------------------------------------- ① 默认批量实现


class _Src:
    """测试用数据源：偶数 code 成功、奇数 code 抛异常。"""

    name = "fake"

    async def get_quote(self, code: str) -> dict:
        n = int(code.split(".")[0])
        if n % 2 == 1:
            raise RuntimeError(f"源异常: {code}")
        return {"code": code, "price": float(n)}


def test_default_get_quotes_keeps_partial_success():
    """默认实现：单只失败 = None，其它 code 的成功数据必须保留。"""
    from datasource.base import DataSource

    # DataSource 是 ABC，这里只取它的 get_quotes 默认实现来测
    src = _Src()

    # 把默认实现的 unbound function 绑到一个带 get_quote 的对象上
    bound = types.MethodType(DataSource.get_quotes, src)
    out = asyncio.run(bound(["000002.SZ", "000001.SZ", "000004.SZ"]))

    assert out["000002.SZ"] == {"code": "000002.SZ", "price": 2.0}
    assert out["000004.SZ"] == {"code": "000004.SZ", "price": 4.0}
    # 失败的只标 None，不污染其它
    assert out["000001.SZ"] is None


def test_default_get_quotes_empty():
    from datasource.base import DataSource

    src = _Src()
    bound = types.MethodType(DataSource.get_quotes, src)
    assert asyncio.run(bound([])) == {}


# ---------------------------------------------------------------- ② 腾讯真批量


def test_tencent_get_quotes_is_single_http():
    """腾讯批量 = 1 次 HTTP（不是 N 次）—— 冷启动 4 只全空的根因修复。

    怎么验「1 次」：桩掉 ``_get``，记录被调用的 URL 次数。若退化成 N 次
    单只，URL 次数会 > 1，这里立刻失败（**可证伪**）。
    """
    from datasource.public_sources import TencentSource

    src = TencentSource()
    calls: list = []

    # 构造批量返回体（腾讯格式：```v_sh000300="...";v_sz000001="...";```）
    payload = (
        'v_sh000300="1~沪深300~HSI300~4507.39~4460.16~4460.16~18992000000~";'
        'v_sz000001="51~平安银行~PAYH~11.70~11.61~11.59~85303800~";'
    )

    async def fake_get(self, url: str) -> str:
        calls.append(url)
        return payload

    # 桩掉节流（测试不需要真实限速），并替换 _get
    src._get = lambda url: fake_get(src, url)  # type: ignore[method-assign]
    src._throttle = classmethod(lambda cls: asyncio.sleep(0))  # type: ignore[method-assign]

    out = asyncio.run(src.get_quotes(["000300.SH", "000001.SZ"]))

    # ★ 关键断言：只打了一次 HTTP
    assert len(calls) == 1, f"应只发起 1 次 HTTP，实际 {len(calls)} 次: {calls}"
    assert "sh000300" in calls[0] and "sz000001" in calls[0], f"URL 未含全部代码: {calls[0]}"

    # 解析正确：代码带后缀、价格对得上
    assert out["000300.SH"]["name"] == "沪深300"
    assert out["000300.SH"]["last"] == 4507.39
    assert out["000001.SZ"]["name"] == "平安银行"
    assert out["000001.SZ"]["last"] == 11.70


def test_tencent_get_quotes_marks_missing_as_none():
    """批量里没拿到的 code 标 None —— 不静默丢，交给上游兜底。"""
    from datasource.public_sources import TencentSource

    src = TencentSource()
    # 只返回 000300，另一只缺席
    payload = 'v_sh000300="1~沪深300~HSI300~4507.39~4460.16~4460.16~18992000000~";'

    src._get = lambda url: asyncio.sleep(0, result=payload)  # type: ignore[method-assign]
    src._throttle = classmethod(lambda cls: asyncio.sleep(0))  # type: ignore[method-assign]

    out = asyncio.run(src.get_quotes(["000300.SH", "000001.SZ"]))
    assert out["000300.SH"] is not None
    assert out["000001.SZ"] is None, "缺失的 code 必须显式标 None"


# ---------------------------------------------------------------- ③ 价格归一化


def test_normalize_quotes_maps_last_to_price():
    """两条来源键名不一致：``last`` 必须归一化成契约名 ``price``。"""
    from app.routes.market import _normalize_quotes

    items = [{"code": "000001.SZ", "name": "平安银行", "last": 11.7}]
    _normalize_quotes(items)
    assert items[0]["price"] == 11.7, f"last 未归一化成 price: {items[0]}"


def test_normalize_quotes_drops_when_no_price():
    """取不到价就**删掉** price 键 —— 绝不写 0（0.00 会被读成「跌到 0」）。"""
    from app.routes.market import _normalize_quotes

    items = [{"code": "000001.SZ", "name": "平安银行"}]
    _normalize_quotes(items)
    assert "price" not in items[0], f"无价时不应残留 price 键: {items[0]}"
    assert items[0]["name"] == "平安银行"


def test_normalize_quotes_clears_code_as_name():
    """「名 == 代码」视为无名称，查本地表补真名（指数兜底）。"""
    from app.routes.market import _normalize_quotes

    items = [{"code": "000300.SH", "name": "000300.SH"}]
    _normalize_quotes(items)
    # 沪深300 在本地指数兜底表里 ⇒ 应被补成真名（不是代码、不是空串）
    nm = items[0]["name"]
    assert nm and nm != "000300.SH", f"指数名未被补上: {items[0]}"


def test_normalize_quotes_never_zero():
    """0 / 负数都不是有效价格 —— 归一化后不得出现。"""
    from app.routes.market import _normalize_quotes

    items = [{"code": "000001.SZ", "name": "x", "last": 0},
             {"code": "000002.SZ", "name": "y", "last": -1}]
    _normalize_quotes(items)
    for it in items:
        assert "price" not in it, f"无效价不应写入: {it}"


# ---------------------------------------------------------------- ④ Manager 层批量
#
# ★ 这一节锁的是「批量链路真的被接上了」。此前 ``get_quotes`` 只加在
#   ``DataSource`` 基类上，而 ``/market/quotes`` 拿到的是 ``DataSourceManager``
#   （两者**没有继承关系**）⇒ ``m.get_quotes`` 抛 AttributeError 被上层 except 吞掉，
#   表现为「后端日志里一次腾讯请求都没有」。所以这里必须测 manager。


class _FakeBatchSource(DataSource):
    """可计数的假源：记录批量被调用了几次、每次带了哪些 code。"""

    def __init__(self, name, mapping):
        self.name = name
        self.mapping = mapping
        self.batch_calls: list = []
        self.detail_calls: list = []

    async def get_quote(self, code: str) -> dict:
        return self.mapping.get(code) or {}

    async def get_quotes(self, codes):
        self.batch_calls.append(list(codes))
        return {c: self.mapping.get(c) for c in codes}

    async def get_details(self, codes):
        self.detail_calls.append(list(codes))
        return {c: dict(self.mapping.get(c) or {}) for c in codes}

    async def get_kline(self, code, period="1d", count=250, adjust=None):
        return []

    async def get_instrument_detail(self, code: str) -> dict:
        return dict(self.mapping.get(code) or {})

    async def get_stock_list(self):
        return []


def test_manager_get_quotes_is_one_batch_call():
    """Manager 层必须是「N 只 = 1 次批量调用」，不是 N 次单只。"""
    from datasource.registry import DataSourceManager

    src = _FakeBatchSource("fb", {
        "000001.SZ": {"code": "000001.SZ", "name": "平安银行", "last": 11.70, "pre_close": 11.61},
        "000300.SH": {"code": "000300.SH", "name": "沪深300", "last": 4507.39, "pre_close": 4460.16},
    })
    mgr = DataSourceManager().register(src)
    out = asyncio.run(mgr.get_quotes(["000001.SZ", "000300.SH"], source="fb"))

    assert len(src.batch_calls) == 1, f"应只批量调用 1 次，实际 {src.batch_calls}"
    assert out["000001.SZ"] is not None and out["000300.SH"] is not None
    # 批量链路必须走到 merge（名称/涨跌派生都在 manager 层）
    assert out["000001.SZ"]["name"] == "平安银行"
    assert out["000001.SZ"]["source"] == "fb"


def test_manager_get_quotes_fills_gaps_on_next_source():
    """降级粒度是 **code 级**：第一个源没拿到的 code 才交给下一个源。

    若实现成「整批失败才换源」，第二个源会被白调一次、且 code2 永远拿不到。
    若实现成「每个源都重拉全部」，第二个源收到的 code 列表会是 2 个（可证伪）。
    """
    from datasource.registry import DataSourceManager

    a = _FakeBatchSource("a", {"000001.SZ": {"code": "000001.SZ", "name": "A", "last": 1.0}})
    b = _FakeBatchSource("b", {"000002.SZ": {"code": "000002.SZ", "name": "B", "last": 2.0}})
    mgr = DataSourceManager().register(a).register(b)
    mgr.set_auto_chain(["a", "b"])

    out = asyncio.run(mgr.get_quotes(["000001.SZ", "000002.SZ"]))

    assert out["000001.SZ"] is not None and out["000002.SZ"] is not None, out
    assert a.batch_calls == [["000001.SZ", "000002.SZ"]]
    # ★ 第二个源只应收到「缺口」
    assert b.batch_calls == [["000002.SZ"]], f"code 级降级失效: {b.batch_calls}"


def test_manager_get_quotes_dedupes_and_suffixes():
    """入参规范化：补交易所后缀 + 去重（保序），返回键齐全。"""
    from datasource.registry import DataSourceManager

    src = _FakeBatchSource("fb", {"600519.SH": {"code": "600519.SH", "name": "贵州茅台", "last": 1257.0}})
    mgr = DataSourceManager().register(src)
    out = asyncio.run(mgr.get_quotes(["600519", "600519.SH", "600519"], source="fb"))

    assert list(out.keys()) == ["600519.SH"], f"未去重/未补后缀: {list(out.keys())}"
    assert src.batch_calls == [["600519.SH"]]


def test_manager_get_quotes_returns_all_keys_even_missing():
    """拿不到的 code 也必须出现在返回里（显式 None），不许静默丢键。"""
    from datasource.registry import DataSourceManager

    src = _FakeBatchSource("fb", {"000001.SZ": {"code": "000001.SZ", "name": "A", "last": 1.0}})
    mgr = DataSourceManager().register(src)
    out = asyncio.run(mgr.get_quotes(["000001.SZ", "999999.SZ"], source="fb"))

    assert set(out) == {"000001.SZ", "999999.SZ"}
    assert out["999999.SZ"] is None
    # 画像批量只应对**命中的** code 发起，缺失的不该浪费一次远程调用
    assert src.detail_calls == [["000001.SZ"]], f"画像范围错: {src.detail_calls}"


def _stub_tencent():
    """返回一个 `_get` 被打桩的腾讯源（记录每次 HTTP 的 URL）。"""
    from datasource.public_sources import TencentSource

    src = TencentSource()
    calls: list = []
    payload = (
        'v_sh000300="1~沪深300~HSI300~4507.39~4460.16~4460.16~18992000000~";'
        'v_sz000001="51~平安银行~PAYH~11.70~11.61~11.59~85303800~";'
    )

    async def fake_get(url: str) -> str:
        calls.append(url)
        return payload

    src._get = fake_get  # type: ignore[method-assign]
    src._throttle = classmethod(lambda cls: asyncio.sleep(0))  # type: ignore[method-assign]
    return src, calls


def test_manager_batch_end_to_end_is_single_http():
    """★ 端到端：manager 批量拉 2 只 = **1 次** HTTP（含画像，不多打一次）。

    这条是可证伪的硬证据。若退化成「行情批量 + 画像批量」两次，或退化成逐只，
    这里都会失败。此前实测后端日志里每次 API 调用都打了 **2 次** 腾讯请求，
    根因就是画像没有走 ``derive_detail`` 派生而是重新回源。
    """
    from datasource.registry import DataSourceManager

    src, calls = _stub_tencent()
    mgr = DataSourceManager().register(src)
    out = asyncio.run(mgr.get_quotes(["000300.SH", "000001.SZ"], source="tencent"))

    assert len(calls) == 1, f"端到端应只打 1 次 HTTP，实际 {len(calls)} 次: {calls}"
    assert "sh000300" in calls[0] and "sz000001" in calls[0]
    # 画像虽未回源，名称/昨收仍须补齐（否则界面只剩代码、涨跌全空）
    assert out["000300.SH"]["name"] == "沪深300"
    assert out["000300.SH"]["pre_close"] == 4460.16
    assert out["000001.SZ"]["name"] == "平安银行"
    assert out["000001.SZ"]["change_pct"] is not None


def test_public_source_derive_detail_is_free():
    """``derive_detail`` 从已有快照派生画像 —— 不发起任何请求。"""
    src, calls = _stub_tencent()
    raw = {"code": "000300.SH", "name": "沪深300", "last": 4507.39,
           "pre_close": 4460.16, "open": 4460.16}
    det = src.derive_detail(raw)
    assert det["name"] == "沪深300" and det["pre_close"] == 4460.16
    assert calls == [], "派生画像不应发起 HTTP"


def test_public_source_get_details_still_works_standalone():
    """``get_details`` 作为独立接口仍然可用（兜底路径）。"""
    src, calls = _stub_tencent()
    dets = asyncio.run(src.get_details(["000300.SH", "000001.SZ"]))
    assert len(calls) == 1
    assert dets["000300.SH"]["name"] == "沪深300"
    assert dets["000001.SZ"]["pre_close"] == 11.61


def test_overview_fetches_all_indices_in_one_batch():
    """市场概览的主要指数：**1 次批量**拿全，不是 N 次单只。

    改成批量前的实测：`asyncio.gather(get_quote(c) for 8 个指数)` —— 看着并发，
    实则被公开源的**全局 0.3s 节流锁**串成 16 次请求（单只 get_quote = 2 次 HTTP），
    耗时 **9.24s**；每只 6s 超时 ⇒ **8 个指数只有 2 个活下来**（`indices: 2`）。
    更糟的是 breadth 非空 ⇒ `unavailable` 也不置 —— 页面安静地少显示 6 个指数，
    没有一句提示，谁都不会发现。

    批量后：0.70s、8 个全拿到。这条断言同时锁住「批量」与「零单只」两件事。
    """
    from app.services.market import aggregates as agg

    calls: dict = {"batch": [], "single": []}
    CODES = ["000001.SH", "399001.SZ", "399006.SZ", "000300.SH"]

    class _Hub:
        async def get_boards(self, *a, **k):
            return [], None

        async def get_board_kline(self, *a, **k):
            return [], None

        async def get_quote(self, code, source="auto", conn_id=None):
            calls["single"].append(code)
            return None

        async def get_quotes(self, codes, source="auto", conn_id=None):
            calls["batch"].append(list(codes))
            return {c: {"code": c, "name": "X", "last": 1.0,
                        "change_pct": 0.5, "amount": 1.0} for c in codes}

    orig_hub, orig_idx = agg.get_hub, agg.configured_indices
    agg.get_hub = lambda: _Hub()
    agg.configured_indices = lambda _st: CODES
    try:
        out = asyncio.run(agg.overview(source="auto", ttl=0))
    finally:
        agg.get_hub = orig_hub
        agg.configured_indices = orig_idx

    assert calls["batch"] == [CODES], f"应只批量调用 1 次且带全量 codes：{calls['batch']}"
    assert calls["single"] == [], f"不应再逐只 get_quote：{calls['single']}"
    # ★ 关键：全部指数都要拿到（改批量前只剩 2/8）
    assert len(out["indices"]) == len(CODES), f"指数缺失: {len(out['indices'])}/{len(CODES)}"


def test_overview_fetches_three_blocks_concurrently():
    """市场概览的三块取数必须**并发**（2026-09-22 修的真问题）。

    此前是顺序 await：``get_boards``（预算 10s）→ ``get_quotes``（10s）→
    ``get_board_kline``（8s），三段相加就是冷延迟 —— curl 实测 **7.988s**。
    而前端传的 ``ttl=10`` 单位是**秒**（``TTLCache.get`` 判 ``time.time() - ts < ttl``）
    ⇒ 10 秒后必过期 ⇒ 基本上**每次**进「市场结构」页都要重付这 8 秒。

    可证伪判据：三段各睡 0.30s，
      - 并发 ⇒ 总耗时 ≈ 0.30s（断言 < 0.60s 通过）；
      - 退回顺序 await ⇒ 总耗时 ≈ 0.90s（**断言必然失败**）。
    没有这个下界，「并发」就只是注释里的一句自夸。
    """
    import time

    from app.services.market import aggregates as agg

    DELAY = 0.30
    CODES = ["000001.SH", "399001.SZ"]

    class _Hub:
        async def get_boards(self, *a, **k):
            await asyncio.sleep(DELAY)
            return [], None

        async def get_board_kline(self, *a, **k):
            await asyncio.sleep(DELAY)
            return [], None

        async def get_quotes(self, codes, source="auto", conn_id=None):
            await asyncio.sleep(DELAY)
            return {c: {"code": c, "name": "X", "last": 1.0,
                        "change_pct": 0.5, "amount": 1.0} for c in codes}

    orig_hub, orig_idx = agg.get_hub, agg.configured_indices
    agg.get_hub = lambda: _Hub()
    agg.configured_indices = lambda _st: CODES
    try:
        t0 = time.perf_counter()
        out = asyncio.run(agg.overview(source="auto", ttl=0))
        elapsed = time.perf_counter() - t0
    finally:
        agg.get_hub = orig_hub
        agg.configured_indices = orig_idx

    assert elapsed < DELAY * 2, (
        f"三块取数应并发（期望 < {DELAY * 2:.2f}s），实测 {elapsed:.3f}s —— "
        f"退化成顺序 await 了")
    # 并发不得改变结果形状：指数照旧全拿到
    assert len(out["indices"]) == len(CODES), f"并发后指数缺失: {out['indices']}"


def test_overview_stat_failure_still_raises_503():
    """并发改造**不得**把 ``get_boards`` 的失败吞成空数据（错误语义必须原样保留）。

    若吞掉，「行情源挂了」会被说成「本来就没有市场概览」——
    与项目「绝不把『不支持』说成『网络坏了』」是同一条纪律的反面。
    """
    from app.services.market import aggregates as agg
    from app.services.market.common import ServiceError

    class _Hub:
        async def get_boards(self, *a, **k):
            raise RuntimeError("TDX 挂了")

        async def get_board_kline(self, *a, **k):
            return [], None

        async def get_quotes(self, codes, source="auto", conn_id=None):
            return {}

    orig_hub, orig_idx = agg.get_hub, agg.configured_indices
    agg.get_hub = lambda: _Hub()
    agg.configured_indices = lambda _st: []
    try:
        try:
            asyncio.run(agg.overview(source="auto", ttl=0))
        except ServiceError as exc:
            assert exc.code == 503, f"应为 503，实际 {exc.code}"
            assert "TDX 挂了" in exc.message, f"应带上真实成因：{exc.message}"
        else:
            raise AssertionError("get_boards 失败必须抛 ServiceError(503)，不得静默返回空概览")
    finally:
        agg.get_hub = orig_hub
        agg.configured_indices = orig_idx


def test_merge_quote_keeps_source_pre_close_when_detail_has_none():
    """昨收三级回退：详情层没有时**不许**把源层已解析好的昨收抹成 None。

    抹掉之后 change/change_pct 推导整体失效 —— 界面会显示「有价格、涨跌全空」。
    """
    from datasource.registry import DataSourceManager

    raw = {"code": "000001.SZ", "name": "平安银行", "last": 11.70, "pre_close": 11.61}
    out = DataSourceManager._merge_quote(raw, "000001.SZ", {}, {}, "tencent")

    assert out["pre_close"] == 11.61, f"源层昨收被抹掉: {out}"
    assert out["change_pct"] is not None and abs(out["change_pct"] - 0.78) < 0.01


if __name__ == "__main__":
    pytest.main([__file__, "-q"])

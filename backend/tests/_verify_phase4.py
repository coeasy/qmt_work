"""Phase 4 逻辑验证（离线、可注入 fake，不触网不连库）。

覆盖：SourcePolicy 解析 / 默认链契约 / BarsProvider 多源降级（含 D12 三环境）/
engine.evaluate_scan / universe 解析 / 公式 DSL / 字段级基本面溯源 / 链路覆盖端点。

为避免依赖实际是否安装 akshare/baostock，本 harness 强制把可选依赖视为可用
（monkeypatch importlib.util.find_spec），使「注册态」成为链路唯一决定因素，从而
稳定复现 D12 三环境矩阵。
"""
import asyncio
import importlib
import importlib.util
import sys
import types

# 强制所有可选依赖「可用」，使链路仅由注册态/许可证决定（离线可复现）
_orig_find_spec = importlib.util.find_spec
importlib.util.find_spec = lambda name: types.ModuleType(name)

from datasource.models import Bar
from app.screener.source_policy import resolve_policy, SourcePolicy


class FakeStore:
    def __init__(self, stock_list=None, bars=None, boards=None, as_of=None):
        self._stock = stock_list or []
        self._bars = bars or {}
        self._boards = boards or {}
        self._as_of = as_of

    def get_stock_list(self):
        return self._stock

    def get_bars(self, code, period="1d", adjust="", limit=250, start=None, end=None):
        return list(self._bars.get(code, [])[-limit:])

    def get_boards(self, kind):
        return self._boards.get(kind, [])

    def latest_bar_dt(self):
        return self._as_of

    def set_meta(self, k, v):
        pass

    def upsert_boards(self, kind, items):
        return len(items)


class FakeKlineHub:
    """source -> {code: [Bar,...]}。模拟各在线源的数据供给。"""
    def __init__(self, data):
        self._data = data

    async def get_kline(self, code, period, count, *, source, adjust=None):
        return self._data.get(source, {}).get(code, None), source


class FakePlugin:
    def __init__(self, row):
        self._row = row

    async def get_fundamentals(self, codes):
        return {c: dict(self._row) for c in codes}


class FakeManager:
    def __init__(self, registered, plugins=None):
        self._reg = set(registered)
        self._plugins = plugins or {}

    def list_sources(self):
        return list(self._reg)


def fake_reg_manager(registered):
    class M:
        _commercial_mode = False
        def list_sources(self_inner):
            return list(registered)
    return M()


PASS = 0
FAIL = 0
def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


def test_source_policy():
    print("[1] SourcePolicy 解析")
    reg = {"broker", "eltdx", "baostock", "akshare"}
    r = resolve_policy("auto", "kline", commercial_mode=False, registered=reg, qmt_connected=True)
    check("auto+qmt: 链含 broker 且置首", r.chain[:1] == ("broker",) and not r.degraded)
    r2 = resolve_policy("auto", "kline", commercial_mode=False, registered=reg, qmt_connected=False)
    check("auto无qmt: broker 剔除 + degraded", "broker" not in r2.chain and r2.degraded)
    check("auto无qmt: 降级到 eltdx", r2.chain[:1] == ("eltdx",))
    r3 = resolve_policy("explicit:akshare", "kline", registered=reg, qmt_connected=False)
    check("explicit: 单源不降级", r3.chain == ("akshare",) and not r3.degraded)
    r4 = resolve_policy("qmt_only", "kline", registered={"broker"}, qmt_connected=False)
    check("qmt_only 无连接: 空链", r4.chain == () and r4.qmt_unavailable_reason)
    r5 = resolve_policy("local_only", "kline")
    check("local_only: 空链", r5.chain == ())
    r6 = resolve_policy("auto", "kline", commercial_mode=True, registered=reg, qmt_connected=False)
    check("商用: 跳过 eltdx 且含 akshare", "eltdx" not in r6.chain and "akshare" in r6.chain)


def test_default_chain():
    print("[2] 默认能力链契约")
    from datasource.providers import DEFAULT_CAPABILITY_CHAINS
    k = list(DEFAULT_CAPABILITY_CHAINS["kline"])
    check("kline 默认链前缀 = broker,eltdx,baostock,akshare",
          k[:4] == ["broker", "eltdx", "baostock", "akshare"])
    q = list(DEFAULT_CAPABILITY_CHAINS["kline_qfq"])
    check("kline_qfq 默认链同前缀", q[:4] == ["broker", "eltdx", "baostock", "akshare"])


async def test_bars_provider():
    print("[3] BarsProvider 多源降级 (D12)")
    from app.data.bars_provider import BarsProvider
    import datasource.registry as regmod

    reg = {"broker", "eltdx", "baostock", "akshare"}
    codes = ["600000.SH", "000001.SZ"]
    eltdx_bars = {c: [Bar(time="2024-01-02", open=1, high=2, low=1, close=15, volume=100)] for c in codes}
    akshare_bars = {c: [Bar(time="2024-01-02", open=1, high=2, low=1, close=15, volume=100)] for c in codes}

    # 环境①：无 QMT 有 eltdx → 走 eltdx
    orig = regmod.get_manager
    regmod.get_manager = lambda: fake_reg_manager(reg)
    try:
        hub = FakeKlineHub({"eltdx": eltdx_bars})
        bp = BarsProvider(hub=hub, store=FakeStore())
        batch, rep = await bp.get_bars_batch(codes, adjust="qfq", policy_str="auto")
        check("无QMT有eltdx: provider_used=eltdx", rep.provider_used == "eltdx")
        check("无QMT有eltdx: 两标的都有数据", all(batch[c] for c in codes))

        # 环境②：仅 akshare（eltdx 无数据）→ 走 akshare
        hub2 = FakeKlineHub({"akshare": akshare_bars})
        bp2 = BarsProvider(hub=hub2, store=FakeStore())
        batch2, rep2 = await bp2.get_bars_batch(codes, adjust="qfq", policy_str="auto")
        check("仅akshare: provider_used=akshare", rep2.provider_used == "akshare")
        check("仅akshare: 全市场可跑通", all(batch2[c] for c in codes))

        # 环境③：所有在线源空 → 回退本地（本地也空）→ degraded + local
        hub3 = FakeKlineHub({})
        bp3 = BarsProvider(hub=hub3, store=FakeStore())
        batch3, rep3 = await bp3.get_bars_batch(codes, adjust="qfq", policy_str="auto")
        check("全在线失败: 回退 local", rep3.provider_used == "local" and rep3.degraded)
        check("全在线失败: 无伪造数据", not any(batch3[c] for c in codes))

        # local_only 强制本地
        bp4 = BarsProvider(hub=hub, store=FakeStore(bars=eltdx_bars))
        batch4, rep4 = await bp4.get_bars_batch(codes, adjust="qfq", policy_str="local_only")
        check("local_only: provider_used=local", rep4.provider_used == "local")
    finally:
        regmod.get_manager = orig


def test_evaluate_scan():
    print("[4] engine.evaluate_scan")
    from app.screener.engine import evaluate_scan
    bars_hit = [Bar(time="t1", open=9, high=11, low=8, close=10, volume=1),
                Bar(time="t2", open=10, high=21, low=9, close=20, volume=1)]
    bars_miss = [Bar(time="t1", open=9, high=11, low=8, close=10, volume=1),
                 Bar(time="t2", open=10, high=11, low=9, close=5, volume=1)]
    cond = {"field": {"name": "close", "op": "gt", "value": 10, "window": -1}}
    res1, scanned1, _ = evaluate_scan(["A"], {"A": bars_hit}, cond)
    res2, scanned2, _ = evaluate_scan(["B"], {"B": bars_miss}, cond)
    check("close>10 命中", len(res1) == 1 and res1[0]["code"] == "A")
    check("close>10 不命中", len(res2) == 0)
    check("scanned 计数", scanned1 == 1 and scanned2 == 0)
    res3, _, _ = evaluate_scan(["A"], {"A": bars_hit}, cond, min_price=100)
    check("min_price 过滤前置", len(res3) == 0)


async def test_universe():
    print("[5] universe 解析")
    from app.screener.universe import resolve_universe, UniverseSpec
    store = FakeStore(stock_list=[{"code": "600000.SH", "name": "浦发"},
                                  {"code": "000001.SZ", "name": "平安"}])
    uni = await resolve_universe(UniverseSpec(kind="all"), store=store)
    check("all: 两个代码", set(uni["codes"]) == {"600000.SH", "000001.SZ"})
    uni2 = await resolve_universe(UniverseSpec(kind="custom", codes=["600000.SH"]), store=store)
    check("custom: 单代码", uni2["codes"] == ["600000.SH"])
    store3 = FakeStore(boards={"screen:my": [{"code": "600000.SH", "name": "浦发"}]})
    uni3 = await resolve_universe(UniverseSpec(kind="saved_board", board="my"), store=store3)
    check("saved_board: 取回成分", uni3["codes"] == ["600000.SH"])
    uni4 = await resolve_universe(UniverseSpec(kind="holdings"), store=store)
    check("holdings 无连接: 空 + degraded", uni4["codes"] == [] and uni4["degraded"])


def test_formula():
    print("[6] 公式 DSL")
    from app.indicators.dsl import parse
    cond = parse("C > MA(20)")
    ok = ("field" in cond) or ("indicator" in cond) or ("compare" in cond)
    check("C > MA(20) → 叶子", ok)
    cond2 = parse("RSI(14) < 30 AND C > MA(5)")
    check("复合公式含 and", "and" in cond2)


async def test_fundamentals():
    print("[7] 字段级基本面溯源")
    from app.screener.fundamentals import fetch_fundamentals
    mgr = FakeManager(
        {"broker", "eltdx", "baostock", "akshare"},
        {"akshare": FakePlugin({"pe": 12.5, "pb": 1.3, "roe": 0.15})})
    res = await fetch_fundamentals(["600000.SH", "000001.SZ"],
                                   policy_str="auto", fields=["pe", "pb", "roe"], hub=mgr)
    check("pe 字段有值", res["fields"]["pe"].get("600000.SH") == 12.5)
    check("provenance 记录源", res["provenance"]["pe"] == "akshare")
    check("000001 也有值", res["fields"]["pb"].get("000001.SZ") == 1.3)
    res2 = await fetch_fundamentals([], fields=["pe"], hub=mgr)
    check("空标的: 空结构不报错", res2["fields"]["pe"] == {})


def test_chain_override():
    print("[8] 链路覆盖 override")
    from datasource.providers import provider_catalog, DEFAULT_CAPABILITY_CHAINS
    orig = dict(provider_catalog._overrides)
    try:
        provider_catalog.set_override("kline", ["broker", "akshare"])
        ch = provider_catalog.default_chain("kline")
        check("override 生效", ch == ["broker", "akshare"])
        raised = False
        try:
            provider_catalog.set_override("kline", ["nope"])
        except ValueError:
            raised = True
        check("未知 provider 抛 ValueError", raised)
        provider_catalog.clear_override("kline")
        check("clear 后恢复默认", provider_catalog.default_chain("kline") ==
              list(DEFAULT_CAPABILITY_CHAINS["kline"]))
        check("默认契约链未被改", list(DEFAULT_CAPABILITY_CHAINS["kline"])[1] == "eltdx")
    finally:
        provider_catalog._overrides = orig


def main():
    test_source_policy()
    test_default_chain()
    asyncio.run(test_bars_provider())
    test_evaluate_scan()
    asyncio.run(test_universe())
    test_formula()
    asyncio.run(test_fundamentals())
    test_chain_override()
    print(f"\n==== Phase4 验证: PASS={PASS} FAIL={FAIL} ====")
    # 还原 find_spec
    importlib.util.find_spec = _orig_find_spec
    sys.exit(1 if FAIL else 0)

if __name__ == "__main__":
    main()

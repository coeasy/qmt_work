"""选股名称补全 + ST 过滤前置：**同一根因的两个后果**（2026-09-20 实测发现）。

## 缺陷回顾

股票池一旦走到「券商板块成分」或「本地日线」兜底，`resolve_universe` 返回的
`names` 就是空字典。此前这个空字典**同时造成两个后果**，且都静默、不报错：

1. **展示**：`engine.evaluate_scan` 里 `"name": names.get(code, "")` ⇒ 选股结果
   每一行的 `name` 都是 `""`。实测（本机真实库、未连券商）：

       GET /market/screen  → {"code":"000333.SZ","name":"","close":84.4,...}
       GET /market/screen/expr → {"code":"000002.SZ","name":"",...}

   而空串**既不是 null 也不是 undefined** ⇒ 前端 `r.name ?? "--"` 不生效，
   界面「名称」列渲染成**一片空白**，用户分不清「没有名称数据」和「界面坏了」。

2. **功能**：`engine._prefilter_codes` 用 `_is_st(names.get(c, ""))` 判 ST/退市
   ⇒ 名称全空时 `_is_st("")` 恒为 False ⇒ **勾选「排除 ST」一只都排不掉**，
   而且 `meta["excluded_st"]` 报 0，看起来像「本来就没有 ST」。
   退市股识别（`"退市" in name`）同样失效。

## 根因不是「没有数据」

运行时数据目录（与 app.db 同目录）里**本来就有** `stock_names.json`：
真实安装 215 KB / 7175 条（`688837.SH -> 信诺维`），由 eltdx 源维护并与之共用
同一份缓存。此前只是**没人去读它**。

## 本文件锁死什么

1. `_fill_names` 的语义：在线名优先、缺失才查本地表、查不到留空（绝不伪造）；
2. `resolve_universe` 的各条兜底路径**真的带上名称**；
3. ST 过滤在**有名称**时生效、在**无名称**时必然失效 —— 反向锁死「必须补名称」。
"""
import asyncio

from _phase4_support import FakeStore, make_bar

import datasource.eltdx_utils as EU

from app.screener import universe as U
from app.screener.engine import _prefilter_codes


def _run(coro):
    return asyncio.run(coro)


# ---- _fill_names 语义 ------------------------------------------------------

def test_fill_names_reads_local_name_table(monkeypatch):
    monkeypatch.setattr(EU, "_NAME_MEMO", {"600000.SH": "浦发银行",
                                          "000001.SZ": "平安银行"})
    got = U._fill_names(["600000.SH", "000001.SZ"])
    assert got["600000.SH"] == "浦发银行"
    assert got["000001.SZ"] == "平安银行"


def test_fill_names_never_fabricates_for_unknown_code(monkeypatch):
    """查不到的名称**必须留空**（零 mock：宁可空着也不编一个名字）。"""
    monkeypatch.setattr(EU, "_NAME_MEMO", {"600000.SH": "浦发银行"})
    got = U._fill_names(["600000.SH", "999999.XX"])
    assert got["600000.SH"] == "浦发银行"
    assert "999999.XX" not in got, "不得为未知代码编造名称"


def test_fill_names_keeps_online_names(monkeypatch):
    """在线源已给出的名称优先，不被本地表覆盖（本地表可能过期）。"""
    monkeypatch.setattr(EU, "_NAME_MEMO", {"600000.SH": "本地旧名"})
    got = U._fill_names(["600000.SH"], {"600000.SH": "在线新名"})
    assert got["600000.SH"] == "在线新名"


def test_fill_names_fills_only_the_missing_ones(monkeypatch):
    """混合场景：已有的保留，缺的补上。"""
    monkeypatch.setattr(EU, "_NAME_MEMO", {"000001.SZ": "平安银行"})
    got = U._fill_names(["600000.SH", "000001.SZ"], {"600000.SH": "在线名"})
    assert got == {"600000.SH": "在线名", "000001.SZ": "平安银行"}


def test_fill_names_survives_missing_cache_file(monkeypatch, tmp_path):
    """名称表文件不存在时：不得抛异常，名称留空（降级但不崩）。"""
    monkeypatch.setattr(EU, "_NAME_MEMO", None)          # 强制走真实加载
    monkeypatch.setattr(EU, "_name_cache_path",
                        lambda: tmp_path / "does_not_exist.json")
    assert U._load_name_cache() == {}
    assert U._fill_names(["600000.SH"]) == {}


# ---- 各条兜底路径必须带名称 ------------------------------------------------

def test_universe_all_local_bars_fallback_carries_names(monkeypatch):
    """★ 本地日线兜底池必须带名称（否则名称列全空 + ST 过滤失效）。"""
    monkeypatch.setattr(EU, "_NAME_MEMO", {"600519.SH": "贵州茅台",
                                          "000001.SZ": "平安银行"})
    store = FakeStore(bars={
        "600519.SH": [make_bar(1500, time_="20260918")],
        "000001.SZ": [make_bar(12, time_="20260918")],
    })
    uni = _run(U.resolve_universe(U.UniverseSpec(kind="all"), store=store))
    assert uni["provider_used"] == "local_bars"
    assert uni["names"]["600519.SH"] == "贵州茅台"
    assert uni["names"]["000001.SZ"] == "平安银行"
    # 覆盖率如实写进降级原因，便于用户判断还差多少
    assert "名称覆盖 2/2" in uni["degraded_reason"]


def test_universe_custom_carries_names(monkeypatch):
    """custom 池同样只从（恒为空的）local_stock_list 取名称 ⇒ 必须补。"""
    monkeypatch.setattr(EU, "_NAME_MEMO", {"600519.SH": "贵州茅台"})
    store = FakeStore()
    uni = _run(U.resolve_universe(
        U.UniverseSpec(kind="custom", codes=["600519.SH", "000001.SZ"]),
        store=store))
    assert uni["codes"] == ["600519.SH", "000001.SZ"]
    assert uni["names"].get("600519.SH") == "贵州茅台"


# ---- ST 过滤：正反两面 -----------------------------------------------------

def test_exclude_st_actually_excludes_when_names_present():
    """★ 有名称时「排除 ST」真的排得掉（含 ST / *ST 两种前缀写法）。"""
    codes = ["600000.SH", "000001.SZ", "600001.SH"]
    names = {"600000.SH": "浦发银行", "000001.SZ": "ST平安", "600001.SH": "*ST退市"}
    out, meta = _prefilter_codes(codes, names, {"exclude_st": True})
    assert out == ["600000.SH"], f"ST / *ST 应被排除，实际留下 {out}"
    assert meta["excluded_st"] == 2
    assert "exclude_st" in meta["applied"]


def test_exclude_st_matches_delisted_marker():
    """退市标记（名称含「退市」）同样要能被排掉。"""
    out, meta = _prefilter_codes(["600000.SH", "000002.SZ"],
                                 {"600000.SH": "浦发银行", "000002.SZ": "某某退市"},
                                 {"exclude_st": True})
    assert out == ["600000.SH"]
    assert meta["excluded_st"] == 1


def test_exclude_st_is_inert_without_names():
    """★ 反向锁死：名称全空时过滤**必然失效** —— 这正是必须在股票池补名称的原因。

    如果哪天有人把 `_fill_names` 去掉，这个测试会提醒他：ST 过滤会跟着一起失效，
    而且报告里 `excluded_st=0` 看起来完全正常。
    """
    codes = ["600000.SH", "000001.SZ"]
    out, meta = _prefilter_codes(codes, {}, {"exclude_st": True})
    assert out == codes, "无名称时 ST 过滤不可能命中任何一只"
    assert meta["excluded_st"] == 0


def test_exclude_st_reports_counts_honestly():
    """排除数量必须如实上报（用户要靠它判断过滤是否生效）。"""
    codes = ["600000.SH", "000001.SZ", "600001.SH"]
    names = {"600000.SH": "浦发银行", "000001.SZ": "ST平安", "600001.SH": "平安银行"}
    out, meta = _prefilter_codes(codes, names, {"exclude_st": True})
    assert meta["excluded_st"] == 1
    assert out == ["600000.SH", "600001.SH"]

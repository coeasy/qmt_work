"""指数名称：`_INDEX_FALLBACK_NAMES` 早就写好了，却**只被一份实现调用**（2026-09-20 实测发现）。

## 缺陷回顾

`datasource/eltdx_utils.py` 里躺着一张 `_INDEX_FALLBACK_NAMES`
（上证指数 / 深证成指 / 沪深300 …），但**声明的唯一实现** `lookup_name()` 只读
`stock_names.json` —— 而那张表由 eltdx **只维护个股**（实测 7175 条，无一条指数）
⇒ 指数永远查不到名。名称随后沿两级退化，两级都错：

1. `registry._merge_quote` 结尾是 `... or code` —— 直接拿**代码冒充名称**；
2. 券商路径 `_from_broker` 连「详情名 == 代码」的守卫都没有（`_from_plugin` 有，
   还写了注释说明），而详情层对**指数/板块**正是把 name 回落成代码
   ⇒ 它把源层已解析好的真名**覆盖掉**。

实测（本机、QMT 已连接、`signal mode=paper`）：

    GET /market/overview
    → {"code":"000300.SH","name":"000300.SH"}        ← 应为「沪深300」

后果：顶部指数条、自选股里的宽基指数一律显示成 `000300.SH` / `399001.SZ`，
用户看不出那是沪深300还是深证成指。

## 修法（两处，缺一不可）

- `lookup_name`：查表失败后回退 `_INDEX_FALLBACK_NAMES`
  （指数名单**稳定且有限**，内置既不联网也不会过时）；
- `_merge_quote`：把「详情名 == 代码」一律视为**没有名称**，逐级回退
  源层名称 → 本地名称表/指数表 → `""`（前端 `format.ts::namePair` 把空串
  渲染成代码占位：显示效果一致，但语义诚实，不再污染 `_is_st` 之类判据）。

## 本文件锁死什么

1. 指数代码能查到中文名；
2. 个股仍以名称表为准，且名称表**优先于**内置指数表；
3. 查不到返回 `""` —— **不得**回退成代码；
4. `_merge_quote` 忽略「名 == 代码」的详情名（券商路径的核心修复）；
5. 源层真名不被覆盖；
6. 全链路都没有名称时给出 `""` 而不是代码。
"""
import pytest

import datasource.eltdx_utils as EU
from datasource.registry import DataSourceManager

INDEX_CASES = {
    "000001.SH": "上证指数",
    "399001.SZ": "深证成指",
    "399006.SZ": "创业板指",
    "000300.SH": "沪深300",
    "000905.SH": "中证500",
    "000016.SH": "上证50",
    "000688.SH": "科创50",
    "899050.BJ": "北证50",
}


@pytest.fixture(autouse=True)
def _isolate_name_memo():
    """每个用例前后都还原名称表进程缓存，避免污染其他测试。"""
    saved = EU._NAME_MEMO
    EU._NAME_MEMO = None
    yield
    EU._NAME_MEMO = saved


def _set_table(mapping: dict) -> None:
    """直接桩住名称表（等价于读到一份 stock_names.json）。"""
    EU._NAME_MEMO = dict(mapping)


# ---- 1. 指数兜底 ----------------------------------------------------------

@pytest.mark.parametrize("code,name", sorted(INDEX_CASES.items()))
def test_index_codes_resolve_to_chinese_names(code, name):
    assert EU.lookup_name(code) == name


def test_index_lookup_is_case_insensitive():
    assert EU.lookup_name("000300.sh") == "沪深300"


def test_lookup_names_includes_indices():
    """批量查（选股股票池 / 行情富化共用）也要能拿到指数名。"""
    got = EU.lookup_names(["000300.SH", "399001.SZ"])
    assert got == {"000300.SH": "沪深300", "399001.SZ": "深证成指"}


# ---- 2. 个股与优先级 ------------------------------------------------------

def test_stock_name_comes_from_table():
    _set_table({"600519.SH": "贵州茅台"})
    assert EU.lookup_name("600519.SH") == "贵州茅台"


def test_table_wins_over_builtin_index_fallback():
    """名称表里若真有该指数（未来 eltdx 补上），必须以表为准。"""
    _set_table({"000300.SH": "沪深300指数（表内）"})
    assert EU.lookup_name("000300.SH") == "沪深300指数（表内）"


def test_index_fallback_works_even_when_table_is_empty():
    _set_table({})
    assert EU.lookup_name("000300.SH") == "沪深300"
    assert EU.lookup_name("600519.SH") == ""


# ---- 3. 查不到必须留空，绝不用代码冒充 ------------------------------------

@pytest.mark.parametrize("code", ["999999.SH", "123456.SZ", "000000.BJ"])
def test_unknown_code_returns_empty_not_code(code):
    assert EU.lookup_name(code) == ""
    assert EU.lookup_name(code) != code


def test_blank_code_returns_empty():
    assert EU.lookup_name("") == ""
    assert EU.lookup_name("   ") == ""


# ---- 4. _merge_quote：核心修复（券商路径） --------------------------------

def _merge(raw, code, detail, src="broker"):
    return DataSourceManager._merge_quote(dict(raw), code, {}, dict(detail), src)


def test_merge_quote_ignores_detail_name_equal_to_code():
    """★ 这是本轮修复的核心：详情层对指数把 name 回落成代码，不得采信。"""
    out = _merge({"last": 11.7}, "000300.SH", {"name": "000300.SH"})
    assert out["name"] == "沪深300"


def test_merge_quote_keeps_real_detail_name():
    out = _merge({"last": 1}, "000300.SH", {"name": "沪深300"})
    assert out["name"] == "沪深300"


def test_merge_quote_prefers_source_name_over_code_like_detail():
    """源层（eltdx get_quote）已解析出真名时，不能被「名==代码」的详情覆盖。"""
    out = _merge({"name": "上证指数"}, "000001.SH", {"name": "000001.SH"})
    assert out["name"] == "上证指数"


def test_merge_quote_never_returns_code_as_name():
    """全链路都没有名称 ⇒ 空串（前端渲染成代码占位），而不是代码冒充名称。"""
    out = _merge({"last": 1}, "999999.SH", {})
    assert out["name"] == ""
    assert out["name"] != "999999.SH"


def test_merge_quote_stock_still_uses_table():
    _set_table({"600519.SH": "贵州茅台"})
    out = _merge({"last": 1}, "600519.SH", {"name": "600519.SH"})
    assert out["name"] == "贵州茅台"

"""量比指标 `vol_ratio` + 前端「放量且收阳」预设的回归测试（2026-09-22 修）。

为什么需要它
------------
前端条件选股的预设「放量（量 > 5 日均量 1.5 倍）且收阳」此前被写成：

    and: [ {indicator:{name:"volume_ma", win:5, op:"gt", value:0}},
           {field:{name:"close",      op:"gt", value:0}} ]

成交量均线**恒为正**、收盘价**恒为正** ⇒ 条件对任何标的都成立。
实测（24 只真实标的，`GET /market/screen`）：

| 条件 | 命中 |
|---|---|
| 该预设（`volume_ma(5) > 0 AND close > 0`） | **24 / 24（全部）** |
| 对照：「收盘价 > MA20」 | 4 / 24 |

即这个预设**看起来在筛「放量」，实际什么都没筛** —— 标签在说谎，而用户无从察觉
（选股结果「全中」看起来像「今天普涨」，不像 bug）。

根因：条件树的叶子 `value` **只能是字面量**，写不出「5 日均量 × 1.5」这种**倍数**；
注册表也只提供**绝对量**的 `volume_ma`。因此补一个比值指标 `vol_ratio`（量比）。

锁定四条不变量：

1. **基准不含当日**（`shift(1)` 后再取 MA）：用含当日的均线做分母，会把放量本身算进
   基准、把信号抹平 —— 同样是「看起来在筛、其实筛不动」；
2. **无基准时返回 null，不猜**（前 `period` 根）；
3. **注册元数据正确**：`inputs=["volume"]`。写错会让 `calc` 把 close 当 volume，
   量比变成「价格比」，数值仍在合理区间 ⇒ **静默算错**；
4. **预设的条件树真的能筛掉标的**（对「无放量」「放量但收阴」两种情形都必须不命中）
   —— 这条是防「空条件」复发的总闸，测的是**求值结果**而不是字符串。
"""
from __future__ import annotations

from app.indicators import calc, get_indicator, list_indicators
from app.screener.conditions import evaluate

# 与前端 ScreenPanels.tsx 的 PRESET_EXAMPLES「放量…且收阳」逐字一致的条件树。
# ⚠️ 两处必须同步：前端改了这里也要改（本用例是它的后端契约）。
PRESET_VOLUME_UP = {
    "and": [
        {"indicator": {"name": "vol_ratio", "params": {"win": 5}, "output": "vol_ratio",
                       "window": -1, "op": "gt", "value": 1.5}},
        {"compare": {"op": "gt",
                     "left": {"kind": "field", "name": "close", "window": -1},
                     "right": {"kind": "field", "name": "open", "window": -1}}},
    ],
}


def _bars(volumes, closes=None, opens=None):
    """构造 K 线：默认收阳（close > open）。"""
    n = len(volumes)
    closes = closes or [10.0] * n
    opens = opens or [9.0] * n
    return [{"open": opens[i], "high": closes[i] + 1, "low": opens[i] - 1,
             "close": closes[i], "volume": float(volumes[i])} for i in range(n)]


def _vol_ratio(bars, period=5):
    return calc("vol_ratio", bars, period=period)["outputs"]["vol_ratio"]


# ---------------- 不变量 1 / 2：指标语义 ----------------

def test_baseline_excludes_today() -> None:
    """不变量 1：分母是**前 5 日**均量。

    最后一根量 300、前 5 根均 100 ⇒ 量比必须是 3.0。
    若基准**含当日**，分母会变成 (100*4+300)/5 = 140 ⇒ 2.14 —— 放量被自己摊薄，
    阈值 1.5 就基本筛不出东西（这正是「看起来在筛、其实筛不动」的第二种形态）。
    """
    bars = _bars([100, 100, 100, 100, 100, 100, 100, 300])
    out = _vol_ratio(bars)
    assert out[-1] == 3.0, f"量比应=300/100=3.0，实测 {out[-1]}"
    assert out[5] == 1.0, f"平稳段量比应=1.0，实测 {out[5]}"


def test_null_when_no_baseline() -> None:
    """不变量 2：前 period 根没有基准 ⇒ null（不猜、不填 0/1）。"""
    out = _vol_ratio(_bars([100] * 8), period=5)
    assert out[:5] == [None] * 5, f"前 5 根应为 null，实测 {out[:5]}"
    assert all(v is not None for v in out[5:]), f"第 6 根起应有值，实测 {out[5:]}"


def test_registered_with_volume_input() -> None:
    """不变量 3：`inputs=["volume"]` —— 写错会静默把 close 当 volume。"""
    names = [i["name"] for i in list_indicators()]
    assert "vol_ratio" in names, "vol_ratio 未注册"
    spec = get_indicator("vol_ratio")
    assert spec.inputs == ["volume"], f"inputs 应为 ['volume']，实测 {spec.inputs}"
    assert spec.outputs == ["vol_ratio"], f"outputs 实测 {spec.outputs}"
    assert spec.params and spec.params[0].name == "period"
    assert spec.params[0].default == 5


# ---------------- 不变量 4：预设条件树真的能筛 ----------------

def test_preset_matches_volume_surge_up_bar() -> None:
    """放量 + 收阳 ⇒ 命中。"""
    bars = _bars([100, 100, 100, 100, 100, 100, 100, 300])
    hit, score, total = evaluate(PRESET_VOLUME_UP, bars)
    assert (hit, score, total) == (True, 2, 2), f"应两条都命中，实测 {hit, score, total}"


def test_preset_rejects_flat_volume() -> None:
    """★ 空条件复发闸门：**没有放量**必须不命中。

    旧实现（`volume_ma(5) > 0 AND close > 0`）在这里会命中 —— 本用例就是它的墓碑。
    注意 `score` 应为 1（「收阳」那一条仍然成立），要害是 `hit is False`。
    """
    bars = _bars([100] * 8)                     # 量完全平稳，量比=1.0
    hit, score, total = evaluate(PRESET_VOLUME_UP, bars)
    assert hit is False, "无放量却命中 ⇒ 条件退化成空条件"
    assert (score, total) == (1, 2), f"只应命中「收阳」一条，实测 {score}/{total}"


def test_preset_rejects_volume_surge_but_down_bar() -> None:
    """放量但**收阴** ⇒ 不命中（「收阳」那一半必须真的在起作用）。"""
    bars = _bars([100, 100, 100, 100, 100, 100, 100, 300],
                 closes=[10.0] * 7 + [9.0], opens=[9.0] * 8)
    hit, score, total = evaluate(PRESET_VOLUME_UP, bars)
    assert hit is False, "收阴却命中 ⇒ 「收阳」条件没生效"
    assert (score, total) == (1, 2), f"应只命中放量那一条，实测 {score}/{total}"

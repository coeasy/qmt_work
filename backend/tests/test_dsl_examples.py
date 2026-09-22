"""公式 DSL 的**文档示例必须真的能解析**（2026-09-22 修）。

为什么需要它
------------
`app/indicators/dsl.py` 的模块文档里写着示例：

    "C > MA(20) AND (VOLUME > MA(VOLUME,20)*1.5 OR KDJ.K(9) > 80)"

而语法（同一份文档第 10–13 行）**没有任何算术运算**（`operand := number | symbol | call`），
且指标参数只接受数字（`MA(VOLUME,20)` 的参数是标识符）。于是**照抄示例必然报错**：

    公式解析失败：无法解析的位置 38：「*1.5 OR KDJ.…」

示例与实现不符比没有示例更糟 —— 读者会以为是自己写错了，而不是文档写错了。
本用例把「示例可解析」变成**可断言的事实**，并同时锁定**已知边界确实存在**。

锁定三条：

1. **文档里示例区的每一条公式都必须能解析**（新增示例若写错，这里立刻红）；
2. **示例条数 ≥ 3**（防止有人把示例删空让用例空转 —— 那就是不可证伪的假绿灯）；
3. **文档声明的「已知边界」必须属实**：`MA(20)*1.5`（算术）与 `MA(VOLUME,20)`
   （字段作指标参数）**当前确实不支持**。若哪天实现了，本用例会红 ——
   那是提醒你**同步更新 dsl.py 的边界说明**，不是让你删用例。
"""
from __future__ import annotations

import re

import pytest

from app.indicators import dsl

# 从文档里抠出「示例区」的引号内公式
_DOC = dsl.__doc__ or ""
_M = re.search(r"示例[^\n]*：\n(.*?)\n\n", _DOC, re.S)
EXAMPLES = re.findall(r'"([^"]+)"', _M.group(1)) if _M else []


def test_doc_examples_are_extractable() -> None:
    """护栏自身的可证伪性：抠不到示例就是用例空转，必须报错。"""
    assert _M, "dsl.py 文档里找不到「示例…：」区块（格式变了？）"
    assert len(EXAMPLES) >= 3, f"示例条数应 ≥3，实测 {len(EXAMPLES)}：{EXAMPLES}"


@pytest.mark.parametrize("expr", EXAMPLES or ["(抠不到示例)"])
def test_doc_example_parses(expr: str) -> None:
    """不变量 1：文档示例必须真的能解析。"""
    cond = dsl.parse(expr)                      # 抛 ValueError 即失败
    assert isinstance(cond, dict) and cond, f"{expr} 解析结果为空"


def test_vol_ratio_is_usable_from_dsl() -> None:
    """示例里的 `VOL_RATIO(5) > 1.5` 必须真能转成条件树（量比是 2026-09-22 新增的指标）。"""
    cond = dsl.parse("VOL_RATIO(5) > 1.5")
    leaf = cond["indicator"]
    assert leaf["name"] == "vol_ratio", leaf
    assert leaf["params"]["win"] == 5, leaf
    assert leaf["op"] == "gt" and leaf["value"] == 1.5, leaf


# ---------------- 不变量 3：文档声明的边界必须属实 ----------------

def test_documented_limitation_no_arithmetic() -> None:
    """文档说「没有算术运算」—— 必须属实。

    若你刚实现了算术（`*` / `/`），请**同步更新 dsl.py 的「已知边界」说明**
    与文档示例，然后删掉本用例 —— 不要只删用例。
    """
    with pytest.raises(ValueError):
        dsl.parse("C > MA(20)*1.5")


def test_documented_limitation_indicator_args_must_be_numeric() -> None:
    """文档说「指标参数只能是数字」—— 必须属实（`MA(VOLUME,20)` 不行）。"""
    with pytest.raises(ValueError):
        dsl.parse("VOLUME > MA(VOLUME,20)")

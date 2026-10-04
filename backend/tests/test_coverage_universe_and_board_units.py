"""覆盖率分母 + 板块单位：两处「字段声明了却恒空」的回归锁定（2026-10-04 R25）。

两处同族病征 —— **假完整**（字段在响应里、前端也渲染，值却永远不出现）：

1. ``/market/coverage`` 的 ``universe_size``：``coverage_report(universe=...)`` 是
   **可选**参数，而 REST ``/market/coverage`` 与系统任务 ``system.coverage_report``
   两个调用点**都没传** ⇒ 界面上恒为 ``null``。只有 EOD 任务传了。
2. 板块榜单的 ``unit``：非统计类（行业/概念，即榜单绝大多数行）此前写死**空串**
   ⇒「单位」这一格恒为空白。更隐蔽的是消费端 ``unit ?? "--"`` 兜不住空串
   （``"" ?? x`` 仍是 ``""``），于是渲染出一个空白单元格、看起来像前端坏了。
"""
import ast
from pathlib import Path

import pytest

from _phase7_support import bar_dt, tmp_db  # noqa: F401  —— 夹具
from datasource.quality import coverage_report

BACKEND = Path(__file__).resolve().parent.parent
_BOARDS_SRC = BACKEND / "datasource" / "eltdx_boards.py"


# ===================== 板块单位（_UNIT_BY_METRIC） =====================

def test_unit_map_never_blank_and_covers_emitted_metrics():
    """单位映射必须覆盖所有会被输出的 metric，且**任何一项都不为空**。"""
    from datasource.eltdx_boards import _UNIT_BY_METRIC

    assert _UNIT_BY_METRIC == {"count": "家", "point": "点"}
    for metric, unit in _UNIT_BY_METRIC.items():
        assert metric.strip(), "metric 不得为空"
        assert unit.strip(), f"metric={metric} 的单位不得为空串（空串会渲染成空白单元格）"


def test_board_rows_come_from_one_metric_unit_source():
    """源码级守卫：两个板块行构造点都必须走 ``_board_metric_unit``。

    这条守的是**缺陷本身**而不是当前取值。此前 ``"unit": "家" if is_stat else ""``
    这份字面量在 ``get_boards`` 与 ``search_boards`` 里**各写了一份**（第二份是加了
    守卫测试之后才被扫出来的）—— 两份复刻必然分叉。收成单一产出点后，
    「空串」这种取值在语法上就没有落脚点。
    """
    src = _BOARDS_SRC.read_text(encoding="utf-8")
    assert src.count("_board_metric_unit(is_stat)") == 2, (
        "get_boards / search_boards 两个构造点都必须调用 _board_metric_unit")
    for bad in ('"unit": ""', "'unit': ''", '"unit": "家" if',
                '"metric": "count" if'):
        assert bad not in src, f"又出现了写死的口径/单位字面量：{bad}"


# ===================== 覆盖率分母（universe_size） =====================

def _insert_bars(db, codes, dt=None):
    dt = dt or bar_dt()
    for c in codes:
        db.execute(
            "INSERT OR REPLACE INTO local_bars "
            "(code, period, adjust, dt, provider_id, close, volume, quality_state) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (c, "1d", "qfq", dt, "tdx", 10.0, 100, "raw"))
    db._conn.commit()


def _insert_stock_list(db, codes):
    for c in codes:
        db.execute(
            "INSERT OR REPLACE INTO local_stock_list (code, name, category) "
            "VALUES (?,?,?)", (c, c, "stock"))
    db._conn.commit()


def test_universe_size_prefers_stock_list(tmp_db):
    """有清单表 ⇒ 分母就是清单规模（此前恒为 null）。"""
    _insert_stock_list(tmp_db, ["600000.SH", "600001.SH", "600002.SH"])
    _insert_bars(tmp_db, ["600000.SH", "600001.SH"])
    rep = coverage_report(tmp_db, period="1d", adjust="qfq", lookback_days=10)
    assert rep["universe_size"] == 3


def test_universe_size_falls_back_to_local_bars(tmp_db):
    """纯券商环境清单表为空 ⇒ 回落到「有本地日线的标的集合」，绝不退化成 null。"""
    _insert_bars(tmp_db, ["600000.SH", "600001.SH", "300001.SZ"])
    rep = coverage_report(tmp_db, period="1d", adjust="qfq", lookback_days=10)
    assert rep["universe_size"] == 3


def test_universe_size_null_when_really_empty(tmp_db):
    """库里真的一行都没有 ⇒ 保持 None（诚实：不是 0，0 会被读成「覆盖度 0%」）。"""
    rep = coverage_report(tmp_db, period="1d", adjust="qfq", lookback_days=10)
    assert rep["universe_size"] is None


def test_explicit_universe_wins(tmp_db):
    """显式传入的分母优先（EOD 任务就是这条路径）。"""
    _insert_stock_list(tmp_db, ["600000.SH", "600001.SH", "600002.SH"])
    rep = coverage_report(tmp_db, period="1d", adjust="qfq", lookback_days=10,
                          universe=["600000.SH"])
    assert rep["universe_size"] == 1


def test_universe_size_does_not_break_report_on_query_failure():
    """分母取不到只影响一个字段，绝不能让整张报表失败。"""

    class _DbNoUniverse:
        """报表查询正常，只有**分母相关查询**抛异常。"""

        def query(self, sql, *args, **kwargs):
            # ⚠️ 判据必须精确到「分母那一句」：主报表查询里也有 `COUNT(DISTINCT code)`，
            #    用 `"DISTINCT code" in sql` 会把主查询一起拦掉，测试就测错了对象。
            if "local_stock_list" in sql or "SELECT DISTINCT code FROM local_bars" in sql:
                raise RuntimeError("boom")
            return []

    rep = coverage_report(_DbNoUniverse(), period="1d", adjust="qfq", lookback_days=5)
    assert rep["universe_size"] is None
    assert rep["lookback_days"] == 5
    assert rep["per_day"] == []

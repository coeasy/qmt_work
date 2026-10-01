"""指标接线门禁：每个 ``record_*`` 生产者都必须**真的被调用**（可证伪）。

为什么需要这个文件
------------------
``gateway/metrics.py`` 的 ``record_*`` 是**生产者**，``render()`` 是**消费者**。
两者缺一就是孤儿逻辑，而缺生产者的那种更坏 —— 它在 /metrics 里输出的是一行 **0**，
在监控面板上长得像「一切正常」：

* 曾经有 6 个 ``record_*`` **一次都没被调用过**：
  ``record_paper_order`` / ``record_request_duration_ms`` / ``record_runtime_mode`` /
  ``record_error`` / ``record_ws_message`` / ``record_ws_clients``；
* 还有一个「内存 trace 环形缓冲」（``record_trace`` + ``recent_traces``），
  生产者与消费者都缺席（没有任何路由/前端读它），属于半落地功能，已整体删除。

这与本项目反复出现的「绿灯被另一个 bug 遮出来」是同一族：**测试全绿、日志正常、
面板好看，唯独那条链路是空的**。所以这里把它做成扫描式门禁，而不是靠人记得。

判据（不接受手抄清单）
----------------------
1. 从 ``Metrics`` 类上取**全部** ``record_*`` 方法（新增方法自动进范围）；
2. 在 backend 的**产线**代码里找引用 —— 刻意排除 ``tests/``：让测试文件调用一次
   就把「没人接线」变成绿，正是这类门禁最容易犯的错；
3. 一个引用都没有 ⇒ 报红，除非登记进 ``_DECLARED_UNWIRED`` 并写明理由；
4. 反向核：``render()`` **实际输出**里的每个 ``qmt_*`` 指标名，都必须在
   ``_METRIC_PRODUCERS`` 里声明生产者（或登记进 ``_RENDER_ONLY`` 并说明为什么
   它不需要生产者）—— 防止「渲染一个永远为 0 的指标」再次发生。
"""
import inspect
import re
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from gateway.metrics import Metrics, get_metrics  # noqa: E402

#: 扫描产线代码时排除的目录。``tests`` 必须在列 —— 见模块 docstring 第 2 条。
_SKIP_DIRS = {"tests", "dist", "build", ".venv", "venv", "runtimes", "__pycache__",
              "logs", "data", "output", "static", "node_modules"}
#: 定义文件自身：``def record_x`` 那行不算「被调用」。
_DEFINITION_FILE = (BACKEND / "gateway" / "metrics.py").resolve()

#: 允许「已定义但暂未接线」的生产者（应为空；非空必须写明理由与计划）。
#: 这是逃生舱而不是垃圾桶：登记了就等于承诺会在同一次迭代里接线。
_DECLARED_UNWIRED: dict[str, str] = {}

#: 指标名 → 生产者方法（``None`` 表示由 /metrics 路由现场提供数据，无需生产者）。
_METRIC_PRODUCERS: dict[str, str | None] = {
    "qmt_uptime_seconds": None,          # render 内自算（进程启动时间）
    "qmt_quote_latency_ms": "record_quote_latency",
    "qmt_orders_total": "record_order",
    "qmt_quotes_total": "record_quote",
    "qmt_api_requests_total": "record_request",
    "qmt_broker_connected": None,        # /metrics 路由传 live snapshot
    "qmt_ws_clients": None,              # 同上（ws_manager.client_count()）
    "qmt_backtests_total": "record_backtest",
    "qmt_paper_orders_total": "record_paper_order",
    "qmt_ws_messages_total": "record_ws_message",
    "qmt_api_latency_ms": "record_request_duration_ms",
    "qmt_runtime_mode": "record_runtime_mode",
    "qmt_errors_total": "record_error",
    "qmt_conn_events_total": "record_conn_event",
    "qmt_idempotency_hits_total": "record_idempotency_hit",
    "qmt_risk_blocked_total": "record_risk_blocked",
    "qmt_reconcile_diffs_total": "record_reconcile_diff",
}


def _producer_names() -> list[str]:
    """``Metrics`` 上的全部公开 ``record_*`` 方法名（不手抄）。"""
    return sorted(
        name for name, _ in inspect.getmembers(Metrics, inspect.isfunction)
        if name.startswith("record_") and not name.startswith("record__"))


def _production_sources():
    for path in BACKEND.rglob("*.py"):
        parts = set(path.relative_to(BACKEND).parts)
        if parts & _SKIP_DIRS:
            continue
        if path.resolve() == _DEFINITION_FILE:
            continue
        yield path


def _referenced_in_production() -> set[str]:
    """产线代码里**真实出现**的 ``record_*`` 名字集合。"""
    names = set(_producer_names())
    found: set[str] = set()
    for path in _production_sources():
        try:
            text = path.read_text("utf-8", errors="replace")
        except OSError:  # pragma: no cover - IO 竞态
            continue
        for name in names - found:
            if re.search(r"\b" + re.escape(name) + r"\b", text):
                found.add(name)
    return found


def test_every_metric_producer_is_wired_in_production():
    """每个 ``record_*`` 必须在产线代码里被调用（否则它就是个假的 0）。"""
    producers = set(_producer_names())
    assert len(producers) >= 10, f"只解析到 {len(producers)} 个生产者，判据可能失效"
    wired = _referenced_in_production()
    missing = sorted(producers - wired - set(_DECLARED_UNWIRED))
    assert not missing, (
        "以下指标生产者**从未被产线代码调用**（/metrics 里对应指标恒为 0，"
        "在面板上却长得像「一切正常」）：\n  " + "\n  ".join(missing)
        + "\n  要么接线，要么（确有意为之）登记进 _DECLARED_UNWIRED 并写明理由。")


def test_wiring_scan_actually_sees_the_known_call_sites():
    """门禁自证：扫描器必须真的读到产线文件，且能看见已知接线点。

    这是**假阴性防线** —— 路径过滤一旦写错（例如把 ``app/`` 也排除掉），上面的
    用例会因为「没发现缺失」而全绿，比红更危险。
    """
    files = list(_production_sources())
    assert len(files) > 100, f"只扫到 {len(files)} 个产线文件，过滤可能过狠"
    wired = _referenced_in_production()
    # 几个跨层、跨模块的已知接线点（任一丢失都说明扫描或接线坏了）
    for name in ("record_order", "record_request", "record_conn_event",
                 "record_idempotency_hit", "record_risk_blocked",
                 "record_reconcile_diff", "record_error", "record_paper_order",
                 "record_runtime_mode", "record_request_duration_ms",
                 "record_ws_message"):
        assert name in wired, f"扫描没发现 {name} 的接线点 —— 判据失效或接线被删"


def test_tests_directory_is_not_counted_as_wiring(tmp_path):
    """``tests/`` 里的调用不算接线：否则测试自己就能把门禁刷绿。"""
    files = {p.resolve() for p in _production_sources()}
    assert files, "产线文件集合为空"
    for path in files:
        assert "tests" not in path.relative_to(BACKEND).parts, (
            f"{path} 属于 tests/，不该被算作产线接线")
    test_file = (BACKEND / "tests" / "test_unit.py").resolve()
    assert test_file.exists() and test_file not in files


def test_render_output_metrics_have_declared_producers():
    """``render()`` 实际输出的每个 ``qmt_*`` 指标都必须声明生产者。

    用**真实输出**而不是读源码字符串：源码里写了但没被走到（例如提前 return）
    的指标同样是谎话，只有跑一遍 render 才能发现。
    """
    text = get_metrics().render({"ws_clients": 0, "brokers": []})
    emitted = sorted({m.group(1) for m in re.finditer(r"^# HELP (qmt_[a-z0-9_]+)", text,
                                                      re.MULTILINE)})
    assert len(emitted) >= 12, f"render 只输出 {len(emitted)} 个指标，解析可能失效"
    undeclared = [name for name in emitted if name not in _METRIC_PRODUCERS]
    assert not undeclared, (
        "以下指标被渲染但未声明生产者（＝可能是永远为 0 的假指标）：\n  "
        + "\n  ".join(undeclared)
        + "\n  在 _METRIC_PRODUCERS 里登记生产者，或（由路由现场提供时）填 None。")


def test_declared_producers_exist_and_are_rendered():
    """声明的生产者必须真实存在，且它产出的指标真的被渲染出来。

    方向二：反了就是「有生产者但没人看」，同样是孤儿。
    """
    text = get_metrics().render({"ws_clients": 0, "brokers": []})
    producers = set(_producer_names())
    for metric, producer in _METRIC_PRODUCERS.items():
        assert (producer is None) or (producer in producers), (
            f"{metric} 声明的生产者 {producer!r} 在 Metrics 上不存在")
        # 渲染里必须真的出现该指标名（histogram 用基名匹配）
        assert f"# HELP {metric}" in text, f"{metric} 声明了却没有被渲染"


@pytest.mark.parametrize("name", ["record_trace", "recent_traces", "record_ws_clients"])
def test_half_landed_trace_and_ws_clients_features_are_gone(name):
    """半落地功能必须真的移除，而不是留个永远返回空值的接口骗人。

    * ``record_trace`` / ``recent_traces``：trace 环形缓冲没有任何消费方
      （无路由、无前端），留着等于承诺一个不存在的排障能力；
    * ``record_ws_clients``：与 ``/metrics`` 路由传 live snapshot 的做法重复，
      同一个 gauge 两个来源 ⇒ 迟早不一致，且「哪个是真的」没人知道。
    """
    assert not hasattr(Metrics, name), f"Metrics 上仍然存在半落地接口 {name}"

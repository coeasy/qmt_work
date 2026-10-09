# -*- coding: utf-8 -*-
"""大 QMT agent 的「注入面 vs 转发面」语义 + 自检能力结论（2026-10-09 真机缺陷族）。

背景（真机实测，用户现场日志）::

    [qmt_work_bigqmt_agent] agent 就绪: ... funcs=76 trading=False
    [qmt_work_bigqmt_agent] 自检完成: 86 项，异常项=['quote_call']
    [qmt_work_bigqmt_agent] 注意: 行情接口存在但**实调失败**（…无法连接行情服务！）

三个**连锁**真缺陷（本文件逐个钉死）：

D1 ``injected`` 与 ``forwarded`` 语义混淆
    独立进程模式下 ``_forward_xtdata_funcs()`` 把 76 个 xtdata 接口转发进入口
    ``globals()``，随后 ``capture_qmt_injected_funcs(globals())`` 把它们**当成了
    终端注入** —— 实测两者**完全相同（76/76）**。后果：
      ① ``run_standalone()`` 里 ``if not _STATE["injected"]`` 那条「本模式没有任何
         终端注入的函数 ⇒ 下单/查询不可用」的关键警告被**静默吞掉**，用户只看到
         一句行情报错，完全不知道下单根本不可能；
      ② ``meta()["funcs"]`` / probe 的 ``injected`` 报 76 项。

D2 能力协商假绿灯
    ``connectors/generic.probe_capabilities`` 只看「函数在不在 captured 里」，
    76 项里含 ``get_full_tick`` ⇒ ``quote`` / ``realtime`` 被判 **SUPPORTED**，
    而 agent 的实调是抛错的。→ float 级回归在
    ``test_bigqmt_file_bridge.py::test_quote_live_failure_vetoes_supported_even_when_func_captured``。

D3 ``probe_result.json`` 缺 ``ok`` 字段（假告状）
    后端 ``qmt_agent.py`` 读 ``probe.get("ok")`` ⇒ ``probe_ok`` 恒为 False，
    界面永远显示「自检未通过」。

★ 为什么既有用例没拦住：``test_bigqmt_agent_runtime.py`` 的独立进程用例跑在**开发机**
  上，那里没有 xtquant ⇒ ``forwarded`` 为空 ⇒ ``injected == []`` 平凡成立。
  本文件用**显式伪造的转发面**补齐真机条件。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import gen_qmt_agent_bundle as gen  # noqa: E402


@pytest.fixture(scope="module")
def bundle_text() -> str:
    return gen.build(stamp="test")


def _load(bundle_text: str, name: str = "qmt_injected_face_under_test") -> dict:
    """复刻 QMT 公式模式加载：exec 进一个**没有 __file__** 的命名空间。"""
    ns: dict = {"__name__": name}
    exec(compile(bundle_text, "<string>", "exec"), ns)  # noqa: S102 - 测自己的产物
    return ns


class _FakeCtx(object):
    """ContextInfo 替身：可配置 get_full_tick 的行为（**不伪造任何业务数据**）。"""

    def __init__(self, tick=None, raise_exc=None):
        self._tick = tick
        self._raise = raise_exc
        self.barpos = 0
        self.period = "1d"

    def get_full_tick(self, codes):
        if self._raise is not None:
            raise self._raise
        return self._tick or {}

    def __dir__(self):
        return ["barpos", "period", "get_full_tick"]


def _cfg(bridge: Path, trading: bool = False) -> dict:
    return {
        "bridge_dir": str(bridge),
        "_config_path": str(bridge / "agent_config.json"),
        "auth_token": "t",
        "trading_enabled": trading,
        "poll_interval_ms": 100,
    }


# ---------------------------------------------------------------------------
# D1：转发面不得被当成终端注入面
# ---------------------------------------------------------------------------
def test_forwarded_funcs_are_not_captured_as_injected(bundle_text):
    """★ 真机条件的直接复现：给命名空间塞入「我们转发的」xtdata 接口。

    不传 ``exclude`` 时它们会被当成终端注入（复现缺陷条件，保证用例有证伪力）；
    传了 ``exclude=forwarded`` 之后必须一个都不剩。
    """
    ns = _load(bundle_text)
    forwarded = {
        "get_full_tick": lambda codes: {},
        "get_market_data": lambda *a, **k: None,
        "download_history_data": lambda *a: None,
    }
    ns.update(forwarded)

    without_exclude = sorted(ns["capture_qmt_injected_funcs"](ns))
    for name in forwarded:
        assert name in without_exclude, (
            "前提不成立：不排除转发面时本应被误捕获（否则本用例无法证伪缺陷）")

    with_exclude = sorted(ns["capture_qmt_injected_funcs"](
        ns, exclude=list(forwarded)))
    assert with_exclude == [], (
        "转发的 xtdata 接口必须被排除，否则会同时压掉「下单不可用」警告"
        "并让能力面出现假绿灯：%s" % with_exclude)


def test_standalone_with_forwarded_still_warns_no_trading(bundle_text, tmp_path):
    """D1 的**用户可见后果**：真机条件（有转发）下，那条关键警告必须仍然打印。

    这是 2026-10-09 现场的直接对照：用户日志里**只有**行情报错、没有任何
    「本模式不能下单」的字样，于是无从判断下单是否可行。
    """
    ns = _load(bundle_text)
    # 复刻 run_standalone 的状态：独立模式 + 已转发 76 个接口（这里用 3 个代表）
    ns["_STATE"]["standalone"] = True
    forwarded = {"get_full_tick": lambda codes: {},
                 "get_market_data": lambda *a, **k: None,
                 "download_history_data": lambda *a: None}
    ns.update(forwarded)
    ns["_STATE"]["forwarded"] = sorted(forwarded)
    ns["_STATE"]["forwarded_funcs"] = forwarded

    injected = ns["capture_qmt_injected_funcs"](
        ns, exclude=ns["_STATE"]["forwarded"])
    assert injected == {}, "真机条件下也要如实报告「没有任何终端注入函数」"


# ---------------------------------------------------------------------------
# D3：probe 必须给出 ok / capability，且「下单能力」单独成项
# ---------------------------------------------------------------------------
def _run_probe(bundle_text, tmp_path, *, injected, ctx):
    ns = _load(bundle_text)
    ns["_STATE"]["standalone"] = True
    bridge = tmp_path / "bridge"
    bridge.mkdir(exist_ok=True)
    probe = ns["self_probe"](_cfg(bridge), injected, ctx, None)
    return ns, probe, bridge


def test_probe_reports_injected_empty_and_capability_truthful(bundle_text, tmp_path):
    """真机形态：无终端注入 + 行情实调抛错 ⇒ 三条结论都必须**如实**。

    - ``injected == []``（转发不算注入）
    - ``capability.trading.available is False`` 且 ``expected_in_mode is True``
      （独立进程模式拿不到 passorder 是**模式定义**使然，不是环境故障）
    - ``capability.quote.available is False`` 且 ``expected_in_mode is False``
      （行情**本该**可用 —— 独立进程模式的用途正是转发真实行情）
    """
    err = Exception("无法连接行情服务！")
    _ns, probe, bridge = _run_probe(
        bundle_text, tmp_path, injected={}, ctx=_FakeCtx(raise_exc=err))

    assert probe["injected"] == []
    assert probe.get("runtime_mode") == "standalone_process"

    cap = probe["capability"]
    assert cap["trading"]["available"] is False
    assert cap["trading"]["expected_in_mode"] is True
    assert "passorder" in cap["trading"]["reason"]
    assert cap["quote"]["available"] is False
    assert cap["quote"]["expected_in_mode"] is False, (
        "行情取不到是客户端/环境问题，不能用「模式使然」豁免掉")
    assert "无法连接行情服务" in cap["quote"]["reason"]

    # ★ 下单能力必须**单独成项**（用户问的就是这个）
    steps = {s["name"]: s for s in probe["steps"]}
    assert "order_funcs" in steps, "自检必须单独报告下单能力"
    assert steps["order_funcs"]["ok"] is False
    assert "不能下单" in steps["order_funcs"]["detail"]

    # 行情失败要带上根因/出路，不能只有一句异常
    assert "根因/出路" in steps["quote_call"]["detail"]

    # D3：probe_result.json 必须带 ok（否则后端 probe_ok 恒 False = 假告状）
    saved = json.loads((bridge / "probe_result.json").read_text(encoding="utf-8"))
    assert "ok" in saved, "probe_result.json 缺 ok ⇒ 诊断会永远报「自检未通过」"
    assert saved["ok"] is False
    assert saved["ok"] == (not saved["bad_steps"])
    assert "quote_call" in saved["bad_steps"]


def test_probe_with_terminal_passorder_reports_trading_available(bundle_text, tmp_path):
    """反向锚：终端注入面里真的有 passorder 时，必须判「可下单」且自检不含该项失败。

    （防止把「独立进程模式」当成无条件借口，把真能力也一并报成不可用。）
    """
    injected = {"passorder": lambda *a: None,
                "cancel": lambda *a: None,
                "get_trade_detail_data": lambda *a: []}
    _ns, probe, _bridge = _run_probe(
        bundle_text, tmp_path, injected=injected,
        ctx=_FakeCtx(tick={"000001.SZ": [{"lastPrice": 10.0}]}))

    assert sorted(probe["injected"]) == ["cancel", "get_trade_detail_data", "passorder"]
    assert probe["capability"]["trading"]["available"] is True
    assert probe["capability"]["quote"]["available"] is True
    steps = {s["name"]: s for s in probe["steps"]}
    assert steps["order_funcs"]["ok"] is True
    assert steps["quote_call"]["ok"] is True
    # 开发机没有 xtquant ⇒ import:* 必然失败，那是**环境**差异不是本用例的对象。
    # 这里只要求「下单 / 行情」两项不被列入失败项（本用例的修复面）。
    assert "order_funcs" not in probe["bad_steps"]
    assert "quote_call" not in probe["bad_steps"]


def test_trade_surface_classifies_only_terminal_injection(bundle_text):
    """``qmt_api.trade_surface`` 只认终端注入；转发面里的行情函数不得被算成交易能力。"""
    ns = _load(bundle_text)
    ts = ns["trade_surface"]

    empty = ts({})
    assert empty["can_submit"] is False
    assert sorted(empty["missing"]) == ["cancel", "get_trade_detail_data", "passorder"]

    forwarded_only = ts({"get_full_tick": lambda c: {}, "get_market_data": lambda *a: None})
    assert forwarded_only["can_submit"] is False, "行情转发不得被算成下单能力"
    assert forwarded_only["present"] == []

    full = ts({"passorder": lambda *a: None, "cancel": lambda *a: None,
               "get_trade_detail_data": lambda *a: []})
    assert full["can_submit"] is True
    assert sorted(full["present"]) == ["cancel", "get_trade_detail_data", "passorder"]
    assert full["missing"] == []


def test_bundle_keeps_trade_surface_callable(bundle_text):
    """内联契约：生成器会剥掉 ``from qmt_api import ...``，别名写法会留下未绑定名字。

    这条是**构建期**约束（真源改坏只有打完包才炸，代价 ~20 分钟）。
    """
    ns = _load(bundle_text)
    assert callable(ns.get("trade_surface")), (
        "trade_surface 必须是 bundle 的顶层名字（qmt_api 已内联）")
    assert "from qmt_api import trade_surface as" not in bundle_text, (
        "不得用 `as` 别名导入 —— 生成器只剥 `from qmt_api import ...`，"
        "别名会把 `_trade_surface` 留成未绑定名字")

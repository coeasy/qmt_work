"""P1-2 双状态容器合一（V11 R5）—— 结构单一性与「无静默 None」护栏。

背景：此前有两份「服务槽位清单」——
``core/state.AppState``（模块级单例，bootstrap 各阶段写）与
``core/context.AppContext``（启动时由 ``build_from_state`` 拷贝出的**快照**，路由读）。
两者字段必须人工保持一致，且快照不会随后续写入更新（仅 ``lifecycle_ready`` /
``lifecycle_stopping`` 靠 ``main.py`` 手工回同步）。

由此产生的**实际故障**（2026-09-15 实测）：
- ``POST /api/v1/alerts/test`` 恒返回 503「告警引擎未初始化」——
  ``bootstrap/phase_db`` 写 ``state.alert_engine``，而路由读快照 ``ctx.alert_engine``；
  该名字**两份清单都没声明**，读时经 ``AppContext.__getattr__`` 兜底**静默返回 None**。
- ``GET /api/v1/market/kline/sync-status`` 的 ``last_run`` 恒为 ``null``——
  同理，``gateway.market_sync`` 写 ``state._market_sync_last``，快照里没有。

现改为：槽位只在 ``AppContext`` 定义一处，``AppState`` 继承它、只补生命周期方法；
``main`` 直接 ``set_active_context(state)``（**同一对象**，无快照）；并移除
``__getattr__`` 静默 None 兜底。本文件钉住上述四点。
"""
from __future__ import annotations

import dataclasses
import pathlib
import re

import pytest

from core.context import AppContext, active_context, set_active_context
from core.state import AppState, state

_BACKEND = pathlib.Path(__file__).resolve().parent.parent
_SKIP_PARTS = {"runtimes", "dist", "__pycache__", ".venv", "tests"}


@pytest.fixture()
def restore_active_ctx():
    """保存/恢复进程级 active context（避免污染同进程其它用例）。"""
    from core import context as ctx_mod
    saved = ctx_mod._ACTIVE
    yield
    ctx_mod._ACTIVE = saved


# ---------------------------------------------------------------------------
# ① 槽位只在 AppContext 定义一处
# ---------------------------------------------------------------------------

def test_appstate_declares_no_extra_slots():
    """AppState 不得再自行声明槽位 —— 槽位只在 AppContext 定义一处。

    若有人把某个槽位写回 AppState（重复定义），这里会红。
    """
    ctx_fields = set(AppContext.__dataclass_fields__)
    state_fields = set(AppState.__dataclass_fields__)
    assert state_fields == ctx_fields, (
        "AppState 与 AppContext 的槽位集合不一致："
        f"AppState 独有={sorted(state_fields - ctx_fields)}，"
        f"AppContext 独有={sorted(ctx_fields - state_fields)}")


def test_state_singleton_is_an_appcontext():
    """单例 `state` 本身就是 AppContext（路由 isinstance 检查仍成立）。"""
    assert isinstance(state, AppContext)
    assert isinstance(state, AppState)
    assert issubclass(AppState, AppContext)


def test_appstate_class_has_no_slot_attributes():
    """AppState 类体只应有方法，不应有槽位类属性（槽位来自 AppContext）。"""
    ctx_fields = set(AppContext.__dataclass_fields__)
    own = {k for k, v in vars(AppState).items()
           if not k.startswith("__") and not callable(v)}
    assert not (own & ctx_fields), (
        f"AppState 重复声明了槽位类属性：{sorted(own & ctx_fields)}")


# ---------------------------------------------------------------------------
# ② 无「未知属性静默 None」兜底
# ---------------------------------------------------------------------------

def test_unknown_slot_raises_attribute_error():
    """拼错槽位必须报错，而不是静默 None（静默 None 会掩盖整类失效）。"""
    with pytest.raises(AttributeError):
        state.no_such_slot  # noqa: B018
    with pytest.raises(AttributeError):
        AppContext().no_such_slot  # noqa: B018
    assert not hasattr(AppContext(), "no_such_slot")


def test_getattr_with_default_still_works_for_optional_slots():
    """官方推荐写法 getattr(x, name, None) 仍可用（不影响既有可选槽位读取）。"""
    assert getattr(state, "no_such_slot", None) is None
    assert getattr(state, "db", "sentinel") is state.db


# ---------------------------------------------------------------------------
# ③ 生产路径：active_context() 与 state 是**同一对象**（无快照）
# ---------------------------------------------------------------------------

def test_active_context_is_the_state_singleton(restore_active_ctx):
    """main.lifespan 的注册方式下，路由读到的就是单例本身。"""
    set_active_context(state)
    assert active_context() is state
    # 关键：启动后写入的槽位，读侧立即可见（快照时代读不到）
    marker = object()
    state.extras["_probe_marker"] = marker
    try:
        assert active_context().extras["_probe_marker"] is marker
    finally:
        state.extras.pop("_probe_marker", None)


def test_writes_after_registration_are_visible(restore_active_ctx):
    """回归：注册之后再写槽位，读侧必须看得到（快照时代看不到）。"""
    set_active_context(state)
    saved = state.alert_engine
    try:
        sentinel = object()
        state.alert_engine = sentinel
        assert active_context().alert_engine is sentinel
    finally:
        state.alert_engine = saved


def test_tests_can_still_inject_isolated_context(restore_active_ctx):
    """测试仍可注入自建 AppContext 做隔离（不影响单例）。"""
    fake = AppContext()
    fake.db = "FAKE"
    set_active_context(fake)
    assert active_context() is fake
    assert active_context().db == "FAKE"
    assert state.db != "FAKE"


# ---------------------------------------------------------------------------
# ④ 结构护栏：bootstrap 写入的槽位必须都已声明
# ---------------------------------------------------------------------------

def _slots_written_on_state():
    """扫描源码里所有 `state.<attr> = ...` / `setattr(state, "<attr>", ...)`。"""
    found: dict[str, set[str]] = {}
    for p in _BACKEND.rglob("*.py"):
        if any(part in _SKIP_PARTS for part in p.parts):
            continue
        try:
            src = p.read_text(encoding="utf-8")
        except OSError:
            continue
        rel = str(p.relative_to(_BACKEND))
        for m in re.finditer(
                r"(?<![\w.])(?:state|self\.state)\.([A-Za-z_][A-Za-z0-9_]*)\s*=(?!=)", src):
            found.setdefault(m.group(1), set()).add(rel)
        for m in re.finditer(
                r"setattr\((?:self\.)?state,\s*[\"']([A-Za-z_][A-Za-z0-9_]*)", src):
            found.setdefault(m.group(1), set()).add(rel)
    return found


def test_every_slot_written_on_state_is_declared():
    """**核心结构护栏**：任何被 `state.X = ...` 写入的槽位都必须在 AppContext 声明。

    未声明 → 快照时代读侧静默 None（``alert_engine`` 就是这么失效的）。
    这条把「新增槽位忘了声明」变成 CI 红，而不是线上静默失效。
    """
    declared = set(AppContext.__dataclass_fields__)
    written = _slots_written_on_state()
    undeclared = {k: sorted(v) for k, v in written.items() if k not in declared}
    assert not undeclared, (
        "以下槽位被写入但未在 AppContext 声明（请补声明，勿依赖动态属性）：\n"
        + "\n".join(f"  {k}: {', '.join(v)}" for k, v in sorted(undeclared.items())))


def test_alert_engine_and_market_sync_last_are_declared():
    """两个曾静默失效的槽位必须保持已声明（回归锚点）。"""
    fields = set(AppContext.__dataclass_fields__)
    assert "alert_engine" in fields, "alert_engine 未声明 → POST /alerts/test 会恒 503"
    assert "order_watchdog" in fields
    assert "_market_sync_last" in fields, "未声明 → sync-status 的 last_run 恒为 null"
    assert "_eod_last_run_date" in fields
    assert "_quote_bound_conns" in fields


# ---------------------------------------------------------------------------
# ⑤ AppContext.update() 的落点语义
# ---------------------------------------------------------------------------

def test_update_declared_slot_sets_field_unknown_goes_to_extras():
    ctx = AppContext()
    ctx.update(db="D", not_a_slot="E")
    assert ctx.db == "D"
    assert "not_a_slot" not in vars(ctx)          # 不动态建属性
    assert ctx.extras["not_a_slot"] == "E"        # 也不静默丢弃


def test_lifecycle_methods_live_on_appstate_only():
    """生命周期方法在 AppState；纯 AppContext 不应有（职责分离）。"""
    st = AppState()
    assert hasattr(st, "mark_ready") and hasattr(st, "begin_shutdown")
    assert not hasattr(AppContext(), "mark_ready")
    st.begin_startup()
    for n in ("db", "engines", "watchdogs", "replay", "misc"):
        st.mark_phase(n, "ready")
    assert st.mark_ready() is True
    st.begin_shutdown()
    assert st.lifecycle_ready is False and st.lifecycle_stopping is True

"""V9 Phase 5 DoD：Lifecycle Required/Optional 分层。

- Required 阶段失败 → run_phases 抛 RuntimeError（阻断启动）；
- Optional 阶段失败 → 降级继续（phase_status=error，不抛）；
- QMT（broker, Optional）失败 → mark_ready() 仍为 True（「QMT 失败不失去 READY」）；
- /ready 的 started 语义 = Required 阶段全部 ready（P1-18 修正恒真）。
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.bootstrap.lifecycle import PHASE_LEVELS, run_phases  # noqa: E402
from core.state import REQUIRED_PHASES, AppState  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


async def _ok(app):
    return {}


async def _boom(app):
    raise RuntimeError("boom")


def test_required_phase_failure_blocks_startup():
    state = AppState()
    phases = [("db", _ok), ("engines", _boom)]
    try:
        _run(run_phases(None, phases, state.mark_phase))
        raise AssertionError("required failure must raise")
    except RuntimeError as exc:
        assert "engines" in str(exc)
    assert state.phase_status["engines"] == "error"
    assert state.phase_status["db"] == "ready"


def test_optional_phase_failure_degrades():
    state = AppState()
    phases = [("db", _ok), ("broker", _boom), ("engines", _ok),
              ("watchdogs", _ok), ("replay", _ok), ("misc", _ok)]
    results = _run(run_phases(None, phases, state.mark_phase))
    assert state.phase_status["broker"] == "error"   # 降级记录
    assert state.mark_ready() is True                # 但不阻断 READY


def test_qmt_broker_failure_does_not_break_ready():
    """显式声明：broker 是 Optional，QMT 失败不影响 /ready。"""
    assert PHASE_LEVELS["broker"] == "optional"
    assert "broker" not in REQUIRED_PHASES
    state = AppState()
    state.mark_phase("db", "ready")
    state.mark_phase("broker", "error")
    for n in ("engines", "watchdogs", "replay", "misc"):
        state.mark_phase(n, "ready")
    assert state.mark_ready() is True


def test_required_all_ready_is_ready():
    state = AppState()
    for n in REQUIRED_PHASES:
        state.mark_phase(n, "ready")
    assert state.mark_ready() is True
    # 任一 Required 未 ready → 不 READY
    state2 = AppState()
    for n in REQUIRED_PHASES:
        state2.mark_phase(n, "ready")
    state2.mark_phase("engines", "error")
    assert state2.mark_ready() is False


def test_phase_levels_covers_all_six_phases():
    assert set(PHASE_LEVELS) == {"db", "broker", "engines",
                                 "watchdogs", "replay", "misc"}
    assert set(REQUIRED_PHASES) == {k for k, v in PHASE_LEVELS.items()
                                    if v == "required"}

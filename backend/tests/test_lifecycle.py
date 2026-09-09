"""Phase 4 lifecycle/readiness contract tests."""
from core.state import AppState


def test_lifecycle_requires_all_core_phases():
    state = AppState()
    state.begin_startup()
    for name in ("db", "engines", "watchdogs", "replay"):
        state.mark_phase(name, "ready")
    assert state.mark_ready() is False
    assert state.lifecycle_ready is False
    state.mark_phase("misc", "ready")
    assert state.mark_ready() is True


def test_shutdown_clears_readiness():
    state = AppState()
    state.lifecycle_ready = True
    state.begin_shutdown()
    assert state.lifecycle_ready is False
    assert state.lifecycle_stopping is True

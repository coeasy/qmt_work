"""_pick_port 端口选择逻辑回归测试。

核心回归点：默认/配置端口空闲时，必须优先用它，而不被陈旧的 .qmt_work.port
锁定端口误导到非预期端口（如 21119）。这是 2026-08-21 调试「EXE 起不来」时
暴露的根因之一——残留实例占用 21118 导致改口 21119，实例退出后陈旧锁让下次
全新启动仍绑到 21119，极具迷惑性。
"""
import run as _run


def test_start_free_ignores_stale_lock(monkeypatch):
    """默认端口空闲时，即便 .qmt_work.port 存着 21119，也应返回 21118。"""
    monkeypatch.setattr(_run, "_MAX_PORT_RETRY", 3)
    monkeypatch.setattr(_run, "_port_in_use", lambda p: False)
    monkeypatch.setattr(_run, "_read_locked_port", lambda: 21119)
    assert _run._pick_port(21118) == 21118


def test_start_occupied_reuses_locked(monkeypatch):
    """默认端口被占用且有空闲锁定端口 → 复用锁定端口（保留持久冲突下的稳定性）。"""
    monkeypatch.setattr(_run, "_MAX_PORT_RETRY", 3)
    monkeypatch.setattr(_run, "_port_in_use", lambda p: p == 21118)
    monkeypatch.setattr(_run, "_read_locked_port", lambda: 21119)
    assert _run._pick_port(21118) == 21119


def test_start_occupied_no_lock_scans(monkeypatch):
    """默认端口被占用且无锁定端口 → 平滑改口到下一个空闲端口（21119）。"""
    monkeypatch.setattr(_run, "_MAX_PORT_RETRY", 3)
    monkeypatch.setattr(_run, "_port_in_use", lambda p: p == 21118)
    monkeypatch.setattr(_run, "_read_locked_port", lambda: None)
    assert _run._pick_port(21118) == 21119


def test_start_occupied_lock_also_occupied_scans(monkeypatch):
    """默认端口与锁定端口都被占用 → 继续向后扫描（21118/21119 占用 → 21120）。"""
    monkeypatch.setattr(_run, "_MAX_PORT_RETRY", 3)
    monkeypatch.setattr(_run, "_port_in_use", lambda p: p in (21118, 21119))
    monkeypatch.setattr(_run, "_read_locked_port", lambda: 21119)
    assert _run._pick_port(21118) == 21120

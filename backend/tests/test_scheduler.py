"""TaskScheduler + 调度路由单元测试（pytest，不依赖券商连接）。

运行：cd backend && python -m pytest tests/test_scheduler.py -q
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import DB  # noqa: E402
from app.state import state  # noqa: E402
from scheduler.scheduler import TaskScheduler  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from app.routes.scheduler import router  # noqa: E402


def _make_scheduler() -> TaskScheduler:
    tmp = tempfile.mktemp(suffix=".db")
    db = DB(Path(tmp))
    s = TaskScheduler()
    s.init(db)
    return s


# ---------------- compute_next_run ----------------
def test_next_run_daily_at():
    s = _make_scheduler()
    base = datetime(2026, 1, 1, 9, 0, 0)
    nxt = s.compute_next_run({"daily_at": "09:30"}, base)
    assert nxt == datetime(2026, 1, 1, 9, 30, 0)
    # 已过当日时刻 -> 次日
    base2 = datetime(2026, 1, 1, 10, 0, 0)
    nxt2 = s.compute_next_run({"daily_at": "09:30"}, base2)
    assert nxt2 == datetime(2026, 1, 2, 9, 30, 0)


def test_next_run_every_minutes():
    s = _make_scheduler()
    base = datetime(2026, 1, 1, 9, 0, 30)
    nxt = s.compute_next_run({"every_minutes": 5}, base)
    assert nxt == datetime(2026, 1, 1, 9, 5, 0)
    # 整点对齐后下一段
    base2 = datetime(2026, 1, 1, 9, 5, 0)
    nxt2 = s.compute_next_run({"every_minutes": 5}, base2)
    assert nxt2 == datetime(2026, 1, 1, 9, 10, 0)


def test_next_run_cron_star():
    s = _make_scheduler()
    base = datetime(2026, 1, 1, 9, 0, 30)
    nxt = s.compute_next_run("* * * * *", base)
    assert nxt == datetime(2026, 1, 1, 9, 1, 0)


def test_next_run_cron_step():
    s = _make_scheduler()
    base = datetime(2026, 1, 1, 9, 0, 30)
    nxt = s.compute_next_run("*/5 * * * *", base)
    assert nxt == datetime(2026, 1, 1, 9, 5, 0)
    # 跨小时对齐
    base2 = datetime(2026, 1, 1, 9, 58, 0)
    nxt2 = s.compute_next_run("*/5 * * * *", base2)
    assert nxt2 == datetime(2026, 1, 1, 10, 0, 0)


def test_next_run_cron_daily_time():
    s = _make_scheduler()
    base = datetime(2026, 1, 1, 12, 0, 0)
    nxt = s.compute_next_run("0 9 * * *", base)
    assert nxt == datetime(2026, 1, 2, 9, 0, 0)


# ---------------- CRUD + run_due ----------------
def test_add_list_remove_enable():
    s = _make_scheduler()
    rec = s.add_task("t1", {"every_minutes": 1}, "webhook",
                     {"url": "http://x"}, enabled=False)
    assert rec["id"] and rec["enabled"] is False
    assert len(s.list_tasks()) == 1
    # 启用
    upd = s.enable_task(rec["id"], True)
    assert upd["enabled"] is True
    # 删除
    assert s.remove_task(rec["id"]) is True
    assert s.get_task(rec["id"]) is None
    assert s.list_tasks() == []


def test_run_due_fires_executor_and_updates_next():
    s = _make_scheduler()
    called = []
    s.executor = lambda action, payload: called.append((action, payload))
    start = datetime(2026, 1, 1, 9, 0, 0)
    rec = s.add_task("t1", {"every_minutes": 1}, "webhook",
                     {"url": "http://x"}, enabled=True, now=start)
    # next_run 在 start+1min；用未来时刻触发
    due_time = start + timedelta(minutes=10)
    results = s.run_due(now=due_time)
    assert len(results) == 1
    assert results[0]["action"] == "webhook"
    assert called == [("webhook", {"url": "http://x"})]
    updated = s.get_task(rec["id"])
    assert updated["last_run"] == due_time.isoformat()
    # next_run 已重算为 +1 分钟
    assert datetime.fromisoformat(updated["next_run"]) == due_time + timedelta(minutes=1)
    assert updated["status"] == "ok"


def test_run_due_skips_disabled_and_future():
    s = _make_scheduler()
    called = []
    s.executor = lambda action, payload: called.append(action)
    start = datetime(2026, 1, 1, 9, 0, 0)
    s.add_task("d", {"every_minutes": 1}, "webhook", enabled=False, now=start)
    s.add_task("f", {"every_minutes": 1}, "webhook", enabled=True, now=start)
    # 此刻 next_run 在未来 -> 不触发
    assert s.run_due(now=start) == []
    assert called == []


def test_shutdown_restart_flags_via_run_due():
    s = _make_scheduler()
    s.executor = lambda a, p: None
    start = datetime(2026, 1, 1, 9, 0, 0)
    s.add_task("sd", {"every_minutes": 1}, "shutdown", now=start)
    s.add_task("rs", {"every_minutes": 1}, "restart", now=start)
    s.run_due(now=start + timedelta(minutes=5))
    assert s.shutdown_requested is True
    assert s.restart_requested is True


# ---------------- 路由（最小 FastAPI 挂载） ----------------
def _client():
    s = _make_scheduler()
    state.scheduler = s
    app = FastAPI()
    app.include_router(router)
    return TestClient(app), s


def test_route_crud_and_status():
    client, s = _client()
    # 新增
    r = client.post("/scheduler/tasks", json={
        "name": "t", "cron": {"every_minutes": 2},
        "action": "backtest", "payload": {"sym": "600000.SH"}})
    assert r.status_code == 200 and r.json()["code"] == 0
    tid = r.json()["data"]["id"]
    # 列表
    lst = client.get("/scheduler/tasks").json()["data"]
    assert len(lst) == 1
    # 启用/停用
    r = client.post(f"/scheduler/tasks/{tid}/enable", json={"enabled": False})
    assert r.json()["data"]["enabled"] is False
    # 状态
    st = client.get("/scheduler/status").json()["data"]
    assert st["task_count"] == 1
    assert st["shutdown_requested"] is False
    # 删除
    r = client.delete(f"/scheduler/tasks/{tid}")
    assert r.json()["data"]["deleted"] is True


def test_route_shutdown_sets_flag_and_restart():
    client, s = _client()
    r = client.post("/scheduler/shutdown")
    assert r.status_code == 200 and r.json()["data"]["scheduled"] is True
    assert s.shutdown_requested is True
    # restart
    r = client.post("/scheduler/restart")
    assert r.json()["data"]["scheduled"] is True
    assert s.restart_requested is True


def test_route_uninitialized_returns_503():
    # 暂时移除 state.scheduler
    saved = getattr(state, "scheduler", None)
    if hasattr(state, "scheduler"):
        del state.scheduler
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    r = client.get("/scheduler/tasks")
    assert r.json()["code"] == 503
    if saved is not None:
        state.scheduler = saved

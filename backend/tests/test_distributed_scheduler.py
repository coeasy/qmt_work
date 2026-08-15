"""分布式调度器单元测试（pytest，完全离线）。

覆盖：内存锁互斥 / 释放后再获取 / is_leader；
Redis 后端在 redis 不可用时优雅降级为内存（无网络）；
schedule_once 领导者执行、非领导者跳过。
运行：cd backend && python -m pytest tests/test_distributed_scheduler.py -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scheduler.distributed import DistributedScheduler  # noqa: E402


def _new_owner():
    import uuid
    return "owner-" + uuid.uuid4().hex[:8]


# ---------------- 内存锁：互斥 / 释放 / 再获取 ----------------
def test_memory_lock_mutual_exclusion():
    ds = DistributedScheduler(backend="memory")
    o1, o2 = _new_owner(), _new_owner()
    assert ds.acquire_lock("k", o1) is True
    assert ds.acquire_lock("k", o2) is False
    assert ds.is_leader("k", o1) is True
    assert ds.is_leader("k", o2) is False


def test_memory_lock_release_and_reacquire():
    ds = DistributedScheduler(backend="memory")
    o1, o2 = _new_owner(), _new_owner()
    assert ds.acquire_lock("k", o1) is True
    assert ds.release_lock("k", o1) is True
    # 释放后第二个 owner 可以获取
    assert ds.acquire_lock("k", o2) is True
    assert ds.is_leader("k", o2) is True
    # 非持有者释放应失败
    assert ds.release_lock("k", o1) is False


def test_memory_lock_heartbeat():
    ds = DistributedScheduler(backend="memory", ttl_seconds=30)
    o1, o2 = _new_owner(), _new_owner()
    ds.acquire_lock("k", o1)
    assert ds.heartbeat("k", o1) is True
    # 非持有者心跳失败
    assert ds.heartbeat("k", o2) is False
    # 续约后仍保持领导权
    assert ds.is_leader("k", o1) is True


# ---------------- Redis 降级（redis 不可用时走内存，无网络） ----------------
def test_redis_backend_degrades_to_memory_when_unavailable(monkeypatch):
    # 强制 redis 不可导入，确定性验证降级分支（无网络请求）
    import scheduler.distributed as sdmod
    monkeypatch.setattr(sdmod, "redis", None)
    ds = DistributedScheduler(backend="redis", redis_url="redis://127.0.0.1:6379/0")
    assert ds.backend_name == "memory"  # 已降级
    o1, o2 = _new_owner(), _new_owner()
    assert ds.acquire_lock("k", o1) is True
    assert ds.acquire_lock("k", o2) is False
    assert ds.is_leader("k", o1) is True
    assert ds.release_lock("k", o1) is True
    assert ds.acquire_lock("k", o2) is True  # 释放后可被他人获取


# ---------------- schedule_once ----------------
def test_schedule_once_runs_when_leader():
    ds = DistributedScheduler(backend="memory")
    owner = _new_owner()
    ds.acquire_lock("job", owner)
    calls = {"n": 0}

    def work():
        calls["n"] += 1
        return {"done": True}

    result = ds.schedule_once("job", owner, work)
    assert result == {"done": True}
    assert calls["n"] == 1


def test_schedule_once_skips_when_not_leader():
    ds = DistributedScheduler(backend="memory")
    leader, other = _new_owner(), _new_owner()
    ds.acquire_lock("job", leader)  # leader 持锁
    calls = {"n": 0}

    def work():
        calls["n"] += 1
        return {"done": True}

    result = ds.schedule_once("job", other, work)
    assert result == {"skipped": True}
    assert calls["n"] == 0  # 非领导者不执行

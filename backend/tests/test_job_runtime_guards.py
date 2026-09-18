"""任务运行时的两条护栏回归（阶段 3/4 新增不变量）。

锁定：
1. **资源组互斥** —— `system.eod` 与 `system.sync_bars` 等都写 `local_bars`，
   绝不允许并发。kind 不同 ≠ 不冲突，冲突的是底层数据资源。
2. **失败钩子** —— 任务失败必须能被外部感知（装配层接 notifier / 告警引擎），
   且**钩子自身抛异常不得拖垮任务**（否则告警抖动会让整个任务系统停摆）。

★ 写法约定：本项目未启用 pytest-asyncio，异步逻辑一律用 `asyncio.run` 包一层
同步测试函数（与 tests/ 下其它用例一致），不依赖任何 async 插件。
"""
import asyncio

from app.runtime.jobs import RESOURCE_GROUP, JobRuntime, JobSpec


def _spec(kind: str, runner):
    return JobSpec(kind=kind, name=f"test-{kind}", runner=runner)


async def _wait_status(rt: JobRuntime, jid: str, want: str, ticks: int = 300) -> bool:
    """轮询等待 job 到达目标状态（派发循环是异步的，不能假设立刻生效）。"""
    for _ in range(ticks):
        if rt._jobs[jid]["status"] == want:
            return True
        await asyncio.sleep(0.01)
    return False


def test_resource_group_blocks_concurrent_local_bars_writers():
    """同资源组的任务必须串行：`sync_bars` 要等 `eod` 跑完才上场。"""

    async def main():
        rt = JobRuntime(db=None)
        gate = asyncio.Event()
        order: list[str] = []

        async def eod_runner(_job):
            order.append("eod:start")
            await gate.wait()
            order.append("eod:end")
            return {"ok": True}

        async def sync_runner(_job):
            order.append("sync:start")
            return {"ok": True}

        eod_id = rt.submit(_spec("system.eod", eod_runner))
        assert await _wait_status(rt, eod_id, "running"), "eod 未被派发"

        sync_id = rt.submit(_spec("system.sync_bars", sync_runner))

        # eod 仍占着组 ⇒ sync_bars 必须排队，绝不能开始
        for _ in range(30):
            await asyncio.sleep(0.01)
        assert rt._jobs[sync_id]["status"] != "running", (
            "local_bars 资源组互斥失效：sync_bars 与 eod 并发写同一份日线数据"
        )
        assert "sync:start" not in order

        gate.set()
        assert await _wait_status(rt, sync_id, "done"), "sync_bars 未在 eod 结束后被派发"
        assert order == ["eod:start", "eod:end", "sync:start"], order

    asyncio.run(main())


def test_resource_group_does_not_block_unrelated_kinds():
    """反例护栏：互斥不能误伤 —— 不同资源组的任务应可并行。"""

    async def main():
        rt = JobRuntime(db=None)
        gate = asyncio.Event()
        screen_gate = asyncio.Event()

        async def eod_runner(_job):
            await gate.wait()
            return {"ok": True}

        # 也挂一个门：否则 screen 可能「瞬间跑完」，轮询根本抓不到 running 状态
        async def screen_runner(_job):
            await screen_gate.wait()
            return {"rows": 1}

        eod_id = rt.submit(_spec("system.eod", eod_runner))
        assert await _wait_status(rt, eod_id, "running"), "eod 未被派发"

        sid = rt.submit(_spec("screen", screen_runner))
        assert await _wait_status(rt, sid, "running"), (
            "资源组互斥误伤：screen 不写 local_bars，不应被 eod 阻塞"
        )
        gate.set()
        screen_gate.set()
        await asyncio.sleep(0.05)

    asyncio.run(main())


def test_resource_group_covers_all_local_bars_writers():
    """凡是会写 local_bars 的 kind 都必须在组里（防新增 kind 时漏登记）。"""
    assert RESOURCE_GROUP["system.eod"] == "local_bars"
    for kind in ("system.sync_bars", "system.rolling_repair", "system.reconcile_bars", "sync"):
        assert RESOURCE_GROUP.get(kind) == "local_bars", f"{kind} 会写 local_bars 却未登记资源组"
    # 选股不写日线库，不应被塞进该组（否则白白串行化）
    assert RESOURCE_GROUP.get("screen") is None


def test_failure_hook_fires_on_runner_exception():
    """任务失败必须通知到外部：「日线不再更新但没人知道」是最典型的失效模式。"""

    async def main():
        rt = JobRuntime(db=None)
        seen: list[dict] = []
        rt.set_failure_hook(seen.append)

        async def boom(_job):
            raise RuntimeError("模拟同步失败")

        jid = rt.submit(_spec("sync", boom))
        assert await _wait_status(rt, jid, "failed"), "任务未标记失败"
        assert len(seen) == 1, "失败钩子未被调用"
        assert seen[0]["id"] == jid
        assert "模拟同步失败" in (seen[0].get("error") or "")

    asyncio.run(main())


def test_failure_hook_exception_is_swallowed():
    """告警抖动绝不能拖垮任务本身：钩子抛异常要被吞掉。"""

    async def main():
        rt = JobRuntime(db=None)

        def bad_hook(_job):
            raise RuntimeError("告警通道挂了")

        rt.set_failure_hook(bad_hook)

        async def boom(_job):
            raise RuntimeError("原始失败")

        jid = rt.submit(_spec("sync", boom))
        # 任务照常标记失败（状态没被钩子的异常改写），且异常不外泄
        assert await _wait_status(rt, jid, "failed")

    asyncio.run(main())


def test_no_failure_hook_is_a_noop():
    """未注入钩子时（如纯单测环境）不得报错。"""

    async def main():
        rt = JobRuntime(db=None)

        async def boom(_job):
            raise RuntimeError("x")

        jid = rt.submit(_spec("sync", boom))
        assert await _wait_status(rt, jid, "failed")

    asyncio.run(main())

"""启动期券商连接护栏回归。

锁定的失效模式（本机实测）：
    17 条指向 ``C:/no_such_qmt/userdata_mini`` 的历史残留连接，每条在启动时
    都被 ``wait_for(bridge.start(), 32s)`` 逐个等待 ⇒ **启动 ~90s**，
    用户看到的现象是「软件半天打不开」，而真正的病因（一堆永远连不上的条目）
    在界面上完全看不出来。

另一条同类失效（R24 补）：客户端**已安装但未启动**时，路径判据放行，
一条连接就烧满 30s 握手超时 ⇒ ``bootstrap phase broker done in 32011ms``，
这期间 uvicorn 一个字都不回。修法是「启动预算 + 并发 + 交后台」，
见本文件下半部分。

不变量：
1. 路径不存在的连接**必须被判为不可尝试**（快速跳过，不烧超时）；
2. 空路径**不能被误杀** —— 空路径走 adapter 自行探测，是合法配置；
3. 慢连接**不得**占满启动预算，且必须与「失败」分开归因（慢有出路）。
"""
import asyncio
import os

from app.bootstrap.phase_broker import path_usable


def test_nonexistent_path_is_not_usable():
    assert path_usable("C:/no_such_qmt/userdata_mini") is False
    assert path_usable("/definitely/not/here") is False


def test_empty_path_is_usable():
    """空路径走 adapter 自行探测 —— 判为不可用会把合法配置误杀。"""
    assert path_usable("") is True
    assert path_usable("   ") is True
    assert path_usable(None) is True  # type: ignore[arg-type]


def test_existing_path_is_usable(tmp_path):
    assert path_usable(str(tmp_path)) is True


def test_real_world_junk_paths_are_all_rejected():
    """本机真实残留形态：绝对路径 + userdata_mini 后缀，但目录不存在。"""
    junk = [
        "C:/no_such_qmt/userdata_mini",
        "C:/no_such_qmt/userdata",
        "D:/qmt/userdata_mini",
        "C:/不存在/客户端/userdata_mini",
    ]
    for p in junk:
        assert path_usable(p) is False, p
        # 反证：确实不存在（防止测试环境碰巧造出了同名目录导致假通过）
        assert not os.path.isdir(p), p


# ---------------- 启动期拉起逻辑（用桩 manager 验证，不碰真适配器） ----------------

class _Cfg:
    def __init__(self, active: bool, client_path: str, name: str = "c"):
        self.active = active
        self.client_path = client_path
        self.name = name
        self.conn_id = name


class _Bridge:
    def __init__(self, hang: bool = False):
        self.hang = hang
        self.called = False

    async def start(self):
        self.called = True
        if self.hang:
            import asyncio
            await asyncio.sleep(60)   # 模拟「客户端未就绪」的长时间等待


class _Adapter:
    def is_connected(self):
        return True


class _Conn:
    def __init__(self, active: bool, path: str, hang: bool = False, bridge=None):
        self.cfg = _Cfg(active, path)
        self.bridge = bridge if bridge is not None else _Bridge(hang)
        self.adapter = _Adapter()
        self.connected = False
        self.last_error = ""


class _Mgr:
    def __init__(self, conns):
        self._conns = conns

    def all_connections(self):
        return list(self._conns)


def test_invalid_path_connection_is_skipped_not_waited():
    """无效路径连接必须**被跳过**，且不能调用 bridge.start()。"""
    import asyncio
    from app.bootstrap.phase_broker import start_persisted_connections

    bad = _Conn(True, "C:/no_such_qmt/userdata_mini")
    good = _Conn(True, os.path.dirname(os.path.abspath(__file__)))
    mgr = _Mgr([bad, good])

    stats = asyncio.run(start_persisted_connections(mgr))

    assert stats == {"started": 1, "failed": 0, "skipped": 1, "deferred": 0}, stats
    assert bad.bridge.called is False, "无效路径连接仍被尝试启动（会白烧 32s 超时）"
    assert good.bridge.called is True
    assert bad.connected is False
    assert "路径不存在" in bad.last_error, bad.last_error


def test_inactive_connections_are_untouched():
    import asyncio
    from app.bootstrap.phase_broker import start_persisted_connections

    off = _Conn(False, "C:/no_such_qmt/userdata_mini")
    stats = asyncio.run(start_persisted_connections(_Mgr([off])))
    assert stats == {"started": 0, "failed": 0, "skipped": 0, "deferred": 0}, stats
    assert off.bridge.called is False, "非 active 连接不应被拉起"


def test_empty_path_is_still_attempted():
    """空路径走 adapter 自行探测 —— 绝不能被护栏误杀。"""
    import asyncio
    from app.bootstrap.phase_broker import start_persisted_connections

    auto = _Conn(True, "")
    stats = asyncio.run(start_persisted_connections(_Mgr([auto])))
    assert stats["skipped"] == 0, "空路径被误判为无效并跳过"
    assert auto.bridge.called is True


# ---------------- 启动预算：慢连接不得阻断启动（R24） ----------------
#
# 锁定的失效模式（本机实测 2026-09-23 client.log）：
#     bridge 握手失败: _ping 调用超时 30.0s
#     bootstrap phase broker (optional) done in 32011ms
#     Application startup complete          <-- 这之后 uvicorn 才开始收请求
# 即「客户端已安装但未启动」（最常见情形）会让**整个 HTTP 服务 32s 不响应**。
# 根因不是「optional 相位失败阻断」（失败确实不阻断），而是 run_phases 对每个
# 相位**顺序 await** —— 「慢」照样阻断。
#
# 三条不变量：
# 1. 慢连接不得占用超过启动预算；
# 2. 多条连接必须**并发**拉起（否则耗时随 N 线性增长）；
# 3. 「慢」必须与「失败」分开归因（慢 ≠ 失败，慢有出路）。


def test_slow_connection_is_handed_off_not_awaited():
    """预算到点必须立刻返回，并把连接交给后台 —— 不得陪跑满硬上限。"""
    import asyncio
    import time

    from app.bootstrap.phase_broker import start_persisted_connections

    slow = _Conn(True, os.path.dirname(os.path.abspath(__file__)), hang=True)
    t0 = time.monotonic()
    stats = asyncio.run(start_persisted_connections(_Mgr([slow]), budget=0.5))
    elapsed = time.monotonic() - t0

    assert stats == {"started": 0, "failed": 0, "skipped": 0, "deferred": 1}, stats
    assert elapsed < 3.0, f"预算 0.5s 却等了 {elapsed:.1f}s（仍在陪跑硬上限）"
    assert slow.bridge.called is True, "交后台 ≠ 不尝试：连接确实被拉起过"


def test_deferred_is_not_reported_as_failure():
    """★ 归因分离：预算内没跑完 = deferred，**不是** failed。

    若把「慢」记成「失败」，用户会跑去改配置/重装客户端 —— 而其实什么都不用做
    （后台会自动连上）。这条测试就是防止把两类混为一谈。
    """
    import asyncio

    from app.bootstrap.phase_broker import start_persisted_connections

    slow = _Conn(True, os.path.dirname(os.path.abspath(__file__)), hang=True)
    stats = asyncio.run(start_persisted_connections(_Mgr([slow]), budget=0.2))

    assert stats["deferred"] == 1 and stats["failed"] == 0, stats
    assert slow.connected is False
    assert "失败" not in slow.last_error, slow.last_error
    assert "后台" in slow.last_error, f"必须给出出路（后台会继续连）：{slow.last_error}"


def test_connections_are_started_concurrently():
    """N 条连接共享同一份启动预算 —— 否则耗时随 N 线性增长。

    判据用「所有连接都被拉起过」而不是计时：顺序 await 的实现只会在预算内
    拉起第 1 条，其余连 start() 都进不去。
    """
    import asyncio
    import time

    from app.bootstrap.phase_broker import start_persisted_connections

    here = os.path.dirname(os.path.abspath(__file__))
    conns = [_Conn(True, here, hang=True) for _ in range(4)]
    t0 = time.monotonic()
    stats = asyncio.run(start_persisted_connections(_Mgr(conns), budget=0.5))
    elapsed = time.monotonic() - t0

    assert all(c.bridge.called for c in conns), \
        "有连接没被拉起 ⇒ 实现是顺序 await（耗时随 N 线性增长）"
    assert stats["deferred"] == 4, stats
    assert elapsed < 1.4, f"4 条连接顺序等待应为 ~2.0s，实测 {elapsed:.1f}s"


def test_fast_connection_is_still_counted_started():
    """防修过头：连得上的连接**必须**算 started，不能被一律记成 deferred。"""
    import asyncio

    from app.bootstrap.phase_broker import start_persisted_connections

    good = _Conn(True, os.path.dirname(os.path.abspath(__file__)))
    stats = asyncio.run(start_persisted_connections(_Mgr([good]), budget=5.0))

    assert stats == {"started": 1, "failed": 0, "skipped": 0, "deferred": 0}, stats
    assert good.connected is True


class _SlowBridge:
    """先慢后成：预算内连不上，但后台跑完后**真的连上**。"""

    def __init__(self, delay: float):
        self.delay = delay
        self.called = False
        self.gateway = None

    async def start(self):
        self.called = True
        await asyncio.sleep(self.delay)


class _ActiveMgr(_Mgr):
    def active_bridge(self):
        for c in self._conns:
            if c.connected:
                return c.bridge
        return None


def test_late_connection_syncs_active_bridge():
    """★ 出路闭环：后台补连成功之后，进程级活跃指针必须被同步。

    不变量：``state.bridge`` 不能在「连接晚到」时永远停在 None。

    ★ P1-4 后语义已变：``state.bridge`` / ``state.gateway`` 是**只写槽位**，
    业务读活跃连接一律走 ``broker_manager.active_bridge()``（动态求值），
    交易日历刷新也已改走 manager。因此本用例守的不再是「日历能不能刷新」，
    而是**只写槽位本身的写入契约**：晚到连接必须被写进去，
    否则任何按 ``hasattr`` / 属性存在性判断「有没有连接」的旧调用点会拿到陈旧值。
    读点不变量由 ``tests/test_state_writeonly_guard.py`` 另行守。
    """
    import asyncio

    from app.bootstrap import phase_broker
    from core.state import state

    slow = _Conn(True, os.path.dirname(os.path.abspath(__file__)),
                 bridge=_SlowBridge(0.2))
    mgr = _ActiveMgr([slow])
    saved = (state.broker_manager, state.bridge, state.gateway)
    state.broker_manager = mgr
    state.bridge = None
    state.gateway = None
    try:
        async def _main():
            stats = await phase_broker.start_persisted_connections(mgr, budget=0.05)
            assert stats["deferred"] == 1, stats
            assert state.bridge is None, "预算内不该有活跃连接"
            await asyncio.sleep(0.4)   # 等后台那条跑完
            return state.bridge

        got = asyncio.run(_main())
        assert got is slow.bridge, "后台补连成功后活跃指针没被同步"
        assert slow.connected is True
    finally:
        state.broker_manager, state.bridge, state.gateway = saved


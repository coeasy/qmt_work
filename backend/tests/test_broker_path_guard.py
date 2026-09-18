"""启动期「无效路径连接」护栏回归。

锁定的失效模式（本机实测）：
    17 条指向 ``C:/no_such_qmt/userdata_mini`` 的历史残留连接，每条在启动时
    都被 ``wait_for(bridge.start(), 32s)`` 逐个等待 ⇒ **启动 ~90s**，
    用户看到的现象是「软件半天打不开」，而真正的病因（一堆永远连不上的条目）
    在界面上完全看不出来。

两条不变量：
1. 路径不存在的连接**必须被判为不可尝试**（快速跳过，不烧超时）；
2. 空路径**不能被误杀** —— 空路径走 adapter 自行探测，是合法配置。
"""
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
    def __init__(self, active: bool, path: str, hang: bool = False):
        self.cfg = _Cfg(active, path)
        self.bridge = _Bridge(hang)
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

    assert stats == {"started": 1, "failed": 0, "skipped": 1}, stats
    assert bad.bridge.called is False, "无效路径连接仍被尝试启动（会白烧 32s 超时）"
    assert good.bridge.called is True
    assert bad.connected is False
    assert "路径不存在" in bad.last_error, bad.last_error


def test_inactive_connections_are_untouched():
    import asyncio
    from app.bootstrap.phase_broker import start_persisted_connections

    off = _Conn(False, "C:/no_such_qmt/userdata_mini")
    stats = asyncio.run(start_persisted_connections(_Mgr([off])))
    assert stats == {"started": 0, "failed": 0, "skipped": 0}, stats
    assert off.bridge.called is False, "非 active 连接不应被拉起"


def test_empty_path_is_still_attempted():
    """空路径走 adapter 自行探测 —— 绝不能被护栏误杀。"""
    import asyncio
    from app.bootstrap.phase_broker import start_persisted_connections

    auto = _Conn(True, "")
    stats = asyncio.run(start_persisted_connections(_Mgr([auto])))
    assert stats["skipped"] == 0, "空路径被误判为无效并跳过"
    assert auto.bridge.called is True

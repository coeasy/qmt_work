"""行情「订阅后回填初始快照」契约测试（SyncEngine.seed_snapshot）。

背景：`subscribe_quote` 只注册推送回调、不返回当前值，于是「订阅成功」与
「界面出现数字」之间隔着第一个 tick。盘前/休市没有 tick，界面就永远显示「--」。
本模块锁死回填行为，以及它「只优化体验、绝不阻断订阅」的边界。
"""
from __future__ import annotations

import asyncio

from sync import SyncEngine


class _FakeGateway:
    def __init__(self, result: dict, boom: bool = False) -> None:
        self.result = result
        self.boom = boom

    def get_full_tick(self, codes):  # noqa: ARG002 — 签名与真实 gateway 一致
        if self.boom:
            raise RuntimeError("行情服务未就绪")
        return self.result


class _FakeBridge:
    def __init__(self, result: dict, boom: bool = False) -> None:
        self.gateway = _FakeGateway(result, boom)

    async def call(self, fn, *a, **kw):
        return fn(*a, **kw)


class _FakeManager:
    def __init__(self, bridge) -> None:
        self._b = bridge

    def active_bridge(self):
        return self._b


def _engine(result: dict | None = None, boom: bool = False, bridge=None):
    b = bridge if bridge is not None else _FakeBridge(result or {}, boom)
    return SyncEngine(_FakeManager(b), None)


def test_seed_fills_latest_quotes_and_broadcast_buffer():
    eng = _engine({
        "600519.SH": {"code": "600519.SH", "last": 1500.0, "lastClose": 1490.0},
        "000001.SZ": {"code": "000001.SZ", "last": 11.2},
    })
    n = asyncio.run(eng.seed_snapshot(["600519.SH", "000001.SZ"]))
    assert n == 2
    assert eng.latest_quotes["600519.SH"]["last"] == 1500.0
    assert eng.latest_quotes["000001.SZ"]["last"] == 11.2
    # 复用既有微批广播通道：前端无需为「种子」写单独分支
    assert len(eng._batch_buf) == 2


def test_seed_skips_ticks_without_last():
    """last 为空说明该标的没有有效快照（停牌/未订阅），不能写进缓存污染界面。"""
    eng = _engine({
        "600519.SH": {"code": "600519.SH", "last": None},
        "000001.SZ": {"code": "000001.SZ", "last": ""},
        "399006.SZ": {"code": "399006.SZ", "last": 2400.0},
    })
    n = asyncio.run(eng.seed_snapshot(["600519.SH", "000001.SZ", "399006.SZ"]))
    assert n == 1
    assert "600519.SH" not in eng.latest_quotes
    assert eng.latest_quotes["399006.SZ"]["last"] == 2400.0


def test_seed_returns_zero_without_bridge():
    eng = _engine(bridge=None)
    eng.manager = _FakeManager(None)
    assert asyncio.run(eng.seed_snapshot(["600519.SH"])) == 0


def test_seed_returns_zero_on_empty_codes():
    eng = _engine({"600519.SH": {"last": 1.0}})
    assert asyncio.run(eng.seed_snapshot([])) == 0


def test_seed_never_raises_on_rpc_failure():
    """★不变量：回填是体验优化，任何失败都只能记日志，绝不向上抛。"""
    eng = _engine(boom=True)
    assert asyncio.run(eng.seed_snapshot(["600519.SH"])) == 0
    assert eng.latest_quotes == {}


def test_seed_handles_non_dict_payload():
    eng = _engine()
    eng.manager = _FakeManager(object())  # 协议异常：返回值不是 dict
    assert asyncio.run(eng.seed_snapshot(["600519.SH"])) == 0


def test_client_subscribe_schedules_seed(monkeypatch):
    """订阅新标的后必须调度回填（且只对新订阅的代码调度）。"""
    eng = _engine({"600519.SH": {"code": "600519.SH", "last": 1500.0}})
    eng.manager._b.gateway.result = {"600519.SH": {"code": "600519.SH", "last": 1500.0}}

    async def scenario():
        # 直接调 client_subscribe（它内部会 create_task 回填），再让出一次让任务跑完
        eng.client_subscribe("c1", ["600519.SH"])
        await asyncio.sleep(0.01)

    asyncio.run(scenario())
    assert eng.latest_quotes.get("600519.SH", {}).get("last") == 1500.0


def test_client_subscribe_without_running_loop_does_not_raise():
    """非事件循环上下文（同步脚本/单测直调）下订阅仍须成功，只是不做回填。"""
    eng = _engine({"600519.SH": {"last": 1.0}})
    eng.client_subscribe("c1", ["600519.SH"])  # 不应抛 RuntimeError
    assert eng.latest_quotes == {}

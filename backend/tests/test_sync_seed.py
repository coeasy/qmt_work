"""行情「订阅后回填初始快照」契约测试（SyncEngine.seed_snapshot）。

背景：`subscribe_quote` 只注册推送回调、不返回当前值，于是「订阅成功」与
「界面出现数字」之间隔着第一个 tick。盘前/休市没有 tick，界面就永远显示「--」。
本模块锁死回填行为，以及它「只优化体验、绝不阻断订阅」的边界。
"""
from __future__ import annotations

import asyncio

from sync import SyncEngine


class _FakeGateway:
    """券商 gateway 替身。

    ⚠️ `get_full_tick` **必须按入参 codes 过滤**：真实实现只返回被请求的标的。
    早期版本直接返回整份 result，于是「只回填了 1 个」的缺陷被替身掩盖
    （回退旧实现后护栏竟然还是绿的）—— 替身要复刻契约，不能只复刻调用方式。
    """

    def __init__(self, result: dict, boom: bool = False) -> None:
        self.result = result
        self.boom = boom

    def get_full_tick(self, codes):
        if self.boom:
            raise RuntimeError("行情服务未就绪")
        return {c: v for c, v in self.result.items() if c in list(codes)}

    def subscribe_quote(self, codes, on_tick, period: str = "1m") -> None:
        return None


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


def test_client_subscribe_seeds_code_already_subscribed_elsewhere():
    """★回归：已被别处订阅过的代码，界面首次订阅时**也必须**回填。

    真实缺陷（2026-09-18 实测）：持仓代码由启动期 `subscribe_positions` 预先订阅，
    于是客户端再订阅它时 `fresh` 为空 ⇒ 旧实现**不调度回填**；盘前又没有 tick，
    `latest_quotes` 里永远没有它 ⇒ **持仓页价格恒为「--」**。
    日志证据：`subscribed to broker: ['513090.SH']` 之后只有 `seed snapshot: 1/1`。

    回退方式：把 `need_seed` 改回 `fresh`（只对全系统首次订阅回填）→ 本用例判红。
    """
    eng = _engine({
        "513090.SH": {"code": "513090.SH", "last": 1.771},
        "000001.SZ": {"code": "000001.SZ", "last": 11.61},
    })
    # 模拟启动期持仓盯市：该代码已在券商侧订阅，但缓存里还没有值
    eng._subscribed_codes.add("513090.SH")
    assert "513090.SH" not in eng.latest_quotes

    async def scenario():
        eng.client_subscribe("c1", ["513090.SH", "000001.SZ"])
        await asyncio.sleep(0.01)

    asyncio.run(scenario())
    assert eng.latest_quotes.get("513090.SH", {}).get("last") == 1.771
    assert eng.latest_quotes.get("000001.SZ", {}).get("last") == 11.61
    # 券商侧不能因为「界面也订阅了」就重复下发一次
    assert "513090.SH" in eng._subscribed_codes


def test_client_subscribe_does_not_reseed_when_cache_already_has_value():
    """缓存里已有值就不该再打一次行情源（每次订阅都回源会把券商打爆）。"""
    calls: list[list[str]] = []

    class _SpyGateway(_FakeGateway):
        def get_full_tick(self, codes):
            calls.append(list(codes))
            return super().get_full_tick(codes)

    bridge = _FakeBridge({"000001.SZ": {"code": "000001.SZ", "last": 11.61}})
    bridge.gateway = _SpyGateway({"000001.SZ": {"code": "000001.SZ", "last": 11.61}})
    eng = _engine(bridge=bridge)

    async def scenario():
        eng.client_subscribe("c1", ["000001.SZ"])
        await asyncio.sleep(0.01)
        # 同一客户端重复订阅 / 另一个客户端订阅同一只 —— 都不该再回源
        eng.client_subscribe("c1", ["000001.SZ"])
        eng.client_subscribe("c2", ["000001.SZ"])
        await asyncio.sleep(0.01)

    asyncio.run(scenario())
    assert len(calls) == 1, calls
    assert calls[0] == ["000001.SZ"]


def test_client_subscribe_without_running_loop_does_not_raise():
    """非事件循环上下文（同步脚本/单测直调）下订阅仍须成功，只是不做回填。"""
    eng = _engine({"600519.SH": {"last": 1.0}})
    eng.client_subscribe("c1", ["600519.SH"])  # 不应抛 RuntimeError
    assert eng.latest_quotes == {}

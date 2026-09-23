"""R25 全链路审计的回归护栏。

锁定的失效模式（均来自 R25 的三遍审计，每条都能定位到具体代码）：

| # | 位置 | 失效模式 |
|---|------|----------|
| 1 | ``tools/target_portfolio.py`` | 前端默认 ``mode="ratio"``，后端只认 ``weight`` ⇒ 权重 0.3 被当成 **0.3 股**，``int(0.3)==0`` ⇒ **对全部持仓生成卖出清仓信号** |
| 2 | ``engines/strategy_runtime.py`` | ``_held_volume`` 查询异常返回 ``0.0`` ⇒ 被当成「无持仓」⇒ **重复买入 / 超额建仓** |
| 3 | ``app/bootstrap/phase_db.py`` + ``phase_misc.py`` + ``runtime/jobs.py`` | 启动补跑早于 ``system.*`` 工厂注册 ⇒ 崩溃残留任务被误标「无法恢复未知任务类型」，且**永不恢复** |
| 4 | ``app/runtime/eod.py`` | 读不存在的 ``failed_count``（真实键是 ``failed``）⇒ 同步大面积失败仍标 ``quality="final"`` |
| 5 | ``app/runtime/schedules.py`` | ``_noop_runner`` 对 **dict** 用属性访问 ``job.report`` ⇒ ``AttributeError`` |
| 6 | ``gateway/execution.py`` | ``cancel_order_price`` 在适配器里不存在 ⇒ ``AttributeError`` ⇒ 全局 500 |
| 7 | ``app/routes/signal.py`` | 失败**一律 503** ⇒ ``signal_router`` 刻意设置的 ``broker_unavailable`` 标志成了**孤儿**，风控拒绝被报成「服务不可用」 |

原则：**一条用例只守一个不变量**（这样变异时只红一条，定位清晰），
并配「防修过头」的反向断言。全部用桩，**不连真券商、不碰真库**。
"""
from __future__ import annotations

import asyncio
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _code(path: str) -> str:
    """读源码并**剥掉注释**，供源码级断言使用。

    ⚠️ 必须剥注释：本项目踩过多次「断言匹配到自己的注释」⇒ 护栏**永远为绿**
    （R25 实证：`defer_unknown=True` 出现在解释它的注释里，把参数改成 False
    后断言照样通过）。剥注释后，断言只能命中真正的代码。
    """
    text = (ROOT / path).read_text(encoding="utf-8")
    lines = []
    for line in text.splitlines():
        if line.strip().startswith("#"):
            continue
        idx = line.find("#")
        if idx != -1:
            line = line[:idx]
        lines.append(line)
    return "\n".join(lines)


# ===========================================================================
# 1. 目标持仓 mode 值域（高：默认「比例」会变成全仓清仓）
# ===========================================================================

class _StubGateway:
    def __init__(self, quotes=None, cash=None, positions=None):
        self._quotes = quotes or {}
        self._cash = cash or {}
        self._positions = positions or []

    async def get_quote(self, code):
        return self._quotes.get(code, {})

    async def get_cash(self):
        return self._cash

    async def get_positions(self, *a, **kw):
        return self._positions


class _StubBridge:
    def __init__(self, gw):
        self.gateway = gw

    async def call(self, fn, *a, **kw):
        return await fn(*a, **kw)


class _StubManager:
    def __init__(self, bridge):
        self._b = bridge

    def bridge(self, conn_id=None):
        return self._b


def _engine(gw=None):
    from tools.target_portfolio import TargetPortfolioEngine
    mgr = _StubManager(_StubBridge(gw)) if gw is not None else _StubManager(None)
    return TargetPortfolioEngine(mgr)


def test_target_portfolio_unknown_mode_is_rejected():
    """未知 mode 必须报错 —— 静默按股数兜底正是「比例→清仓」事故的成因。"""
    res = asyncio.run(_engine().sync({"600000": 100}, mode="bogus"))
    assert res["ok"] is False
    assert "mode" in res["reason"]


def test_target_portfolio_ratio_is_treated_as_weight():
    """前端 UI 的「比例」= 后端 weight：必须按总资产折算股数，而不是当股数。"""
    gw = _StubGateway(quotes={"600000": {"lastPrice": 10.0}},
                      cash={"assets": 100000.0}, positions=[])
    res = asyncio.run(_engine(gw).sync({"600000": 0.3}, mode="ratio", dry_run=True))
    assert res["ok"] is True
    assert res["mode"] == "weight"
    # 100000 * 0.3 / 10 = 3000 股（不是 0.3 股 ⇒ 0）
    assert res["target"]["600000"] == 3000


def test_target_portfolio_amount_divides_by_price():
    """「金额」模式：按最新价折算股数。"""
    gw = _StubGateway(quotes={"600000": {"lastPrice": 10.0}}, positions=[])
    res = asyncio.run(_engine(gw).sync({"600000": 50000}, mode="amount", dry_run=True))
    assert res["ok"] is True
    assert res["target"]["600000"] == 5000


def test_target_portfolio_weight_uses_unique_price_entry():
    """取价必须走 core.quote_fields 唯一入口：只给 lastPrice 的源也要取到价。

    （此前内联 `q.get("last")` ⇒ 取不到 ⇒ 静默 continue ⇒ 该标的被悄悄跳过。）
    """
    gw = _StubGateway(quotes={"600000": {"lastPrice": 8.0}},
                      cash={"assets": 80000.0}, positions=[])
    res = asyncio.run(_engine(gw).sync({"600000": 0.5}, mode="weight", dry_run=True))
    assert res["target"]["600000"] == 5000      # 80000*0.5/8 = 5000


def test_target_portfolio_volume_mode_still_passthrough():
    """防修过头：股数模式（volume/shares）必须原样当股数。"""
    for mode in ("volume", "shares"):
        gw = _StubGateway(positions=[])
        res = asyncio.run(_engine(gw).sync({"600000": 1200}, mode=mode, dry_run=True))
        assert res["ok"] is True, mode
        assert res["mode"] == "volume", mode
        assert res["target"]["600000"] == 1200, mode


# ===========================================================================
# 2. 持仓查询失败 ≠ 无持仓（高：重复建仓）
# ===========================================================================

def _runtime_stub():
    """绕过 __init__ 构造 StrategyRuntime，只挂 _held_volume 需要的属性。"""
    from engines.strategy_runtime import StrategyRuntime
    rt = object.__new__(StrategyRuntime)
    rt.state = types.SimpleNamespace(paper_engine=None)
    rt._log = lambda *a, **k: None          # type: ignore[assignment]
    return rt


class _BoomBridge:
    gateway = types.SimpleNamespace(get_positions=lambda: None)

    async def call(self, fn, *a, **kw):
        raise RuntimeError("券商查询抖动")


class _FlatBridge:
    # gateway **必须真的有** get_positions：否则「取属性」那一步就 AttributeError，
    # 会被 _held_volume 的 except 吃掉而返回 None —— 恰好是它要区分的那种情形。
    gateway = types.SimpleNamespace(get_positions=lambda *a, **k: [])

    async def call(self, fn, *a, **kw):
        return []                            # 正常应答：确实没有持仓


def test_held_volume_returns_none_when_query_fails():
    """查询异常 ⇒ None（未知），**绝不能**是 0.0（会被当成「无持仓」而重复买入）。"""
    rt = _runtime_stub()
    got = asyncio.run(rt._held_volume({"id": 1, "mode": "live"}, "600000", _BoomBridge()))
    assert got is None


def test_held_volume_returns_none_without_bridge():
    rt = _runtime_stub()
    assert asyncio.run(rt._held_volume({"id": 1, "mode": "live"}, "600000", None)) is None


def test_held_volume_returns_zero_when_genuinely_flat():
    """防修过头：拿到持仓列表、确实没有该标的 ⇒ 0.0（这是「确实没有」）。"""
    rt = _runtime_stub()
    got = asyncio.run(rt._held_volume({"id": 1, "mode": "live"}, "600000", _FlatBridge()))
    assert got == 0.0


def test_strategy_skips_round_when_held_is_unknown():
    """调用方必须把 None 当成「跳过本轮」——不能下单，也不能写 _prev_held 基线。"""
    src = _code("backend/engines/strategy_runtime.py")
    idx = src.index("held = await self._held_volume(")
    window = src[idx:idx + 1600]
    assert "if held is None:" in window, "缺少「持仓未知 ⇒ 跳过本轮」分支"
    # 跳过必须发生在下单判断之前
    assert window.index("if held is None:") < window.index('signal == "buy"')


# ===========================================================================
# 3. 任务恢复时序：工厂没注册 ≠ 任务类型未知（高：永不恢复）
# ===========================================================================

class _StubDb:
    def __init__(self, rows):
        self.rows = rows
        self.executed: list = []      # execute()：只记 UPDATE 等（判「是否被标 failed」）
        self.upserts: list = []       # upsert()：进度落库，与上面的判据分开记

    def query(self, sql, params=()):
        return list(self.rows)

    def execute(self, sql, params=()):
        self.executed.append((sql, params))
        return 1

    def upsert(self, table, payload):
        self.upserts.append((table, payload))
        return 1


def _row(kind: str, jid: int = 1) -> dict:
    return {"id": jid, "kind": kind, "name": kind, "priority": 3, "progress": 0,
            "message": "", "created_at": "2026-09-23T00:00:00", "params_json": "{}",
            "checkpoint_json": "{}"}


def test_attach_db_defer_unknown_does_not_mark_failed():
    """defer_unknown=True：工厂还没注册时**不判定**，保持 queued 等下一轮。"""
    from app.runtime.jobs import JobRuntime
    db = _StubDb([_row("system.__r25_not_registered_yet__")])
    JobRuntime().attach_db(db, defer_unknown=True)
    assert db.executed == [], "延后判定期间不该写任何 failed"


def test_attach_db_default_still_marks_failed():
    """防修过头：工厂已齐全（默认 defer_unknown=False）时，真未知类型仍要标失败。"""
    from app.runtime.jobs import JobRuntime
    db = _StubDb([_row("system.__r25_really_unknown__")])
    JobRuntime().attach_db(db)
    assert len(db.executed) == 1
    assert db.executed[0][1][0] == "failed"
    assert "无法恢复" in db.executed[0][1][1]


def test_attach_db_resumes_once_factory_registered():
    """注册工厂后再 catch-up，同一个任务必须被真正恢复进队列。"""
    from app.runtime.jobs import JobRuntime, register_runner_factory
    kind = "system.__r25_resume_probe__"
    register_runner_factory(kind, lambda params: (lambda job: None))

    db = _StubDb([_row(kind)])
    rt = JobRuntime()
    rt.attach_db(db, defer_unknown=True)          # 第一轮：工厂未注册
    assert db.executed == []
    rt.attach_db(db)                              # 第二轮：工厂已注册
    assert db.executed == [], "已注册的 kind 不该被标 failed"
    assert len(rt._jobs) == 1, "任务应被恢复进队列"


def test_phase_db_defers_and_phase_misc_registers_first():
    """启动编排的两条硬约束（源码级，**剥注释后**断言）：
    phase_db 必须延后判定；phase_misc 必须「先注册工厂、再补跑」。
    """
    db_src = _code("backend/app/bootstrap/phase_db.py")
    assert "defer_unknown=True" in db_src, "phase_db 必须延后判定"

    misc_src = _code("backend/app/bootstrap/phase_misc.py")
    i_reg = misc_src.index("register_all()")
    i_att = misc_src.index("runtime.attach_db(state.db)")
    assert i_reg < i_att, "必须先 register_all() 再 attach_db()，否则 system.* 永不恢复"


# ===========================================================================
# 4. EOD 快照 quality 的判据键（高：假 final）
# ===========================================================================

def test_eod_quality_reads_a_key_that_really_exists():
    from app.sync.bars import SyncSummary
    keys = set(SyncSummary(started="").to_dict().keys())
    assert "failed" in keys
    assert "failed_count" not in keys, "SyncSummary 根本没有 failed_count 这个键"

    src = (ROOT / "backend/app/runtime/eod.py").read_text(encoding="utf-8")
    assert 'get("failed_count")' not in src, \
        "eod 读了不存在的键 ⇒ failed 恒 0 ⇒ quality 假 final"
    assert 'steps["bars_sync"].get("failed")' in src


# ===========================================================================
# 5. 模拟盘委托号判定（中：PAPER- 递给券商 ⇒ 500）
# ===========================================================================

def test_is_paper_order_id():
    from engines.paper_engine import is_paper_order_id
    assert is_paper_order_id("PAPER-1") is True
    assert is_paper_order_id("paper-42") is True
    assert is_paper_order_id("123456") is False
    assert is_paper_order_id("") is False
    assert is_paper_order_id(None) is False


def test_paper_guard_covers_all_three_cancel_paths():
    """三条撤单路径都必须过同一道闸（此前只有 /trade/cancel 有）。"""
    for rel in ("backend/app/routes/trade.py",
                "backend/app/routes/account.py",
                "backend/tools/trading.py"):
        src = (ROOT / rel).read_text(encoding="utf-8")
        assert "is_paper_order_id" in src, f"{rel} 缺少模拟盘委托号拦截"


# ===========================================================================
# 6. 无能力时的明确出路（低：AttributeError ⇒ 500）
# ===========================================================================

def test_cancel_order_price_without_adapter_support_returns_reason():
    from gateway.execution import ExecutionService

    class _Bridge:
        gateway = types.SimpleNamespace()      # 刻意没有 cancel_order_price

        async def call_locked(self, fn, *a, **kw):
            return await fn(*a, **kw)

    res = asyncio.run(ExecutionService().cancel_order_price(_Bridge(), "123"))
    assert res["ok"] is False
    assert "不支持" in res["reason"]


# ===========================================================================
# 7. 兜底 runner 的参数形态（低：AttributeError）
# ===========================================================================

def test_noop_runner_uses_dict_access():
    from app.runtime.schedules import _noop_runner
    calls = []
    job = {"report": lambda pct, msg: calls.append((pct, msg))}
    res = asyncio.run(_noop_runner(job))
    assert res["ok"] is True
    assert calls == [(100, "noop")]


# ===========================================================================
# 8. 相位明细上报（低：降级原因在接口上不可见）
# ===========================================================================

def test_run_phases_reports_status_ms_error():
    """run_phases 的返回值必须真的含 status/ms/error（docstring 声称有）。"""
    src = (ROOT / "backend/app/bootstrap/lifecycle.py").read_text(encoding="utf-8")
    assert '"status": "ready"' in src
    assert '"status": "error"' in src
    assert '"ms": round(ms, 1)' in src

    main_src = (ROOT / "backend/app/main.py").read_text(encoding="utf-8")
    assert "app.state.phase_results" in main_src, "相位明细被丢弃"

    health_src = (ROOT / "backend/app/routes/health.py").read_text(encoding="utf-8")
    assert "phase_details" in health_src, "/health 没有暴露相位明细"


# ===========================================================================
# 9. 信号失败的归因分流（中：孤儿标志 ⇒ 风控拒绝被报成「服务不可用」）
# ===========================================================================

def test_signal_submit_uses_broker_unavailable_flag():
    """`signal.py` 必须**真的读** `broker_unavailable` 来分流 503 / 400。

    ★ 为什么这条是「孤儿逻辑」而不是风格问题：
      `gateway/signal_router.py` 在两处**刻意**设置该标志（无券商会话 /
      `BrokerNotConnectedError|BrokerSDKError`），注释写明「路由据此返回 503
      （而非 400）」。但路由层从未读过它，改用「一律 503」——
      于是标志成了死代码，且风控熔断 / 额度 / 资金 / 参数错全被报成
      「服务不可用」，把用户引向错误的排查方向。
    ⚠️ 断言必须基于 `_code()`（剥注释）：本函数的注释里满是
      `broker_unavailable` 字样，不剥注释就**永远为绿**（R25 已实证过一次）。
    """
    code = _code("backend/app/routes/signal.py")
    lines = code.splitlines()

    # ★ 必须**限定在 `signal_submit` 函数体内**再断言顺序：
    #   同一文件里 `set_signal_mode` 早就有 `return err(400, str(exc))`，
    #   不切片就会拿它的行号跟 submit 的分支比 ⇒ 永远比错（R25 首跑实证）。
    start = next((i for i, ln in enumerate(lines) if "async def signal_submit" in ln), None)
    assert start is not None, "找不到 signal_submit"
    end = next((i for i, ln in enumerate(lines[start + 1:], start + 1)
                if ln.startswith("async def ") or ln.startswith("@router.")), len(lines))
    body = lines[start:end]
    body_code = "\n".join(body)

    assert 'res.get("broker_unavailable")' in body_code, \
        "signal.py 没读 broker_unavailable ⇒ 该标志是孤儿，503/400 无法分流"

    i_branch = next(
        (i for i, ln in enumerate(body) if 'res.get("broker_unavailable")' in ln), None)
    assert i_branch is not None
    # 紧随其后的可执行行必须是 503（券商不可用），而不是 400
    nxt = next((ln.strip() for ln in body[i_branch + 1:] if ln.strip()), "")
    assert nxt.startswith("return err(503"), \
        f"broker_unavailable 分支必须返 503（券商不可用），实为：{nxt[:80]!r}"

    # 反向：业务拒绝必须是 400，且出现在 503 分支**之后**（否则 400 会吞掉 503）
    i_400 = next((i for i, ln in enumerate(body)
                  if ln.strip().startswith("return err(400")), None)
    assert i_400 is not None, "业务拒绝没有 400 出口 ⇒ 又退回「一律 503」"
    assert i_400 > i_branch, \
        "400 出口必须在 503 分支之后，否则 broker_unavailable 永远走不到"

    # 防修过头：503 不得再作为无条件兜底出现
    assert 'return err(503, res.get("reason", "信号路由失败")' not in body_code, \
        "又变回「失败一律 503」⇒ 风控拒绝会被报成服务不可用"


# ===========================================================================
# 10. 归因分流（中：业务拒绝被报成「服务不可用」）
# ===========================================================================

def test_target_portfolio_sync_splits_broker_unavailable():
    """`/target-portfolio/sync` 必须按 `broker_unavailable` 分流 503 / 400。

    ★ 为什么这是缺陷而不是风格：旧实现对**所有**失败一律 503。而 `sync()` 的失败原因
      绝大多数是业务性的（`未知 mode：'ratio'`、targets 为空、总资金为 0）。
      尤其致命：R25 刚把「未知 mode」从「静默按股数处理（⇒ 全仓清仓）」改成
      **显式报错**，若出口仍是 503，这条修复的提示就被归因错埋掉了。
    ⚠️ 断言用 `_code()` 剥注释（本文件注释里满是该字符串）。
    """
    route = _code("backend/app/routes/target_portfolio.py")
    lines = route.splitlines()
    assert 'res.get("broker_unavailable")' in route, \
        "路由没读 broker_unavailable ⇒ 又退回「一律 503」"

    i_branch = next(i for i, ln in enumerate(lines) if 'res.get("broker_unavailable")' in ln)
    nxt = next(ln.strip() for ln in lines[i_branch + 1:] if ln.strip())
    assert nxt.startswith("return err(503"), f"券商不可用分支必须 503，实为 {nxt[:70]!r}"
    i_400 = next((i for i, ln in enumerate(lines)
                  if ln.strip().startswith("return err(400")), None)
    assert i_400 is not None and i_400 > i_branch, "业务拒绝（未知 mode 等）必须走 400"

    # 引擎侧必须真的把标志**设**出来，否则路由的分流永远走不到 503
    engine = _code("backend/tools/target_portfolio.py")
    assert '"broker_unavailable": True' in engine, \
        "sync() 没设 broker_unavailable ⇒ 路由的分流是死代码"


def test_broker_connect_unknown_conn_id_is_404_not_503():
    """`/brokers/{conn_id}/connect` 对**不存在的 conn_id** 必须 404，不是 503。

    ★ `manager.connect()` 显式 `raise KeyError(f"未知连接：{conn_id}")`。
      旧实现把 `KeyError` 和 `BrokerError` 合在一个 `except` 里统一报 503
      ⇒ 用户传错 conn_id 时被告知「服务不可用」，排查方向彻底被带偏。
      `BrokerError`（券商层不可用）保持 503 才是如实的。
    """
    code = _code("backend/app/routes/broker.py")
    lines = code.splitlines()
    i_fn = next(i for i, ln in enumerate(lines) if "async def connect_broker" in ln)
    body = lines[i_fn:i_fn + 40]
    joined = "\n".join(body)

    assert "except KeyError" in joined, "KeyError 没有单独分支 ⇒ 与 BrokerError 混在一起"
    i_key = next(i for i, ln in enumerate(body) if "except KeyError" in ln)
    # ⚠️ 必须找该分支里的**第一条 return**，而不是「下一行」——分支里先有一句
    #    `ctx.db.audit(...)` 审计，取下一行会拿到它（首跑就这么红了一次）。
    nxt = next((ln.strip() for ln in body[i_key + 1:]
                if ln.strip().startswith("return ")), "")
    assert nxt.startswith("return err(404"), \
        f"未知 conn_id 必须 404（连接不存在），实为 {nxt[:70]!r}"
    assert "except BrokerError" in joined, "BrokerError 必须保留 503（券商层不可用）"
    assert "(KeyError, BrokerError)" not in joined, "又合并回一个 except ⇒ 归因再次丢失"

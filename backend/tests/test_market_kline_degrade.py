"""A8 回归护栏：``/market/kline`` 在「源都在、都正常应答、但都返回空壳」时不许静默。

背景（2026-09-21 实测，用户报「从行情工作台打开时 K 线图无法正常展示」的**第二根因**）
------------------------------------------------------------------------------------
``/market/kline`` 原兜底条件是 ``not bars and not res.get("source")`` —— 只有
**一个源都没应答**时才走兜底。而实测最常见的形态恰恰是**源都在、都正常应答、
都返回空壳**：

    GET /market/kline?code=000001.SH  →  {"source": "broker", "count": 0, "bars": []}

此时 ``res["source"]`` 是 ``"broker"``（真值）⇒ 兜底被整段跳过 ⇒ 直接
**200 + 0 根 + ``note: null``** ⇒ 前端 K 线图留一块白板，**没有任何文字说明为什么**。

两类「空」必须分开处理（这是本文件的主题）：
- **源都不可用**（抛异常 / 超时）⇒ 语义是「获取失败」，处置是「连券商/查网络」；
- **源都在但都返回空壳**（正常返回 0 根）⇒ 语义是「这份数据本身没有」，
  处置是「新上市 / 停牌 / 该周期无成交 / 源不覆盖」。

只接住前者，就会在后者上静默 —— 而后者才是常态。

另：这里刻意返回 **200 + note** 而不是 503。前端 ``KLineChart`` 的 ``.catch()``
分支（网络/错误码）只能显示一句通用猜测「暂无 K 线数据（可能停牌或该周期无成交）」；
而 200 + note 会走 ``meta.note`` 分支把**真实原因**写在画布上。错误归因必须落到
用户看得见的地方，不能退化成一句猜测。
"""
from __future__ import annotations

import asyncio

import pytest

import datasource.degrade as degrade_mod
import tools as tools_mod


def _call_kline(monkeypatch, *, fetched, local, code="000001.SH",
                period="1d", count=30, adj=""):
    """直接调用路由函数，把「取数」与「本地兜底」两处外部依赖换成替身。

    ``/market/kline`` 内部是 ``from tools import fetch_kline_cached`` /
    ``from datasource.degrade import ...``（函数内导入）⇒ monkeypatch 模块属性即可生效。
    返回 ``(响应 dict, local_bars 的调用记录)``。
    """
    async def _fake_fetch(*a, **kw):
        return fetched

    monkeypatch.setattr(tools_mod, "fetch_kline_cached", _fake_fetch)

    seen: list[dict] = []

    def _fake_local(c, period=None, adjust=None, limit=None):
        seen.append({"code": c, "period": period, "adjust": adjust, "limit": limit})
        return local

    monkeypatch.setattr(degrade_mod, "local_bars", _fake_local)

    from app.routes.market import market_kline

    resp = asyncio.run(market_kline(code=code, period=period, count=count,
                                    adj=adj, ctx=None))
    return resp, seen


def _dres(bars: list, as_of: str = "2026-09-18"):
    """造一个真实的 DataResult（降级信封需要它，不伪造其类型）。"""
    from datasource.result import DataResult

    return DataResult.from_source(list(bars), source="local:sqlite", stale=True,
                                  as_of=as_of,
                                  warnings=[f"远程行情源不可用，返回本地数据仓数据（截至 {as_of}）。"])


# ============================================ 1. 空壳 ⇒ 200 + note（不许静默）
def test_empty_shell_with_source_returns_explanatory_note(monkeypatch):
    """★ 核心：``source=broker`` + ``bars=[]`` + 本地也没有 ⇒ 必须给出说明。

    改动前这里是 ``code=0`` + ``count=0`` + ``note=None``，前端一片白板。
    """
    resp, _ = _call_kline(monkeypatch, code="000001.SH",
                          fetched={"source": "broker", "bars": [], "cached_at": None},
                          local=None)

    assert resp["code"] == 0, f"空壳不该被当成错误码，实得 {resp}"
    data = resp["data"]
    assert data["count"] == 0
    assert data["bars"] == []
    note = data.get("note")
    assert note, "空壳必须有 note —— 否则前端白板且无从判断原因（A8 的原缺陷）"
    assert "000001.SH" in note, "说明里必须点明是哪个标的，否则用户不知道在说谁"
    assert "broker" in note, "说明里应指出是哪个源应答的（可追溯）"


def test_empty_shell_note_does_not_claim_fetch_failure(monkeypatch):
    """措辞纪律：此刻所有源都**成功应答**了，不许说成「获取失败」。

    项目已经因为「把『不支持』说成『网络坏了』」吃过亏（见硬约束清单）；
    这里同理：把「都成功了但没数据」说成「获取失败」会把排查方向带偏。
    """
    resp, _ = _call_kline(monkeypatch, fetched={"source": "eltdx", "bars": []}, local=None)
    note = resp["data"]["note"]
    for bad in ("获取失败", "网络", "连接券商"):
        assert bad not in note, f"空壳说明里不该出现「{bad}」：源其实成功应答了。实得：{note}"


def test_empty_shell_note_does_not_read_global_failure_trace(monkeypatch):
    """不许用全局共享的 ``last_failure_trace()`` 拼本请求文案。

    那份记录是**全局「最近一次」**，并发下完全可能来自**另一个请求**的链路 ⇒
    文案会张冠李戴。做法：把它换成「一被调用就炸」的探针，若实现真读了就会失败。
    """
    import app.services.market.aggregates as agg

    def _boom(*a, **kw):  # pragma: no cover - 只在被误用时触发
        raise AssertionError("空壳文案不该读全局 last_failure_trace（会张冠李戴）")

    monkeypatch.setattr(agg, "last_failure_trace", _boom, raising=False)
    resp, _ = _call_kline(monkeypatch, fetched={"source": "broker", "bars": []}, local=None)
    assert resp["data"]["note"], "仍应给出说明"


# ============================================ 2. 本地兜底（有数据时优先本地）
def test_empty_shell_falls_back_to_local_bars(monkeypatch):
    """远程空壳但本地有 ⇒ 用本地兜底，并**如实**标 stale / as_of / source。"""
    local = _dres([{"time": "20260918", "open": 1.0, "high": 1.1, "low": 0.9,
                    "close": 1.05, "volume": 100, "amount": 1000.0}])
    resp, _ = _call_kline(monkeypatch, fetched={"source": "broker", "bars": []}, local=local)

    data = resp["data"]
    assert data["count"] == 1
    assert data["bars"], "本地有数据就该回数据"
    assert data["source"] == "local:sqlite", "不得冒充远程源"
    assert data["stale"] is True, "本地兜底必须显式标陈旧"
    assert data["as_of"] == "2026-09-18", "必须给出数据真实截至时间"
    assert data.get("note") is None, "已有真实数据时不必再挂「为什么没有」的说明"


def test_local_fallback_receives_requested_count(monkeypatch):
    """★ 本地兜底必须**透传 count**。

    ``local_bars`` 默认 ``limit=500``；不透传会让「请求 30 根」在远程不可用时
    返回最多 500 根（实测 320 根 = 本地全量）⇒ 前端图表与指标计算随之失真。
    降级路径与主路径必须给出同一根数契约。
    """
    resp, seen = _call_kline(monkeypatch, count=30,
                             fetched={"source": "broker", "bars": []}, local=None)
    assert seen, "空 bars 时必须尝试过本地兜底"
    assert seen[0]["limit"] == 30, (
        f"local_bars 应收到 limit=30（请求根数），实得 {seen[0]['limit']!r}")
    assert seen[0]["code"] == "000001.SH"
    assert seen[0]["period"] == "1d"


def test_local_fallback_receives_adjust(monkeypatch):
    """复权口径也要透传到本地仓，否则兜底数据与请求口径不符。"""
    _, seen = _call_kline(monkeypatch, adj="qfq",
                          fetched={"source": "broker", "bars": []}, local=None)
    assert seen[0]["adjust"] == "qfq"


# ============================================ 3. 真「无源」仍走 503
def test_no_source_and_no_local_returns_503(monkeypatch):
    """一个源都没应答（``source`` 为空）且本地也没有 ⇒ 仍是 503「获取失败」。

    这是与「空壳」并列的另一条分支，**必须保持不同**：
    前者是「拿不到」（可重试 / 查连接），后者是「本来就没有」（换标的 / 换周期）。
    """
    resp, _ = _call_kline(monkeypatch, fetched={"source": None, "bars": []}, local=None)
    assert resp["code"] == 503, f"无源且无本地应 503，实得 {resp}"
    assert "K 线获取失败" in resp["message"]


def test_nonempty_bars_bypass_local_fallback(monkeypatch):
    """有数据时**不该**去打本地仓（避免无谓 IO 与「本地盖掉远程」）。"""
    bars = [{"time": "20260921", "open": 1.0, "high": 1.1, "low": 0.9,
             "close": 1.05, "volume": 100, "amount": 1000.0}]
    resp, seen = _call_kline(monkeypatch, fetched={"source": "broker", "bars": bars,
                                                   "cached_at": "2026-09-21T15:00:00"},
                             local=None)
    assert not seen, "有数据时不该调用本地兜底"
    assert resp["data"]["count"] == 1
    assert resp["data"]["source"] == "broker"
    assert resp["data"]["stale"] is False


# ============================================ 4. stale 一致性（既有不变量回归）
def test_cache_stale_source_sets_stale_true(monkeypatch):
    """``source=cache_stale`` ⇒ ``stale`` 必须为 True 且透出 ``as_of``。

    （曾经出现 ``source=cache_stale`` 却 ``stale:false`` ⇒ 用户以为是最新数据。）
    """
    bars = [{"time": "20260918", "open": 1.0, "high": 1.1, "low": 0.9,
             "close": 1.05, "volume": 100, "amount": 1000.0}]
    resp, _ = _call_kline(monkeypatch, fetched={"source": "cache_stale", "bars": bars,
                                                "cached_at": "2026-09-18T15:00:00"},
                          local=None)
    data = resp["data"]
    assert data["stale"] is True, "cache_stale 必须标 stale"
    assert data["as_of"] == "2026-09-18T15:00:00", "stale 时须给出数据截至时间"


# ============================================ 5. 周期校验（入口纪律回归）
def test_unknown_period_returns_400_not_silent_downgrade(monkeypatch):
    """未知周期 ⇒ 400 明确报错，绝不静默降级成日线（P0-1）。"""
    resp, _ = _call_kline(monkeypatch, period="不存在的周期",
                          fetched={"source": "broker", "bars": []}, local=None)
    assert resp["code"] == 400, f"未知周期应 400，实得 {resp}"


def test_tick_period_redirected_to_minutes_endpoint(monkeypatch):
    """分时不是 K 线周期 ⇒ 400 并指出正确端点（否则「校验过 → 券商返空 → 静默 0 根」）。

    ⚠️ 分时的周期名是 ``"tick"``（``datasource/periods.py::TICK_PERIOD``），
    **不是** ``"1m"`` —— 写成 ``"1m"`` 会走「未知周期」分支（也是 400，但测不到这条路径）。
    """
    resp, _ = _call_kline(monkeypatch, period="tick",
                          fetched={"source": "broker", "bars": []}, local=None)
    assert resp["code"] == 400
    assert "/market/minutes" in resp["message"]

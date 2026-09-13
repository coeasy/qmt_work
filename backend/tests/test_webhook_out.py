"""Phase F (F1)：WebhookOut 单元测试 —— HMAC 签名 / 事件匹配 / 重试退避。

覆盖面（对应 V10 方案 Phase F DoD）：
1. 事件匹配：精确 / 通配 ``*`` / 前缀 ``order.*``；
2. HMAC-SHA256 签名格式（``t=<ts>,v1=<hmac>``）与可复算性（接收方可验签）；
3. 投递成功：首次即 2xx → attempts=1、计数、订阅状态更新；
4. 失败重试 + 指数退避：前 N 次失败 → 按 base_delay*backoff^(attempt-1) 退避，
   最终成功记录 attempts=N；彻底失败 → status=failed + fail 计数；
5. 事件类型过滤：不匹配的订阅不投递。

外发 HTTP 用 httpx.MockTransport 拦截，零真实网络。
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json

import httpx
import pytest

from gateway.webhook_out import WebhookOut


# ---------------- 夹具 ----------------
class FakeDB:
    """只实现 WebhookOut 用到的 3 个方法。"""

    def __init__(self, subs=None):
        self._subs = subs or []
        self.inserts = []   # [(table, data)]
        self.executes = []  # [(sql, params)]

    def query(self, sql, params=()):
        return [dict(s) for s in self._subs]

    def query_one(self, sql, params=()):
        return None

    async def ainsert(self, table, data):
        self.inserts.append((table, dict(data)))
        return len(self.inserts)

    async def aexecute(self, sql, params=()):
        self.executes.append((sql, tuple(params)))


def make_wh(subs=None, **kw):
    kw.setdefault("base_delay", 0.001)
    db = FakeDB(subs)
    wh = WebhookOut(db, **kw)
    return wh, db


def sub(url="http://cb.test/hook", **kw):
    base = {"id": 1, "name": "t", "url": url, "events": "*", "secret": "",
            "enabled": 1, "max_retries": 3, "timeout_ms": 1000, "headers": {}}
    base.update(kw)
    return base


async def drain(wh):
    if wh._tasks:
        await asyncio.gather(*list(wh._tasks), return_exceptions=True)


async def dispatch_and_wait(wh, event, data):
    """dispatch 创建的 task 必须与等待在同一 event loop 内，否则跨 loop 等待不可靠。"""
    await wh.dispatch(event, data)
    await drain(wh)


def requests_seen(handler_holder):
    return handler_holder["requests"]


def mock_client(holder):
    """构造 MockTransport 客户端，把每个请求记进 holder 并按脚本响应。"""
    script = holder["script"]  # list of (status, text)；弹尽后重复最后一个

    def handler(request: httpx.Request) -> httpx.Response:
        holder["requests"].append({
            "url": str(request.url),
            "headers": dict(request.headers),
            "content": request.content.decode("utf-8"),
        })
        status, text = script.pop(0) if len(script) > 1 else script[0]
        return httpx.Response(status, text=text)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ---------------- 1. 事件匹配 ----------------
def test_event_match_patterns():
    f = WebhookOut._event_match
    assert f("order.event", "*")
    assert f("order.event", "order.event")
    assert not f("order.event", "deal.event")
    assert f("order.event", "order.*")
    assert f("order.filled", "order.*")
    assert not f("deal.event", "order.*")


def test_event_filter_dispatch():
    """事件类型覆盖：order.* 订阅只接 order.event，不接 deal.event。"""
    wh, db = make_wh([sub(events="order.*")])
    holder = {"script": [(200, "ok")], "requests": []}
    wh._http = mock_client(holder)

    asyncio.run(dispatch_and_wait(wh, "deal.event", {"x": 1}))
    asyncio.run(dispatch_and_wait(wh, "order.event", {"x": 2}))
    asyncio.run(wh.close())

    urls = [r["url"] for r in holder["requests"]]
    assert urls == ["http://cb.test/hook"]  # 只有 order.event 被投递
    assert wh.sent == 1 and wh.failed == 0


# ---------------- 2. HMAC 签名 ----------------
def test_signature_format_and_verifiability():
    """接收方视角：用 (ts, body, secret) 复算 v1 必须一致。"""
    secret = "s3cret"
    wh, _ = make_wh([sub(secret=secret)])
    holder = {"script": [(200, "ok")], "requests": []}
    wh._http = mock_client(holder)

    asyncio.run(dispatch_and_wait(wh, "order.event", {"code": "600519.SH", "volume": 100}))
    asyncio.run(wh.close())

    assert len(holder["requests"]) == 1
    req = holder["requests"][0]
    assert req["headers"]["x-qmtwork-event"] == "order.event"

    body = json.loads(req["content"])
    sig = req["headers"]["x-qmtwork-signature"]
    assert sig.startswith("t=") and ",v1=" in sig
    ts, v1 = sig[2:].split(",v1=")
    assert ts == body["ts"]
    expected = hmac.new(secret.encode(), f"{ts}.{req['content']}".encode(),
                        hashlib.sha256).hexdigest()
    assert v1 == expected


def test_unsigned_subscription_has_no_signature_header():
    wh, _ = make_wh([sub(secret="")])
    holder = {"script": [(200, "ok")], "requests": []}
    wh._http = mock_client(holder)
    asyncio.run(dispatch_and_wait(wh, "risk.event", {}))
    asyncio.run(wh.close())
    assert "x-qmtwork-signature" not in holder["requests"][0]["headers"]


# ---------------- 3/4. 重试与退避 ----------------
def test_retry_then_success_with_backoff(monkeypatch):
    """前 2 次 500、第 3 次成功 → attempts=3，退避序列 = base*backoff^(i-1)。"""
    wh, db = make_wh([sub(max_retries=3)], base_delay=0.5, backoff=2.0)
    holder = {"script": [(500, "e1"), (500, "e2"), (200, "ok")], "requests": []}
    wh._http = mock_client(holder)

    delays = []
    real_sleep = asyncio.sleep

    async def fake_sleep(d, *a, **kw):
        delays.append(d)
        await real_sleep(0)  # 测试不等真实退避

    monkeypatch.setattr("gateway.webhook_out.asyncio.sleep", fake_sleep)

    asyncio.run(dispatch_and_wait(wh, "order.event", {"v": 1}))
    monkeypatch.undo()
    asyncio.run(wh.close())

    assert len(holder["requests"]) == 3
    assert wh.sent == 1 and wh.failed == 0
    assert delays == [0.5, 1.0]  # base * backoff^(attempt-1)
    # 投递记录：最终 ok + attempts=3
    upd = [e for e in db.executes if "webhook_deliveries" in e[0]]
    assert upd and upd[0][1][0] == "ok" and upd[0][1][1] == 3


def test_exhausted_retries_marked_failed():
    wh, db = make_wh([sub(max_retries=2)], base_delay=0.001)
    holder = {"script": [(500, "always")], "requests": []}
    wh._http = mock_client(holder)

    asyncio.run(dispatch_and_wait(wh, "deal.event", {"v": 1}))
    asyncio.run(wh.close())

    assert len(holder["requests"]) == 2
    assert wh.failed == 1 and wh.sent == 0
    upd = [e for e in db.executes if "webhook_deliveries" in e[0]]
    assert upd and upd[0][1][0] == "failed" and upd[0][1][1] == 2
    sub_upd = [e for e in db.executes if "fail_count" in e[0]]
    assert sub_upd, "订阅 fail_count 必须被更新"

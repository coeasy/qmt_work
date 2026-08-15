"""B2 事件出站 webhook：委托/成交/告警/风控/算法/条件单事件主动推送给外部服务。

与通知中心（Notifier，面向 IM 渠道）的区别：
- Notifier 是「人」可读的多渠道消息（钉钉/企微/飞书/邮件）；
- WebhookOut 是「机器」可消费的结构化事件投递（JSON + HMAC-SHA256 签名 + 重试），
  供第三方系统（量化平台、风控系统、运维大盘）订阅实时事件流。

投递协议：
- POST <url>  Content-Type: application/json
- X-QmtWork-Event: <event>
- X-QmtWork-Signature: t=<ts>,v1=<hmac>
- Body: {"event","ts","data"}（data 为原始事件 payload）

订阅管理：webhook_subscriptions 表（CRUD 经 REST）；投递记录 webhook_deliveries 表。
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time

import httpx

log = logging.getLogger("qmt_work.webhook_out")


class WebhookOut:
    def __init__(self, db, max_retries: int = 3, base_delay: float = 2.0,
                 timeout: float = 5.0, backoff: float = 2.0):
        self.db = db
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.timeout = timeout
        self.backoff = backoff
        self._http = httpx.AsyncClient(timeout=timeout)
        self._cache: list[dict] | None = None
        self._lock = asyncio.Lock()
        self.sent = 0
        self.failed = 0

    async def close(self):
        await self._http.aclose()

    def invalidate(self):
        self._cache = None

    # ---------------- 订阅配置 ----------------
    def _configs(self) -> list[dict]:
        if self._cache is None:
            rows = self.db.query(
                "SELECT id, name, url, events, secret, enabled, max_retries, "
                "timeout_ms, headers_json FROM webhook_subscriptions ORDER BY id")
            for r in rows:
                try:
                    r["headers"] = json.loads(r.pop("headers_json") or "{}")
                except Exception:  # noqa: BLE001
                    r["headers"] = {}
                try:
                    r["max_retries"] = int(r.get("max_retries") or self.max_retries)
                except (TypeError, ValueError):
                    r["max_retries"] = self.max_retries
                try:
                    r["timeout_ms"] = int(r.get("timeout_ms") or int(self.timeout * 1000))
                except (TypeError, ValueError):
                    r["timeout_ms"] = int(self.timeout * 1000)
            self._cache = rows
        return self._cache

    @staticmethod
    def _event_match(event: str, pattern: str) -> bool:
        if pattern == "*" or pattern == event:
            return True
        if pattern.endswith(".*") and event.startswith(pattern[:-1]):
            return True
        return False

    def _matches(self, event: str) -> list[dict]:
        out = []
        for cfg in self._configs():
            if not cfg.get("enabled"):
                continue
            patterns = (cfg.get("events") or "*").replace(",", " ").split()
            if any(self._event_match(event, p) for p in patterns):
                out.append(cfg)
        return out

    # ---------------- 签名 ----------------
    @staticmethod
    def _sign(secret: str, ts: str, body: str) -> str:
        mac = hmac.new((secret or "").encode(), f"{ts}.{body}".encode(), hashlib.sha256)
        return mac.hexdigest()

    # ---------------- 投递 ----------------
    async def dispatch(self, event: str, data) -> None:
        """向所有匹配订阅投递一次事件（失败按各自 max_retries 重试，指数退避）。"""
        subs = self._matches(event)
        if not subs:
            return
        ts = str(int(time.time()))
        body = json.dumps({"event": event, "ts": ts, "data": data},
                          ensure_ascii=False, default=str)
        await asyncio.gather(*[self._deliver(s, event, ts, body) for s in subs],
                             return_exceptions=True)

    async def _deliver(self, sub: dict, event: str, ts: str, body: str) -> None:
        log_id = self.db.insert("webhook_deliveries", {
            "subscription_id": sub["id"], "event": event, "payload_json": body,
            "status": "pending", "attempts": 0, "created_at":
                time.strftime("%Y-%m-%dT%H:%M:%S")})
        max_tries = max(1, int(sub.get("max_retries") or self.max_retries))
        timeout = max(1.0, int(sub.get("timeout_ms") or 5000) / 1000.0)
        headers = dict(sub.get("headers") or {})
        headers.setdefault("Content-Type", "application/json")
        headers["X-QmtWork-Event"] = event
        secret = sub.get("secret") or ""
        if secret:
            headers["X-QmtWork-Signature"] = f"t={ts},v1={self._sign(secret, ts, body)}"
        last_err = ""
        http_status = 0
        for attempt in range(1, max_tries + 1):
            try:
                r = await self._http.post(sub["url"], content=body, headers=headers, timeout=timeout)
                http_status = r.status_code
                if 200 <= r.status_code < 300:
                    self.db.execute(
                        "UPDATE webhook_deliveries SET status=?, attempts=?, "
                        "http_status=?, error='', delivered_at=? WHERE id=?",
                        ("ok", attempt, http_status,
                         time.strftime("%Y-%m-%dT%H:%M:%S"), log_id))
                    self.db.execute(
                        "UPDATE webhook_subscriptions SET success_count=success_count+1, "
                        "last_status=?, last_error='', last_sent_at=? WHERE id=?",
                        ("ok", time.strftime("%Y-%m-%dT%H:%M:%S"), sub["id"]))
                    self.sent += 1
                    return
                last_err = f"HTTP {r.status_code}: {r.text[:200]}"
            except Exception as exc:  # noqa: BLE001
                last_err = str(exc)[:300]
            if attempt < max_tries:
                await asyncio.sleep(self.base_delay * (self.backoff ** (attempt - 1)))
        self.db.execute(
            "UPDATE webhook_deliveries SET status=?, attempts=?, http_status=?, error=? WHERE id=?",
            ("failed", max_tries, http_status, last_err, log_id))
        self.db.execute(
            "UPDATE webhook_subscriptions SET fail_count=fail_count+1, "
            "last_status=?, last_error=? WHERE id=?",
            ("failed", last_err[:300], sub["id"]))
        self.failed += 1

    # ---------------- 订阅 CRUD ----------------
    def list_subs(self) -> list[dict]:
        rows = self.db.query(
            "SELECT id, name, url, events, enabled, max_retries, timeout_ms, "
            "success_count, fail_count, last_status, last_error, last_sent_at, "
            "created_at, updated_at FROM webhook_subscriptions ORDER BY id")
        return rows

    def get_sub(self, sid: int) -> dict | None:
        return self.db.query_one(
            "SELECT * FROM webhook_subscriptions WHERE id=?", (sid,))

    def save_sub(self, data: dict) -> int:
        payload = {
            "name": data.get("name", ""),
            "url": data.get("url", ""),
            "events": data.get("events", "*"),
            "secret": data.get("secret", ""),
            "enabled": 1 if data.get("enabled", True) else 0,
            "max_retries": int(data.get("max_retries", 3)),
            "timeout_ms": int(data.get("timeout_ms", 5000)),
            "headers_json": json.dumps(data.get("headers", {}), ensure_ascii=False),
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        if not payload["url"]:
            raise ValueError("url 不能为空")
        if data.get("id"):
            fields = [f"{k}=?" for k in payload]
            self.db.execute(
                f"UPDATE webhook_subscriptions SET {','.join(fields)} WHERE id=?",
                (*payload.values(), data["id"]))
            sid = int(data["id"])
        else:
            payload["created_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            sid = self.db.insert("webhook_subscriptions", payload)
        self.invalidate()
        return sid

    def delete_sub(self, sid: int) -> None:
        self.db.execute("DELETE FROM webhook_subscriptions WHERE id=?", (sid,))
        self.db.execute("DELETE FROM webhook_deliveries WHERE subscription_id=?", (sid,))
        self.invalidate()

    def deliveries(self, sid: int = 0, limit: int = 50) -> list[dict]:
        if sid:
            return self.db.query(
                "SELECT * FROM webhook_deliveries WHERE subscription_id=? "
                "ORDER BY id DESC LIMIT ?", (sid, limit))
        return self.db.query(
            "SELECT * FROM webhook_deliveries ORDER BY id DESC LIMIT ?", (limit,))

    async def test(self, sid: int) -> dict:
        """向指定订阅发一条 test 事件（用于配置后验证连通性）。"""
        sub = self.get_sub(sid)
        if not sub:
            raise KeyError(f"未知订阅 #{sid}")
        await self.dispatch("webhook.test", {"hello": "qmt_work", "sub_id": sid})
        return {"tested": sid}

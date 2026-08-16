"""通知推送抽象：支持钉钉/企微/飞书/邮件/Webhook 模板，失败重试。

持久化：
- notifications 表：渠道配置 + 订阅事件 + 模板
- notification_log 表：发送记录与结果

事件命名约定（使用点号便于通配过滤）：
- order.filled          委托成交
- order.error           委托失败
- limitup.triggered     涨停触发
- algo.finished         算法单完成
- condition.triggered   条件单触发
- risk.blocked          风控拦截
- system.error          系统级错误
- broker.connected / broker.disconnected
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.mime.text import MIMEText
from typing import Any

import httpx

# 钉钉签名
import base64  # noqa: E402
import hmac  # noqa: E402
import hashlib  # noqa: E402
import urllib.parse  # noqa: E402


@dataclass
class NotifyMessage:
    event: str
    title: str
    body: str
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.monotonic)


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _render(template: str, ctx: dict[str, Any]) -> str:
    """极简模板：{{key}} 替换，支持 {{key|default}}。"""
    def repl(m: re.Match) -> str:
        expr = m.group(1).strip()
        if "|" in expr:
            key, default = expr.split("|", 1)
            default = default.strip().strip('"').strip("'")
        else:
            key, default = expr, ""
        val = ctx
        for part in key.split("."):
            if isinstance(val, dict):
                val = val.get(part, "")
            else:
                val = ""
        return str(val) if val is not None else default
    return re.sub(r"\{\{\s*(.+?)\s*\}\}", repl, template)


def _event_match(event: str, pattern: str) -> bool:
    """通配匹配：'*' 通吃；'order.*' 匹配 order.filled；'order.filled' 精确匹配。"""
    if pattern == "*" or pattern == event:
        return True
    if pattern.endswith(".*") and event.startswith(pattern[:-1]):
        return True
    return False


class Notifier:
    """异步通知中心：从 SQLite 读取配置，按需发送（支持静默期去重防轰炸）。"""

    def __init__(self, db: Any, max_retries: int = 3, base_delay: float = 2.0,
                 dedup_seconds: float = 0.0):
        self.db = db
        self.max_retries = max_retries
        self.base_delay = base_delay
        self._dedup_seconds = dedup_seconds
        self._dedup_seen: dict[tuple, float] = {}
        self._cache: list[dict] | None = None
        self._lock = asyncio.Lock()
        self._http = httpx.AsyncClient(timeout=30.0)
        self.on_event = None  # 可选回调：notify 时同步回调（告警引擎订阅用）

    def _dedup_allowed(self, key: tuple) -> bool:
        """静默期去重：同一 key 在窗口内只放行一次；0 = 不启用。

        key 形如 (channel_id, event, title) —— 相同渠道相同事件相同标题视为重复。
        """
        if self._dedup_seconds <= 0:
            return True
        now = time.monotonic()
        last = self._dedup_seen.get(key, 0.0)
        if now - last < self._dedup_seconds:
            return False
        self._dedup_seen[key] = now
        # 防内存膨胀：仅保留窗口内的时间戳
        if len(self._dedup_seen) > 4096:
            cutoff = now - self._dedup_seconds
            self._dedup_seen = {k: v for k, v in self._dedup_seen.items() if v >= cutoff}
        return True

    async def close(self):
        await self._http.aclose()

    def invalidate(self):
        self._cache = None

    async def _configs(self) -> list[dict]:
        if self._cache is None:
            rows = self.db.query(
                "SELECT id, name, channel, enabled, params_json, events, template "
                "FROM notifications ORDER BY id")
            for r in rows:
                try:
                    r["params"] = json.loads(r.get("params_json") or "{}")
                except Exception:  # noqa: BLE001
                    r["params"] = {}
            self._cache = rows
        return self._cache

    async def notify(self, event: str, title: str, body: str, payload: dict | None = None,
                     channels: list[str] | None = None, _internal: bool = False):
        """触发一次通知，按配置表中 enabled=1 且事件匹配的记录分发。

        channels 非空时仅向指定渠道名（channel 字段）发送（告警规则限定渠道用）。
        _internal=True 时不再回调 on_event（避免告警自触发死循环）。
        """
        if self.on_event and not _internal:
            try:
                self.on_event(event, payload or {})
            except Exception:  # noqa: BLE001
                pass
        msg = NotifyMessage(event=event, title=title, body=body, payload=payload or {})
        configs = await self._configs()
        tasks = []
        for cfg in configs:
            if not cfg.get("enabled"):
                continue
            if channels and cfg.get("channel") not in channels:
                continue
            events = (cfg.get("events") or "*").replace(",", " ").split()
            if not any(_event_match(event, p) for p in events):
                continue
            # P1 静默期去重：同渠道 + 同事件 + 同标题在窗口内只发一次（防告警轰炸）
            if not self._dedup_allowed((cfg.get("id"), event, title)):
                continue
            tasks.append(self._send_one(cfg, msg))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _send_one(self, cfg: dict, msg: NotifyMessage):
        ctx = {
            "event": msg.event, "title": msg.title, "body": msg.body,
            "payload": msg.payload, "ts": _now_iso(),
            "name": cfg.get("name", "")
        }
        rendered_title = _render(cfg.get("template") or "{{title}}", ctx)
        rendered_body = _render(cfg.get("template_body") or "{{body}}", ctx)
        log_id = self.db.insert("notification_log", {
            "notification_id": cfg["id"], "event": msg.event,
            "title": rendered_title, "body": rendered_body,
            "status": "pending", "created_at": _now_iso(),
        })

        channel = cfg.get("channel", "webhook")
        params = cfg.get("params", {})
        attempt = 0
        last_err = ""
        while attempt <= self.max_retries:
            try:
                if channel == "webhook":
                    await self._webhook(params, rendered_title, rendered_body)
                elif channel == "dingtalk":
                    await self._dingtalk(params, rendered_title, rendered_body)
                elif channel == "wecom":
                    await self._wecom(params, rendered_title, rendered_body)
                elif channel == "feishu":
                    await self._feishu(params, rendered_title, rendered_body)
                elif channel == "email":
                    await self._email(params, rendered_title, rendered_body)
                else:
                    raise ValueError(f"unknown channel {channel}")
                self.db.execute(
                    "UPDATE notification_log SET status=?, sent_at=?, response=? WHERE id=?",
                    ("ok", _now_iso(), "", log_id))
                return
            except Exception as e:  # noqa: BLE001
                last_err = str(e)[:500]
                attempt += 1
                if attempt <= self.max_retries:
                    await asyncio.sleep(self.base_delay * (2 ** (attempt - 1)))
        self.db.execute(
            "UPDATE notification_log SET status=?, response=? WHERE id=?",
            ("failed", last_err, log_id))

    async def _webhook(self, params: dict, title: str, body: str):
        url = params.get("url")
        if not url:
            raise ValueError("webhook url missing")
        payload = params.get("payload") or {"text": f"{title}\n{body}"}
        headers = params.get("headers") or {"Content-Type": "application/json"}
        r = await self._http.post(url, json=payload, headers=headers)
        r.raise_for_status()

    async def _dingtalk(self, params: dict, title: str, body: str):
        url = params.get("url")
        secret = params.get("secret", "")
        if not url:
            raise ValueError("dingtalk url missing")
        ts = str(int(time.time() * 1000))
        if secret:
            sign_str = f"{ts}\n{secret}"
            sign = urllib.parse.quote_plus(
                base64.b64encode(hmac.new(
                    secret.encode(), sign_str.encode(), hashlib.sha256).digest()))
            url = f"{url}&timestamp={ts}&sign={sign}"
        payload = {
            "msgtype": "markdown",
            "markdown": {"title": title, "text": f"## {title}\n{body}"}
        }
        r = await self._http.post(url, json=payload)
        r.raise_for_status()
        data = r.json()
        if data.get("errcode"):
            raise RuntimeError(data)

    async def _wecom(self, params: dict, title: str, body: str):
        url = params.get("url")
        if not url:
            raise ValueError("wecom url missing")
        payload = {"msgtype": "markdown", "markdown": {"content": f"**{title}**\n>{body}"}}
        r = await self._http.post(url, json=payload)
        r.raise_for_status()
        data = r.json()
        if data.get("errcode"):
            raise RuntimeError(data)

    async def _feishu(self, params: dict, title: str, body: str):
        url = params.get("url")
        if not url:
            raise ValueError("feishu url missing")
        payload = {"msg_type": "text", "content": {"text": f"{title}\n{body}"}}
        r = await self._http.post(url, json=payload)
        r.raise_for_status()
        data = r.json()
        if data.get("code") and data.get("code") != 0:
            raise RuntimeError(data)

    async def _email(self, params: dict, title: str, body: str):
        import smtplib
        host = params.get("host")
        port = int(params.get("port", 587))
        user = params.get("user")
        password = params.get("password")
        to = params.get("to")
        if not host or not user or not password or not to:
            raise ValueError("email host/user/password/to missing")
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = title
        msg["From"] = user
        msg["To"] = to

        def _send():
            with smtplib.SMTP(host, port, timeout=30) as s:
                if port == 587:
                    s.starttls()
                s.login(user, password)
                s.send_message(msg)
        await asyncio.to_thread(_send)

    # ---------------- 配置 CRUD ----------------

    def list_configs(self) -> list[dict]:
        rows = self.db.query(
            "SELECT id, name, channel, enabled, events, template, params_json, "
            "created_at, updated_at FROM notifications ORDER BY id")
        for r in rows:
            try:
                r["params"] = json.loads(r.pop("params_json", "{}"))
            except Exception:  # noqa: BLE001
                r["params"] = {}
        return rows

    def get_config(self, nid: int) -> dict | None:
        row = self.db.query_one(
            "SELECT id, name, channel, enabled, events, template, params_json, "
            "created_at, updated_at FROM notifications WHERE id=?", (nid,))
        if row:
            try:
                row["params"] = json.loads(row.pop("params_json", "{}"))
            except Exception:  # noqa: BLE001
                row["params"] = {}
        return row

    def save_config(self, data: dict) -> int:
        payload = {
            "name": data.get("name", ""),
            "channel": data.get("channel", "webhook"),
            "enabled": 1 if data.get("enabled", True) else 0,
            "events": data.get("events", "*"),
            "template": data.get("template", "{{title}}\n{{body}}"),
            "params_json": json.dumps(data.get("params", {}), ensure_ascii=False),
            "updated_at": _now_iso(),
        }
        if "id" in data and data["id"]:
            fields = [f"{k}=?" for k in payload]
            self.db.execute(
                f"UPDATE notifications SET {','.join(fields)} WHERE id=?",
                (*payload.values(), data["id"]))
            nid = int(data["id"])
        else:
            payload["created_at"] = _now_iso()
            nid = self.db.insert("notifications", payload)
        self.invalidate()
        return nid

    def delete_config(self, nid: int):
        self.db.execute("DELETE FROM notifications WHERE id=?", (nid,))
        self.db.execute("DELETE FROM notification_log WHERE notification_id=?", (nid,))
        self.invalidate()

    def recent_logs(self, limit: int = 50) -> list[dict]:
        return self.db.query(
            "SELECT * FROM notification_log ORDER BY id DESC LIMIT ?", (limit,))

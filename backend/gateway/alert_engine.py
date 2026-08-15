"""告警规则引擎：基于通知事件流与指标阈值触发告警。

两类规则：
- 事件型：event 匹配通知事件（如 order.error / risk.blocked / broker.disconnected / *），
  由 Notifier.on_event 回调 evaluate_event 评估。
- 指标型：metric 字段非空（如 quote_latency），由行情/指标采集点调用 evaluate_metric 评估，
  按 op/threshold 比较，命中即触发。

命中规则后：经 Notifier 推送（可限定 channel），写入 alerts_history，并维护冷却（cooldown_seconds）。
"""
import asyncio
import logging
import time
from datetime import datetime, timezone

from gateway.notifier import _event_match

log = logging.getLogger("qmt_work.alert")


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class AlertEngine:
    def __init__(self, db, notifier, on_event=None):
        self._db = db
        self._notifier = notifier
        self._on_event = on_event

    def _cooldown_ok(self, rule: dict) -> bool:
        last = (rule.get("last_triggered") or "").strip()
        if not last:
            return True
        raw = rule.get("cooldown_seconds")
        cd = 300 if raw in (None, "") else int(raw)
        if cd <= 0:
            return True   # 0 = 不设冷却
        try:
            return time.time() - datetime.fromisoformat(last).timestamp() >= cd
        except Exception:  # noqa: BLE001
            return True

    def _fire(self, rule: dict, event_type: str, payload: dict) -> None:
        title = f"[告警] {rule.get('name', '')}"
        body = f"规则：{rule.get('event')} | 触发事件：{event_type} | 负载：{str(payload)[:200]}"
        channels = None
        ch = (rule.get("channel") or "*").strip()
        if ch and ch != "*":
            channels = [c.strip() for c in ch.split(",") if c.strip()]
        self._db.execute(
            "UPDATE alert_rules SET last_triggered=? WHERE id=?", (_now_iso(), rule["id"]))
        self._db.insert("alerts_history", {
            "rule_id": rule["id"], "event": event_type, "message": body,
            "triggered_at": _now_iso()})
        if self._notifier:
            asyncio.create_task(self._notifier.notify(
                "alert.triggered", title, body, payload, channels=channels, _internal=True))
        if self._on_event:
            try:
                self._on_event({"type": "alert", "data": {
                    "rule": rule.get("name"), "event": event_type}})
            except Exception:  # noqa: BLE001
                pass

    def evaluate_event(self, event_type: str, payload: dict | None = None) -> None:
        """事件型规则评估（由 Notifier.on_event 回调）。"""
        if event_type == "alert.triggered":
            return  # 阻断告警自触发死循环
        rules = self._db.query("SELECT * FROM alert_rules WHERE enabled=1")
        for r in rules:
            if r.get("metric"):
                continue  # 指标型不在此评估
            # 注意实参顺序：_event_match(实际事件, 规则模式)
            if not _event_match(event_type, r.get("event") or "*"):
                continue
            if not self._cooldown_ok(r):
                continue
            self._fire(r, event_type, payload or {})

    def evaluate_metric(self, metric: str, value: float, payload: dict | None = None) -> None:
        """指标型规则评估（由行情/指标采集点调用）。"""
        rules = self._db.query(
            "SELECT * FROM alert_rules WHERE enabled=1 AND metric=?", (metric,))
        for r in rules:
            try:
                thr = float(r.get("threshold") or 0)
                op = r.get("op") or ">"
                if op == ">":
                    ok = value > thr
                elif op == "<":
                    ok = value < thr
                elif op == ">=":
                    ok = value >= thr
                elif op == "<=":
                    ok = value <= thr
                else:
                    ok = False
            except Exception:  # noqa: BLE001
                continue
            if not ok:
                continue
            if not self._cooldown_ok(r):
                continue
            self._fire(r, f"metric.{metric}", payload or {})

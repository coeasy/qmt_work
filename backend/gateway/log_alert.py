"""日志告警：ERROR 及以上级别日志推送到外部 webhook（工业级可观测性）。

- 后台线程 + 队列异步发送，不阻塞业务线程
- 支持 HMAC-SHA256 签名（webhook 配置为 `{url}|{secret}` 时携带 X-Signature 头）
- 指数退避重试 3 次，失败仅记一条日志（不打断主流程）
- 日志聚合：log_json=true 时输出结构化 JSON（逐行），便于接入 Loki/ELK
"""
import hashlib
import hmac
import json
import logging
import os
import queue
import threading
import time
import urllib.error
import urllib.request


class LogAlertHandler(logging.Handler):
    """把达到指定级别的日志异步推送到 webhook（告警集成）。"""

    _MAX_QUEUE = 500  # 队列上限，防止日志洪峰打爆内存

    def __init__(self, webhook: str, level: str = "ERROR"):
        super().__init__()
        self.setLevel(getattr(logging, level.upper(), logging.ERROR))
        self._url, self._secret = self._parse(webhook)
        self._queue: queue.Queue = queue.Queue(maxsize=self._MAX_QUEUE)
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="qmt-log-alert")
        self._thread.start()

    @staticmethod
    def _parse(webhook: str) -> tuple[str, str]:
        """webhook 支持 `{url}|{secret}`（HMAC 签名）或纯 `{url}`。"""
        if "|" in webhook:
            url, secret = webhook.rsplit("|", 1)
            return url.strip(), secret.strip()
        return webhook.strip(), ""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            # 避免告警日志自身再触发告警形成死循环
            if record.name.startswith("qmt_log_alert"):
                return
            self._queue.put_nowait(record)
        except queue.Full:
            pass  # 洪峰时丢弃，保主流程

    def _run(self) -> None:
        while True:
            record = self._queue.get()
            try:
                self._post(record)
            except Exception as exc:  # noqa: BLE001
                self._log_quiet(f"log alert post failed: {exc}")
            finally:
                self._queue.task_done()

    def _post(self, record: logging.LogRecord) -> None:
        body = {
            "app": "qmt_work",
            "level": record.levelname,
            "logger": record.name,
            "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "message": self.format(record),
            "pid": os.getpid(),
        }
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self._secret:
            headers["X-Signature"] = self._sign(data)
        # 指数退避重试 3 次
        delay = 1.0
        for attempt in range(3):
            try:
                req = urllib.request.Request(self._url, data=data, headers=headers,
                                             method="POST")
                with urllib.request.urlopen(req, timeout=5) as resp:
                    if 200 <= resp.status < 300:
                        return
                raise OSError(f"http status {getattr(resp, 'status', '?')}")
            except Exception as exc:  # noqa: BLE001
                if attempt < 2:
                    time.sleep(delay)
                    delay *= 2
                else:
                    self._log_quiet(f"log alert retries exhausted: {exc}")

    def _sign(self, data: bytes) -> str:
        return hmac.new(self._secret.encode(), data, hashlib.sha256).hexdigest()

    @staticmethod
    def _log_quiet(msg: str) -> None:
        try:
            logging.getLogger("qmt_log_alert").warning(msg)
        except Exception:  # noqa: BLE001
            pass


def setup_log_alert(root: logging.Logger) -> None:
    """若配置了日志告警 webhook，则挂载 LogAlertHandler。"""
    from app.config import settings
    webhook = (settings.log_alert_webhook or "").strip()
    if not webhook:
        return
    try:
        root.addHandler(LogAlertHandler(webhook, settings.log_alert_level))
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("qmt_work").warning("日志告警初始化失败: %s", exc)

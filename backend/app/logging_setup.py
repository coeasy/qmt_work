"""日志体系（工业级）：控制台 + 滚动文件 + 结构化 JSON + 告警推送。

- 按天滚动（TimedRotatingFileHandler），保留 backup_days 天
- 日志目录：settings.log_dir（默认 backend/logs），自动创建
- log_json=true 时额外输出逐行 JSON（qmt_work.jsonl），便于接入 Loki/ELK
- 配置 log_alert_webhook 后，ERROR+ 日志异步推送到外部监控（见 gateway.log_alert）
- 供 app.main 在启动时调用 setup_logging()
"""
import json
import logging
import logging.handlers
import os

from app.config import settings

_FILE_FMT = "%(asctime)s %(levelname)s %(name)s [%(request_id)s] %(message)s"
_CONSOLE_FMT = "%(asctime)s %(levelname)s %(name)s [%(request_id)s] %(message)s"


class _JsonFormatter(logging.Formatter):
    """逐行 JSON 结构化输出（Loki/ELK 友好），脱敏由过滤器在 emit 前完成。"""

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "request_id": getattr(record, "request_id", ""),
            "pid": record.process,
            "message": record.getMessage(),
        }
        if record.exc_info:
            entry["exc_info"] = self.formatException(record.exc_info)
        try:
            return json.dumps(entry, ensure_ascii=False)
        except Exception:  # noqa: BLE001
            return json.dumps({"level": record.levelname, "logger": record.name,
                               "message": str(record.getMessage())}, ensure_ascii=False)


def setup_logging(level: int = logging.INFO) -> None:
    root = logging.getLogger()
    if getattr(root, "_qmt_work_configured", False):
        return
    root.setLevel(level)
    root._qmt_work_configured = True

    # E4：敏感信息脱敏过滤器（api_key / token / secret / Bearer 等一律掩码）
    try:
        from gateway.masking import SensitiveFilter
        sensitive = SensitiveFilter()
    except Exception:  # noqa: BLE001
        sensitive = None

    # X-Request-ID：请求链路号注入日志（便于按请求聚合排障）
    try:
        from app.middleware.request_id import RequestIDFilter
        req_id = RequestIDFilter()
    except Exception:  # noqa: BLE001
        req_id = None

    console = logging.StreamHandler()
    console.setLevel(level)
    console.setFormatter(logging.Formatter(_CONSOLE_FMT))
    if sensitive:
        console.addFilter(sensitive)
    if req_id:
        console.addFilter(req_id)
    root.addHandler(console)

    try:
        log_dir = os.path.abspath(str(settings.log_dir))
        os.makedirs(log_dir, exist_ok=True)
        file_handler = logging.handlers.TimedRotatingFileHandler(
            os.path.join(log_dir, "qmt_work.log"),
            when="midnight", backupCount=14, encoding="utf-8")
        file_handler.setLevel(level)
        file_handler.setFormatter(logging.Formatter(_FILE_FMT))
        if sensitive:
            file_handler.addFilter(sensitive)
        if req_id:
            file_handler.addFilter(req_id)
        root.addHandler(file_handler)

        # 结构化 JSON 输出：单独文件 qmt_work.jsonl，供日志聚合采集器抓取
        if settings.log_json:
            json_handler = logging.handlers.TimedRotatingFileHandler(
                os.path.join(log_dir, "qmt_work.jsonl"),
                when="midnight", backupCount=14, encoding="utf-8")
            json_handler.setLevel(level)
            json_handler.setFormatter(_JsonFormatter())
            if sensitive:
                json_handler.addFilter(sensitive)
            if req_id:
                json_handler.addFilter(req_id)
            root.addHandler(json_handler)
        # 将 qmt_work 日志器也挂到文件（root 已覆盖所有子日志器）
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("qmt_work").warning("文件日志初始化失败：%s", exc)

    # 日志告警：ERROR+ 异步推送 webhook（钉钉/企微/自定义监控）
    try:
        from gateway.log_alert import setup_log_alert
        setup_log_alert(root)
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("qmt_work").warning("日志告警初始化失败：%s", exc)

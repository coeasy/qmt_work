"""日志体系（工业级）：控制台 + 滚动文件双输出。

- 按天滚动（TimedRotatingFileHandler），保留 backup_days 天
- 日志目录：settings.log_dir（默认 backend/logs），自动创建
- 供 app.main 在启动时调用 setup_logging()
"""
import logging
import logging.handlers
import os

from app.config import settings

_FILE_FMT = "%(asctime)s %(levelname)s %(name)s [%(request_id)s] %(message)s"
_CONSOLE_FMT = "%(asctime)s %(levelname)s %(name)s [%(request_id)s] %(message)s"


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
        # 将 qmt_work 日志器也挂到文件（root 已覆盖所有子日志器）
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("qmt_work").warning("文件日志初始化失败：%s", exc)

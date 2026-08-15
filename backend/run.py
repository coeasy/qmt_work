"""开发/运行入口：python run.py

端口处理：默认 21117；端口可在用户配置文件中修改（qmt_work_config.json 的 port 字段，
打包运行时首次启动自动生成该文件）。若目标端口被占用则自动 +1 平滑改口（最多 10 次），
避免旧实例残留导致启动失败；实际监听端口通过 QMT_PORT_FILE 写出（桌面壳据此发现，
前端始终同源加载，无需感知端口）。

端口锁定（多实例防冲突）：当发生自动 +1 改口时，实际端口会持久化到
<db_dir>/.qmt_work.port。下次启动优先复用该端口（端口锁定），避免每次重启漂移；
若该端口又空闲则稳定复用，被占用则重新扫描并更新锁文件。
不同实例使用不同数据库目录即天然获得不同端口，多实例部署互不冲突。

可用环境变量：
- QMT_PORT：覆盖起始端口（如 QMT_PORT=8013；优先级高于配置文件）
- QMT_PORT_FILE：把实际监听端口写入该文件（桌面壳据此发现实际端口，规避端口冲突）
- QMT_PORT_SCAN：端口冲突时向后扫描的最大范围（默认 10）

单实例保护：以数据库同目录 .qmt_work.lock 为锁，第二个实例启动时直接退出（防双写同一库）。
"""
import logging
import os
import socket
import sys

import uvicorn

from app.config import settings

log = logging.getLogger("qmt_work")
_MAX_PORT_RETRY = int(os.environ.get("QMT_PORT_SCAN", "10") or 10)


def _acquire_singleton_lock(data_dir) -> int | None:
    """对 data_dir/.qmt_work.lock 加排他锁；成功返回文件描述符，失败返回 None。

    Windows 用 msvcrt，POSIX 用 fcntl——进程退出自动释放，不残留。
    """
    import threading
    _once = threading.local()

    lock_path = os.path.join(str(data_dir), ".qmt_work.lock")
    try:
        # 先确保目录存在：data 目录由 _self_check 创建，但锁在 _self_check 之前获取，
        # 全新安装时目录尚不存在会导致 os.open(O_CREAT) 抛 ENOENT 而误判“已在运行”。
        os.makedirs(str(data_dir), exist_ok=True)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError:
                os.close(fd)
                return None
        else:
            import fcntl
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(fd)
                return None
        # 记录 pid 便于排查
        try:
            os.ftruncate(fd, 0)
            os.write(fd, str(os.getpid()).encode())
        except OSError:
            pass
        return fd
    except Exception as exc:  # noqa: BLE001
        log.warning("单实例锁获取失败（继续启动）: %s", exc)
        return None


def _self_check() -> None:
    """启动自检：数据库目录可写、时钟基准、API Key 存在性。"""
    from datetime import datetime, timezone
    db_dir = os.path.dirname(str(settings.db_path)) or "."
    try:
        os.makedirs(db_dir, exist_ok=True)
        probe = os.path.join(db_dir, ".write_probe")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe)
        log.info("自检：数据库目录可写 ✓ %s", db_dir)
    except Exception as exc:  # noqa: BLE001
        log.warning("自检：数据库目录不可写！%s（%s）", db_dir, exc)
    local = datetime.now().astimezone()
    log.info("自检：本地时间 %s（UTC%+d）— TOTP 二次确认依赖本机时钟，偏差须 <30s",
             local.isoformat(timespec="seconds"), local.utcoffset().total_seconds() // 3600)
    if not settings.api_key:
        log.warning("自检：未配置 API Key（api_key），鉴权端点将拒绝访问")
    elif settings.api_key == "qmt-dev-key":
        log.warning("自检：API Key 仍为默认值 qmt-dev-key —— 生产环境必须修改，"
                    "否则远程调用可被他人用默认密钥接管！")
    else:
        log.info("自检：API Key 已配置（%s）", settings.api_key)


def _port_in_use(port: int) -> bool:
    """检测端口是否已被占用（bind 测试，立即释放）。

    注意：不能设置 SO_REUSEADDR —— Windows 上它允许「二次绑定」，
    会让探测误判端口空闲（而 uvicorn 实际 bind 失败），导致平滑改口失效。
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((settings.host, port))
        return False
    except OSError:
        return True
    finally:
        s.close()


def _port_lock_file() -> str:
    """端口锁文件路径：<db_dir>/.qmt_work.port（与单实例锁同目录）。"""
    db_dir = os.path.dirname(str(settings.db_path)) or "."
    return os.path.join(db_dir, ".qmt_work.port")


def _read_locked_port() -> int | None:
    """读取上次持久化的实际端口（端口锁定）；无记录/损坏返回 None。"""
    try:
        with open(_port_lock_file(), "r", encoding="utf-8") as f:
            v = int(f.read().strip())
        return v if 1 <= v <= 65535 else None
    except (OSError, ValueError):
        return None


def _write_locked_port(port: int) -> None:
    """持久化实际监听端口（端口锁定），供下次启动复用。"""
    try:
        os.makedirs(os.path.dirname(_port_lock_file()) or ".", exist_ok=True)
        with open(_port_lock_file(), "w", encoding="utf-8") as f:
            f.write(str(port))
    except OSError as exc:  # noqa: BLE001
        log.warning("写端口锁文件失败: %s", exc)


def _pick_port(start: int) -> int:
    """端口选择（多实例防冲突）：

    1. 若存在历史锁定端口（上次自动改口得到的实际端口）且当前空闲 → 优先复用（端口锁定，
       避免每次重启在默认端口与改口端口之间漂移）；
    2. 否则从 start 开始找第一个可用端口（平滑改口）；
    3. 全部占用则返回 start（交由 uvicorn 报错）。
    """
    locked = _read_locked_port()
    if locked is not None and locked != start and not _port_in_use(locked):
        log.info("端口锁定：复用上次实际端口 %s（锁文件 %s）", locked, _port_lock_file())
        return locked
    for port in range(start, start + _MAX_PORT_RETRY):
        if not _port_in_use(port):
            return port
    log.warning("端口 %s~%s 均被占用（扫描范围 %s），将尝试直接启动 %s（若失败会明确报错）",
                start, start + _MAX_PORT_RETRY - 1, _MAX_PORT_RETRY, start)
    return start


def _write_port_file(port: int) -> None:
    """若设置了 QMT_PORT_FILE，把实际监听端口写入该文件（桌面壳据此探测）。"""
    path = os.environ.get("QMT_PORT_FILE")
    if not path:
        return
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(str(port))
        log.info("actual port written to %s: %s", path, port)
    except Exception as exc:  # noqa: BLE001
        log.warning("write port file failed: %s", exc)


if __name__ == "__main__":
    from app.config import config_file, settings
    from app.logging_setup import setup_logging
    setup_logging()
    # 配置文件：打包(frozen)模式由 app.config 导入时自动生成；开发模式只提示路径
    cfg_path = config_file()
    if cfg_path.exists():
        log.info("配置文件：%s（已存在）", cfg_path)
    elif getattr(sys, "frozen", False):
        from app.config import ensure_config_file
        ensure_config_file()
        log.info("配置文件：%s（已自动生成）", cfg_path)
    else:
        log.info("配置文件：%s（开发模式不生成，使用默认配置；打包运行将自动生成）", cfg_path)
    log.info("数据库：%s", settings.db_path)
    log.info("日志目录：%s", settings.log_dir)
    log.info("API Key：%s", settings.api_key)
    # 端口来源提示：环境变量 QMT_PORT > 配置文件 port > 默认 21117
    _port_src = "默认值 21117"
    if os.environ.get("QMT_PORT"):
        _port_src = f"环境变量 QMT_PORT={os.environ['QMT_PORT']}"
    else:
        try:
            import json as _json
            _cfg = _json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
            if isinstance(_cfg.get("port"), int) and _cfg["port"] != 21117:
                _port_src = f"配置文件 port={_cfg['port']}"
            elif isinstance(_cfg.get("port"), int):
                _port_src = "配置文件 port（等于默认值 21117）"
        except Exception:  # noqa: BLE001
            pass
    log.info("监听端口：%s（来源：%s）", settings.port, _port_src)
    # 单实例保护：双开直接退出，防止两个进程并发写同一 SQLite
    lock_fd = _acquire_singleton_lock(
        os.path.dirname(str(settings.db_path)) or ".")
    if lock_fd is None:
        log.error("检测到 qmt_work 已在运行（数据库锁被占用），本实例退出。"
                  "若确无其他实例，请删除 %s 后重试。",
                  os.path.join(os.path.dirname(str(settings.db_path)) or ".",
                               ".qmt_work.lock"))
        sys.exit(1)
    _self_check()
    # 启动诊断：ABI 运行时探测结果（frozen EXE 黑盒下排查桥接问题的关键日志）
    try:
        from xtquant_client.runtime import (discover_bundled_runtimes,
                                            discover_system_runtimes)
        _b = discover_bundled_runtimes()
        _s = discover_system_runtimes()
        log.info("runtime 探测: bundled=%s", _b)
        log.info("runtime 探测: system=%s", _s)
        log.info("runtime 探测: host_abi=%s（%s）",
                 sys.version_info[0] * 100 + sys.version_info[1], sys.version.split()[0])
    except Exception as _exc:  # noqa: BLE001
        log.warning("runtime 探测失败: %s", _exc)
    target = _pick_port(settings.port)
    if target != settings.port:
        log.warning("端口 %s 已被占用，自动平滑改口为 %s（可改配置文件 port 或设置 QMT_PORT 指定起始端口）",
                    settings.port, target)
    # 端口锁定：把实际监听端口持久化，下次启动优先复用（多实例部署互不冲突）
    _write_locked_port(target)
    _write_port_file(target)
    uvicorn.run("app.main:app", host=settings.host, port=target, reload=False)

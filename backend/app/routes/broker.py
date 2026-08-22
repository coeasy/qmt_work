from app.routes._common import ok, err, state, BrokerError, ConnectionConfig, get_profile, list_profiles

from fastapi import APIRouter
# --- stdlib imports injected by fix_route_imports ---
import asyncio
import sys
import time



router = APIRouter()

@router.get("/brokers/auto-detect")
async def auto_detect_brokers():
    """自动发现本机 QMT / MiniQMT 客户端（运行中进程 + 安装目录扫描），返回候选列表。

    候选含：客户端根、疑似券商档案、userdata_mini、xtquant 定位与可导入状态；
    前端据此一键填入「添加券商连接」表单。
    """
    from xtquant_client.discovery import discover
    try:
        cands = await asyncio.to_thread(discover)
    except Exception as exc:  # noqa: BLE001
        return err(500, f"自动探测失败：{exc}")
    return ok({"candidates": cands, "count": len(cands)})

@router.get("/brokers/runtimes")
async def broker_runtimes():
    """ABI 运行时矩阵：主后端 Python、随包附带的桥接运行时、当前策略。

    供排障确认「哪些券商 xtquant ABI 可被进程内直连 / 桥接子进程覆盖」。
    """
    from xtquant_client.runtime import (host_python_minor, discover_bundled_runtimes)
    bundled = discover_bundled_runtimes()
    return ok({
        "host_python": sys.version.split()[0],
        "host_abi": host_python_minor(),
        "bundled_runtimes": {str(k): v for k, v in sorted(bundled.items())},
        "supported_abi_note": "迅投 xtquant 官方支持 cp36~cp312；cp313 暂未发布变体",
    })


@router.get("/brokers/diagnostics")
async def broker_diagnostics(deep: bool = False):
    """端到端可观测性快照（排障 / 长时段稳定性观察用，不依赖真实券商）。

    默认浅层（快）：宿主 ABI、随包桥接运行时、各连接状态与行情泵健康。
    ``?deep=1`` 额外含系统 Python 运行时发现与各连接的 ABI 桥接方案
    （首次会 spawn ``py`` 启动器 + 注册表扫描，较重但进程内缓存，故放线程池避免
    阻塞事件循环）。
    """
    from xtquant_client.runtime import (
        host_python_minor, discover_bundled_runtimes, discover_system_runtimes)

    host_abi = host_python_minor()
    bundled = discover_bundled_runtimes()
    payload: dict = {
        "host_python": sys.version.split()[0],
        "host_abi": host_abi,
        "bundled_runtimes": {str(k): v for k, v in sorted(bundled.items())},
        "connections": [],
        "generated_at": time.time(),
    }
    if deep:
        system = await asyncio.to_thread(discover_system_runtimes)
        payload["system_runtimes"] = {str(k): v for k, v in sorted(system.items())}

    conns = state.broker_manager.all_connections()
    for conn in conns:
        active = (state.broker_manager._active_id == conn.cfg.conn_id)
        entry = {
            "conn_id": conn.cfg.conn_id,
            "name": conn.cfg.name,
            "broker_id": conn.cfg.broker_id,
            "broker_name": conn.adapter.broker_name,
            "adapter": conn.adapter.adapter_id,
            "client_path": conn.cfg.client_path,
            "connected": conn.connected,
            "active": active,
            "health_status": conn.health_status,
            "last_error": conn.last_error,
            "reconnect_attempts": conn.reconnect_attempts,
            "pump_running": bool(conn.bridge and conn.bridge.pump_running()),
        }
        if deep:
            # 各连接 ABI 桥接方案（in_process / bridge + 解释器路径）；失败不阻断快照
            try:
                entry["runtime_plan"] = await asyncio.to_thread(
                    _runtime_plan_for, conn.cfg.client_path)
            except Exception:  # noqa: BLE001
                entry["runtime_plan"] = None
        payload["connections"].append(entry)
    return ok(payload)


def _runtime_plan_for(client_path: str):
    """安全计算某 client_path 的 ABI 运行时方案（供 deep 诊断），失败返回 None。"""
    try:
        from xtquant_client.runtime import xtp_runtime_plan
        return xtp_runtime_plan(client_path or "")
    except Exception:  # noqa: BLE001
        return None


@router.get("/brokers/profiles")
async def broker_profiles():
    return ok([{"id": p.id, "name": p.name, "adapter": p.adapter,
                "supported_account_types": p.supported_account_types,
                "supported_periods": p.supported_periods,
                "sdk_required": p.sdk_required, "min_version": p.min_version,
                "note": p.note} for p in list_profiles()])

@router.get("/brokers")
async def list_brokers():
    return ok(state.broker_manager.status_list())

@router.post("/brokers")
async def add_broker(body: dict):
    broker_id = body.get("broker_id") or ""
    if not get_profile(broker_id):
        return err(400, f"未知券商：{broker_id}")
    cfg = ConnectionConfig(
        conn_id=body.get("conn_id", ""), name=body.get("name", ""),
        broker_id=broker_id, client_path=body.get("client_path", ""),
        account_id=body.get("account_id", ""), account_type=body.get("account_type", "STOCK"),
        session_id=int(body.get("session_id", 0) or 0),
        min_version=body.get("min_version", ""),
        active=bool(body.get("active", False)))
    try:
        # 阶段 0-D（C7）：add_connection(autoconnect=True) 会同步拉起子进程 + 握手
        # （最坏 _ping 90s 超时），放线程池执行，避免冻结 FastAPI 事件循环。
        conn = await asyncio.to_thread(
            state.broker_manager.add_connection,
            cfg, bool(body.get("autoconnect", True)))
    except BrokerError as exc:
        return err(503, str(exc))
    return ok({"conn_id": conn.cfg.conn_id, "name": conn.cfg.name,
               "connected": conn.connected})

@router.post("/brokers/test")
async def test_broker(body: dict):
    cfg = ConnectionConfig(
        conn_id=body.get("conn_id", ""), broker_id=body.get("broker_id", ""),
        client_path=body.get("client_path", ""), account_id=body.get("account_id", ""),
        account_type=body.get("account_type", "STOCK"),
        session_id=int(body.get("session_id", 0) or 0),
        min_version=body.get("min_version", ""))
    # 阶段 0-D（C7）：test_connection 会临时拉起子进程（最坏 90s 超时），须放线程池。
    return ok(await asyncio.to_thread(state.broker_manager.test_connection, cfg))

@router.post("/brokers/{conn_id}/connect")
async def connect_broker(conn_id: str):
    try:
        # 阶段 0-D（C7）：connect 同步 start() + test_connection()（最坏 90s），放线程池。
        res = await asyncio.to_thread(state.broker_manager.connect, conn_id)
    except (KeyError, BrokerError) as exc:
        state.db.audit("broker", "broker.connect_failed", conn_id, {}, str(exc))
        return err(503, str(exc))
    state.db.audit("broker", "broker.connect", conn_id,
                   {"connected": res.get("connected")}, "ok")
    return ok(res)

@router.post("/brokers/{conn_id}/disconnect")
async def disconnect_broker(conn_id: str):
    state.broker_manager.disconnect(conn_id)
    state.db.audit("broker", "broker.disconnect", conn_id, {}, "ok")
    return ok({"disconnected": conn_id})

@router.post("/brokers/{conn_id}/active")
async def set_active_broker(conn_id: str):
    try:
        state.broker_manager.set_active(conn_id)
    except KeyError as exc:
        return err(404, str(exc))
    state.db.audit("broker", "broker.set_active", conn_id, {}, "ok")
    return ok({"active": conn_id})

@router.delete("/brokers/{conn_id}")
async def remove_broker(conn_id: str):
    state.broker_manager.remove(conn_id)
    return ok({"removed": conn_id})

@router.get("/brokers/{conn_id}/health")
async def broker_health(conn_id: str):
    if state.health_monitor is None:
        return err(503, "连接健康监控未初始化")
    s = state.health_monitor.status(conn_id)
    if s is None:
        return err(404, f"未知连接：{conn_id}")
    return ok(s)


# ---------------- 账户与分析 ----------------


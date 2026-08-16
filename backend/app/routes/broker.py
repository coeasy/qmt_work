from app.routes._common import ok, err, state, BrokerError, ConnectionConfig, get_profile, list_profiles

from fastapi import APIRouter
# --- stdlib imports injected by fix_route_imports ---
import asyncio
import sys



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
        conn = state.broker_manager.add_connection(cfg, autoconnect=bool(body.get("autoconnect", True)))
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
    return ok(state.broker_manager.test_connection(cfg))

@router.post("/brokers/{conn_id}/connect")
async def connect_broker(conn_id: str):
    try:
        res = state.broker_manager.connect(conn_id)
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


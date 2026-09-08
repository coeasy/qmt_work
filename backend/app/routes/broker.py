# --- stdlib imports injected by fix_route_imports ---
import asyncio
import logging
import sys
import time

from fastapi import APIRouter

from app.routes._common import BrokerError, ConnectionConfig, err, get_profile, ok, state

log = logging.getLogger("qmt_work.broker")



router = APIRouter()

@router.get("/brokers/auto-detect")
async def auto_detect_brokers():
    """自动发现本机 QMT / MiniQMT 客户端（运行中进程 + 安装目录扫描），返回候选列表。

    候选含：客户端根、疑似券商档案、userdata_mini、xtquant 定位与可导入状态、
    **自动发现的资金账号**（从 userdata[(_mini)]/users/<登录>/Config.xml 读取，可免手填）。
    前端据此一键填入「添加券商连接」表单。
    """
    from xtquant_client.discovery import discover, discover_accounts
    try:
        cands = await asyncio.to_thread(discover)
        for c in cands:
            try:
                # 防御性归一化：c["root"] 可能缺失，发现接口也可能非 list
                scan_path = c.get("client_path") or c.get("root") or ""
                c["accounts"] = list(discover_accounts(scan_path) or [])
                if c["accounts"]:
                    # 取第一个 STOCK 资金账号作为默认补全，供前端「一键填入」
                    c["default_account_id"] = c["accounts"][0]["account_id"]
                    c["broker_name"] = c["accounts"][0].get("broker_name") or c.get("broker_name", "")
                else:
                    c["default_account_id"] = ""

                # 券商识别（优先级：Config.xml 真实券商名 > 路径猜测 > generic 兜底）。
                # 路径缩写（gd_qmt）可能指光大或广发，语义有歧义，因此不靠路径猜；
                # 以 Config.xml 读出的真实券商名为准，识别不到再回退路径猜测/generic。
                c["broker_id"] = _resolve_broker_id(c)
                # 通用档案建议用极速版：识别为 generic（含光大/国信等）或无明确券商时，
                # 完整版大客户端(XtItClient)常对独立外部进程报 'illegal pid' 拒绝接入，
                # 而极速版 MiniQMT(userdata_mini) 是更稳的程序化通道——优先建议 mini。
                if c.get("broker_id") == "generic" and c.get("has_userdata_mini"):
                    c["client_mode"] = "mini"
                    if not c["client_path"].endswith("userdata_mini"):
                        c["client_path"] = c.get("client_path_mini") or c["client_path"]
            except Exception:  # noqa: BLE001
                c["accounts"] = []
                c["default_account_id"] = ""
                if not c.get("broker_id"):
                    c["broker_id"] = "generic"
    except Exception as exc:  # noqa: BLE001
        return err(500, f"自动探测失败：{exc}")
    return ok({"candidates": cands, "count": len(cands)})


def _resolve_broker_id(c: dict) -> str:
    """候选券商档案 id（优先级：Config.xml 真实券商名 > 路径猜测 > generic）。

    只认「无歧义」的券商名关键词（光大/国信等确认归入 generic 通用迅投档案，
    广发/银河/国金等归各自档案）。路径缩写（gd=广发/光大）在这里不猜。
    真实券商名无法确定识别时不覆盖路径猜测，仍为空则归 generic —— 避免把
    目录明确的候选（如 银河/国金）在无账号时被误降级成 generic。
    """
    # 局部导入：guess_broker_id_by_name 原在端点函数内导入，模块级函数取不到（F821）
    from xtquant_client.discovery import guess_broker_id_by_name
    by_name = guess_broker_id_by_name(c.get("broker_name") or "")
    if by_name:
        return by_name
    return c.get("broker_id") or "generic"


def _resolve_account(client_path: str, account_id: str, account_type: str = "STOCK") -> dict:
    """账号自动补全：account_id 留空时从客户端配置自动发现（依赖不填即可连）。

    返回 {"account_id", "account_type", "discovered": bool, "accounts": [...]}。
    discovered=True 表示自动发现并补全；accounts 是全部发现的账号供前端展示。
    """
    acc_id = (account_id or "").strip()
    if acc_id:
        return {"account_id": acc_id, "account_type": account_type or "STOCK",
                "discovered": False, "accounts": []}
    try:
        from xtquant_client.discovery import discover_accounts
        accounts = discover_accounts(client_path or "")
    except Exception:  # noqa: BLE001
        accounts = []
    if not accounts:
        return {"account_id": "", "account_type": account_type or "STOCK",
                "discovered": False, "accounts": []}
    # 首选与请求 account_type 匹配的账号；匹配不到时返回空（前端提示手动选），
    # 不静默退回 STOCK —— 避免用户点信用账户实际连到股票账户。
    target = (account_type or "STOCK").upper()
    matched = [a for a in accounts
               if (a.get("account_type") or "STOCK").upper() == target]
    if not matched:
        return {"account_id": "", "account_type": target,
                "discovered": False, "accounts": accounts,
                "hint": f"未发现 {target} 类型账户，请选择账户类型"}
    pick = matched[0]
    return {"account_id": pick["account_id"],
            "account_type": pick.get("account_type") or "STOCK",
            "discovered": True, "accounts": accounts}

@router.get("/brokers/runtimes")
async def broker_runtimes():
    """ABI 运行时矩阵：主后端 Python、随包附带的桥接运行时、当前策略。

    供排障确认「哪些券商 xtquant ABI 可被进程内直连 / 桥接子进程覆盖」。
    """
    from xtquant_client.runtime import discover_bundled_runtimes, host_python_minor
    bundled = discover_bundled_runtimes()
    return ok({
        "host_python": sys.version.split()[0],
        "host_abi": host_python_minor(),
        "bundled_runtimes": {str(k): v for k, v in sorted(bundled.items())},
        "supported_abi_note": "迅投 xtquant 官方支持 cp36~cp312；cp313 暂未发布变体",
    })


@router.post("/brokers/version-info")
async def broker_version_info(body: dict):
    """QMT 客户端版本画像探测（不连接券商）：识别客户端类型 / 版本 / 能力矩阵。

    解决「全功能完整版 vs 仅部分功能的极速 MiniQMT」的识别与能力路由：
    返回一份 {client_type, version_str, sdk_version, trade_dir, capabilities} 画像，
    前端据此展示检测到的 QMT 版本，并可按能力自动禁用/标记不支持的入口。
    """
    from xtquant_client.xtp import build_version_profile
    client_path = body.get("client_path") or ""
    if not client_path:
        return err(400, "client_path 不能为空")
    try:
        profile = await asyncio.to_thread(
            build_version_profile,
            client_path,
            body.get("client_mode", "auto") or "auto",
            body.get("account_id", "") or "",
            body.get("account_type", "STOCK") or "STOCK",
            bool(body.get("realtime_push", False)),
            None)
        return ok(profile.to_dict())
    except Exception as exc:  # noqa: BLE001
        return err(500, f"版本画像探测失败：{exc}")


@router.get("/brokers/diagnostics")
async def broker_diagnostics(deep: bool = False):
    """端到端可观测性快照（排障 / 长时段稳定性观察用，不依赖真实券商）。

    默认浅层（快）：宿主 ABI、随包桥接运行时、各连接状态与行情泵健康。
    ``?deep=1`` 额外含系统 Python 运行时发现与各连接的 ABI 桥接方案
    （首次会 spawn ``py`` 启动器 + 注册表扫描，较重但进程内缓存，故放线程池避免
    阻塞事件循环）。
    """
    from xtquant_client.runtime import discover_bundled_runtimes, discover_system_runtimes, host_python_minor

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
    """获取brokers / profiles（GET /brokers/profiles）。"""
    from xtquant_client.registry import BROKER_PROFILES, registry
    builtin = {p.id for p in BROKER_PROFILES}
    # H1 修复：planned profile 也返回（前端显示为路线图，但不可连接）
    return ok([{"id": p.id, "name": p.name, "adapter": p.adapter,
                "supported_account_types": p.supported_account_types,
                "supported_periods": p.supported_periods,
                "sdk_required": p.sdk_required, "min_version": p.min_version,
                "note": p.note, "is_custom": p.id not in builtin,
                "status": getattr(p, "status", "active")} for p in registry.list()])

@router.get("/brokers")
async def list_brokers():
    """获取brokers（GET /brokers）。

    status_list() 内含磁盘/子进程 I/O，是同步阻塞调用。前端「连接管理」页每 15s
    轮询一次，若直接在事件循环里执行，单次耗时会被放大为整机卡死（实测未缓存时
    单次 ~47s → 请求堆积 → 任何页面都加载不出来）。此处统一丢进线程池，
    保证事件循环永远不被单个连接状态查询拖住。
    """
    return ok(await asyncio.to_thread(state.broker_manager.status_list))

@router.post("/brokers")
async def add_broker(body: dict):
    """创建/提交brokers（POST /brokers）。"""
    target = _resolve_account(body.get("client_path", ""), body.get("account_id", ""),
                              body.get("account_type", "STOCK"))
    broker_id = body.get("broker_id") or ""
    # 券商通用化：未知券商放行（create_adapter 会降级到通用迅投 XTP 适配器）
    if not get_profile(broker_id) and broker_id:
        from xtquant_client.registry import hotplug_profile
        try:
            hotplug_profile({
                "id": broker_id, "name": agent_broker_name(body, broker_id),
                "adapter": "xtp",
                "supported_account_types": ["STOCK", "CREDIT", "OPTION", "FUTURES"],
                "note": "自动登记：QMT 全券商通用迅投适配器"})
        except Exception as exc:  # noqa: BLE001
            log.warning("hotplug 自动登记失败 %r: %s", broker_id, exc)
    cfg = ConnectionConfig(
        conn_id=body.get("conn_id", ""), name=body.get("name", "") or agent_broker_name(body, broker_id),
        broker_id=broker_id, client_path=body.get("client_path", ""),
        client_mode=body.get("client_mode", "auto") or "auto",
        account_id=target["account_id"], account_type=target["account_type"],
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
               "connected": conn.connected,
               "account_id": conn.cfg.account_id, "account_type": conn.cfg.account_type,
               "account_discovered": target["discovered"],
               "accounts": target["accounts"]})


def agent_broker_name(body: dict, broker_id: str) -> str:
    """连接显示名：优先 body.name，其次配置里发现的真实券商名，兜底 broker_id。"""
    if body.get("name"):
        return body["name"]
    try:
        accounts = _resolve_account(body.get("client_path", ""), "", "STOCK")["accounts"]
        if accounts and accounts[0].get("broker_name"):
            return f"{accounts[0]['broker_name']} QMT"
    except (FileNotFoundError, ValueError, KeyError, AttributeError) as exc:
        # client_path 无效/QMT 未装/account 字段缺失：取账户名兜底
        log.debug("_resolve_account 失败，回退到 profile.name：%s", exc)
    prof = get_profile(broker_id)
    if prof is not None:
        return prof.name
    # 未知 broker_id：有自定义 id 时保留它（如 auto-detect 返回的 gf/guojin 但 registry 未预注册），
    # 无自定义 id 时再兜底通用迅投名。
    if broker_id:
        return f"{broker_id} QMT"
    return "通用迅投 QMT"


@router.post("/brokers/test")
async def test_broker(body: dict):
    """创建/提交brokers / test（POST /brokers/test）。"""
    target = _resolve_account(body.get("client_path", ""), body.get("account_id", ""),
                              body.get("account_type", "STOCK"))
    cfg = ConnectionConfig(
        conn_id=body.get("conn_id", ""), broker_id=body.get("broker_id", ""),
        client_path=body.get("client_path", ""),
        client_mode=body.get("client_mode", "auto") or "auto",
        account_id=target["account_id"], account_type=target["account_type"],
        session_id=int(body.get("session_id", 0) or 0),
        min_version=body.get("min_version", ""))
    # 阶段 0-D（C7）：test_connection 会临时拉起子进程（最坏 90s 超时），须放线程池。
    res = await asyncio.to_thread(state.broker_manager.test_connection, cfg)
    if isinstance(res, dict):
        res["account_id"] = target["account_id"]
        res["account_type"] = target["account_type"]
        res["account_discovered"] = target["discovered"]
        res["accounts"] = target["accounts"]
    return ok(res)


@router.post("/brokers/launch")
async def launch_broker_client(body: dict):
    """按模式启动 QMT 客户端主程序（full=完整版 XtItClient / mini=极速版 XtMiniQmt /
    quote=独立行情 miniquote）。GUI 程序立即返回，登录需用户在弹出的窗口中完成。"""
    from xtquant_client.xtp import launch_client
    client_path = body.get("client_path") or ""
    if not client_path:
        return err(400, "请提供 client_path")
    mode = body.get("mode", "full") or "full"
    try:
        return ok(await asyncio.to_thread(launch_client, client_path, mode))
    except Exception as exc:  # noqa: BLE001
        return err(500, f"启动客户端失败：{exc}")

@router.post("/brokers/{conn_id}/connect")
async def connect_broker(conn_id: str):
    """创建/提交brokers / connect（POST /brokers/{conn_id}/connect）。"""
    try:
        # 阶段 0-D（C7）：connect 同步 start() + test_connection()（最坏 90s），放线程池。
        res = await asyncio.to_thread(state.broker_manager.connect, conn_id)
    except (KeyError, BrokerError) as exc:
        state.db.audit("broker", "broker.connect_failed", conn_id, {}, str(exc))
        return err(503, str(exc))
    except Exception as exc:  # noqa: BLE001  探测抛 RuntimeError 等也记录
        state.db.audit("broker", "broker.connect_failed", conn_id, {}, str(exc))
        return err(503, str(exc))
    state.db.audit("broker", "broker.connect", conn_id,
                   {"connected": res.get("connected")}, "ok")
    return ok(res)

@router.post("/brokers/{conn_id}/disconnect")
async def disconnect_broker(conn_id: str):
    """创建/提交brokers / disconnect（POST /brokers/{conn_id}/disconnect）。"""
    try:
        state.broker_manager.disconnect(conn_id)
        state.db.audit("broker", "broker.disconnect", conn_id, {}, "ok")
        return ok({"disconnected": conn_id})
    except Exception as exc:  # noqa: BLE001
        state.db.audit("broker", "broker.disconnect_failed", conn_id, {}, str(exc))
        return err(500, str(exc))

@router.post("/brokers/{conn_id}/active")
async def set_active_broker(conn_id: str):
    """创建/提交brokers / active（POST /brokers/{conn_id}/active）。"""
    try:
        state.broker_manager.set_active(conn_id)
    except KeyError as exc:
        return err(404, str(exc))
    except Exception as exc:  # noqa: BLE001
        return err(500, str(exc))
    state.db.audit("broker", "broker.set_active", conn_id, {}, "ok")
    return ok({"active": conn_id})

@router.delete("/brokers/{conn_id}")
async def remove_broker(conn_id: str):
    """删除brokers（DELETE /brokers/{conn_id}）。"""
    try:
        state.broker_manager.remove(conn_id)
        state.db.audit("broker", "broker.remove", conn_id, {}, "ok")
        return ok({"removed": conn_id})
    except Exception as exc:  # noqa: BLE001
        state.db.audit("broker", "broker.remove_failed", conn_id, {}, str(exc))
        return err(500, str(exc))

@router.post("/brokers/batch-delete")
async def batch_remove_brokers(body: dict):
    """创建/提交brokers / batch-delete（POST /brokers/batch-delete）。"""
    ids = [str(x) for x in (body.get("ids") or []) if x not in (None, "")]
    if not ids:
        return err(400, "ids 不能为空")
    removed = []
    for conn_id in ids:
        try:
            state.broker_manager.remove(conn_id)
            removed.append(conn_id)
        except (KeyError, ConnectionError, RuntimeError) as exc:
            # 单条删除失败：继续处理剩余项，整体不阻断
            log.warning("删除连接 %s 失败，已跳过：%s", conn_id, exc)
    state.db.audit("broker", "broker.batch_remove", f"#{len(removed)}", {"ids": removed}, "ok")
    return ok({"removed": removed, "deleted": len(removed)})

@router.get("/brokers/{conn_id}/health")
async def broker_health(conn_id: str):
    """获取brokers / health（GET /brokers/{conn_id}/health）。"""
    if state.health_monitor is None:
        return err(503, "连接健康监控未初始化")
    s = state.health_monitor.status(conn_id)
    if s is None:
        return err(404, f"未知连接：{conn_id}")
    return ok(s)


# ---------------- 账户与分析 ----------------


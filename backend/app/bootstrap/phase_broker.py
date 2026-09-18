"""阶段 2：券商连接 + 行情管道注册。

- 加载持久化的券商连接
- 引导连接（环境变量 QMT_ACCOUNT_ID/QMT_CLIENT_PATH）
- 启动各连接 bridge（带超时保护）
- **启动自动连接**（默认开）：无活跃连接时探测本机运行中的 QMT 客户端并接入
- 交易日历感知调度刷新
"""
from __future__ import annotations

import asyncio
import logging
import os
import time

from fastapi import FastAPI

from core.config import settings
from core.state import state
from core.state import init_broker_manager
from xtquant_client.manager import ConnectionConfig

log = logging.getLogger("qmt_work.bootstrap.broker")


def path_usable(client_path: str) -> bool:
    """客户端路径是否**有可能**连上（唯一入口，界面与启动共用同一判据）。

    ★ 为什么需要它：指向不存在目录的历史/测试残留连接（如
    ``C:/no_such_qmt/userdata_mini``）永远连不上，但启动时仍会被逐个
    ``wait_for(bridge.start(), 32s)`` —— 本机 17 条这类残留就把启动拖到 ~90s，
    用户看到的是「软件半天打不开」，而不是「有 17 条无效连接」。

    路径为空时返回 True：空路径走「adapter 自行探测」，**不能**因此判定不可用
    （否则会把合法配置误杀）。

    注：与 ``manager.status()`` 的 ``path_exists`` 不是同一个问题 —— 那个是给界面
    显示的「路径是否存在」（空路径也算不存在），这里是「值不值得尝试连接」。
    """
    p = (client_path or "").strip()
    if not p:
        return True
    return os.path.isdir(p)


def _bootstrap_from_env() -> None:
    """环境变量引导连接。"""
    if settings.account_id or settings.client_path:
        cfg = ConnectionConfig(
            conn_id="bootstrap", name="引导连接",
            broker_id=settings.broker_id or "guojin",
            client_path=settings.client_path, account_id=settings.account_id,
            account_type=settings.account_type, session_id=settings.session_id,
            active=True)
        state.broker_manager.add_connection(cfg, autoconnect=False)


async def _auto_connect_active() -> str:
    """启动自动连接：探测本机**正在运行**的 QMT 客户端并接入。

    先按「券商 + 客户端路径 + 资金账号」找既有连接：**有就复用**（重新点亮持久意图
    并拉起），没有才新建 —— 否则「用户手动断开过 → 下次启动又新建一条」会让连接
    列表随启动次数膨胀（实测踩过：三次启动累积出 3 条重复的「迅投 XTQuant 22453951」）。

    全程**非阻断**：探测 / 建连的任何失败都只记日志，绝不抛出（启动流程优先）。
    返回接入的 conn_id；未接入返回 ""。
    """
    from xtquant_client.autoconnect import build_connection, detect_candidates, pick_active

    try:
        cands = await asyncio.wait_for(asyncio.to_thread(detect_candidates), timeout=60.0)
    except asyncio.TimeoutError:
        log.warning("自动探测本机 QMT 客户端超时（60s），跳过自动连接")
        return ""
    except Exception as exc:  # noqa: BLE001
        log.warning("自动探测本机 QMT 客户端失败，跳过自动连接：%s", exc)
        return ""

    cand = pick_active(cands)
    if cand is None:
        log.info("未发现正在运行的 QMT 客户端（候选 %d 个），跳过自动连接", len(cands))
        return ""
    fields = build_connection(cand)
    if fields is None:
        log.warning("候选客户端缺少资金账号或客户端路径，跳过自动连接：%s",
                    cand.get("root") or cand.get("name"))
        return ""

    try:
        existing = state.broker_manager.find_by_identity(
            fields["broker_id"], fields["client_path"], fields["account_id"])
        if existing is not None:
            # 复用前先修路径：持久化写法（客户端根）与探测到的数据目录
            # （userdata_mini / userdata）常常不一致，不改会让这条连接一直连不上，
            # 又因为它「已存在」把自动连接挡在外面。只在未连上时修（能连上就别动）。
            if not existing.connected:
                await asyncio.to_thread(
                    state.broker_manager.repair_client_path,
                    existing.cfg.conn_id, fields["client_path"],
                    fields.get("client_mode") or "")
            conn = await asyncio.to_thread(
                state.broker_manager.activate, existing.cfg.conn_id)
            conn.connected = conn.adapter.is_connected()
            log.info("已复用既有连接接入本机 QMT 客户端：%s（账号 %s / %s）connected=%s",
                     conn.cfg.name, conn.cfg.account_id, conn.cfg.client_path, conn.connected)
            return conn.cfg.conn_id
        conn = await asyncio.to_thread(
            state.broker_manager.add_connection, ConnectionConfig(**fields), True)
    except Exception as exc:  # noqa: BLE001
        log.warning("自动连接失败（已跳过，不影响启动）：%s", exc)
        return ""

    conn.connected = conn.adapter.is_connected()
    log.info("已自动连接本机 QMT 客户端：%s（账号 %s / %s）connected=%s",
             fields["name"], fields["account_id"], fields["client_path"], conn.connected)
    return conn.cfg.conn_id


async def start_persisted_connections(mgr) -> dict:
    """拉起所有「应保持连接」的持久连接，返回 ``{started, failed, skipped}``。

    ★ 单独抽成函数（而非内联在 ``setup`` 里）是为了**可测**：启动期逻辑最难验证，
    内联就只能用真库真适配器跑，而真适配器在 CI 里根本不存在。抽出来后可用
    桩 manager 断言「无效路径被跳过、且不占用 32s 超时预算」。

    ``skipped`` = 客户端路径不存在的连接（直接跳过，不尝试连接）。
    """
    started = 0
    failed = 0
    skipped = 0
    for conn in mgr.all_connections():
        if not conn.cfg.active:
            continue
        # 路径不存在 ⇒ 不可能连上，**立刻跳过**而不是烧掉 32s 超时。
        # 这类条目是历史/测试残留（界面已标「路径无效」），等待它们只会拖慢启动：
        # 每条 32s，十几条就把「打开软件」变成一件要等几分钟的事。
        if not path_usable(conn.cfg.client_path):
            conn.connected = False
            conn.last_error = f"客户端路径不存在：{conn.cfg.client_path}"
            skipped += 1
            log.warning("跳过无效连接 %s（%s）：客户端路径不存在 %s —— 请在「连接管理」中修正或删除",
                        conn.cfg.name, conn.cfg.conn_id, conn.cfg.client_path)
            continue
        try:
            await asyncio.wait_for(conn.bridge.start(), timeout=32.0)
            conn.connected = conn.adapter.is_connected()
            log.info("broker connection started: %s (%s) connected=%s",
                     conn.cfg.name, conn.cfg.conn_id, conn.connected)
            started += 1
        except asyncio.TimeoutError:
            log.warning("broker start timed out (32s) for %s: 券商客户端未就绪，已跳过自动连接",
                        conn.cfg.conn_id)
            conn.connected = False
            failed += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("broker start failed %s: %s", conn.cfg.conn_id, exc)
            failed += 1
    return {"started": started, "failed": failed, "skipped": skipped}


async def setup(app: FastAPI) -> dict:
    init_broker_manager()  # V10 A2：core 延迟绑定，避免在 core 顶层 import xtquant_client
    # V10 A4：注入连接事件指标回调（xtquant_client 不再反向依赖 gateway.metrics）
    try:
        from gateway.metrics import get_metrics
        state.broker_manager.metrics_fn = (
            lambda conn_id, ev: get_metrics().record_conn_event(conn_id, ev))
    except Exception:  # noqa: BLE001
        pass
    state.broker_manager.load_persisted()
    _bootstrap_from_env()

    stats = await start_persisted_connections(state.broker_manager)

    state.bridge = state.broker_manager.active_bridge()
    state.gateway = state.bridge.gateway if state.bridge else None

    # 启动自动连接：**没有任何「应保持连接」的连接**时才探测并接入本机运行中的客户端。
    #
    # 判据是「有没有 cfg.active 的连接」而不是「连接列表是否为空」，也不是
    # 「有没有活跃 bridge」：
    # - 用「没有活跃 bridge」会在「用户配了连接但客户端当时没开」时每次启动都再塞
    #   一条新连接，列表随启动次数膨胀（实测三次启动累积出 3 条重复连接）；
    # - 用「列表是否为空」则「用户手动断开过」之后就再也不会自动接了 —— 而手动断开
    #   恰恰最常见的原因是「当时客户端没开」，下次启动正应该自动接上。
    # `cfg.active` 是**持久意图**（用户显式断开才熄灭），因此这个判据既幂等
    # （复用既有连接，见 `_auto_connect_active`）又能覆盖上述两种情形。
    auto_conn_id = ""
    # ★ 路径不存在的连接不算「意图」：它永远连不上，若把它当意图就会**永久挡住**
    # 自动连接 —— 用户明明开着客户端，软件却始终连不上，且界面只显示一堆无效条目。
    has_intent = any(
        c.cfg.active and path_usable(c.cfg.client_path)
        for c in state.broker_manager.all_connections()
    )
    if settings.broker_auto_connect and not has_intent:
        auto_conn_id = await _auto_connect_active()
        if auto_conn_id:
            state.bridge = state.broker_manager.active_bridge()
            state.gateway = state.bridge.gateway if state.bridge else None
            stats["started"] += 1

    # 交易日历感知调度
    from gateway.trading_session import default_session
    try:
        if state.bridge is not None:
            cal = await state.bridge.call(state.bridge.gateway.get_trading_calendar)
            if not default_session.refresh_from_calendar(cal or []):
                default_session.use_fallback()
    except Exception as exc:  # noqa: BLE001
        default_session.use_fallback()
        log.warning("trading calendar unavailable, fallback weekday rule: %s", exc)
    log.info("trading session: %s", default_session.stats())

    state.started_at = time.time()
    return {**stats, "auto_connected": auto_conn_id}


__all__ = ["setup", "path_usable", "start_persisted_connections"]


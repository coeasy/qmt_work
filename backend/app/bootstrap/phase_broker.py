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
from core.errors import swallow
from core.state import state
from core.state import init_broker_manager
from xtquant_client.base import brief_error
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


async def _detect_candidates_budgeted(timeout: float = 60.0) -> list:
    """探测本机 QMT 客户端候选 —— **每轮只探一次**，结果同时喂给路径 A 与路径 B。

    ★ 为什么必须只探一次（2026-10-14 实测）：``detect_candidates`` 是全盘查找
    （本机实测 **2.6~3.3s**，4 个候选），而 ``detect_bridge_agents(cands=None)``
    会**自己再探一遍**。于是 ``_auto_connect_active`` 一轮就是两遍全盘扫描；
    守护每 30s 跑一轮 ⇒ 每 30s 白烧 ~6s 磁盘 IO，且是**常驻**行为
    （「先开软件、后开券商」时守护会一直探下去）。故这里探一次、显式传入。

    失败/超时返回 ``[]``（**不是 None**）：调用方按「本轮没找到候选」处理，
    下轮守护自然重试 —— 「探测失败」与「机器上确实没有 QMT」走同一条出路。
    """
    from xtquant_client.autoconnect import detect_candidates

    try:
        return await asyncio.wait_for(asyncio.to_thread(detect_candidates), timeout=timeout)
    except asyncio.TimeoutError:
        log.warning("自动探测本机 QMT 客户端超时（%.0fs），跳过本轮自动连接", timeout)
        return []
    except Exception as exc:  # noqa: BLE001
        log.warning("自动探测本机 QMT 客户端失败，跳过本轮自动连接：%s", exc)
        return []


async def _bounded_connect_action(fn, *args) -> tuple[object, bool]:
    """带**硬超时**执行一次会触发 ``adapter.start()`` 的同步动作。

    返回 ``(结果, True)``；超时返回 ``(None, False)``。

    ★ 为什么必须有（2026-10-15 真机取证：路径 A 慢失败饿死路径 B）
    ------------------------------------------------------------
    ``add_connection(cfg, True)`` 与 ``activate()`` 内部都走
    ``_safe_start`` → ``adapter.start()``。路径 A 在本机（券商授权
    ``mdl_auth_gt_ipc_pair=0`` 让 ``XtQuantServer`` 起不来）是**慢失败**：
    ``bridge_client._READY_TIMEOUT=100s`` + 连接重试预算，实测整轮 41.87s 起。
    旧实现对这两个调用**没有 bound** ⇒ 路径 A 挂起时 ``_auto_connect_active``
    永远走不到路径 B（大 QMT 文件桥）—— 这正是「客户端没有自动连接大 QMT」
    的时序根因之一（另一条是守护首轮晚 30s，见 ``phase_watchdogs``）。

    超时**不取消底层线程**（``to_thread`` 取消不掉，取消只会让状态停在半路）：
    此刻连接对象已在 manager 里注册（``_build`` 先注册再 ``_safe_start``），
    由 ``BrokerHealthMonitor`` 的后台重连接管结局；本函数按「未连通」返回，
    让调用方继续走路径 B。

    注：``CONNECT_HARD_TIMEOUT`` 定义在本模块后方（启动预算常量区），函数体
    在运行时才解析该名字，前向引用无碍。
    """
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(fn, *args), timeout=CONNECT_HARD_TIMEOUT)
        return result, True
    except asyncio.TimeoutError:
        return None, False


async def _auto_connect_qmt_client(cands: list) -> tuple[str, bool]:
    """启动自动连接（**路径 A**：接入 ``cands`` 里**正在运行**的 QMT 客户端）。

    先按「券商 + 客户端路径 + 资金账号」找既有连接：**有就复用**（重新点亮持久意图
    并拉起），没有才新建 —— 否则「用户手动断开过 → 下次启动又新建一条」会让连接
    列表随启动次数膨胀（实测踩过：三次启动累积出 3 条重复的「迅投 XTQuant 22453951」）。

    全程**非阻断**：探测 / 建连的任何失败都只记日志，绝不抛出（启动流程优先）。
    返回 ``(conn_id, connected)``；未接入时 ``("", False)``。

    ★ 建连动作带 ``CONNECT_HARD_TIMEOUT``（32s）硬上限（2026-10-15）：本机路径 A
    是慢失败（41.87s 起），无 bound 时会把整轮自动连接拖死、路径 B 永远走不到。
    超时按「未连通」返回（连接已注册，交后台重连），调用方继续走文件桥。

    ★ 返回 ``connected`` 而不是只回 conn_id：``add_connection`` **建出连接对象**不等于
    连上了。旧实现只看「有没有拿到 conn_id」，于是路径 A 建出一条连不上的连接后
    就收工，**永远不会走到路径 B（文件桥）** —— 这正是「桥部署好了却从不自动接上」的成因。

    ``cands`` 由 ``_detect_candidates_budgeted`` 探好传入（本函数**不再自己探**）。
    """
    from xtquant_client.autoconnect import build_connection, pick_active

    cand = pick_active(cands)
    if cand is None:
        log.info("未发现正在运行的 QMT 客户端（候选 %d 个），跳过自动连接", len(cands))
        return "", False
    fields = build_connection(cand)
    if fields is None:
        log.warning("候选客户端缺少资金账号或客户端路径，跳过自动连接：%s",
                    cand.get("root") or cand.get("name"))
        return "", False

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
            conn, ok = await _bounded_connect_action(
                state.broker_manager.activate, existing.cfg.conn_id)
            if not ok:
                # activate 内部 _safe_start → adapter.start() 超过硬上限：线程仍在
                # 后台跑（连接已点亮 active），交 health monitor 重连；这里按
                # 「未连通」返回，让 _auto_connect_active 继续走路径 B。
                log.warning("路径 A 激活既有连接超时（%.0fs）：%s —— 已交后台重连，"
                            "继续探测大 QMT 文件桥", CONNECT_HARD_TIMEOUT,
                            existing.cfg.name)
                return existing.cfg.conn_id, False
            conn.connected = conn.adapter.is_connected()
            # ★ 措辞不得与事实冲突（2026-10-15）：旧实现无论连通与否都打
            #   「已复用既有连接**接入**本机 QMT 客户端 … connected=False」，
            #   一行日志里同时说「接入了」和「没连上」，排障时只能靠猜。
            if conn.connected:
                log.info("已复用既有连接接入本机 QMT 客户端：%s（账号 %s / %s）",
                         conn.cfg.name, conn.cfg.account_id, conn.cfg.client_path)
            else:
                log.warning("已复用既有连接但**未连通**：%s（账号 %s / %s）last_error=%s",
                            conn.cfg.name, conn.cfg.account_id, conn.cfg.client_path,
                            (getattr(conn, "last_error", "") or "无")[:200])
            return conn.cfg.conn_id, bool(conn.connected)
        conn, ok = await _bounded_connect_action(
            state.broker_manager.add_connection, ConnectionConfig(**fields), True)
        if not ok:
            # 超时时连接已注册（_build 先注册再 _safe_start），按身份回查报结局；
            # 回查不到（极少见：注册前就失败）按空串处理，调用方继续走路径 B。
            cid = ""
            try:
                found = state.broker_manager.find_by_identity(
                    fields["broker_id"], fields["client_path"], fields["account_id"])
                cid = found.cfg.conn_id if found is not None else ""
            except Exception as exc:  # noqa: BLE001 — 回查失败不影响继续走 B
                log.debug("建连超时后按身份回查连接失败：%s", exc)
            log.warning("路径 A 建连超时（%.0fs）：%s（conn_id=%s）—— 已交后台重连，"
                        "继续探测大 QMT 文件桥", CONNECT_HARD_TIMEOUT,
                        fields["name"], cid or "?")
            return cid, False
    except Exception as exc:  # noqa: BLE001
        log.warning("自动连接失败（已跳过，不影响启动）：%s", exc)
        return "", False

    conn.connected = conn.adapter.is_connected()
    if conn.connected:
        log.info("已自动连接本机 QMT 客户端：%s（账号 %s / %s）",
                 fields["name"], fields["account_id"], fields["client_path"])
    else:
        log.warning("已建出连接但**未连通**：%s（账号 %s / %s）last_error=%s",
                    fields["name"], fields["account_id"], fields["client_path"],
                    (getattr(conn, "last_error", "") or "无")[:200])
    return conn.cfg.conn_id, bool(conn.connected)


def _find_bridge_conn(bridge_dir: str):
    """按 ``connector_key`` + 归一化 ``bridge_dir`` 找既有的桥连接。

    ★ 不能复用 ``find_by_identity``：它**刻意跳过** ``connector_key`` 非空的连接
    （桥没有 ``client_path``，参与路径身份匹配会互相误复用）。所以桥的幂等必须
    单独按「同一个桥目录」判 —— 否则每次启动都会新增一条指向同一桥的连接，
    正是 ``find_by_identity`` 当初要消灭的那类膨胀。
    """
    want = os.path.normcase(os.path.normpath(bridge_dir or ""))
    if not want:
        return None
    for conn in state.broker_manager.all_connections():
        if not conn.cfg.connector_key:
            continue
        if os.path.normcase(os.path.normpath(conn.cfg.bridge_dir or "")) == want:
            return conn
    return None


async def _auto_connect_bridge(cands: list) -> str:
    """启动自动连接（**路径 B 兜底**：探测本机已部署且在跑的大 QMT 文件桥 agent）。

    为什么必须有（2026-10-14 用户实测反馈「客户端没有自动连接大 qmt」）
    ----------------------------------------------------------------
    当券商把 ``mdl_auth_gt_ipc_pair`` 关成 0 时，客户端日志出现
    ``CIPCManager::init, not auth:mdl_auth_gt_ipc_pair`` 与
    ``The XtQuantServer is not allowed to start.`` —— **路径 A 在这台机器上永远
    不可能成功**，唯一可用的交易通道是跑在大 QMT 内置 Python 策略里的 agent + 文件桥。
    旧实现只做路径 A 的探测，于是「QMT 明明开着、桥也部署好了，客户端却永远不自动
    接上大 QMT」，用户只能手填 ``bridge_dir`` / ``auth_token``。

    口径与 ``detect_bridge_agents`` 一致（只读、不猜账号、不生成 token）；
    已存在同 ``bridge_dir`` 的连接时**复用**而不是新建。全程非阻断。
    """
    from xtquant_client.autoconnect import build_bridge_connection, detect_bridge_agents

    try:
        # ★ 传入 cands：让桥探测**复用**本轮已探好的 QMT 根，而不是再全盘扫一遍
        #   （detect_bridge_agents(candidates=None) 会自己调 detect_candidates，
        #   实测 ~3s；守护每 30s 一轮 ⇒ 这一处就是常驻的重复 IO）。
        found = await asyncio.wait_for(
            asyncio.to_thread(detect_bridge_agents, cands), timeout=30.0)
    except asyncio.TimeoutError:
        log.warning("探测大 QMT 文件桥超时（30s），跳过桥接自动连接")
        return ""
    except Exception as exc:  # noqa: BLE001
        log.warning("探测大 QMT 文件桥失败，跳过桥接自动连接：%s", exc)
        return ""

    for item in found or []:
        fields = build_bridge_connection(item)
        if fields is None:
            continue
        existing = _find_bridge_conn(fields["bridge_dir"])
        try:
            if existing is not None:
                # 配置漂移（token 换了 / 账号补上了）时必须同步，否则复用旧配置
                # 会一直握手失败，而用户看到的是「明明配对了却连不上」。
                await asyncio.to_thread(
                    state.broker_manager.repair_bridge_config,
                    existing.cfg.conn_id, fields["bridge_dir"],
                    fields["auth_token"], fields["account_id"],
                    fields["account_type"])
                conn, ok = await _bounded_connect_action(
                    state.broker_manager.activate, existing.cfg.conn_id)
            else:
                conn, ok = await _bounded_connect_action(
                    state.broker_manager.add_connection,
                    ConnectionConfig(**fields), True)
            if not ok:
                # 与路径 A 同一族：握手挂起（agent 假活 / 文件 IO 卡住）不得拖住
                # 整轮自动连接。连接已注册，交 health monitor 后台重连。
                log.warning("大 QMT 文件桥建连超时（%.0fs）：%s（bridge_dir=%s）—— "
                            "已交后台重连", CONNECT_HARD_TIMEOUT,
                            fields.get("name") or "?",
                            fields.get("bridge_dir") or "")
                continue
        except Exception as exc:  # noqa: BLE001
            log.warning("桥接自动连接失败（已跳过，不影响启动）：%s", exc)
            continue
        conn.connected = conn.adapter.is_connected()
        if not conn.connected:
            # ★ 建连 ≠ 连通（同上）：桥 agent 探测到了、连接对象也建出来了，但
            #   握手没成功时不得返回 conn_id —— 否则调用方会据此打「已接入」、
            #   刷活跃指针，界面一路绿灯而查询全 503。
            log.warning("大 QMT 文件桥已建连但**未连通**：%s（bridge_dir=%s, agent=%s, "
                        "心跳 %.0fs 前）last_error=%s",
                        conn.cfg.name, item.get("bridge_dir"), item.get("agent_ver"),
                        item.get("heartbeat_age_s") or 0.0,
                        (getattr(conn, "last_error", "") or "无")[:200])
            continue
        log.info("已自动连接大 QMT 文件桥：%s（bridge_dir=%s, agent=%s, 心跳 %.0fs 前, "
                 "runtime_mode=%s）",
                 conn.cfg.name, item.get("bridge_dir"), item.get("agent_ver"),
                 item.get("heartbeat_age_s") or 0.0,
                 item.get("runtime_mode") or "?")
        return conn.cfg.conn_id
    return ""


async def _auto_connect_active() -> str:
    """启动自动连接总入口：先路径 A（xtquant 直连），连不上再兜底路径 B（文件桥）。

    ★ 为什么不是「A 拿到 conn_id 就收工」：``add_connection`` **建出连接对象**并不
    代表连上了 —— 本机实测（2026-10-14）它建出「集中交易001（自动连接）」后
    ``connected=false``，``last_error=交易连接异常：WaitingFreeWriter instances
    exceed maximum limit``（根因是券商授权 ``mdl_auth_gt_ipc_pair=0`` 让
    ``XtQuantServer`` 起不来）。旧实现只看 conn_id 是否非空，于是**永远不会
    走到路径 B** —— 这正是「桥部署好了却从不自动接上」的成因。
    现在以 ``adapter.is_connected()`` 为准：A 真连上才收工，否则继续试 B。

    返回**真正连通**的 conn_id（A 连上 → A；否则 B 连上 → B；都没连上 → ""）。

    ★ 2026-10-15：返回值语义收紧为「连通」而非「建连」。旧实现 ``bid or cid``
    会把「路径 A 建出了连接对象但没连上」的 conn_id 也返回，于是守护每 30s
    打一条 ``broker auto-connect guard connected: xxx`` 并刷新活跃指针，而那条
    连接 ``connected=false`` —— 日志全是绿灯、界面全是红灯，排障无从下手。
    **建出连接对象是副作用，不是「接入成功」的证据**；调用方只该信返回值。

    ★ 候选**只探一次**（``_detect_candidates_budgeted``）再分发给 A / B：见该函数
    的 docstring（重复全盘扫描是本机实测 ~3s/次 × 每 30s 一轮的常驻浪费）。
    """
    cands = await _detect_candidates_budgeted()
    cid, ok = await _auto_connect_qmt_client(cands)
    if cid and ok:
        return cid
    if cid:
        log.info("路径 A 已建连但未连通（conn_id=%s），继续探测大 QMT 文件桥", cid)
    return await _auto_connect_bridge(cands)


#: 启动期给「持久连接」的**总**等待预算（秒）。
#:
#: ★ 为什么必须有：``broker`` 相位在 ``lifecycle.PHASE_LEVELS`` 里是 **optional**，
#:   但「optional」只保证「**失败**不阻断 READY」—— ``run_phases`` 对每个相位是
#:   顺序 ``await`` 的，**「慢」照样阻断**。实测（2026-09-23 ``client.log``，本机
#:   「客户端已安装但未启动」这一最常见情形）：
#:
#:       ERROR  bridge 握手失败: _ping 调用超时 30.0s
#:       INFO   bootstrap phase broker (optional) done in 32011ms
#:       INFO   Application startup complete      <-- 这之后 uvicorn 才开始收请求
#:
#:   也就是说这 32s 里**整个 HTTP 服务一个字都不回**，用户看到的是「软件打不开」。
#:
#: 取值依据（来自 ``bridge_client`` 的设计注释）：客户端**已登录**时 ``_ping``
#: 5s 内返回；**未登录**时 SDK 要 10~30s 才阻塞/异常。8s 因此能干净切开
#: 「能连上」与「连不上」两类，既不误杀慢机器冷启动，也不再陪跑满 30s。
STARTUP_CONNECT_BUDGET = 8.0

#: 单条连接的**硬**上限（秒）。它不是启动预算：预算到点就把连接交还后台继续跑，
#: 这里只是兜底，防止某条连接的启动永久挂起。与 ``bridge_client._HANDSHAKE_TIMEOUT``
#: （30s）对齐并留 2s 余量，使「后台继续跑」的行为与改动前完全一致。
CONNECT_HARD_TIMEOUT = 32.0

#: 交易日历拉取的超时（秒）。券商 ``bridge.call`` 内部**没有** wait_for，
#: 接口卡住时这里会**无限等待**，把启动与「连接晚到后的重刷」一起堵死（R25 补齐）。
#: 超时即退回「工作日」规则 —— 日历只影响会话判定，不值得为它拖住任何东西。
CALENDAR_FETCH_TIMEOUT = 5.0

#: 启动预算内没跑完、仍在后台拉起的任务。**必须持强引用**：asyncio 只保留任务的
#: 弱引用，fire-and-forget 的任务可能在跑完之前就被 GC 掉（经典陷阱），
#: 表现为「连接永远停在半路」。
_bg_start_tasks: set[asyncio.Task] = set()


def _sync_active_bridge() -> None:
    """把进程级活跃指针同步为 manager 当前活跃连接（幂等，任何时刻可调用）。

    ★ 为什么不能只在 ``setup`` 末尾同步一次：预算内没连上的连接是在**之后**才连上的，
    而 ``state.bridge`` / ``state.gateway`` 只在装配期赋值 —— 不同步的话，
    「先开软件、后开 QMT 客户端」（最常见路径）会变成「连上了但活跃指针是空的」。

    注：**业务侧一律走 ``broker_manager.active_bridge()``（动态）**。``state.bridge`` /
    ``state.gateway`` 只是「装配期快照」，自 P1-4 起**没有任何读点** ——
    交易日历刷新也已改为直接问 manager。保留这两个槽位是为了兼容旧调用点的
    属性存在性（`hasattr`），不再作为数据来源。
    """
    state.cache_active_bridge()


async def _refresh_trading_calendar() -> None:
    """用活跃券商拉交易日历刷新会话判定；不可用则退回「工作日」规则（永不抛）。

    抽成函数是为了能在**连接晚到**时再刷一次：启动预算内没连上时先按 fallback
    运行，等后台把连接拉起来后再校正 —— 否则整个进程生命周期都停在 fallback
    （工作日规则会把节假日当交易日）。

    ★ P1-4：活跃连接**只问 ``broker_manager.active_bridge()``**，不再读
    ``state.bridge``。后者是**只写缓存**（见 :func:`_sync_active_bridge`），
    晚到连接若没走到同步点，缓存就停在 ``None`` —— 日历会永久停在 fallback。
    问 manager 是**动态**的，不存在「缓存过期」这一状态。
    """
    from gateway.trading_session import default_session
    mgr = state.broker_manager
    bridge = mgr.active_bridge() if mgr is not None else None
    try:
        if bridge is not None:
            # ★ 必须带超时（R25）：券商 `call` 内部没有 wait_for，接口卡住时
            #   这里会无限等待 —— 既堵启动，也堵「连接晚到后重刷日历」那条路。
            cal = await asyncio.wait_for(
                bridge.call(bridge.gateway.get_trading_calendar),
                timeout=CALENDAR_FETCH_TIMEOUT)
            if not default_session.refresh_from_calendar(cal or []):
                default_session.use_fallback()
    except asyncio.TimeoutError:
        default_session.use_fallback()
        log.warning("交易日历获取超时（%.0fs），改用工作日规则", CALENDAR_FETCH_TIMEOUT)
    except Exception as exc:  # noqa: BLE001
        default_session.use_fallback()
        log.warning("trading calendar unavailable, fallback weekday rule: %s", exc)
    log.info("trading session: %s", default_session.stats())


async def _start_one(conn) -> str:
    """拉起一条连接，返回 ``"started"`` / ``"failed"``（**绝不抛异常**）。"""
    try:
        await asyncio.wait_for(conn.bridge.start(), timeout=CONNECT_HARD_TIMEOUT)
    except asyncio.TimeoutError:
        conn.connected = False
        conn.last_error = f"连接超时（{CONNECT_HARD_TIMEOUT:.0f}s）：券商客户端未就绪"
        log.warning("broker start timed out (%.0fs) for %s: 券商客户端未就绪，已跳过自动连接",
                    CONNECT_HARD_TIMEOUT, conn.cfg.conn_id)
        return "failed"
    except Exception as exc:  # noqa: BLE001
        conn.connected = False
        conn.last_error = brief_error(exc, 500)
        log.warning("broker start failed %s: %s", conn.cfg.conn_id, exc)
        return "failed"
    conn.connected = conn.adapter.is_connected()
    log.info("broker connection started: %s (%s) connected=%s",
             conn.cfg.name, conn.cfg.conn_id, conn.connected)
    return "started"


def _hand_off_to_background(task: asyncio.Task, conn) -> None:
    """把「预算内没跑完」的连接交给后台：它结束时同步活跃指针 + 校正交易日历。

    交接而非取消：``bridge.start()`` 的真实工作在 ``run_in_executor`` 的线程里跑，
    取消协程**停不掉那个线程**，只会让状态停在半路；让它自然跑完（``_start_one``
    内部仍有 32s 硬上限）反而是确定的行为，与改动前一致。
    """
    _bg_start_tasks.add(task)

    def _on_done(t: asyncio.Task) -> None:
        _bg_start_tasks.discard(t)
        try:
            outcome = t.result()
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001
            log.warning("后台拉起连接 %s 异常：%s", conn.cfg.conn_id, exc)
            return
        log.info("后台拉起连接 %s 结束：%s（connected=%s）",
                 conn.cfg.conn_id, outcome, conn.connected)
        if outcome != "started":
            return
        _sync_active_bridge()
        try:
            _cal = asyncio.get_running_loop().create_task(_refresh_trading_calendar())
            # 同样必须持**强引用**：这是 fire-and-forget，asyncio 只保留弱引用，
            # 任务可能在跑完前被 GC（R25 补齐 —— 与 _bg_start_tasks 的用法一致）。
            _bg_start_tasks.add(_cal)
            _cal.add_done_callback(_bg_start_tasks.discard)
        except RuntimeError as exc:
            # 停机中：事件循环已关闭，没有可调度的地方，也没有人再需要这份日历。
            # 用 swallow 而不是 `pass` —— 静默失败是本项目最难排查的一类问题。
            swallow(exc, why="停机中事件循环已关闭，交易日历无需再校正", logger=log)

    task.add_done_callback(_on_done)


async def start_persisted_connections(mgr, *,
                                      budget: float = STARTUP_CONNECT_BUDGET) -> dict:
    """拉起所有「应保持连接」的持久连接，返回
    ``{started, failed, skipped, deferred}``。

    ★ 单独抽成函数（而非内联在 ``setup`` 里）是为了**可测**：启动期逻辑最难验证，
    内联就只能用真库真适配器跑，而真适配器在 CI 里根本不存在。抽出来后可用
    桩 manager 断言「无效路径被跳过」「慢连接不占满启动预算」。

    计数语义（四类**互不混淆**，尤其别把「慢」算成「失败」）：

    - ``started``  : 预算内连上；
    - ``failed``   : 预算内**确定失败**（超时 / 异常），已写 ``last_error``；
    - ``skipped``  : 客户端路径不存在 ⇒ 直接跳过，不尝试连接（历史/测试残留）；
    - ``deferred`` : 预算内还没跑完 —— **不是失败**，已交给后台自愈
      （``gateway.health.BrokerHealthMonitor`` 的指数退避重连 +
      ``phase_watchdogs`` 的 30s 自动连接守护），启动流程不再等它。

    ``budget`` 是**总预算**（不是每条）：所有连接**并发**拉起、共享同一份预算，
    因此 N 条连接的启动耗时与 1 条相当。顺序 ``await`` 会让耗时随 N 线性增长
    —— 「十几条残留连接把启动拖到 ~90s」就是这么来的。
    """
    started = 0
    failed = 0
    skipped = 0
    deferred = 0
    eligible = []
    for conn in mgr.all_connections():
        if not conn.cfg.active:
            continue
        # 路径不存在 ⇒ 不可能连上，**立刻跳过**而不是烧掉超时预算。
        # 这类条目是历史/测试残留（界面已标「路径无效」），等待它们只会拖慢启动：
        # 每条 30s+，十几条就把「打开软件」变成一件要等几分钟的事。
        if not path_usable(conn.cfg.client_path):
            conn.connected = False
            conn.last_error = f"客户端路径不存在：{conn.cfg.client_path}"
            skipped += 1
            log.warning("跳过无效连接 %s（%s）：客户端路径不存在 %s —— 请在「连接管理」中修正或删除",
                        conn.cfg.name, conn.cfg.conn_id, conn.cfg.client_path)
            continue
        eligible.append(conn)

    if eligible:
        tasks = {asyncio.ensure_future(_start_one(c)): c for c in eligible}
        done, pending = await asyncio.wait(set(tasks), timeout=budget)
        for t in done:
            if t.result() == "started":
                started += 1
            else:
                failed += 1
        for t in pending:
            conn = tasks[t]
            conn.connected = False
            # 「慢」与「失败」必须分开归因：这里给的是**出路**（后台会继续连），
            # 不是判决。若写成「连接失败」，用户会跑去改配置 —— 而其实什么都不用做。
            conn.last_error = (f"启动期 {budget:.0f}s 内未连上（券商客户端可能未启动或未登录），"
                               f"已转入后台自动重连")
            log.warning("启动期 %s（%s）未在 %.0fs 内连上 —— 已转入后台自动重连，不阻断启动",
                        conn.cfg.conn_id, conn.cfg.name, budget)
            _hand_off_to_background(t, conn)
            deferred += 1

    return {"started": started, "failed": failed,
            "skipped": skipped, "deferred": deferred}


async def setup(app: FastAPI) -> dict:
    init_broker_manager()  # V10 A2：core 延迟绑定，避免在 core 顶层 import xtquant_client
    # V10 A4：注入连接事件指标回调（xtquant_client 不再反向依赖 gateway.metrics）
    try:
        from gateway.metrics import get_metrics
        state.broker_manager.metrics_fn = (
            lambda conn_id, ev: get_metrics().record_conn_event(conn_id, ev))
        # 运行时模式指标（in_process / bridge）：同样由 app 层注入。
        # 它此前**从未被写入**过 —— /metrics 里 qmt_runtime_mode 永远是空的。
        state.broker_manager.runtime_mode_fn = (
            lambda conn_id, mode: get_metrics().record_runtime_mode(conn_id, mode))
    except Exception:  # noqa: BLE001
        pass
    state.broker_manager.load_persisted()
    _bootstrap_from_env()

    stats = await start_persisted_connections(state.broker_manager)

    _sync_active_bridge()

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
        # ★ 自动连接同样受启动预算约束（R25 补齐）：`_auto_connect_active` 内部的
        #   客户端探测最长 60s，此前**在预算之外** await ⇒ optional 相位照样能堵住
        #   启动 60s（R24 只包住了 start_persisted_connections，这里是漏网的那条）。
        #   超时不影响最终结果：`_broker_auto_connect_guard` 每 30s 复探一次，
        #   接上后会重同步 state.bridge。
        try:
            auto_conn_id = await asyncio.wait_for(
                _auto_connect_active(), timeout=STARTUP_CONNECT_BUDGET)
        except asyncio.TimeoutError:
            log.warning("启动期 %.0fs 内未完成本机客户端探测 —— 已交给后台自动连接守卫，"
                        "不阻断启动", STARTUP_CONNECT_BUDGET)
            auto_conn_id = ""
        if auto_conn_id:
            _sync_active_bridge()
            stats["started"] += 1

    # 交易日历感知调度
    await _refresh_trading_calendar()

    state.started_at = time.time()
    return {**stats, "auto_connected": auto_conn_id}


__all__ = ["setup", "path_usable", "start_persisted_connections",
           "STARTUP_CONNECT_BUDGET"]


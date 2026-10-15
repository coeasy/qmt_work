"""自动探测与「启动自动连接」决策。

为什么要单独成模块
------------------
「探测本机客户端 → 挑一个连上」这条链路有两个消费方：

1. REST 端点 ``GET /brokers/auto-detect``（用户点「重新探测」/ 进页面自动探测）；
2. 启动 bootstrap 的自动连接（``app/bootstrap/phase_broker.py``）。

两者**必须同源**：如果界面推荐的是 A 客户端、启动时自动连的是 B，用户会觉得
「软件自己在乱连」。因此把「丰富化候选 + 挑活跃项 + 组装连接字段」全部收在这里，
路由层与 bootstrap 都只调用本模块。

设计要点
--------
- ``detect_candidates``：唯一的候选丰富化实现（账号发现 / 券商档案 / 极速版改写）。
- ``pick_active``：**只挑「正在运行」的客户端**。未运行的客户端需要用户先在 GUI 里
  登录，自动拉起进程会弹出登录窗并可能让无人值守的启动路径卡住 —— 那不属于
  「自动连接」的语义，属于「自动启动客户端」，刻意不做。
- ``build_connection``：缺资金账号或路径时返回 ``None``，**绝不猜一个账号出来** ——
  猜错账号意味着把订单下到别人的户上。
"""
from __future__ import annotations

import json
import os
import time

from .discovery import discover, discover_accounts, guess_broker_id_by_name

__all__ = ["detect_candidates", "pick_active", "build_connection", "resolve_broker_id",
           "detect_bridge_agents", "build_bridge_connection",
           "BRIDGE_HEARTBEAT_MAX_AGE_S"]

#: 大 QMT 文件桥 agent 的默认连接器 key（见 ``connectors/registry.py``）。
BRIDGE_CONNECTOR_KEY = "qmt.big.bridge.file"

#: agent 心跳可接受的最大陈旧度（秒）。
#:
#: ★ 为什么是「小时级」而不是「秒级」：``agent_status.json`` 的 ``ts`` 由 agent 的
#:   写入节奏决定，**与运行形态有关** ——
#:     * 独立进程形态：``run_standalone`` 主循环里持续写，永远新鲜；
#:     * 公式（内置Python）形态：只在 ``handlebar`` 被 QMT 调用时写，
#:       **非交易时段/午休会自然停更**。
#:   若按秒级判新鲜，公式形态在收盘后会被误判成「没部署」⇒ 自动连接永远不生效，
#:   正是「客户端明明开着大 QMT 却从不自动接上」的成因。
#:   因此这里只把心跳当「**这份部署最近活过**」的粗筛，真伪交给连接自身的
#:   往返探活（``BigQmtBridge.is_connected`` 的 ``_liveness_probe``）判定。
BRIDGE_HEARTBEAT_MAX_AGE_S = 12 * 3600.0

#: 探测桥 agent 时最多看几个 QMT 候选根（避免大机群下扫描失控）。
_BRIDGE_SCAN_LIMIT = 8


def _read_json(path: str) -> dict:
    """读 JSON，任何失败都返回空 dict（探测路径绝不抛）。"""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 — 探测是只读尽力而为
        return {}


def _bridge_config_paths(qmt_root: str) -> list[str]:
    """agent_config.json 的候选位置（部署约定，按序回退）。

    与 ``agent_bigqmt`` bundle 的查找顺序一致：先 ``python/agent_config.json``，
    再旧布局 ``python/agent_bigqmt/agent_config.json``。
    """
    sdir = os.path.join(qmt_root, "python")
    return [os.path.join(sdir, "agent_config.json"),
            os.path.join(sdir, "agent_bigqmt", "agent_config.json")]


def detect_bridge_agents(candidates: list[dict] | None = None,
                         now: float | None = None) -> list[dict]:
    """探测本机**已部署且在跑**的大 QMT 文件桥 agent（路径 B）。

    为什么需要它（2026-10-14 用户实测反馈：「客户端没有自动连接大 qmt」）
    -------------------------------------------------------------------
    ``detect_candidates`` 只找**路径 A**（外部 xtquant 直连）的候选。当券商把
    ``mdl_auth_gt_ipc_pair`` 关成 0 时，客户端日志会出现
    ``CIPCManager::init, not auth:mdl_auth_gt_ipc_pair`` 与
    ``The XtQuantServer is not allowed to start.`` —— **路径 A 在本机根本不可能成功**，
    唯一可用的交易通道是跑在大 QMT 内置 Python 策略里的 agent + 文件桥。
    但那条连接的 ``bridge_dir`` / ``auth_token`` 只有部署者知道，自动连接从不认识它，
    于是「QMT 明明开着，客户端永远不自动接上大 QMT」，用户只能手填一遍。

    发现口径（**只读、无副作用、不猜**）
    ----------------------------------
    1. 从 QMT 候选根的 ``python/agent_config.json`` 读 agent 部署配置；
    2. 要求 ``bridge_dir`` 非空且目录存在（否则连上也无处投递请求）；
    3. 读 ``bridge_dir/agent_status.json``，要求 ``alive`` 为真且心跳不过期
       （窗口见 ``BRIDGE_HEARTBEAT_MAX_AGE_S``）—— **文件存在不算**，
       残留的过期心跳是「假存活」的主要来源；
    4. ``auth_token`` 直接沿用部署配置里的值（**不猜、不生成**：猜错 token 会被
       agent 侧拒绝，表现为「一直超时」，比不连更误导）。

    ``candidates`` 省略时内部调 ``detect_candidates()``（已含 QMT 根发现）。
    返回列表，每项形如 ``{"root", "config_path", "bridge_dir", "auth_token",
    "account_id", "transport", "status", "heartbeat_age_s", "agent_ver"}``。
    """
    cands = candidates if candidates is not None else detect_candidates()
    now = time.time() if now is None else now
    found: list[dict] = []
    seen_dirs: set[str] = set()

    for c in (cands or [])[:_BRIDGE_SCAN_LIMIT]:
        root = (c.get("root") or c.get("client_path") or "").strip()
        if not root:
            continue
        for cfg_path in _bridge_config_paths(root):
            if not os.path.isfile(cfg_path):
                continue
            cfg = _read_json(cfg_path)
            bridge_dir = str(cfg.get("bridge_dir") or "").strip()
            if not bridge_dir:
                continue
            # 归一化比较（去掉尾部斜杠 / 统一分隔符），避免同一条桥被重复登记
            key = os.path.normcase(os.path.normpath(bridge_dir))
            if key in seen_dirs:
                continue
            seen_dirs.add(key)
            if not os.path.isdir(bridge_dir):
                continue

            st_path = os.path.join(bridge_dir, "agent_status.json")
            st = _read_json(st_path)
            age = None
            ts_ms = st.get("ts")
            if isinstance(ts_ms, (int, float)) and ts_ms > 0:
                age = now - (float(ts_ms) / 1000.0)
            alive = bool(st.get("alive"))
            if not st or not alive:
                continue
            if age is None or age > BRIDGE_HEARTBEAT_MAX_AGE_S:
                # 心跳过期 ⇒ 这份部署近期没活过，不自动接入
                # （与 ``pick_active`` 拒绝自动连「没在跑的客户端」同一纪律）。
                continue

            acc = str(cfg.get("account_id") or c.get("default_account_id") or "").strip()
            found.append({
                "root": root,
                "config_path": cfg_path,
                "bridge_dir": bridge_dir,
                "auth_token": str(cfg.get("auth_token") or ""),
                "transport": str(cfg.get("transport") or "file"),
                "account_id": acc,
                "account_type": str(cfg.get("account_type") or "STOCK").upper(),
                "status_path": st_path,
                "heartbeat_age_s": round(age, 1) if age is not None else None,
                "agent_ver": str(st.get("agent_ver") or ""),
                "runtime_mode": str(st.get("runtime_mode") or ""),
                "injected": list(st.get("injected") or []),
            })
            break   # 同一根只用第一个存在的配置文件
    return found


def build_bridge_connection(found: dict) -> dict | None:
    """把 ``detect_bridge_agents`` 的结果转成 ``add_connection`` 的连接字段。

    返回 ``None`` = 不足以建连（缺 ``bridge_dir``）。``account_id`` 允许为空 ——
    桥的账户可由请求携带或在 ``agent_config.json`` 里指定，这里**不猜账号**
    （``build_connection`` 对路径 A 的同一条纪律）。
    """
    bridge_dir = str((found or {}).get("bridge_dir") or "").strip()
    if not bridge_dir:
        return None
    label = "大QMT桥接·文件"
    return {
        "conn_id": "",
        "name": "%s（自动连接）" % label,
        "broker_id": "generic",
        "client_path": "",
        "client_mode": "auto",
        "account_id": str(found.get("account_id") or ""),
        "account_type": str(found.get("account_type") or "STOCK"),
        "active": True,
        "connector_key": BRIDGE_CONNECTOR_KEY,
        "bridge_dir": bridge_dir,
        "auth_token": str(found.get("auth_token") or ""),
    }


def resolve_broker_id(cand: dict) -> str:
    """候选的券商档案 id（优先级：Config.xml 真实券商名 > 路径猜测 > generic）。

    只认「无歧义」的券商名关键词（光大/国信等确认归入 generic 通用迅投档案，
    广发/银河/国金等归各自档案）。路径缩写（gd=广发/光大）在这里不猜。
    真实券商名无法确定识别时不覆盖路径猜测，仍为空则归 generic —— 避免把
    目录明确的候选（如 银河/国金）在无账号时被误降级成 generic。
    """
    by_name = guess_broker_id_by_name(cand.get("broker_name") or "")
    if by_name:
        return by_name
    return cand.get("broker_id") or "generic"


def detect_candidates() -> list[dict]:
    """探测本机 QMT 候选并**就地丰富化**（资金账号 / 券商档案 / 客户端模式）。

    这是「自动探测」的唯一实现，REST 端点与启动自动连接都走它。
    单个候选的丰富化失败被隔离（该候选降级为「无账号 + generic」），不影响其它候选。
    """
    cands = discover()
    for c in cands:
        try:
            # 防御性归一化：c["root"] 可能缺失，发现接口也可能非 list
            scan_path = c.get("client_path") or c.get("root") or ""
            c["accounts"] = list(discover_accounts(scan_path) or [])
            if c["accounts"]:
                # 取第一个账号作为默认补全，供前端「一键填入」与启动自动连接使用
                c["default_account_id"] = c["accounts"][0]["account_id"]
                c["broker_name"] = (c["accounts"][0].get("broker_name")
                                    or c.get("broker_name", ""))
            else:
                c["default_account_id"] = ""

            c["broker_id"] = resolve_broker_id(c)

            # 通用档案的**兜底**建议：识别为 generic（含光大/国信等）且**无法从运行
            # 进程判定模式**时，优先极速版 userdata_mini —— 完整版大客户端未以
            # 「极简模式」登录时，对独立外部进程的接入更易被拒（历史 'illegal pid' 反馈），
            # 而极速版 MiniQMT(userdata_mini) 通常更稳。
            # ★ 2026-09-28：当候选已按**运行中进程**判定出模式（mode_source=="process"）
            #   时**不再改写** —— 用户实际启动的是哪个客户端就以哪个为准。旧实现在两
            #   目录并存时无条件改写为 mini，会把「完整版以极简模式在跑」的连接硬指向
            #   userdata_mini（错误数据目录 → 交易取不到数据），属孤儿逻辑。
            if (c.get("broker_id") == "generic" and c.get("has_userdata_mini")
                    and c.get("mode_source") != "process"):
                c["client_mode"] = "mini"
                cur = c.get("client_path") or ""
                if not cur.endswith("userdata_mini"):
                    c["client_path"] = c.get("client_path_mini") or cur
        except Exception:  # noqa: BLE001 — 单候选失败降级，不影响整体
            c["accounts"] = []
            c["default_account_id"] = ""
            if not c.get("broker_id"):
                c["broker_id"] = "generic"
    return cands


def _rank(cand: dict) -> tuple[int, int, int]:
    """活跃度打分：**运行中 + 有资金账号** > 仅运行中 > 仅有账号。

    三个分量都进元组（而不是只留总分），是为了让「运行中且无账号」仍然排在
    「未运行但有账号」之前 —— 前者只差一次账号发现，后者还差用户去登录客户端。
    """
    running = 1 if cand.get("running") else 0
    has_acc = 1 if (cand.get("default_account_id") or cand.get("accounts")) else 0
    return (running * 2 + has_acc, running, has_acc)


def pick_active(candidates: list[dict] | None) -> dict | None:
    """挑出「当前活跃客户端」：运行中且已读到资金账号的那个。

    返回 ``None`` 表示没有可自动接入的客户端（没装 / 没启动 / 没登录）。
    排序用 ``sorted``（稳定），同分时保持 ``discover()`` 的原始顺序，
    使「界面列表里的第一个推荐项」与「实际自动连的」一致。
    """
    usable = [c for c in (candidates or []) if isinstance(c, dict)]
    if not usable:
        return None
    best = sorted(usable, key=_rank, reverse=True)[0]
    # 未运行 → 不自动连（需要用户先登录客户端，见模块 docstring）
    if not best.get("running"):
        return None
    return best


def build_connection(cand: dict) -> dict | None:
    """把候选转成 ``BrokerManager.add_connection`` 所需的连接字段。

    返回 ``None`` = 该候选不足以建立连接（缺资金账号或客户端路径）。
    ``conn_id`` 留空由 manager 生成，避免与既有连接 id 冲突。
    """
    client_path = (cand.get("client_path") or cand.get("root") or "").strip()
    accounts = cand.get("accounts") or []
    acc_id = str(cand.get("default_account_id") or "").strip()
    if not acc_id and accounts:
        acc_id = str(accounts[0].get("account_id") or "").strip()
    if not client_path or not acc_id:
        return None

    acc_type = "STOCK"
    for a in accounts:
        if str(a.get("account_id") or "") == acc_id:
            acc_type = a.get("account_type") or "STOCK"
            break

    label = cand.get("broker_name") or cand.get("name") or "本机客户端"
    return {
        "conn_id": "",
        "name": f"{label}（自动连接）",
        "broker_id": resolve_broker_id(cand),
        "client_path": client_path,
        "client_mode": cand.get("client_mode") or "auto",
        "account_id": acc_id,
        "account_type": acc_type,
        "active": True,
    }

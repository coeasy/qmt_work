"""券商连接管理器（多券商 / 多账户 / 多客户端版本同时存在）。

- 每个连接 = 配置 + 适配器实例 + 专属线程桥（行情订阅按连接隔离）
- 连接配置持久化到 `broker_connections` 表；启动时可自动重连
- 对外暴露「当前活跃连接」与「指定连接」的 bridge，供 routes/tools/mcp/sync 使用
- 未连接任何券商时，`active_bridge()` 返回 None，上层返回明确的 503（不返回假数据）
"""
import asyncio
import logging
import os
import threading
import uuid
from dataclasses import dataclass, field

from core.db import get_db

from .base import BrokerAdapter
from .gateway import XTQuantBridge
from .registry import create_adapter, get_profile
from core.clock import now_iso

log = logging.getLogger("qmt_work.manager")


def _norm_path(p: str) -> str:
    """路径归一化（比较用）：Windows 大小写不敏感 + 斜杠/尾斜杠差异。

    用于判断「探测到的客户端」与「已有连接」是不是同一个 —— 直接比字符串会因
    `P:\\stock\\gd_qmt\\userdata_mini` 与 `p:/stock/gd_qmt/userdata_mini/` 判为不同，
    导致自动连接重复建连（同一账号多份连接）。
    """
    if not p:
        return ""
    return os.path.normcase(os.path.normpath(str(p).strip()))


def _version_profile(adapter: BrokerAdapter, allow_block: bool = True) -> dict | None:
    """提取适配器的版本画像（xtp 系提供；其余适配器无则返回 None）。

    兼容两种返回形态：XTPQuantAdapter 返回 QmtVersionProfile（需 .to_dict()），
    BridgeAdapter 的 version_profile() 已回传纯 dict（直接可用）。

    allow_block=False（高频路径专用，如 /brokers 轮询）：
        只取已缓存画像，绝不触发同步探测。画像探测成本极高（子进程 RPC 8s +
        本地静态目录/SDK 扫描，实测单次 ~47s），在 async 路由里同步调用会冻结
        FastAPI 事件循环 → 「连接管理」永远加载不出并拖垮整机。
        未命中缓存时返回 None 并在后台线程预热，下一轮轮询即可拿到画像。
    """
    getter = getattr(adapter, "version_profile", None)
    if not callable(getter):
        return None

    cached_getter = getattr(adapter, "version_profile_cached", None)
    warmer = getattr(adapter, "warm_version_profile", None)

    if not allow_block:
        if callable(cached_getter):
            try:
                val = cached_getter()
                if isinstance(val, dict):
                    return val
                to_dict = getattr(val, "to_dict", None)
                if callable(to_dict):
                    return to_dict()
            except Exception:  # noqa: BLE001
                pass
        # 后台预热：本轮先返回 None，画像稍后由缓存补齐
        if callable(warmer):
            try:
                warmer()
            except Exception:  # noqa: BLE001
                pass
        return None

    try:
        val = getter()
        if isinstance(val, dict):
            return val
        to_dict = getattr(val, "to_dict", None)
        return to_dict() if callable(to_dict) else None
    except Exception:  # noqa: BLE001  画像探测失败不影响连接状态列表
        return None


@dataclass
class ConnectionConfig:
    conn_id: str = ""
    name: str = ""
    broker_id: str = ""
    client_path: str = ""
    client_mode: str = "auto"  # auto/mini(极速版)/full(完整版大客户端)
    account_id: str = ""
    account_type: str = "STOCK"
    session_id: int = 0
    min_version: str = ""
    active: bool = False


@dataclass
class Connection:
    cfg: ConnectionConfig
    adapter: BrokerAdapter
    bridge: XTQuantBridge
    connected: bool = False
    health_status: str = "disconnected"
    last_error: str = ""
    reconnect_attempts: int = 0
    reconnect_task: object = field(default=None)


class BrokerManager:
    def __init__(self):
        self._conns: dict[str, Connection] = {}
        self._active_id: str | None = None
        self._lock = threading.Lock()
        # V10 A4：连接事件指标回调（app 层注入；xtquant_client 不反向依赖 gateway）
        self.metrics_fn = None

    # ---------------- 持久化加载 ----------------
    def load_persisted(self) -> None:
        db = get_db()
        rows = db.query("SELECT * FROM broker_connections ORDER BY id")
        for r in rows:
            try:
                cfg = ConnectionConfig(
                    conn_id=r.get("conn_id", ""), name=r.get("name", ""), broker_id=r.get("broker_id", ""),
                    client_path=r.get("client_path", ""), client_mode=r.get("client_mode", "") or "auto",
                    account_id=r.get("account_id", ""),
                    account_type=r.get("account_type", "STOCK"), session_id=int(r.get("session_id", 0) or 0),
                    min_version=r.get("min_version", "") or "", active=bool(r.get("active", False)))
                if not cfg.conn_id:
                    continue
                self._build(cfg, connect=False)
            except Exception as exc:  # noqa: BLE001  单条损坏行不阻断整体加载
                log.warning("load_persisted: 跳过损坏记录 %r: %s", r, exc)
        # 注意：这里不自动 start —— 由应用 lifespan 在事件循环上统一启动 active 连接，
        # 避免「load_persisted 一次性 loop 启动 + lifespan 再启动」造成子进程重复拉起。

    # ---------------- 增删改连 ----------------
    def _build(self, cfg: ConnectionConfig, connect: bool) -> Connection:
        if not cfg.conn_id:
            cfg.conn_id = uuid.uuid4().hex[:12]
        if not cfg.name:
            prof = get_profile(cfg.broker_id)
            cfg.name = prof.name if prof else cfg.broker_id
        adapter = create_adapter(cfg.broker_id, cfg.client_path, cfg.account_id,
                                 cfg.account_type, cfg.session_id, cfg.min_version,
                                 cfg.client_mode, metrics_fn=self.metrics_fn)
        conn = Connection(cfg=cfg, adapter=adapter, bridge=XTQuantBridge(adapter))
        self._conns[cfg.conn_id] = conn
        if connect:
            self._safe_start(cfg.conn_id)
        return conn

    def _safe_start(self, conn_id: str) -> None:
        """同步启动适配器（spawn 子进程 + 握手）。

        不做异步泵（那需要一个可靠运行的事件循环）——行情泵由应用主事件循环的
        `ensure_pump` 统一托管，避免在一次性/线程池 loop 上创建导致事件无法投递。
        """
        conn = self._conns.get(conn_id)
        if not conn:
            log.warning("_safe_start: 未知连接 %r", conn_id)
            return
        try:
            conn.adapter.start()  # 幂等：子进程已在运行则复用
            conn.connected = conn.adapter.is_connected()
            if conn.connected and self._active_id is None:
                self._active_id = conn_id
        except Exception as exc:  # noqa: BLE001
            conn.connected = False
            conn.last_error = str(exc)[:500]
            log.error("_safe_start %r 失败: %s", conn_id, exc)

    async def ensure_pump(self, conn_id: str) -> None:
        """在（应用主）事件循环上确保连接的行情泵已启动（幂等）。"""
        conn = self._conns.get(conn_id)
        if conn is None:
            return
        await conn.bridge.start()  # 幂等：gateway 已运行则复用，泵已存在则跳过

    def add_connection(self, cfg: ConnectionConfig, autoconnect: bool = True) -> Connection:
        with self._lock:
            conn = self._build(cfg, connect=autoconnect)
            self._persist(conn.cfg)
            if conn.connected and self._active_id is None:
                self._active_id = conn.cfg.conn_id
                self._persist_active(exclusive=False)
        return conn

    def connect(self, conn_id: str) -> dict:
        conn = self._conns.get(conn_id)
        if not conn:
            raise KeyError(f"未知连接：{conn_id}")
        # 阶段 5：连接前快速检查 QMT/恒生UF 客户端是否在运行。
        # 这是"无法连接正在运行的QMT客户端"反馈的核心：用户已开客户端但 SDK
        # 仍报"未登录"——根因是 client_path 填错或 client_path 指向 userdata_mini
        # 但 xtquant 在子目录。先用 discovery 做一次轻量探测，把根因提前给到用户。
        try:
            from .discovery import discover
            from .xtp import probe_environment
            client_path = conn.cfg.client_path or ""
            probe = probe_environment(client_path, light=True)
            # 路径不存在 / xtquant 未定位：提前抛错（带结构化诊断），避免 SDK 在子进程内阻塞
            if not probe.get("client_exists"):
                raise RuntimeError(
                    f"客户端路径不存在：{client_path}\n"
                    f"→ 请在「券商连接」页确认路径，"
                    f"通常为 ...\\客户端根\\userdata_mini（极速版）或 userdata（完整版）目录。")
            if not probe.get("xtquant_found"):
                # 进一步：扫描本机是否有运行中的 QMT 客户端，提示用户参考
                try:
                    cands = discover()
                    running = [c.get("root", "") for c in cands if c.get("running")]
                    hint = ""
                    if running:
                        hint = (f"\n→ 已在本机发现运行中的 QMT 客户端：{', '.join(running[:3])}。"
                                f"请确认「客户端路径」与之一致（极速版填 userdata_mini，"
                                f"完整版大客户端填 userdata）。")
                    else:
                        hint = "\n→ 未发现运行中的 QMT 客户端；请先启动并登录客户端。"
                    raise RuntimeError(
                        f"在「{client_path}」中未找到 xtquant SDK（xtquant/ 目录）。"
                        f"→ 请确认 client_path 指向客户端根或其数据目录"
                        f"（极速版 userdata_mini / 完整版 userdata）。{hint}")
                except RuntimeError:
                    raise
                except Exception:
                    raise RuntimeError(
                        f"在「{client_path}」中未找到 xtquant SDK（xtquant/ 目录）。"
                        f"→ 请确认 client_path 指向客户端根或其数据目录"
                        f"（极速版 userdata_mini / 完整版 userdata）。") from None
        except RuntimeError:
            # 探测发现的根因已包含可操作指引，直接透出
            raise
        except Exception as exc:  # noqa: BLE001  探测本身失败不阻断，但记录以便排障
            log.warning("connect 预探测失败（回退到 SDK 报错兜底）: %s", exc)
        conn.adapter.start()
        conn.connected = conn.adapter.is_connected()
        # 用户点了「连接」= 意图明确要它保持连接：内存与 DB 的 active 必须同义
        # （内存 `cfg.active` 决定健康监控是否自愈，DB 决定下次启动是否自动拉起）。
        if conn.connected and not conn.cfg.active:
            conn.cfg.active = True
            self._persist(conn.cfg)
        if conn.connected and self._active_id is None:
            self._active_id = conn_id
            self._persist_active(exclusive=False)
        res = conn.adapter.test_connection()
        # 打通「版本画像 → 前端」链路：连接成功即带上检测到的客户端类型/版本/能力，
        # 前端据此展示「当前连接的是完整版/极速版、支持哪些功能」（任何客户端版本通用）。
        res = dict(res)
        res["version_profile"] = _version_profile(conn.adapter)
        return res

    def disconnect(self, conn_id: str, keep_active: bool = False) -> None:
        """断开一条连接。

        `keep_active=True`：**只断运行时连接，保留「启动时自动连接」的持久意图**。
        优雅停机必须走这个分支 —— 否则每次退出都会把 `cfg.active` 抹成 False，
        下次启动既不自动拉起（`phase_broker` 只看 `cfg.active`）、又因为「连接列表
        非空」跳过自动连接 ⇒ **第二次启动开始永远连不上**（实测：持久化行
        `active=0`，`/brokers` 显示「已有连接 (1)」但「未连接」，行情通道空转）。
        """
        conn = self._conns.get(conn_id)
        if not conn:
            return
        # 阶段 0-D（C16）：手动断开必须清 active + 取消重连任务——
        # 原实现不清 active，健康监控在 5s 内把已断开连接自动重连回来；
        # 不取消 reconnect_task 则后台重连任务继续拉起子进程（已删除连接被"复活"）。
        if not keep_active and conn.cfg.active:
            conn.cfg.active = False
            self._persist(conn.cfg)
        if self._active_id == conn_id:
            self._active_id = None
            self._persist_active()
        rt = conn.reconnect_task
        if rt is not None:
            try:
                if isinstance(rt, asyncio.Task):
                    if not rt.done():
                        rt.cancel()
                elif callable(rt):
                    rt()
            except Exception:  # noqa: BLE001
                pass
            conn.reconnect_task = None
        try:
            conn.adapter.close()
        except Exception as exc:  # noqa: BLE001
            log.warning("disconnect %r close 失败: %s", conn_id, exc)
        conn.connected = False

    def disconnect_all(self, keep_active: bool = True) -> int:
        """断开全部连接（P2-7：优雅停机用；返回断开数量）。

        与逐个 disconnect 语义一致：清运行时活跃指针、取消重连任务、关闭适配器。
        单个连接失败不阻断其余连接的断开。

        ⚠️ 默认 `keep_active=True`：本方法**只用于停机**，停机不改用户的持久意图
        （「下次启动自动连接这些连接」）。要连持久意图一起清，显式传 False。
        """
        n = 0
        for conn_id in list(self._conns.keys()):
            try:
                self.disconnect(conn_id, keep_active=keep_active)
                n += 1
            except Exception as exc:  # noqa: BLE001
                log.warning("disconnect_all %r 失败: %s", conn_id, exc)
        return n

    def remove(self, conn_id: str) -> None:
        self.disconnect(conn_id)
        self._conns.pop(conn_id, None)
        db = get_db()
        db.execute("DELETE FROM broker_connections WHERE conn_id=?", (conn_id,))

    def test_connection(self, cfg: ConnectionConfig) -> dict:
        """在不影响已运行连接的前提下探测可用性（失败时附带结构化环境诊断）。"""
        if cfg.conn_id and cfg.conn_id in self._conns:
            conn = self._conns[cfg.conn_id]
            if conn.connected:
                res = dict(conn.adapter.test_connection())
                res["version_profile"] = _version_profile(conn.adapter)
                return res
        tmp = None
        try:
            # create_adapter 在 ABI 不兼容且无兼容运行时时会抛 BrokerSDKError；
            # 必须放在 try 内，否则会穿透为 500（此前 3.13 上「No module named ...」 的旧路径）
            tmp = create_adapter(cfg.broker_id, cfg.client_path, cfg.account_id,
                                 cfg.account_type, cfg.session_id, cfg.min_version,
                                 cfg.client_mode)
            tmp.start()
            res = dict(tmp.test_connection())
            res["version_profile"] = _version_profile(tmp)
            return res
        except Exception as exc:  # noqa: BLE001
            res = {"connected": False, "detail": str(exc)}
            # 附加环境诊断（sdk 发现/导入/目录线索），前端据此给出可操作提示
            try:
                if tmp is not None and hasattr(tmp, "probe"):
                    res["probe"] = tmp.probe()
                else:
                    # create_adapter 本身已失败（如 ABI 不兼容无运行时），
                    # 直接调用 probe_environment 获取结构化诊断
                    from xtquant_client.xtp import probe_environment
                    res["probe"] = probe_environment(cfg.client_path)
            except Exception:  # noqa: BLE001
                log.debug("test_connection 附加诊断失败: probe 不可用")
                pass
            return res
        finally:
            # C17：无论成功/失败都必须释放临时适配器（BridgeAdapter 会 spawn 子进程、
            # XTPQuantAdapter 会占用交易会话）。原实现仅在成功路径 close()，失败时
            # 反复点击「测试连接」会把残留子进程/会话越积越多 → 同账号会话被占满、
            # 后续真实连接报「session 被占用」，表现为「连接不上 QMT」。
            if tmp is not None:
                try:
                    tmp.close()
                except Exception:  # noqa: BLE001
                    pass

    def set_active(self, conn_id: str) -> None:
        """显式指定**唯一**活跃连接（用户点「设为活跃」）。

        「活跃」在 DB 里用同一列 `active` 表达「应保持连接」，故这里必须**内存与 DB
        一起改**：只写 DB 会导致 `cfg.active` 仍为 False ⇒ 健康监控不自愈；
        只改内存则下次启动不自动拉起。指定唯一活跃时，其余连接的持久意图一并熄灭。
        """
        if conn_id not in self._conns:
            raise KeyError(f"未知连接：{conn_id}")
        for cid, c in self._conns.items():
            if cid != conn_id and c.cfg.active:
                c.cfg.active = False
                self._persist(c.cfg)
        conn = self._conns[conn_id]
        if not conn.cfg.active:
            conn.cfg.active = True
            self._persist(conn.cfg)
        self._active_id = conn_id
        self._persist_active()

    # ---------------- 查询 ----------------
    def active_bridge(self) -> XTQuantBridge | None:
        if self._active_id and self._active_id in self._conns:
            return self._conns[self._active_id].bridge
        # 退而求其次：第一个已连接
        for conn in self._conns.values():
            if conn.connected:
                self._active_id = conn.cfg.conn_id
                return conn.bridge
        return None

    def active_adapter(self) -> BrokerAdapter | None:
        b = self.active_bridge()
        return b.gateway if b else None

    def bridge(self, conn_id: str | None = None) -> XTQuantBridge | None:
        if conn_id:
            conn = self._conns.get(conn_id)
            return conn.bridge if conn else None
        return self.active_bridge()

    def all_connections(self) -> list[Connection]:
        return list(self._conns.values())

    def status_list(self) -> list[dict]:
        out = []
        for cid, conn in self._conns.items():
            out.append({
                "conn_id": cid, "name": conn.cfg.name, "broker_id": conn.cfg.broker_id,
                "broker_name": conn.adapter.broker_name,
                "account_id": conn.cfg.account_id, "account_type": conn.cfg.account_type,
                "client_mode": conn.cfg.client_mode or "auto",
                "connected": conn.connected, "active": (cid == self._active_id),
                "health_status": conn.health_status,
                "reconnect_attempts": conn.reconnect_attempts,
                "last_error": conn.last_error,
                "adapter": conn.adapter.adapter_id,
                "client_version": conn.adapter.client_version,
                "supported_periods": conn.adapter.supported_periods,
                "supported_account_types": conn.adapter.supported_account_types,
                # 高频轮询路径：只取缓存画像，避免 ~47s 同步探测冻结事件循环
                "version_profile": _version_profile(conn.adapter, allow_block=False),
            })
        return out

    # ---------------- 持久化辅助 ----------------
    def _persist(self, cfg: ConnectionConfig) -> None:
        db = get_db()
        existing = db.query_one("SELECT id FROM broker_connections WHERE conn_id=?",
                                (cfg.conn_id,))
        if existing:
            db.execute(
                "UPDATE broker_connections SET name=?, broker_id=?, client_path=?, "
                "client_mode=?, account_id=?, account_type=?, session_id=?, "
                "min_version=?, active=? WHERE conn_id=?",
                (cfg.name, cfg.broker_id, cfg.client_path, cfg.client_mode or "auto",
                 cfg.account_id, cfg.account_type, cfg.session_id, cfg.min_version,
                 1 if cfg.active else 0, cfg.conn_id))
        else:
            db.insert("broker_connections", {
                "conn_id": cfg.conn_id, "name": cfg.name, "broker_id": cfg.broker_id,
                "client_path": cfg.client_path, "client_mode": cfg.client_mode or "auto",
                "account_id": cfg.account_id,
                "account_type": cfg.account_type, "session_id": cfg.session_id,
                "min_version": cfg.min_version, "active": 1 if cfg.active else 0,
                "created_at": now_iso()})

    def _persist_active(self, exclusive: bool = True) -> None:
        """把「当前活跃连接」的持久意图写进 DB（`active=1`）。

        ⚠️ **`_active_id is None` 时必须直接返回**：旧实现在这种情况下会执行
        `UPDATE broker_connections SET active=0`（**全表清零**），而 `_active_id`
        恰好在「断开连接」后被置空 —— 于是「断开」这个动作会顺手把**所有**连接的
        「启动时自动连接」意图抹掉（实测：优雅停机后第二次启动 `active=0`，
        既不自动拉起、又因「列表非空」跳过自动连接 ⇒ 永远连不上）。

        `exclusive` 区分两个调用方语义：
        - `True`（`set_active` 路由 = 用户显式指定**唯一**活跃连接）：先清零其余；
        - `False`（连接刚连上、活跃指针自动落到它）：**只标记自己**，绝不触碰其它
          连接的 active —— 那些是各自独立的「启动时自动连接」意图，被顺手清掉就是
          「下次启动少拉起一条」的静默故障。
        """
        if not self._active_id:
            return
        if self._active_id not in self._conns:
            return
        db = get_db()
        if exclusive:
            db.execute("UPDATE broker_connections SET active=0")
        db.execute("UPDATE broker_connections SET active=1 WHERE conn_id=?",
                   (self._active_id,))

    # ---------------- 自动连接复用 ----------------
    def find_by_identity(self, broker_id: str, client_path: str,
                         account_id: str) -> Connection | None:
        """按「券商 + 客户端路径 + 资金账号」定位已有连接。

        自动连接用它避免重复建连：同一个人客户端被反复探测到时，应该复用那条
        持久化连接（重新标记 active + 拉起），而不是每次新增一条
        （实测踩过：三次启动累积出 3 条重复的「迅投 XTQuant 22453951」）。
        """
        want = (broker_id or "", _norm_path(client_path), str(account_id or ""))
        for conn in self._conns.values():
            c = conn.cfg
            if (c.broker_id or "", _norm_path(c.client_path),
                    str(c.account_id or "")) == want:
                return conn
        return None

    def activate(self, conn_id: str) -> Connection:
        """把**已有**连接重新标记为「应保持连接」并启动（自动连接复用路径）。

        与 `add_connection` 的区别：不新建连接、不重复落库，只是把持久意图
        重新点亮并拉起。供「用户手动断开过、下次启动仍应自动接上」这一路径使用。
        """
        conn = self._conns.get(conn_id)
        if conn is None:
            raise KeyError(f"未知连接：{conn_id}")
        conn.cfg.active = True
        self._persist(conn.cfg)
        self._safe_start(conn_id)
        if conn.connected and self._active_id is None:
            self._active_id = conn_id
            self._persist_active(exclusive=False)
        return conn

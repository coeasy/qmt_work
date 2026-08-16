"""券商连接管理器（多券商 / 多账户 / 多客户端版本同时存在）。

- 每个连接 = 配置 + 适配器实例 + 专属线程桥（行情订阅按连接隔离）
- 连接配置持久化到 `broker_connections` 表；启动时可自动重连
- 对外暴露「当前活跃连接」与「指定连接」的 bridge，供 routes/tools/mcp/sync 使用
- 未连接任何券商时，`active_bridge()` 返回 None，上层返回明确的 503（不返回假数据）
"""
import threading
import time
import uuid
from dataclasses import dataclass, field

from app.db import get_db
from .base import BrokerAdapter
from .gateway import XTQuantBridge
from .registry import create_adapter, get_profile


@dataclass
class ConnectionConfig:
    conn_id: str = ""
    name: str = ""
    broker_id: str = ""
    client_path: str = ""
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

    # ---------------- 持久化加载 ----------------
    def load_persisted(self) -> None:
        db = get_db()
        rows = db.query("SELECT * FROM broker_connections ORDER BY id")
        for r in rows:
            cfg = ConnectionConfig(
                conn_id=r["conn_id"], name=r["name"], broker_id=r["broker_id"],
                client_path=r["client_path"], account_id=r["account_id"],
                account_type=r["account_type"], session_id=int(r["session_id"] or 0),
                min_version=r["min_version"] or "", active=bool(r["active"]))
            self._build(cfg, connect=False)
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
                                 cfg.account_type, cfg.session_id, cfg.min_version)
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
            return
        try:
            conn.adapter.start()  # 幂等：子进程已在运行则复用
            conn.connected = conn.adapter.is_connected()
            if conn.connected and self._active_id is None:
                self._active_id = conn_id
        except Exception:  # noqa: BLE001
            conn.connected = False

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
                self._persist_active()
        return conn

    def connect(self, conn_id: str) -> dict:
        conn = self._conns.get(conn_id)
        if not conn:
            raise KeyError(f"未知连接：{conn_id}")
        conn.adapter.start()
        conn.connected = conn.adapter.is_connected()
        if conn.connected and self._active_id is None:
            self._active_id = conn_id
            self._persist_active()
        return conn.adapter.test_connection()

    def disconnect(self, conn_id: str) -> None:
        conn = self._conns.get(conn_id)
        if not conn:
            return
        conn.adapter.close()
        conn.connected = False
        if self._active_id == conn_id:
            self._active_id = None
            self._persist_active()

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
                return conn.adapter.test_connection()
        tmp = None
        try:
            # create_adapter 在 ABI 不兼容且无兼容运行时时会抛 BrokerSDKError；
            # 必须放在 try 内，否则会穿透为 500（此前 3.13 上「No module named ...」 的旧路径）
            tmp = create_adapter(cfg.broker_id, cfg.client_path, cfg.account_id,
                                 cfg.account_type, cfg.session_id, cfg.min_version)
            tmp.start()
            res = tmp.test_connection()
            tmp.close()
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
                pass
            return res

    def set_active(self, conn_id: str) -> None:
        if conn_id not in self._conns:
            raise KeyError(f"未知连接：{conn_id}")
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
                "connected": conn.connected, "active": (cid == self._active_id),
                "health_status": conn.health_status,
                "reconnect_attempts": conn.reconnect_attempts,
                "last_error": conn.last_error,
                "adapter": conn.adapter.adapter_id,
                "client_version": conn.adapter.client_version,
                "supported_periods": conn.adapter.supported_periods,
                "supported_account_types": conn.adapter.supported_account_types,
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
                "account_id=?, account_type=?, session_id=?, min_version=?, active=? WHERE conn_id=?",
                (cfg.name, cfg.broker_id, cfg.client_path, cfg.account_id,
                 cfg.account_type, cfg.session_id, cfg.min_version,
                 1 if cfg.active else 0, cfg.conn_id))
        else:
            db.insert("broker_connections", {
                "conn_id": cfg.conn_id, "name": cfg.name, "broker_id": cfg.broker_id,
                "client_path": cfg.client_path, "account_id": cfg.account_id,
                "account_type": cfg.account_type, "session_id": cfg.session_id,
                "min_version": cfg.min_version, "active": 1 if cfg.active else 0,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S")})

    def _persist_active(self) -> None:
        db = get_db()
        db.execute("UPDATE broker_connections SET active=0")
        if self._active_id:
            conn = self._conns.get(self._active_id)
            if conn:
                db.execute("UPDATE broker_connections SET active=1 WHERE conn_id=?",
                           (self._active_id,))

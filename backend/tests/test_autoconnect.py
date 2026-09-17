"""启动自动连接（autoconnect）契约测试。

覆盖两个消费方共用的决策逻辑：

- ``detect_candidates``：候选丰富化（与 ``GET /brokers/auto-detect`` 同源）
- ``pick_active``：挑「当前活跃客户端」
- ``build_connection``：组装连接字段
- ``phase_broker._auto_connect_active``：bootstrap 侧的非阻断行为

★ 两条核心不变量（错了会出真事）：

1. **未运行的客户端绝不自动连** —— 代拉进程会弹登录窗，且无人值守时会卡住启动路径；
2. **没有资金账号就返回 None，绝不猜一个账号** —— 猜错账号意味着把订单下到别人户上。
"""
from __future__ import annotations

import asyncio

import pytest

from xtquant_client import autoconnect
from xtquant_client.autoconnect import (
    build_connection,
    detect_candidates,
    pick_active,
    resolve_broker_id,
)


def _cand(**kw) -> dict:
    base = {
        "root": r"P:\stock\gd_qmt",
        "name": "gd_qmt",
        "broker_id": "generic",
        "running": False,
        "client_path": r"P:\stock\gd_qmt\userdata_mini",
        "client_mode": "mini",
        "accounts": [],
        "default_account_id": "",
    }
    base.update(kw)
    return base


def _acc(acc_id: str = "22453951", acc_type: str = "STOCK",
         broker_name: str = "光大证券") -> dict:
    return {"account_id": acc_id, "account_type": acc_type, "broker_name": broker_name}


# ---------------- pick_active ----------------

def test_pick_active_prefers_running_with_account():
    """排序：运行中 + 有账号 > 仅运行中 > 仅有账号。"""
    off_with_acc = _cand(root="A", running=False, default_account_id="1")
    on_no_acc = _cand(root="B", running=True)
    on_with_acc = _cand(root="C", running=True, default_account_id="3")
    assert pick_active([off_with_acc, on_no_acc, on_with_acc])["root"] == "C"


def test_pick_active_running_beats_account_only():
    """运行中但没账号，仍优先于「没运行但有账号」（前者只差一次账号发现）。"""
    on_no_acc = _cand(root="B", running=True)
    off_with_acc = _cand(root="A", running=False, default_account_id="1")
    assert pick_active([off_with_acc, on_no_acc])["root"] == "B"


def test_pick_active_returns_none_when_nothing_running():
    """★不变量 1：全部候选都没在运行 → 不自动连。"""
    assert pick_active([_cand(root="A"), _cand(root="B")]) is None


def test_pick_active_none_and_empty_and_garbage():
    assert pick_active(None) is None
    assert pick_active([]) is None
    assert pick_active([{"x": 1}, "not-a-dict"]) is None


def test_pick_active_is_stable_on_ties():
    """同分保持 discover() 原序 —— 界面列表第一项 = 实际自动连的那个。"""
    a = _cand(root="A", running=True, default_account_id="1")
    b = _cand(root="B", running=True, default_account_id="2")
    assert pick_active([a, b])["root"] == "A"
    assert pick_active([b, a])["root"] == "B"


# ---------------- build_connection ----------------

def test_build_connection_refuses_without_account():
    """★不变量 2：没有资金账号 → None（不猜账号）。"""
    assert build_connection(_cand(running=True)) is None


def test_build_connection_refuses_without_path():
    c = _cand(running=True, client_path="", root="", default_account_id="1")
    assert build_connection(c) is None


def test_build_connection_fields_and_account_type():
    c = _cand(running=True, accounts=[_acc("999", "CREDIT")], default_account_id="999")
    out = build_connection(c)
    assert out is not None
    assert out["account_id"] == "999"
    assert out["account_type"] == "CREDIT"
    assert out["active"] is True
    assert out["conn_id"] == ""          # 交给 manager 生成，避免与既有连接 id 冲突
    assert "自动连接" in out["name"]


def test_build_connection_falls_back_to_first_account():
    c = _cand(running=True, accounts=[_acc("111")], default_account_id="")
    out = build_connection(c)
    assert out is not None and out["account_id"] == "111"


# ---------------- resolve_broker_id ----------------

def test_resolve_broker_id_priority():
    # Config.xml 真实券商名优先于路径猜测
    assert resolve_broker_id(_cand(broker_name="广发证券", broker_id="generic")) == "gf"
    # 识别不到真实券商名时用路径猜测
    assert resolve_broker_id(_cand(broker_name="", broker_id="yinhe")) == "yinhe"
    # 两者都没有 → generic 兜底
    assert resolve_broker_id(_cand(broker_name="", broker_id="")) == "generic"


# ---------------- detect_candidates ----------------

def test_detect_candidates_isolates_single_failure(monkeypatch):
    """单个候选丰富化失败只降级该候选，不影响其它候选。"""
    def boom(path: str):
        if path == "BAD":
            raise OSError("boom")
        return []

    monkeypatch.setattr(autoconnect, "discover",
                        lambda: [{"root": "BAD"}, _cand(root="GOOD", broker_id="yinhe")])
    monkeypatch.setattr(autoconnect, "discover_accounts", boom)
    out = detect_candidates()
    assert len(out) == 2
    assert out[0]["accounts"] == [] and out[0]["default_account_id"] == ""
    assert out[0]["broker_id"] == "generic"      # 降级兜底，不炸整体
    assert out[1]["broker_id"] == "yinhe"        # 第二个候选不受影响


def test_detect_candidates_rewrites_generic_to_mini(monkeypatch):
    """generic（国信/光大等）+ 有 userdata_mini → 建议 mini 并改写 client_path。"""
    c = _cand(root="R", broker_id="generic", broker_name="国信证券",
              has_userdata_mini=True, client_mode="full",
              client_path=r"R\userdata", client_path_mini=r"R\userdata_mini")
    monkeypatch.setattr(autoconnect, "discover", lambda: [c])
    monkeypatch.setattr(autoconnect, "discover_accounts", lambda p: [])
    out = detect_candidates()[0]
    assert out["broker_id"] == "generic"
    assert out["client_mode"] == "mini"
    assert out["client_path"].endswith("userdata_mini")


# ---------------- bootstrap：_auto_connect_active ----------------

def test_auto_connect_skips_when_no_running_client(monkeypatch):
    from app.bootstrap import phase_broker

    monkeypatch.setattr(autoconnect, "detect_candidates", lambda: [_cand(running=False)])
    assert asyncio.run(phase_broker._auto_connect_active()) == ""


def test_auto_connect_skips_on_detect_failure(monkeypatch):
    """★非阻断：探测抛异常时只记日志，绝不向上抛（启动流程优先）。"""
    from app.bootstrap import phase_broker

    def boom():
        raise RuntimeError("scan exploded")

    monkeypatch.setattr(autoconnect, "detect_candidates", boom)
    assert asyncio.run(phase_broker._auto_connect_active()) == ""


def test_auto_connect_skips_when_account_missing(monkeypatch):
    from app.bootstrap import phase_broker

    monkeypatch.setattr(autoconnect, "detect_candidates",
                        lambda: [_cand(running=True, accounts=[], default_account_id="")])
    assert asyncio.run(phase_broker._auto_connect_active()) == ""


def test_auto_connect_skips_on_add_connection_failure(monkeypatch):
    """★非阻断：建连失败（如客户端拒绝接入）也只记日志。"""
    from app.bootstrap import phase_broker
    from core.state import init_broker_manager, state

    init_broker_manager()

    def boom(cfg, autoconnect_flag):  # noqa: ARG001
        raise RuntimeError("illegal pid")

    monkeypatch.setattr(state.broker_manager, "add_connection", boom)
    monkeypatch.setattr(autoconnect, "detect_candidates",
                        lambda: [_cand(running=True, accounts=[_acc()],
                                       default_account_id="22453951")])
    assert asyncio.run(phase_broker._auto_connect_active()) == ""


def test_auto_connect_creates_connection_when_client_running(monkeypatch):
    """运行中 + 有账号 → 调 add_connection(autoconnect=True) 并返回 conn_id。"""
    from app.bootstrap import phase_broker
    from core.state import init_broker_manager, state

    init_broker_manager()
    seen: list = []

    class _FakeAdapter:
        def is_connected(self) -> bool:
            return True

    class _FakeConn:
        def __init__(self, cfg):
            self.cfg = cfg
            self.connected = False
            self.adapter = _FakeAdapter()

    monkeypatch.setattr(state.broker_manager, "add_connection",
                        lambda cfg, ac: seen.append((cfg, ac)) or _FakeConn(cfg))
    monkeypatch.setattr(autoconnect, "detect_candidates",
                        lambda: [_cand(running=True, accounts=[_acc()],
                                       default_account_id="22453951")])

    conn_id = asyncio.run(phase_broker._auto_connect_active())
    assert len(seen) == 1
    cfg, auto_flag = seen[0]
    assert auto_flag is True                     # 建连（不是只登记）
    assert cfg.account_id == "22453951"
    assert cfg.active is True
    assert conn_id == cfg.conn_id                # 返回值即新连接 id


def test_broker_auto_connect_defaults_on():
    """★产品契约：启动自动连接**默认开启**（「启动默认自动连接当前活跃客户端」）。

    读 model_fields 而非 settings 实例 —— 测试环境的 conftest 会把实例改成 False，
    这里断言的是「出厂默认值」，两者是不同的东西。
    """
    from core.config import Settings

    assert Settings.model_fields["broker_auto_connect"].default is True


# ---------------- manager：持久意图 vs 运行时状态 ----------------
#
# 本轮真根因：`active` 这一个字段被两种语义共用 ——
#   ① 「这条连接应当保持连接」（持久意图：决定下次启动是否自动拉起 / 健康监控是否自愈）
#   ② 「当前活跃连接是谁」（运行时指针：`_active_id`）
# 优雅停机走 `disconnect_all()` → `disconnect()` 把 ① 当成 ② 一起清掉，
# 于是**从第二次启动开始永远连不上**。下面几条把两者的边界钉死。


class _StubAdapter:
    adapter_id = "stub"
    broker_name = "stub"
    client_version = ""
    supported_periods: list = []
    supported_account_types: list = []

    def start(self) -> None:
        pass

    def is_connected(self) -> bool:
        return False

    def close(self) -> None:
        pass


class _StubBridge:
    gateway = None


@pytest.fixture
def mgr(tmp_path):
    """独立临时 DB 的 BrokerManager（用完把全局 DB 还原，避免污染其它用例）。"""
    from core import db as db_mod
    from xtquant_client.manager import BrokerManager

    prev = db_mod._db
    db_mod.init_db(tmp_path / "mgr.db")
    try:
        yield BrokerManager()
    finally:
        db_mod._db = prev


def _inject(m, conn_id: str, *, active: bool = True,
            client_path: str = r"P:\stock\gd_qmt\userdata_mini",
            account_id: str = "22453951", broker_id: str = "generic"):
    """往 manager 里塞一条连接并落库（绕过 create_adapter，不碰真实 SDK）。"""
    from xtquant_client.manager import Connection, ConnectionConfig

    cfg = ConnectionConfig(conn_id=conn_id, name=conn_id, broker_id=broker_id,
                           client_path=client_path, account_id=account_id, active=active)
    conn = Connection(cfg=cfg, adapter=_StubAdapter(), bridge=_StubBridge())
    m._conns[conn_id] = conn
    m._persist(cfg)
    return conn


def _row(conn_id: str):
    from core.db import get_db
    return get_db().query_one("SELECT * FROM broker_connections WHERE conn_id=?", (conn_id,))


def test_disconnect_all_keeps_startup_intent(mgr):
    """★不变量：优雅停机只断运行时连接，**必须保留**「启动时自动连接」的持久意图。

    回归背景（本轮真根因）：停机 → `disconnect()` 把 `cfg.active` 清成 False 并落库
    `active=0`；下次启动该连接既不自动拉起（`phase_broker` 只看 `cfg.active`）、
    又因「连接列表非空」跳过自动连接 ⇒ **第二次启动开始永远连不上**。
    实测现象：`/brokers` 显示「已有连接 (1)」但「未连接」，行情通道空转、自选股恒 `--`。
    """
    m = mgr
    _inject(m, "c1", active=True)
    m._active_id = "c1"

    assert m.disconnect_all() == 1
    assert m._conns["c1"].connected is False
    assert _row("c1")["active"] == 1, "停机抹掉了启动自动连接的意图 → 下次启动不会再连"


def test_disconnect_clears_intent_for_manual_disconnect(mgr):
    """用户点「断开」= 意图变更，仍要清 active（守住 C16：不清则 5s 内被自动重连回来）。"""
    m = mgr
    _inject(m, "c1", active=True)
    m._active_id = "c1"

    m.disconnect("c1")
    assert m._conns["c1"].cfg.active is False
    assert _row("c1")["active"] == 0


def test_persist_active_does_not_wipe_other_intents(mgr):
    """★不变量：`_active_id` 为空时 `_persist_active()` 必须什么都不做。

    旧实现无条件执行 `UPDATE broker_connections SET active=0`（**全表清零**），
    而 `_active_id` 恰好在「断开连接」后被置空 —— 于是断开一条连接会顺手把**所有**
    连接的启动意图抹掉（多券商场景下静默少拉起若干条）。
    """
    m = mgr
    _inject(m, "a", active=True)
    _inject(m, "b", active=True)
    m._active_id = None

    m._persist_active()
    assert _row("a")["active"] == 1 and _row("b")["active"] == 1


def test_find_by_identity_normalizes_path(mgr):
    """按「券商 + 路径 + 账号」定位：路径大小写/斜杠/尾斜杠差异不得判为不同客户端。"""
    m = mgr
    _inject(m, "c1", client_path=r"P:\stock\gd_qmt\userdata_mini")

    assert m.find_by_identity("generic", "p:/stock/gd_qmt/userdata_mini/", "22453951")
    assert m.find_by_identity("generic", r"P:\stock\gd_qmt\userdata", "22453951") is None
    assert m.find_by_identity("generic", r"P:\stock\gd_qmt\userdata_mini", "999") is None


def test_activate_relights_intent_and_persists(mgr):
    """复用路径：`activate()` 把既有连接的持久意图重新点亮并落库。"""
    m = mgr
    _inject(m, "c1", active=False)

    m.activate("c1")
    assert m._conns["c1"].cfg.active is True
    assert _row("c1")["active"] == 1


def test_auto_connect_reuses_existing_connection(mgr, monkeypatch):
    """★不变量：同一客户端已有连接时**复用**，不得新建。

    否则「用户手动断开过 → 下次启动又新建一条」会让连接列表随启动次数膨胀
    （实测踩过：三次启动累积出 3 条重复的「迅投 XTQuant 22453951」）。
    """
    from app.bootstrap import phase_broker
    from core.state import state

    m = mgr
    _inject(m, "old", active=False)          # 用户手动断开过
    seen: list = []
    monkeypatch.setattr(m, "activate", lambda cid: seen.append(cid) or m._conns[cid])
    monkeypatch.setattr(m, "add_connection", lambda cfg, ac: seen.append("ADD"))
    monkeypatch.setattr(state, "broker_manager", m)
    monkeypatch.setattr(autoconnect, "detect_candidates",
                        lambda: [_cand(running=True, accounts=[_acc()],
                                       default_account_id="22453951")])

    assert asyncio.run(phase_broker._auto_connect_active()) == "old"
    assert seen == ["old"], "应复用既有连接，不得新建"

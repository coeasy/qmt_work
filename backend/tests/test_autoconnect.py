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
import threading
import types

import pytest

from xtquant_client import autoconnect
from xtquant_client.autoconnect import (
    build_connection,
    detect_candidates,
    pick_active,
    resolve_broker_id,
)

#: 真实实现的引用。下面的 autouse fixture 会把模块属性换成空桩（防单元测试误触
#: 真实机器的桥），需要走真实逻辑的用例用它在自己的 monkeypatch 里还原。
_REAL_DETECT_BRIDGE_AGENTS = autoconnect.detect_bridge_agents


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
#
# ★ 2026-10-14：`_auto_connect_active` 现在会**再探一次路径 B（大 QMT 文件桥）**
#   （见 phase_broker._auto_connect_bridge）。所以这一组测试必须把桥探测也隔离掉 ——
#   否则 `detect_bridge_agents` 会去扫**真实机器**的 `<QMT>/python/agent_config.json`，
#   本机恰好部署着桥 ⇒ 单元测试会真的建出一条连接（污染 + 假通过）。
#   用下面的 autouse fixture 统一关掉：默认「这台机器没部署桥」，
#   需要测桥的用例再自行 patch。

@pytest.fixture(autouse=True)
def _no_real_bridge_probe(monkeypatch):
    monkeypatch.setattr(autoconnect, "detect_bridge_agents", lambda *a, **k: [])


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


# ---------------- 客户端根归一 / 复用既有连接（防重复建连 + 自愈） ----------------
# 背景真缺陷：持久化连接写客户端根（P:\stock\gd_qmt），自动探测给出数据目录
# （P:\stock\gd_qmt\userdata_mini）。精确字符串比对判为两个客户端 ⇒
# ① 每次守护都新建一条（同账号堆多份）；②「已存在」又把自动连接挡在外面，
# 表现为「客户端明明开着却永远连不上」。

def test_client_root_strips_data_dir_tails():
    """根写法与数据目录写法必须归一到同一个客户端根。"""
    from xtquant_client.manager import _client_root
    assert _client_root(r"P:\stock\gd_qmt\userdata_mini") == _client_root(r"P:\stock\gd_qmt")
    assert _client_root(r"P:\stock\gd_qmt\userdata") == _client_root(r"P:\stock\gd_qmt")
    assert _client_root(r"P:\stock\gd_qmt\bin.x64") == _client_root(r"P:\stock\gd_qmt")
    # 不同客户端不能归一到一处
    assert _client_root(r"P:\stock\gd_qmt") != _client_root(r"P:\stock\zj_QMT")


def _mk_manager_with_conn(client_path: str, account_id: str = "22453951",
                          broker_id: str = "generic", active: bool = True):
    """造一个只含内存连接的 BrokerManager（不落库、不建真实适配器）。"""
    from xtquant_client.manager import BrokerManager, Connection, ConnectionConfig
    mgr = BrokerManager()
    cfg = ConnectionConfig(conn_id="c1", name="x", broker_id=broker_id,
                           client_path=client_path, account_id=account_id, active=active)
    mgr._conns["c1"] = Connection(cfg=cfg, adapter=None, bridge=None)
    return mgr


def test_find_by_identity_matches_root_when_path_form_differs():
    """★回归：根写法 vs userdata_mini 写法 + 同账号 → 必须判定为同一客户端。"""
    mgr = _mk_manager_with_conn(r"P:\stock\gd_qmt")
    hit = mgr.find_by_identity("generic", r"P:\stock\gd_qmt\userdata_mini", "22453951")
    assert hit is not None and hit.cfg.conn_id == "c1"


def test_find_by_identity_tolerates_broker_id_drift():
    """券商档案漂移（手填 guojin / 探测 generic）不应导致重复建连。"""
    mgr = _mk_manager_with_conn(r"P:\stock\gd_qmt", broker_id="guojin")
    hit = mgr.find_by_identity("generic", r"P:\stock\gd_qmt\userdata_mini", "22453951")
    assert hit is not None and hit.cfg.conn_id == "c1"


def test_find_by_identity_still_distinguishes_other_accounts():
    """同客户端不同账号 → 不是同一条连接（多账户必须各自一条）。"""
    mgr = _mk_manager_with_conn(r"P:\stock\gd_qmt")
    assert mgr.find_by_identity("generic", r"P:\stock\gd_qmt\userdata_mini", "99999999") is None


def test_repair_client_path_rebuilds_adapter_and_persists(monkeypatch):
    """★自愈：把持久连接的路径修正为探测到的数据目录，且必须重建适配器。

    适配器在 _build 时就绑定了 client_path，只改 cfg 不生效 ⇒ 必须连同
    adapter / bridge 一起重建，否则「修了等于没修」。
    """
    from xtquant_client import manager as mgr_mod

    class _A:
        def __init__(self):
            self.closed = False
        def close(self):
            self.closed = True
        def is_connected(self):
            return False

    mgr = _mk_manager_with_conn(r"P:\stock\gd_qmt")
    conn = mgr._conns["c1"]
    conn.adapter = _A()

    persisted = []
    monkeypatch.setattr(mgr, "_persist", lambda cfg: persisted.append(cfg.client_path))
    monkeypatch.setattr(mgr_mod, "create_adapter", lambda *a, **kw: "NEW_ADAPTER")
    monkeypatch.setattr(mgr_mod, "XTQuantBridge", lambda a: "NEW_BRIDGE")

    out = mgr.repair_client_path("c1", r"P:\stock\gd_qmt\userdata_mini", "mini")
    assert out is conn
    assert conn.cfg.client_path == r"P:\stock\gd_qmt\userdata_mini"
    assert conn.cfg.client_mode == "mini"
    assert conn.adapter == "NEW_ADAPTER"      # 重建，不是沿用旧的
    assert conn.bridge == "NEW_BRIDGE"
    assert conn.connected is False
    assert persisted == [r"P:\stock\gd_qmt\userdata_mini"]   # 修正已落库


def test_repair_client_path_is_noop_when_already_correct():
    """路径已经一致 → 不动（避免无谓重建与断连）。"""
    mgr = _mk_manager_with_conn(r"P:\stock\gd_qmt\userdata_mini")
    conn = mgr._conns["c1"]
    assert mgr.repair_client_path("c1", r"P:\stock\gd_qmt\userdata_mini") is conn


# ---------------- 守护触发判据（自愈 vs 尊重手动断开） ----------------

def _fake_conn(connected: bool = False, active: bool = True):
    class _Cfg:
        def __init__(self, a):
            self.active = a
    class _C:
        def __init__(self, c, a):
            self.connected = c
            self.cfg = _Cfg(a)
    return _C(connected, active)


def test_should_auto_connect_decision_table():
    """★判据：以「有没有真正连上」为准，不是「列表是否为空」。

    这条错了就会出现真故障：存在一条坏连接 ⇒ 守护永不介入 ⇒
    「先开软件、后开 QMT 客户端」永远连不上。
    """
    from app.bootstrap.phase_watchdogs import _should_auto_connect

    assert _should_auto_connect([]) is True                       # 全新安装
    assert _should_auto_connect([_fake_conn(connected=True)]) is False   # 已连上
    # ★核心回归：有持久意图但没连上 → 必须自愈（旧判据「列表非空」会漏掉这个）
    assert _should_auto_connect([_fake_conn(False, True)]) is True
    assert _should_auto_connect([_fake_conn(False, True),
                                 _fake_conn(False, False)]) is True
    # 用户手动断开过（非空且全部 inactive）→ 尊重意图，不介入
    assert _should_auto_connect([_fake_conn(False, False)]) is False
    assert _should_auto_connect([_fake_conn(False, False),
                                 _fake_conn(False, False)]) is False


def test_status_list_marks_invalid_client_path(tmp_path):
    """界面要能识别「指向不存在目录」的残留连接（永远连不上，必须可一键清理）。"""
    from xtquant_client.manager import BrokerManager, Connection, ConnectionConfig

    class _Adapter:
        broker_name = "测试券商"
        adapter_id = "xtp"
        client_version = ""
        supported_periods = ["1d"]
        supported_account_types = ["STOCK"]

    mgr = BrokerManager()
    good = tmp_path / "real_client"
    good.mkdir()
    mgr._conns["ok"] = Connection(
        cfg=ConnectionConfig(conn_id="ok", name="有效", broker_id="generic",
                             client_path=str(good), account_id="1"),
        adapter=_Adapter(), bridge=None)
    mgr._conns["bad"] = Connection(
        cfg=ConnectionConfig(conn_id="bad", name="无效", broker_id="generic",
                             client_path=str(tmp_path / "no_such_dir"), account_id="2"),
        adapter=_Adapter(), bridge=None)

    out = {r["conn_id"]: r for r in mgr.status_list()}
    assert out["ok"]["path_exists"] is True
    assert out["bad"]["path_exists"] is False
    assert out["ok"]["client_path"] == str(good)   # 界面要展示路径，不能是缺失字段


def test_status_list_path_exists_false_when_empty():
    """未填 client_path → 判定为不存在（不能因缺字段而误判有效）。"""
    from xtquant_client.manager import BrokerManager, Connection, ConnectionConfig

    class _Adapter:
        broker_name = "x"
        adapter_id = "xtp"
        client_version = ""
        supported_periods = []
        supported_account_types = []

    mgr = BrokerManager()
    mgr._conns["e"] = Connection(
        cfg=ConnectionConfig(conn_id="e", name="空", broker_id="generic",
                             client_path="", account_id="3"),
        adapter=_Adapter(), bridge=None)
    assert mgr.status_list()[0]["path_exists"] is False


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

    # ★ 复用的事实由 `seen` 断言（只调了 activate、没调 add_connection）。
    #   返回值必须是 ""：桩适配器 is_connected() 恒 False ⇒ **没连通**。
    #   旧实现返回 "old"（把「建连」当「连通」）⇒ 守护据此打「已接入」并刷活跃
    #   指针 —— 假绿灯（2026-10-15 真机取证，见 phase_broker._auto_connect_active）。
    assert asyncio.run(phase_broker._auto_connect_active()) == ""
    assert seen == ["old"], "应复用既有连接，不得新建"


# ---------------- 路径 B：大 QMT 文件桥的自动发现（2026-10-14） ----------------
#
# 用户实测反馈：「客户端没有自动连接大 qmt」。
# 根因：`detect_candidates` 只找**路径 A**（外部 xtquant 直连）的候选；而本机券商把
# `mdl_auth_gt_ipc_pair` 关成 0 ⇒ `XtQuantServer` 起不来（客户端日志
# `CIPCManager::init, not auth:mdl_auth_gt_ipc_pair` + `The XtQuantServer is not
# allowed to start.`）⇒ 路径 A **永远不可能成功**。唯一可用的交易通道是跑在大 QMT
# 内置 Python 策略里的 agent + 文件桥，但自动连接从不认识它（bridge_dir / token
# 只有部署者知道）。这组测试锁住新的发现口径。

def _bridge_cand(root):
    return {"root": str(root), "name": root.name, "running": True}


def _write_agent(tmp_path, root, *, bridge_dir, token="tok-1", account_id="22453951",
                 status=True, alive=True, ts_ms=None, legacy=False):
    """搭一份「已部署的桥」现场：<root>/python/agent_config.json + <桥>/agent_status.json。"""
    import json
    import time as _t
    sdir = root / "python" / ("agent_bigqmt" if legacy else "")
    sdir.mkdir(parents=True, exist_ok=True)
    cfg = {"bridge_dir": str(bridge_dir), "transport": "file", "auth_token": token,
           "trading_enabled": False, "poll_interval_ms": 500}
    if account_id:
        cfg["account_id"] = account_id
    (sdir / "agent_config.json").write_text(json.dumps(cfg), encoding="utf-8")
    if status:
        bridge_dir.mkdir(parents=True, exist_ok=True)
        (bridge_dir / "agent_status.json").write_text(json.dumps({
            "ts": ts_ms if ts_ms is not None else int(_t.time() * 1000),
            "alive": alive, "agent_ver": "1.2.0", "runtime_mode": "qmt_formula",
            "injected": ["passorder"],
        }), encoding="utf-8")
    return cfg


def test_detect_bridge_agents_finds_deployed_and_running_bridge(tmp_path, monkeypatch):
    """已部署 + 心跳新鲜 ⇒ 发现；bridge_dir / token / 账号逐字段带出来（不猜）。"""
    root = tmp_path / "gd_qmt"
    br = tmp_path / "bigqmt_bridge"
    _write_agent(tmp_path, root, bridge_dir=br, token="tok-abc", account_id="22453951")

    monkeypatch.setattr(autoconnect, "detect_bridge_agents",
                        _REAL_DETECT_BRIDGE_AGENTS)
    found = autoconnect.detect_bridge_agents([_bridge_cand(root)])
    assert len(found) == 1, found
    f = found[0]
    assert f["bridge_dir"] == str(br)
    assert f["auth_token"] == "tok-abc"
    assert f["account_id"] == "22453951"
    assert f["runtime_mode"] == "qmt_formula"
    assert f["heartbeat_age_s"] is not None and f["heartbeat_age_s"] < 60

    fields = autoconnect.build_bridge_connection(f)
    assert fields["connector_key"] == "qmt.big.bridge.file"
    assert fields["bridge_dir"] == str(br)
    assert fields["auth_token"] == "tok-abc"
    assert fields["active"] is True
    assert fields["client_path"] == "", "桥没有 client_path，不得伪造一个"


def test_detect_bridge_agents_supports_legacy_config_location(tmp_path, monkeypatch):
    """旧布局 <root>/python/agent_bigqmt/agent_config.json 也要认。"""
    root = tmp_path / "gd_qmt"
    br = tmp_path / "bridge"
    _write_agent(tmp_path, root, bridge_dir=br, legacy=True)
    monkeypatch.setattr(autoconnect, "detect_bridge_agents",
                        _REAL_DETECT_BRIDGE_AGENTS)
    assert len(autoconnect.detect_bridge_agents([_bridge_cand(root)])) == 1


def test_detect_bridge_agents_rejects_stale_heartbeat(tmp_path):
    """★ 心跳过期 ⇒ **不**自动接入（残留的过期心跳是「假存活」的主要来源）。

    与 `pick_active` 拒绝自动连「没在跑的客户端」同一纪律。
    """
    import time as _t
    root = tmp_path / "gd_qmt"
    br = tmp_path / "bridge"
    stale = int((_t.time() - autoconnect.BRIDGE_HEARTBEAT_MAX_AGE_S - 600) * 1000)
    _write_agent(tmp_path, root, bridge_dir=br, ts_ms=stale)
    assert autoconnect.detect_bridge_agents([_bridge_cand(root)]) == []


def test_detect_bridge_agents_rejects_not_alive_or_missing_status(tmp_path):
    """`alive=false` 或缺 agent_status.json ⇒ 不接入。"""
    root = tmp_path / "gd_qmt"
    br = tmp_path / "bridge"
    _write_agent(tmp_path, root, bridge_dir=br, alive=False)
    assert autoconnect.detect_bridge_agents([_bridge_cand(root)]) == []

    root2 = tmp_path / "gd_qmt2"
    br2 = tmp_path / "bridge2"
    _write_agent(tmp_path, root2, bridge_dir=br2, status=False)
    assert autoconnect.detect_bridge_agents([_bridge_cand(root2)]) == []


def test_detect_bridge_agents_rejects_missing_bridge_dir(tmp_path):
    """配置里写了 bridge_dir 但目录不存在 ⇒ 不接入（连上也无处投递请求）。"""
    root = tmp_path / "gd_qmt"
    br = tmp_path / "nope"
    _write_agent(tmp_path, root, bridge_dir=br, status=False)
    assert autoconnect.detect_bridge_agents([_bridge_cand(root)]) == []


def test_detect_bridge_agents_dedups_same_bridge_dir(tmp_path, monkeypatch):
    """两个 QMT 根指向同一个 bridge_dir ⇒ 只报一条（否则每次启动都会多一条连接）。"""
    r1, r2 = tmp_path / "qmt1", tmp_path / "qmt2"
    br = tmp_path / "shared"
    _write_agent(tmp_path, r1, bridge_dir=br)
    _write_agent(tmp_path, r2, bridge_dir=br)
    monkeypatch.setattr(autoconnect, "detect_bridge_agents",
                        _REAL_DETECT_BRIDGE_AGENTS)
    found = autoconnect.detect_bridge_agents([_bridge_cand(r1), _bridge_cand(r2)])
    assert len(found) == 1, found


def test_detect_bridge_agents_tolerates_broken_json(tmp_path):
    """配置文件不是合法 JSON ⇒ 跳过该候选，绝不抛（探测是尽力而为）。"""
    root = tmp_path / "gd_qmt"
    (root / "python").mkdir(parents=True)
    (root / "python" / "agent_config.json").write_text("{not json", encoding="utf-8")
    assert autoconnect.detect_bridge_agents([_bridge_cand(root)]) == []


def test_build_bridge_connection_refuses_without_bridge_dir():
    assert autoconnect.build_bridge_connection({"bridge_dir": ""}) is None
    assert autoconnect.build_bridge_connection({}) is None


def test_auto_connect_falls_back_to_bridge_when_path_a_not_connected(tmp_path, monkeypatch):
    """★ 核心修复：路径 A **建出了连接但没连上** ⇒ 必须继续走路径 B。

    旧实现只看 `_auto_connect_qmt_client()` 有没有返回 conn_id，路径 A 一旦建出
    （连不上的）连接就收工 ⇒ 桥永远不会被自动接上。本机实测正是这种情形：
    路径 A 建出「集中交易001（自动连接）」后 connected=false，
    last_error=交易连接异常：WaitingFreeWriter instances exceed maximum limit。
    """
    from app.bootstrap import phase_broker
    from core.state import init_broker_manager, state

    init_broker_manager()
    root = tmp_path / "gd_qmt"
    br = tmp_path / "bridge"
    _write_agent(tmp_path, root, bridge_dir=br)

    class _DeadAdapter:
        def is_connected(self):
            return False

    class _FakeConn:
        def __init__(self, cfg):
            self.cfg = cfg
            self.connected = False
            self.adapter = _DeadAdapter()

    built: list = []

    def _fake_add(cfg, ac):          # noqa: ARG001
        # ★ 必须像真实 `_build` 那样补 conn_id：真实现里 conn_id 为空才生成 uuid。
        #   假实现若不补，cfg.conn_id 恒为 "" ⇒ 断言 "" == "" 会**空洞通过**。
        if not cfg.conn_id:
            cfg.conn_id = "fake-%d" % (len(built) + 1)
        built.append(cfg)
        return _FakeConn(cfg)

    monkeypatch.setattr(state.broker_manager, "add_connection", _fake_add)
    # 隔离真实 app.db 里的持久化连接：否则 find_by_identity 会命中它，
    # 走到 existing 分支并用**真实适配器**的 is_connected()（恒 False）判定。
    monkeypatch.setattr(state.broker_manager, "find_by_identity",
                        lambda *a, **k: None)
    monkeypatch.setattr(autoconnect, "detect_candidates",
                        lambda: [_cand(root=str(root), running=True, accounts=[_acc()],
                                       default_account_id="22453951")])
    monkeypatch.setattr(autoconnect, "detect_bridge_agents",
                        lambda *a, **k: _REAL_DETECT_BRIDGE_AGENTS([_bridge_cand(root)]))

    cid = asyncio.run(phase_broker._auto_connect_active())
    kinds = [c.connector_key for c in built]
    assert "qmt.big.bridge.file" in kinds, (
        "路径 A 未连通时必须兜底走桥；实际建连: %r" % kinds)
    # 桥在这组桩里同样没连通（_DeadAdapter）⇒ 返回值必须是 ""，不得把「建出了
    # 桥连接」冒充成「接入成功」（假绿灯，2026-10-15）。
    assert cid == "", (
        "未连通时 _auto_connect_active 必须返回空串，实际 %r" % cid)


def test_auto_connect_prefers_path_a_when_it_really_connects(tmp_path, monkeypatch):
    """路径 A 真连上 ⇒ 收工，**不得**再去建桥（避免多余连接）。"""
    from app.bootstrap import phase_broker
    from core.state import init_broker_manager, state

    init_broker_manager()

    class _AliveAdapter:
        def is_connected(self):
            return True

    class _FakeConn:
        def __init__(self, cfg):
            self.cfg = cfg
            self.connected = False
            self.adapter = _AliveAdapter()

    built: list = []

    def _fake_add(cfg, ac):          # noqa: ARG001
        # ★ 必须像真实 `_build` 那样补 conn_id：真实现里 conn_id 为空才生成 uuid。
        #   假实现若不补，cfg.conn_id 恒为 "" ⇒ 断言 "" == "" 会**空洞通过**。
        if not cfg.conn_id:
            cfg.conn_id = "fake-%d" % (len(built) + 1)
        built.append(cfg)
        return _FakeConn(cfg)

    monkeypatch.setattr(state.broker_manager, "add_connection", _fake_add)
    # 隔离真实 app.db 里的持久化连接：否则 find_by_identity 会命中它，
    # 走到 existing 分支并用**真实适配器**的 is_connected()（恒 False）判定。
    monkeypatch.setattr(state.broker_manager, "find_by_identity",
                        lambda *a, **k: None)
    monkeypatch.setattr(autoconnect, "detect_candidates",
                        lambda: [_cand(running=True, accounts=[_acc()],
                                       default_account_id="22453951")])
    probe_calls: list = []
    monkeypatch.setattr(autoconnect, "detect_bridge_agents",
                        lambda *a, **k: probe_calls.append(1) or [])

    cid = asyncio.run(phase_broker._auto_connect_active())
    assert cid == built[0].conn_id
    assert probe_calls == [], "路径 A 已连通就不该再探桥"


def test_find_bridge_conn_matches_by_normalized_bridge_dir(mgr):
    """桥连接的幂等判据是**归一化后的 bridge_dir**（不是 client_path）。"""
    from app.bootstrap import phase_broker
    from core.state import state

    conn = _inject(mgr, "b1", client_path="")
    conn.cfg.connector_key = "qmt.big.bridge.file"
    conn.cfg.bridge_dir = r"C:/Users/x/qmt_work/bigqmt_bridge"
    monkeypatch_target = state
    monkeypatch_target.broker_manager = mgr

    got = phase_broker._find_bridge_conn("c:/users/x/qmt_work/bigqmt_bridge/")
    assert got is not None and got.cfg.conn_id == "b1", "路径写法差异必须归一化后匹配"
    assert phase_broker._find_bridge_conn(r"C:/other/dir") is None
    assert phase_broker._find_bridge_conn("") is None


def test_auto_connect_scans_candidates_exactly_once(monkeypatch):
    """★ 一轮自动连接**只能**全盘探测一次候选（路径 A 与路径 B 共用结果）。

    为什么值得钉死：``detect_candidates`` 是全盘查找，本机实测 **2.6~3.3s**，
    而守护每 30s 就跑一轮。若 A 探一次、B 内部
    （``detect_bridge_agents(candidates=None)`` 会自己再探）又探一次，就是
    **每 30s 白烧 ~3s 磁盘 IO 的常驻浪费** —— 用户「先开软件、后开券商」时
    守护会一直探下去。

    本用例把「只探一次」钉成可证伪事实：把 ``detect_candidates`` 换成计数桩，
    并把 ``detect_bridge_agents`` 还原成**真实实现**（autouse fixture 已将其换成
    空桩），真实实现收到 ``cands=[]`` 后**不会**再调 ``detect_candidates``。
    """
    from app.bootstrap import phase_broker

    calls: list = []

    def _counting_detect():
        calls.append(1)
        return []

    monkeypatch.setattr(autoconnect, "detect_candidates", _counting_detect)
    monkeypatch.setattr(autoconnect, "detect_bridge_agents", _REAL_DETECT_BRIDGE_AGENTS)

    assert asyncio.run(phase_broker._auto_connect_active()) == ""
    assert len(calls) == 1, (
        "一轮自动连接应只全盘探测一次候选（A 与 B 共用）；实测 %d 次" % len(calls))


# ---------------- 守护：不得用「路径 A 有没有候选」当闸门（2026-10-14 真机取证） ----------------

class _EmptyMgr:
    """没有任何连接的 manager（`_should_auto_connect([])` → True，即「该去接」）。"""

    def all_connections(self):
        return []


def test_guard_reaches_path_b_without_any_running_xtquant_client(monkeypatch):
    """★ 守护不得前置「有没有运行中的 xtquant 客户端」闸门。

    旧写法先 ``detect_candidates`` + ``pick_active``，拿不到就 ``continue`` ——
    于是**路径 B 永远走不到**：大 QMT 走文件桥时唯一可用的交易通道是跑在 QMT
    **内置策略**里的 agent，而路径 A 因券商未授权（``mdl_auth_gt_ipc_pair=0``）
    永远连不上。这正是「QMT 明明开着、桥也部署好了，客户端却始终不自动接上」的
    第二条复现路径（第一条在 ``phase_broker.setup``，已修）。

    判据：即使路径 A **零候选**，也必须调用 ``_auto_connect_active``（由它去走 B）。
    """
    from app.bootstrap import phase_broker
    from app.bootstrap import phase_watchdogs as pw
    from core.config import settings
    from core.state import state

    monkeypatch.setattr(settings, "broker_auto_connect", True)
    monkeypatch.setattr(state, "broker_manager", _EmptyMgr())

    calls: list = []

    async def _fake_active() -> str:
        calls.append(1)
        return ""

    monkeypatch.setattr(phase_broker, "_auto_connect_active", _fake_active)

    asyncio.run(pw._auto_connect_once())
    assert calls == [1], (
        "守护必须在路径 A 零候选时**仍然**调用 _auto_connect_active，"
        "否则文件桥永远不会被复探接入（本机实测的真故障）")


def test_guard_respects_disabled_switch(monkeypatch):
    """反面一：``broker_auto_connect=False`` ⇒ 一次都不探（用户显式关闭）。"""
    from app.bootstrap import phase_broker
    from app.bootstrap import phase_watchdogs as pw
    from core.config import settings
    from core.state import state

    monkeypatch.setattr(settings, "broker_auto_connect", False)
    monkeypatch.setattr(state, "broker_manager", _EmptyMgr())

    calls: list = []

    async def _fake_active() -> str:
        calls.append(1)
        return ""

    monkeypatch.setattr(phase_broker, "_auto_connect_active", _fake_active)
    assert asyncio.run(pw._auto_connect_once()) == ""
    assert calls == [], "开关关闭时守护必须完全不介入"


def test_guard_respects_manual_disconnect(monkeypatch):
    """反面二：列表非空且全部 inactive（用户手动断开过）⇒ 不介入，尊重意图。

    ★ 这条与上一条同样重要：若判据退化成「列表非空就接」，用户手动断开后
    软件会立刻自己接回来 —— 用户再也无法「安静地断开」。
    """
    from app.bootstrap import phase_broker
    from app.bootstrap import phase_watchdogs as pw
    from core.config import settings
    from core.state import state

    class _Off:
        connected = False
        cfg = type("_Cfg", (), {"active": False})()

    class _MgrWithOffConn:
        def all_connections(self):
            return [_Off()]

    monkeypatch.setattr(settings, "broker_auto_connect", True)
    monkeypatch.setattr(state, "broker_manager", _MgrWithOffConn())

    calls: list = []

    async def _fake_active() -> str:
        calls.append(1)
        return ""

    monkeypatch.setattr(phase_broker, "_auto_connect_active", _fake_active)
    assert asyncio.run(pw._auto_connect_once()) == ""
    assert calls == [], "用户手动断开过就不该自动接回来"


# ---------------- 守护：假绿灯与退避（2026-10-15 真机取证） ----------------
#
# 本机实测（客户端后端 21118，凌晨连跑数小时）：
#   * 日志每 30~38s 一条 ``broker auto-connect guard connected: 8f975d5f982d``；
#   * 而同一时刻 ``GET /brokers`` 里那条连接 ``connected=false``、
#     ``last_error=交易连接异常：WaitingFreeWriter instances exceed maximum limit``、
#     ``reconnect_attempts=36``。
# 也就是说：**日志全程绿灯，界面全程红灯**。根因是 ``_auto_connect_active``
# 把「路径 A 建出了连接对象」当成「接入成功」返回了 conn_id。
#
# 同时守护恒 30s 一轮、无退避地重复整轮探测 + 建连 ⇒ 每轮新建最多 12 个
# ``XtQuantTrader``，几十轮后把 SDK writer 池撑爆（``WaitingFreeWriter``），
# 反过来盖住了真正的券商拒绝（``connect rc=-1``）。退避是止血。

class _NotReallyConn:
    """「建出来了但没连上」的连接 —— 假绿灯的原型。

    ★ ``cfg.active`` 必须是 True：``_should_auto_connect`` 对「列表非空且全部
    inactive」返回 False（尊重用户手动断开）。若这里不给 active，守护会在入口
    就 return ""，两个用例都会**空洞通过** —— 断言根本没跑到被测的那段判定。
    """

    def __init__(self, cid: str):
        self.connected = False
        self.last_error = "交易连接异常：WaitingFreeWriter instances exceed maximum limit"
        self.cfg = type("_Cfg", (), {"conn_id": cid, "active": True})()


class _MgrWithDeadConn:
    def __init__(self, cid: str):
        self._c = _NotReallyConn(cid)
        self.cache_calls: list = []

    def all_connections(self):
        return [self._c]


def test_guard_must_not_report_connected_when_conn_is_down(monkeypatch):
    """★ ``_auto_connect_active`` 返回了 conn_id，但那条连接没连上 ⇒ 不得算接入。

    旧实现据此打 ``broker auto-connect guard connected: xxx`` 并刷新活跃指针，
    于是日志全程绿灯、界面全程红灯（2026-10-15 真机）。
    """
    from app.bootstrap import phase_broker
    from app.bootstrap import phase_watchdogs as pw
    from core.config import settings
    from core.state import state

    m = _MgrWithDeadConn("dead-conn")
    monkeypatch.setattr(settings, "broker_auto_connect", True)
    monkeypatch.setattr(state, "broker_manager", m)
    monkeypatch.setattr(state, "cache_active_bridge",
                        lambda: m.cache_calls.append(1))

    async def _fake_active() -> str:
        return "dead-conn"

    monkeypatch.setattr(phase_broker, "_auto_connect_active", _fake_active)

    assert asyncio.run(pw._auto_connect_once()) == "", (
        "未连通的连接不得被当作「已接入」")
    assert m.cache_calls == [], (
        "未连通时不得刷新活跃指针，否则会把坏连接扶成 active（假绿灯）")


def test_guard_reports_connected_when_conn_is_really_up(monkeypatch):
    """对照组：真连上了 ⇒ 正常算接入并刷新活跃指针（防修复过度）。"""
    from app.bootstrap import phase_broker
    from app.bootstrap import phase_watchdogs as pw
    from core.config import settings
    from core.state import state

    m = _MgrWithDeadConn("live-conn")
    monkeypatch.setattr(settings, "broker_auto_connect", True)
    monkeypatch.setattr(state, "broker_manager", m)
    monkeypatch.setattr(state, "cache_active_bridge",
                        lambda: m.cache_calls.append(1))

    async def _fake_active() -> str:
        # 模拟「本轮真的接上了」：注意守护入口的 `_should_auto_connect` 是在
        # **接入前**判的（那时还是 False），接上后状态才翻正。
        m._c.connected = True
        m._c.last_error = ""
        return "live-conn"

    monkeypatch.setattr(phase_broker, "_auto_connect_active", _fake_active)

    assert asyncio.run(pw._auto_connect_once()) == "live-conn"
    assert m.cache_calls == [1]


def test_guard_backs_off_after_repeated_failures():
    """★ 连续「探了但没接上」必须退避，不得恒 30s 一轮地重连。

    恒频重连实测会自伤：每轮新建最多 12 个 XtQuantTrader，几十轮后把 SDK
    writer 池撑爆（``WaitingFreeWriter``），盖住真正的券商拒绝。
    """
    from app.bootstrap import phase_watchdogs as pw

    pw._reset_guard_backoff()
    try:
        first = pw._guard_next_interval()
        assert first == 30.0
        pw._guard_fail_streak = 1
        assert pw._guard_next_interval() == 60.0
        pw._guard_fail_streak = 2
        assert pw._guard_next_interval() == 120.0
        pw._guard_fail_streak = 10
        assert pw._guard_next_interval() == pw._GUARD_MAX_BACKOFF, (
            "退避必须有上限，否则失败久了守护等于停摆")
    finally:
        pw._reset_guard_backoff()


# ---------------- 守护：先跑再睡 + 无事可做不计失败（2026-10-15 真机取证） ----------------
#
# 真机时序（客户端后端 21118，验证窗口 08:42:06~08:42:49）：
#   * 08:42:06 启动 8s 预算内探测都没跑完（冷缓存）⇒ 交给后台守护；
#   * 守护 ``while True: sleep(30)`` **先睡后跑** ⇒ 首轮 08:42:39 才开始；
#   * 08:42:39 建路径 A 连接、08:42:44 解析交易目录 —— 路径 A 的连接尝试
#     （慢失败，41.87s 起）还没结束，验证脚本 08:42:49 就关机了 ⇒ 桥兜底
#     根本没跑到。用户看到「客户端没有自动连接大 qmt」，而机制其实是通的。
# 下面两组用例把「先跑再睡」与「路径 A 挂起不得饿死路径 B」钉成可证伪事实。

class _ThreadScopedSleepShim:
    """把 ``pw.asyncio`` 换成「只在本线程替换 ``sleep``」的代理。

    ★ 为什么不能 ``monkeypatch.setattr(pw.asyncio, "sleep", ...)``
    （2026-10-15 全量 flaky 根因）：``pw.asyncio`` 就是**全局 asyncio 模块**，
    改它的 ``sleep`` 会击中**进程内所有**后台任务 —— lifespan 装在 TestClient
    portal 线程上的 broadcast(5s)/pump(2s)/health(5s)/guard(30s)/条件单(2s)
    循环全都 ``await asyncio.sleep(...)``。它们在用例的 patch 窗口里醒来就会
    调到测试桩：既把断言桶（``slept``/``order``）污染成假失败，桩里抛的
    ``_StopGuard`` 又会炸死生产任务（实测
    ``PytestUnhandledThreadExceptionWarning``）。

    本代理对**其它线程**透明委派真实 ``asyncio.sleep``；只有发起用例的线程
    （``threading.get_ident() == main``）拿到测试桩。其余属性（CancelledError /
    create_task / get_running_loop…）一律委派，phase_watchdogs 里其它函数
    与后台循环的行为不受影响。
    """

    def __init__(self, real, fn, main):
        self._real = real
        self._fn = fn
        self._main = main

    async def sleep(self, delay):
        if threading.get_ident() != self._main:
            await self._real.sleep(delay)
            return
        await self._fn(delay)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_guard_runs_first_round_before_sleeping(monkeypatch):
    """★ 守护必须**先跑再睡**：首轮立即执行，不等 30s。

    旧顺序 ``sleep(30)`` 在前 ⇒ 启动预算（8s）在冷缓存机器上都没探完时，
    守护首轮比启动还晚 30s。修法：循环体改为先 ``_auto_connect_once()``
    再 ``sleep`` —— 首轮在相位装配结束时立即执行，与 phase_broker 的启动
    自动连接无缝衔接（那次可能刚被 8s 预算掐断）。

    ★ 隔离手法：不碰全局 ``asyncio.sleep``，只换 ``pw.asyncio`` 模块属性
    （见 ``_ThreadScopedSleepShim``）；``_fake_once`` 也只记录**发起线程**的
    调用 —— portal 线程上 lifespan 装的其它后台循环必须看到真实行为。
    """
    from app.bootstrap import phase_watchdogs as pw
    from core.config import settings
    from core.state import state

    pw._reset_guard_backoff()
    monkeypatch.setattr(settings, "broker_auto_connect", True)
    monkeypatch.setattr(state, "broker_manager", _EmptyMgr())

    main = threading.get_ident()
    order: list = []
    slept: list = []

    async def _fake_once():
        if threading.get_ident() == main:
            order.append("run")
        return ""

    class _StopGuard(Exception):
        """第一轮睡下时退出守护（测试用，勿在生产出现）。"""

    async def _fake_sleep(d):
        order.append("sleep")
        slept.append(d)
        raise _StopGuard

    monkeypatch.setattr(pw, "_auto_connect_once", _fake_once)
    monkeypatch.setattr(pw, "asyncio",
                        _ThreadScopedSleepShim(asyncio, _fake_sleep, main))
    try:
        with pytest.raises(_StopGuard):
            asyncio.run(pw._broker_auto_connect_guard())
    finally:
        pw._reset_guard_backoff()

    assert order == ["run", "sleep"], (
        "守护必须先跑一轮再睡；旧的「先睡再跑」让首轮比启动预算还晚 30s")
    assert slept == [60.0], (
        "首轮失败后按退避节拍睡（30×2^1=60s）；连上/无事可做则回到 30s")


def test_guard_idle_rounds_do_not_inflate_backoff(monkeypatch):
    """★「无事可做」的轮次不得计入失败退避。

    旧实现把 ``_auto_connect_once`` 返回空串一律当失败 ⇒ 连接稳定连上后
    每 30s 白涨一节拍，几分钟后退避到 300s 上限；真掉线时要等最多 5 分钟
    才复探。这些轮次（已连上 / 用户手动断开 / 开关关闭）**没有任何探测动作**，
    谈不上失败，必须把退避清零、保持 30s 基础节拍。

    ★ 隔离手法同 ``test_guard_runs_first_round_before_sleeping``：只换
    ``pw.asyncio``（线程作用域），不碰全局 ``asyncio.sleep``。
    """
    from app.bootstrap import phase_watchdogs as pw
    from core.config import settings
    from core.state import state

    pw._reset_guard_backoff()
    monkeypatch.setattr(settings, "broker_auto_connect", True)

    class _Live:
        connected = True
        cfg = type("_Cfg", (), {"active": True, "conn_id": "live"})()

    class _MgrWithLiveConn:
        def all_connections(self):
            return [_Live()]

    monkeypatch.setattr(state, "broker_manager", _MgrWithLiveConn())

    main = threading.get_ident()
    slept: list = []

    class _StopGuard(Exception):
        pass

    async def _fake_sleep(d):
        if threading.get_ident() != main:
            await asyncio.sleep(d)      # portal 线程：真实睡，不污染断言桶
            return
        if len(slept) >= 2:
            raise _StopGuard
        slept.append(d)

    monkeypatch.setattr(pw, "asyncio",
                        _ThreadScopedSleepShim(asyncio, _fake_sleep, main))
    try:
        with pytest.raises(_StopGuard):
            asyncio.run(pw._broker_auto_connect_guard())
    finally:
        pw._reset_guard_backoff()

    assert slept == [30.0, 30.0], (
        "无事可做的轮次必须保持 30s 基础节拍；旧实现会退避到 60/120…300，"
        "导致真掉线后复探被无谓拖慢")


# ---------------- 守护装配受开关控制（2026-10-15） ----------------

def test_guard_installed_only_when_switch_on(monkeypatch):
    """★ 开关关闭 ⇒ **不装配**守护任务（「完全不介入」，不空转）。

    装配条件写反的后果是自动连接整体停摆（用户报过的故障），必须正反两面都锁：
    开 ⇒ 装配；关 ⇒ 不装配。
    """
    from app.bootstrap import phase_watchdogs as pw
    from core.config import settings

    class _FakeApp:
        def __init__(self):
            self.state = types.SimpleNamespace()

    async def _fake_guard():
        return None

    monkeypatch.setattr(pw, "_broker_auto_connect_guard", _fake_guard)

    monkeypatch.setattr(settings, "broker_auto_connect", False)
    app_off = _FakeApp()
    asyncio.run(pw._install_auto_connect_guard(app_off))
    assert getattr(app_off.state, "_broker_acg", None) is None, (
        "开关关闭时不得装配守护 —— 那是个每 30s 空转一圈的任务")

    monkeypatch.setattr(settings, "broker_auto_connect", True)
    app_on = _FakeApp()
    asyncio.run(pw._install_auto_connect_guard(app_on))
    assert getattr(app_on.state, "_broker_acg", None) is not None, (
        "开关开启时必须装配守护，否则「先开软件后开券商客户端」永远连不上")


# ---------------- 路径 A 挂起不得饿死路径 B（2026-10-15 真机取证） ----------------
#
# ``add_connection(cfg, True)`` / ``activate()`` 内部都走 ``_safe_start`` →
# ``adapter.start()``。本机路径 A 是慢失败（41.87s 起），旧实现无 bound ⇒
# ``_auto_connect_active`` 整轮被拖死，文件桥（路径 B）永远走不到。
# 下面两组用例把 ``_bounded_connect_action`` 的硬超时钉死：超时后必须继续
# 探桥，且桥连上时正常返回桥的 conn_id。

def _hang_then_ok_add(bridge_out: list, release, started):
    """造一个「路径 A 挂起、路径 B 秒连」的 add_connection 桩。

    ★ 按 ``connector_key`` 分派：路径 A（无 key）阻塞到 ``release``（模拟
    adapter.start() 慢失败），路径 B（桥）立即返回已连接的假连接。
    """

    class _Adapter:
        def __init__(self, alive):
            self._alive = alive

        def is_connected(self):
            return self._alive

    class _FakeConn:
        def __init__(self, cfg, alive):
            self.cfg = cfg
            self.connected = False
            self.adapter = _Adapter(alive)

    def _fake_add(cfg, ac):          # noqa: ARG001
        if cfg.connector_key:
            if not cfg.conn_id:
                cfg.conn_id = "bridge-%d" % (len(bridge_out) + 1)
            bridge_out.append(cfg)
            return _FakeConn(cfg, alive=True)
        started.set()
        release.wait(1.0)            # 有界：测试结束前一定放行，不会挂住 pytest
        return _FakeConn(cfg, alive=False)

    return _fake_add


def test_path_a_add_connection_hang_does_not_starve_path_b(tmp_path, monkeypatch):
    """★ 路径 A 建连挂起（慢失败）⇒ 硬超时后必须继续走文件桥并返回桥连接。"""
    import threading
    import time as _t

    from app.bootstrap import phase_broker
    from core.state import init_broker_manager, state

    init_broker_manager()
    root = tmp_path / "gd_qmt"
    br = tmp_path / "bridge"
    _write_agent(tmp_path, root, bridge_dir=br)
    # 把 32s 硬上限缩到 250ms：被测的是「超时后继续走 B」的控制流，不是时长本身。
    monkeypatch.setattr(phase_broker, "CONNECT_HARD_TIMEOUT", 0.25)

    release = threading.Event()
    started = threading.Event()
    bridge_built: list = []
    monkeypatch.setattr(state.broker_manager, "add_connection",
                        _hang_then_ok_add(bridge_built, release, started))
    monkeypatch.setattr(state.broker_manager, "find_by_identity",
                        lambda *a, **k: None)
    monkeypatch.setattr(autoconnect, "detect_candidates",
                        lambda: [_cand(root=str(root), running=True, accounts=[_acc()],
                                       default_account_id="22453951")])
    probe_calls: list = []
    monkeypatch.setattr(autoconnect, "detect_bridge_agents",
                        lambda *a, **k: probe_calls.append(1)
                        or _REAL_DETECT_BRIDGE_AGENTS([_bridge_cand(root)]))

    t0 = _t.monotonic()
    cid = asyncio.run(phase_broker._auto_connect_active())
    elapsed = _t.monotonic() - t0

    assert started.is_set(), "路径 A 的建连应当被尝试过（否则本用例空洞通过）"
    assert probe_calls == [1], (
        "路径 A 挂起后必须继续探测文件桥（不得被饿死）；实测 %r" % (probe_calls,))
    assert elapsed < 5.0, (
        "路径 A 的挂起不得拖住整轮自动连接（硬超时后放行）；实测 %.2fs" % elapsed)
    assert cid == bridge_built[0].conn_id, (
        "桥连上时必须返回桥的 conn_id（路径 A 超时不配冒充结局）；实测 %r" % cid)


def test_path_a_activate_hang_does_not_starve_path_b(tmp_path, monkeypatch):
    """★ existing 分支：``activate``（内部 _safe_start）挂起同样不得饿死路径 B。"""
    import threading
    import time as _t

    from app.bootstrap import phase_broker
    from core.state import init_broker_manager, state

    init_broker_manager()
    root = tmp_path / "gd_qmt"
    br = tmp_path / "bridge"
    _write_agent(tmp_path, root, bridge_dir=br)
    monkeypatch.setattr(phase_broker, "CONNECT_HARD_TIMEOUT", 0.25)

    class _Existing:
        def __init__(self):
            self.connected = False
            self.cfg = type("_Cfg", (), {"conn_id": "old", "name": "旧连接",
                                         "active": True, "connector_key": ""})()
            self.adapter = type("_A", (), {"is_connected": lambda s: False})()

    existing = _Existing()
    release = threading.Event()
    monkeypatch.setattr(state.broker_manager, "find_by_identity",
                        lambda *a, **k: existing)
    monkeypatch.setattr(state.broker_manager, "repair_client_path",
                        lambda *a, **k: None)

    def _hang_activate(cid):
        release.wait(1.0)            # 有界：模拟 activate 内 adapter.start() 挂起
        return existing

    monkeypatch.setattr(state.broker_manager, "activate", _hang_activate)
    bridge_built: list = []
    monkeypatch.setattr(state.broker_manager, "add_connection",
                        _hang_then_ok_add(bridge_built, release, threading.Event()))
    monkeypatch.setattr(autoconnect, "detect_candidates",
                        lambda: [_cand(root=str(root), running=True, accounts=[_acc()],
                                       default_account_id="22453951")])
    probe_calls: list = []
    monkeypatch.setattr(autoconnect, "detect_bridge_agents",
                        lambda *a, **k: probe_calls.append(1)
                        or _REAL_DETECT_BRIDGE_AGENTS([_bridge_cand(root)]))

    t0 = _t.monotonic()
    cid = asyncio.run(phase_broker._auto_connect_active())
    elapsed = _t.monotonic() - t0

    assert probe_calls == [1], (
        "路径 A 激活挂起后必须继续探测文件桥（不得被饿死）；实测 %r" % (probe_calls,))
    assert elapsed < 5.0, (
        "激活挂起不得拖住整轮自动连接；实测 %.2fs" % elapsed)
    assert cid == bridge_built[0].conn_id, (
        "桥连上时必须返回桥的 conn_id；实测 %r" % cid)

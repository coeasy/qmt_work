"""启动去重（``BrokerManager.dedupe_identical``）契约测试。

为什么需要它
------------
``find_by_identity`` 只能拦住**新建**重复，管不了库里**已经存在**的重复行。
实测（2026-09-19）客户端 ``app.db`` 里有两条六个身份字段逐字相同的连接，
界面于是列出两条一模一样的连接，其中一条必然连不上 —— 用户看到「有一条永远
红着」却无从判断该删哪条。

锁定四条不变量（错了会出真事）：

1. **身份完全相同必须合并成一条**；
2. **只认一级身份**：``userdata`` 与 ``userdata_mini`` 是同一客户端的两种**模式**，
   不算重复（与 ``find_by_identity`` 的二级放宽口径一致）；
3. **保留的那条必须是 active 的**，且被删者的 active 意图**合并进保留者** ——
   否则「删掉的那条恰好是 active」会变成「下次启动不自动连了」这种更隐蔽的故障；
4. **活跃指针若指向被删的连接必须改指保留者**，不能悬空。
"""
from __future__ import annotations

import pytest

from xtquant_client.manager import BrokerManager, Connection, ConnectionConfig


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


class _StubBridge:
    gateway = None


@pytest.fixture
def mgr(tmp_path):
    """独立临时 DB 的 BrokerManager（用完还原全局 DB，避免污染其它用例）。"""
    from core import db as db_mod

    prev = db_mod._db
    db_mod.init_db(tmp_path / "dedupe.db")
    try:
        yield BrokerManager()
    finally:
        db_mod._db = prev


def _inject(m, conn_id: str, *, active: bool = False,
            client_path: str = r"P:\stock\gd_qmt\userdata_mini",
            account_id: str = "22453951", broker_id: str = "generic",
            account_type: str = "STOCK") -> None:
    """塞一条连接进 manager（绕过 create_adapter，绝不碰真实券商 SDK）。"""
    cfg = ConnectionConfig(conn_id=conn_id, name=conn_id, broker_id=broker_id,
                           client_path=client_path, account_id=account_id,
                           account_type=account_type, active=active)
    m._conns[conn_id] = Connection(cfg=cfg, adapter=_StubAdapter(), bridge=_StubBridge())


def test_identical_connections_are_merged(mgr):
    """六字段逐字相同的三条 → 合并成一条。"""
    m = mgr
    _inject(m, "aaa", active=False)
    _inject(m, "bbb", active=False)
    _inject(m, "ccc", active=False)
    assert len(m._conns) == 3

    removed = m.dedupe_identical()

    assert len(m._conns) == 1, f"应只剩一条，实际 {list(m._conns)}"
    assert len(removed) == 2
    assert {r["conn_id"] for r in removed} == {"aaa", "bbb"}
    assert {r["kept"] for r in removed} == {"ccc"}


def test_path_case_and_separator_are_normalized(mgr):
    """路径大小写/分隔符不同也算同一条（``_norm_path`` 口径）。"""
    m = mgr
    _inject(m, "upper", client_path=r"P:\stock\gd_qmt\userdata_mini")
    _inject(m, "lower", client_path=r"p:\stock\gd_qmt\userdata_mini")

    m.dedupe_identical()

    assert len(m._conns) == 1, f"大小写不同应视为同一条：{list(m._conns)}"


def test_different_client_modes_are_not_duplicates(mgr):
    """``userdata`` 与 ``userdata_mini`` 是两种**模式**，不得误合并。"""
    m = mgr
    _inject(m, "mini", client_path=r"P:\stock\gd_qmt\userdata_mini")
    _inject(m, "full", client_path=r"P:\stock\gd_qmt\userdata")

    removed = m.dedupe_identical()

    assert removed == []
    assert set(m._conns) == {"mini", "full"}


def test_different_accounts_are_not_duplicates(mgr):
    """同客户端不同资金账号 = 两个账户，不得误合并。"""
    m = mgr
    _inject(m, "acc1", account_id="22453951")
    _inject(m, "acc2", account_id="88888888")

    m.dedupe_identical()

    assert set(m._conns) == {"acc1", "acc2"}


def test_active_intent_is_merged_into_keeper(mgr):
    """★ 被删掉的那条是 active 时，保住自动连接意图 —— 不能静默丢掉。"""
    m = mgr
    _inject(m, "old_active", active=True)
    _inject(m, "newer_idle", active=False)

    m.dedupe_identical()

    assert list(m._conns) == ["old_active"], "应保留 active 那条，而非最新那条"
    assert m._conns["old_active"].cfg.active is True, "active 意图不得丢失"


def test_active_pointer_is_remapped_not_left_dangling(mgr):
    """活跃指针指向被删连接时，必须改指保留者（不能悬空）。"""
    m = mgr
    # 让 survivor 是 active 那条 ⇒ 保留者 = survivor，doomed 被删；
    # 把活跃指针指向 doomed，验证去重后指针改指 survivor 而不是悬空。
    _inject(m, "doomed", active=False)
    _inject(m, "survivor", active=True)
    m._active_id = "doomed"

    m.dedupe_identical()

    assert m._active_id == "survivor", f"活跃指针悬空或指错：{m._active_id}"
    assert m._active_id in m._conns


def test_no_duplicates_returns_empty(mgr):
    """没有重复时不得误删任何东西。"""
    m = mgr
    _inject(m, "only")
    assert m.dedupe_identical() == []
    assert set(m._conns) == {"only"}


def test_persisted_rows_are_actually_removed(mgr):
    """去重必须**落库**：只删内存不删行，下次启动重复又回来了。"""
    m = mgr
    _inject(m, "dup1", active=False)
    _inject(m, "dup2", active=False)
    m._persist(m._conns["dup1"].cfg)
    m._persist(m._conns["dup2"].cfg)

    m.dedupe_identical()

    from core.db import get_db
    rows = get_db().query("SELECT conn_id FROM broker_connections")
    assert [r["conn_id"] for r in rows] == ["dup2"], f"库里仍残留重复行：{rows}"

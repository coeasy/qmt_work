"""券商档案注册表测试：落库持久化 + 能力协商 + 动态探测。"""
import tempfile
from pathlib import Path

from app.db import DB
from xtquant_client.registry import BrokerProfile, Registry


def _tmp_db():
    d = Path(tempfile.mkdtemp())
    return DB(d / "app.db"), d


def test_reload_includes_builtins():
    db, _ = _tmp_db()
    reg = Registry(db)
    ids = {p.id for p in reg.list()}
    assert "guojin" in ids and "huaxin" in ids


def test_custom_profile_persists_across_reload():
    db, _ = _tmp_db()
    reg = Registry(db)
    p = BrokerProfile(id="mybroker", name="自定义券商", adapter="xtp",
                      default_client_path="C:/x",
                      capabilities=["quote", "kline"])
    reg.register_profile(p)
    # 新实例从 DB 加载（重启等价）
    reg2 = Registry(db)
    loaded = reg2.get("mybroker")
    assert loaded is not None
    assert loaded.name == "自定义券商"
    assert loaded.adapter == "xtp"
    # 内置档案仍在
    assert reg2.get("guojin") is not None


def test_cannot_delete_builtin():
    db, _ = _tmp_db()
    reg = Registry(db)
    try:
        reg.unregister_profile("guojin")
        raise AssertionError("内置券商应不可删除")
    except ValueError:
        pass


def test_unregister_custom():
    db, _ = _tmp_db()
    reg = Registry(db)
    reg.register_profile(BrokerProfile(id="cb", name="CB", adapter="xtp"))
    reg.unregister_profile("cb")
    assert reg.get("cb") is None
    # DB 中也已删除
    reg2 = Registry(db)
    assert reg2.get("cb") is None


def test_effective_capabilities_derives_from_account_types():
    db, _ = _tmp_db()
    reg = Registry(db)
    p = reg.get("guojin")  # STOCK/CREDIT/OPTION/FUTURES
    caps = reg.effective_capabilities(p)
    assert "quote" in caps and "trade" in caps
    assert "option" in caps and "credit" in caps and "futures" in caps


def test_negotiate_splits_supported_unsupported():
    db, _ = _tmp_db()
    reg = Registry(db)
    r = reg.negotiate("guojin", ["quote", "trade", "flying"])
    assert r["found"] is True
    assert "quote" in r["supported"]
    assert "flying" in r["unsupported"]


def test_probe_uses_runtime_adapter_then_falls_back():
    db, _ = _tmp_db()
    reg = Registry(db)

    class FakeAdapter:
        def capabilities(self):
            return ["quote", "kline", "custom_x"]

    caps = reg.probe("guojin", FakeAdapter())
    assert "custom_x" in caps
    # 无运行时适配器 → 回退到静态推导
    caps2 = reg.probe("guojin")
    assert "quote" in caps2 and "custom_x" not in caps2


def test_list_profiles_v2_marks_custom():
    from xtquant_client.registry import list_profiles_v2, registry
    db, _ = _tmp_db()
    # list_profiles_v2 读取全局单例，需把临时 DB 挂到全局注册表
    registry.attach_db(db)
    registry.register_profile(BrokerProfile(id="cb2", name="CB2", adapter="xtp"))
    items = {it["id"]: it for it in list_profiles_v2()}
    assert items["guojin"]["is_custom"] is False
    assert items["cb2"]["is_custom"] is True

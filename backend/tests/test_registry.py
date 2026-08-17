"""券商档案注册表测试：落库持久化 + 能力协商 + 动态探测。"""
import os
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


def test_uf_broker_registered():
    """阶段 5 修复：恒生 UF 定制版（华泰/国泰君安/海通 等白标）内置档案。"""
    from xtquant_client.registry import BROKER_PROFILES
    ids = {p.id for p in BROKER_PROFILES}
    assert "uf" in ids
    p = next(p for p in BROKER_PROFILES if p.id == "uf")
    # 适配器复用 xtp（同 SDK）；支持账户类型覆盖股票/信用/期权/期货
    assert p.adapter == "xtp"
    assert "STOCK" in p.supported_account_types
    assert "OPTION" in p.supported_account_types
    # 默认路径指向 C:\恒生UF\userdata_mini
    assert "恒生UF" in p.default_client_path


def test_discovery_recognizes_uf_procs():
    """阶段 5 修复：discovery 识别恒生 UF 系列进程名（HsUFTrader / UFClient 等）。"""
    from xtquant_client.discovery import _QMT_PROC_NAMES, _ROOT_HINTS
    uf_procs = {"hsuftrader.exe", "ufclient.exe", "uftrader.exe",
                "hundsunuf.exe", "ufqmt.exe", "ufminiqmt.exe"}
    assert uf_procs.issubset(_QMT_PROC_NAMES), f"缺少 UF 进程识别: {uf_procs - _QMT_PROC_NAMES}"
    # 关键词 hint 能把"恒生" / "Hundsun" 路径归到 uf 档案
    uf_hints = [k for k, b in _ROOT_HINTS if b == "uf"]
    assert any("恒生" in k for k in uf_hints)
    assert any("hundsun" in k.lower() for k in uf_hints)


def test_xtp_resolves_uf_layout():
    """阶段 5 修复：xtp._XTQUANT_REL 包含恒生 UF 定制版常见嵌入位（client/uf/app/inner）。"""
    from xtquant_client.xtp import _XTQUANT_REL
    rel_strs = "\\".join(p.replace("\\", "/") for p in _XTQUANT_REL)
    # 至少包含 client/python/Lib/site-packages 与 uf/Lib/site-packages
    assert any("client" in p and "python" in p and "site-packages" in p for p in _XTQUANT_REL), \
        f"缺少 client/python 路径: {_XTQUANT_REL}"
    assert any("uf" in p.split(os.sep) for p in _XTQUANT_REL), \
        f"缺少 uf 路径: {_XTQUANT_REL}"

"""三档远程访问模型守卫。

覆盖：
- normalize_remote_access 合法/非法/大小写/空白
- is_remote_enabled / is_wan_enabled / effective_host 决策表
- remote_mode_label 三档中文名
- 启动自检：默认密钥 + lan/wan 拒绝启动；wan 无 TOTP 拒绝启动
- off 档保持现状：默认密钥 + 0.0.0.0 拒绝（兼容既有断言）

设计意图（docs/REMOTE_ACCESS_DECISION.md）：
- lan/wan 强制非默认 API key（防网络可达+弱密钥接管）
- wan 强制 TOTP（防真实资金远程交易）
- off 档行为不变（向后兼容）
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

# 保证 config.py 在测试进程中被干净导入（避免其他测试污染 settings 单例）

import pytest


def _fresh_config():
    """返回**已存在的** core.config 单例，并把档位字段重置回默认值。

    ★ 2026-10-03 修（发布阻塞级）：原实现是 ``sys.modules.pop("core.config")`` +
    重新 import，即**每个用例都造一个新的 ``settings`` 对象**。而生产代码在生产
    导入期就用 ``from core.config import settings`` 绑定了旧对象的引用
    （``app.routes._common`` / 路由层 / ``gateway.auth`` …），从此永远指向旧对象。
    同一个测试进程里于是出现两个 settings「真相源」——任何跨模块比较都不可信。

    实测后果（全量同进程回归必现，逐文件跑全绿 ⇒ 典型假绿灯）：
      ``[WS-DENIED] token='contract-introspect-key'  settings.api_key='qmt-dev-key'``
      ``test_ws_contract`` 3 条 4401 —— 新 settings 读到了 introspect 泄漏的
      ``QMT_API_KEY``，而路由层仍在用旧 settings 的主密钥。

    被测的 ``normalize_remote_access`` / ``effective_host`` 等函数都在**调用时**
    读取模块级 ``settings``，原地改写与「全新实例」语义完全等价，且不分裂单例。
    """
    import core.config as cc

    # 复位档位字段：patch.object 会自行恢复，但直接赋值的历史测试可能留脏值。
    cc.settings.remote_access = "off"
    cc.settings.host = "127.0.0.1"
    cc.settings.api_key = "qmt-dev-key"
    return cc


def _fresh_run():
    """返回已导入的 run 模块（绝不重建 core.config 单例）。

    关键约束：``run.settings`` 必须与 ``core.config.settings`` 是**同一个对象**，
    否则对其中一个打补丁另一个看不到。只要不 pop ``core.config``，
    ``run.py`` 的 ``from core.config import settings`` 拿到的就是同一单例。
    """
    import run as _run_mod

    assert _run_mod.settings is _fresh_config().settings, (
        "run.settings 与 core.config.settings 不是同一对象 ⇒ 单例已被其他测试分裂，"
        "此时对 run.settings 打补丁不会影响路由层，测试结论无效")
    return _run_mod


def test_normalize_accepts_valid_lowercase():
    cfg = _fresh_config()
    assert cfg.normalize_remote_access("off") == "off"
    assert cfg.normalize_remote_access("lan") == "lan"
    assert cfg.normalize_remote_access("wan") == "wan"


def test_normalize_case_insensitive_and_strips():
    cfg = _fresh_config()
    assert cfg.normalize_remote_access("LAN") == "lan"
    assert cfg.normalize_remote_access("  Wan  ") == "wan"
    assert cfg.normalize_remote_access("OFF") == "off"


def test_normalize_fallback_to_off_on_garbage():
    cfg = _fresh_config()
    assert cfg.normalize_remote_access("nonsense") == "off"
    assert cfg.normalize_remote_access("") == "off"
    assert cfg.normalize_remote_access(None) == "off"


def test_effective_host_off_uses_settings_host():
    cfg = _fresh_config()
    assert cfg.effective_host() == "127.0.0.1"
    with patch.object(cfg.settings, "remote_access", "off"):
        with patch.object(cfg.settings, "host", "0.0.0.0"):
            # off 档：effective_host 直接返回 settings.host（用户显式设 0.0.0.0 会走 off 档，
            # 由 _self_check 拒绝默认密钥的启动）
            assert cfg.effective_host() == "0.0.0.0"


def test_effective_host_forces_0000_when_remote_enabled():
    cfg = _fresh_config()
    with patch.object(cfg.settings, "remote_access", "lan"):
        with patch.object(cfg.settings, "host", "127.0.0.1"):
            assert cfg.effective_host() == "0.0.0.0"
    with patch.object(cfg.settings, "remote_access", "wan"):
        assert cfg.effective_host() == "0.0.0.0"


def test_is_remote_enabled_semantics():
    cfg = _fresh_config()
    with patch.object(cfg.settings, "remote_access", "off"):
        assert cfg.is_remote_enabled() is False
    with patch.object(cfg.settings, "remote_access", "lan"):
        assert cfg.is_remote_enabled() is True
    with patch.object(cfg.settings, "remote_access", "wan"):
        assert cfg.is_remote_enabled() is True


def test_is_wan_enabled_only_for_wan():
    cfg = _fresh_config()
    for v in ("off", "lan"):
        with patch.object(cfg.settings, "remote_access", v):
            assert cfg.is_wan_enabled() is False
    with patch.object(cfg.settings, "remote_access", "wan"):
        assert cfg.is_wan_enabled() is True


def test_remote_mode_label_three_tiers():
    cfg = _fresh_config()
    assert cfg.remote_mode_label() == "单机（仅本机）"
    with patch.object(cfg.settings, "remote_access", "lan"):
        assert cfg.remote_mode_label() == "内网（局域网多设备）"
    with patch.object(cfg.settings, "remote_access", "wan"):
        assert cfg.remote_mode_label() == "公网（远程访问）"


def test_default_is_off():
    cfg = _fresh_config()
    assert cfg.settings.remote_access == "off"


def test_default_config_payload_has_remote_access():
    cfg = _fresh_config()
    assert "remote_access" in cfg._default_config_payload()


# ---------- 启动自检：三档差异化守卫 ----------

def test_self_check_rejects_default_key_with_off_and_remote_listen(caplog):
    """既有断言：off 档 + 默认密钥 + 0.0.0.0 拒绝启动。"""
    mod = _fresh_run()
    caplog.set_level("WARNING", logger="qmt_work")
    with patch.object(mod.settings, "remote_access", "off"):
        with patch.object(mod.settings, "host", "0.0.0.0"):
            with patch.object(mod.settings, "api_key", "qmt-dev-key"):
                with pytest.raises(SystemExit):
                    mod._self_check()
    joined = " ".join(r.message for r in caplog.records)
    assert "默认 API Key" in joined or "qmt-dev-key" in joined


def test_self_check_accepts_off_with_localhost():
    """off 档 + 127.0.0.1 + 默认密钥：允许（本地开发）。"""
    mod = _fresh_run()
    with patch.object(mod.settings, "remote_access", "off"):
        with patch.object(mod.settings, "host", "127.0.0.1"):
            with patch.object(mod.settings, "api_key", "qmt-dev-key"):
                mod._self_check()  # 不应抛 SystemExit


def test_self_check_rejects_default_key_with_lan(caplog):
    """lan 档 + 默认密钥：拒绝（防网络可达+弱密钥接管）。"""
    mod = _fresh_run()
    caplog.set_level("ERROR", logger="qmt_work")
    with patch.object(mod.settings, "remote_access", "lan"):
        with patch.object(mod.settings, "api_key", "qmt-dev-key"):
            with pytest.raises(SystemExit):
                mod._self_check()
    joined = " ".join(r.message for r in caplog.records)
    assert "默认密钥" in joined or "强密钥" in joined


def test_self_check_accepts_lan_with_custom_key():
    """lan 档 + 非默认密钥：允许。"""
    mod = _fresh_run()
    with patch.object(mod.settings, "remote_access", "lan"):
        with patch.object(mod.settings, "api_key", "my-strong-key-12345"):
            mod._self_check()


def test_self_check_rejects_wan_without_totp(caplog):
    """wan 档 + 无 TOTP：拒绝（防真实资金远程交易）。"""
    mod = _fresh_run()
    caplog.set_level("ERROR", logger="qmt_work")
    with patch.object(mod.settings, "remote_access", "wan"):
        with patch.object(mod.settings, "api_key", "custom-key-123456"):
            with patch.object(mod.settings, "totp_secret", ""):
                with pytest.raises(SystemExit):
                    mod._self_check()
    joined = " ".join(r.message for r in caplog.records)
    assert "TOTP" in joined or "二次确认" in joined


def test_self_check_accepts_wan_with_totp():
    """wan 档 + 非默认密钥 + TOTP：允许。"""
    mod = _fresh_run()
    with patch.object(mod.settings, "remote_access", "wan"):
        with patch.object(mod.settings, "api_key", "custom-key-123456"):
            with patch.object(mod.settings, "totp_secret", "JBSWY3DPEHPK3PXP"):
                mod._self_check()

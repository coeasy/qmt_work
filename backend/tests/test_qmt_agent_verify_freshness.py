"""`scripts/qmt_agent_verify.py` 的**部署漂移**与**注册名大小写**护栏。

背景（2026-10-12 真机事故，两次同族事故）
----------------------------------------
① **部署漂移型假绿灯**：源码已含「转发函数不得冒充终端注入」的修复
   （``capture_qmt_injected_funcs(globals(), exclude=_STATE["forwarded"])``），
   但 QMT 里跑的部署件是修复**前 10 分钟**生成的旧件 ⇒ agent 报 ``funcs=76``（假），
   并把「本模式没有任何终端注入的函数 ⇒ 下单不可用」这条关键告警静默吞掉。
   文件在、语法对、能跑 —— 旧的 bundle 检查全绿，**只有内容比对**才逮得到。

② **注册名大小写假告状**：注册树里的显示名是 ``QMT_WORK_AGENT``，而工具默认
   ``--strategy qmt_work_agent``（小写）—— 精确比对把**已注册**误报成
   「不在注册树里」，用户据此白做一次导入。

工具不在 ``sys.path`` 上，故用 importlib 按路径加载。
"""
from __future__ import annotations

import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
VERIFY_SCRIPT = ROOT / "scripts" / "qmt_agent_verify.py"


def _load_verify():
    """把 scripts/qmt_agent_verify.py 当模块加载（它不在 sys.path 上）。"""
    spec = importlib.util.spec_from_file_location("_qmt_agent_verify_under_test",
                                                  VERIFY_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_bundle_freshness_detects_stale_deploy(tmp_path):
    """源码现生成件 == 部署件 ⇒ 新鲜；少一行修复（旧版本）⇒ 判滞后并指出首个差异行。"""
    verify = _load_verify()
    gen = verify._load_bundle_generator()
    text = gen.build(stamp="2026-10-12 08:22")
    p = tmp_path / "QMT_WORK_AGENT.py"
    p.write_text(text, encoding="utf-8")

    # 与源码现生成件一致（仅「生成于」戳不同）⇒ 判新鲜
    assert verify.bundle_freshness(str(p))["fresh"] is True

    # 抹掉关键修复（模拟旧部署件）⇒ 判滞后
    stale = text.replace("exclude=_STATE", "XXXXXXXXXXXXXX")
    assert stale != text, "测试锚点失效：源码里已无 exclude=_STATE"
    p.write_text(stale, encoding="utf-8")
    fr = verify.bundle_freshness(str(p))
    assert fr["fresh"] is False
    assert fr["reason"] == "content_differs"
    assert fr["first_diff_line"] > 0


def test_bundle_freshness_ignores_stamp_only_diff(tmp_path):
    """「生成于」戳每次生成都不同，**不得**因此判滞后（否则天天假告警）。"""
    verify = _load_verify()
    gen = verify._load_bundle_generator()
    p = tmp_path / "b.py"
    p.write_text(gen.build(stamp="2026-01-01 00:00"), encoding="utf-8")
    assert verify.bundle_freshness(str(p))["fresh"] is True
    p.write_text(gen.build(stamp="2099-12-31 23:59"), encoding="utf-8")
    assert verify.bundle_freshness(str(p))["fresh"] is True


def test_bundle_health_reports_source_fresh(tmp_path):
    """bundle_health 必须把「源码一致性」作为独立字段上浮，供 diag/前端消费。"""
    verify = _load_verify()
    gen = verify._load_bundle_generator()
    p = tmp_path / "b.py"
    p.write_text(gen.build(stamp=""), encoding="utf-8")
    h = verify.bundle_health(str(p))
    assert h["source_fresh"] is True
    assert h["source_drift_line"] == -1


def _write_qmt_log(tmp_path, name_in_log):
    qmt = tmp_path / "qmt"
    (qmt / "userdata" / "log").mkdir(parents=True)
    log = qmt / "userdata" / "log" / "XtClient_20261012.log"
    log.write_text(
        "[CStrategyLoadSetting]Account:22453951 , FomrulaName: %s, "
        "startupAutorun: true, ID:7\n"
        "from configFormula, index:8, utfName:%s, gbkName:%s\n"
        % (name_in_log, name_in_log, name_in_log),
        encoding="utf-8")
    return qmt


def test_registration_name_match_is_case_insensitive(tmp_path):
    """★ 注册名与 --strategy 默认值大小写不同时**不得**误报「未注册」（假告状）。"""
    verify = _load_verify()
    qmt = _write_qmt_log(tmp_path, "QMT_WORK_AGENT")
    reg = verify.registration(str(qmt), "qmt_work_agent")   # 小写默认名
    assert reg["registered"] is True, "大小写不同就把已注册报成未注册 —— 假告状"
    assert reg["autorun"] is True


def test_registration_really_missing_name_is_false(tmp_path):
    """反向锚：名字确实不在注册树里时必须报 False（不能为了好脸色一律放行）。"""
    verify = _load_verify()
    qmt = _write_qmt_log(tmp_path, "OTHER_STRATEGY")
    reg = verify.registration(str(qmt), "qmt_work_agent")
    assert reg["registered"] is False

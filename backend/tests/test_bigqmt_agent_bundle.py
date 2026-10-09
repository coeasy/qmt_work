# -*- coding: utf-8 -*-
"""单文件 agent bundle 的契约测试。

为什么必须有这组测试
--------------------
真机实测（2026-10-01）确认两件事：

1. **QMT 的策略列表是客户端持久化的注册树，不是策略目录扫描** ——
   目录里两个 **md5 完全相同**的 .py，重启后一个在列表、一个不在，即可自证。
   所以「拷贝文件进 python/ 就会出现在模型交易里」是错的，必须做一次
   「新建/导入策略」的注册动作。发布成单文件的意义是：导入时只需选一个文件。
   （子目录仍不扫、`_` 前缀仍被排除，这两条独立成立。）
2. **入口必须自己就是实现**：QMT 只把下单/查询函数注入被挂载的那个文件的
   命名空间，薄壳转发会让唯一来源捕获彻底失效。

因此这里锁五件事：
  1. 生成器产出的 bundle 通过自检、py3.6 兼容、入口函数齐备；
  2. bundle 里不再残留 ``from qmt_api import``（内联完整性）；
  3. 配置查找能兼容旧部署（``agent_bigqmt/agent_config.json``）与内嵌配置；
  4. 自检 + 心跳真的会落盘（自动验证的数据来源）；
  5. 源码编码必须是 **UTF-8**，且**注册态判定不会静默假阴性**
     （副日志里没有注册树行；按 mtime 取「最新」会取到副日志并把「已注册」
      全部误报成「未注册」—— 这类假阴性必须由用例锁住）。
"""
from __future__ import annotations

import ast
import importlib.util
import json
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import gen_qmt_agent_bundle as gen  # noqa: E402


def _write_bundle(tmp_path: Path, embed: dict | None = None) -> Path:
    """把生成的 bundle 写到临时目录（模拟 QMT 策略目录顶层）。"""
    text = gen.build(embed=embed, stamp="test")
    out = tmp_path / "qmt_work_agent.py"
    out.write_text(text, encoding="utf-8", newline="\n")
    return out


def _load(path: Path):
    """按文件路径导入 bundle 模块（模块名唯一，避免跨用例互相污染）。"""
    name = "qmt_bundle_" + uuid.uuid4().hex
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# 1. 生成物形态
# ---------------------------------------------------------------------------
def test_bundle_passes_self_check():
    problems = gen.check(gen.build(stamp="test"))
    assert problems == [], problems


def test_bundle_is_py36_compatible():
    text = gen.build(stamp="test")
    # feature_version 会把 walrus / 位置-only 参数等 3.7+ 语法直接判错
    ast.parse(text, filename="<bundle>", feature_version=(3, 6))


def test_bundle_exposes_mount_entries():
    """QMT 挂载入口 / 回调 / 自检入口都必须在 bundle 顶层。"""
    top = set()
    for node in ast.parse(gen.build(stamp="test")).body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            top.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    top.add(t.id)
    for name in ("init", "handle_init", "handlebar", "order_callback",
                 "deal_callback", "after_init", "self_probe", "write_status",
                 "Executor", "capture_qmt_injected_funcs"):
        assert name in top, name


def test_bundle_has_no_residual_qmt_api_import():
    """薄壳转发会让「唯一来源捕获」拿到空名单 —— 内联必须彻底。"""
    text = gen.build(stamp="test")
    assert "from qmt_api import" not in text
    assert text.count("from __future__ import print_function") == 1


def test_generator_requires_embed_anchor():
    with pytest.raises(SystemExit):
        gen._embed("x = 1  # 没有锚点\n", {"bridge_dir": "D:/x"})


def test_bundle_has_main_guard_for_standalone_process():
    """QMT 的「运行」是把策略当**独立进程**拉起来的：
    `pythonw.exe -u <策略.py> <userdata> <ts>`。没有 `if __name__ == "__main__"`
    自举 = 进程起来后立刻退出 = 模型交易里看到「启动即停止」。
    """
    text = gen.build(stamp="test")
    tree = ast.parse(text, filename="<bundle>", feature_version=(3, 6))
    assert gen._has_main_guard(tree), "bundle 缺自举入口"
    assert "run_standalone" in {n.name for n in tree.body
                                if isinstance(n, ast.FunctionDef)}, (
        "bundle 未暴露 run_standalone（独立进程主循环）")
    assert "should_autorun" in {n.name for n in tree.body
                                if isinstance(n, ast.FunctionDef)}


def test_generator_check_flags_missing_main_guard():
    """把自举入口挖掉后，生成器自检必须报错（且点名「启动即停止」）。"""
    text = gen.build(stamp="test")
    broken = text.replace('if __name__ == "__main__" and should_autorun():',
                          'if False:  # 被挖掉的自举')
    problems = gen.check(broken)
    assert any("自举" in p for p in problems), problems


def test_own_names_are_injected_not_placeholder():
    """`_OWN_NAMES` 必须由生成器按 AST 填实 —— 仍是空 frozenset() 就是没注入。

    它决定「哪些名字算我们自己的」，直接决定 probe 报出的注入函数面是真是假。
    """
    text = gen.build(stamp="test")
    assert gen._OWN_ANCHOR not in text, "占位锚点仍存在 → 注入没生效"
    # 必须给 __name__（否则模块末尾的 `if __name__ == "__main__"` 会 NameError）；
    # 故意**不**给 "__main__"，以证明它不会被误判成自举场景。
    ns: dict = {"__name__": "qmt_bundle_probe"}
    exec(compile(text, "<bundle>", "exec"), ns)  # noqa: S102 - 测自己的产物
    own = ns["_OWN_NAMES"]
    assert isinstance(own, frozenset) and len(own) > 20, sorted(own)[:5]
    # 我们自己的实现必须在里面（否则会被当成终端注入的函数）
    for name in ("Executor", "init", "handlebar", "capture_qmt_injected_funcs",
                 "run_standalone"):
        assert name in own, name
    # 终端注入的名字绝不能在里侧（否则唯一来源捕获会把真注入过滤掉）
    for name in ("passorder", "get_trade_detail_data", "cancel"):
        assert name not in own, name


def test_generator_check_flags_unfilled_own_names():
    text = gen.build(stamp="test")
    broken = text.replace("# 由生成器按 bundle 的 AST 注入（运行时用于剔除自有名字）",
                          "")
    # 把注入出来的 frozenset 换回占位锚点
    lines = broken.split("\n")
    out, skipping = [], False
    for ln in lines:
        if ln.startswith("_OWN_NAMES = frozenset(["):
            skipping = True
            out.append(gen._OWN_ANCHOR)
            continue
        if skipping:
            if ln.strip() == "])":
                skipping = False
            continue
        out.append(ln)
    problems = gen.check("\n".join(out))
    assert any("_OWN_NAMES" in p for p in problems), problems


# ---------------------------------------------------------------------------
# 2. 配置查找（旧部署兼容 + 内嵌）
# ---------------------------------------------------------------------------
def test_load_config_finds_legacy_subdir_config(tmp_path):
    bundle = _write_bundle(tmp_path)
    bridge = tmp_path / "bridge"
    legacy = tmp_path / "agent_bigqmt"
    legacy.mkdir()
    (legacy / "agent_config.json").write_text(json.dumps({
        "bridge_dir": str(bridge), "auth_token": "t", "trading_enabled": False,
    }), encoding="utf-8")

    module = _load(bundle)
    cfg = module.load_config()
    assert cfg["bridge_dir"] == str(bridge)
    assert cfg["_config_path"].endswith("agent_config.json")
    # 桥目录结构由 agent 自己保证存在，否则首次落盘会失败
    assert (bridge / "req").is_dir() and (bridge / "resp").is_dir()


def test_load_config_uses_embedded_when_no_file(tmp_path):
    bridge = tmp_path / "bridge_embedded"
    bundle = _write_bundle(tmp_path, embed={
        "bridge_dir": str(bridge), "auth_token": "abc", "trading_enabled": False,
    })
    module = _load(bundle)
    cfg = module.load_config()
    assert cfg["bridge_dir"] == str(bridge)
    assert cfg["_config_path"] == "<embedded>"


def test_load_config_raises_with_actionable_message(tmp_path):
    bundle = _write_bundle(tmp_path, embed=None)
    module = _load(bundle)
    with pytest.raises(RuntimeError) as exc:
        module.load_config()
    # 报错必须带上「找过哪些路径」，否则用户只能猜
    assert "agent_config.json" in str(exc.value)


# ---------------------------------------------------------------------------
# 3. 自检 + 心跳（自动验证的数据来源）
# ---------------------------------------------------------------------------
def test_self_probe_and_heartbeat_land_on_disk(tmp_path):
    bridge = tmp_path / "bridge"
    bundle = _write_bundle(tmp_path, embed={
        "bridge_dir": str(bridge), "auth_token": "t", "trading_enabled": False,
    })
    module = _load(bundle)
    cfg = module.load_config()

    injected = {"passorder": object(), "get_trade_detail_data": object()}
    probe = module.self_probe(cfg, injected, None, None)
    assert probe["injected"] == sorted(injected.keys())
    assert probe["trading_enabled"] is False
    # 写权限这一项必须是真跑出来的结论
    write_step = [s for s in probe["steps"] if s["name"] == "bridge_dir_write"][0]
    assert write_step["ok"] is True

    status = module.write_status(cfg, injected, None, None, 0.0)
    assert status["alive"] is True
    assert status["injected"] == sorted(injected.keys())

    on_disk = json.loads((bridge / "probe_result.json").read_text(encoding="utf-8"))
    assert on_disk["injected"] == sorted(injected.keys())
    hb = json.loads((bridge / "agent_status.json").read_text(encoding="utf-8"))
    assert hb["agent_ver"] == module._AGENT_VERSION


def test_status_reports_executor_meta(tmp_path):
    """心跳必须带 executor 画像（外部端据此判断能力与 trading 开关）。"""
    bridge = tmp_path / "bridge"
    bundle = _write_bundle(tmp_path, embed={"bridge_dir": str(bridge)})
    module = _load(bundle)
    cfg = module.load_config()

    class _FakeExecutor:
        def meta(self):
            return {"ver": "1.0.0", "actions": ["PROBE", "PLACE"],
                    "trading_enabled": False, "callback_bound": False}

    status = module.write_status(cfg, {}, None, _FakeExecutor(), 0.0)
    assert status["meta"]["actions"] == ["PROBE", "PLACE"]


def test_verify_script_flags_missing_agent(tmp_path):
    """验证器对「从未启动」必须给出未通过与下一步动作，而不是沉默通过。"""
    sys.path.insert(0, str(ROOT / "scripts"))
    import qmt_agent_verify as verify

    data = verify.collect(str(tmp_path))
    ok, lines, details = verify.evaluate(data)
    assert ok is False
    assert details["alive"] is False
    assert any("agent_status.json" in p for p in details["problems"])
    assert any("模型交易" in ln for ln in lines)


def test_verify_script_passes_on_fresh_heartbeat(tmp_path):
    sys.path.insert(0, str(ROOT / "scripts"))
    import qmt_agent_verify as verify

    now_ms = int(__import__("time").time() * 1000)
    (tmp_path / "req").mkdir()
    (tmp_path / "resp").mkdir()
    (tmp_path / "probe_result.json").write_text(json.dumps({
        "steps": [{"name": "bridge_dir_write", "ok": True, "detail": "ok"}],
        "injected": ["passorder", "cancel", "get_trade_detail_data"],
        "ctx_methods": ["get_full_tick"],
    }), encoding="utf-8")
    (tmp_path / "agent_status.json").write_text(json.dumps({
        "ts": now_ms, "alive": True, "uptime_s": 3, "py": "3.6.8",
        "agent_ver": "1.1.0", "trading_enabled": False,
        "meta": {"actions": ["PROBE"], "callback_bound": False},
    }), encoding="utf-8")

    ok, lines, details = verify.evaluate(verify.collect(str(tmp_path)))
    assert ok is True, lines
    assert details["alive"] is True


def test_verify_stale_probe_must_not_accuse_missing_funcs(tmp_path):
    """陈旧 probe 不得被当成「当前能力缺失」（TD-33）。

    策略没在跑时 probe_result.json 必然是历史快照。曾据此断言「未捕获 cancel」并计入
    致命 problems —— 违反 qmt_api._need「只能说未捕获，不能断言终端没有」的措辞纪律：
    注入函数只有在鲜活运行时才由 capture_qmt_injected_funcs(globals()) 决定。
    """
    sys.path.insert(0, str(ROOT / "scripts"))
    import qmt_agent_verify as verify

    bridge = tmp_path / "bridge"
    (bridge / "req").mkdir(parents=True)
    (bridge / "resp").mkdir(parents=True)
    (bridge / "probe_result.json").write_text(json.dumps({
        "steps": [{"name": "bridge_dir_write", "ok": True, "detail": "ok"}],
        "injected": ["passorder", "get_trade_detail_data"],   # 陈旧快照里没有 cancel
        "ctx_methods": [],
    }), encoding="utf-8")
    stale_ms = int(__import__("time").time() - 3600) * 1000   # 1 小时前的心跳
    (bridge / "agent_status.json").write_text(json.dumps({
        "ts": stale_ms, "uptime_s": 1, "py": "3.6.8",
        "agent_ver": "1.1.0", "trading_enabled": False,
    }), encoding="utf-8")

    ok, lines, details = verify.evaluate(verify.collect(str(bridge)))
    assert details["probe_stale"] is True
    assert details["alive"] is False
    # 关键：陈旧证据只能降级为提示，绝不能计入致命 problems
    assert not any("未捕获交易函数" in p for p in details["problems"])
    assert any("陈旧快照" in ln for ln in lines)


def test_verify_fresh_probe_still_flags_missing_funcs(tmp_path):
    """对照组：心跳新鲜且 probe 里确实没有 cancel ⇒ 必须照常报致命。

    与上一条成对存在 —— 否则「放宽陈旧判定」可能被误扩到鲜活场景，把真缺陷放过。
    """
    sys.path.insert(0, str(ROOT / "scripts"))
    import qmt_agent_verify as verify

    bridge = tmp_path / "bridge"
    (bridge / "req").mkdir(parents=True)
    (bridge / "resp").mkdir(parents=True)
    (bridge / "probe_result.json").write_text(json.dumps({
        "steps": [{"name": "bridge_dir_write", "ok": True, "detail": "ok"}],
        "injected": ["passorder", "get_trade_detail_data"],
        "ctx_methods": [],
    }), encoding="utf-8")
    now_ms = int(__import__("time").time() * 1000)
    (bridge / "agent_status.json").write_text(json.dumps({
        "ts": now_ms, "uptime_s": 5, "py": "3.6.8",
        "agent_ver": "1.1.0", "trading_enabled": False,
    }), encoding="utf-8")

    ok, lines, details = verify.evaluate(verify.collect(str(bridge)))
    assert details["probe_stale"] is False
    assert details["alive"] is True
    assert any("未捕获交易函数" in p for p in details["problems"])


# ---------------------------------------------------------------------------
# 4. 源码编码（★ 部署必须 UTF-8 —— 2026-10-08 真机事故，R27）
# ---------------------------------------------------------------------------
def test_recode_utf8_is_default_and_lossless():
    """UTF-8 是部署口径：recode 不动内容、不产告警。"""
    text = gen.build(stamp="test")
    out, enc, notes = gen.recode(text, "utf-8")
    assert (out, enc, notes) == (text, "utf-8", [])


def test_recode_gbk_still_converts_but_must_warn_loudly():
    """gbk/gb18030 仍可转换（给「不在本机跑、只给编辑器看」的场景），
    但**必须**带一条说清后果的强告警 —— 否则用户会以为它是推荐选项。

    R27 事故：gb18030 的 52 KB bundle 在开发机 py3.11 上编译通过，
    QMT 内置 pythonw.exe（Python 3.6.8）却 `SyntaxError: encoding problem: gb18030`，
    进程 return code:1，**一条自检都不落盘**。
    """
    text = gen.build(stamp="test")
    out, enc, notes = gen.recode(text, "gbk")
    assert enc in ("gbk", "utf-8")
    assert notes, "编码降级必须报出来，不能静默"
    assert any("3.6.8" in n or "py3.6" in n for n in notes), (
        "告警必须点明 QMT 内置解释器是 Python 3.6.8（否则用户不知道为什么要改）")
    if enc == "gbk":
        assert out.splitlines()[0] == "#coding:gbk"
        # 内联了多个源文件，各自带 cookie —— 只能留一条，否则第一行才是生效的那条
        assert out.count("coding:") == 1
        assert out.encode("gbk").decode("gbk") == out
        assert out.count("EMBEDDED_CONFIG") == text.count("EMBEDDED_CONFIG")
    else:
        # GBK 表示不了某些字符时必须**报出来**并退回 utf-8，不能静默写坏
        assert len(notes) >= 2


def test_recode_gb18030_warns_too():
    """gb18030 分支同样必须带告警（它在 QMT 上是实测失败的那一个）。"""
    _out, enc, notes = gen.recode(gen.build(stamp="test"), "gb18030")
    assert notes, "gb18030 必须带告警"
    assert enc in ("gb18030", "utf-8")


def test_generated_utf8_bundle_is_qmt_safe(tmp_path):
    """生成的 bundle 落盘后必须被「运行前体检」判为 QMT 安全编码。

    这条把生成器与验证器**接在一起**：任一侧改了编码口径，这里立刻红。
    """
    sys.path.insert(0, str(ROOT / "scripts"))
    import qmt_agent_verify as verify

    out = tmp_path / "bundle_utf8.py"
    out.write_text(gen.build(stamp="test"), encoding="utf-8", newline="\n")
    health = verify.bundle_health(str(out))
    assert health["encoding_ok"] is True, health.get("encoding_note")
    assert health["ok"] is True, health


def test_gbk_bundle_is_rejected_by_preflight_health(tmp_path):
    """反向对照：同一份内容落成 gb18030，体检必须判红。

    没有这条，「编码判定」很容易被改回永远绿灯 —— 那正是 R27 事故的成因。
    """
    sys.path.insert(0, str(ROOT / "scripts"))
    import qmt_agent_verify as verify

    text = gen.build(stamp="test")
    kept = [ln for ln in text.split("\n")
            if "coding" not in ln or not ln.lstrip().startswith("#")]
    body = "#coding:gb18030\n" + "\n".join(kept)
    out = tmp_path / "bundle_gbk.py"
    out.write_bytes(body.encode("gb18030"))
    health = verify.bundle_health(str(out))
    assert health["syntax_ok"] is True, "开发机能解析 —— 这正是它危险的地方"
    assert health["encoding_ok"] is False
    assert health["ok"] is False
    assert "3.6.8" in (health["encoding_note"] or "")


# ---------------------------------------------------------------------------
# 5. 注册态判定（防静默假阴性）
# ---------------------------------------------------------------------------
def test_registration_only_reads_main_log(tmp_path):
    """副日志没有注册树行。按 mtime 取「最新」会取到副日志，
    于是把「已注册」全部误报成「未注册」—— 这比报错危险，必须锁死。"""
    import os as _os
    import time as _time

    sys.path.insert(0, str(ROOT / "scripts"))
    import qmt_agent_verify as verify

    qmt = tmp_path / "qmt"
    logd = qmt / "userdata" / "log"
    logd.mkdir(parents=True)
    (qmt / "python").mkdir()
    main_log = logd / "XtClient_20261001.log"
    main_log.write_text(
        "[python formula] from configFormula, index:0, utfName:qmt_work_agent, gbkName:x\n"
        "[CStrategyLoadSetting]Account:a , FomrulaName: qmt_work_agent, "
        "startupAutorun: true, ID:9\n", encoding="utf-8")
    sibling = logd / "XtClient_datasource_20261001.log"
    sibling.write_text("副日志：没有注册树行\n", encoding="utf-8")
    older = _time.time() - 600          # 让主日志比副日志旧 → 老实现必然选错
    _os.utime(str(main_log), (older, older))

    reg = verify.registration(str(qmt), "qmt_work_agent")
    assert reg["registered"] is True
    assert reg["autorun"] is True
    assert reg["file_exists"] is False


def test_registration_unknown_when_main_log_lacks_tree(tmp_path):
    """主日志里一条注册树记录都没有 ⇒ 判「无法判定」，不能判「未注册」。"""
    sys.path.insert(0, str(ROOT / "scripts"))
    import qmt_agent_verify as verify

    qmt = tmp_path / "qmt"
    logd = qmt / "userdata" / "log"
    logd.mkdir(parents=True)
    (logd / "XtClient_20261001.log").write_text("nothing here\n", encoding="utf-8")

    assert verify.registration(str(qmt), "qmt_work_agent")["registered"] is None

    data = verify.collect(str(tmp_path / "bridge"), str(qmt), "qmt_work_agent")
    ok, lines, _ = verify.evaluate(data)
    assert ok is False
    assert any("无法判定" in ln for ln in lines)

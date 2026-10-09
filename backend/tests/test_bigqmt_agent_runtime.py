# -*- coding: utf-8 -*-
"""bundle 的**运行期**契约测试（R27 引入 · 2026-10-08 事故复盘）。

背景：`QMT_WORK_AGENT.py` 在 QMT 里点「运行」立刻停止，`return code:1`，
**一条自检都不落盘**。排查后确认四个独立缺陷，全部由本文件锁住：

  ① 源码编码 gb18030 → QMT 内置 Python 3.6.8 tokenizer SyntaxError
     （编码判定在 `test_bundle_hardening.py` / `test_bigqmt_agent_bundle.py` 锁）；
  ② 模块级 `os.path.dirname(__file__)` → 公式模式（终端把源码 exec 进自己的
     命名空间）**没有 `__file__`** ⇒ NameError，连自检都不落盘；
  ③ 缺 `if __name__ == "__main__"` 自举 → QMT 的「运行」是
     `pythonw.exe -u <策略.py> <userdata> <ts>`，进程起来没代码可跑就退出；
  ④ 注入面被自有函数污染 → probe 把 37 个「终端注入函数」报出来，真货只有 2 个
     （典型的假绿灯：面板看着能力齐全，其实一个都没接上）。

运行期的事实只有**真跑**才拿得到，所以这里第 3 组用真实解释器把 bundle 当
独立进程拉起来（与 QMT 完全同形），而不是靠读代码断言。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import gen_qmt_agent_bundle as gen  # noqa: E402
from qmt_agent_local_run import FAKE_INJECTED  # noqa: E402


@pytest.fixture(scope="module")
def bundle_text() -> str:
    """整份 bundle 的源码（生成一次，多处复用）。"""
    return gen.build(stamp="test")


@pytest.fixture()
def bundle_file(tmp_path: Path, bundle_text: str) -> Path:
    p = tmp_path / "QMT_WORK_AGENT.py"
    p.write_text(bundle_text, encoding="utf-8", newline="\n")
    return p


def _load_as_namespace(text: str, name: str = "qmt_bundle_under_test",
                       extra: dict | None = None) -> dict:
    """复刻 QMT **公式模式**加载：`exec(compile(src, '<string>', 'exec'), ns)`。

    ★ 关键：这个 ns 里**没有 `__file__`**（终端就是这么干的）。
    """
    ns: dict = {"__name__": name}
    if extra:
        ns.update(extra)
    exec(compile(text, "<string>", "exec"), ns)  # noqa: S102 - 测自己的产物
    return ns


# ---------------------------------------------------------------------------
# 1. 没有 __file__ 也必须能加载（公式模式）
# ---------------------------------------------------------------------------
def test_bundle_loads_without_dunder_file(bundle_text):
    """QMT 公式模式没有 `__file__`；模块级引用它会让**整份策略** NameError。

    当时的现象是「策略在列表里、点了没反应、bridge 里连 probe_result.json 都没有」。
    """
    ns = _load_as_namespace(bundle_text)
    # 没有 NameError 就已经是主要结论；再确认 _HERE 落到了一个真实目录
    here = ns["_HERE"]
    assert isinstance(here, str) and here
    assert os.path.isdir(here), here
    # 入口/回调/自举都在（说明模块体确实跑完了）
    for name in ("init", "handlebar", "after_init", "run_standalone",
                 "should_autorun", "capture_qmt_injected_funcs"):
        assert callable(ns[name]), name


def test_resolve_self_dir_prefers_argv_py(bundle_text):
    """`argv[0]` 是本 .py 时（独立进程模式）优先用它所在目录。"""
    ns = _load_as_namespace(bundle_text)
    target = os.path.dirname(os.path.abspath(__file__))
    argv = ["/somewhere/else/QMT_WORK_AGENT.py"]
    if os.path.isabs(argv[0]):
        argv[0] = os.path.join(target, "QMT_WORK_AGENT.py")
    old_argv = sys.argv
    try:
        sys.argv = argv
        assert ns["_resolve_self_dir"]() == target
    finally:
        sys.argv = old_argv


def test_resolve_self_dir_falls_back_to_cwd_when_nothing_known(bundle_text):
    """什么线索都没有时也不能抛异常 —— 退回 cwd 是最后一级退让。"""
    ns = _load_as_namespace(bundle_text)
    old_argv, old_path = sys.argv, list(sys.path)
    try:
        sys.argv = ["XtItClient.exe"]          # 公式模式：宿主进程名，不是 .py
        sys.path = [p for p in sys.path if p]
        if sys.path:
            sys.path[0] = ""
        got = ns["_resolve_self_dir"]()
        assert os.path.isdir(got)
    finally:
        sys.argv, sys.path = old_argv, old_path


# ---------------------------------------------------------------------------
# 2. 注入面必须只报「终端真注入的」
# ---------------------------------------------------------------------------
def test_capture_reports_exactly_the_injected_functions(bundle_text):
    """ns 里既有我们自己的函数、也有终端注入的函数 ⇒ 只许报后者。

    污染版会把 `Executor` / `init` / `handlebar` 一起报成「终端注入」，
    在面板上表现为「能力齐备」，实际上那些名字根本不是终端给的。
    """
    ns = _load_as_namespace(bundle_text)
    for name in FAKE_INJECTED:
        ns[name] = lambda *a, **k: None
    captured = ns["capture_qmt_injected_funcs"](ns)
    assert sorted(captured) == sorted(FAKE_INJECTED), sorted(captured)


def test_capture_ignores_dunder_and_non_callables(bundle_text):
    ns = _load_as_namespace(bundle_text)
    ns["__injected_looking__"] = lambda *a, **k: None
    ns["some_config_dict"] = {"a": 1}
    ns["passorder"] = lambda *a, **k: None
    assert sorted(ns["capture_qmt_injected_funcs"](ns)) == ["passorder"]


def test_own_names_covers_every_top_level_callable(bundle_text):
    """`_OWN_NAMES` 与「顶层可调用名字」两个集合必须对得上。

    它由生成器按 AST 注入。若以后有人手工加一个顶层函数却忘了重新生成 bundle，
    这个新函数就会被当成「终端注入」—— 这里提前红，避免面板上再次出现假能力。
    """
    import ast

    tree = ast.parse(bundle_text, filename="<bundle>", feature_version=(3, 6))
    top_callables = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            top_callables.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and isinstance(node.value, ast.Lambda):
                    top_callables.add(t.id)
    ns = _load_as_namespace(bundle_text)
    own = ns["_OWN_NAMES"]
    missing = {n for n in top_callables if n not in own}
    assert not missing, "这些顶层可调用名字未进 _OWN_NAMES: %s" % sorted(missing)


# ---------------------------------------------------------------------------
# 3. 自举判定（公式模式绝不能启动主循环）
# ---------------------------------------------------------------------------
def test_should_autorun_false_outside_main(bundle_text):
    """被 import / 被 exec 进别人命名空间 ⇒ 绝不自举（否则会把终端线程卡死）。"""
    ns = _load_as_namespace(bundle_text)
    assert ns["should_autorun"](["QMT_WORK_AGENT.py"]) is False
    assert ns["_is_process_main"]() is False


def test_should_autorun_respects_env_kill_switch(bundle_text, monkeypatch):
    """`QMT_WORK_AGENT_NO_AUTORUN=1` 必须能压掉自举（测试/诊断用）。

    为了让这条真的有判别力，把 `_is_process_main` 打桩成真、`__name__` 取
    `"__main__"` —— 否则前两条判据会把结果压成 False，测不出环境开关这一段。
    """
    monkeypatch.delenv("QMT_WORK_AGENT_NO_AUTORUN", raising=False)
    ns = _load_as_namespace(bundle_text, name="__main__")
    ns["_is_process_main"] = lambda: True
    assert ns["should_autorun"](["QMT_WORK_AGENT.py"]) is True
    monkeypatch.setenv("QMT_WORK_AGENT_NO_AUTORUN", "1")
    assert ns["should_autorun"](["QMT_WORK_AGENT.py"]) is False


def test_should_autorun_false_when_argv0_is_not_py(bundle_text, monkeypatch):
    """argv[0] 不是 .py（= 终端宿主进程）⇒ 不自举。"""
    monkeypatch.delenv("QMT_WORK_AGENT_NO_AUTORUN", raising=False)
    ns = _load_as_namespace(bundle_text, name="__main__")
    ns["_is_process_main"] = lambda: True
    assert ns["should_autorun"](["XtItClient.exe"]) is False
    assert ns["should_autorun"]([]) is False
    assert ns["should_autorun"](["QMT_WORK_AGENT.py"]) is True


def test_no_autorun_env_really_suppresses_process_main(bundle_file, tmp_path):
    """行为层验证环境开关：置了它，独立进程起来后**立刻退出且不落任何自检**。

    这条同时也是「进程不会僵死」的对照 —— 有自举时会常驻到 MAX_SECONDS。
    """
    sandbox = tmp_path / "strategy"
    bridge = tmp_path / "bridge"
    sandbox.mkdir()
    bridge.mkdir()
    target = sandbox / bundle_file.name
    target.write_text(bundle_file.read_text(encoding="utf-8"),
                      encoding="utf-8", newline="\n")
    (sandbox / "agent_config.json").write_text(json.dumps({
        "bridge_dir": str(bridge).replace("\\", "/"),
        "transport": "file", "auth_token": "t", "trading_enabled": False,
    }), encoding="utf-8")
    env = dict(os.environ)
    env["QMT_WORK_AGENT_NO_AUTORUN"] = "1"
    env["QMT_WORK_AGENT_MAX_SECONDS"] = "30"
    proc = subprocess.Popen(
        [sys.executable, "-u", str(target), str(tmp_path / "userdata"), "1"],
        cwd=str(sandbox), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = proc.communicate(timeout=30)
    assert proc.returncode == 0
    assert not (bridge / "agent_status.json").exists(), (
        "环境开关没生效：进程还是自举了（真机上是「本想干别的却把主循环跑起来」）")


# ---------------------------------------------------------------------------
# 4. 真跑：把 bundle 当独立进程拉起（与 QMT 完全同形）
# ---------------------------------------------------------------------------
def _spawn_standalone(bundle: Path, tmp_path: Path, max_seconds: int = 3):
    """`python -u <bundle> <userdata> <ts>` —— 与 QMT 拉起策略的 argv 一致。

    用**开发机解释器**跑，好处是这条用例不依赖本机装了 QMT；
    真·QMT 内置 py3.6 的实跑由 `scripts/qmt_agent_local_run.py --mode process` 负责
    （并已接进 deploy 的前置门禁）。
    """
    sandbox = tmp_path / "strategy"
    bridge = tmp_path / "bridge"
    sandbox.mkdir()
    bridge.mkdir()
    target = sandbox / bundle.name
    target.write_text(bundle.read_text(encoding="utf-8"),
                      encoding="utf-8", newline="\n")
    (sandbox / "agent_config.json").write_text(json.dumps({
        "bridge_dir": str(bridge).replace("\\", "/"),
        "transport": "file", "auth_token": "unit-token",
        "trading_enabled": False, "poll_interval_ms": 100,
    }, ensure_ascii=False), encoding="utf-8")
    env = dict(os.environ)
    env["QMT_WORK_AGENT_MAX_SECONDS"] = str(max_seconds)
    env.pop("QMT_WORK_AGENT_NO_AUTORUN", None)
    argv = [sys.executable, "-u", str(target), str(tmp_path / "userdata"),
            str(int(time.time() * 1000))]
    proc = subprocess.Popen(argv, cwd=str(sandbox), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return proc, sandbox, bridge


def test_standalone_process_boots_and_exits_cleanly(bundle_file, tmp_path):
    """★ 这是「能不能真的运行」的核心回归：进程启动 → 自检落盘 → 主循环 → 正常退出。

    修复前这里必然是「启动即退出 + return code 1 + bridge 里空空如也」。
    """
    proc, sandbox, bridge = _spawn_standalone(bundle_file, tmp_path, max_seconds=3)
    try:
        out, err = proc.communicate(timeout=60)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
        pytest.fail("独立进程没有按 QMT_WORK_AGENT_MAX_SECONDS 正常退出（主循环失控）")

    stderr = (err or b"").decode("utf-8", "replace")
    stdout = (out or b"").decode("utf-8", "replace")
    assert proc.returncode == 0, "return code=%s\nSTDOUT:\n%s\nSTDERR:\n%s" % (
        proc.returncode, stdout[-2000:], stderr[-2000:])

    probe_path = bridge / "probe_result.json"
    assert probe_path.exists(), ("自检没落盘 —— QMT 里就是「点了运行什么都没发生」"
                                 "\nSTDOUT:\n%s\nSTDERR:\n%s"
                                 % (stdout[-2000:], stderr[-2000:]))
    probe = json.loads(probe_path.read_text(encoding="utf-8"))
    assert probe.get("runtime_mode") == "standalone_process"
    # 独立进程模式下必须如实报「没有终端注入函数」，而不是编一个能力面
    assert probe.get("injected") == []

    status = json.loads((bridge / "agent_status.json").read_text(encoding="utf-8"))
    assert status["alive"] is True
    assert status["runtime_mode"] == "standalone_process"
    assert status["agent_ver"], "心跳必须带 agent 版本（外部端据此判断版本一致性）"
    assert int(time.time() * 1000) - int(status["ts"]) < 3600_000


def test_standalone_process_does_not_pretend_to_have_trading(bundle_file, tmp_path):
    """诚实边界：没有终端注入 ⇒ probe 必须明说「下单/查询不可用」。

    本仓铁律是**零 mock**，把「没能力」如实报出来比面板好看重要得多。
    """
    proc, _sandbox, bridge = _spawn_standalone(bundle_file, tmp_path, max_seconds=2)
    out, err = proc.communicate(timeout=60)
    text = ((out or b"") + (err or b"")).decode("utf-8", "replace")
    assert proc.returncode == 0
    # run_standalone 在注入面为空时会打印一条明确提示（带「独立进程模式」字样）
    assert "独立进程模式" in text
    assert "没有" in text and "注入" in text, text[-1500:]


def test_bundle_runs_under_qmt_launch_shape(bundle_file, tmp_path):
    """argv 形状必须与终端的实际命令一致：`-u <策略.py> <userdata> <ts>`。

    这条锁住「用户data 从 argv[1] 取」这条契约 —— 早前用 `sys.argv[0]` 自己当
    userdata，配置查找永远落空（而且只在真机上才暴露）。
    """
    proc, sandbox, bridge = _spawn_standalone(bundle_file, tmp_path, max_seconds=2)
    out, err = proc.communicate(timeout=60)
    text = ((out or b"") + (err or b"")).decode("utf-8", "replace")
    assert proc.returncode == 0
    assert "userdata=" in text, text[-1200:]
    # 配置是从 bundle 同目录的 agent_config.json 读的（不是 cwd 猜的）
    assert (sandbox / "agent_config.json").exists()
    assert (bridge / "agent_status.json").exists()

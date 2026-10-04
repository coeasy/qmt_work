"""无出口循环 / 无超时等待门禁（``scripts/check_unbounded_waits.py``）的回归测试。

这个门禁的价值**全部**在于「只抓铁证、不猜」，所以它的误报面就是它的成败：
首版实测踩到两类误报，各锁一条——

1. **超时可以是位置参数**：``threading.Event.wait(timeout)`` 的签名是
   ``wait(timeout=None)``，仓库里就是这么写的。只查 ``timeout=`` 关键字，
   会把一条**真·带超时**的等待报成「永不返回」。
2. **daemon 守护线程的循环不需要独立出口**：它随进程消亡。
   若按「无 break 即违规」报，本仓 4 条合法的守护循环（apikey 刷盘 / 日志告警
   消费者 / 桥接父进程看护 / 连接态泵）会全数变红 —— 门禁第一天就会被关掉。

同时锁住**不该漏**的方向：非 daemon、无出口、无 ``await`` 的循环必须报；
以及「嵌套函数里的 return 不算外层循环的出口」这个**假出口**陷阱。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = ROOT / "scripts" / "check_unbounded_waits.py"


def _load():
    spec = importlib.util.spec_from_file_location("_check_unbounded_waits", _SCRIPT)
    assert spec and spec.loader, f"无法加载 {_SCRIPT}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def gate(tmp_path, monkeypatch):
    """把 ROOT 指到临时目录，便于用合成源码驱动 scan_file。"""
    mod = _load()
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    return mod


def _scan(gate, tmp_path, src: str):
    f = tmp_path / "sample.py"
    f.write_text(src, encoding="utf-8")
    loops, resident, joins = gate.scan_file(f)
    return loops, resident, joins


# ---------------------------------------------------------------- Gate A

def test_true_loop_without_exit_or_await_is_reported(gate, tmp_path):
    loops, _, _ = _scan(gate, tmp_path, """
def busy():
    while True:
        x = 1
""")
    assert len(loops) == 1
    assert "不可中断" in loops[0]


def test_break_return_raise_count_as_exit(gate, tmp_path):
    for exit_stmt in ("    if x:\n        break", "        return 1",
                      "        raise RuntimeError('stop')"):
        indented = "\n".join("    " + ln for ln in exit_stmt.split("\n"))
        src = f"def f(x):\n    while True:\n{indented}\n"
        loops, _, _ = _scan(gate, tmp_path, src)
        assert loops == [], f"{exit_stmt!r} 应被当作出口：{loops}"


def test_exit_inside_nested_function_is_a_fake_exit(gate, tmp_path):
    """``def _inner(): ... return`` 里的 return 属于另一个函数，不是外层循环的出口。"""
    loops, _, _ = _scan(gate, tmp_path, """
def f():
    while True:
        def _inner():
            return 1
        _inner()
""")
    assert len(loops) == 1, loops


def test_await_makes_loop_cancellable_and_is_not_reported(gate, tmp_path):
    loops, resident, _ = _scan(gate, tmp_path, """
async def pump():
    while True:
        msg = await q.get()
        handle(msg)
""")
    assert loops == []
    assert len(resident) == 1


def test_daemon_thread_target_loop_is_not_reported(gate, tmp_path):
    """daemon=True ⇒ 随进程消亡，不需要独立停止机制。"""
    loops, resident, _ = _scan(gate, tmp_path, """
def _loop():
    while True:
        time.sleep(1)

threading.Thread(target=_loop, daemon=True).start()
""")
    assert loops == [], loops
    assert len(resident) == 1 and "daemon" in resident[0]


def test_non_daemon_thread_target_loop_IS_reported(gate, tmp_path):
    """反面：非 daemon 线程卡住会真的阻塞进程退出 ⇒ 必须报。"""
    loops, _, _ = _scan(gate, tmp_path, """
def _loop():
    while True:
        time.sleep(1)

threading.Thread(target=_loop).start()
""")
    assert len(loops) == 1, loops


def test_variable_condition_is_not_reported(gate, tmp_path):
    """不猜「变量是否可能一直为真」——只抓字面量恒真。"""
    loops, _, _ = _scan(gate, tmp_path, """
def f(flag):
    while flag:
        flag = compute()
""")
    assert loops == []


# ---------------------------------------------------------------- Gate B

def test_join_without_timeout_is_reported(gate, tmp_path):
    _, _, joins = _scan(gate, tmp_path, """
def f(worker):
    worker.join()
""")
    assert len(joins) == 1, joins


def test_join_with_keyword_timeout_is_ok(gate, tmp_path):
    _, _, joins = _scan(gate, tmp_path, """
def f(worker):
    worker.join(timeout=5)
""")
    assert joins == []


def test_event_wait_positional_timeout_is_ok(gate, tmp_path):
    """首版误报点：``ev.wait(timeout)`` 是位置传参，同样是真超时。"""
    _, _, joins = _scan(gate, tmp_path, """
def f(ev, timeout):
    if ev.wait(timeout):
        return 1
""")
    assert joins == [], joins


def test_event_wait_without_any_timeout_is_reported(gate, tmp_path):
    _, _, joins = _scan(gate, tmp_path, """
def f(ev):
    ev.wait()
""")
    assert len(joins) == 1, joins

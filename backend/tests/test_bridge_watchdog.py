"""bridge 父进程看护测试：父进程（主后端）死亡后，桥接子进程必须自动退出，杜绝孤儿残留。

对应修复：xtquant_client/bridge_server.py 的 _watch_parent / _parent_alive；
xtquant_client/bridge_client.py 拉起子进程时传 --parent-pid。
"""
import os
import subprocess
import sys
import time


BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable


def _spawn_parent():
    return subprocess.Popen([PY, "-c", "import time; time.sleep(600)"],
                            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))


def _spawn_bridge(parent_pid):
    env = dict(os.environ)
    env["PYTHONPATH"] = BACKEND + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.Popen(
        [PY, "-m", "xtquant_client.bridge_server", "--adapter", "guojin",
         "--config", "{}", "--parent-pid", str(parent_pid)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env, text=True)


def test_bridge_alive_while_parent_alive():
    parent = _spawn_parent()
    bridge = _spawn_bridge(parent.pid)
    try:
        time.sleep(4)
        assert bridge.poll() is None, "父进程存活时 bridge 不应退出"
    finally:
        parent.terminate()
        parent.wait(timeout=10)
        bridge.terminate()
        try:
            bridge.wait(timeout=5)
        except subprocess.TimeoutExpired:  # noqa: PERF203
            bridge.kill()


def test_bridge_self_exit_on_parent_death():
    parent = _spawn_parent()
    bridge = _spawn_bridge(parent.pid)
    time.sleep(4)
    assert bridge.poll() is None, "父进程存活时 bridge 不应退出"
    parent.terminate()
    parent.wait(timeout=10)
    deadline = time.time() + 15
    while time.time() < deadline:
        if bridge.poll() is not None:
            break
        time.sleep(0.5)
    assert bridge.poll() is not None, "父进程死亡后 bridge 未自动退出（看护失效）"
    assert bridge.poll() == 0, f"bridge 应以 0 退出，实际 rc={bridge.poll()}"

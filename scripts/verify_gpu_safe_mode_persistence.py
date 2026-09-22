#!/usr/bin/env python3
"""打包态 GPU 自愈回归：证明「安全模式不会把 ok=false 改回 true」。

## 为什么需要这个用例（2026-09-21 实测缺陷）

``electron/main.cjs`` 里有一段「跨过崩溃窗口后写 ok=true，避免下次再探测」的收尾。
它**漏判了 SAFE_MODE** —— 安全模式下压根没用过 GPU（已
``disableHardwareAcceleration`` + swiftshader），跑满崩溃窗口当然是「不崩」，
于是被误判成「GPU 好了」并写回 true；下一次启动读回 true ⇒ 回到正常模式 ⇒ 再崩一次。
用户的体感是「这客户端一半概率打不开，能打开的那次一重启又坏」。这就是**隔次启动失败**。

修法只是加一行 ``if (SAFE_MODE) return;``。但这类**接线错误**用纯函数单测抓不住
（判据本身没错，错的是没接上），只有**真的连续启动几次**才能证伪 —— 所以本用例
不 mock、不读源码，直接跑打包产物。

## 判据（连续三次启动，每次都读 gpu-verdict.json）

| 次 | 启动时读到 | 期望模式 | 期望结果 | 期望判定文件 |
| --- | --- | --- | --- | --- |
| 1 | 无判定 | 正常 | 本机无 GPU ⇒ 崩（GPU 子进程崩 2 次） | 写成 ``{"ok":false}`` |
| 2 | ``false`` | 安全模式 | **必须起来** | **仍是 ``false``** ← ★ 核心断言 |
| 3 | ``false`` | 安全模式 | **必须起来**（用户实际体验到的那一次） | 仍是 ``false`` |

- 若第 1 次就正常起来并写了 ``{"ok":true}`` ⇒ 本机 GPU 正常，整个场景不适用 ⇒ **SKIP**（不是失败）。
- 修复前的表现：第 2 次起来但把判定改成 ``true``，第 3 次**崩** ⇒ 本用例红。

## 用法

    backend/runtimes/cp311/python.exe tests/verify_gpu_safe_mode_persistence.py

⚠️ 需要先有打包产物 ``frontend-next/dist-electron/win-unpacked/qmt_work.exe``。
⚠️ 本用例会**真的启动三次客户端**（约 1~2 分钟），且只使用自己的隔离 profile，
   不碰用户的 userData（见 pids_of_profile 的说明）。
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXE = ROOT / "frontend-next" / "dist-electron" / "win-unpacked" / "qmt_work.exe"
OUT = ROOT / "output" / "gpu_verdict_test"
PROFILE = OUT / "profile"
VERDICT = PROFILE / "gpu-verdict.json"
PORT_FILE = PROFILE / "port.txt"
LOG = OUT / "client.log"

# 复用自检脚本里已验证过的工具函数，避免两份实现漂移
_spec = importlib.util.spec_from_file_location(
    "cst", str(ROOT / "scripts" / "client_start_test.py"))
cst = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cst)

RESULTS: list[tuple[bool, str, str]] = []


def record(ok: bool, name: str, detail: str = "") -> None:
    RESULTS.append((ok, name, detail))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def read_verdict() -> dict | None:
    try:
        v = json.loads(VERDICT.read_text(encoding="utf-8"))
        return v if isinstance(v, dict) else None
    except Exception:  # noqa: BLE001
        return None


def kill_own() -> None:
    for pid in cst.pids_of_profile("qmt_work.exe", PROFILE):
        cst.kill_tree(pid)
    time.sleep(1.5)


def http_alive(port: int, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/v1/live",
                                    timeout=timeout) as resp:
            return resp.status == 200
    except urllib.error.HTTPError:
        return True  # 有 HTTP 响应就算监听成功
    except Exception:  # noqa: BLE001
        return False


def launch() -> subprocess.Popen:
    """起一次客户端（正常模式 + 免模态框）。

    ⚠️ 刻意**不设** ``QMT_CLIENT_TEST_MODE`` / ``QMT_CLIENT_SAFE_MODE``：
       前者会顺带强制安全模式，后者直接进安全模式 —— 那样就永远复现不出
       「正常模式崩一次」这条路径。只用 ``QMT_CLIENT_NO_MODAL`` 免掉阻塞对话框
       （崩溃收尾那个 showMessageBoxSync 在非交互会话里会一直挂着）。
    """
    try:
        PORT_FILE.unlink()
    except FileNotFoundError:
        pass
    env = cst.clean_env({"QMT_CLIENT_NO_MODAL": "1"})
    argv = [str(EXE), f"--user-data-dir={PROFILE}"]
    log_file = open(LOG, "wb")  # noqa: SIM115 (句柄随进程生命周期)
    return subprocess.Popen(argv, cwd=str(EXE.parent), env=env, stdout=log_file,
                            stderr=subprocess.STDOUT, creationflags=0x00000200)


def wait_ready(proc: subprocess.Popen, seconds: float) -> int | None:
    """等后端就绪，返回实际端口；未就绪返回 None。

    端口从**隔离 profile** 的 port.txt 读（后端端口被占用时会自动 +1，
    因此不能假设就是 21118），并要求文件是本轮新写的。
    """
    deadline = time.time() + seconds
    while time.time() < deadline:
        if proc.poll() is not None:
            return None  # 进程已退出 ⇒ 没起来
        try:
            port = int(PORT_FILE.read_text(encoding="utf-8").strip())
        except Exception:  # noqa: BLE001
            port = 0
        if port > 0 and http_alive(port):
            return port
        time.sleep(1.0)
    return None


def main() -> int:
    print("=== 打包态 GPU 自愈回归（隔次启动失败）===")
    print(f"    产物: {EXE}")
    print(f"    隔离 profile: {PROFILE}")
    if not EXE.exists():
        record(False, "打包产物存在", f"缺少 {EXE}")
        return 1
    record(True, "打包产物存在", str(EXE))

    kill_own()
    OUT.mkdir(parents=True, exist_ok=True)
    PROFILE.mkdir(parents=True, exist_ok=True)
    # 从「无判定」开始：确保第 1 次走正常模式
    try:
        VERDICT.unlink()
    except FileNotFoundError:
        pass
    record(read_verdict() is None, "起点：无 GPU 判定（第 1 次必然走正常模式）")

    # ---------------- 第 1 次：正常模式，本机无 GPU 应当崩 ----------------
    print("\n[1/3] 正常模式启动（预期：无 GPU 则崩，并写 ok=false）")
    p1 = launch()
    port1 = wait_ready(p1, 45)
    if port1 is not None:
        # 本机 GPU 正常 —— 整个场景不适用
        record(True, "本机 GPU 正常，跳过隔次启动场景",
               f"第 1 次即就绪于 {port1}，判定={read_verdict()}")
        kill_own()
        return _finish(skipped=True)
    v1 = read_verdict()
    record(v1 is not None and v1.get("ok") is False,
           "第 1 次崩后写入 ok=false", json.dumps(v1, ensure_ascii=False))
    if not (v1 and v1.get("ok") is False):
        kill_own()
        record(False, "前置条件：本机无 GPU 且判定落盘", "拿不到 ok=false，无法继续")
        return _finish()

    # ---------------- 第 2 次：安全模式必须起来，且判定不能被改写 ----------------
    print("\n[2/3] 安全模式启动（预期：起来；★ 判定必须仍是 false）")
    p2 = launch()
    port2 = wait_ready(p2, 75)
    record(port2 is not None, "第 2 次（安全模式）成功就绪", f"port={port2}")
    # ★ 核心：跨过 GPU_CRASH_WINDOW_MS(12s) + 500ms 之后再看判定文件
    print("    等待 15s 跨过崩溃确认窗口，再复查判定文件 …")
    time.sleep(15)
    v2 = read_verdict()
    record(v2 is not None and v2.get("ok") is False,
           "★ 安全模式**不得**把判定改写成 ok=true（隔次启动失败的根因）",
           json.dumps(v2, ensure_ascii=False))
    kill_own()

    # ---------------- 第 3 次：仍进安全模式，必须起来 ----------------
    print("\n[3/3] 再次启动（预期：仍进安全模式并起来 —— 用户实际体验的那一次）")
    p3 = launch()
    port3 = wait_ready(p3, 75)
    record(port3 is not None, "第 3 次成功就绪（不再隔次崩）", f"port={port3}")
    kill_own()

    return _finish()


def _finish(skipped: bool = False) -> int:
    failed = [r for r in RESULTS if not r[0]]
    print("\n" + "=" * 68)
    if skipped:
        print(f"结果: SKIP（本机 GPU 正常，不适用）  通过 {len(RESULTS) - len(failed)}/{len(RESULTS)}")
    else:
        print(f"结果: {len(RESULTS) - len(failed)}/{len(RESULTS)} 通过")
    for ok, name, detail in failed:
        print(f"  - {name}  {detail}")
    print(f"日志: {LOG}")
    print("=" * 68)
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        # 无论成功失败都清掉自己的进程，别留残留
        for pid in cst.pids_of_profile("qmt_work.exe", PROFILE):
            cst.kill_tree(pid)

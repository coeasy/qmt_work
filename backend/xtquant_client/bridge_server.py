"""桥接子进程服务端（在匹配 ABI 的嵌入式 Python 中运行）。

由 BridgeAdapter 通过 `python -m xtquant_client.bridge_server --adapter xtp --config <json>`
拉起。协议（行分隔 JSON，stdin 请求 / stdout 响应）：

- 请求: {"id": int, "method": str, "args": [...]}
- 响应: {"id": int, "ok": bool, "result": ..., "error": str, "error_type": str}
- 事件: {"event": "quote", "data": {...}}            (无 id；订阅后由回调推送)
- 启动失败: {"event": "init_error", "error": str, "error_type": str}

特殊方法：
- _ping       : 握手，返回 {"alive": true, "connected": adapter.is_connected()}
- _subscribe_quote : 注册内部回调推送 quote 事件，立即返回 ok
- _shutdown   : 关闭适配器并退出循环

其余方法一律通过 getattr(adapter, method)(*args) 反射调用，因此服务端无需枚举每个方法。
"""
import argparse
import json
import os
import sys
import threading
import time
import traceback
from typing import TextIO

from xtquant_client.base import BrokerNotConnectedError

# stdout 写锁：QMT 回调线程（_on_quote）、状态泵线程与请求处理主线程都会写 stdout，
# 多线程同时 write 同一管道可能写穿（半个 JSON 行），导致对端 json 解析失败。
_WRITE_LOCK = threading.Lock()


def _parent_alive(pid: int | None) -> bool:
    """检查父进程是否仍存活（纯标准库，兼容任意嵌入式 Python）。

    Windows 用 OpenProcess + GetExitCodeProcess（STILL_ACTIVE=259 表示存活）；
    POSIX 用 os.kill(pid, 0) 探测。

    注意：OpenProcess 打开失败**必须区分**「进程不存在」（ERROR_INVALID_PARAMETER
    87 / ERROR_INVALID_HANDLE 6）与「存在但权限不足」（ERROR_ACCESS_DENIED 5）。
    后者（如父进程为提升权限运行的打包 EXE）必须视为存活，否则看护线程会误判
    父进程死亡而自杀——这是「桥接子进程握手成功后退出」的高危根因。
    """
    if not pid or pid <= 0:
        return True  # 未指定父进程则不约束
    if os.name == "nt":
        import ctypes
        try:
            _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            _STILL_ACTIVE = 259
            _OpenProcess = ctypes.windll.kernel32.OpenProcess
            _OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_bool, ctypes.c_uint32]
            _OpenProcess.restype = ctypes.c_void_p
            h = _OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not h:
                err = ctypes.get_last_error()
                # 87=进程不存在 / 6=句柄无效 / 128=线程不存在：父进程确实已死
                if err in (6, 87, 128):
                    return False
                # 其余（含 5=权限不足）保守视为存活，绝不误杀
                return True
            try:
                code = ctypes.c_ulong()
                ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
                return code.value == _STILL_ACTIVE
            finally:
                ctypes.windll.kernel32.CloseHandle(h)
        except Exception:  # noqa: BLE001
            return True  # 探测失败不误杀
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 存在但无权限 -> 存活
    except OSError:
        return False
    except Exception:  # noqa: BLE001
        return True


def _watch_parent(pid: int | None, interval: float = 3.0, grace: float = 8.0) -> None:
    """看护线程：父进程退出后，本桥接子进程自动退出，杜绝孤儿残留。

    宽限期（grace）：启动后前 grace 秒不检查，避免与父端握手/初始化竞态
    （若握手阶段误判父死并自杀，会表现为「握手成功后子进程退出」）。
    """
    if not pid or pid <= 0:
        return

    def _loop():
        time.sleep(grace)  # 启动宽限：先让父端完成握手与初始化
        while True:
            if not _parent_alive(pid):
                # 父进程已死：优雅关闭适配器后自杀
                try:
                    sys.stderr.write(f"[bridge] parent {pid} gone, exit\n")
                    sys.stderr.flush()
                except Exception:  # noqa: BLE001
                    pass
                os._exit(0)
            time.sleep(interval)

    threading.Thread(target=_loop, daemon=True).start()


def _err_type(e: Exception) -> str:
    return type(e).__name__


def _safe_err(exc: BaseException) -> str:
    """异常文案规范化（防「None」失明）。

    xtquant SDK 在部分失败路径上抛出 args=(None,) 或无参异常，`str(exc)` 得到
    "None" / ""。这个字符串原样透传到父端后会变成「桥接子进程握手失败：None」，
    用户与日志同时失明（真实根因被吞掉）。此处退化为「异常类型 + args」，
    至少保证可定位；父端再据 error_type 补可操作指引。
    """
    try:
        msg = str(exc)
    except Exception:  # noqa: BLE001
        msg = ""
    if msg.strip().lower() in ("", "none", "null", "nonetype"):
        try:
            args = exc.args if exc.args else ()
        except Exception:  # noqa: BLE001
            args = ()
        return f"{type(exc).__name__}{args if args else '（SDK 未提供错误详情）'}"
    return msg


def _write(out: TextIO, obj: dict) -> None:
    # 见 _WRITE_LOCK 注释：跨线程写 stdout 必须加锁防写穿
    with _WRITE_LOCK:
        try:
            out.write(json.dumps(obj, ensure_ascii=False) + "\n")
            out.flush()
        except Exception:  # noqa: BLE001
            pass


def _start_status_pump(adapter, stdout, state, interval: float = 2.0) -> None:
    """状态泵线程：周期性轮询 adapter.is_connected()，状态翻转时主动推送 conn_state。

    让桥接客户端能感知 SDK 真实连接态（而非仅握手时刻快照）：QMT 客户端关闭 /
    重新登录后，服务端在此检测到 is_connected 翻转并立即推送，客户端 is_connected()
    随即返回 False，健康监控据此触发重连（不再卡在陈旧的缓存标志上）。
    """

    def _loop():
        while True:
            try:
                cur = bool(adapter.is_connected())
            except Exception:  # noqa: BLE001
                cur = False
            if cur != state.get("connected"):
                state["connected"] = cur
                _write(stdout, {"event": "conn_state", "data": {"connected": cur}})
            time.sleep(interval)

    threading.Thread(target=_loop, daemon=True).start()


def serve_adapter(adapter, stdin=None, stdout=None,
                  status_interval: float = 2.0) -> int:
    """在给定（文本）流上服务某个适配器，直到收到 _shutdown 或流关闭。返回退出码。"""
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout

    # 共享连接态：状态泵线程与请求处理线程共同维护；变化时推送 conn_state 事件
    state = {"connected": False}

    # ---- 启动（可能抛 BrokerError / DLL 不兼容）----
    try:
        adapter.start()
        state["connected"] = bool(adapter.is_connected())
    except Exception as exc:  # noqa: BLE001
        # 启动失败是最需要可观测性的路径：除规范化文案外，必须把 traceback
        # 同时（a）放入事件供父端记日志、（b）写 stderr 供父端 _stderr_tail 读取。
        # 此前两者都缺失，父端只能看到「None」，真实根因彻底丢失。
        tb = traceback.format_exc()
        _write(stdout, {"event": "init_error", "error": _safe_err(exc),
                        "error_type": _err_type(exc), "traceback": tb[-2000:]})
        try:
            stderr = sys.stderr
            if stderr is not None:
                stderr.write(f"[bridge_server] adapter.start() 失败: "
                             f"{_err_type(exc)}: {_safe_err(exc)}\n{tb}")
                stderr.flush()
        except Exception:  # noqa: BLE001
            pass
        # 仍进入读取循环，使父端 _ping 能收到响应（再带上 init_error 已记录）
        log_init = _safe_err(exc)
    else:
        log_init = None

    # SDK 连接态变化时主动推送（QMT 客户端关闭/重登即时感知，不等健康轮询）
    _start_status_pump(adapter, stdout, state, status_interval)

    def _on_quote(evt):
        _write(stdout, {"event": "quote", "data": evt})

    def _mark_connected(value: bool) -> None:
        """更新共享连接态；变化时立即推送 conn_state（客户端 is_connected 实时翻转）。"""
        if state.get("connected") != bool(value):
            state["connected"] = bool(value)
            _write(stdout, {"event": "conn_state",
                            "data": {"connected": bool(value)}})

    def _dispatch(req):
        rid = req.get("id")
        method = req.get("method", "")
        params = req.get("args", []) or []
        if method == "_ping":
            cur = bool(adapter.is_connected())
            _mark_connected(cur)
            _write(stdout, {"id": rid, "ok": True,
                            "result": {"alive": True, "connected": cur}})
            return
        if method == "_shutdown":
            try:
                adapter.close()
            except Exception:  # noqa: BLE001
                pass
            _write(stdout, {"id": rid, "ok": True, "result": None})
            return "stop"
        if method == "_subscribe_quote":
            try:
                codes = params[0] if params else []
                adapter.subscribe_quote(list(codes), _on_quote)
                _write(stdout, {"id": rid, "ok": True, "result": {"subscribed": len(codes)}})
            except Exception as exc:  # noqa: BLE001
                _write(stdout, {"id": rid, "ok": False, "error": _safe_err(exc),
                                "error_type": _err_type(exc)})
            return
        fn = getattr(adapter, method, None)
        if fn is None or not callable(fn):
            _write(stdout, {"id": rid, "ok": False, "error": f"未知方法: {method}"})
            return
        try:
            result = fn(*params)
            _write(stdout, {"id": rid, "ok": True, "result": result})
        except BrokerNotConnectedError as exc:
            # 真实查询失败是断开强信号：立即把状态翻为断开并推送。
            # 即使 adapter.is_connected() 返回的是陈旧缓存标志（如 XTP 启动后不再
            # 刷新），客户端也能据此把 is_connected() 翻为 False，触发健康重连。
            _mark_connected(False)
            _write(stdout, {"id": rid, "ok": False, "error": _safe_err(exc),
                            "error_type": _err_type(exc)})
        except Exception as exc:  # noqa: BLE001
            _write(stdout, {"id": rid, "ok": False, "error": _safe_err(exc),
                            "error_type": _err_type(exc)})

    if log_init:
        # 启动已失败：仅保持最小读取循环以便父端探测到 init_error
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            stop = _dispatch(req)
            if stop == "stop":
                break
        return 1

    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        try:
            stop = _dispatch(req)
        except Exception:  # noqa: BLE001
            stop = None
        if stop == "stop":
            break
    try:
        adapter.close()
    except Exception:  # noqa: BLE001
        pass
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="qmt_work xtquant bridge server")
    # 券商档案 id（guojin/huaxin/...），用于 create_adapter 实例化对应适配器。
    # --adapter 保留作兼容别名（旧测试/旧客户端传的是券商 id 而非适配器类型）。
    parser.add_argument("--broker", default="",
                        help="券商档案 id（registry 中的键，如 guojin/huaxin）")
    parser.add_argument("--adapter", default="",
                        help="兼容别名：与 --broker 等价（已废弃）")
    parser.add_argument("--config", default="{}", help="适配器构造参数 JSON")
    parser.add_argument("--parent-pid", type=int, default=0,
                        help="父进程 PID：父进程退出后本进程自动退出（防孤儿残留）")
    args = parser.parse_args()
    _watch_parent(args.parent_pid)
    cfg = json.loads(args.config)
    from xtquant_client.registry import create_adapter
    broker_id = args.broker or args.adapter
    if not broker_id:
        raise SystemExit("缺少 --broker（券商档案 id）")
    adapter = create_adapter(
        broker_id,
        cfg.get("client_path", ""),
        cfg.get("account_id", ""),
        cfg.get("account_type", "STOCK"),
        int(cfg.get("session_id", 0) or 0),
        cfg.get("min_version", ""),
    )
    return serve_adapter(adapter)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(0)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        raise SystemExit(2)

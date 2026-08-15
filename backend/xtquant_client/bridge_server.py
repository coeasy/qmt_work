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
import traceback
from typing import TextIO

from xtquant_client.base import BrokerError


def _parent_alive(pid: int | None) -> bool:
    """检查父进程是否仍存活（纯标准库，兼容任意嵌入式 Python）。

    Windows 用 GetExitCodeProcess（STILL_ACTIVE=259 表示存活，进程句柄不要求权限）；
    POSIX 用 os.kill(pid, 0) 探测。
    """
    if not pid or pid <= 0:
        return True  # 未指定父进程则不约束
    if os.name == "nt":
        import ctypes
        try:
            _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            _STILL_ACTIVE = 259
            h = ctypes.windll.kernel32.OpenProcess(
                _PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not h:
                return False  # 打不开句柄 = 进程已退出
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
    except OSError:
        return False
    except Exception:  # noqa: BLE001
        return True


def _watch_parent(pid: int | None, interval: float = 3.0) -> None:
    """看护线程：父进程退出后，本桥接子进程自动退出，杜绝孤儿残留。"""
    if not pid or pid <= 0:
        return

    def _loop():
        while True:
            if not _parent_alive(pid):
                # 父进程已死：优雅关闭适配器后自杀
                try:
                    sys.stderr.write(f"[bridge] parent {pid} gone, exit\n")
                    sys.stderr.flush()
                except Exception:  # noqa: BLE001
                    pass
                os._exit(0)
            threading.Event().wait(interval)

    threading.Thread(target=_loop, daemon=True).start()


def _err_type(e: Exception) -> str:
    return type(e).__name__


def _write(out: TextIO, obj: dict) -> None:
    try:
        out.write(json.dumps(obj, ensure_ascii=False) + "\n")
        out.flush()
    except Exception:  # noqa: BLE001
        pass


def serve_adapter(adapter, stdin=None, stdout=None) -> int:
    """在给定（文本）流上服务某个适配器，直到收到 _shutdown 或流关闭。返回退出码。"""
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout

    # ---- 启动（可能抛 BrokerError / DLL 不兼容）----
    try:
        adapter.start()
    except Exception as exc:  # noqa: BLE001
        _write(stdout, {"event": "init_error", "error": str(exc),
                        "error_type": _err_type(exc)})
        # 仍进入读取循环，使父端 _ping 能收到响应（再带上 init_error 已记录）
        log_init = str(exc)
    else:
        log_init = None

    def _on_quote(evt):
        _write(stdout, {"event": "quote", "data": evt})

    def _dispatch(req):
        rid = req.get("id")
        method = req.get("method", "")
        params = req.get("args", []) or []
        if method == "_ping":
            _write(stdout, {"id": rid, "ok": True,
                            "result": {"alive": True, "connected": adapter.is_connected()}})
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
                _write(stdout, {"id": rid, "ok": False, "error": str(exc),
                                "error_type": _err_type(exc)})
            return
        fn = getattr(adapter, method, None)
        if fn is None or not callable(fn):
            _write(stdout, {"id": rid, "ok": False, "error": f"未知方法: {method}"})
            return
        try:
            result = fn(*params)
            _write(stdout, {"id": rid, "ok": True, "result": result})
        except Exception as exc:  # noqa: BLE001
            _write(stdout, {"id": rid, "ok": False, "error": str(exc),
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
    parser.add_argument("--adapter", default="xtp", help="适配器 id（registry 中的键）")
    parser.add_argument("--config", default="{}", help="适配器构造参数 JSON")
    parser.add_argument("--parent-pid", type=int, default=0,
                        help="父进程 PID：父进程退出后本进程自动退出（防孤儿残留）")
    args = parser.parse_args()
    _watch_parent(args.parent_pid)
    cfg = json.loads(args.config)
    from xtquant_client.registry import create_adapter
    adapter = create_adapter(
        args.adapter,
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

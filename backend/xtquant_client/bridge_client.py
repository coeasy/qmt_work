"""桥接客户端：在主后端（任意 Python）侧代理一个运行在匹配 ABI 子进程里的适配器。

通过行分隔 JSON-RPC 与 `bridge_server` 通信，完全实现 BrokerAdapter 接口；
主后端无需自身 Python 与券商 xtquant ABI 兼容（根治 3.13 打包无法加载 cp311 的问题）。

设计要点：
- 启动：按 runtime 方案选一个「ABI 匹配」的 python.exe，拉起
  `python -m xtquant_client.bridge_server`；通过 _ping 握手确认子进程就绪。
- 调用：每个方法 -> 一条 JSON 请求，future 等待响应；子进程异常按 error_type 重建。
- 事件：子进程推送的 {"event":"quote"} 行 -> 转发给本地注册的 on_tick 回调
  （与 sync 引擎 `b.gateway.subscribe_quote(codes, lambda evt: b.enqueue(evt))` 衔接）。
- 关闭：发送 _shutdown，终止子进程，清理线程。
"""
import json
import os
import subprocess
import sys
import threading
from concurrent.futures import Future

from .base import (BrokerAdapter, BrokerError, BrokerNotConnectedError,
                   BrokerSDKError)
from .runtime import select_runtime, require_runtime_or_raise

# 子进程异常类型名 -> 客户端重建的异常类
_ERR_MAP = {
    "BrokerNotConnectedError": BrokerNotConnectedError,
    "BrokerSDKError": BrokerSDKError,
    "BrokerError": BrokerError,
}

# 后端目录（含 xtquant_client 包），用于让嵌入式子进程找到本模块
_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class BridgeAdapter(BrokerAdapter):
    """在子进程解释器（ABI 匹配）中托管券商适配器，本端做 JSON-RPC 代理。"""

    def __init__(self, client_path: str, account_id: str, account_type: str = "STOCK",
                 session_id: int = 0, min_version: str = "", adapter: str = "xtp",
                 python_exe: str | None = None,
                 server_module: str = "xtquant_client.bridge_server",
                 prefer_bridge: bool = False, runtime: dict | None = None,
                 backend_dir: str | None = None):
        self.client_path = client_path
        self._account_id = account_id
        self._account_type = (account_type or "STOCK").upper()
        self.session_id = int(session_id or 0)
        self.min_version = min_version
        self._adapter_id = adapter
        self._server_module = server_module
        self._python_exe = python_exe
        self._prefer_bridge = prefer_bridge
        self._runtime = runtime
        self._backend_dir = backend_dir or _BACKEND_DIR
        self._proc = None
        self._lock = threading.Lock()
        self._req_id = 0
        self._pending: dict[int, Future] = {}
        self._reader_thread = None
        self._stderr_thread = None
        self._quote_handlers: list = []
        self._connected = False
        self._init_error: str | None = None
        self._stderr_buf: list[str] = []

    # ---------------- 身份 ----------------
    @property
    def broker_name(self) -> str:
        return "迅投XTQuant(bridge)"

    @property
    def adapter_id(self) -> str:
        return self._adapter_id

    @property
    def client_version(self) -> str:
        return "xtquant(bridge)"

    @property
    def account_id(self) -> str:
        return self._account_id

    @property
    def account_type(self) -> str:
        return self._account_type

    @property
    def supported_periods(self) -> list[str]:
        return ["1m", "5m", "15m", "30m", "60m", "1d", "1w", "1mon"]

    @property
    def supported_account_types(self) -> list[str]:
        return ["STOCK", "CREDIT", "OPTION", "FUTURES"]

    @property
    def sdk_required(self) -> str:
        return "xtquant"

    # ---------------- 生命周期 ----------------
    def _xtquant_site(self):
        try:
            from .xtp import _resolve_xtquant_path
            return _resolve_xtquant_path(self.client_path)
        except Exception:  # noqa: BLE001
            return None

    def start(self) -> None:
        runtime = self._runtime
        if runtime is None:
            runtime = select_runtime(self._xtquant_site(),
                                     prefer_bridge=self._prefer_bridge)
            if runtime is None:
                runtime = require_runtime_or_raise(
                    self._xtquant_site(), prefer_bridge=self._prefer_bridge)
        exe = self._python_exe or runtime["python_exe"]
        cfg = {"client_path": self.client_path, "account_id": self._account_id,
               "account_type": self._account_type, "session_id": self.session_id,
               "min_version": self.min_version}
        cmd = [exe, "-m", self._server_module, "--adapter", self._adapter_id,
               "--config", json.dumps(cfg), "--parent-pid", str(os.getppid())]
        env = dict(os.environ)
        # 让嵌入式子进程能 import xtquant_client 包（纯 Python，无需额外依赖）
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = (self._backend_dir + os.pathsep + existing).rstrip(os.pathsep)
        # 子进程以独立进程组运行，避免被父进程信号误伤
        try:
            self._proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, env=env, text=True, bufsize=1,
                creationflags=0)
        except Exception as exc:  # noqa: BLE001
            raise BrokerNotConnectedError(f"桥接子进程启动失败：{exc}") from exc

        self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._reader_thread.start()
        self._stderr_thread = threading.Thread(target=self._stderr_loop, daemon=True)
        self._stderr_thread.start()

        # 握手：_ping 确认子进程 IPC 就绪；超时则清理并抛错（附 stderr 便于定位）
        try:
            self._rpc("_ping", [], timeout=30.0)
        except Exception as exc:  # noqa: BLE001
            err = self._stderr_tail()
            self.close()
            tail = f"（子进程 stderr: {err}）" if err else ""
            raise BrokerNotConnectedError(f"桥接子进程握手失败：{exc}{tail}") from exc
        if self._init_error:
            err = self._init_error
            self.close()
            raise BrokerNotConnectedError(f"桥接子进程初始化失败：{err}")
        try:
            self._connected = bool(self._rpc("is_connected", [], timeout=10.0) or False)
        except Exception:  # noqa: BLE001
            self._connected = False

    def _stderr_loop(self):
        try:
            for line in self._proc.stderr:
                self._stderr_buf.append(line.rstrip("\n")[-200:])
                if len(self._stderr_buf) > 50:
                    self._stderr_buf.pop(0)
        except Exception:  # noqa: BLE001
            pass

    def _stderr_tail(self, n: int = 8) -> str:
        """返回子进程 stderr 最近 n 行（便于失败时透出真实原因）。"""
        try:
            return " | ".join(self._stderr_buf[-n:])[:500]
        except Exception:  # noqa: BLE001
            return ""

    def _read_loop(self):
        try:
            for line in self._proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                if "event" in msg:
                    self._on_event(msg.get("event"), msg.get("data"))
                    continue
                rid = msg.get("id")
                fut = self._pending.get(rid)
                if fut is not None and not fut.done():
                    fut.set_result(msg)
        except Exception:  # noqa: BLE001
            pass
        finally:
            # 子进程退出：唤醒所有挂起调用为失败
            for f in list(self._pending.values()):
                if not f.done():
                    f.set_exception(BrokerNotConnectedError("桥接子进程已退出"))

    def _on_event(self, event, data):
        if event == "init_error":
            self._init_error = data.get("error") if isinstance(data, dict) else str(data)
            # 唤醒所有挂起调用（握手会因此拿到 init_error）
            for f in list(self._pending.values()):
                if not f.done():
                    f.set_exception(BrokerNotConnectedError(self._init_error))
        elif event == "quote":
            for h in self._quote_handlers:
                try:
                    h(data)
                except Exception:  # noqa: BLE001
                    pass

    def _rpc(self, method: str, args, timeout: float = 30.0):
        if self._proc is None or self._proc.poll() is not None:
            raise BrokerNotConnectedError("桥接子进程未运行")
        with self._lock:
            self._req_id += 1
            rid = self._req_id
            fut: Future = Future()
            self._pending[rid] = fut
            try:
                self._proc.stdin.write(
                    json.dumps({"id": rid, "method": method, "args": list(args)}) + "\n")
                self._proc.stdin.flush()
            except Exception as exc:  # noqa: BLE001
                self._pending.pop(rid, None)
                raise BrokerNotConnectedError(f"桥接写入失败：{exc}") from exc
        try:
            res = fut.result(timeout=timeout)
        finally:
            self._pending.pop(rid, None)
        if not res.get("ok"):
            etype = res.get("error_type")
            emsg = res.get("error", "桥接调用失败")
            raise _ERR_MAP.get(etype, BrokerError)(emsg)
        return res.get("result")

    # ---------------- 行情 ----------------
    def get_quote(self, code: str) -> dict:
        return self._rpc("get_quote", [code])

    def get_full_tick(self, codes: list[str]) -> dict:
        return self._rpc("get_full_tick", [list(codes)])

    def get_kline(self, code: str, period: str, count: int,
                  start: str = "", end: str = "") -> list[dict]:
        return self._rpc("get_kline", [code, period, count, start, end])

    def get_tick(self, code: str) -> dict:
        return self._rpc("get_tick", [code])

    def get_stock_list(self, sector: str = "沪深A股") -> list[dict]:
        return self._rpc("get_stock_list", [sector])

    def search_stocks(self, keyword: str, limit: int = 20) -> list[dict]:
        return self._rpc("search_stocks", [keyword, limit])

    def subscribe_quote(self, codes: list[str], on_tick) -> None:
        if on_tick is not None and on_tick not in self._quote_handlers:
            self._quote_handlers.append(on_tick)
        self._rpc("_subscribe_quote", [list(codes)], timeout=15.0)

    # ---------------- 账户 ----------------
    def get_account(self) -> dict:
        return self._rpc("get_account", [])

    def get_positions(self, symbol: str | None = None) -> list[dict]:
        return self._rpc("get_positions", [symbol])

    def get_cash(self) -> dict:
        return self._rpc("get_cash", [])

    def get_orders(self) -> list[dict]:
        return self._rpc("get_orders", [])

    def get_deals(self) -> list[dict]:
        return self._rpc("get_deals", [])

    # ---------------- 交易 ----------------
    def place_order(self, code: str, direction: str, price_type: str,
                    price: float, volume: int, strategy_name: str = "",
                    remark: str = "") -> dict:
        return self._rpc("place_order", [code, direction, price_type, price,
                                          volume, strategy_name, remark])

    def cancel_order(self, order_id: str) -> dict:
        return self._rpc("cancel_order", [order_id])

    # ---------------- 参考数据 / L2 ----------------
    def get_sector_list(self) -> list[str]:
        return self._rpc("get_sector_list")

    def get_sector_stocks(self, sector: str = "沪深A股") -> list[str]:
        return self._rpc("get_sector_stocks", [sector])

    def get_trading_calendar(self, start: str = "", end: str = "") -> list[str]:
        return self._rpc("get_trading_calendar", [start, end])

    def get_financial(self, code: str) -> dict:
        return self._rpc("get_financial", [code])

    def get_l2_transactions(self, code: str, count: int = 100) -> list[dict]:
        return self._rpc("get_l2_transactions", [code, count])

    # ---------------- 状态 ----------------
    def is_connected(self) -> bool:
        return self._connected

    def close(self) -> None:
        self._connected = False
        if self._proc is None:
            return
        try:
            if self._proc.poll() is None:
                try:
                    self._proc.stdin.write(json.dumps({"id": -1, "method": "_shutdown", "args": []}) + "\n")
                    self._proc.stdin.flush()
                except Exception:  # noqa: BLE001
                    pass
                try:
                    self._proc.wait(timeout=5.0)
                except Exception:  # noqa: BLE001
                    self._proc.kill()
        except Exception:  # noqa: BLE001
            pass
        finally:
            # 唤醒残留调用
            for f in list(self._pending.values()):
                if not f.done():
                    f.set_exception(BrokerNotConnectedError("桥接已关闭"))
            # 主动关闭管道，避免子进程退出后 TextIOWrapper 在 GC 时 flush 报错（Windows OSError 22）
            for s in (self._proc.stdin, self._proc.stdout, self._proc.stderr):
                try:
                    s.close()
                except Exception:  # noqa: BLE001
                    pass
            self._proc = None

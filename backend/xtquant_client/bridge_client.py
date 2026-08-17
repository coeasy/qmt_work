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
import logging
import os
import queue as _queue
import subprocess
import sys
import threading
from concurrent.futures import Future, TimeoutError as _FutTimeout

from .base import (BrokerAdapter, BrokerError, BrokerNotConnectedError,
                   BrokerSDKError)
from .runtime import select_runtime, require_runtime_or_raise

log = logging.getLogger("qmt_work")

# 握手超时：从 90s 收紧到 30s。
# 设计依据：QMT 客户端已登录时 _ping < 5s 即可返回；未登录时 SDK 在 10~30s 内
# 必然阻塞/异常。30s 既覆盖慢机器冷启动，也能在用户感知层「点了就连不上」
# 时及时给反馈（原 90s 期间按钮 spinner 死转、无任何提示，被反馈为「无法点击」）。
# 阶段 0-D（C6）：超时后必须强制 kill 子进程，否则 SDK 在进程内死锁，
# 下次 start 复用旧 proc 同样卡住——是「连接按钮点很多次都不通」的根因之一。
_HANDSHAKE_TIMEOUT = 30.0

# Windows：spawn 桥接子进程（python.exe，控制台子系统）时隐藏其控制台窗口，
# 避免桌面运行时券商连接/重连时弹出黑窗。其他平台该值为 0（无副作用）。
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

# 子进程异常类型名 -> 客户端重建的异常类
_ERR_MAP = {
    "BrokerNotConnectedError": BrokerNotConnectedError,
    "BrokerSDKError": BrokerSDKError,
    "BrokerError": BrokerError,
}

# 后端目录（含 xtquant_client 包），用于让嵌入式子进程找到本模块
_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 「空洞错误」集合：SDK 抛空异常时 str() 的各种退化形态。
# 注意必须包含字符串 "none"——JSON 里 None 与字符串 "None" 都会出现，
# 后者是 truthy，只做 falsy 判断会漏。
_VOID_ERRS = {"", "none", "null", "nonetype", "()", "nonetype()"}


def _humanize_init_error(raw, error_type: str = "") -> str:
    """把子进程 init_error 规范化为「用户可直接照做」的文案。

    设计取舍：xtquant 是黑盒，穷举它每个抛空异常的分支不可行，因此在**边界处**
    统一做防御——只要拿不到有效文案，就给出按概率排序的可操作原因清单，
    而不是把 "None" 抛给用户。
    """
    msg = ("" if raw is None else str(raw)).strip()
    if msg.lower() in _VOID_ERRS:
        base = "券商 SDK 启动失败，但未返回错误详情"
        if error_type:
            base += f"（异常类型：{error_type}）"
        return (base + "。请按以下顺序排查：\n"
                "1) 打开并登录 QMT 客户端（极速/普通模式均可），保持客户端运行；\n"
                "2) 确认客户端行情/交易服务已启动（能正常看行情、下单）；\n"
                "3) 关闭重复打开的客户端或其他占用同一账号/session 的程序；\n"
                "4) 确认「客户端路径」指向 userdata_mini 目录。")
    return msg


class BridgeAdapter(BrokerAdapter):
    """在子进程解释器（ABI 匹配）中托管券商适配器，本端做 JSON-RPC 代理。"""

    def __init__(self, client_path: str, account_id: str, account_type: str = "STOCK",
                 session_id: int = 0, min_version: str = "", adapter: str = "xtp",
                 broker_id: str = "", python_exe: str | None = None,
                 server_module: str = "xtquant_client.bridge_server",
                 prefer_bridge: bool = False, runtime: dict | None = None,
                 backend_dir: str | None = None):
        self.client_path = client_path
        self._account_id = account_id
        self._account_type = (account_type or "STOCK").upper()
        self.session_id = int(session_id or 0)
        self.min_version = min_version
        self._adapter_id = adapter
        # 子进程需用「券商档案 id」（guojin/huaxin/...）调 create_adapter，
        # 而非适配器类型（xtp）——类型只决定实现类，档案 id 才可被 registry 识别。
        self._broker_id = broker_id or ("guojin" if adapter == "xtp" else adapter)
        self._server_module = server_module
        self._python_exe = python_exe
        self._prefer_bridge = prefer_bridge
        self._runtime = runtime
        self._backend_dir = backend_dir or _BACKEND_DIR
        self._proc = None
        self._lock = threading.Lock()
        # 阶段 0-D（C6）：start() 临界区锁——健康重连 / 手动 connect / 批量重连三路
        # 并发调用 start() 时，无双检锁会各自 Popen 双孤儿子进程、同账号双交易会话。
        self._start_lock = threading.Lock()
        self._req_id = 0
        self._pending: dict[int, Future] = {}
        self._reader_thread = None
        self._stderr_thread = None
        self._quote_handlers: list = []
        # 阶段 0-D（C5）：记录已订阅 codes，子进程重启后自动重新下发订阅（行情恢复）
        self._subscribed_codes: set[str] = set()
        # 阶段 1（C12）：quote handler 移出 reader 线程——独立有界事件队列 + 独立线程执行。
        # reader 线程只做「解析 + RPC 响应分发」，高并发行情下 handler 阻塞不再拖垮
        # 响应分发（此前 handler 在 reader 线程同步执行，会延迟 future 兑现/触发超时）。
        self._quote_q: _queue.Queue = _queue.Queue(maxsize=2000)
        self._quote_thread: threading.Thread | None = None
        self._quote_dropped = 0  # 有界队列满丢弃计数（C12 背压指标）
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
        # 幂等复用：子进程存活 且 SDK 仍连接 → 直接复用（避免重复拉起同账户多连接/泄漏）
        if self._proc is not None and self._proc.poll() is None and self._connected:
            return
        # 阶段 0-D（C6）：并发 start()（健康重连/手动 connect/批量重连三路）必须串行化，
        # 否则各自 Popen 双孤儿子进程、同一资金账号双交易会话。
        with self._start_lock:
            # 双检：等待锁期间另一线程可能已完成重启（保持幂等复用语义）
            if self._proc is not None and self._proc.poll() is None and self._connected:
                return
            self._start_inner()

    def _start_inner(self) -> None:
        # 上次残留（已退出 / SDK 已断开 / 未清理）：先彻底终止旧子进程，再干净重启。
        # 关键：SDK 断开（服务端已推 conn_state:false，_connected=False）时强制重启子进程
        # ——让新子进程重新执行 adapter.start()，清掉 SDK 内部可能卡死的状态
        #（如 QMT 客户端重登后的脏会话），否则旧子进程里陈旧的 _connected 会挡住自愈。
        if self._proc is not None:
            self._cleanup_proc()
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
        cmd = [exe, "-m", self._server_module, "--broker", self._broker_id,
               "--config", json.dumps(cfg),
               # 看护目标 = 主后端自身 PID（桥接子进程的直接父进程）：
               # 后端无论优雅退出还是被强杀（taskkill /F），子进程都能感知并自杀，
               # 避免「后端已死、父进程仍存活」时桥接子进程残留。
               "--parent-pid", str(os.getpid())]
        env = dict(os.environ)
        # 隔离宿主 Python 环境变量：宿主（桌面壳 / 开发 shell / CI）可能注入
        # PYTHONHOME / PYTHONUTF8 / PYTHONIOENCODING / PYTHONDONTWRITEBYTECODE /
        # 无关 PYTHONPATH 等，会干扰 embed 子进程（._pth 虽忽略 PYTHONPATH，
        # 但其他 PYTHON* 变量仍可能改变编码/路径行为）与系统 python 的模块解析。
        # 统一清掉后只保留必要的 PYTHONPATH=后端目录，彻底隔离宿主噪音。
        for _k in [k for k in env if k.startswith("PYTHON")]:
            env.pop(_k, None)
        env["PYTHONPATH"] = self._backend_dir
        # 关键：显式统一子进程 stdout 编码为 UTF-8（与主端读取一致）。
        # 若宿主是 UTF-8 模式（PYTHONUTF8=1）而子进程用 locale(cp936) 写中文，
        # 主端 UTF-8 解码会抛 UnicodeDecodeError -> 误判「桥接子进程已退出」。
        env["PYTHONIOENCODING"] = "utf-8"
        # 子进程以独立进程组运行，避免被父进程信号误伤
        try:
            self._proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, env=env, text=True, bufsize=1,
                encoding="utf-8", errors="replace",
                creationflags=CREATE_NO_WINDOW)
        except Exception as exc:  # noqa: BLE001
            raise BrokerNotConnectedError(f"桥接子进程启动失败：{exc}") from exc

        # 新子进程从头记录 stderr，避免旧子进程的错误信息串台
        self._stderr_buf = []
        self._reader_thread = threading.Thread(
            target=self._read_loop, args=(self._proc,), daemon=True)
        self._reader_thread.start()
        self._stderr_thread = threading.Thread(
            target=self._stderr_loop, args=(self._proc,), daemon=True)
        self._stderr_thread.start()
        # 阶段 1（C12）：独立 quote 处理线程（队列泵），与 reader 线程解耦
        self._quote_thread = threading.Thread(
            target=self._quote_loop, daemon=True)
        self._quote_thread.start()

        # 握手：_ping 确认子进程 IPC 就绪；超时则清理并抛错（附 stderr 便于定位）
        # 阶段 5：超时从 90s 收紧到 30s，与 _HANDSHAKE_TIMEOUT 一致；
        # 早期 90s 时用户看到「连接中」 spinner 30s+ 无反应，会误判为「点不动连接」。
        try:
            self._rpc("_ping", [], timeout=_HANDSHAKE_TIMEOUT)
        except Exception as exc:  # noqa: BLE001
            err = self._stderr_tail()
            # 握手超时几乎是「QMT 客户端未登录导致 SDK 阻塞」的专属信号：
            # XtQuantTrader.start() 在未登录时会一直阻塞等登录，子进程无法在
            # 窗口内响应 _ping。给出明确指引，避免用户误以为是平台 bug。
            hint = ""
            if isinstance(exc, TimeoutError) or "Timeout" in type(exc).__name__:
                hint = (f"（握手超时：子进程 {_HANDSHAKE_TIMEOUT:.0f}s 内无响应——最常见原因是 QMT 客户端"
                        f"未登录，导致 xtquant 交易连接 SDK 阻塞。请先登录客户端后重试）")
            # 失败必落日志（打包 EXE 黑盒下，qmt_work.log 是唯一诊断通道）
            try:
                log.error("bridge 握手失败: %s%s | 子进程 stderr: %s | proc rc=%s",
                          exc, hint, err, self._proc.poll() if self._proc else "N/A")
            except Exception:  # noqa: BLE001
                pass
            init_err = self._init_error  # close() 前先取，避免被清理
            self.close()
            # 若握手失败源自子进程 init_error，其文案已是「用户可照做」的指引：
            # 直接透出，不要再套「桥接子进程握手失败」这类内部术语前缀——
            # 用户读到术语只会误判成平台 bug，而不是去登录客户端。
            if init_err:
                raise BrokerNotConnectedError(init_err) from exc
            tail = f"（子进程 stderr: {err}）" if err else ""
            raise BrokerNotConnectedError(f"桥接子进程握手失败：{exc}{hint}{tail}") from exc
        if self._init_error:
            err = self._init_error
            self.close()
            raise BrokerNotConnectedError(f"桥接子进程初始化失败：{err}")
        try:
            self._connected = bool(self._rpc("is_connected", [], timeout=10.0) or False)
        except Exception:  # noqa: BLE001
            self._connected = False
        # 阶段 0-D（C5）订阅恢复：子进程（重新）启动后其订阅状态清零，
        # 若不清洗 _subscribed_codes 的 SyncEngine 会以为「已订阅」而永不重订，
        # 行情流静默死亡直到客户端退订再重订。这里在握手上成功后重新下发已订阅 codes，
        # 让断线重连 / SDK 断开强重启 / 手动 connect 后行情立即恢复。
        if self._connected and self._subscribed_codes:
            try:
                self._rpc("_subscribe_quote", [list(self._subscribed_codes)], timeout=15.0)
                log.info("bridge resubscribed %d code(s) after (re)start",
                         len(self._subscribed_codes))
                # 阶段 3：订阅恢复可观测——指标 qmt_conn_events_total{event="subscription_recovered"}
                try:
                    from gateway.metrics import get_metrics
                    get_metrics().record_conn_event("bridge", "subscription_recovered")
                except Exception:  # noqa: BLE001
                    pass
            except Exception as exc:  # noqa: BLE001
                log.warning("bridge resubscribe failed after start: %s", exc)

    def _stderr_loop(self, proc):
        try:
            for line in proc.stderr:
                self._stderr_buf.append(line.rstrip("\n")[-200:])
                if len(self._stderr_buf) > 50:
                    self._stderr_buf.pop(0)
        except Exception:  # noqa: BLE001
            pass

    def _quote_loop(self) -> None:
        """阶段 1（C12）：独立 quote 处理线程——从有界队列取事件并逐一派发 handler。

        reader 线程（_read_loop）只负责解析 + RPC 响应分发；行情事件经队列在此线程
        执行 handler，避免 handler 阻塞拖垮响应分发。线程退出条件：_cleanup_proc 把
        self._quote_thread 置 None，循环在 0.5s 超时内自然退出。
        """
        t = threading.current_thread()
        while self._quote_thread is t:
            try:
                evt = self._quote_q.get(timeout=0.5)
            except _queue.Empty:
                continue
            if evt is None:
                return
            for h in list(self._quote_handlers):
                try:
                    h(evt)
                except Exception:  # noqa: BLE001
                    pass

    def _stderr_tail(self, n: int = 8) -> str:
        """返回子进程 stderr 最近 n 行（便于失败时透出真实原因）。"""
        try:
            return " | ".join(self._stderr_buf[-n:])[:500]
        except Exception:  # noqa: BLE001
            return ""

    def _read_loop(self, proc):
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                if "event" in msg:
                    # 传整条消息：事件字段的位置在协议里并不统一——
                    # init_error 的 error/error_type/traceback 在**顶层**，
                    # quote/conn_state 的载荷在 data 里。此前只传 msg["data"]
                    # 会让 init_error 的 error 永远丢失（详见 _on_event 注释）。
                    self._on_event(msg.get("event"), msg)
                    continue
                rid = msg.get("id")
                fut = self._pending.get(rid)
                if fut is not None and not fut.done():
                    fut.set_result(msg)
        except Exception:  # noqa: BLE001
            pass
        finally:
            # 仅当仍是当前子进程时才标记断开/唤醒挂起调用：
            # 旧子进程被强制重启后其 stdout EOF 不得误杀新子进程的握手 future。
            if self._proc is proc:
                self._connected = False
                tail = self._stderr_tail()
                msg = f"桥接子进程已退出{('（stderr: ' + tail + '）') if tail else ''}"
                for f in list(self._pending.values()):
                    if not f.done():
                        f.set_exception(BrokerNotConnectedError(msg))

    def _on_event(self, event, msg):
        """处理子进程事件。`msg` 是**整条**事件消息（不是 msg["data"]）。

        协议不一致史（曾导致「桥接子进程握手失败：None」这一失真报错）：
        bridge_server 的 init_error 把 error/error_type/traceback 放在消息顶层，
        而 quote/conn_state 的载荷放在 data 里。此前 _read_loop 统一只传
        msg["data"]，init_error 时 data 为 None，`str(None)` 得到字符串 "None"
        并被当作真实错误透出——真实原因（如「QMT 客户端未登录」）被完全吞掉。
        现在传整条消息，各分支按协议自取，并同时兼容「字段在 data 内」的形态。
        """
        m = msg if isinstance(msg, dict) else {}
        data = m.get("data")
        if event == "init_error":
            d = data if isinstance(data, dict) else {}
            raw = m.get("error") if m.get("error") is not None else d.get("error")
            etype = str(m.get("error_type") or d.get("error_type") or "")
            tb = str(m.get("traceback") or d.get("traceback") or "")
            # 防失真空洞：SDK 在部分失败路径抛 args=(None,) 的空异常，序列化后
            # error 可能是 None，也可能是**字符串 "None"**（truthy，会绕过朴素的
            # falsy 兜底）。原样透出即「桥接子进程握手失败：None」，用户彻底失明。
            self._init_error = _humanize_init_error(raw, etype)
            # traceback 只入日志、不进用户提示：打包 EXE 黑盒下 qmt_work.log 是
            # 唯一诊断通道，缺了它任何 SDK 内部错误都无法追查。
            if tb:
                try:
                    log.error("bridge 子进程启动失败 [%s] raw=%r\n%s", etype, raw, tb)
                except Exception:  # noqa: BLE001
                    pass
            # 唤醒所有挂起调用（握手会因此拿到 init_error）
            for f in list(self._pending.values()):
                if not f.done():
                    f.set_exception(BrokerNotConnectedError(self._init_error))
        elif event == "conn_state":
            # 服务端状态泵推送的 SDK 真实连接态：据此更新本端 _connected，
            # 使 is_connected() 反映真实状态（健康监控据此触发重连）
            d = data if isinstance(data, dict) else {}
            self._connected = bool(d.get("connected", self._connected))
        elif event == "quote":
            # 阶段 1（C12）：仅入队，由独立 quote 线程执行 handler——reader 线程只做
            # 解析 + RPC 响应分发。有界队列满（handler 持续阻塞）时丢弃最旧并计数，
            # 防止内存无界增长（背压：宁可丢行情也不拖垮 RPC 通道）。
            try:
                self._quote_q.put_nowait(data)
            except _queue.Full:
                try:
                    self._quote_q.get_nowait()
                    self._quote_q.put_nowait(data)
                except Exception:  # noqa: BLE001
                    pass
                self._quote_dropped += 1

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
        except _FutTimeout as exc:
            # 阶段 1（C13）：方法级超时——超时≠未执行（尤其下单），
            # 抛 BrokerError 供上层幂等层去重，避免 HTTP 500 让调用方
            # 误以为「未下单」而重复提交造成双单。
            raise BrokerError(
                f"{method} 调用超时（{timeout}s）：请求可能仍在子进程执行，"
                f"请勿盲目重试（幂等层会对同一请求去重）") from exc
        finally:
            self._pending.pop(rid, None)
        if not res.get("ok"):
            etype = res.get("error_type")
            # 陷阱：dict.get(k, default) 在「键存在但值为 None」时返回 None 而非
            # default，会构造出 BrokerError(None) —— str() 得 "None"，用户失明。
            # 统一走 _humanize_init_error 兜底成可操作文案。
            emsg = _humanize_init_error(res.get("error"), str(etype or ""))
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
        # 阶段 0-D（C5）：记录订阅 codes，子进程重启后据此自动恢复订阅
        if codes:
            self._subscribed_codes.update(codes)
        self._rpc("_subscribe_quote", [list(codes)], timeout=15.0)

    def unsubscribe_quote(self, codes: list[str]) -> None:
        """退订：从本端记录中移除并下发子进程（子进程侧 XTP 无对应接口时降级为仅本端）。"""
        for c in codes:
            self._subscribed_codes.discard(c)
        try:
            self._rpc("unsubscribe_quote", [list(codes)], timeout=15.0)
        except Exception as exc:  # noqa: BLE001
            log.debug("unsubscribe_quote failed (child may not support): %s", exc)

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
        # 反映真实子进程存活状态：子进程退出后返回 False，健康监控据此触发重连
        if self._proc is None or self._proc.poll() is not None:
            self._connected = False
            return False
        return self._connected

    def _cleanup_proc(self) -> None:
        """彻底释放旧子进程：终止进程 + 关闭管道 + 唤醒挂起调用（幂等）。

        在强制重启（SDK 断开）与 close() 时调用；终止策略：先发送 _shutdown 优雅
        退出，超时则强杀——避免 SDK 断开后每次强制重启都堆积一个残留子进程。
        """
        self._connected = False
        # 阶段 1（C12）：停止独立 quote 处理线程（0.5s 内自然退出，daemon 兜底）
        self._quote_thread = None
        if self._proc is not None:
            proc = self._proc
            for f in list(self._pending.values()):
                if not f.done():
                    f.set_exception(BrokerNotConnectedError("桥接已关闭"))
            if proc.poll() is None:
                # 阶段 1（C14）：写 stdin 统一持锁，防止与并发 _rpc 请求交错成半行 JSON
                #（子进程 json.loads 失败静默 continue → 父端 future 挂到超时）
                with self._lock:
                    try:
                        proc.stdin.write(json.dumps(
                            {"id": -1, "method": "_shutdown", "args": []}) + "\n")
                        proc.stdin.flush()
                    except Exception:  # noqa: BLE001
                        pass
                try:
                    proc.wait(timeout=3.0)
                except Exception:  # noqa: BLE001
                    try:
                        proc.kill()
                    except Exception:  # noqa: BLE001
                        pass
            # 主动关闭管道，避免子进程退出后 TextIOWrapper 在 GC 时 flush 报错（Windows OSError 22）
            for s in (proc.stdin, proc.stdout, proc.stderr):
                try:
                    s.close()
                except Exception:  # noqa: BLE001
                    pass
        self._pending.clear()
        self._proc = None

    def close(self) -> None:
        if self._proc is None:
            self._connected = False
            return
        self._cleanup_proc()

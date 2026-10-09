"""Fake 大 QMT 端（仅用于**传输/协议层测试**，严禁产线使用）。

它站在**真实的 wire 协议另一侧**：读 ``req/<signal_id>.json``、写
``resp/<signal_id>.json``、追加 ``events.ndjson``。被测对象是
``FileSignalTransport`` + ``BigQmtV1`` + ``GenericConnector`` 这条链路，
Fake 替换的是「大 QMT 内置 Python」那一端。

为什么可以有一个 fake（与零 mock 冲突吗）
-----------------------------------------
不冲突：零 mock 约束的是「**产线**不得返回假数据」。这里是**回归测试替身**，
与仓内既有的 ``fake_bridge_server.py``（子进程 ABI 桥测试）同一性质：
它验证的是「协议有没有被正确处理」，而不是「券商返回了什么」。
所有 Fake 行为都必须是**可控且显式声明**的，禁止隐式补全字段。
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

#: 与真实 agent 的 ``agent_bigqmt/qmt_api.py::_ACTIONS`` **逐字对齐**。
#: ``BigQmtBridge.connector_probe()`` 的「函数缺失」判别消费这个清单，
#: 少了就会把「已支持」误判成「不支持」（反向假阴性同样是 bug）。
_ACTIONS = ("PROBE", "PLACE", "CANCEL_ORDER", "QUERY_ASSET", "QUERY_POSITION",
            "QUERY_ORDER", "QUERY_TRADE", "QUERY_QUOTE", "QUERY_KLINE",
            "QUERY_STOCK_LIST", "QUERY_SECTOR_LIST", "QUERY_INSTRUMENT",
            "QUERY_CALENDAR", "SUB_QUOTE")


class FakeBigQmtAgent:
    """在后台线程里扮演 agent 端。

    ★ 线程刻意 ``daemon=True``：解释器退出时不会被 join 住（TD-25：
    给非 daemon 线程安排可能无界阻塞的活，会让进程「关不掉」）。
    循环本身也带 ``stop()`` 显停条件，保证有界。
    """

    def __init__(self, bridge_dir: str, *, token: str = "",
                 trading_enabled: bool = True, ignore_ops: tuple[str, ...] = ()):
        self.bridge_dir = bridge_dir
        self.req_dir = os.path.join(bridge_dir, "req")
        self.resp_dir = os.path.join(bridge_dir, "resp")
        self.events_path = os.path.join(bridge_dir, "events.ndjson")
        self.token = token
        self.trading_enabled = trading_enabled
        self.ignore_ops = set(ignore_ops)   # 模拟「agent 没处理」（→ 超时）
        self.received: list[dict[str, Any]] = []
        # meta() 的诊断字段：真实 agent 由 Executor 自发维护，fake 同样如实记账。
        self._started_at = time.time()
        self.callback_hits = 0              # 回调真的被触发过几次（0 = 未绑定）
        self.direction_unknown = 0          # 方向仲裁落 unknown 的计数
        self.subscribed: set[str] = set()   # 已订阅代码（SUB_QUOTE 记账）
        #: 行情**实调**结论 ``(ok, detail)`` 或 ``None``。``None`` = 未测（默认，
        #: 既有用例行为不变）。★ 置 ``(False, "...")`` 可复现 2026-10-09 真机的
        #: 「接口在 captured 里、实调却抛错」场景 —— 正是能力协商假绿灯的触发条件。
        self.quote_call: tuple[bool, str] | None = None
        self._seq = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    def start(self) -> None:
        os.makedirs(self.req_dir, exist_ok=True)
        os.makedirs(self.resp_dir, exist_ok=True)
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.process_once()
            except Exception:  # noqa: BLE001  fake 不该把测试进程打崩
                pass
            if self._stop.wait(0.02):
                break

    def process_once(self) -> int:
        handled = 0
        for name in sorted(os.listdir(self.req_dir)):
            if not name.endswith(".json"):
                continue
            path = os.path.join(self.req_dir, name)
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    envelope = json.load(fh)
            except Exception:
                _silent_remove(path)
                continue
            try:
                os.remove(path)
            except OSError:
                pass
            self.received.append(envelope)
            if envelope.get("op") in self.ignore_ops:
                continue  # 不回包 ⇒ 调用方应超时
            self._write(envelope.get("signal_id", ""), self._reply(envelope))
            handled += 1
        return handled

    # ------------------------------------------------------------------
    def _write(self, signal_id: str, reply: dict[str, Any]) -> None:
        target = os.path.join(self.resp_dir, "%s.json" % signal_id)
        tmp = target + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(reply, fh, ensure_ascii=False)
        os.replace(tmp, target)

    def emit(self, kind: str, data: dict[str, Any]) -> None:
        self._seq += 1
        with open(self.events_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"seq": self._seq, "type": kind,
                                 "data": data, "ts": int(time.time() * 1000)},
                                ensure_ascii=False) + "\n")

    # ------------------------------------------------------------------
    def meta(self) -> dict[str, Any]:
        """与真实 agent 的 ``qmt_api.Executor.meta()`` **同形**（字段名逐个对齐）。

        ★ 为什么 fake 必须照抄真实现：``BigQmtBridge.connector_probe()`` 的四类根因
          判别（agent 未运行 / 函数缺失 / bridge_dir 两端不一致 / token 错）全靠这些
          字段。fake 少一个字段，那些判别路径在测试里就**永远不会被走到** ——
          测试全绿但线上仍误诊，是本项目已两次栽过的「绿灯被另一个 bug 遮出来」。
          参照实现：``agent_bigqmt/qmt_api.py::Executor.meta``。
        """
        return {
            "ver": "fake-1.0",
            "py": "3.6.8",
            "funcs": ["passorder", "cancel", "get_trade_detail_data",
                      "get_full_tick", "get_market_data",
                      "get_stock_list_in_sector", "get_sector_list",
                      "get_instrument_detail", "get_trading_dates",
                      "download_history_data"],
            "trading_enabled": self.trading_enabled,
            "bridge_dir": self.bridge_dir,
            "uptime_s": int(time.time() - self._started_at),
            "actions": list(_ACTIONS),
            "callback_bound": self.callback_hits > 0,
            "callback_hits": self.callback_hits,
            "direction_unknown": self.direction_unknown,
            "subscribed": len(self.subscribed),
            "ascii_only": False,
        }

    # ------------------------------------------------------------------
    def _reply(self, envelope: dict[str, Any]) -> dict[str, Any]:
        """构造响应信封。

        ★ 信封里的 ``agent``（meta）必须在**动作执行之后**取，与真 agent 对齐：
          真实现是 ``Executor.execute()`` 产出响应、再由 ``_write_response()`` 拼
          ``"agent": self.meta()``（``qmt_api.py``），所以它如实反映**本次动作生效
          后**的状态（如 SUB_QUOTE 之后的 ``subscribed`` 计数）。

          本方法曾经把 ``{... "agent": self.meta()}`` 先算好、再 ``_dispatch`` ——
          于是 fake 的 meta 恒为「上一条命令时」的快照，任何断言 post-action meta
          的测试都会**系统性差一拍**（假绿/假红都出现过）：又一处
          「fake 比现实更不忠实」——本项目已两次栽在这上面。
        """
        op = str(envelope.get("op") or "").upper()
        params = envelope.get("params") or {}
        ok, result, error, error_type = True, None, "", ""

        if self.token and envelope.get("auth") != self.token:
            ok, error, error_type = False, "auth token 不匹配", "Rejected"
        # D3 幽灵单防护（与真实 agent 同语义）：写请求过期拒执行
        elif op in ("PLACE", "CANCEL_ORDER"):
            ttl = int(params.get("ttl_ms") or envelope.get("ttl_ms") or 0)
            sent = int(envelope.get("ts") or 0)
            if ttl > 0 and sent and int(time.time() * 1000) > sent + ttl:
                ok, error, error_type = False, (
                    "请求已过期（ttl_ms=%d），拒绝执行以防幽灵单" % ttl), "Expired"

        if ok:
            try:
                result = self._dispatch(op, params)
            except _FakeBrokerError as exc:
                # 柜台拒单：error_type=BrokerError ⇒ 上层应映射成 400 + 真因
                ok, error, error_type = False, str(exc), exc.error_type

        return {
            "v": 1,
            "signal_id": envelope.get("signal_id", ""),
            "ok": ok,
            "result": result,
            "error": error,
            "error_type": error_type,
            "ts": int(time.time() * 1000),
            # meta 在动作之后取（见 docstring）——顺序是本方法的关键语义。
            "agent": self.meta(),
        }

    def _dispatch(self, op: str, params: dict[str, Any]) -> Any:
        if op == "PROBE":
            # 真实 QMT 的 ContextInfo 带有 get_full_tick / get_market_data，
            # 此处如实上报；探针据此把 quote/kline 判为 SUPPORTED。
            # M2.1 后行情扩展函数的捕获面也在 fake 里如实体现（能力=probe 实测，不猜）。
            out = {"captured": ["passorder", "cancel", "get_trade_detail_data",
                                "get_full_tick", "get_market_data",
                                "get_stock_list_in_sector", "get_sector_list",
                                "get_instrument_detail", "get_trading_dates",
                                "download_history_data"]}
            # ★ 真 agent 的 PROBE 应答带 quote_call（实调结论）；fake 只有显式声明
            #   过才带 —— 保持「Fake 行为可控且显式」的纪律，默认不带。
            if self.quote_call is not None:
                out["quote_call"] = list(self.quote_call)
            return out
        if op == "PLACE":
            if not self.trading_enabled:
                raise _FakeBrokerError(
                    "Rejected", "agent 侧 trading_enabled=false，拒绝下单")
            if float(params.get("price") or 0) <= 0 and \
                    str(params.get("price_type")) == "limit":
                raise _FakeBrokerError("BrokerError", "限价单必须给出正的价格")
            return {"order_id": "7160",
                    "client_order_id": params.get("client_order_id", ""),
                    "status": "submitted", "raw": [7160]}
        if op == "CANCEL_ORDER":
            if not params.get("order_id"):
                raise _FakeBrokerError("BrokerError", "撤单缺少 order_id")
            return {"order_id": params["order_id"], "status": "cancelled", "ok": True}
        if op == "QUERY_ORDER":
            return [{"order_id": "7160", "code": "600036.SH", "order_status": 56,
                     "volume": 100, "dealt": 100, "traded_price": 35.5,
                     # D2：fake 给的是**已仲裁**的归一化方向（真实 agent 输出同形）
                     "direction": "buy", "time": "14:31:02"}]
        if op == "QUERY_TRADE":
            return [{"trade_id": "50016562", "order_id": "7160",
                     "code": "600036.SH", "price": 35.5, "volume": 100,
                     "direction": "buy", "time": "14:31:02"}]
        if op == "QUERY_POSITION":
            return [{"code": "600036.SH", "name": "招商银行", "volume": 200,
                     "avail": 100, "cost": 34.2, "market_value": 7100.0}]
        if op == "QUERY_ASSET":
            return {"account_id": "8888", "cash": 10000.0, "frozen": 0.0,
                    "assets": 45200.0}
        # ---- 行情：必须与 ``_ACTIONS`` 的声明一致 ----
        # ★ 这两个 handler 曾经缺失，而 ``_ACTIONS`` 却宣称支持 ⇒
        #   ``GET_KLINE`` 打到 fake 会得到 ``UnsupportedOp: 未知 action: QUERY_KLINE``。
        #   声明的能力面与实现面必须闭合（与真实 agent 的 ``execute()`` 路由一致），
        #   否则测试会在一个**假能力面**上跑绿。
        if op == "QUERY_QUOTE":
            codes = list(params.get("codes") or [])
            if not codes:
                raise _FakeBrokerError("BrokerError", "QUERY_QUOTE 缺少 codes")
            return {c: {"lastPrice": 35.5, "volume": 1000,
                        "askPrice": [35.51, 35.52, 0, 0, 0],
                        "bidPrice": [35.49, 35.48, 0, 0, 0]}
                    for c in codes}
        if op == "QUERY_KLINE":
            code = params.get("stock_code") or ""
            if not code:
                raise _FakeBrokerError("BrokerError", "QUERY_KLINE 缺少 stock_code")
            count = int(params.get("count") or 0) or 3
            rows = []
            for i in range(max(1, min(count, 5))):
                rows.append({"time": "2026092%d" % (8 + i), "open": 34.0 + i,
                             "high": 36.0 + i, "low": 33.5 + i,
                             "close": 35.5 + i, "volume": 1000 + i})
            return rows
        # ---- M2.1 行情/合约扩展 action ----
        if op == "QUERY_STOCK_LIST":
            return [{"code": "600036.SH", "name": "招商银行"},
                    {"code": "000001.SZ", "name": "平安银行"}]
        if op == "QUERY_SECTOR_LIST":
            return ["沪深A股", "ETF", "指数"]
        if op == "QUERY_INSTRUMENT":
            code = params.get("code") or ""
            if not code:
                raise _FakeBrokerError("BrokerError", "QUERY_INSTRUMENT 缺少 code")
            return {"code": code, "InstrumentName": "测试标的"}
        if op == "QUERY_CALENDAR":
            return ["20260928", "20260929", "20260930"]
        if op == "SUB_QUOTE":
            codes = sorted(c for c in (params.get("codes") or []) if c)
            if not codes:
                raise _FakeBrokerError("BrokerError", "SUB_QUOTE 缺少 codes")
            self.subscribed.update(codes)      # meta()["subscribed"] 据此诚实上报
            return {"subscribed": codes, "mode": "poll_forward"}
        raise _FakeBrokerError("Unsupported", "未知 action: %s" % op)


class _FakeBrokerError(Exception):
    def __init__(self, error_type: str, message: str):
        Exception.__init__(self, message)
        self.error_type = error_type


def _silent_remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


__all__ = ["FakeBigQmtAgent"]

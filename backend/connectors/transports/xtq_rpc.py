"""xtquant 原生 RPC 传输层（58600 / 58670），**零 SDK 依赖**的行情旁路。

为什么需要它
------------
实测（2026-11-08）：
  * ``xtdata`` 官方客户端在外部进程里连不上 —— ``xtdata.ini`` 指向
    ``127.0.0.1:58610``，但大 QMT **不提供**该端口，``~/.xtquant`` 也不存在；
  * 国信那套 xtquant 包还有**包内错配**（``xtdata.py`` 引用了 pyd 不再导出的符号），
    外部 ``import xtquant.xtdata`` 直接崩；
  * 但两个 IPC 端口本身是活的，且说的是同一套协议：
    NetPackage 12 字节大端头 + BSON body（``flags & 7 == 2`` 时 zlib 压缩）。

于是这一层直接讲协议：不装 xtquant、不挑 Python 版本、不受包内错配影响。

协议要点（实测）
----------------
* 头 ``>IIHH`` = (total_len, seq, type, flags)，``total_len`` 含 12 字节头；
* 请求 ``type=3, flags=0x0042``（enc=2 表示 zlib）；
* 心跳 ``type=1``，周期 4.04s，body 只有 4 字节时间戳 —— **必须跳过**，
  否则会被当成"收到了推送"；
* 业务应答 ``type=3``，``{status: 0, params: {...}}``；
* 业务错误在 ``params.ErrorID``（如 ``200005 未找到处理函数`` / ``300000 invalid stockcode``）。

★ 判活口径（本文件最容易被误用的地方）
  ``connect()`` 成功**不等于**连上了。实测 ``xtdata.connect(port=58611)``（空端口）
  也在 0.000s 返回成功。因此本 transport 的 ``PROBE`` 必须打一次真实业务方法
  （``getTag``）并校验返回值，**绝不**用"TCP 能连"当就绪证据。

订阅形状（逆向 + 客户端实现双向确认）
  ``subscribe_quote`` 的参数是**三个嵌套文档**，不是扁平字典：

      {"meta":   {stockCode, period, metaid, periodnum, dividendtype},
       "region": {startTime, endTime, count},
       "param":  {needCallback}}

  扁平或数组形状会被服务端读成空值，恒回 ``invalid stockcode: ``。
"""
from __future__ import annotations

import asyncio
import re
import socket
import struct
import time
import zlib
from dataclasses import dataclass
from typing import Any, Iterable

from ..dialects.base import Ops
from ..transport import MAX_TIMEOUT, TransportError, WireRequest, WireResponse
from .base import BaseTransport

#: 帧头：total_len(含头) | seq | type | flags，全部大端。
_HDR = struct.Struct(">IIHH")
_HDR_LEN = _HDR.size

_REQ_TYPE = 3
_REQ_FLAGS = 0x0042          # enc=2(zlib) | class=1(rpc)
_HEARTBEAT_TYPE = 1

#: 单帧上限。帧头里的 total_len 是对端给的，损坏/错位时会读到荒谬的值；
#: 不设上限会让 recv 循环一直追着它跑，把一次协议错位变成内存耗尽。
MAX_FRAME_BYTES = 8 * 1024 * 1024

#: 服务端"没有这个处理函数"的错误码 —— 与"参数不对"必须区分开。
ERR_NO_HANDLER = 200005

#: 判定"这一帧确实带行情"的字段线索。**只用于证据分级，不用于丢弃帧**：
#: 未识别的帧照样完整保留并 dump 键集，交由人工核对。
_QUOTE_MARKERS = frozenset({
    "pushquote", "quotedata", "quotelist", "fulltick", "tickdata", "quotes",
})
_CODE_KEYS = frozenset({
    "stockcode", "stock_code", "code", "symbol", "instrumentid", "stocks",
})
_PRICE_KEYS = frozenset({
    "lastprice", "askprice", "bidprice", "price", "open", "high", "low",
    "close", "volume", "amount", "timetag", "ask", "bid", "last",
})

#: 每个端口的**已验证**方法集（实测扫描结果，不是猜测）。
#: 只登记实测通过的；新增方法必须先跑扫描器确认，不得凭想象补。
KNOWN_METHODS: dict[int, frozenset[str]] = {
    58600: frozenset({"getStockListInSector", "getInstrumentDetail", "getMarketData"}),
    58670: frozenset({
        "getTag", "checkVersion", "getDataDir", "getAppBinDir",
        "getSectorList", "getStockListInSector", "getInstrumentDetail",
        "getFullTick", "subscribeQuote", "unsubscribeQuote",
        "createView", "destoryView", "bindViewFormula",
        "subscribeFormula", "unsubscribeFormula", "callFormula", "commonControl",
    }),
}

#: canonical op → RPC 方法名。只映射**行情类**；下单类不在此列（大 QMT 无此能力）。
_OP_TO_METHOD: dict[str, str] = {
    Ops.PROBE: "getTag",
    Ops.TEST_CONNECTION: "getTag",
    Ops.GET_FULL_TICK: "getFullTick",
    Ops.GET_QUOTE: "getMarketData",
    Ops.SUBSCRIBE_QUOTE: "subscribeQuote",
    "GET_SECTOR_LIST": "getSectorList",
    "GET_STOCK_LIST_IN_SECTOR": "getStockListInSector",
    "GET_INSTRUMENT_DETAIL": "getInstrumentDetail",
}

#: 写操作一律拒绝 —— 这两个端口实测**没有任何**下单/撤单/账户方法。
_WRITE_OPS = frozenset({Ops.PLACE_ORDER, Ops.CANCEL_ORDER})


class BsonError(ValueError):
    """BSON 编解码失败。"""


# ----------------------------------------------------------------------
# 最小 BSON（只覆盖 QMT 实际用到的类型）
# ----------------------------------------------------------------------
def _cstr(s: str) -> bytes:
    return s.encode("utf-8") + b"\x00"


def _elem(key: str, value: Any) -> bytes:
    k = _cstr(key)
    if isinstance(value, bool):
        return b"\x08" + k + (b"\x01" if value else b"\x00")
    if isinstance(value, int):
        if -2 ** 31 <= value < 2 ** 31:
            return b"\x10" + k + struct.pack("<i", value)
        return b"\x12" + k + struct.pack("<q", value)
    if isinstance(value, float):
        return b"\x01" + k + struct.pack("<d", value)
    if isinstance(value, str):
        b = value.encode("utf-8")
        return b"\x02" + k + struct.pack("<i", len(b) + 1) + b + b"\x00"
    if isinstance(value, (bytes, bytearray)):
        return b"\x05" + k + struct.pack("<i", len(value)) + b"\x00" + bytes(value)
    if isinstance(value, (list, tuple)):
        body = b"".join(_elem(str(i), v) for i, v in enumerate(value)) + b"\x00"
        return b"\x04" + k + struct.pack("<i", len(body) + 4) + body
    if isinstance(value, dict):
        return b"\x03" + k + bson_encode(value)
    if value is None:
        return b"\x0a" + k
    raise BsonError(f"不支持的 BSON 值类型: {type(value).__name__}")


def bson_encode(doc: dict) -> bytes:
    """把 dict 编成一个完整 BSON 文档（含长度前缀与结尾 \\x00）。"""
    if not isinstance(doc, dict):
        raise BsonError("bson_encode 需要 dict")
    body = b"".join(_elem(k, v) for k, v in doc.items()) + b"\x00"
    return struct.pack("<i", len(body) + 4) + body


def _read_cstr(buf: bytes, i: int) -> tuple[str, int]:
    j = buf.index(b"\x00", i)
    return buf[i:j].decode("utf-8", "replace"), j + 1


def bson_decode(buf: bytes, offset: int = 0) -> tuple[dict, int]:
    """解码一个 BSON 文档，返回 ``(dict, 结束偏移)``。"""
    if offset + 4 > len(buf):
        raise BsonError("BSON 长度字段越界")
    total = struct.unpack_from("<i", buf, offset)[0]
    if total < 5 or offset + total > len(buf):
        raise BsonError(f"BSON 文档长度非法: {total}")
    end = offset + total
    i = offset + 4
    out: dict[str, Any] = {}
    while i < end and buf[i] != 0:
        t = buf[i]
        i += 1
        key, i = _read_cstr(buf, i)
        if t == 0x01:
            out[key] = struct.unpack_from("<d", buf, i)[0]
            i += 8
        elif t == 0x02:
            n = struct.unpack_from("<i", buf, i)[0]
            i += 4
            out[key] = buf[i:i + n - 1].decode("utf-8", "replace")
            i += n
        elif t in (0x03, 0x04):
            sub, i = bson_decode(buf, i)
            out[key] = list(sub.values()) if t == 0x04 else sub
        elif t == 0x05:
            n = struct.unpack_from("<i", buf, i)[0]
            i += 5
            out[key] = buf[i:i + n]
            i += n
        elif t == 0x08:
            out[key] = buf[i] != 0
            i += 1
        elif t == 0x0a:
            out[key] = None
        elif t == 0x10:
            out[key] = struct.unpack_from("<i", buf, i)[0]
            i += 4
        elif t == 0x12:
            out[key] = struct.unpack_from("<q", buf, i)[0]
            i += 8
        elif t == 0x11:
            out[key] = struct.unpack_from("<Q", buf, i)[0]
            i += 8
        else:
            raise BsonError(f"未支持的 BSON 类型 0x{t:02x}（键 {key}）")
    return out, end


# ----------------------------------------------------------------------
# 帧
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class Frame:
    total: int
    seq: int
    ftype: int
    flags: int
    body: bytes

    @property
    def encoding(self) -> int:
        return self.flags & 7

    @property
    def direction(self) -> int:
        return (self.flags >> 13) & 7

    @property
    def is_heartbeat(self) -> bool:
        return self.ftype == _HEARTBEAT_TYPE

    def decode(self) -> dict:
        raw = zlib.decompress(self.body) if self.encoding == 2 else self.body
        doc, _ = bson_decode(raw, 0)
        return doc


def parse_header(hdr: bytes) -> tuple[int, int, int, int]:
    if len(hdr) != _HDR_LEN:
        raise ValueError(f"帧头必须 {_HDR_LEN} 字节，实际 {len(hdr)}")
    return _HDR.unpack(hdr)


def build_request(func: str, params: Any, seq: int = 1) -> bytes:
    """构造一个请求帧（含头）。"""
    payload = bson_encode({"func": func, "params": params})
    body = zlib.compress(payload)
    return _HDR.pack(_HDR_LEN + len(body), seq, _REQ_TYPE, _REQ_FLAGS) + body


class _FrameReader:
    """跨 ``recv`` / 超时**保持半帧缓冲**的读帧器。

    ★ 这是抓推送能不能信的关键。原实现把 ``hdr`` / ``body`` 放在函数局部变量里，
      socket 超时（0.5s）一抛异常，已收到的字节就丢了；外层 ``continue`` 后
      从**帧中间**重新开始解 12 字节头 ⇒ 协议错位、后续全部解不出来。
      收盘后帧小，几乎不触发；交易时段一张大 tick 帧必然跨多次 recv，
      那时"没有行情推送"是**探针自己的错**，不是服务端没推。

    ``read()`` 返回 ``None`` 表示"还没凑齐一帧"（超时或数据不足），调用方据此
    复查时间预算；已收到的字节始终留在 ``self._buf`` 里。
    """
    __slots__ = ("_sock", "_buf", "_meta", "_need", "_max")

    def __init__(self, sock: socket.socket, *, max_bytes: int = MAX_FRAME_BYTES):
        self._sock = sock
        self._buf = bytearray()
        self._meta: tuple[int, int, int, int] | None = None
        self._need: int | None = None
        self._max = max_bytes

    def _pump(self) -> bool:
        try:
            chunk = self._sock.recv(65536)
        except socket.timeout:
            return False
        if not chunk:
            raise EOFError("连接被对端关闭")
        self._buf.extend(chunk)
        return True

    def read(self) -> Frame | None:
        while True:
            if self._need is None:
                if len(self._buf) < _HDR_LEN:
                    if not self._pump():
                        return None
                    continue
                total, seq, ftype, flags = parse_header(bytes(self._buf[:_HDR_LEN]))
                if total < _HDR_LEN:
                    raise BsonError(f"帧长度非法: {total}")
                if total > self._max:
                    raise BsonError(
                        f"帧长度 {total} 超过上限 {self._max}（多半是流已错位）")
                self._meta = (total, seq, ftype, flags)
                self._need = total
            assert self._need is not None and self._meta is not None
            if len(self._buf) < self._need:
                if not self._pump():
                    return None
                continue
            raw = bytes(self._buf[:self._need])
            del self._buf[:self._need]
            total, seq, ftype, flags = self._meta
            self._meta = None
            self._need = None
            return Frame(total=total, seq=seq, ftype=ftype, flags=flags,
                         body=raw[_HDR_LEN:])


def _quote_signal(doc: Any, *, depth: int = 3) -> bool:
    """保守判断"这帧确实带行情字段"，不认 ``status`` / ``result`` 这类通用应答。

    返回 False **不代表不是行情**，只代表"自动判据不敢认" —— 这类帧会作为
    ``push_unclassified`` 完整保留并 dump 键集，避免把未知结构的推送误判成
    "没推送"，也避免把 ACK 误判成"有推送"。
    """
    if depth <= 0 or not isinstance(doc, dict):
        return False
    keys = {str(k).lower() for k in doc}
    has_marker = bool(keys & _QUOTE_MARKERS)
    has_code = bool(keys & _CODE_KEYS)
    has_price = bool(keys & _PRICE_KEYS)
    if (has_marker and (has_code or has_price)) or (has_code and has_price):
        return True
    for value in doc.values():
        if isinstance(value, dict) and _quote_signal(value, depth=depth - 1):
            return True
        if isinstance(value, list):
            for item in value[:3]:
                if isinstance(item, dict) and _quote_signal(item, depth=depth - 1):
                    return True
    return False


#: 形如 ``600036.SH`` 的键。``getFullTick`` 的应答是「代码 → 字段字典」，
#: 外层键名并不是 ``stockCode``，只看 ``_CODE_KEYS`` 会把真实 tick 漏判成"没行情"。
_CODE_SHAPED = re.compile(r"^\d{6}\.(SH|SZ|BJ)$", re.IGNORECASE)


def classify_payload(value: Any) -> dict:
    """给**拉取型**应答的结果体分级，防两类假绿 + 一类假红。

    * 假绿① ``getStockListInSector`` 返回 5224 个代码字符串 —— 这是**证券代码表**，
      不是行情数据，``len > 0`` 绝不能读成"行情已通"；
    * 假绿② ``{status: 0, params: {result: []}}`` 只是服务端受理了查询；
    * 假红 ``getFullTick`` 应答形如 ``{代码: {lastPrice, open, high, ...}}``，
      外层没有 ``stockCode`` 键，旧的 ``_quote_signal`` 不认 ⇒ 真 tick 被判"未证实"。

    返回 ``{kind, size, quote_proven, price_keys, code_keys, markers,
    code_shaped_keys, key_sets}``。``key_sets`` 让"未证实"这一结论本身可复核
    —— 探针断言不可证伪 = 探针错。
    """
    out: dict[str, Any] = {
        "kind": "empty", "size": 0, "quote_proven": False,
        "price_keys": [], "code_keys": [], "markers": [],
        "code_shaped_keys": [], "key_sets": [],
    }
    if value is None:
        return out
    if isinstance(value, (list, tuple)):
        out["size"] = len(value)
        if not value:
            return out
        samples = [x for x in value[:3] if isinstance(x, dict)]
        if samples:
            out["kind"] = "object_list"
        elif all(isinstance(x, str) for x in value[:3]):
            shaped = any(_CODE_SHAPED.match(str(x)) for x in value[:3])
            out["kind"] = "code_list" if shaped else "text_list"
            return out                      # 纯字符串表里没有任何行情字段
        else:
            out["kind"] = "scalar_list"
            return out
    elif isinstance(value, dict):
        out["size"] = len(value)
        if not value:
            return out
        out["kind"] = "dict"
        samples = [value]
    else:
        out["kind"] = type(value).__name__
        out["size"] = 1
        return out

    keys: set[str] = set()
    key_sets: list[list[str]] = []

    def walk(doc: Any, depth: int) -> None:
        if depth <= 0 or not isinstance(doc, dict):
            return
        ks = sorted(str(k) for k in doc)
        keys.update(ks)
        if len(key_sets) < 4 and ks not in key_sets:
            key_sets.append(ks)
        for item in doc.values():
            if isinstance(item, dict):
                walk(item, depth - 1)
            elif isinstance(item, list):
                for sub in item[:2]:
                    if isinstance(sub, dict):
                        walk(sub, depth - 1)

    for sample in samples:
        walk(sample, 3)
    lower = {k.lower() for k in keys}
    out["price_keys"] = sorted(lower & _PRICE_KEYS)
    out["code_keys"] = sorted(lower & _CODE_KEYS)
    out["markers"] = sorted(lower & _QUOTE_MARKERS)
    out["code_shaped_keys"] = sorted(k for k in keys if _CODE_SHAPED.match(k))
    out["key_sets"] = key_sets
    out["quote_proven"] = (bool(out["markers"])
                           or (bool(out["price_keys"])
                               and bool(out["code_keys"] or out["code_shaped_keys"]))
                           or len(out["price_keys"]) >= 3)
    return out


def subscribe_params(stock_code: str, *, period: str = "tick", metaid: int = 0,
                     periodnum: int = 0, dividendtype: Any = None,
                     start_time: str = "", end_time: str = "", count: int = 0,
                     need_callback: bool = True) -> dict:
    """构造 ``subscribeQuote`` 的**正确**参数（三个嵌套文档）。

    形状来自客户端自身实现 ``xtdata.py::subscribe_quote2``：
    Python 层传三块 BSON，C++ 包装层按 ``meta`` / ``region`` / ``param`` 三键下发。
    扁平或数组形状会被读成空值（实测恒回 ``invalid stockcode: ``）。
    """
    if not stock_code or "." not in stock_code:
        raise ValueError(f"stock_code 必须形如 '600036.SH'，收到 {stock_code!r}")
    code, _, market = stock_code.partition(".")
    if not code or not market:
        raise ValueError(f"stock_code 的代码段与市场段都不能为空，收到 {stock_code!r}")
    return {
        "meta": {"stockCode": stock_code, "period": period, "metaid": metaid,
                 "periodnum": periodnum, "dividendtype": dividendtype},
        "region": {"startTime": start_time, "endTime": end_time, "count": count},
        "param": {"needCallback": bool(need_callback)},
    }


# ----------------------------------------------------------------------
# Transport
# ----------------------------------------------------------------------
class XtqRpcTransport(BaseTransport):
    """58600 / 58670 的行情旁路传输（只读）。

    ★ ``supports_callback = False``：本传输能**收到**推送帧，但回调对象无法跨进程
      序列化给调用方，实时性由 ``capture()`` 拉取。与 file/redis 同族语义。
    """

    transport_id = "xtq_rpc"
    supports_callback = False

    def __init__(self, host: str = "127.0.0.1", port: int = 58670,
                 *, timeout: float = 5.0, label: str = "") -> None:
        self.host = host
        self.port = int(port)
        self.timeout = min(max(float(timeout), 0.1), MAX_TIMEOUT)
        self.label = label or f"{host}:{port}"

    # -- 能力面 --------------------------------------------------------
    def known_methods(self) -> frozenset[str]:
        """该端口**实测**支持的方法集；未知端口返回空集（不假装支持）。"""
        return KNOWN_METHODS.get(self.port, frozenset())

    def supports(self, op: str) -> bool:
        method = _OP_TO_METHOD.get(op)
        return bool(method) and method in self.known_methods()

    # -- 同步底层 ------------------------------------------------------
    def _call_sync(self, func: str, params: Any, timeout: float | None = None) -> dict:
        """一次新连接一次调用；跳过心跳帧。返回完整应答文档。"""
        budget = self.timeout if timeout is None else min(max(timeout, 0.1), MAX_TIMEOUT)
        try:
            sock = socket.create_connection((self.host, self.port), timeout=budget)
        except OSError as exc:
            raise TransportError(
                f"[xtq_rpc] 连不上 {self.label}: {exc}") from exc
        try:
            sock.settimeout(min(0.5, budget))
            sock.sendall(build_request(func, params, seq=1))
            reader = _FrameReader(sock)
            deadline = time.monotonic() + budget
            while time.monotonic() < deadline:
                frame = reader.read()
                if frame is None:          # 半帧 / 超时，缓冲保留，下一轮接着收
                    continue
                if frame.is_heartbeat:
                    continue
                return frame.decode()
            raise TransportError(
                f"[xtq_rpc] {func} 在 {budget:.1f}s 内无应答（{self.label}）")
        except TransportError:
            raise
        except (OSError, EOFError, zlib.error, BsonError) as exc:
            raise TransportError(f"[xtq_rpc] {func} 通信失败: {exc}") from exc
        finally:
            try:
                sock.close()
            except OSError:
                pass

    @staticmethod
    def _read_frame(sock: socket.socket) -> Frame:
        """兼容旧调用：一次性读到整帧；超时原样抛出。

        生产路径一律用 ``_FrameReader`` —— 只有它能跨 socket 超时保住半帧。
        """
        frame = _FrameReader(sock).read()
        if frame is None:
            raise socket.timeout("等待完整帧超时")
        return frame

    # -- 语义层 --------------------------------------------------------
    @staticmethod
    def _unwrap(func: str, doc: dict) -> Any:
        """把应答拆成结果或抛错。

        ★ 三种失败必须区分开，否则上层会把"没有这个方法"当成"没有数据"：
          * ``ErrorID`` 存在            → 服务端拒绝（可能是能力缺失，也可能是参数错）；
          * ``status`` 存在且非 0       → 业务失败；
          * 两者都没有                  → 协议异常。
        """
        params = doc.get("params") if isinstance(doc.get("params"), dict) else doc
        eid = params.get("ErrorID")
        if eid is not None:
            msg = str(params.get("ErrorMsg") or "")
            raise TransportError(f"[xtq_rpc] {func} 被拒: ErrorID={eid} {msg}".strip())
        status = doc.get("status", params.get("status") if isinstance(params, dict) else None)
        if status is not None and int(status) != 0:
            raise TransportError(f"[xtq_rpc] {func} 失败: status={status}")
        if isinstance(params, dict) and "result" in params:
            return params["result"]
        return params

    async def _dispatch(self, request: WireRequest, started: float) -> WireResponse:
        op = request.op
        if op in _WRITE_OPS:
            return self._fail(
                f"xtq_rpc 是只读行情通道，不支持 {op}（58600/58670 实测无任何下单方法）",
                "ReadOnlyTransport", request, started, port=self.port)

        method = _OP_TO_METHOD.get(op)
        if not method:
            return self._fail(f"xtq_rpc 不支持 op={op}", "UnsupportedOp",
                              request, started, port=self.port)

        if op == Ops.SUBSCRIBE_QUOTE:
            code = str(request.params.get("stock_code") or request.params.get("code") or "")
            try:
                params = subscribe_params(
                    code,
                    period=str(request.params.get("period") or "tick"),
                    count=int(request.params.get("count") or 0),
                    start_time=str(request.params.get("start_time") or ""),
                    end_time=str(request.params.get("end_time") or ""),
                )
            except (ValueError, TypeError) as exc:
                return self._fail(str(exc), "BadRequest", request, started, port=self.port)
        else:
            params = request.params.get("params", request.params)

        loop = asyncio.get_running_loop()
        try:
            doc = await loop.run_in_executor(
                None, self._call_sync, method, params, request.timeout)
        except TransportError as exc:
            return self._fail(str(exc), "TransportError", request, started,
                              port=self.port, method=method)
        try:
            result = self._unwrap(method, doc)
        except TransportError as exc:
            return self._fail(str(exc), "RemoteError", request, started,
                              port=self.port, method=method)

        if op in (Ops.PROBE, Ops.TEST_CONNECTION):
            # ★ 判活必须看内容，不能看"有没有回包"
            tag = result if isinstance(result, dict) else {}
            if str(tag.get("tag") or "") != "xtquant":
                return self._fail(
                    f"探活失败：getTag 未返回 xtquant 标识（实际 {tag!r}）",
                    "NotReady", request, started, port=self.port)
            return self._ok({"tag": tag.get("tag"), "version": tag.get("version"),
                             "port": self.port, "methods": sorted(self.known_methods())},
                            request, started, port=self.port)

        return self._ok(result, request, started, port=self.port, method=method)

    # -- 订阅抓取 ------------------------------------------------------
    def capture(self, stock_code: str, *, seconds: float = 10.0,
                period: str = "tick") -> list[dict]:
        """订阅一只标的并**同步**收取推送，返回分级后的帧列表。

        用于诊断与离线核对（"到底有没有推"）。生产实时链路应走 EventPort。

        ★ 两条防假绿纪律：
          1. ``_FrameReader`` 跨超时保住半帧，不做"帧中间重新解头"；
          2. **订阅 ACK 不是行情推送**。实测收盘后能收到一帧
             ``{status: 0, params: ...}`` 的应答 + 8 帧心跳；旧实现把
             任何非心跳帧都算成 ``data``，于是汇总会报"有行情推送"——
             那是把"服务端受理了订阅"读成"推了 tick"，性质完全不同。

        帧分四级：``heartbeat`` / ``ack`` / ``quote`` / ``data_other``。
        最后一级**照样完整保留 doc 与键集**，因为真实推送的键名尚未实证；
        自动判据不敢认 ≠ 没有推送，这种情况下结论必须说"需人工核对"。
        """
        budget = min(max(float(seconds), 0.1), 300.0)
        params = subscribe_params(stock_code, period=period)
        frames: list[dict] = []
        try:
            sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except OSError as exc:
            raise TransportError(f"[xtq_rpc] 连不上 {self.label}: {exc}") from exc
        try:
            sock.settimeout(0.5)
            sock.sendall(build_request("subscribeQuote", params, seq=1))
            reader = _FrameReader(sock)
            deadline = time.monotonic() + budget
            while time.monotonic() < deadline:
                try:
                    frame = reader.read()
                except (BsonError, ValueError) as exc:
                    # 帧头自相矛盾 ⇒ 流已不可信，继续读只会产出垃圾帧。
                    frames.append({"kind": "protocol_error", "error": str(exc)})
                    break
                except EOFError:
                    # 对端提前关闭：如实记录，但**已收到的帧不丢** ——
                    # 抛错会让"收到 3 帧心跳后被关"变成"什么都没收到"，
                    # 那正好掩盖了"订阅未生效"这个真正要看的信息。
                    frames.append({"kind": "closed"})
                    break
                if frame is None:              # 半帧 / 超时，缓冲保留
                    continue
                meta = {"seq": frame.seq, "type": frame.ftype,
                        "flags": frame.flags, "length": frame.total}
                if frame.is_heartbeat:
                    frames.append({"kind": "heartbeat", **meta})
                    continue
                try:
                    doc = frame.decode()
                except (zlib.error, BsonError, ValueError) as exc:
                    frames.append({"kind": "undecodable", "error": str(exc), **meta})
                    continue
                keys = sorted(str(k) for k in doc) if isinstance(doc, dict) else []
                common = {"keys": keys, "doc": doc, **meta}
                if _quote_signal(doc):
                    frames.append({"kind": "quote", **common})
                elif isinstance(doc, dict) and ("status" in doc or "ErrorID" in doc):
                    frames.append({"kind": "ack", **common})
                else:
                    frames.append({"kind": "data_other", **common})
        except OSError as exc:
            raise TransportError(f"[xtq_rpc] capture 中断: {exc}") from exc
        finally:
            try:
                sock.close()
            except OSError:
                pass
        return frames

    @staticmethod
    def summarize_capture(frames: Iterable[dict]) -> dict:
        """把 capture 结果压成一句可判读的结论。

        ``proven`` 只在**确实看到行情字段**时为 True；只有 ACK、只有心跳、
        或只有未识别帧时都不得说"通了"。``key_sets`` 把未知帧的键集带出来，
        使"未证实"这一结论本身可被复核（探针断言不可证伪 = 探针错）。
        """
        items = list(frames)
        kinds = [str(f.get("kind") or "") for f in items]
        count = lambda k: kinds.count(k)  # noqa: E731
        hb, ack = count("heartbeat"), count("ack")
        quote, other = count("quote"), count("data_other")
        data = quote + other
        key_sets = []
        for f in items:
            if f.get("kind") in ("data_other", "quote", "ack") and f.get("keys"):
                pair = [f.get("kind"), f.get("keys")]
                if pair not in key_sets:
                    key_sets.append(pair)
        if quote:
            verdict = "有行情推送"
        elif other:
            verdict = f"收到 {other} 个非心跳帧但未能确认含行情字段（需人工核对键集）"
        elif hb and ack:
            verdict = "只有订阅应答与心跳（订阅未生效或非交易时段）"
        elif hb:
            verdict = "只有心跳（订阅未生效或非交易时段）"
        elif ack:
            verdict = "只有订阅应答，无推送"
        elif items:
            verdict = "无任何有效帧"
        else:
            verdict = "无任何帧"
        return {
            "total": len(items),
            "heartbeat": hb,
            "ack": ack,
            "quote": quote,
            "data_other": other,
            "data": data,
            "undecodable": count("undecodable"),
            "protocol_error": count("protocol_error"),
            "closed": count("closed"),
            "key_sets": key_sets,
            "proven": quote > 0,
            "verdict": verdict,
        }


__all__ = [
    "XtqRpcTransport", "Frame", "BsonError",
    "bson_encode", "bson_decode", "build_request", "parse_header",
    "subscribe_params", "classify_payload", "KNOWN_METHODS",
    "ERR_NO_HANDLER", "MAX_FRAME_BYTES",
]

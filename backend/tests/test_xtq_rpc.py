"""xtquant 原生 RPC 传输层（58600/58670）：**零 SDK 依赖**的行情旁路。

为什么单独测：这条路不装 xtquant、不挑 Python 版本，讲的是裸协议
（NetPackage 12 字节大端头 + BSON + 可选 zlib）。协议层错一个字节就是
"连上了但拿不到数据"，而那种失败最容易被误判成"服务端没数据"。

盯的不变量：
* BSON 编解码自洽（含嵌套文档/数组/bytes/None/int64）；
* 请求帧头字段与实测一致（type=3, flags=0x0042）；
* **心跳必须被跳过** —— 4.04s 一帧的 type=1 若被当成数据，收盘后也会"有推送"；
* 探活必须看内容（``getTag`` 要返回 ``xtquant``），不能只看"有没有回包"；
* 写操作一律拒绝（这两个端口实测**没有**任何下单方法）；
* 业务错误（``ErrorID``）必须翻译成失败，不能当成"空结果"。
"""
import asyncio
import socket
import struct
import sys
import threading
import time
import zlib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from connectors.dialects.base import Ops  # noqa: E402
from connectors.transport import WireRequest  # noqa: E402
from connectors.transports import get_transport  # noqa: E402
from connectors.transports.xtq_rpc import (  # noqa: E402
    ERR_NO_HANDLER,
    MAX_FRAME_BYTES,
    XtqRpcTransport,
    BsonError,
    _FrameReader,
    bson_decode,
    bson_encode,
    build_request,
    classify_payload,
    parse_header,
    subscribe_params,
)

_HDR = struct.Struct(">IIHH")


# ----------------------------------------------------------------------
# 假服务端：真实 socket，不 mock 协议
# ----------------------------------------------------------------------
class FakeServer:
    """按 responder 回一帧；可先发若干心跳帧，用来验证"心跳被跳过"。"""

    def __init__(self, responder, *, heartbeats: int = 0, heartbeat_first: bool = True):
        self._responder = responder
        self._heartbeats = heartbeats
        self._hb_first = heartbeat_first
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(4)
        self._sock.settimeout(5)
        self.port = self._sock.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass
        self._thread.join(timeout=3)

    def _serve(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except (socket.timeout, OSError):
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket):
        with conn:
            try:
                hdr = b""
                while len(hdr) < 12:
                    c = conn.recv(12 - len(hdr))
                    if not c:
                        return
                    hdr += c
                total, seq, _t, _f = parse_header(hdr)
                body = b""
                while len(body) < total - 12:
                    c = conn.recv(total - 12 - len(body))
                    if not c:
                        return
                    body += c
                req, _ = bson_decode(zlib.decompress(body), 0)

                hb = _HDR.pack(12 + 4, 0, 1, 0x0C40) + struct.pack(">I", 1794126957)
                if self._hb_first:
                    for _ in range(self._heartbeats):
                        conn.sendall(hb)

                doc = self._responder(req.get("func"), req.get("params"))
                payload = bson_encode(doc)
                blob = zlib.compress(payload)
                conn.sendall(_HDR.pack(12 + len(blob), seq, 3, 0x2042) + blob)

                if not self._hb_first:
                    for _ in range(self._heartbeats):
                        conn.sendall(hb)
            except OSError:
                return


def _wire_frame(seq, ftype, flags, *, doc=None, raw_body=None):
    """拼一帧（可指定任意 flags / 原始 body），供"原始帧脚本"用。"""
    if doc is not None:
        payload = bson_encode(doc)
        body = zlib.compress(payload) if (flags & 7) == 2 else payload
    else:
        body = raw_body or b""
    return _HDR.pack(12 + len(body), seq, ftype, flags) + body


def _heartbeat_frame():
    return _wire_frame(0, 1, 0x0C40, raw_body=struct.pack(">I", 1))


class RawServer:
    """按脚本逐段发原始帧；用于验证"ACK 不是行情"和半帧保活。"""

    def __init__(self, chunks, *, keepalive: float = 2.5):
        self._chunks = list(chunks)
        self._keepalive = keepalive
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(4)
        self._sock.settimeout(5)
        self.port = self._sock.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass
        self._thread.join(timeout=3)

    def _serve(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except (socket.timeout, OSError):
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        with conn:
            try:
                hdr = b""
                while len(hdr) < 12:
                    c = conn.recv(12 - len(hdr))
                    if not c:
                        return
                    hdr += c
                total, _, _, _ = parse_header(hdr)
                body = b""
                while len(body) < total - 12:
                    c = conn.recv(total - 12 - len(body))
                    if not c:
                        return
                    body += c
                for chunk in self._chunks:
                    if chunk:
                        conn.sendall(chunk)
                    time.sleep(0.03)       # 制造帧间/帧内间隔
                limit = time.monotonic() + self._keepalive
                while not self._stop.is_set() and time.monotonic() < limit:
                    time.sleep(0.02)
            except OSError:
                return


class _ChunkSocket:
    """按脚本吐字节；``None`` 表示一次 socket.timeout（模拟半帧后超时）。"""

    def __init__(self, chunks):
        self._chunks = list(chunks)

    def recv(self, n):
        if not self._chunks:
            raise socket.timeout()
        item = self._chunks.pop(0)
        if item is None:
            raise socket.timeout()
        assert n >= len(item), "测试用块必须小于 recv 请求量"
        return item


def _invoke(transport, op, params=None, timeout=3.0):
    return asyncio.run(transport.invoke(
        WireRequest(op=op, params=params or {}, timeout=timeout)))


# ----------------------------------------------------------------------
# BSON
# ----------------------------------------------------------------------
def test_bson_roundtrip_nested_and_typed():
    doc = {
        "s": "600036.SH", "i": 42, "big": 2 ** 40, "f": 1.5,
        "b": True, "n": None, "raw": b"\x01\x02",
        "sub": {"stockCode": "600036.SH", "metaid": 0},
        "arr": ["a", 1, False],
    }
    back, end = bson_decode(bson_encode(doc), 0)
    assert back == doc
    assert end == len(bson_encode(doc))


def test_bson_rejects_unsupported_type():
    with pytest.raises(BsonError):
        bson_encode({"x": object()})


def test_bson_rejects_truncated_document():
    good = bson_encode({"a": 1})
    with pytest.raises(BsonError):
        bson_decode(good[:3], 0)


# ----------------------------------------------------------------------
# 帧
# ----------------------------------------------------------------------
def test_build_request_header_matches_measured_wire():
    frame = build_request("getTag", {}, seq=7)
    total, seq, ftype, flags = parse_header(frame[:12])
    assert total == len(frame)
    assert seq == 7
    assert ftype == 3, "请求 type 实测为 3"
    assert flags == 0x0042, "请求 flags 实测为 0x0042（enc=2 zlib | class=1 rpc）"
    doc, _ = bson_decode(zlib.decompress(frame[12:]), 0)
    assert doc == {"func": "getTag", "params": {}}


def test_parse_header_rejects_wrong_length():
    with pytest.raises(ValueError):
        parse_header(b"\x00" * 8)


# ----------------------------------------------------------------------
# 订阅参数形状（本模块最核心的一条）
# ----------------------------------------------------------------------
def test_subscribe_params_is_three_nested_documents():
    p = subscribe_params("600036.SH")
    assert set(p) == {"meta", "region", "param"}, \
        "扁平/数组形状会被服务端读成空值（实测恒回 invalid stockcode: ）"
    assert p["meta"]["stockCode"] == "600036.SH"
    assert p["meta"]["period"] == "tick"
    assert p["region"] == {"startTime": "", "endTime": "", "count": 0}
    assert p["param"] == {"needCallback": True}


def test_subscribe_params_rejects_malformed_code():
    for bad in ("", "600036", "600036."):
        with pytest.raises(ValueError):
            subscribe_params(bad)


# ----------------------------------------------------------------------
# 能力面
# ----------------------------------------------------------------------
def test_supports_only_measured_methods():
    t70 = XtqRpcTransport(port=58670)
    assert t70.supports(Ops.GET_FULL_TICK) is True
    assert t70.supports(Ops.SUBSCRIBE_QUOTE) is True

    t00 = XtqRpcTransport(port=58600)
    assert t00.supports(Ops.GET_FULL_TICK) is False, \
        "58600 实测没有 getFullTick，不能假装支持"

    t99 = XtqRpcTransport(port=59999)
    assert t99.known_methods() == frozenset(), "未知端口不假装支持任何方法"


def test_write_ops_are_rejected_without_touching_network():
    t = XtqRpcTransport(port=58670)
    rv = _invoke(t, Ops.PLACE_ORDER, {"code": "600036.SH"})
    assert rv.ok is False
    assert rv.error_type == "ReadOnlyTransport"
    assert "只读" in rv.error


def test_unknown_op_reports_unsupported():
    t = XtqRpcTransport(port=58670)
    rv = _invoke(t, "NOT_A_REAL_OP")
    assert rv.ok is False
    assert rv.error_type == "UnsupportedOp"


# ----------------------------------------------------------------------
# 端到端（真 socket）
# ----------------------------------------------------------------------
def test_probe_requires_tag_content_not_just_reply():
    """回包 ≠ 就绪：getTag 不返回 xtquant 标识必须判 NotReady。"""
    with FakeServer(lambda f, p: {"status": 0, "params": {"result": {"tag": "other"}}}) as srv:
        t = XtqRpcTransport(port=58670, host="127.0.0.1")
        t.port = srv.port
        rv = _invoke(t, Ops.PROBE)
    assert rv.ok is False
    assert rv.error_type == "NotReady"


def test_probe_ok_and_skips_heartbeats():
    with FakeServer(lambda f, p: {"status": 0, "params": {"result": {"tag": "xtquant", "version": "1.0"}}},
                    heartbeats=3) as srv:
        t = XtqRpcTransport(port=58670, host="127.0.0.1")
        t.port = srv.port
        rv = _invoke(t, Ops.PROBE)
    assert rv.ok is True, rv.error
    assert rv.result["tag"] == "xtquant"
    assert rv.result["port"] == srv.port


def test_error_id_becomes_failure_not_empty_result():
    with FakeServer(lambda f, p: {"params": {"ErrorID": ERR_NO_HANDLER,
                                             "ErrorMsg": "未找到处理函数"}}) as srv:
        t = XtqRpcTransport(port=58670, host="127.0.0.1")
        t.port = srv.port
        rv = _invoke(t, Ops.GET_FULL_TICK, {"stockCode": "600036.SH"})
    assert rv.ok is False
    assert str(ERR_NO_HANDLER) in rv.error
    assert "未找到处理函数" in rv.error


def test_result_extracted_on_success():
    with FakeServer(lambda f, p: {"status": 0, "params": {"result": {"600036.SH": {"lastPrice": 10.5}}}}) as srv:
        t = XtqRpcTransport(port=58670, host="127.0.0.1")
        t.port = srv.port
        rv = _invoke(t, Ops.GET_FULL_TICK, {"stockCode": "600036.SH"})
    assert rv.ok is True, rv.error
    assert rv.result["600036.SH"]["lastPrice"] == 10.5


def test_subscribe_sends_three_nested_documents():
    seen = {}

    def responder(func, params):
        seen["func"] = func
        seen["params"] = params
        return {"status": 0, "params": {"result": 1}}

    with FakeServer(responder) as srv:
        t = XtqRpcTransport(port=58670, host="127.0.0.1")
        t.port = srv.port
        rv = _invoke(t, Ops.SUBSCRIBE_QUOTE, {"stock_code": "600036.SH"})
    assert rv.ok is True, rv.error
    assert seen["func"] == "subscribeQuote"
    assert set(seen["params"]) == {"meta", "region", "param"}
    assert seen["params"]["meta"]["stockCode"] == "600036.SH"


def test_subscribe_rejects_bad_code_before_network():
    t = XtqRpcTransport(port=58670)
    rv = _invoke(t, Ops.SUBSCRIBE_QUOTE, {"stock_code": "600036"})
    assert rv.ok is False
    assert rv.error_type == "BadRequest"


# ----------------------------------------------------------------------
# 订阅抓取
# ----------------------------------------------------------------------
def test_capture_ack_is_not_quote_data():
    """★ 订阅应答（status/result）不是行情推送。

    旧实现把任何非心跳帧都算成 ``data``，于是"服务端受理了订阅 + 8 帧心跳"
    会被汇总成"有行情推送"。收盘后那次实测正是这个形状 —— 假绿会让人
    以为 tick 已通，从而跳过交易时段复跑。
    """
    chunks = [_heartbeat_frame(),
              _wire_frame(1, 3, 0x2042, doc={"status": 0, "params": {"result": 1}}),
              _heartbeat_frame()]
    with RawServer(chunks) as srv:
        t = XtqRpcTransport(port=58670, host="127.0.0.1")
        t.port = srv.port
        frames = t.capture("600036.SH", seconds=1.0)
    s = XtqRpcTransport.summarize_capture(frames)
    assert s["ack"] == 1 and s["quote"] == 0 and s["data"] == 0
    assert s["proven"] is False
    assert "订阅应答" in s["verdict"]


def test_capture_quote_push_is_proven():
    """真正带行情字段的推送帧才把 proven 置 True。"""
    push = _wire_frame(9, 3, 0x2042, doc={
        "func": "pushQuote", "params": {
            "stockCode": "600036.SH", "lastPrice": 38.51, "volume": 1200,
            "timetag": "2026-11-09 09:38:01",
        }})
    with RawServer([_heartbeat_frame(), push]) as srv:
        t = XtqRpcTransport(port=58670, host="127.0.0.1")
        t.port = srv.port
        frames = t.capture("600036.SH", seconds=1.0)
    s = XtqRpcTransport.summarize_capture(frames)
    assert s["quote"] == 1 and s["proven"] is True
    assert s["verdict"] == "有行情推送"
    assert s["key_sets"], "证据要带键集，便于事后复核"


def test_capture_unknown_push_is_not_claimed_as_quote():
    """键名未知的推送帧：既不能说通了，也不能说没推 —— 必须留键集让人工判。"""
    unknown = _wire_frame(7, 3, 0x2042, doc={"func": "onSomething", "args": [1, 2]})
    with RawServer([unknown]) as srv:
        t = XtqRpcTransport(port=58670, host="127.0.0.1")
        t.port = srv.port
        frames = t.capture("600036.SH", seconds=1.0)
    s = XtqRpcTransport.summarize_capture(frames)
    assert s["data_other"] == 1 and s["proven"] is False
    assert "需人工核对键集" in s["verdict"]
    assert s["key_sets"][0][0] == "data_other"


def test_frame_reader_keeps_partial_frame_across_timeouts():
    """★ 半帧 + socket.timeout 后不能丢已收字节（交易时段大帧必踩）。"""
    full = _wire_frame(3, 3, 0x2042, doc={"stockCode": "600036.SH", "lastPrice": 38.5})
    chunks = [full[:5], None, full[5:20], None, full[20:]]
    reader = _FrameReader(_ChunkSocket(chunks))
    assert reader.read() is None, "第一次超时：只收到 5 字节头，缓冲必须留着"
    assert reader.read() is None, "第二次超时：仍不足一帧"
    frame = reader.read()
    assert frame is not None and frame.seq == 3
    assert frame.decode()["stockCode"] == "600036.SH"


def test_frame_reader_rejects_bogus_length():
    """帧头长度荒谬时如实报错，不能无限追着 recv 跑。"""
    bogus = _HDR.pack(MAX_FRAME_BYTES + 1, 1, 3, 0x2042) + b"x" * 4
    reader = _FrameReader(_ChunkSocket([bogus]))
    with pytest.raises(BsonError, match="超过上限"):
        reader.read()


def test_capture_heartbeat_only_is_not_success():
    """收盘后只有心跳 —— 结论必须说"只有心跳"，不能算通。"""
    class HbOnly(FakeServer):
        def _handle(self, conn):
            with conn:
                try:
                    hdr = b""
                    while len(hdr) < 12:
                        c = conn.recv(12 - len(hdr))
                        if not c:
                            return
                        hdr += c
                    total, seq, _t, _f = parse_header(hdr)
                    body = b""
                    while len(body) < total - 12:
                        c = conn.recv(total - 12 - len(body))
                        if not c:
                            return
                        body += c
                    for _ in range(3):
                        conn.sendall(_HDR.pack(16, 0, 1, 0x0C40) + struct.pack(">I", 1))
                except OSError:
                    return

    with HbOnly(lambda f, p: {}) as srv:
        t = XtqRpcTransport(port=58670, host="127.0.0.1")
        t.port = srv.port
        frames = t.capture("600036.SH", seconds=1.5)
    s = XtqRpcTransport.summarize_capture(frames)
    assert s["data"] == 0
    assert s["heartbeat"] >= 3
    assert s["verdict"] == "只有心跳（订阅未生效或非交易时段）"


def test_get_transport_registry_knows_xtq_rpc():
    t = get_transport("xtq_rpc", port=58670)
    assert isinstance(t, XtqRpcTransport)
    assert t.transport_id == "xtq_rpc"
    assert t.supports_callback is False, "跨进程拿不到回调对象，不能声称支持回调订阅"


# --------------------------------------------------------------------------
# 拉取型应答的证据分级：代码表 ≠ 行情，ACK ≠ 行情，真 tick 不能被漏判
# --------------------------------------------------------------------------

def test_classify_payload_code_list_is_not_quote():
    """★ 5224 个代码字符串只是证券代码表，len>0 不是"行情已通"。"""
    got = classify_payload(["600036.SH", "000001.SZ"] * 10)
    assert got["kind"] == "code_list"
    assert got["size"] == 20
    assert got["quote_proven"] is False
    assert got["price_keys"] == []


def test_classify_payload_fulltick_shape_is_quote():
    """★ getFullTick 是「代码 → 字段字典」，外层键名不是 stockCode，不能漏判。"""
    got = classify_payload({
        "600036.SH": {"lastPrice": 38.5, "open": 38.0, "high": 39.0,
                      "low": 37.5, "volume": 12345,
                      "timetag": "2026-11-09 09:38:01"},
    })
    assert got["kind"] == "dict"
    assert got["quote_proven"] is True
    assert got["code_shaped_keys"] == ["600036.SH"]
    assert "lastprice" in got["price_keys"]


def test_classify_payload_kline_object_list_is_quote():
    got = classify_payload([
        {"time": "20261109", "open": 38.0, "high": 39.0,
         "low": 37.5, "close": 38.5, "volume": 12345},
    ])
    assert got["kind"] == "object_list"
    assert got["quote_proven"] is True
    assert got["key_sets"], "未识别结构要留键集供人工复核"


def test_classify_payload_empty_and_ack_are_not_quote():
    assert classify_payload([])["quote_proven"] is False
    assert classify_payload(None)["kind"] == "empty"
    ack = classify_payload({"status": 0, "params": {"result": []}})
    assert ack["quote_proven"] is False, "服务端受理查询 ≠ 有行情数据"


def test_classify_payload_nested_quote_marker_is_quote():
    got = classify_payload({"pushQuote": {"600036.SH": {"lastPrice": 38.5}}})
    assert got["markers"] == ["pushquote"]
    assert got["quote_proven"] is True

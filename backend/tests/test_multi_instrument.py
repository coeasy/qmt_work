# -*- coding: utf-8 -*-
"""P1+P2 多标的账户类型支持 + 代码格式校验（R18 引入）。

覆盖 5 类标的 × 3 级签名降级 × 6 类代码格式：
  account_type: stock / etf / future / option / credit
  signature    : 12-arg 扩展 / 11-arg 标准 / 10-arg 老券商
  code_format  : A股 60xxxx.SH / ETF 5xxxxx.SH / 期货 IF2312.SHF / 期权 100xxxxx.SH / 两融 60xxxx.SH

真实事故复盘：R17 之前 do_place 只支持 A 股 passorder 标准签名，
前端只能选「股票」一个 account_type。券商柜台一旦返「invalid instrument」
就无从定位。现在把 account_type 显式建模 + 前置校验 + 多签名降级。
"""
from __future__ import print_function

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "backend", "agent_bigqmt"))

from qmt_api import (  # noqa: E402
    ActionError,
    Executor,
    normalize_account_type,
    resolve_account_type,
    validate_code,
)


def _mk_executor(cfg=None, passorder=None):
    return Executor(cfg or {"trading_enabled": True},
                    {"passorder": passorder or (lambda *a: 7000)},
                    None)


# ---------------------------------------------------------------------------
# 账户类型别名归一化
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    ("stock", "stock"),
    ("etf", "etf"),
    ("fund_etf", "etf"),
    ("lof", "etf"),
    ("future", "future"),
    ("futures", "future"),
    ("期货", "future"),
    ("option", "option"),
    ("期权", "option"),
    ("credit", "credit"),
    ("margin", "credit"),
    ("两融", "credit"),
    ("CREDIT", "credit"),       # 大小写
    (" ETF ", "etf"),            # 空格
    ("", "stock"),
    (None, "stock"),
])
def test_normalize_account_type_aliases(raw, expected):
    assert normalize_account_type(raw) == expected


@pytest.mark.parametrize("bad", ["unknown_type", "futur", "futures_", "两融券", "0"])
def test_normalize_account_type_unknown_raises(bad):
    """★ R19 第 1 轮修正：未知 account_type **必须报错**，不得静默落到 A 股。

    R18 初版把未知值兜底成 'stock'，与「零 mock / 不静默吞错」纪律冲突：
    用户把 futures 写成 futur 时会被**当成 A 股送单**。credit 与 stock 共用同一套
    代码格式，代码校验兜不住 ⇒ 只能靠这里显式拒绝。
    """
    with pytest.raises(ValueError):
        normalize_account_type(bad)


def test_resolve_account_type_unknown_raises():
    with pytest.raises(ValueError):
        resolve_account_type("nope")


def test_do_place_unknown_account_type_is_broker_error():
    """未知 account_type 经 do_place → BrokerError（400 + 真因），而不是静默成交。"""
    ex = _mk_executor()
    res = ex.execute({"op": "PLACE", "params": {
        "side": "buy", "stock_code": "600000.SH", "price_type": "limit",
        "price": 10.0, "volume": 100, "account_type": "nope"}})
    assert res["ok"] is False
    assert res["error_type"] == "BrokerError"
    assert "account_type" in res["error"]


# ---------------------------------------------------------------------------
# 账户类型 → opAccountType 数值映射
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name,op", [
    ("stock", 0),
    ("etf", 1),
    ("option", 2),
    ("future", 3),
    ("credit", 4),
])
def test_resolve_account_type_default_map(name, op):
    key, value = resolve_account_type(name)
    assert key == name
    assert value == op


def test_resolve_account_type_config_override():
    """券商版本可以覆盖 opAccountType_* 数值。"""
    po = {"opAccountType_future": 99}
    key, value = resolve_account_type("future", po=po)
    assert key == "future"
    assert value == 99


# ---------------------------------------------------------------------------
# 代码格式校验（每种标的的正则）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("code,at,ok", [
    # A 股
    ("600036.SH", "stock", True),
    ("601398.SH", "stock", True),
    ("000001.SZ", "stock", True),
    ("300059.SZ", "stock", True),
    ("688981.SH", "stock", True),  # 科创板
    ("301001.SZ", "stock", True),  # 创业板
    ("832000.SH", "stock", True),  # 北交所
    ("430047.NQ", "stock", False),  # 北交所后缀不匹配（当前只支持 .SH/.SZ）
    ("INVALID", "stock", False),
    # ETF
    ("510300.SH", "etf", True),
    ("159915.SZ", "etf", True),
    ("510050.SH", "etf", True),
    ("600036.SH", "etf", False),  # A 股代码给 ETF 校验 → 拒绝
    # 期货
    ("IF2312.SHF", "future", True),
    ("AU2402.SHF", "future", True),
    ("M2401.DCE", "future", True),
    ("SR2405.CZCE", "future", True),
    ("IF2312.BAD", "future", False),
    # 期权
    ("10005847.SH", "option", True),
    ("02000031.SZ", "option", True),
    ("600036.SH", "option", False),
    # 两融
    ("600036.SH", "credit", True),
    ("IF2312.SHF", "credit", False),
])
def test_validate_code_patterns(code, at, ok):
    assert validate_code(code, at)[0] is ok


def test_validate_code_error_message():
    ok, err = validate_code("INVALID", "stock")
    assert ok is False
    assert "code='INVALID'" in err
    assert "stock" in err


# ---------------------------------------------------------------------------
# do_place 分发 + 3 级签名降级
# ---------------------------------------------------------------------------
def test_place_stock_default_uses_11arg():
    """A 股默认走标准 11-arg 签名，不带 account_type 参数。"""
    calls = []
    ex = _mk_executor(passorder=lambda *a: calls.append(a) or 7000)
    r = ex.do_place({"stock_code": "600036.SH", "side": "buy",
                     "price": 35.0, "volume": 100})
    assert r["order_id"] == "7000"
    assert r["account_type"] == "stock"
    assert len(calls[-1]) == 11
    assert r.get("extended_signature_fallback") is None


def test_place_etf_uses_12arg_extended():
    """ETF 走扩展 12-arg 签名，末位是 opAccountType=1。"""
    calls = []
    ex = _mk_executor(passorder=lambda *a: calls.append(a) or 7000)
    r = ex.do_place({"stock_code": "510300.SH", "side": "buy",
                     "price": 3.5, "volume": 100, "account_type": "etf"})
    assert r["order_id"] == "7000"
    assert r["account_type"] == "etf"
    assert len(calls[-1]) == 12
    assert calls[-1][-1] == 1  # opAccountType_etf


def test_place_future_chinese_alias():
    """期货中文别名 → 归一化到 future，opAccountType=3。"""
    calls = []
    ex = _mk_executor(passorder=lambda *a: calls.append(a) or 7000)
    r = ex.do_place({"stock_code": "IF2312.SHF", "side": "buy",
                     "price": 3800.0, "volume": 1, "account_type": "期货"})
    assert r["account_type"] == "future"
    assert calls[-1][-1] == 3


def test_place_option():
    calls = []
    ex = _mk_executor(passorder=lambda *a: calls.append(a) or 7000)
    r = ex.do_place({"stock_code": "10005847.SH", "side": "sell",
                     "price": 0.5, "volume": 1, "account_type": "option"})
    assert r["account_type"] == "option"
    assert calls[-1][-1] == 2


def test_place_credit():
    calls = []
    ex = _mk_executor(passorder=lambda *a: calls.append(a) or 7000)
    r = ex.do_place({"stock_code": "600036.SH", "side": "buy",
                     "price": 35.0, "volume": 100, "account_type": "credit"})
    assert r["account_type"] == "credit"
    assert calls[-1][-1] == 4


# ---------------------------------------------------------------------------
# 3 级签名降级
# ---------------------------------------------------------------------------
def test_place_fallback_12to11():
    """12-arg 被拒 → 降级到 11-arg，标记 fallback_used=True。"""
    calls = []
    def po(*a):
        calls.append(a)
        if len(a) != 11:
            raise TypeError("need 11 got %d" % len(a))
        return 7001
    ex = _mk_executor(passorder=po)
    r = ex.do_place({"stock_code": "510300.SH", "side": "buy",
                     "price": 3.5, "volume": 100, "account_type": "etf"})
    assert r["order_id"] == "7001"
    assert r["extended_signature_fallback"] is True
    assert len(calls[-1]) == 11


def test_place_fallback_12to11to10():
    """12 和 11 都拒 → 降级到 10-arg（老券商无 ContextInfo 形参）。"""
    calls = []
    def po(*a):
        calls.append(a)
        if len(a) != 10:
            raise TypeError("need 10 got %d" % len(a))
        return 7002
    ex = _mk_executor(passorder=po)
    r = ex.do_place({"stock_code": "510300.SH", "side": "buy",
                     "price": 3.5, "volume": 100, "account_type": "etf"})
    assert r["order_id"] == "7002"
    assert r["extended_signature_fallback"] is True
    assert len(calls[-1]) == 10


def test_place_fallback_all_fail_raises():
    """三层全失败 → 抛 TypeError，让 execute() 转成 BrokerSDKError 返回外部端。"""
    def po(*a):
        raise TypeError("unsupported signature: %d args" % len(a))
    ex = _mk_executor(passorder=po)
    with pytest.raises(TypeError):
        ex.do_place({"stock_code": "510300.SH", "side": "buy",
                     "price": 3.5, "volume": 100, "account_type": "etf"})


# ---------------------------------------------------------------------------
# 代码格式错直接 BrokerError（不下单）
# ---------------------------------------------------------------------------
def test_place_code_format_error():
    ex = _mk_executor()
    with pytest.raises(ActionError) as ei:
        ex.do_place({"stock_code": "INVALID", "side": "buy",
                     "price": 3.5, "volume": 100, "account_type": "stock"})
    assert ei.value.error_type == "BrokerError"
    assert "code='INVALID'" in str(ei.value)


# ---------------------------------------------------------------------------
# 元数据能力面（供前端下拉框渲染）
# ---------------------------------------------------------------------------
def test_meta_exposes_account_types():
    ex = _mk_executor()
    meta = ex.meta()
    assert meta["account_types"] == {"stock": 0, "etf": 1, "option": 2,
                                     "future": 3, "credit": 4}
    assert meta["default_account_type"] == "stock"


def test_meta_account_types_config_override():
    """券商版本覆盖 opAccountType → 元数据也要如实反映。"""
    cfg = {"trading_enabled": True,
           "passorder": {"opAccountType_future": 99}}
    ex = _mk_executor(cfg=cfg)
    meta = ex.meta()
    assert meta["account_types"]["future"] == 99
    # 其它保持默认
    assert meta["account_types"]["stock"] == 0
    assert meta["account_types"]["etf"] == 1


# ---------------------------------------------------------------------------
# Agent 未开启 trading_enabled → 直接拒绝（P0 安全阀不变）
# ---------------------------------------------------------------------------
def test_place_shield_blocked():
    cfg = {"trading_enabled": False}
    ex = _mk_executor(cfg=cfg)
    with pytest.raises(ActionError) as ei:
        ex.do_place({"stock_code": "600036.SH", "side": "buy",
                     "price": 35.0, "volume": 100, "account_type": "stock"})
    assert ei.value.error_type == "Rejected"


# ---------------------------------------------------------------------------
# P0 回归（R19 第 1 轮）：passorder 是 void，正常**返回 None**
# ---------------------------------------------------------------------------
def test_place_void_return_is_success_not_failure():
    """★ QMT ``passorder`` 正常返回 None（void）。绝不能把 None 当失败哨兵。

    R18 初版用 ``ret is None`` 判「三层签名全失败」，于是：
      * 首签成功但返回 None → ``raise last_err``，而 last_err 也是 None
        → ``TypeError: exceptions must derive from BaseException``；
      * 一单**已送达柜台**的委托被报成 BrokerSDKError，用户重试即**重复委托**。
    本用例锁死「返回 None = 成功（只是拿不到委托号）」。
    """
    ex = _mk_executor(passorder=lambda *a: None)
    r = ex.do_place({"stock_code": "600036.SH", "side": "buy",
                     "price": 35.0, "volume": 100})
    assert r["order_id"] == ""            # 如实回空，不伪造柜台号
    assert r["status"] == "unknown"       # 诚实状态：未确认
    assert r["account_type"] == "stock"
    assert r["client_order_id"]           # 平台侧关联号仍在（对账锚点）


def test_place_void_return_after_fallback_is_success():
    """降级到 11-arg 后返回 None 也要判成功（不得把旧 TypeError 冒出来）。"""
    seen = []

    def po(*a):
        seen.append(len(a))
        if len(a) == 12:
            raise TypeError("no extended signature")
        return None

    ex = _mk_executor(passorder=po)
    r = ex.do_place({"stock_code": "510300.SH", "side": "buy",
                     "price": 3.5, "volume": 100, "account_type": "etf"})
    assert seen == [12, 11]
    assert r["status"] == "unknown"
    assert r["extended_signature_fallback"] is True


def test_place_execute_envelope_ok_on_void_return():
    """经 execute() 信封：返回 None 也不得产出 error/BrokerSDKError。"""
    ex = _mk_executor(passorder=lambda *a: None)
    res = ex.execute({"op": "PLACE", "params": {
        "side": "buy", "stock_code": "600036.SH", "price_type": "limit",
        "price": 35.0, "volume": 100}})
    assert res["ok"] is True, res
    assert res["result"]["status"] == "unknown"

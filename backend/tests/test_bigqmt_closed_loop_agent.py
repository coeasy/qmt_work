# -*- coding: utf-8 -*-
"""闭环模式的**策略端控制流**契约测试（新规形态，2026-10-31）。

为什么这组测试必须存在
----------------------
2026-10-23 起，策略沙箱内 ``open()`` 直接抛 ``PermissionError: Forbidden
FileIO``，连 ``127.0.0.1`` 的回环网络都算外联（处置是**停模 + 防外挂弹窗**）。
于是旧 agent 的三个支点同时失效：读配置、扫 req/、写 events.ndjson。

新控制流一旦写错，症状都是**真机才炸**，而且很难看：

* 在闭环里退回写文件 ⇒ 每拍抛 Forbidden FileIO，外部只看到「策略在跑但没有
  任何事件」—— 一个标准假绿灯；
* 拿不到柜台就当作空仓 ⇒ 按「目标 50%」重复建仓，真金白银；
* 在途委托不扣 ⇒ 3 秒一拍、5 小时窗口，同一目标连发上千张委托；
* 计划过期仍执行 ⇒ 照着昨天的权重调仓，一切正常但持续亏钱。

所以这里锁的都是「静默错误」而不是「会不会崩」。
"""
from __future__ import annotations

import importlib.util
import json
import sys
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "backend"))

import gen_qmt_agent_bundle as gen  # noqa: E402
from connectors.bigqmt_log_tail import LogTail  # noqa: E402

ACCOUNT = "8888"
NAV = 1000000.0
PRICE = 4.0

PLAN = {
    "id": "p-test-01",
    "strat": "etf_rot",
    "window": ["00:00", "23:59"],
    "rule": {"type": "rebalance",
             "target": [{"code": "513100.SH", "w": 0.5},
                        {"code": "511990.SH", "w": 0.5}]},
    # ★ max_turnover=1.0：从空仓建满仓本身就是 100% 换手，闸门值必须容得下，
    #   否则「计划永远被 DROP」—— 那是配置问题，但很容易被误读成策略坏了。
    "guard": {"max_orders": 8, "max_turnover": 1.0, "stop_dd": 0.0},
}

CFG = {"account_id": ACCOUNT, "account_type": "etf", "trading_enabled": True}


# ---------------------------------------------------------------------------
# 夹具：真生成一份带计划的 bundle，再按文件路径导入
# ---------------------------------------------------------------------------
class FakeBroker(object):
    """柜台替身：形如 QMT 注入的 ``get_trade_detail_data`` / ``passorder``。"""

    def __init__(self, nav=NAV, positions=None, orders=None, price=PRICE):
        self.nav = nav
        self.positions = list(positions or [])
        self.orders = list(orders or [])
        self.price = price
        self.placements = []
        self.position_none = False   # 模拟「查询失败返回 None」
        self.position_raises = False

    # -- 注入面 ---------------------------------------------------------
    def get_trade_detail_data(self, account, account_type, kind):
        if kind == "POSITION":
            if self.position_raises:
                raise RuntimeError("柜台连接断开")
            if self.position_none:
                return None
            return self.positions
        if kind == "ACCOUNT":
            return [{"m_dBalance": self.nav}] if self.nav else []
        if kind == "ORDER":
            return self.orders
        return None

    def passorder(self, *args):
        # 备注位（第 10 个位置参数，index 9）= cid，是跨进程对账的唯一锚点
        self.placements.append({"args": args, "cid": args[9] if len(args) > 9 else ""})
        return ["OID%d" % len(self.placements)]

    def get_full_tick(self, codes):
        return dict((c, {"lastPrice": self.price}) for c in codes)


class FakeCtx(object):
    def __init__(self, broker):
        self._broker = broker

    def get_full_tick(self, codes):
        return self._broker.get_full_tick(codes)


def _load(path: Path, tag=""):
    name = "qmt_closed_%s_%s" % (tag, uuid.uuid4().hex)
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _bundle(tmp_path, plan=None, cfg=None, tag=""):
    """生成一份 bundle 文件并导入（每份独立模块，``_STATE`` 互不污染）。"""
    plan = PLAN if plan is None else plan
    cfg = CFG if cfg is None else cfg
    text = gen.build(embed=cfg, stamp="test", plan=plan, bundle_id="b-test")
    out = tmp_path / ("qmt_agent_%s.py" % (tag or uuid.uuid4().hex[:8]))
    out.write_text(text, encoding="utf-8", newline="\n")
    return _load(out, tag), out


def _wire(module, broker):
    """把柜台替身塞进 bundle 的 globals —— 模拟终端注入。

    ★ 必须在 import **之后**赋值：``capture_qmt_injected_funcs`` 只认
      ``globals()`` 里的可调用对象，且它们的 ``__module__`` 不是本 bundle
      （否则会被当成「我们自己定义的」而排除）。
    """
    module.get_trade_detail_data = broker.get_trade_detail_data
    module.passorder = broker.passorder
    return FakeCtx(broker)


def _lines(capsys):
    """取出已打印的 ``@@QMTW|`` 记录（解析成 dict）。"""
    out = capsys.readouterr().out
    records = []
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("@@QMTW|"):
            continue
        records.append(json.loads(line[len("@@QMTW|"):]))
    return records


def _kinds(records):
    return [r["k"] for r in records]


def _of(records, kind):
    return [r for r in records if r["k"] == kind]


# ---------------------------------------------------------------------------
# 1. 模式判定：只有烘焙了计划才走闭环
# ---------------------------------------------------------------------------
def test_mode_is_bridge_without_embedded_plan(tmp_path):
    """没有 ``EMBEDDED_PLAN`` ⇒ 仍是旧文件桥模式（新规生效前还要能用）。"""
    module, _ = _bundle(tmp_path, plan=None)
    # 显式抹掉计划，模拟「没给 --plan 的产物」
    module.EMBEDDED_PLAN = None
    assert module._closed_loop() is False
    assert module._STATE["mode"] == "bridge"


def test_mode_requires_non_empty_plan(tmp_path):
    """空 dict 不算计划 —— 否则会走进一条「没有计划却自称闭环」的死路。"""
    module, _ = _bundle(tmp_path)
    module.EMBEDDED_PLAN = {}
    assert module._closed_loop() is False


# ---------------------------------------------------------------------------
# 2. 配置：闭环模式一次磁盘访问都不做
# ---------------------------------------------------------------------------
def test_closed_load_config_ignores_disk(tmp_path):
    """同目录放一份**诱饵** agent_config.json，闭环模式必须完全无视它。

    旧 ``load_config()`` 的第一句是 ``os.path.exists`` + ``open``，在新规下
    直接抛 ``PermissionError: Forbidden FileIO``，而它发生在 ``init()`` 里
    ⇒ 策略「启动即停止」，日志里连原因都没有。
    """
    module, out = _bundle(tmp_path)
    decoy = out.parent / "agent_config.json"
    decoy.write_text(json.dumps({"bridge_dir": "D:/decoy", "account_id": "9999"}),
                     encoding="utf-8")

    cfg = module.load_config()
    assert cfg["_config_path"] == "<embedded:closed-loop>"
    assert cfg["account_id"] == ACCOUNT, "读了磁盘上的诱饵配置"
    assert "bridge_dir" not in cfg or cfg.get("bridge_dir") != "D:/decoy"


def test_closed_config_requires_account_id(tmp_path):
    """没有 account_id ⇒ 响亮失败（不猜账户）。

    猜一个账户 = 拿别人的持仓去调仓，是这整套设计里最贵的一种错。
    """
    module, _ = _bundle(tmp_path, cfg={"trading_enabled": True})
    with pytest.raises(RuntimeError) as exc:
        module.load_config()
    assert "account_id" in str(exc.value)


# ---------------------------------------------------------------------------
# 3. init：校验计划 → 柜台重建 → 只读或可交易
# ---------------------------------------------------------------------------
def test_init_rebuilds_positions_from_counter(tmp_path, capsys):
    module, _ = _bundle(tmp_path)
    broker = FakeBroker(positions=[
        {"m_strInstrumentID": "513100", "m_strExchangeID": "SH",
         "m_nVolume": 1000, "m_nCanUseVolume": 800}])
    ctx = _wire(module, broker)
    module.init(ctx)

    records = _lines(capsys)
    assert module._STATE["mode"] == "closed"
    assert module._STATE["readonly_reason"] == ""
    assert module._STATE["positions"].snapshot()["513100.SH"] == {
        "volume": 1000, "available": 800}
    boot = _of(records, "BOOT")[0]["d"]
    assert boot["plan_id"] == "p-test-01"
    assert boot["positions"] == 1
    assert boot["nav"] == NAV
    assert boot["trading_enabled"] is True
    assert "get_trade_detail_data" in boot["injected"]
    assert boot["bundle"] == "b-test"
    assert boot["boot_id"], "boot_id 是 seq 去重的前提，不能为空"


def test_init_counter_none_is_readonly_not_flat(tmp_path, capsys):
    """柜台返回 None ⇒ **只读**，绝不当作空仓。

    把「查询失败」当成「没有持仓」，会按目标权重把仓位再建一遍 ——
    这是新规改造里最贵的单个 bug。
    """
    module, _ = _bundle(tmp_path)
    broker = FakeBroker()
    broker.position_none = True
    module.init(_wire(module, broker))

    assert module._STATE["readonly_reason"], "柜台查询失败必须进只读"
    assert module._STATE["fatal_reason"] == "", "柜台失败是暂时的，不该判死"
    kinds = _kinds(_lines(capsys))
    assert "READONLY" in kinds


def test_init_counter_raises_is_readonly(tmp_path, capsys):
    module, _ = _bundle(tmp_path)
    broker = FakeBroker()
    broker.position_raises = True
    module.init(_wire(module, broker))
    assert "柜台持仓查询失败" in module._STATE["readonly_reason"]
    assert "READONLY" in _kinds(_lines(capsys))


def test_init_without_query_func_is_readonly(tmp_path, capsys):
    """独立进程模式拿不到柜台接口 ⇒ 只读，且原因必须点明「需公式模式」。"""
    module, _ = _bundle(tmp_path)
    module.init(FakeCtx(FakeBroker()))   # 不注入任何函数
    reason = module._STATE["readonly_reason"]
    assert "公式模式" in reason
    assert module._STATE["fatal_reason"], "拿不到注入面重试无意义 ⇒ 判死"
    assert "READONLY" in _kinds(_lines(capsys))


def test_init_expired_plan_is_readonly(tmp_path, capsys):
    """计划日期 ≠ 今日 ⇒ 只读。**这一条防的是「照着昨天的权重调仓」。**"""
    plan = dict(PLAN)
    plan["date"] = "2000-01-01"
    module, _ = _bundle(tmp_path, plan=plan)
    module.init(_wire(module, FakeBroker()))
    assert "计划日期" in module._STATE["readonly_reason"]
    assert module._STATE["fatal_reason"]
    boot = _of(_lines(capsys), "BOOT")[0]["d"]
    assert boot["fatal"]


def test_init_invalid_plan_never_raises(tmp_path, capsys):
    """计划非法 ⇒ 只读 + 回传原因，**绝不抛异常**。

    抛出去 = 策略「启动即停止」，用户只能在模型交易里看到闪一下。
    """
    plan = dict(PLAN)
    plan["rule"] = {"type": "rebalance",
                    "target": [{"code": "513100.SH", "w": 0.9},
                               {"code": "511990.SH", "w": 0.9}]}
    module, _ = _bundle(tmp_path, plan=plan)
    module.init(_wire(module, FakeBroker()))   # 不抛即通过
    assert "计划校验失败" in module._STATE["readonly_reason"]
    assert "权重合计" in module._STATE["readonly_reason"]


# ---------------------------------------------------------------------------
# 4. 回传：闭环只走日志行，一个文件都不写
# ---------------------------------------------------------------------------
def test_closed_mode_writes_no_files(tmp_path, capsys):
    """闭环模式跑完 init + tick 后，目录里**不能多出任何文件**。

    这是新规的核心：任何一次 ``open(..., 'w')`` 都会抛 Forbidden FileIO，
    而它会被主循环的 try/except 吞成「每拍一条异常」——外部看到的是
    「策略在跑但没有事件」，一个典型假绿灯。
    """
    module, out = _bundle(tmp_path)
    before = set(p.name for p in tmp_path.iterdir())
    broker = FakeBroker()
    ctx = _wire(module, broker)
    module.init(ctx)
    module.tick(time.time() + 10)
    after = set(p.name for p in tmp_path.iterdir())
    assert after == before, "闭环模式写出了文件: %s" % sorted(after - before)
    assert "events.ndjson" not in after
    assert "agent_status.json" not in after
    assert "probe_result.json" not in after
    assert _kinds(_lines(capsys)), "没有任何回传记录 —— 等于外部完全瞎了"


def test_emitted_lines_round_trip_through_log_tail(tmp_path, capsys):
    """``@@QMTW|`` 是**跨进程契约**：bundle 打出来的行必须能被后端 LogTail 解析。

    两边的 PREFIX / 字段名任一漂移，外部就只会看到「日志里有乱码行」。
    """
    module, _ = _bundle(tmp_path)
    broker = FakeBroker()
    module.init(_wire(module, broker))
    module.tick(time.time() + 10)

    raw = capsys.readouterr().out
    tail = LogTail()
    records = tail.feed(raw)
    kinds = [r.kind for r in records]
    assert "BOOT" in kinds
    assert "STATUS" in kinds
    assert tail.stats.bad == 0, "bundle 打出了 LogTail 解析不了的行"
    assert tail.stats.parsed == len(records) + tail.stats.duplicates
    # boot_id 必须被带出来（否则重启后 seq 归零会被当成重复记录丢掉）
    assert all(r.boot_id for r in records)
    assert len(set(r.boot_id for r in records)) == 1


# ---------------------------------------------------------------------------
# 5. tick：意图 → 闸门 → passorder → 在途
# ---------------------------------------------------------------------------
def test_tick_places_orders_with_cid_anchor(tmp_path, capsys):
    module, _ = _bundle(tmp_path)
    broker = FakeBroker()
    ctx = _wire(module, broker)
    module.init(ctx)
    capsys.readouterr()
    module.tick(time.time() + 10)

    assert len(broker.placements) == 2
    cids = [p["cid"] for p in broker.placements]
    assert all(c.startswith("cw") for c in cids), cids
    assert len(set(cids)) == 2, "cid 必须唯一（它是跨进程对账锚点）"
    orders = _of(_lines(capsys), "ORDER")
    assert len(orders) == 2
    assert set(o["d"]["cid"] for o in orders) == set(cids)
    # 125000 股 × 4.0 = 50 万 = 净值 50%
    assert {o["d"]["volume"] for o in orders} == {125000}
    assert set(module._STATE["pending"].keys()) == set(cids)


def test_tick_suppressed_by_pending(tmp_path, capsys):
    """成交前重复决策必须被在途表吃掉 —— 否则一天能发上千张重复委托。"""
    module, _ = _bundle(tmp_path)
    broker = FakeBroker()
    ctx = _wire(module, broker)
    module.init(ctx)
    module.tick(time.time() + 10)
    first = len(broker.placements)
    module.tick(time.time() + 20)
    module.tick(time.time() + 30)
    assert len(broker.placements) == first, "在途未核销却又发了新单"


def test_tick_outside_window_does_nothing(tmp_path, capsys):
    plan = dict(PLAN)
    plan["window"] = ["09:35", "09:36"]     # 几乎不可能正好命中
    module, _ = _bundle(tmp_path, plan=plan)
    broker = FakeBroker()
    module.init(_wire(module, broker))
    module.tick(time.time() + 10)
    if not module.in_window(module._STATE["plan"], module.hhmm_of(time.time() + 10)):
        assert broker.placements == []


def test_tick_guard_blocks_and_reports_reason(tmp_path, capsys):
    """闸门裁剪必须**留原因**（否则用户只看到「少下了几笔」，查不出为什么）。"""
    plan = dict(PLAN)
    plan["guard"] = {"max_orders": 8, "max_turnover": 0.3, "stop_dd": 0.0}
    module, _ = _bundle(tmp_path, plan=plan)
    broker = FakeBroker()
    module.init(_wire(module, broker))
    capsys.readouterr()
    module.tick(time.time() + 10)
    assert broker.placements == [], "换手上限 30% 时不该建 100% 仓"
    drops = _of(_lines(capsys), "DROP")
    assert drops and "max_turnover" in drops[0]["d"]["dropped"][0]["reason"]


def test_tick_skips_when_nav_unknown(tmp_path, capsys):
    """拿不到净值 ⇒ 不动，且说清「净值未知（不是零资产）」。"""
    module, _ = _bundle(tmp_path)
    broker = FakeBroker(nav=0.0)      # ACCOUNT 查询返回空列表 ⇒ 净值未知
    module.init(_wire(module, broker))
    module.tick(time.time() + 10)
    assert broker.placements == [], "净值未知却下了单"
    warns = _of(_lines(capsys), "WARN")
    assert warns, "净值未知必须有一条 WARN（否则外部只看到「策略在跑但不动」）"
    assert "总资产" in warns[0]["d"]["reason"]


def test_tick_skips_code_without_price(tmp_path, capsys):
    """没价格的标的**不参与决策**：用 0 价算出来的股数是无穷大。"""
    module, _ = _bundle(tmp_path)
    broker = FakeBroker()

    def half(codes):
        return {"513100.SH": {"lastPrice": PRICE}}     # 511990.SH 没价

    broker.get_full_tick = half
    module.init(_wire(module, broker))
    capsys.readouterr()
    module.tick(time.time() + 10)
    assert [p["args"][3] for p in broker.placements] == ["513100.SH"]
    warns = _of(_lines(capsys), "WARN")
    assert any("511990.SH" in w["d"].get("codes", []) for w in warns)


# ---------------------------------------------------------------------------
# 6. 在途三态：查不到 ⇒ 允许重决策；久不成交 ⇒ 停机；查询失败 ⇒ 不下结论
# ---------------------------------------------------------------------------
def test_pending_lost_allows_replan(tmp_path, capsys):
    """柜台委托列表里查不到该 cid ⇒ 允许重新决策（**唯一**允许重发的情形）。"""
    plan = dict(PLAN)
    # 放开闸门：本用例只验证「在途核销」，不验证风控裁剪
    plan["guard"] = {"max_orders": 20, "max_turnover": 10.0, "stop_dd": 0.0}
    module, _ = _bundle(tmp_path, plan=plan)
    broker = FakeBroker()          # ORDER 返回 [] ⇒ 查不到
    module.PENDING_GRACE_S = 0.0
    ctx = _wire(module, broker)
    module.init(ctx)
    module.tick(time.time() + 10)
    assert len(broker.placements) == 2
    capsys.readouterr()
    module.tick(time.time() + 20)
    assert "LOST" in _kinds(_lines(capsys))
    # 允许重新决策 ⇒ 又发了两张（max_orders 是这行为的兜底上限，不是无限重试）
    assert len(broker.placements) == 4


def test_pending_stale_halts_instead_of_resending(tmp_path, capsys):
    """委托查得到但久不成交 ⇒ 停机，**绝不自动重发**。"""
    module, _ = _bundle(tmp_path)
    broker = FakeBroker()
    ctx = _wire(module, broker)
    module.init(ctx)
    module.tick(time.time() + 10)
    cid = broker.placements[0]["cid"]
    broker.orders = [{"m_strRemark": cid, "m_nVolume": 125000}]
    module.PENDING_STALE_S = 0.0
    capsys.readouterr()
    module.tick(time.time() + 20)
    kinds = _kinds(_lines(capsys))
    assert "HALT" in kinds
    assert module._STATE["guard"].halted
    assert len(broker.placements) == 2, "停机后不得再发单"


def test_pending_untouched_when_order_query_fails(tmp_path, capsys):
    """委托查询失败（None）⇒ **什么结论都不下**：不放行重发，也不停机。"""
    module, _ = _bundle(tmp_path)
    broker = FakeBroker()
    ctx = _wire(module, broker)
    module.init(ctx)
    module.tick(time.time() + 10)
    pending_before = dict(module._STATE["pending"])
    assert pending_before

    def boom(*args):
        raise RuntimeError("柜台忙")

    module._STATE["query"] = boom
    module.PENDING_GRACE_S = 0.0
    module.PENDING_STALE_S = 0.0
    module.tick(time.time() + 20)
    assert module._STATE["pending"] == pending_before, "查询失败却动了在途表"


# ---------------------------------------------------------------------------
# 7. 成交回报：持仓增量 + 在途核销
# ---------------------------------------------------------------------------
def test_deal_callback_updates_positions_and_settles_pending(tmp_path, capsys):
    module, _ = _bundle(tmp_path)
    broker = FakeBroker()
    ctx = _wire(module, broker)
    module.init(ctx)
    module.tick(time.time() + 10)
    cid = broker.placements[0]["cid"]
    capsys.readouterr()

    module.deal_callback(ctx, {
        "m_strInstrumentID": "513100", "m_strExchangeID": "SH",
        "m_nVolume": 125000, "m_strRemark": cid, "side": "BUY"})
    assert module._STATE["positions"].volume_of("513100.SH") == 125000
    assert cid not in module._STATE["pending"], "成交后必须核销在途"
    assert "TRADE" in _kinds(_lines(capsys))


def test_order_callback_is_reported(tmp_path, capsys):
    module, _ = _bundle(tmp_path)
    broker = FakeBroker()
    ctx = _wire(module, broker)
    module.init(ctx)
    capsys.readouterr()
    module.order_callback(ctx, {"m_strInstrumentID": "513100", "m_strExchangeID": "SH",
                                "m_strRemark": "cw12345671", "m_nVolume": 100})
    records = _of(_lines(capsys), "ORDER")
    assert records and records[0]["d"]["event"] == "callback"


# ---------------------------------------------------------------------------
# 8. 生成器：内联完整性 + 计划烘焙
# ---------------------------------------------------------------------------
def test_bundle_inlines_closed_loop_modules():
    text = gen.build(stamp="test")
    assert "BEGIN position_source.py" in text
    assert "BEGIN closed_loop.py" in text
    assert gen.check(text) == []
    for name in ("PositionSource", "parse_plan", "decide", "emit", "tick"):
        assert name in text


def test_bundle_embeds_plan_and_bundle_id():
    text = gen.build(embed=CFG, stamp="test", plan=PLAN, bundle_id="b-xyz")
    assert gen.check(text) == []
    assert "EMBEDDED_PLAN = None" not in text
    assert "p-test-01" in text
    assert "EMBEDDED_BUNDLE_ID = 'b-xyz'" in text


def test_embedded_plan_is_python_literal_not_json():
    """计划必须用 **Python 字面量**渲染：JSON 的 true/false/null 在源码里是
    未定义名字 ⇒ 策略一启动就 NameError（部署变事故）。"""
    text = gen.build(embed=CFG, stamp="test", plan=PLAN, bundle_id="b")
    assert "EMBEDDED_PLAN = {'id': 'p-test-01'" in text
    assert ": true" not in text.split("EMBEDDED_PLAN = ")[1].split("\n\n")[0]


# ---------------------------------------------------------------------------
# 9. 自愈：回调不触发时靠**持仓**核销在途（本轮最重要的修复）
# ---------------------------------------------------------------------------
def test_position_resync_settles_pending_without_callback(tmp_path, capsys):
    """``deal_callback`` 不触发时，在途必须靠**持仓变化**销账。

    现场：部分终端/版本只推委托、不推成交。只靠回调核销 ⇒ 在途一直挂着 ⇒
    300 秒后被判「久不成交」⇒ 停机。而事实是早就成交了。
    症状极具欺骗性：策略跑一整天只下了第一轮单，日志里只有一条 HALT。
    """
    module, _ = _bundle(tmp_path)
    # 默认 60s 才对齐一次；用例只推进 10s，这里放开节流（生产默认值不动）
    module.POSITION_RESYNC_S = 0.0
    broker = FakeBroker()
    ctx = _wire(module, broker)
    module.init(ctx)
    module.tick(time.time() + 10)
    assert len(module._STATE["pending"]) == 2
    capsys.readouterr()

    # 只改柜台持仓，**不调** deal_callback —— 模拟回调丢了
    broker.positions = [{"m_strInstrumentID": "513100", "m_strExchangeID": "SH",
                         "m_nVolume": 125000, "m_nCanUseVolume": 125000}]
    module.tick(time.time() + 20)

    pending = module._STATE["pending"]
    assert len(pending) == 1, "已成交那一腿必须被持仓销账"
    assert list(pending.values())[0]["code"] == "511990.SH"
    records = _of(_lines(capsys), "SETTLED")
    assert records and records[0]["d"]["by"] == "position"
    assert records[0]["d"]["pos_now"] == 125000


def test_resync_failure_skips_tick_without_readonly_flapping(tmp_path, capsys):
    """运行中柜台查询失败 ⇒ 跳过本拍并播报一次，**不**整体转只读。

    整体转只读会每拍刷 READONLY/RECOVER 两条，把真正的问题埋掉；
    而「跳过本拍」在语义上已经足够安全（不交易）。
    """
    module, _ = _bundle(tmp_path)
    module.POSITION_RESYNC_S = 0.0
    broker = FakeBroker()
    ctx = _wire(module, broker)
    module.init(ctx)
    module.tick(time.time() + 10)
    placed = len(broker.placements)
    broker.position_raises = True
    capsys.readouterr()
    module.tick(time.time() + 20)
    assert len(broker.placements) == placed, "持仓查不到却继续下单"
    assert module._STATE["readonly_reason"] == "", "不该整体转只读"
    warns = _of(_lines(capsys), "WARN")
    assert any("重新对齐失败" in w["d"].get("reason", "") for w in warns)


# ---------------------------------------------------------------------------
# 10. 拒单必须归还换手额度（否则一个配置错就烧光当天预算）
# ---------------------------------------------------------------------------
def test_reject_refunds_turnover_but_not_order_count(tmp_path, capsys):
    module, _ = _bundle(tmp_path, cfg={"account_id": ACCOUNT, "account_type": "stock",
                                      "trading_enabled": True})
    broker = FakeBroker()      # ETF 代码 + stock 类型 ⇒ 每单都被拒
    ctx = _wire(module, broker)
    module.init(ctx)
    module.tick(time.time() + 10)

    guard = module._STATE["guard"]
    assert _of(_lines(capsys), "REJECT"), "本用例前提是全部被拒"
    assert guard.turnover == 0.0, "拒单没有送到市场，不该占用换手额度"
    assert guard.orders_sent == 2, "笔数必须照常计（要能兜住重试次数）"


# ---------------------------------------------------------------------------
# 11. 明确播报「到位」与「窗口收尾」
# ---------------------------------------------------------------------------
def test_done_is_announced_once(tmp_path, capsys):
    module, _ = _bundle(tmp_path)
    module.POSITION_RESYNC_S = 0.0
    broker = FakeBroker()
    ctx = _wire(module, broker)
    module.init(ctx)
    module.tick(time.time() + 10)
    # 成交落地：两只都到位
    broker.positions = [
        {"m_strInstrumentID": "513100", "m_strExchangeID": "SH",
         "m_nVolume": 125000, "m_nCanUseVolume": 125000},
        {"m_strInstrumentID": "511990", "m_strExchangeID": "SH",
         "m_nVolume": 125000, "m_nCanUseVolume": 125000},
    ]
    capsys.readouterr()
    module.tick(time.time() + 20)
    assert len(_of(_lines(capsys), "DONE")) == 1
    module.tick(time.time() + 30)
    assert len(_of(_lines(capsys), "DONE")) == 0, "DONE 不能每拍刷一遍"


def test_window_closed_is_announced_once(tmp_path, capsys, monkeypatch):
    module, _ = _bundle(tmp_path)
    broker = FakeBroker()
    module.init(_wire(module, broker))
    flags = iter([True, False, False, False])
    monkeypatch.setattr(module, "in_window", lambda plan, hhmm: next(flags))
    module.tick(time.time() + 10)
    capsys.readouterr()
    module.tick(time.time() + 20)
    assert len(_of(_lines(capsys), "WINDOW_CLOSED")) == 1
    module.tick(time.time() + 30)
    assert len(_of(_lines(capsys), "WINDOW_CLOSED")) == 0


# ---------------------------------------------------------------------------
# 12. 心跳必须在「只读/致命」时照常发（否则外部以为策略死了）
# ---------------------------------------------------------------------------
def test_status_still_emitted_when_fatal(tmp_path, capsys):
    plan = dict(PLAN)
    plan["date"] = "2000-01-01"
    module, _ = _bundle(tmp_path, plan=plan)
    module.init(_wire(module, FakeBroker()))
    capsys.readouterr()
    module.tick(time.time() + 10)
    kinds = _kinds(_lines(capsys))
    assert "STATUS" in kinds, "只读/致命的策略在外部看来必须还「活着」"
    assert module._STATE["fatal_reason"]


# ---------------------------------------------------------------------------
# 13. 生成器：计划不合法要在**生成时**就失败，不要产出「真机只读」的产物
# ---------------------------------------------------------------------------
def test_generator_check_flags_invalid_embedded_plan():
    bad = dict(PLAN)
    bad["rule"] = {"type": "rebalance",
                   "target": [{"code": "513100.SH", "w": 0.9},
                              {"code": "511990.SH", "w": 0.9}]}
    text = gen.build(embed=CFG, stamp="test", plan=bad, bundle_id="b")
    problems = gen.check(text)
    assert any("EMBEDDED_PLAN 不合法" in p for p in problems), problems


def test_generator_main_rejects_plan_without_account_id(tmp_path, capsys):
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(PLAN), encoding="utf-8")
    rc = gen.main(["--plan", str(plan_path), "--check"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "account_id" in out


def test_generator_main_rejects_invalid_plan_file(tmp_path, capsys):
    bad = dict(PLAN)
    bad["window"] = ["14:50", "09:35"]
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(bad), encoding="utf-8")
    assert gen.main(["--plan", str(plan_path), "--check"]) == 1
    assert "校验失败" in capsys.readouterr().out


def test_generator_warns_when_turnover_cap_too_small(tmp_path, capsys):
    """建仓型计划的换手上限小于目标权重合计 ⇒ 每腿都会被 DROP，必须提示。"""
    plan = dict(PLAN)
    plan["guard"] = {"max_orders": 8, "max_turnover": 0.3, "stop_dd": 0.0}
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    cfg_path = tmp_path / "cfg.json"
    cfg_path.write_text(json.dumps(CFG), encoding="utf-8")
    assert gen.main(["--plan", str(plan_path), "--embed-config", str(cfg_path),
                     "--check"]) == 0
    assert "max_turnover" in capsys.readouterr().out

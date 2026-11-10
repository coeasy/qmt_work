# -*- coding: utf-8 -*-
"""策略内闭环：计划解析 / 时间窗 / 风控闸门 / 意图生成 / 事件回传（py3.6 兼容）。

新规之后，「后端算信号 → 送进 QMT」这条路断了（禁文件、禁网络、网络还会**停模**）。
剩下的形态是：后端盘前把**计划**烘焙进策略源码，盘中由策略自己判断「该不该动、动多少」。
本模块就是那个「自己判断」的最小实现，也是外部与内部之间唯一被允许的语义接口。

三条不可让步的纪律
------------------
1. **无状态启动**：本模块不读文件、不写文件、不联网。持仓由 ``position_source``
   从柜台重建；计划由源码常量提供。
2. **风控内建**：闭环后外部无法实时熔断，所以单日笔数、换手上限、回撤停机必须
   在策略里判定 —— 本模块的 ``Guard`` 就是最后一道闸。
3. **回传只走日志**：``emit()`` 返回一行 ``@@QMTW|{json}``，由客户端日志带出，
   外部 ``LogTail`` 解析。**不要**在这里尝试任何 IO。

★ py3.6 约束：禁 dataclasses / f-string / 海象 / 内建泛型注解（本文件会被内联）。
"""
import json
import time

#: 与 ``connectors.bigqmt_log_tail.PREFIX`` 必须逐字一致（跨进程契约，改一处必改两处）。
PREFIX = "@@QMTW|"

#: 单行上限。超长拆多条，绝不截断 JSON（截断后解析出来是半个对象）。
MAX_LINE = 8192

_SIDES = ("BUY", "SELL")


class PlanError(ValueError):
    """计划不合法。★ 一律响亮失败：宁可当天不交易，也不要按半个计划下单。"""


def parse_plan(raw):
    """校验并归一化一份计划。返回 dict（不修改入参）。"""
    if not isinstance(raw, dict):
        raise PlanError("计划必须是 dict，实际是 %s" % type(raw).__name__)
    plan_id = str(raw.get("id") or "").strip()
    if not plan_id:
        raise PlanError("计划缺少 id")
    strat = str(raw.get("strat") or "").strip()
    if not strat:
        raise PlanError("计划缺少 strat")

    window = raw.get("window") or ["09:30", "14:57"]
    if not isinstance(window, (list, tuple)) or len(window) != 2:
        raise PlanError("window 必须是 [开始HH:MM, 结束HH:MM]")
    start, end = str(window[0]), str(window[1])
    for mark in (start, end):
        if not _valid_hhmm(mark):
            raise PlanError("window 时间格式非法: %s" % mark)
    if start > end:
        raise PlanError("window 开始晚于结束: %s > %s" % (start, end))

    rule = raw.get("rule") or {}
    if not isinstance(rule, dict):
        raise PlanError("rule 必须是 dict")
    rule_type = str(rule.get("type") or "rebalance").lower()
    if rule_type != "rebalance":
        raise PlanError("暂不支持的 rule.type: %s" % rule_type)
    targets = rule.get("target") or []
    if not isinstance(targets, (list, tuple)) or not targets:
        raise PlanError("rule.target 不能为空")
    norm_targets = []
    total = 0.0
    for item in targets:
        if not isinstance(item, dict):
            raise PlanError("rule.target 元素必须是 dict")
        code = str(item.get("code") or "").strip().upper()
        if not code:
            raise PlanError("rule.target 缺少 code")
        try:
            weight = float(item.get("w"))
        except (TypeError, ValueError):
            raise PlanError("rule.target[%s].w 不是数字" % code)
        if weight < 0 or weight > 1:
            raise PlanError("rule.target[%s].w 越界(0~1): %s" % (code, weight))
        total += weight
        norm_targets.append({"code": code, "w": weight})
    if total > 1.0001:
        raise PlanError("目标权重合计 %.4f > 1（不允许隐性加杠杆）" % total)

    guard = raw.get("guard") or {}
    if not isinstance(guard, dict):
        raise PlanError("guard 必须是 dict")
    return {
        "id": plan_id,
        "strat": strat,
        "window": [start, end],
        "rule": {"type": rule_type, "target": norm_targets},
        "guard": {
            "max_orders": int(guard.get("max_orders", 8) or 8),
            "max_turnover": float(guard.get("max_turnover", 0.5) or 0.5),
            "stop_dd": float(guard.get("stop_dd", 0.0) or 0.0),
        },
    }


def _valid_hhmm(text):
    parts = str(text).split(":")
    if len(parts) != 2:
        return False
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except (TypeError, ValueError):
        return False
    return 0 <= hour <= 23 and 0 <= minute <= 59


def hhmm_of(ts=None):
    """时间戳 → ``HH:MM``。**必须传时间**，不要在策略里隐式取系统时钟做交易决策。"""
    moment = time.localtime(ts if ts is not None else time.time())
    return "%02d:%02d" % (moment.tm_hour, moment.tm_min)


def in_window(plan, hhmm):
    start, end = plan["window"]
    return start <= hhmm <= end


class Guard(object):
    """闸门：单日笔数 / 换手上限 / 回撤停机。**任一触发都要留下可读的原因。**

    ★ ``max_turnover`` 的语义是**当日累计换手 / 总资产**，从空仓建满仓本身就是
      100%。所以「从零建仓」的计划必须给到 ≥ 目标权重合计（通常 1.0），
      否则每一腿都会被 ``max_turnover`` 裁掉、当天一单不发。
      默认值 0.5 只适合「已有仓位做小幅再平衡」，**不适合首次建仓** ——
      这个坑很难从现象上看出来（只看到 DROP，看不到「你配小了」）。
    """

    def __init__(self, spec, nav=0.0):
        self.max_orders = int(spec.get("max_orders", 8) or 8)
        self.max_turnover = float(spec.get("max_turnover", 0.5) or 0.5)
        self.stop_dd = float(spec.get("stop_dd", 0.0) or 0.0)
        self.nav = float(nav or 0.0)
        self.peak_nav = float(nav or 0.0)
        self.orders_sent = 0
        self.turnover = 0.0
        self.halt_reason = ""

    # ------------------------------------------------------------------
    def mark_nav(self, nav):
        """更新净值并检查回撤。返回 True 表示「已停机」。"""
        self.nav = float(nav or 0.0)
        if self.nav > self.peak_nav:
            self.peak_nav = self.nav
        if self.stop_dd > 0 and self.peak_nav > 0:
            drawdown = (self.peak_nav - self.nav) / self.peak_nav
            if drawdown >= self.stop_dd:
                self.halt("drawdown %.2f%% >= stop_dd %.2f%%" % (drawdown * 100, self.stop_dd * 100))
        return self.halted

    def halt(self, reason):
        if not self.halt_reason:
            self.halt_reason = str(reason)

    @property
    def halted(self):
        return bool(self.halt_reason)

    def reset_day(self, nav=0.0):
        self.orders_sent = 0
        self.turnover = 0.0
        self.halt_reason = ""
        self.nav = float(nav or 0.0)
        self.peak_nav = float(nav or 0.0)

    def refund(self, value):
        """归还**换手额度**（不退单日笔数）。

        ★ 为什么要退：拒单（账户类型不匹配、代码格式错、trading_enabled=false）
          根本没有送到市场，它不该消耗换手预算。不退的后果很具体 ——
          一次配置错误就把当天的额度烧光，改好之后也做不了任何调仓。
        ★ 为什么笔数不退：拒单仍然是「发过一次」，笔数要留着兜住重试次数，
          否则「改错了 → 每拍重试 → 打爆柜台」没有任何闸门拦得住。
        """
        self.turnover = max(0.0, self.turnover - abs(float(value or 0.0)))

    # ------------------------------------------------------------------
    def filter(self, intents):
        """按闸门裁剪意图列表，返回 ``(allowed, dropped)``；``dropped`` 带原因。"""
        if self.halted:
            return [], [{"code": it.get("code", ""), "reason": "halted: " + self.halt_reason}
                        for it in intents]
        allowed, dropped = [], []
        budget_nav = self.nav if self.nav > 0 else 0.0
        for intent in intents:
            if self.orders_sent + len(allowed) >= self.max_orders:
                dropped.append({"code": intent.get("code", ""), "reason": "max_orders"})
                continue
            value = abs(float(intent.get("value", 0.0) or 0.0))
            if budget_nav > 0:
                projected = (self.turnover + value) / budget_nav
                if projected > self.max_turnover:
                    dropped.append({"code": intent.get("code", ""),
                                    "reason": "max_turnover %.2f%% > %.2f%%"
                                              % (projected * 100, self.max_turnover * 100)})
                    continue
            self.turnover += value
            allowed.append(intent)
        self.orders_sent += len(allowed)
        return allowed, dropped


def decide(plan, positions, nav, prices, lot=100):
    """按目标权重算调仓意图。**只算意图，不下单**（下单由调用方走 ``passorder``）。"""
    if nav <= 0:
        raise PlanError("nav 必须 > 0（拿不到总资产就不要算调仓）")
    intents = []
    for target in plan["rule"]["target"]:
        code = target["code"]
        price = float(prices.get(code) or 0.0)
        if price <= 0:
            # ★ 没价格就不算这只：用 0 价算出来的股数是无穷大。
            continue
        current = int((positions.get(code) or {}).get("volume", 0))
        target_value = nav * target["w"]
        delta_value = target_value - current * price
        shares = int(delta_value / price / lot) * lot
        if shares == 0:
            continue
        intents.append({
            "code": code,
            "side": "BUY" if shares > 0 else "SELL",
            "volume": abs(shares),
            "ref_price": price,
            "value": abs(shares) * price,
            "weight": target["w"],
        })
    intents.sort(key=lambda it: -it["value"])
    return intents


def emit(kind, data, seq, boot_id, ts=None):
    """构造一行回传记录。返回**不含换行**的字符串；调用方 ``print`` 它即可。"""
    payload = {
        "k": str(kind or "").upper(),
        "s": int(seq or 0),
        "b": str(boot_id or ""),
        "t": float(ts if ts is not None else time.time()),
        "d": data if isinstance(data, dict) else {"v": data},
    }
    line = PREFIX + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(line) > MAX_LINE:
        # 超长就丢掉大字段并标注，绝不输出半个 JSON（半个 JSON = 坏行）。
        payload["d"] = {"truncated": True, "keys": sorted(payload["d"].keys())[:20]}
        line = PREFIX + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return line


__all__ = ["Guard", "MAX_LINE", "PREFIX", "PlanError", "decide", "emit",
           "hhmm_of", "in_window", "parse_plan"]

"""统一风控闸门（工业级增强）：下单前校验 + 运行期可配置 + 审计拒绝。

单笔/比例/频率规则：
- 单笔金额上限 max_amount
- 最小数量 min_qty（100 股整数倍）
- 单标的总持仓占比上限 max_position_ratio（全局）/ max_single_position_ratio（单票）
- 下单频率限制 max_orders_per_min（滑动窗口，防策略失控/连点）

日级规则（B4，0 表示不启用；跨自然日自动重置）：
- daily_amount_limit：日累计下单金额上限（买卖合计）
- per_code_daily_orders：单标的日下单次数上限（防单票反复刷单）
- daily_loss_limit：日亏损熔断——净值较**日初**回撤达该金额后熔断，
  熔断期间**只允许卖出平仓**、拒绝一切买入开仓，需人工 reset 或次日自动解除。

全部参数支持运行期经 risk_config 表持久化（config/risk 端点读写），拒绝时返回原因。
"""
import time
from collections import deque
from dataclasses import dataclass, field

# 可运行期配置的参数：字段名 -> (类型转换, 是否允许 0/关闭)
_TUNABLES: dict[str, tuple[type, bool]] = {
    "max_amount": (float, False),
    "min_qty": (int, False),
    "max_position_ratio": (float, False),
    "max_single_position_ratio": (float, False),
    "max_orders_per_min": (int, False),
    # B4：0 = 不限制
    "daily_amount_limit": (float, True),
    "daily_loss_limit": (float, True),
    "per_code_daily_orders": (int, True),
    # P1：价格偏离 / 标的白黑名单（0/空 = 关闭）
    "price_deviation_pct": (float, True),
    "symbol_allow": (str, True),
    "symbol_deny": (str, True),
}


def _today() -> str:
    return time.strftime("%Y-%m-%d")


@dataclass
class RiskManager:
    max_amount: float = 100_000.0
    min_qty: int = 100
    max_position_ratio: float = 0.3
    max_single_position_ratio: float = 0.2
    max_orders_per_min: int = 30
    # ---- B4 日级限额与熔断（0 = 关闭）----
    daily_amount_limit: float = 0.0
    daily_loss_limit: float = 0.0
    per_code_daily_orders: int = 0
    # ---- P1 预交易扩展（0/空 = 关闭）----
    price_deviation_pct: float = 0.0    # 下单价相对最新价允许偏离上限（0.05=±5%）
    symbol_allow: str = ""              # 标的白名单（逗号分隔；空=全部允许）
    symbol_deny: str = ""               # 标的黑名单（逗号分隔；命中即拒）
    # 简单内存态：code -> 持仓市值（真实场景由账户网关提供）
    positions_value: dict[str, float] = field(default_factory=dict)
    total_assets: float = 1_000_000.0
    # 数据来源标注：未接入真实持仓时为 "demo"（演示级 100 万默认值），
    # 由 SyncEngine 账户快照喂入后切换为 "live"。前端据此显式提示「演示值」。
    data_source: str = "demo"
    _order_times: deque = field(default_factory=lambda: deque(maxlen=500))
    _price_provider: object = None      # 最新价回调：fn(code) -> float|None（价格偏离校验用）
    # 日级计数（跨日自动清零）
    _day: str = field(default_factory=_today)
    _day_amount: float = 0.0
    _day_orders: int = 0
    _day_code_orders: dict[str, int] = field(default_factory=dict)
    _day_start_net: float = 0.0
    _cur_net: float = 0.0
    _broken: bool = False
    _broken_reason: str = ""
    _broken_at: str = ""

    # ---------------- 配置持久化 ----------------
    def to_dict(self) -> dict:
        d = {k: getattr(self, k) for k in _TUNABLES}
        d["data_source"] = self.data_source
        return d

    def feed_account_snapshot(self, positions_value: dict[str, float], total_assets: float) -> None:
        """由 SyncEngine 账户快照喂入真实持仓市值与总资产，切换数据来源为 live。

        未调用前 total_assets 停留在默认 100 万（演示级），data_source="demo"。
        """
        self.positions_value = dict(positions_value or {})
        if total_assets and total_assets > 0:
            self.total_assets = float(total_assets)
        self.data_source = "live"

    def update_from(self, data: dict) -> list[str]:
        """按传入字典更新参数；返回实际变更的字段名列表。"""
        changed = []
        for key, (cast, allow_zero) in _TUNABLES.items():
            if key not in data:
                continue
            val = cast(data[key])
            if not isinstance(val, str):
                # 数值校验：不能为负；非「允许 0」的参数不能为 0
                if val < 0 or (val == 0 and not allow_zero):
                    raise ValueError(f"{key} 必须为正数" if not allow_zero
                                     else f"{key} 不能为负数")
            if getattr(self, key) != val:
                setattr(self, key, val)
                changed.append(key)
        return changed

    def save_to_db(self, db) -> None:
        import json
        row = db.query_one("SELECT id FROM risk_config WHERE scope='global'")
        if row:
            db.execute("UPDATE risk_config SET params_json=?, updated_at=? WHERE scope='global'",
                       (json.dumps(self.to_dict()), time.strftime("%Y-%m-%dT%H:%M:%S")))
        else:
            db.insert("risk_config", {"scope": "global",
                                      "params_json": json.dumps(self.to_dict()),
                                      "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S")})

    @classmethod
    def load_from_db(cls, db, defaults: dict | None = None) -> "RiskManager":
        import json
        rm = cls(**(defaults or {}))
        if db is None:
            return rm
        try:
            row = db.query_one("SELECT params_json FROM risk_config WHERE scope='global'")
            if row and row.get("params_json"):
                rm.update_from(json.loads(row["params_json"]))
        except Exception:  # noqa: BLE001
            pass
        return rm

    # ---------------- B4 日级状态 ----------------
    def _roll_day(self) -> bool:
        """跨自然日则清零日级计数并解除熔断；返回是否发生了跨日。"""
        d = _today()
        if d == self._day:
            return False
        self._day = d
        self._day_amount = 0.0
        self._day_orders = 0
        self._day_code_orders = {}
        self._day_start_net = 0.0  # 待下一次净值上报重新锚定日初
        self._broken = False
        self._broken_reason = ""
        self._broken_at = ""
        return True

    def update_net_value(self, net_value: float) -> str | None:
        """上报账户净值（账户快照循环调用）。

        首次上报锚定为日初净值；回撤达 daily_loss_limit 时置熔断。
        返回值：本次**新触发**熔断时返回原因文本（供调用方推送告警），否则 None。
        """
        self._roll_day()
        try:
            nv = float(net_value)
        except (TypeError, ValueError):
            return None
        if nv <= 0:
            return None
        if self._day_start_net <= 0:
            self._day_start_net = nv
        self._cur_net = nv
        if self.daily_loss_limit <= 0 or self._broken:
            return None
        loss = self._day_start_net - nv
        if loss >= self.daily_loss_limit:
            self._broken = True
            self._broken_at = time.strftime("%Y-%m-%dT%H:%M:%S")
            self._broken_reason = (
                f"日内亏损熔断：净值 {self._day_start_net:.0f} → {nv:.0f}，"
                f"回撤 {loss:.0f} ≥ 阈值 {self.daily_loss_limit:.0f}，已禁止买入开仓")
            return self._broken_reason
        return None

    def trip(self, reason: str = "人工熔断") -> str:
        """手动熔断（一键停止开仓）。"""
        self._roll_day()
        self._broken = True
        self._broken_at = time.strftime("%Y-%m-%dT%H:%M:%S")
        self._broken_reason = reason
        return reason

    def reset_circuit(self) -> None:
        """人工解除熔断（同时把当前净值重新锚定为日初，避免立即再次触发）。"""
        self._broken = False
        self._broken_reason = ""
        self._broken_at = ""
        if self._cur_net > 0:
            self._day_start_net = self._cur_net

    def reset_daily(self) -> None:
        """人工清零日级计数（一般用于测试或换日容错）。"""
        self._day = _today()
        self._day_amount = 0.0
        self._day_orders = 0
        self._day_code_orders = {}
        self.reset_circuit()

    def set_price_provider(self, fn) -> None:
        """注入最新价提供者（fn(code) -> float|None），用于价格偏离校验。"""
        self._price_provider = fn

    @property
    def circuit_broken(self) -> bool:
        return self._broken

    def daily_stats(self) -> dict:
        """日级风控实时状态（供 /config/risk 与前端展示）。"""
        self._roll_day()
        drawdown = (self._day_start_net - self._cur_net) if self._day_start_net > 0 and self._cur_net > 0 else 0.0
        return {
            "date": self._day,
            "day_amount": round(self._day_amount, 2),
            "day_amount_limit": self.daily_amount_limit,
            "day_amount_used_pct": (round(self._day_amount / self.daily_amount_limit * 100, 2)
                                    if self.daily_amount_limit > 0 else None),
            "day_orders": self._day_orders,
            "day_code_orders": dict(self._day_code_orders),
            "per_code_daily_orders": self.per_code_daily_orders,
            "day_start_net": round(self._day_start_net, 2),
            "current_net": round(self._cur_net, 2),
            "drawdown": round(drawdown, 2),
            "daily_loss_limit": self.daily_loss_limit,
            "circuit_broken": self._broken,
            "circuit_reason": self._broken_reason,
            "circuit_at": self._broken_at,
        }

    # ---------------- 校验 ----------------
    def _validate_order(self, code: str, price: float, volume: int,
                        direction: str) -> tuple[bool, str]:
        """纯校验（不修改任何计数/状态）：返回 (是否放行, 原因)。

        供 `precheck_order` 复用 —— 预检只判断「这笔委托会不会被风控拦截」，
        但不计入频率窗口与日级用量，避免预检本身污染真实风控计数。
        """
        self._roll_day()
        if volume <= 0:
            return False, "volume must be positive"
        if volume < self.min_qty:
            return False, f"volume {volume} < min qty {self.min_qty}"
        if volume % 100 != 0:
            return False, f"volume {volume} 须为 100 的整数倍"
        # ---- P1 标的白/黑名单 ----
        deny = {s.strip() for s in self.symbol_deny.split(",") if s.strip()}
        allow = {s.strip() for s in self.symbol_allow.split(",") if s.strip()}
        if code in deny:
            return False, f"{code} 在风控黑名单中，禁止交易"
        if allow and code not in allow:
            return False, f"{code} 不在风控白名单中，禁止交易"
        # ---- P1 价格偏离拒单：下单价相对最新价偏离超限即拒 ----
        if self.price_deviation_pct > 0 and price > 0:
            ref = None
            if self._price_provider is not None:
                try:
                    ref = self._price_provider(code)
                except Exception:  # noqa: BLE001
                    ref = None
            if ref and ref > 0:
                dev = abs(price - ref) / ref
                if dev > self.price_deviation_pct:
                    return False, (
                        f"price deviation {dev * 100:.1f}% > "
                        f"limit {self.price_deviation_pct * 100:.1f}% "
                        f"(latest {ref:.2f})")
        amount = price * volume
        if amount > self.max_amount:
            return False, f"order amount {amount:.0f} > max amount {self.max_amount:.0f}"
        is_buy = str(direction).lower() in ("buy", "b", "1", "long", "开仓")
        # ---- B4 日级熔断：熔断期间只允许卖出平仓 ----
        if self._broken and is_buy:
            return False, f"风控熔断中，禁止买入开仓（{self._broken_reason or '人工熔断'}）"
        # ---- B4 单标的日下单次数上限 ----
        if self.per_code_daily_orders > 0:
            used = self._day_code_orders.get(code, 0)
            if used >= self.per_code_daily_orders:
                return False, (f"{code} 今日下单 {used} 笔已达上限 "
                               f"{self.per_code_daily_orders}")
        # ---- B4 日累计下单金额上限（达到上限即拒单）----
        if self.daily_amount_limit > 0 and self._day_amount + amount >= self.daily_amount_limit:
            return False, (f"日累计下单金额将达 {self._day_amount + amount:.0f} "
                           f"≥ 上限 {self.daily_amount_limit:.0f}"
                           f"（今日已用 {self._day_amount:.0f}）")
        if is_buy:
            cur = self.positions_value.get(code, 0.0)
            new_ratio = (cur + amount) / self.total_assets
            if new_ratio > self.max_single_position_ratio:
                return False, (
                    f"single position ratio would be {new_ratio:.2f} > "
                    f"max {self.max_single_position_ratio:.2f}")
            if new_ratio > self.max_position_ratio:
                return False, (
                    f"position ratio would be {new_ratio:.2f} > "
                    f"max ratio {self.max_position_ratio:.2f}")
        return True, "ok"

    def precheck_order(self, code: str, price: float, volume: int,
                       direction: str) -> tuple[bool, str]:
        """非变更型预检：判断委托是否会被风控放行，但不计入频率/日级用量。

        前端「风控预检」按钮调用，避免预检本身污染真实风控计数（与 `check_order` 的区别）。
        """
        return self._validate_order(code, price, volume, direction)

    def check_order(self, code: str, price: float, volume: int,
                    direction: str) -> tuple[bool, str]:
        """下单前校验 + 计入频率窗口与日级用量（放行的委托才计数）。"""
        self._roll_day()
        now = time.time()
        self._order_times.append(now)
        # 频率限制：滑动窗口 60s 内下单数
        while self._order_times and now - self._order_times[0] > 60:
            self._order_times.popleft()
        if len(self._order_times) > self.max_orders_per_min:
            return False, (
                f"下单频率超限：近 60s 已 {len(self._order_times)} 笔 "
                f"> 上限 {self.max_orders_per_min}")
        ok, reason = self._validate_order(code, price, volume, direction)
        if not ok:
            return False, reason
        # 全部通过 -> 计入日级用量（只统计放行的委托）
        amount = price * volume
        self._day_amount += amount
        self._day_orders += 1
        self._day_code_orders[code] = self._day_code_orders.get(code, 0) + 1
        return True, "ok"

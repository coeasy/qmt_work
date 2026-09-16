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
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from core.clock import now_iso, today_str

# P0-5：账户快照未就绪时的演示级默认总资产。占比类闸门用它做分母等于「形同虚设」
# （10 万单 = 演示 100 万的 10%），故实盘（live）买入前必须要求快照就绪。
DEMO_TOTAL_ASSETS = 1_000_000.0

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
    # P0-5：实盘买入是否强制要求账户快照就绪（0 = 关闭该闸门）
    "require_snapshot_for_buy": (int, True),
}


def _today() -> str:
    return today_str()


# 交易方向归一化（中英 / 多同义词）。未知方向必须显式拒绝，绝不能按「卖出」处理
# （阶段 0-B / F5：原实现未知方向一律按卖出，导致熔断禁买失效、买入检查被绕过）。
_BUY_SYN = {"buy", "b", "1", "long", "开仓", "买入", "purchase", "bid", "买"}
_SELL_SYN = {"sell", "s", "2", "short", "平仓", "卖出", "sale", "ask", "卖"}


def normalize_direction(direction) -> str | None:
    """归一化交易方向：buy / sell / None（未知）。

    未知方向返回 None —— 调用方必须拒绝，而非默认当作卖出。
    """
    d = str(direction or "").strip().lower()
    if not d:
        return None
    if d in _BUY_SYN:
        return "buy"
    if d in _SELL_SYN:
        return "sell"
    return None


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
    total_assets: float = DEMO_TOTAL_ASSETS
    # 数据来源标注：未接入真实持仓时为 "demo"（演示级 100 万默认值），
    # 由 SyncEngine 账户快照喂入后切换为 "live"。前端据此显式提示「演示值」。
    data_source: str = "demo"
    # ---- P0-5 资金/仓位原子化 ----
    # 可用资金（来自账户快照 get_cash 的 cash 字段）
    available_cash: float = 0.0
    # 快照是否已喂入现金（未喂入时不做资金校验，宁可放过也不误拒）
    _cash_known: bool = False
    # 实盘买入是否强制要求账户快照就绪（1=是，默认；0=关闭该闸门，供联调/无资产
    # 上报能力的券商临时放行）。仅对「实盘（live）买入」生效，模拟盘不受影响。
    require_snapshot_for_buy: int = 1
    # 在途委托金额（P0-5 原子化核心）：已放行但尚未体现在 positions_value 里的
    # 买入金额，按 (code, amount, ts) 记录并 TTL 过期。占比闸门必须把它算进去，
    # 否则并发多单会各自按「未含彼此」的旧仓位判定，双双通过 → 超仓。
    _pending_buys: list = field(default_factory=list)
    _pending_ttl: float = 120.0
    # 校验+计数+在途登记必须原子（跨线程调用方：REST/MCP/引擎）
    _lock: object = field(default_factory=threading.Lock)
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

    def feed_account_snapshot(self, positions_value: dict[str, float], total_assets: float,
                              available_cash: float | None = None) -> None:
        """由 SyncEngine 账户快照喂入真实持仓市值与总资产，切换数据来源为 live。

        未调用前 total_assets 停留在默认 100 万（演示级），data_source="demo"。

        P0-5：额外接收 ``available_cash``（券商 get_cash 的可用资金）。一旦喂入，
        后续实盘买入会做「可用资金充足性」校验；未喂入（None）则不做该校验，
        避免把「没有资产上报能力」误判成「没有资金」。
        """
        self.positions_value = dict(positions_value or {})
        if total_assets and total_assets > 0:
            self.total_assets = float(total_assets)
        if available_cash is not None:
            self.available_cash = float(available_cash or 0.0)
            self._cash_known = True
        self.data_source = "live"

    # ---------------- P0-5 在途委托（原子化占比判定）----------------
    def _prune_pending(self, now: float) -> None:
        """丢弃超过 TTL 的在途委托（视为已被账户快照反映或已失效）。"""
        ttl = self._pending_ttl
        if ttl <= 0:
            self._pending_buys = []
            return
        if self._pending_buys:
            self._pending_buys = [e for e in self._pending_buys if now - e[2] < ttl]

    def _pending_total(self) -> float:
        return sum(e[1] for e in self._pending_buys)

    def _pending_for(self, code: str) -> float:
        return sum(e[1] for e in self._pending_buys if e[0] == code)

    def _reserve_pending(self, code: str, amount: float, now: float) -> None:
        """登记一笔已放行的买入在途金额（调用方须持锁）。"""
        if amount > 0:
            self._pending_buys.append((code, float(amount), float(now)))

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
                       (json.dumps(self.to_dict()), now_iso()))
        else:
            db.insert("risk_config", {"scope": "global",
                                      "params_json": json.dumps(self.to_dict()),
                                      "updated_at": now_iso()})

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
            self._broken_at = now_iso()
            self._broken_reason = (
                f"日内亏损熔断：净值 {self._day_start_net:.0f} → {nv:.0f}，"
                f"回撤 {loss:.0f} ≥ 阈值 {self.daily_loss_limit:.0f}，已禁止买入开仓")
            return self._broken_reason
        return None

    def trip(self, reason: str = "人工熔断") -> str:
        """手动熔断（一键停止开仓）。"""
        self._roll_day()
        self._broken = True
        self._broken_at = now_iso()
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
    def _effective_price(self, code: str, price: float, price_type: str = "limit") -> float:
        """估算委托金额用有效价：限价单用下单价；市价单用最新价（取不到则 0）。

        供金额闸门/日额度/仓位比例统一使用，避免校验与计数口径不一致（P1-4）。
        """
        if float(price) > 0:
            return float(price)
        if (price_type or "limit").lower() != "market":
            return 0.0
        try:
            r = self._price_provider(code) if self._price_provider else None
        except Exception:  # noqa: BLE001
            r = None
        return float(r) if r and r > 0 else 0.0

    def _validate_order(self, code: str, price: float, volume: int,
                        direction: str, price_type: str = "limit",
                        require_account: bool = False) -> tuple[bool, str]:
        """纯校验（不修改任何计数/状态）：返回 (是否放行, 原因)。

        供 `precheck_order` 复用 —— 预检只判断「这笔委托会不会被风控拦截」，
        但不计入频率窗口与日级用量，避免预检本身污染真实风控计数。

        ``require_account``（P0-5）：该笔为**实盘**下单时为 True —— 此时才要求
        账户快照就绪并做可用资金校验。模拟盘（paper）无券商账户，快照永不就绪，
        若一刀切会废掉模拟盘，故由调用方按 mode 显式传入。

        price_type 区分限价/市价：
        - 限价单：price 必须 >0（防「限价单以 0 元送出」灾难），价格相关校验齐全；
        - 市价单：成交价未知，价格偏离不校验；金额/仓位/日额度尽量用最新价估算，
          无最新价可估时仅放行（qty/白黑名单/频率等非价格闸门已校验）。
        """
        self._roll_day()
        if volume <= 0:
            return False, "volume must be positive"
        if volume < self.min_qty:
            return False, f"volume {volume} < min qty {self.min_qty}"
        if volume % 100 != 0:
            return False, f"volume {volume} 须为 100 的整数倍"
        is_market = (price_type or "limit") == "market"
        if not is_market and price <= 0:
            return False, f"限价单必须提供 >0 的委托价，当前 price={price}"
        # ---- P1 标的白/黑名单 ----
        deny = {s.strip() for s in self.symbol_deny.split(",") if s.strip()}
        allow = {s.strip() for s in self.symbol_allow.split(",") if s.strip()}
        if code in deny:
            return False, f"{code} 在风控黑名单中，禁止交易"
        if allow and code not in allow:
            return False, f"{code} 不在风控白名单中，禁止交易"
        # ---- P1 价格偏离拒单：下单价相对最新价偏离超限即拒（仅限价单）----
        if not is_market and self.price_deviation_pct > 0 and price > 0:
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
        # P1-4：统一用有效价估算金额（限价=下单价；市价=最新价）
        eff_price = self._effective_price(code, price, price_type)
        amount = eff_price * volume
        # 市价单取不到最新价（amount<=0 且价格未给出）时无法核算金额 → 拒绝，
        # 不再「跳过金额类校验放行」（P0-4/P1-4：避免无价市价单绕过金额闸门）。
        if (price_type or "limit").lower() == "market" and eff_price <= 0:
            return False, "市价单无法取得最新行情，无法估算金额与仓位，已拒绝（请先获取行情）"
        if amount > 0 and amount > self.max_amount:
            return False, f"order amount {amount:.0f} > max amount {self.max_amount:.0f}"
        # 阶段 0-B（F5）：方向归一化；未知方向显式拒绝，绝不按卖出处理
        nd = normalize_direction(direction)
        if nd is None:
            return False, f"未知交易方向：{direction!r}（须为 buy/sell 或其同义表述）"
        is_buy = nd == "buy"
        # ---- B4 日级熔断：熔断期间只允许卖出平仓 ----
        if self._broken and is_buy:
            return False, f"风控熔断中，禁止买入开仓（{self._broken_reason or '人工熔断'}）"
        # ---- B4 单标的日下单次数上限 ----
        if self.per_code_daily_orders > 0:
            used = self._day_code_orders.get(code, 0)
            if used >= self.per_code_daily_orders:
                return False, (f"{code} 今日下单 {used} 笔已达上限 "
                               f"{self.per_code_daily_orders}")
        # ---- B4 日累计下单金额上限（达到上限即拒单；金额可估时生效）----
        if amount > 0 and self.daily_amount_limit > 0 and self._day_amount + amount >= self.daily_amount_limit:
            return False, (f"日累计下单金额将达 {self._day_amount + amount:.0f} "
                           f"≥ 上限 {self.daily_amount_limit:.0f}"
                           f"（今日已用 {self._day_amount:.0f}）")
        if is_buy and amount > 0:
            # ---- P0-5：实盘买入的资金/仓位闸门（require_account=实盘下单时为 True）----
            if require_account:
                # ① 账户快照未就绪：total_assets 仍是演示级 100 万 → 占比闸门形同虚设，
                #    一律拒绝买入开仓，绝不用假分母放行真单（卖出/平仓不受限）。
                if self.require_snapshot_for_buy and self.data_source != "live":
                    return False, (
                        "账户快照未就绪：拒绝买入开仓（总资产仍为演示值 "
                        f"{DEMO_TOTAL_ASSETS:.0f}，无法核算仓位与资金；"
                        "请等待账户同步完成或连接券商）")
                # ② 可用资金充足性（仅在快照已喂入现金时校验）
                if self._cash_known and amount > self.available_cash + 1e-9:
                    return False, (
                        f"可用资金不足：本单需 {amount:.0f}，"
                        f"可用 {self.available_cash:.0f}")
            # P0-5：占比分母/分子都并入「在途买入」，否则并发多单各自按未含彼此的
            # 旧仓位判定，会双双通过 → 超仓。positions_value 由 5s 快照喂入，
            # 在快照到达前只能靠在途登记兜住。
            pend_code = self._pending_for(code)
            pend_total = self._pending_total()
            cur = self.positions_value.get(code, 0.0) + pend_code
            # P1-4：单票占比用「单票市值 + 在途 + 本单金额」
            new_single = (cur + amount) / self.total_assets
            if new_single > self.max_single_position_ratio:
                return False, (
                    f"single position ratio would be {new_single:.2f} > "
                    f"max {self.max_single_position_ratio:.2f}")
            # P0-4/P1-4：全局总仓位占比用「组合总市值 + 在途 + 本单金额」，单票占比规则
            # 不再重复遮蔽全局规则（此前两者都按单票，max_position_ratio 永不生效）。
            port = (sum(self.positions_value.values()) + pend_total + amount) / self.total_assets
            if port > self.max_position_ratio:
                return False, (
                    f"portfolio position ratio would be {port:.2f} > "
                    f"max ratio {self.max_position_ratio:.2f}")
        return True, "ok"

    def precheck_order(self, code: str, price: float, volume: int,
                       direction: str, price_type: str = "limit",
                       require_account: bool = False) -> tuple[bool, str]:
        """非变更型预检：判断委托是否会被风控放行，但不计入频率/日级用量。

        前端「风控预检」按钮调用，避免预检本身污染真实风控计数（与 `check_order` 的区别）。
        预检同样**不登记在途金额**（只判断，不预留）。
        """
        return self._validate_order(code, price, volume, direction, price_type,
                                    require_account=require_account)

    def check_order(self, code: str, price: float, volume: int,
                    direction: str, price_type: str = "limit",
                    require_account: bool = False) -> tuple[bool, str]:
        """下单前校验 + 计入频率窗口与日级用量（放行的委托才计数）。

        P0-5：整个「校验 → 计数 → 在途登记」在同一把锁内完成，保证并发下单时
        占比闸门串行判定；放行的买入单登记在途金额，供后续并发单的占比分母/分子
        计入，从而在 5s 账户快照到达前也不会超仓。
        """
        with self._lock:
            self._roll_day()
            now = time.time()
            self._prune_pending(now)
            # 频率窗口当前计数（不含本次）——先判窗口，再校验
            while self._order_times and now - self._order_times[0] > 60:
                self._order_times.popleft()
            if len(self._order_times) >= self.max_orders_per_min:
                # 阶段 3：风控拦截可观测（频率）
                try:
                    from gateway.metrics import get_metrics
                    get_metrics().record_risk_blocked("frequency")
                except Exception:  # noqa: BLE001
                    pass
                return False, (
                    f"下单频率超限：近 60s 已 {len(self._order_times)} 笔 "
                    f"> 上限 {self.max_orders_per_min}")
            ok, reason = self._validate_order(code, price, volume, direction,
                                              price_type, require_account=require_account)
            if not ok:
                # 阶段 3：风控拦截可观测（校验类：额度/黑名单/偏离/熔断/仓位）
                try:
                    from gateway.metrics import get_metrics
                    get_metrics().record_risk_blocked("validation")
                except Exception:  # noqa: BLE001
                    pass
                # 阶段 0-B（F5 修正）：被拒单**不占用频率额度**，避免频率 DoS
                # （连续构造被拒委托即可耗尽频率窗口、阻断正常下单）。
                return False, reason
            # 全部通过 -> 计入频率窗口与日级用量（只统计放行的委托）
            # P1-4：日额度用**有效价**估算金额（市价单用最新价），避免「校验用估价、
            # 计数却用原始 price(市价=0)」导致日累计金额被低估。
            self._order_times.append(now)
            amount = self._effective_price(code, price, price_type) * volume
            self._day_amount += amount
            self._day_orders += 1
            self._day_code_orders[code] = self._day_code_orders.get(code, 0) + 1
            # P0-5：买入在途登记（供后续并发单占比判定计入）
            if normalize_direction(direction) == "buy":
                self._reserve_pending(code, amount, now)
            return True, "ok"

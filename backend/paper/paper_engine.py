"""模拟盘引擎（Paper Trading）：虚拟资金 + 虚拟撮合 + **真实行情**盯市。

定位说明：本引擎不伪造任何价格。
- 撮合价由调用方给出（手动下单价 / 策略信号价 / 真实行情价）；
- 持仓市值与浮动盈亏一律由 `process_quote()` 注入的**真实行情**最新价计算
  （由集成方把行情监听回调接到 `state.paper_engine.process_quote`）。

设计要点：
- 纯 Python，无网络、无券商依赖，可单元测试；
- 状态（现金 / 持仓 / 成交）落 SQLite，进程重启后自动恢复；
- 自建表（paper_positions / paper_cash / paper_trades），不改动 app/db.py 迁移。
"""
import logging
import threading
from datetime import datetime

log = logging.getLogger("qmt_work.paper")

DEFAULT_INITIAL = 1_000_000.0

_DDL = (
    """CREATE TABLE IF NOT EXISTS paper_positions (
        code TEXT PRIMARY KEY,
        name TEXT,
        volume REAL,
        avg_cost REAL,
        side TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS paper_cash (
        id INTEGER PRIMARY KEY CHECK(id=1),
        cash REAL,
        initial REAL
    )""",
    """CREATE TABLE IF NOT EXISTS paper_trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT,
        side TEXT,
        price REAL,
        volume REAL,
        ts TEXT,
        pnl REAL
    )""",
)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class PaperEngine:
    """模拟盘账户引擎。

    费用模型与 tools/backtest 保持一致口径：佣金双边（万 3，最低 5 元），
    印花税卖出单边千 1。
    """

    def __init__(self, initial_capital: float = DEFAULT_INITIAL,
                 commission_rate: float = 0.0003, min_commission: float = 5.0,
                 stamp_tax_rate: float = 0.001):
        self.db = None
        self.initial = float(initial_capital)
        self.cash = float(initial_capital)
        self.commission_rate = float(commission_rate)
        self.min_commission = float(min_commission)
        self.stamp_tax_rate = float(stamp_tax_rate)
        self.positions: dict[str, dict] = {}
        self.last_prices: dict[str, float] = {}
        self._lock = threading.RLock()
        self._seq = 0

    # ---------------- 生命周期 ----------------
    def init(self, db) -> "PaperEngine":
        """绑定 DB、幂等建表、加载已有状态（现金 + 持仓 + 最近成交价）。"""
        self.db = db
        for sql in _DDL:
            db.execute(sql)
        self.load()
        return self

    def load(self) -> None:
        """从 DB 恢复现金与持仓（无记录则以当前 initial 初始化现金行）。"""
        if self.db is None:
            return
        with self._lock:
            row = self.db.query_one("SELECT cash, initial FROM paper_cash WHERE id=1")
            if row is None:
                self.db.upsert("paper_cash", {"id": 1, "cash": self.cash,
                                              "initial": self.initial})
            else:
                self.cash = float(row["cash"] or 0.0)
                self.initial = float(row["initial"] or DEFAULT_INITIAL)
            self.positions = {}
            for p in self.db.query("SELECT code, name, volume, avg_cost, side "
                                   "FROM paper_positions"):
                self.positions[p["code"]] = {
                    "code": p["code"], "name": p["name"] or p["code"],
                    "volume": float(p["volume"] or 0.0),
                    "avg_cost": float(p["avg_cost"] or 0.0),
                    "side": p["side"] or "long",
                }
            # 无行情时用最近成交价兜底（仍是真实成交/行情价，不是编造价）
            for t in self.db.query(
                    "SELECT code, price FROM paper_trades ORDER BY id"):
                if t["code"] in self.positions:
                    self.last_prices[t["code"]] = float(t["price"] or 0.0)

    def _save_cash(self) -> None:
        if self.db is not None:
            self.db.upsert("paper_cash", {"id": 1, "cash": round(self.cash, 4),
                                          "initial": self.initial})

    def _save_position(self, code: str) -> None:
        if self.db is None:
            return
        pos = self.positions.get(code)
        if pos is None or pos["volume"] <= 0:
            self.db.execute("DELETE FROM paper_positions WHERE code=?", (code,))
            return
        self.db.upsert("paper_positions", {
            "code": code, "name": pos.get("name") or code,
            "volume": round(pos["volume"], 4),
            "avg_cost": round(pos["avg_cost"], 6),
            "side": pos.get("side") or "long",
        })

    def reset(self, initial_capital: float = DEFAULT_INITIAL) -> dict:
        """清空模拟盘：删除持仓/成交，现金重置为 initial_capital。"""
        with self._lock:
            initial = float(initial_capital or DEFAULT_INITIAL)
            if initial <= 0:
                raise ValueError("initial_capital 必须大于 0")
            self.initial = initial
            self.cash = initial
            self.positions = {}
            self.last_prices = {}
            self._seq = 0
            if self.db is not None:
                self.db.execute("DELETE FROM paper_positions")
                self.db.execute("DELETE FROM paper_trades")
                self._save_cash()
            log.info("模拟盘已重置：初始资金 %.2f", initial)
            return self.get_account()

    # ---------------- 费用 ----------------
    def _commission(self, amount: float) -> float:
        return round(max(amount * self.commission_rate, self.min_commission), 4)

    def _stamp_tax(self, amount: float) -> float:
        return round(amount * self.stamp_tax_rate, 4)

    # ---------------- 下单（立即成交） ----------------
    def submit_order(self, code: str, side: str, price: float, volume: float,
                     price_type: str = "limit", remark: str = "",
                     name: str = "") -> dict:
        """模拟撮合：以 price 立即全额成交，扣减费用并更新持仓/现金。

        参数非法、现金不足、可卖数量不足时抛 ValueError（由路由层转 400）。
        """
        code = str(code or "").strip().upper()
        side = str(side or "").strip().lower()
        if not code:
            raise ValueError("code 不能为空")
        if side not in ("buy", "sell"):
            raise ValueError("side 只能是 buy 或 sell")
        try:
            price = float(price)
            volume = float(volume)
        except (TypeError, ValueError):
            raise ValueError("price/volume 必须是数字")
        if volume <= 0:
            raise ValueError("volume 必须大于 0")
        if price <= 0:
            raise ValueError("price 必须大于 0")

        with self._lock:
            amount = price * volume
            commission = self._commission(amount)
            pnl = 0.0
            if side == "buy":
                cost = amount + commission
                if cost > self.cash + 1e-9:
                    raise ValueError(f"模拟盘现金不足：需要 {cost:.2f}，可用 {self.cash:.2f}")
                self.cash -= cost
                pos = self.positions.get(code)
                if pos is None:
                    self.positions[code] = {"code": code, "name": name or code,
                                            "volume": volume, "avg_cost": price,
                                            "side": "long"}
                else:
                    total_vol = pos["volume"] + volume
                    pos["avg_cost"] = (pos["avg_cost"] * pos["volume"]
                                       + price * volume) / total_vol
                    pos["volume"] = total_vol
                    if name:
                        pos["name"] = name
            else:
                pos = self.positions.get(code)
                held = pos["volume"] if pos else 0.0
                if volume > held + 1e-9:
                    raise ValueError(f"模拟盘可卖数量不足：持有 {held:g}，卖出 {volume:g}")
                stamp = self._stamp_tax(amount)
                self.cash += amount - commission - stamp
                pnl = (price - pos["avg_cost"]) * volume - commission - stamp
                pos["volume"] = held - volume
                if pos["volume"] <= 1e-9:
                    self.positions.pop(code, None)

            self.last_prices[code] = price
            self._save_cash()
            self._save_position(code)
            trade = {"code": code, "side": side, "price": round(price, 4),
                     "volume": volume, "ts": _now(), "pnl": round(pnl, 4)}
            if self.db is not None:
                tid = self.db.insert("paper_trades", trade)
            else:
                self._seq += 1
                tid = self._seq
            log.info("模拟盘成交 %s %s %g@%.4f 现金 %.2f", side, code, volume,
                     price, self.cash)
            return {"order_id": f"PAPER-{tid}", "trade_id": tid, "code": code,
                    "side": side, "price": round(price, 4), "volume": volume,
                    "price_type": price_type or "limit", "remark": remark or "",
                    "commission": commission, "pnl": round(pnl, 4),
                    "cash_after": round(self.cash, 2), "status": "filled"}

    # ---------------- 行情盯市 ----------------
    def process_quote(self, code: str, price: float) -> dict:
        """接入真实行情最新价，刷新盯市价并返回该标的浮动盈亏。"""
        code = str(code or "").strip().upper()
        try:
            price = float(price)
        except (TypeError, ValueError):
            return {"code": code, "price": 0.0, "volume": 0.0, "unrealized_pnl": 0.0}
        if not code or price <= 0:
            return {"code": code, "price": price, "volume": 0.0, "unrealized_pnl": 0.0}
        with self._lock:
            self.last_prices[code] = price
            pos = self.positions.get(code)
            if not pos:
                return {"code": code, "price": price, "volume": 0.0,
                        "unrealized_pnl": 0.0}
            upnl = (price - pos["avg_cost"]) * pos["volume"]
            return {"code": code, "price": price, "volume": pos["volume"],
                    "avg_cost": round(pos["avg_cost"], 4),
                    "market_value": round(price * pos["volume"], 2),
                    "unrealized_pnl": round(upnl, 2)}

    def _price_of(self, code: str, pos: dict) -> float:
        """盯市价：优先真实行情最新价，缺失时退化为持仓成本（不编造涨跌）。"""
        p = self.last_prices.get(code)
        return float(p) if p and p > 0 else float(pos["avg_cost"])

    def sync_from_map(self, price_map: dict) -> None:
        """批量注入实时行情价（code -> 最新价），用于盯市刷新。

        price_map 来自行情源（如 state.sync_engine.latest_quotes）；
        缺失的标的保持原盯市价，绝不编造。
        """
        if not price_map:
            return
        with self._lock:
            for code, price in price_map.items():
                try:
                    p = float(price)
                except (TypeError, ValueError):
                    continue
                if p > 0:
                    self.last_prices[str(code).strip().upper()] = p

    # ---------------- 查询 ----------------
    def get_positions(self) -> list[dict]:
        with self._lock:
            out = []
            for code, pos in sorted(self.positions.items()):
                last = self._price_of(code, pos)
                mv = last * pos["volume"]
                upnl = (last - pos["avg_cost"]) * pos["volume"]
                cost = pos["avg_cost"] * pos["volume"]
                out.append({
                    "code": code, "name": pos.get("name") or code,
                    "volume": pos["volume"], "avg_cost": round(pos["avg_cost"], 4),
                    "last_price": round(last, 4), "side": pos.get("side") or "long",
                    "market_value": round(mv, 2), "cost": round(cost, 2),
                    "unrealized_pnl": round(upnl, 2),
                    "profit_ratio": round(upnl / cost, 4) if cost else 0.0,
                })
            return out

    def realized_pnl(self) -> float:
        if self.db is None:
            return 0.0
        row = self.db.query_one(
            "SELECT COALESCE(SUM(pnl), 0) AS s FROM paper_trades WHERE side='sell'")
        return round(float((row or {}).get("s") or 0.0), 2)

    def get_account(self) -> dict:
        with self._lock:
            positions = self.get_positions()
            mv = sum(p["market_value"] for p in positions)
            upnl = sum(p["unrealized_pnl"] for p in positions)
            rpnl = self.realized_pnl()
            total = self.cash + mv
            return {
                "cash": round(self.cash, 2),
                "market_value": round(mv, 2),
                "total_assets": round(total, 2),
                "initial": round(self.initial, 2),
                "unrealized_pnl": round(upnl, 2),
                "realized_pnl": rpnl,
                "total_return": round(total / self.initial - 1, 6) if self.initial else 0.0,
                "position_count": len(positions),
                "positions": positions,
            }

    def get_trades(self, limit: int = 50) -> list[dict]:
        if self.db is None:
            return []
        limit = max(1, min(int(limit or 50), 1000))
        return self.db.query(
            "SELECT id, code, side, price, volume, ts, pnl FROM paper_trades "
            "ORDER BY id DESC LIMIT ?", (limit,))

    def metrics(self) -> dict:
        """模拟盘绩效摘要（口径参考 tools/backtest._metrics 的胜率/均值部分）。"""
        with self._lock:
            trades = self.get_trades(limit=1000)
            closes = [t for t in trades if t.get("side") == "sell"]
            wins = [t for t in closes if float(t.get("pnl") or 0.0) > 0]
            pnls = [float(t.get("pnl") or 0.0) for t in closes]
            acc = self.get_account()
            return {
                "trade_count": len(trades),
                "close_count": len(closes),
                "win_count": len(wins),
                "win_rate": round(len(wins) / len(closes), 3) if closes else 0.0,
                "avg_pnl": round(sum(pnls) / len(pnls), 4) if pnls else 0.0,
                "best_pnl": round(max(pnls), 2) if pnls else 0.0,
                "worst_pnl": round(min(pnls), 2) if pnls else 0.0,
                "realized_pnl": acc["realized_pnl"],
                "unrealized_pnl": acc["unrealized_pnl"],
                "total_pnl": round(acc["realized_pnl"] + acc["unrealized_pnl"], 2),
                "total_return": acc["total_return"],
                "initial": acc["initial"],
                "total_assets": acc["total_assets"],
            }

    # ---------------- 序列化 ----------------
    def to_dict(self) -> dict:
        with self._lock:
            return {"cash": round(self.cash, 4), "initial": self.initial,
                    "positions": {c: dict(p) for c, p in self.positions.items()},
                    "last_prices": dict(self.last_prices)}

    def from_dict(self, data: dict) -> "PaperEngine":
        data = data or {}
        with self._lock:
            self.cash = float(data.get("cash", self.cash) or 0.0)
            self.initial = float(data.get("initial", self.initial) or DEFAULT_INITIAL)
            self.positions = {}
            for code, p in (data.get("positions") or {}).items():
                self.positions[code] = {
                    "code": code, "name": p.get("name") or code,
                    "volume": float(p.get("volume") or 0.0),
                    "avg_cost": float(p.get("avg_cost") or 0.0),
                    "side": p.get("side") or "long"}
            self.last_prices = {k: float(v) for k, v in
                                (data.get("last_prices") or {}).items()}
            if self.db is not None:
                self.db.execute("DELETE FROM paper_positions")
                self._save_cash()
                for code in self.positions:
                    self._save_position(code)
            return self

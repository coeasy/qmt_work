"""进程指标收集器（Prometheus 文本格式），零外部依赖。

暴露核心可观测性指标：
- qmt_uptime_seconds         进程运行时长
- qmt_orders_total{side,status}   委托计数（submitted/filled/error/paper）
- qmt_quotes_total           行情 tick 计数
- qmt_quote_latency_ms{quantile}  行情延迟分位（p50/p95/p99）
- qmt_api_requests_total{scope,status,key_id}  API 请求计数（按 scope/状态/密钥）
- qmt_ws_clients             在线 WebSocket 客户端数
- qmt_broker_connected{conn_id}  券商连接状态（1/0）

可观测性扩展（P2）：
- qmt_backtests_total{status}    回测任务计数
- qmt_paper_orders_total{side}   模拟盘委托计数
- qmt_ws_clients                 WebSocket 客户端数（gauge，render 复用 snapshot）
- qmt_api_latency_ms_bucket/count/sum  API 延迟直方图
- qmt_runtime_mode{conn_id,mode} 运行时模式（in_process/bridge/unknown）
- qmt_errors_total{scope}        错误计数
- 内存 trace 环形缓冲（recent_traces）

用法：在关键路径调用 record_*；routes /metrics 输出文本格式。
"""
import threading
import time
from collections import defaultdict


# API 延迟直方图分桶（毫秒）
_API_LATENCY_BUCKETS = (50, 100, 200, 500, 1000)
# trace 环形缓冲容量
_TRACE_CAP = 200


class Metrics:
    def __init__(self):
        self._lock = threading.Lock()
        self._orders = defaultdict(int)
        self._requests = defaultdict(int)
        self._quotes = 0
        self._latency = []
        self._latency_cap = 3000
        self.start_ts = time.time()

        # --- P2 可观测性扩展 ---
        self._backtests = defaultdict(int)
        self._paper_orders = defaultdict(int)
        self._ws_messages = 0
        self._ws_clients = 0
        self._api_latency = defaultdict(int)   # bucket -> count
        self._api_latency_count = 0
        self._api_latency_sum = 0.0
        self._runtime_mode = defaultdict(int)  # (conn_id, mode) -> 1
        self._errors = defaultdict(int)
        self._recent_traces: list[dict] = []
        # 阶段 3：生命周期/幂等/风控/对账可观测
        self._conn_events = defaultdict(int)   # (conn_id, ev) -> 1
        self._idem_hits = 0
        self._risk_blocked = defaultdict(int)  # rule -> 1
        self._reconcile_diffs = defaultdict(int)  # scope -> 1

    def record_order(self, side: str, status: str) -> None:
        with self._lock:
            self._orders[(side, status)] += 1

    def record_quote(self) -> None:
        with self._lock:
            self._quotes += 1

    def record_quote_latency(self, ms: float) -> None:
        with self._lock:
            self._latency.append(ms)
            if len(self._latency) > self._latency_cap:
                self._latency.pop(0)

    def record_request(self, scope: str, status: int, key_id: str = "") -> None:
        with self._lock:
            self._requests[(scope, status, key_id)] += 1

    # ---------------- P2 可观测性扩展 ----------------

    def record_backtest(self, status: str) -> None:
        """回测任务计数（status: ok/error/running...）。"""
        with self._lock:
            self._backtests[status] += 1

    def record_paper_order(self, side: str) -> None:
        """模拟盘委托计数（side: buy/sell）。"""
        with self._lock:
            self._paper_orders[side] += 1

    def record_ws_message(self) -> None:
        """累计收到的 WebSocket 消息条数。"""
        with self._lock:
            self._ws_messages += 1

    def record_ws_clients(self, n: int) -> None:
        """上报当前在线 WebSocket 客户端数（gauge）。"""
        with self._lock:
            self._ws_clients = n

    def record_request_duration_ms(self, ms: float) -> None:
        """记录一次 API 请求的耗时（毫秒），累计到直方图。"""
        b = None
        for edge in _API_LATENCY_BUCKETS:
            if ms < edge:
                b = edge
                break
        with self._lock:
            self._api_latency[b] += 1
            self._api_latency_count += 1
            self._api_latency_sum += ms

    def record_runtime_mode(self, conn_id: str, mode: str) -> None:
        """记录某连接的运行时模式（in_process/bridge/unknown）。"""
        if mode not in ("in_process", "bridge", "unknown"):
            mode = "unknown"
        with self._lock:
            # 仅保留该连接最新模式：清空同 conn_id 的其它 mode 后写入
            for m in ("in_process", "bridge", "unknown"):
                if m != mode:
                    self._runtime_mode.pop((conn_id, m), None)
            self._runtime_mode[(conn_id, mode)] = 1

    def record_error(self, scope: str) -> None:
        """错误计数（scope: trade/market/auth/ws/...）。"""
        with self._lock:
            self._errors[scope] += 1

    # ---------------- 阶段 3：生命周期/幂等/风控/对账可观测 ----------------

    def record_conn_event(self, conn_id: str, ev: str) -> None:
        """连接事件计数（ev: disconnected/reconnected/subscription_recovered）。"""
        with self._lock:
            self._conn_events[(conn_id, ev)] += 1

    def record_idempotency_hit(self) -> None:
        """幂等命中计数（同一 client_order_id 重复请求被单飞拦截）。"""
        with self._lock:
            self._idem_hits += 1

    def record_risk_blocked(self, rule: str) -> None:
        """风控拦截计数（rule: circuit/limit/frequency/daily_loss/...）。"""
        with self._lock:
            self._risk_blocked[(rule or "unknown")] += 1

    def record_reconcile_diff(self, scope: str) -> None:
        """对账差异计数（scope: order/deal/position/account）。"""
        with self._lock:
            self._reconcile_diffs[(scope or "unknown")] += 1

    def record_trace(self, req_id: str, path: str, status: int,
                     ms: float, scope: str = "") -> None:
        """写入一条请求 trace 到内存环形缓冲（最多 _TRACE_CAP 条）。"""
        entry = {
            "req_id": req_id,
            "path": path,
            "status": status,
            "ms": round(ms, 3),
            "scope": scope,
            "ts": round(time.time(), 3),
        }
        with self._lock:
            self._recent_traces.append(entry)
            if len(self._recent_traces) > _TRACE_CAP:
                self._recent_traces.pop(0)

    def recent_traces(self) -> list[dict]:
        """返回 trace 环形缓冲副本（按时间升序）。"""
        with self._lock:
            return list(self._recent_traces)

    def _quantile(self, qs):
        if not self._latency:
            return {q: 0.0 for q in qs}
        s = sorted(self._latency)
        out = {}
        n = len(s)
        for q in qs:
            idx = min(n - 1, int(q / 100.0 * n))
            out[q] = round(s[idx], 3)
        return out

    def render(self, snapshot: dict | None = None) -> str:
        snapshot = snapshot or {}
        lines: list[str] = []

        def counter(name, help_, value, labels=""):
            lines.append(f"# HELP {name} {help_}")
            lines.append(f"# TYPE {name} counter")
            lines.append(f"{name}{labels} {value}")

        def gauge(name, help_, value, labels=""):
            lines.append(f"# HELP {name} {help_}")
            lines.append(f"# TYPE {name} gauge")
            lines.append(f"{name}{labels} {value}")

        gauge("qmt_uptime_seconds", "process uptime in seconds",
              round(time.time() - self.start_ts, 1))

        lines.append("# HELP qmt_orders_total total orders by side/status")
        lines.append("# TYPE qmt_orders_total counter")
        with self._lock:
            for (side, status), v in self._orders.items():
                lines.append(f'qmt_orders_total{{side="{side}",status="{status}"}} {v}')

        counter("qmt_quotes_total", "total quote ticks received", self._quotes)

        q = self._quantile([50, 95, 99])
        lines.append("# HELP qmt_quote_latency_ms quote latency in milliseconds")
        lines.append("# TYPE qmt_quote_latency_ms gauge")
        for qq, vv in q.items():
            lines.append(f'qmt_quote_latency_ms{{quantile="{qq}"}} {vv}')

        lines.append("# HELP qmt_api_requests_total total api requests by scope/status/key")
        lines.append("# TYPE qmt_api_requests_total counter")
        with self._lock:
            for (scope, status, key_id), v in self._requests.items():
                lines.append(
                    f'qmt_api_requests_total{{scope="{scope}",status="{status}",'
                    f'key_id="{key_id}"}} {v}')

        ws_clients = snapshot.get("ws_clients", self._ws_clients)
        gauge("qmt_ws_clients", "connected websocket clients", ws_clients)

        lines.append("# HELP qmt_broker_connected broker connection state (1 connected / 0 not)")
        lines.append("# TYPE qmt_broker_connected gauge")
        brokers = snapshot.get("brokers") or []
        items = brokers.items() if isinstance(brokers, dict) else brokers
        for cid, connected in items:
            lines.append(f'qmt_broker_connected{{conn_id="{cid}"}} {1 if connected else 0}')

        # ---------------- P2 可观测性扩展 ----------------

        with self._lock:
            bt = dict(self._backtests)
            po = dict(self._paper_orders)
            al = dict(self._api_latency)
            alc = self._api_latency_count
            als = round(self._api_latency_sum, 3)
            rm = dict(self._runtime_mode)
            er = dict(self._errors)

        counter("qmt_backtests_total", "total backtest runs by status", 0)
        for status, v in bt.items():
            lines.append(f'qmt_backtests_total{{status="{status}"}} {v}')

        counter("qmt_paper_orders_total", "total paper-trade orders by side", 0)
        for side, v in po.items():
            lines.append(f'qmt_paper_orders_total{{side="{side}"}} {v}')

        # API 延迟直方图
        lines.append("# HELP qmt_api_latency_ms API request latency in milliseconds")
        lines.append("# TYPE qmt_api_latency_ms histogram")
        cum = 0
        for edge in _API_LATENCY_BUCKETS:
            cum += al.get(edge, 0)
            lines.append(f'qmt_api_latency_ms_bucket{{le="{edge}"}} {cum}')
        cum += al.get(None, 0)  # >=1000 桶（落在上界之外）
        lines.append(f'qmt_api_latency_ms_bucket{{le="+Inf"}} {cum}')
        lines.append(f"qmt_api_latency_ms_count {alc}")
        lines.append(f"qmt_api_latency_ms_sum {als}")

        lines.append("# HELP qmt_runtime_mode runtime mode per connection "
                     "(in_process/bridge/unknown)")
        lines.append("# TYPE qmt_runtime_mode gauge")
        for (conn_id, mode), v in rm.items():
            lines.append(f'qmt_runtime_mode{{conn_id="{conn_id}",mode="{mode}"}} {v}')

        counter("qmt_errors_total", "total errors by scope", 0)
        for scope, v in er.items():
            lines.append(f'qmt_errors_total{{scope="{scope}"}} {v}')

        # ---------------- 阶段 3：生命周期/幂等/风控/对账可观测 ----------------

        with self._lock:
            ce = dict(self._conn_events)
            idem = self._idem_hits
            rb = dict(self._risk_blocked)
            rd = dict(self._reconcile_diffs)

        lines.append("# HELP qmt_conn_events_total connection lifecycle events "
                     "(disconnected/reconnected/subscription_recovered)")
        lines.append("# TYPE qmt_conn_events_total counter")
        for (conn_id, ev), v in ce.items():
            lines.append(f'qmt_conn_events_total{{conn_id="{conn_id}",event="{ev}"}} {v}')

        counter("qmt_idempotency_hits_total", "idempotency (single-flight) hits", idem)

        lines.append("# HELP qmt_risk_blocked_total risk blocks by rule")
        lines.append("# TYPE qmt_risk_blocked_total counter")
        for rule, v in rb.items():
            lines.append(f'qmt_risk_blocked_total{{rule="{rule}"}} {v}')

        lines.append("# HELP qmt_reconcile_diffs_total reconcile diffs by scope")
        lines.append("# TYPE qmt_reconcile_diffs_total counter")
        for scope, v in rd.items():
            lines.append(f'qmt_reconcile_diffs_total{{scope="{scope}"}} {v}')

        return "\n".join(lines) + "\n"


_metrics = Metrics()


def get_metrics() -> Metrics:
    return _metrics

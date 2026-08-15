import { lazy, Suspense, useEffect, useState } from "react";
import { BrokerProvider } from "./BrokerContext.jsx";
import { useSystemStatus } from "./hooks/useSystemWS.js";
import ErrorBoundary from "./components/ErrorBoundary.jsx";
import BrokerBar from "./components/BrokerBar.jsx";
// 默认页（仪表盘）急切加载保证首屏；其余页面按需懒加载（P1 前端性能：分包 + 路由级代码分割）
import Dashboard from "./components/Dashboard.jsx";

const Quote = lazy(() => import("./components/Quote.jsx"));
const Trade = lazy(() => import("./components/Trade.jsx"));
const LimitUp = lazy(() => import("./components/LimitUp.jsx"));
const Algo = lazy(() => import("./components/Algo.jsx"));
const Backtest = lazy(() => import("./components/Backtest.jsx"));
const Factors = lazy(() => import("./components/Factors.jsx"));
const Paper = lazy(() => import("./components/Paper.jsx"));
const StrategyMarket = lazy(() => import("./components/StrategyMarket.jsx"));
const Scheduler = lazy(() => import("./components/Scheduler.jsx"));
const Observability = lazy(() => import("./components/Observability.jsx"));
const Registry = lazy(() => import("./components/Registry.jsx"));
const Alerts = lazy(() => import("./components/Alerts.jsx"));
const Webhooks = lazy(() => import("./components/Webhooks.jsx"));
const Signal = lazy(() => import("./components/Signal.jsx"));
const Reconcile = lazy(() => import("./components/Reconcile.jsx"));
const TargetPortfolio = lazy(() => import("./components/TargetPortfolio.jsx"));
const Rebalance = lazy(() => import("./components/Rebalance.jsx"));
const Strategies = lazy(() => import("./components/Strategies.jsx"));
const Reference = lazy(() => import("./components/Reference.jsx"));
const Audit = lazy(() => import("./components/Audit.jsx"));
const Agent = lazy(() => import("./components/Agent.jsx"));
const Brokers = lazy(() => import("./components/Brokers.jsx"));
const AccountsGrid = lazy(() => import("./components/AccountsGrid.jsx"));
const Settings = lazy(() => import("./components/Settings.jsx"));

const NAV = [
  { key: "dashboard", label: "仪表盘", comp: Dashboard },
  { key: "quote", label: "实时行情", comp: Quote },
  { key: "trade", label: "手动交易", comp: Trade },
  { key: "limitup", label: "涨停监控", comp: LimitUp },
  { key: "algo", label: "算法交易", comp: Algo },
  { key: "backtest", label: "回测对比", comp: Backtest },
  { key: "factors", label: "因子/指标", comp: Factors },
  { key: "paper", label: "模拟盘", comp: Paper },
  { key: "strategies", label: "策略库", comp: Strategies },
  { key: "strategy-market", label: "策略市场", comp: StrategyMarket },
  { key: "rebalance", label: "分仓再平衡", comp: Rebalance },
  { key: "reference", label: "参考数据", comp: Reference },
  { key: "audit", label: "审计日志", comp: Audit },
  { key: "agent", label: "Agent 对话", comp: Agent },
  { key: "accounts", label: "多账户网格", comp: AccountsGrid },
  { key: "brokers", label: "券商连接", comp: Brokers },
  { key: "scheduler", label: "定时任务", comp: Scheduler },
  { key: "observability", label: "可观测性", comp: Observability },
  { key: "registry", label: "注册表 V2", comp: Registry },
  { key: "alerts", label: "告警规则", comp: Alerts },
  { key: "webhooks", label: "Webhook", comp: Webhooks },
  { key: "signal", label: "外部信号", comp: Signal },
  { key: "reconcile", label: "对账核销", comp: Reconcile },
  { key: "target-portfolio", label: "目标持仓", comp: TargetPortfolio },
  { key: "settings", label: "设置", comp: Settings },
];

export default function App() {
  const [cur, setCur] = useState("dashboard");
  useEffect(() => {
    const onNav = (e) => setCur(e.detail);
    window.addEventListener("nav", onNav);
    return () => window.removeEventListener("nav", onNav);
  }, []);
  const View = NAV.find((n) => n.key === cur).comp;
  return (
    <BrokerProvider>
      <div className="app">
        <aside className="sidebar">
          <div className="brand">qmt_work<small>多券商 · 可视化 · MCP · REST · 实时同步</small></div>
          {NAV.map((n) => (
            <div key={n.key} className={`nav-item ${cur === n.key ? "active" : ""}`} onClick={() => setCur(n.key)}>
              {n.label}
            </div>
          ))}
          <SystemBadge />
        </aside>
        <main className="main">
          <BrokerBar />
          <Suspense fallback={<div className="page-loading">页面加载中…</div>}>
            <ErrorBoundary>
              <View key={cur} />
            </ErrorBoundary>
          </Suspense>
        </main>
      </div>
    </BrokerProvider>
  );
}

function SystemBadge() {
  const { status, latency } = useSystemStatus();
  const label = status === "connected" ? "● 已连接"
    : status === "reconnecting" ? "⟳ 重连中…"
    : status === "connecting" ? "○ 连接中…" : "○ 离线";
  const color = status === "connected" ? "#27c08a"
    : status === "reconnecting" ? "#e0a93b" : "#e0594f";
  return (
    <div style={{ marginTop: "auto", padding: "10px 14px", fontSize: 12, color, borderTop: "1px solid #22304a" }}>
      {label}{status === "connected" && latency != null ? ` · ${latency}ms` : ""}
    </div>
  );
}

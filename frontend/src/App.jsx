import { lazy, Suspense, useEffect, useState } from "react";
import { BrokerProvider } from "./BrokerContext.jsx";
import { useSystemStatus } from "./hooks/useSystemWS.js";
import ErrorBoundary from "./components/ErrorBoundary.jsx";
import BrokerBar from "./components/BrokerBar.jsx";
import Hub from "./components/Hub.jsx";
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

/* 合并导航：把原来扁平 25 个 tab 收敛为「分组 + Hub 子页签」。
   分组只是侧边栏视觉归类；真正减少顶层入口的是 Hub（一个领域一个入口，内部子 tab 切换）。
   注意：必须保留 "dashboard" 与 "brokers" 两个 key —— BrokerBar / Dashboard / Settings / ErrorBoundary
   通过 nav 事件硬引用它们。brokers → 账户与连接 Hub 且默认显示「连接管理」。 */
const NAV = [
  { key: "dashboard", label: "仪表盘", group: "总览", comp: Dashboard },
  { key: "quote", label: "实时行情", group: "行情", comp: Quote },

  { key: "trade", label: "交易", group: "交易", comp: () => (
    <Hub tabs={[
      { key: "trade", label: "手动交易", comp: <Trade /> },
      { key: "limitup", label: "涨停监控", comp: <LimitUp /> },
      { key: "algo", label: "算法交易", comp: <Algo /> },
      { key: "paper", label: "模拟盘", comp: <Paper /> },
    ]} />
  ) },

  { key: "strategy", label: "策略", group: "策略与组合", comp: () => (
    <Hub tabs={[
      { key: "strategies", label: "模板生成", comp: <Strategies /> },
      { key: "market", label: "策略市场", comp: <StrategyMarket /> },
    ]} />
  ) },
  { key: "portfolio", label: "调仓组合", group: "策略与组合", comp: () => (
    <Hub tabs={[
      { key: "target", label: "目标持仓", comp: <TargetPortfolio /> },
      { key: "rebalance", label: "即时再平衡", comp: <Rebalance /> },
    ]} />
  ) },

  { key: "research", label: "研究回测", group: "研究", comp: () => (
    <Hub tabs={[
      { key: "backtest", label: "回测对比", comp: <Backtest /> },
      { key: "factors", label: "因子/指标", comp: <Factors /> },
      { key: "reference", label: "参考数据", comp: <Reference /> },
    ]} />
  ) },

  { key: "signals", label: "信号与告警", group: "信号与告警", comp: () => (
    <Hub tabs={[
      { key: "signal", label: "信号路由", comp: <Signal /> },
      { key: "alerts", label: "告警规则", comp: <Alerts /> },
      { key: "webhooks", label: "出站 Webhook", comp: <Webhooks /> },
    ]} />
  ) },

  { key: "brokers", label: "账户与连接", group: "账户", comp: () => (
    <Hub initial="brokers" tabs={[
      { key: "brokers", label: "连接管理", comp: <Brokers /> },
      { key: "accounts", label: "多账户网格", comp: <AccountsGrid /> },
    ]} />
  ) },

  { key: "ops", label: "运维中心", group: "运维", comp: () => (
    <Hub tabs={[
      { key: "scheduler", label: "定时任务", comp: <Scheduler /> },
      { key: "observability", label: "可观测性", comp: <Observability /> },
      { key: "registry", label: "注册表 V2", comp: <Registry /> },
      { key: "audit", label: "审计日志", comp: <Audit /> },
    ]} />
  ) },

  { key: "reconcile", label: "对账核销", group: "运维", comp: Reconcile },
  { key: "agent", label: "Agent 对话", group: "智能", comp: Agent },
  { key: "settings", label: "设置", group: "系统", comp: Settings },
];

export default function App() {
  const [cur, setCur] = useState("dashboard");
  const [collapsed, setCollapsed] = useState({}); // 分组折叠状态
  useEffect(() => {
    const onNav = (e) => setCur(e.detail);
    window.addEventListener("nav", onNav);
    return () => window.removeEventListener("nav", onNav);
  }, []);

  // 按出现顺序收集分组
  const groups = [];
  const order = {};
  NAV.forEach((n) => { if (!(n.group in order)) { order[n.group] = groups.length; groups.push(n.group); } });

  const View = NAV.find((n) => n.key === cur)?.comp;
  const toggle = (g) => setCollapsed((c) => ({ ...c, [g]: !c[g] }));

  return (
    <BrokerProvider>
      <div className="app">
        <aside className="sidebar">
          <div className="brand">qmt_work<small>多券商 · 可视化 · MCP · REST · 实时同步</small></div>

          {groups.map((g) => {
            const items = NAV.filter((n) => n.group === g);
            const isCollapsed = !!collapsed[g];
            return (
              <div className={`nav-group ${isCollapsed ? "collapsed" : ""}`} key={g}>
                <div className="nav-group-title" onClick={() => toggle(g)}>
                  <span className="caret">{isCollapsed ? "▸" : "▾"}</span>
                  <span className="g-name">{g}</span>
                </div>
                {!isCollapsed && (
                  <div className="nav-items">
                    {items.map((n) => (
                      <div key={n.key}
                           className={`nav-item ${cur === n.key ? "active" : ""}`}
                           onClick={() => setCur(n.key)}>
                        {n.label}
                      </div>
                    ))}
                  </div>
                )}
              </div>
            );
          })}

          <SystemBadge />
        </aside>
        <main className="main">
          <BrokerBar />
          <Suspense fallback={<div className="page-loading">页面加载中…</div>}>
            <ErrorBoundary>
              {View ? <View key={cur} /> : <div className="page-loading">页面不存在</div>}
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

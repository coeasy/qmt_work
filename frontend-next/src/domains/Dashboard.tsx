import { useEffect, useState } from "react";
import { Badge, Button, EmptyState, Panel } from "@/design/primitives";
import { accountApi, systemApi, type HealthResponse } from "@/services/api";
import { useBrokerStore } from "@/stores/broker";
import { useWorkspaceStore } from "@/stores/workspace";
import { fmtMoney, fmtPrice, toneColor } from "@/shared/format";
import type { AccountStatus } from "@/shared/types";
import s from "./dashboard.module.css";

/**
 * 仪表盘：连接态 / 账户概览 / 健康状态 / 快捷入口。
 *
 * ★ 契约要点：/account/status 返回 {connected, assets, cash, position_count, positions}，
 *   没有 total_assets / profit 字段（盈亏需从 positions 聚合，本页不臆造）。
 *   /health 的 checks 用 status: pass|warn|fail，不是 ok 布尔。
 */
export function Dashboard() {
  const connections = useBrokerStore((st) => st.connections);
  const loadBrokers = useBrokerStore((st) => st.load);
  const open = useWorkspaceStore((st) => st.open);

  const [acct, setAcct] = useState<AccountStatus | null>(null);
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [err, setErr] = useState("");

  useEffect(() => {
    void loadBrokers();
    void systemApi
      .health()
      .then(setHealth)
      .catch(() => setHealth(null));
    void accountApi
      .status()
      .then((a) => {
        setAcct(a);
        setErr("");
      })
      .catch((e) => {
        setAcct(null);
        setErr(e instanceof Error ? e.message : String(e));
      });
  }, [loadBrokers]);

  const connected = connections.filter((c) => c.connected);

  // 后端 /health 的 checks 含 backtest（后端模块保留），但前端已按约定移除回测，
  // 这里过滤掉，避免界面上出现用户已明确要求下线的能力项。
  const checks = (health?.checks ?? []).filter((c) => c.name !== "backtest");

  /** 持仓市值由 positions 聚合（真实字段求和，不做估算） */
  const posMv = acct?.positions?.length
    ? acct.positions.reduce((sum, p) => sum + (p.market_value ?? 0), 0)
    : null;
  const posProfit = acct?.positions?.length
    ? acct.positions.reduce((sum, p) => sum + (p.profit ?? 0), 0)
    : null;

  return (
    <div className={s.wrap}>
      <div className={s.cards}>
        <Panel title="券商连接">
          <div className={s.cardBody}>
            <div className={s.big}>
              {connected.length}
              <span className={s.unit}>/ {connections.length}</span>
            </div>
            <div className={s.sub}>
              {connected.length > 0 ? (
                <Badge tone="success">已连接</Badge>
              ) : (
                <Badge tone="danger">未连接</Badge>
              )}
            </div>
            <Button size="sm" onClick={() => open("brokers", {}, { title: "连接管理" })}>
              管理连接
            </Button>
          </div>
        </Panel>

        <Panel title="账户概览">
          <div className={s.cardBody}>
            {acct ? (
              <>
                {/* ★ 账户金额一律 fmtMoney（2 位小数）：fmtAmount 是给成交额/量做
                    万/亿缩写的，<1 万时会 toFixed(0) 丢掉角分 —— 总资产与券商
                    对账单永远差几元，用户会以为账算错了。 */}
                <div className={s.big}>{fmtMoney(acct.assets)}</div>
                <div className={s.sub} style={{ color: toneColor(posProfit) }}>
                  持仓盈亏 {fmtMoney(posProfit)} · {acct.position_count ?? 0} 只
                </div>
                <div className={s.kvRow}>
                  <span>可用</span>
                  <span className={s.mono}>{fmtMoney(acct.cash)}</span>
                </div>
                <div className={s.kvRow}>
                  <span>持仓市值</span>
                  <span className={s.mono}>{fmtMoney(posMv)}</span>
                </div>
              </>
            ) : (
              <EmptyState
                text={err || "未连接券商，无法读取账户"}
                actionText="去连接"
                onAction={() => open("brokers", {}, { title: "连接管理" })}
              />
            )}
          </div>
        </Panel>

        <Panel title="系统健康">
          <div className={s.cardBody}>
            {health ? (
              <>
                <div className={s.big}>
                  {checks.filter((c) => c.status === "pass").length}
                  <span className={s.unit}>/ {checks.length}</span>
                </div>
                <div className={s.sub}>v{health.version ?? "—"}</div>
                <div className={s.checks}>
                  {checks.slice(0, 6).map((c) => (
                    <div key={c.name} className={s.kvRow}>
                      <span>{c.name}</span>
                      <Badge
                        tone={c.status === "pass" ? "success" : c.status === "warn" ? "warning" : "danger"}
                      >
                        {c.status === "pass" ? "OK" : c.status === "warn" ? "WARN" : "FAIL"}
                      </Badge>
                    </div>
                  ))}
                </div>
              </>
            ) : (
              <EmptyState text="无法读取健康状态" />
            )}
          </div>
        </Panel>
      </div>

      <Panel title="快捷入口">
        <div className={s.quick}>
          {[
            ["quoteboard", "报价牌"],
            ["quote", "K 线分析"],
            ["trade", "手动交易"],
            ["watchlist", "自选股"],
            ["screen", "条件选股"],
            ["algo", "算法交易"],
            ["limitup", "涨停监控"],
            ["reconcile", "对账核销"],
          ].map(([key, label]) => (
            <Button
              key={key}
              size="sm"
              onClick={() => open(key!, {}, { title: label })}
            >
              {label}
            </Button>
          ))}
        </div>
      </Panel>
    </div>
  );
}

export default Dashboard;

/** 供测试断言使用的最小工具导出 */
export const __dashPrice = fmtPrice;

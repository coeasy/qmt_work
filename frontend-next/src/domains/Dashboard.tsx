import { useEffect, useState } from "react";
import { Badge, Button, EmptyState, Panel } from "@/design/primitives";
import { accountApi, systemApi, type HealthResponse } from "@/services/api";
import { useBrokerStore } from "@/stores/broker";
import { useWorkspaceStore } from "@/stores/workspace";
import { fmtPrice } from "@/shared/format";
import type { AccountGrid } from "@/shared/types";
import { AssetSummaryCards, CrossAccountPositions } from "./account/AssetSummary";
import s from "./dashboard.module.css";

/**
 * 仪表盘：资产汇总（跨账户）/ 连接态 / 持仓情况 / 系统健康 / 快捷入口。
 *
 * ## 数据源口径（★ 本轮修正）
 *
 * 原先本页吃 `GET /account/status` —— 那只看**第一个可用连接**的账户，且没有
 * `total_assets` 字段，于是前端用 `positions.reduce()` 自己把持仓市值和盈亏
 * 加出来。结果是「同一时刻，仪表盘与多账户网格页给出两个总资产」。
 *
 * 现在统一吃 `GET /account/grid`：它才是跨账户汇总（`account.py:account_grid`），
 * 且后端已经用与 `/account/status`、`/trade/positions` **同一份** `enrich_positions`
 * 富化过持仓（现价/成本/浮动盈亏/中文名）。**本页不再做任何求和**。
 *
 * ★ 契约要点：
 *   - `/account/grid` 完全无连接时返回 `code=503` + 「尚未添加任何券商连接…」
 *   - `/health` 的 checks 用 `status: pass|warn|fail`，不是 `ok` 布尔
 *   - 金额一律 `fmtMoney`（2 位小数）；`null` 表示「无数据」而非 0
 */
export function Dashboard() {
  const connections = useBrokerStore((st) => st.connections);
  const loadBrokers = useBrokerStore((st) => st.load);
  const open = useWorkspaceStore((st) => st.open);

  const [grid, setGrid] = useState<AccountGrid | null>(null);
  const [gridLoading, setGridLoading] = useState(true);
  const [gridErr, setGridErr] = useState("");
  const [health, setHealth] = useState<HealthResponse | null>(null);

  useEffect(() => {
    void loadBrokers();
    void systemApi
      .health()
      .then(setHealth)
      .catch(() => setHealth(null));
  }, [loadBrokers]);

  useEffect(() => {
    let alive = true;
    setGridLoading(true);
    void accountApi
      .grid()
      .then((g) => {
        if (!alive) return;
        setGrid(g);
        setGridErr("");
      })
      .catch((e) => {
        if (!alive) return;
        setGrid(null);
        setGridErr(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (alive) setGridLoading(false);
      });
    return () => {
      alive = false;
    };
  }, []);

  const connected = connections.filter((c) => c.connected);

  // 后端 /health 的 checks 含 backtest（后端模块保留），但前端已按约定移除回测，
  // 这里过滤掉，避免界面上出现用户已明确要求下线的能力项。
  const checks = (health?.checks ?? []).filter((c) => c.name !== "backtest");

  return (
    <div className={s.wrap}>
      <Panel
        title="资产汇总"
        extra={
          grid ? (
            <span className={s.sub}>
              生成于 {grid.generated_at} · 共 {grid.account_count} 个账户
            </span>
          ) : undefined
        }
      >
        {gridErr ? (
          <div className={s.cardBody}>
            <EmptyState
              text={gridErr}
              actionText="去连接"
              onAction={() => open("brokers", {}, { title: "连接管理" })}
            />
          </div>
        ) : (
          <div className={s.cardBody}>
            <AssetSummaryCards grid={grid} loading={gridLoading} />
            <div className={s.sub}>
              数据源：GET /account/grid（跨账户汇总，与「多账户网格」页同源）
            </div>
          </div>
        )}
      </Panel>

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

      <Panel
        flush
        title={`持仓情况（${grid?.positions.length ?? 0} 只标的）`}
        extra={
          <Button size="sm" variant="ghost" onClick={() => open("accounts", {}, { title: "多账户网格" })}>
            多账户网格
          </Button>
        }
        className={s.positions}
      >
        <div className={s.tableArea}>
          {grid ? (
            <CrossAccountPositions
              positions={grid.positions}
              emptyText="当前无持仓（或账户未连接）"
            />
          ) : (
            <div className={s.cardBody}>
              <EmptyState
                text={gridLoading ? "读取持仓中…" : gridErr || "未连接券商，无法读取持仓"}
                actionText={gridLoading ? undefined : "去连接"}
                onAction={gridLoading ? undefined : () => open("brokers", {}, { title: "连接管理" })}
              />
            </div>
          )}
        </div>
      </Panel>

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
            <Button key={key} size="sm" onClick={() => open(key!, {}, { title: label })}>
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

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
            {/* ★ 这行说明**不写接口路径**：``GET /account/grid`` 是内部契约，
                用户不需要它；他要知道的只是「这些数字跨账户汇总过、与多账户页同源」，
                否则对不上账时不知道该拿哪一页去核。 */}
            <div className={s.sub}>跨账户汇总口径，与「多账户网格」页同源</div>
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
              /* ★ 2026-09-22：原先写死「当前无持仓（或账户未连接）」——
                 括号里的「或」把两个成因糊在一起。已有 `connected` 可用，
                 就该如实分叉：连上了就是「无持仓」，没连上才是「查不到」。 */
              emptyText={connected.length > 0
                ? "当前账户无持仓"
                : "未连接券商：无法确认持仓，此处可能不完整"}
              asOf={grid.generated_at}
            />
          ) : (
            <div className={s.cardBody}>
              <EmptyState
                text={gridLoading
                  ? "读取持仓中…"
                  : gridErr || (connected.length > 0 ? "持仓汇总返回为空" : "未连接券商，无法读取持仓")}
                /* 出口也要跟着成因走：已经连上了还把人送去「连接管理」是白跑一趟。
                   连上但没数据时面板头部本就有「多账户网格」入口，不必再给假出口。 */
                actionText={gridLoading || connected.length > 0 ? undefined : "去连接"}
                onAction={gridLoading || connected.length > 0
                  ? undefined
                  : () => open("brokers", {}, { title: "连接管理" })}
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
            // ★ 原先这里是 ["screen", "条件选股"] —— 但 `screen` 已并入「选股」
            //   工作台并设为 menu:false，从快捷入口打开它只会进到一个**只有条件
            //   面板、没有其它页签**的旧独立页。合并页面必须同步所有入口，
            //   否则用户会以为「条件选股还是单独一页」。
            ["screen_workbench", "选股"],
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

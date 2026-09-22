import { useState } from "react";
import { Button, EmptyState, Panel, Spinner, type Column, DataTable, Badge } from "@/design/primitives";
import { accountApi } from "@/services/api";
import { useAsync } from "@/hooks/useAsync";
import { useWorkspaceStore } from "@/stores/workspace";
import { fmtMoney } from "@/shared/format";
import type { AccountGrid, AccountGridPosition, AccountGridRow } from "@/shared/types";
import {
  AssetSummaryCards,
  ConnBadge,
  CrossAccountPositions,
  PositionDistribution,
} from "./AssetSummary";
import s from "../domain.module.css";

/**
 * 多账户网格（统一看板）。
 *
 * ★ 契约要点（account.py:account_grid）：
 *   - 返回 {account_count, connected_count, total_*, total_profit, accounts, positions, generated_at}
 *   - **未连接的账户也会列出**，其 error 字段说明原因（这是运维视图，不是只列健康的）
 *   - 跨账户持仓按标的汇总，accounts 子数组给出每个账户的分量
 *   - 完全无连接时后端返回 503（err(503, "尚未添加任何券商连接…")）
 *
 * ★ 汇总卡与跨账户持仓表已抽到 `./AssetSummary`，与仪表盘共用同一份实现 ——
 *   两页各自 `reduce` 求和曾导致「同一时刻两个总资产」。
 */
export function Accounts() {
  const open = useWorkspaceStore((st) => st.open);
  const grid = useAsync<AccountGrid>(() => accountApi.grid(), []);
  const [selected, setSelected] = useState<AccountGridPosition | null>(null);
  const [busy, setBusy] = useState(false);
  const [banner, setBanner] = useState<{ tone: "ok" | "error"; text: string } | null>(null);

  const data = grid.data;

  const reconnectAll = async () => {
    setBusy(true);
    setBanner(null);
    try {
      const res = await accountApi.batchReconnect([]);
      setBanner({ tone: "ok", text: `已发起重连：共 ${res.total} 个账户` });
      await grid.reload();
    } catch (e) {
      setBanner({ tone: "error", text: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy(false);
    }
  };

  const accountCols: Column<AccountGridRow>[] = [
    { key: "name", header: "账户", width: 150, render: (r) => r.name || r.conn_id },
    { key: "broker", header: "券商", width: 110, render: (r) => r.broker || "--" },
    { key: "acc", header: "账号", width: 110, mono: true, render: (r) => r.account_id || "--" },
    {
      key: "st",
      header: "状态",
      width: 72,
      render: (r) => <ConnBadge connected={r.connected} />,
    },
    { key: "assets", header: "总资产", width: 110, align: "right", mono: true, render: (r) => fmtMoney(r.assets) },
    { key: "cash", header: "可用资金", width: 110, align: "right", mono: true, render: (r) => fmtMoney(r.cash) },
    { key: "mv", header: "持仓市值", width: 110, align: "right", mono: true, render: (r) => fmtMoney(r.market_value) },
    {
      key: "pnl",
      header: "浮动盈亏",
      width: 110,
      align: "right",
      mono: true,
      render: (r) => (r.profit === null || r.profit === undefined ? <span className={s.muted}>—</span> : fmtMoney(r.profit)),
    },
    { key: "pos", header: "持仓数", width: 68, align: "right", mono: true, render: (r) => String(r.position_count) },
    { key: "ord", header: "委托", width: 62, align: "right", mono: true, render: (r) => String(r.order_count) },
    { key: "deal", header: "成交", width: 62, align: "right", mono: true, render: (r) => String(r.deal_count) },
    {
      key: "err",
      header: "错误",
      render: (r) => (r.error ? <span style={{ color: "var(--danger)" }}>{r.error}</span> : <span className={s.muted}>—</span>),
    },
  ];

  return (
    <div className={s.page}>
      <div className={s.toolbar}>
        <Button size="sm" onClick={() => void grid.reload()}>
          刷新
        </Button>
        <Button size="sm" variant="ghost" disabled={busy} onClick={() => void reconnectAll()}>
          批量重连（active）
        </Button>
        {/*
          routes.tsx 里 rebalance 声明了 `entryFrom: "accounts"`，但**本页原先并没有
          这个入口** —— 门禁只校验「menu:false 的页面有没有声明 entryFrom」，不校验
          「声明的那页上真有按钮」。结果是：声明看起来合规，用户却找不到调仓入口
          （只能靠命令面板）。这里把声明坐实。
          语义上也顺：看完跨账户持仓分布，下一步就是按目标比例调仓。
        */}
        <Button size="sm" variant="ghost" onClick={() => open("rebalance", {}, { title: "分仓再平衡" })}>
          分仓再平衡
        </Button>
        <span className={s.spacer} />
        {data && (
          <span className={s.muted}>
            共 {data.account_count} 个账户，已连接 {data.connected_count} · 生成于 {data.generated_at}
          </span>
        )}
      </div>

      {banner && (
        <div className={`${s.note} ${banner.tone === "ok" ? s.noteOk : s.noteError}`}>{banner.text}</div>
      )}

      {grid.error && (
        <div className={`${s.note} ${s.noteWarn}`}>
          {grid.error}
          {grid.error.includes("尚未添加") && " —— 请到「连接管理」添加券商。"}
        </div>
      )}

      {grid.loading && !data ? (
        <Spinner label="加载账户网格…" />
      ) : data ? (
        <>
          <AssetSummaryCards grid={data} loading={grid.loading} />

          <Panel flush title="逐账户指标" className={s.grow}>
            <div className={s.tableArea}>
              {data.accounts.length === 0 ? (
                // 「无账户」不是故障：账户来自连接管理里添加的券商连接，
                // 光秃秃三个字会让人以为接口坏了 —— 给出下一步。
                <EmptyState
                  text="尚无账户 —— 账户来自「连接管理」中添加并连接的券商"
                  actionText="去连接管理"
                  onAction={() => open("brokers", {}, { title: "连接管理" })}
                />
              ) : (
                <DataTable
                  columns={accountCols}
                  rows={data.accounts}
                  rowKey={(r) => r.conn_id}
                  rowHeight={24}
                  onRowClick={(r) => {
                    if (r.error) setBanner({ tone: "error", text: `${r.name}：${r.error}` });
                  }}
                />
              )}
            </div>
          </Panel>

          <Panel
            flush
            title={`跨账户持仓汇总（${data.positions.length}）`}
            className={s.grow}
            extra={<Badge tone="neutral">按标的合并</Badge>}
          >
            <div className={s.tableArea}>
              <CrossAccountPositions
                positions={data.positions}
                onInspect={setSelected}
                asOf={data.generated_at}
              />
            </div>
          </Panel>
        </>
      ) : null}

      {selected && (
        <Panel
          title={`持仓分布 · ${selected.code} ${selected.name}`}
          extra={
            <Button size="sm" variant="ghost" onClick={() => setSelected(null)}>
              关闭
            </Button>
          }
        >
          <PositionDistribution position={selected} />
        </Panel>
      )}
    </div>
  );
}

export default Accounts;

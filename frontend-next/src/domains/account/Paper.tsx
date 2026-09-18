import { useCallback, useMemo, useState } from "react";
import {
  Badge,
  Button,
  DataTable,
  EmptyState,
  Panel,
  Spinner,
  Tabs,
  type Column,
} from "@/design/primitives";
import { paperApi, type PaperPosition, type PaperTrade } from "@/services/api";
import { useAsync } from "@/hooks/useAsync";
import { useLiveQuotes } from "@/hooks/useLiveQuotes";
import { fmtMoney, fmtPct, fmtPrice, tone, toneColor } from "@/shared/format";
import s from "../domain.module.css";

type Tab = "positions" | "trades" | "metrics";

/**
 * 模拟盘（Paper Trading）。
 *
 * 补齐动机：后端 PaperEngine 能力完整（下单/持仓/成交/绩效/重置），而旧前端**零入口**
 * —— 用户在「外部信号」页能把模式切成 paper，下单后却无处查看模拟成交与持仓，
 * 体验断裂。本页把这 6 个端点接进账户域。
 *
 * ★ 契约要点（已核对 paper.py + paper_engine.py）：
 *   - /paper/reset 返回的是**重置后的 account**（不是 {ok:true}），可直接回写概览。
 *   - /paper/account 的 total_return 是小数（0.0123 = 1.23%），展示需 ×100。
 *   - 成交记录主键是 id（不是 trade_id）；pnl 仅卖出平仓有值，买入为 0/null。
 *   - marking_source=frozen 表示**没拿到实时行情**，市值/浮亏退化为成本价 ——
 *     必须显式提示，否则用户会误以为「浮动盈亏 0 = 没亏」。
 *   - 引擎未初始化时后端返回 503，本页显式提示，绝不展示空表冒充「无成交」。
 */
export function Paper() {
  const [tab, setTab] = useState<Tab>("positions");

  const account = useAsync(() => paperApi.account(), []);
  const positions = useAsync(() => paperApi.positions(), []);
  const trades = useAsync(() => paperApi.trades(50), []);
  const metrics = useAsync(() => paperApi.metrics(), []);

  // 下单表单
  const [code, setCode] = useState("");
  const [side, setSide] = useState<"buy" | "sell">("buy");
  const [price, setPrice] = useState("");
  const [volume, setVolume] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [orderErr, setOrderErr] = useState<string | null>(null);

  // 重置是破坏性操作：两段式确认，避免误点清空全部模拟持仓
  const [confirmReset, setConfirmReset] = useState(false);

  const reloadAll = useCallback(() => {
    void account.reload();
    void positions.reload();
    void trades.reload();
    void metrics.reload();
  }, [account, positions, trades, metrics]);

  const submitOrder = useCallback(async () => {
    setOrderErr(null);
    const p = Number(price);
    const v = Number(volume);
    if (!code.trim()) return setOrderErr("请填写标的代码");
    if (!(p > 0)) return setOrderErr("价格必须大于 0");
    if (!(v > 0)) return setOrderErr("数量必须大于 0");
    setSubmitting(true);
    try {
      await paperApi.order({
        code: code.trim().toUpperCase(),
        side,
        price: p,
        volume: v,
        price_type: "limit",
      });
      setCode("");
      setPrice("");
      setVolume("");
      reloadAll();
    } catch (exc) {
      setOrderErr(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setSubmitting(false);
    }
  }, [code, side, price, volume, reloadAll]);

  const doReset = useCallback(async () => {
    setConfirmReset(false);
    try {
      await paperApi.reset();
      reloadAll();
    } catch {
      void account.reload();
    }
  }, [reloadAll, account]);

  const acc = account.data;
  const retPct = acc ? acc.total_return * 100 : 0;
  const frozen = acc?.marking_source === "frozen";

  // ★ 模拟盘叠加实时行情：后端 marking_source=frozen 表示**没取到实时行情**，
  //   此时 last_price 退化为成本价、浮亏恒为 0 —— 界面上会显示成「逼真的 0.00」，
  //   用户会误以为没亏。前端能拿到行情时优先用实时价，并据此重算市值/浮亏。
  const positionCodes = useMemo(
    () => (positions.data ?? []).map((p) => p.code),
    [positions.data],
  );
  const quotes = useLiveQuotes(positionCodes);
  /** 实时价优先，取不到才回退后端快照（绝不用 0 冒充价格） */
  const lastPrice = (r: PaperPosition): number | undefined => {
    const q = quotes[r.code]?.price;
    if (q !== undefined && q > 0) return q;
    return r.last_price !== undefined && r.last_price > 0 ? r.last_price : undefined;
  };
  const lastPct = (r: PaperPosition): number | undefined => quotes[r.code]?.change_pct;

  const positionCols: Column<PaperPosition>[] = [
    { key: "code", header: "代码", width: 96, mono: true, render: (r) => r.code },
    { key: "name", header: "名称", width: 92, render: (r) => r.name || "--" },
    { key: "vol", header: "持仓", width: 76, align: "right", mono: true, render: (r) => String(r.volume) },
    { key: "cost", header: "成本价", width: 78, align: "right", mono: true, render: (r) => fmtPrice(r.avg_cost) },
    { key: "last", header: "现价", width: 78, align: "right", mono: true, render: (r) => fmtPrice(lastPrice(r)) },
    {
      key: "chg",
      header: "涨跌",
      width: 78,
      align: "right",
      mono: true,
      render: (r) => <span style={{ color: toneColor(lastPct(r)) }}>{fmtPct(lastPct(r))}</span>,
    },
    {
      key: "mv",
      header: "市值",
      width: 104,
      align: "right",
      mono: true,
      render: (r) => {
        const p = lastPrice(r);
        const mv = p !== undefined && p > 0 ? p * r.volume : r.market_value;
        return fmtMoney(mv);
      },
    },
    {
      key: "pnl",
      header: "浮动盈亏",
      width: 104,
      align: "right",
      mono: true,
      render: (r) => {
        const p = lastPrice(r);
        // ★ 算不出就显示 "--"，**绝不用 0 冒充**：后端行情冻结、均价缺失时，
        // 这里若兜底成 0，界面会显示「浮亏 0.00」——用户会以为持仓不盈不亏，
        // 与「没数据」是完全不同的两件事。
        const v =
          p !== undefined && p > 0 && r.avg_cost !== undefined
            ? (p - r.avg_cost) * r.volume
            : r.unrealized_pnl;
        return <span style={{ color: toneColor(v) }}>{fmtMoney(v)}</span>;
      },
    },
    {
      key: "marking",
      header: "盯市",
      width: 76,
      render: (r) => {
        // 前端拿到实时价即视为「实时」—— 后端 frozen 只说明**它自己**没取到
        const live = lastPrice(r) !== undefined && quotes[r.code] !== undefined;
        return (
          <Badge tone={live ? "success" : r.marking === "live" ? "success" : "neutral"}>
            {live ? "实时" : r.marking === "live" ? "实时" : "成本价"}
          </Badge>
        );
      },
    },
  ];

  const tradeCols: Column<PaperTrade>[] = [
    { key: "id", header: "编号", width: 68, mono: true, render: (r) => String(r.id) },
    { key: "code", header: "代码", width: 96, mono: true, render: (r) => r.code },
    {
      key: "side",
      header: "方向",
      width: 56,
      render: (r) => (
        <span style={{ color: r.side === "buy" ? "var(--up)" : "var(--down)" }}>
          {r.side === "buy" ? "买入" : "卖出"}
        </span>
      ),
    },
    { key: "price", header: "成交价", width: 80, align: "right", mono: true, render: (r) => fmtPrice(r.price) },
    { key: "vol", header: "数量", width: 76, align: "right", mono: true, render: (r) => String(r.volume) },
    {
      key: "pnl",
      header: "平仓盈亏",
      width: 96,
      align: "right",
      mono: true,
      render: (r) =>
        r.side === "sell" ? (
          <span style={{ color: toneColor(Number(r.pnl ?? 0) || 0) }}>
            {fmtMoney(Number(r.pnl ?? 0) || 0)}
          </span>
        ) : (
          <span className={s.muted}>—</span>
        ),
    },
    { key: "ts", header: "时间", width: 150, mono: true, render: (r) => r.ts ?? "--" },
  ];

  const cur =
    tab === "positions" ? positions : tab === "trades" ? trades : metrics;
  const engineDown = !!account.error;

  return (
    <div className={s.page}>
      <div className={s.toolbar}>
        <span className={s.muted}>
          模拟盘：虚拟资金 + 真实行情盯市，用于策略演练与联调，不产生真实委托。
        </span>
        <span className={s.spacer} />
        {confirmReset ? (
          <>
            <span className={s.muted}>确认清空全部模拟持仓与成交？</span>
            <Button size="sm" variant="danger" onClick={() => void doReset()}>
              确认重置
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setConfirmReset(false)}>
              取消
            </Button>
          </>
        ) : (
          <Button size="sm" variant="ghost" onClick={() => setConfirmReset(true)}>
            重置账户
          </Button>
        )}
        <Button size="sm" onClick={reloadAll}>
          刷新
        </Button>
      </div>

      {engineDown && (
        <div className={`${s.note} ${s.noteError}`}>
          模拟盘引擎未就绪：{account.error}
        </div>
      )}

      {acc && (
        <>
          <div className={s.stats}>
            <div className={s.stat}>
              <span className={s.statLabel}>总资产</span>
              <span className={s.statValue}>{fmtMoney(acc.total_assets)}</span>
              <span className={s.statSub}>初始 {fmtMoney(acc.initial)}</span>
            </div>
            <div className={s.stat}>
              <span className={s.statLabel}>可用资金</span>
              <span className={s.statValue}>{fmtMoney(acc.cash)}</span>
            </div>
            <div className={s.stat}>
              <span className={s.statLabel}>持仓市值</span>
              <span className={s.statValue}>{fmtMoney(acc.market_value)}</span>
              <span className={s.statSub}>{acc.position_count} 只</span>
            </div>
            <div className={s.stat}>
              <span className={s.statLabel}>总收益率</span>
              <span className={s.statValue} style={{ color: toneColor(retPct) }}>
                {retPct >= 0 ? "+" : ""}
                {retPct.toFixed(2)}%
              </span>
            </div>
            <div className={s.stat}>
              <span className={s.statLabel}>浮动盈亏</span>
              <span className={s.statValue} style={{ color: toneColor(acc.unrealized_pnl) }}>
                {fmtMoney(acc.unrealized_pnl)}
              </span>
            </div>
            <div className={s.stat}>
              <span className={s.statLabel}>已实现盈亏</span>
              <span className={s.statValue} style={{ color: toneColor(acc.realized_pnl) }}>
                {fmtMoney(acc.realized_pnl)}
              </span>
            </div>
          </div>

          {frozen && (
            <div className={`${s.note} ${s.noteWarn}`}>
              后端报告<b>未取到实时行情</b>（marking_source=frozen）：其返回的市值与浮动盈亏退化为
              <b>成本价</b>估算，并非真实盯市结果。本页已尝试叠加前端实时行情（「现价 / 涨跌」列），
              <b>盯市</b>列显示「成本价」即表示该标的仍未取到行情。
            </div>
          )}
        </>
      )}

      {/* 模拟下单 */}
      <Panel title="模拟下单" className={s.inline}>
        <div className={s.actions}>
          <input
            className={s.mono}
            style={INPUT}
            placeholder="代码 600519.SH"
            value={code}
            onChange={(e) => setCode(e.target.value)}
          />
          <select value={side} onChange={(e) => setSide(e.target.value as "buy" | "sell")} style={INPUT}>
            <option value="buy">买入</option>
            <option value="sell">卖出</option>
          </select>
          <input
            className={s.mono}
            style={{ ...INPUT, width: 90 }}
            placeholder="价格"
            value={price}
            onChange={(e) => setPrice(e.target.value)}
          />
          <input
            className={s.mono}
            style={{ ...INPUT, width: 90 }}
            placeholder="数量"
            value={volume}
            onChange={(e) => setVolume(e.target.value)}
          />
          <Button size="sm" disabled={submitting} onClick={() => void submitOrder()}>
            {submitting ? "提交中…" : "下单"}
          </Button>
        </div>
        {orderErr && <div className={`${s.note} ${s.noteError}`}>{orderErr}</div>}
        <div className={s.note}>
          模拟撮合按给定价格<b>立即全额成交</b>（不含撮合排队）；现金或可卖数量不足时后端返回 400。
        </div>
      </Panel>

      <Panel
        flush
        className={s.grow}
        title="模拟盘明细"
        extra={
          <Tabs
            items={[
              { key: "positions", label: `持仓 ${positions.data?.length ?? 0}` },
              { key: "trades", label: `成交 ${trades.data?.length ?? 0}` },
              { key: "metrics", label: "绩效" },
            ]}
            value={tab}
            onChange={setTab}
          />
        }
      >
        <div className={s.tableArea}>
          {cur.loading ? (
            <Spinner label="加载中…" />
          ) : tab === "positions" ? (
            (positions.data ?? []).length === 0 ? (
              <EmptyState text={engineDown ? "引擎未就绪" : "暂无模拟持仓"} />
            ) : (
              <DataTable
                columns={positionCols}
                rows={positions.data ?? []}
                rowKey={(r) => r.code}
                rowHeight={24}
                rowTone={(r) => tone(r.unrealized_pnl)}
              />
            )
          ) : tab === "trades" ? (
            (trades.data ?? []).length === 0 ? (
              <EmptyState text={engineDown ? "引擎未就绪" : "暂无模拟成交"} />
            ) : (
              <DataTable
                columns={tradeCols}
                rows={trades.data ?? []}
                rowKey={(r) => String(r.id)}
                rowHeight={24}
              />
            )
          ) : metrics.data === null ? (
            <EmptyState text={engineDown ? "引擎未就绪" : "暂无绩效数据"} />
          ) : (
            <div className={s.stats}>
              <div className={s.stat}>
                <span className={s.statLabel}>成交笔数</span>
                <span className={s.statValue}>{metrics.data.trade_count}</span>
                <span className={s.statSub}>平仓 {metrics.data.close_count} 笔</span>
              </div>
              <div className={s.stat}>
                <span className={s.statLabel}>胜率</span>
                <span className={s.statValue}>{(metrics.data.win_rate * 100).toFixed(1)}%</span>
                <span className={s.statSub}>盈利 {metrics.data.win_count} 笔</span>
              </div>
              <div className={s.stat}>
                <span className={s.statLabel}>平均盈亏</span>
                <span className={s.statValue} style={{ color: toneColor(metrics.data.avg_pnl) }}>
                  {fmtMoney(metrics.data.avg_pnl)}
                </span>
              </div>
              <div className={s.stat}>
                <span className={s.statLabel}>最好 / 最差</span>
                <span className={s.statValue}>
                  {fmtMoney(metrics.data.best_pnl)} / {fmtMoney(metrics.data.worst_pnl)}
                </span>
              </div>
            </div>
          )}
          {cur.error && !cur.loading && (
            <div className={`${s.note} ${s.noteError}`}>加载失败：{cur.error}</div>
          )}
        </div>
      </Panel>
    </div>
  );
}

const INPUT: React.CSSProperties = {
  width: 130,
  padding: "2px 6px",
  border: "1px solid var(--border)",
  borderRadius: "var(--radius-sm)",
  background: "var(--bg-2)",
  color: "var(--text)",
};

export default Paper;

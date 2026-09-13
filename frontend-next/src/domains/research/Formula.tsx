import { useState } from "react";
import {
  Badge,
  Button,
  DataTable,
  EmptyState,
  FormRow,
  Input,
  Panel,
  Select,
  Spinner,
  type Column,
} from "@/design/primitives";
import { screenApi, type ScreenResponse, type ScreenRow } from "@/services/api";
import { fmtPct, fmtPrice, toneColor } from "@/shared/format";
import s from "../domain.module.css";

/**
 * 公式选股（类通达信公式 DSL）。
 *
 * ★ 契约要点（screen.py:market_screen_expr）：
 *   - POST /market/screen/expr，body.expr 必须为非空字符串；解析失败返回 400 + 原因
 *   - 公式经 app/indicators/dsl.py 解析为条件树，再走与 /market/screen **同一个**求值内核
 *   - 因此公式与条件树能力等价，不存在「公式能跑但条件树不行」的分叉
 *   - 返回结构与条件选股一致（count/total_scanned/results/provenance/degraded）
 *
 * 示例：C > MA(20) AND RSI(14) < 30
 * 注意：截面算子 RANK(x) / TOP(x,n) 由条件评估层在后续版本接入，当前不可用 —— 本页不假装支持。
 */
const EXAMPLES = [
  "C > MA(20)",
  "C > MA(20) AND RSI(14) < 30",
  "VOL > VOL_MA(5) * 1.5",
  "MACD > 0 AND KDJ < 30",
];

export function Formula() {
  const [expr, setExpr] = useState("C > MA(20) AND RSI(14) < 30");
  const [limit, setLimit] = useState("100");
  const [sortBy, setSortBy] = useState("score");
  const [adjust, setAdjust] = useState("qfq");
  const [sourcePolicy, setSourcePolicy] = useState("auto");

  const [res, setRes] = useState<ScreenResponse | null>(null);
  const [busy, setBusy] = useState(false);
  const [banner, setBanner] = useState<{ tone: "ok" | "error"; text: string } | null>(null);

  const run = async () => {
    setBusy(true);
    setBanner(null);
    try {
      const out = await screenApi.expr({
        expr,
        limit: Number(limit) || 100,
        sort_by: sortBy,
        sort_desc: 1,
        adjust,
        source_policy: sourcePolicy,
      });
      setRes(out);
      setBanner({
        tone: "ok",
        text: `公式解析成功，扫描 ${out.total_scanned} 只，命中 ${out.count} 只，耗时 ${out.elapsed_ms}ms`,
      });
    } catch (e) {
      setRes(null);
      setBanner({ tone: "error", text: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy(false);
    }
  };

  const cols: Column<ScreenRow>[] = [
    { key: "code", header: "代码", width: 100, mono: true, render: (r) => r.code },
    { key: "name", header: "名称", width: 120, render: (r) => r.name ?? "--" },
    { key: "close", header: "收盘", width: 86, align: "right", mono: true, render: (r) => fmtPrice(r.close) },
    {
      key: "pct",
      header: "涨跌幅",
      width: 86,
      align: "right",
      mono: true,
      render: (r) => <span style={{ color: toneColor(r.change_pct) }}>{fmtPct(r.change_pct)}</span>,
    },
    {
      key: "score",
      header: "命中分",
      width: 80,
      align: "right",
      mono: true,
      render: (r) => (r.score === undefined ? "--" : String(r.score)),
    },
  ];

  return (
    <div className={s.page}>
      <Panel title="公式输入">
        <div className={s.form}>
          <FormRow label="公式">
            <Input value={expr} onChange={(e) => setExpr(e.target.value)} mono placeholder="C > MA(20)" />
          </FormRow>

          <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
            {EXAMPLES.map((x) => (
              <Button key={x} size="sm" variant="ghost" onClick={() => setExpr(x)}>
                {x}
              </Button>
            ))}
          </div>

          <div className={s.cols4}>
            <FormRow label="数据源策略">
              <Select
                value={sourcePolicy}
                onChange={(e) => setSourcePolicy(e.target.value)}
                options={[
                  { value: "auto", label: "auto" },
                  { value: "prefer_qmt", label: "prefer_qmt" },
                  { value: "qmt_only", label: "qmt_only" },
                  { value: "local_only", label: "local_only" },
                ]}
              />
            </FormRow>
            <FormRow label="复权">
              <Select
                value={adjust}
                onChange={(e) => setAdjust(e.target.value)}
                options={[
                  { value: "qfq", label: "前复权" },
                  { value: "hfq", label: "后复权" },
                  { value: "", label: "不复权" },
                ]}
              />
            </FormRow>
            <FormRow label="排序">
              <Select
                value={sortBy}
                onChange={(e) => setSortBy(e.target.value)}
                options={[
                  { value: "score", label: "命中分" },
                  { value: "close", label: "收盘价" },
                  { value: "change_pct", label: "涨跌幅" },
                ]}
              />
            </FormRow>
            <FormRow label="条数">
              <Input value={limit} onChange={(e) => setLimit(e.target.value)} mono />
            </FormRow>
          </div>

          <Button block disabled={busy || !expr.trim()} onClick={() => void run()}>
            {busy ? "解析并扫描…" : "执行公式"}
          </Button>

          <div className={s.note}>
            可用函数与字段由后端指标注册表驱动（MA / EMA / MACD / KDJ / RSI / BOLL / VOL_MA …），
            字段为 C（收盘）/ O（开盘）/ H（最高）/ L（最低）/ VOL（成交量）。
            截面算子 RANK / TOP 尚未接入求值层，使用会返回解析错误 —— 本页不提供假按钮。
          </div>
        </div>
      </Panel>

      {banner && (
        <div className={`${s.note} ${banner.tone === "ok" ? s.noteOk : s.noteError}`}>{banner.text}</div>
      )}

      {res && (
        <div className={s.stats}>
          <div className={s.stat}>
            <span className={s.statLabel}>命中</span>
            <span className={s.statValue}>{res.count}</span>
          </div>
          <div className={s.stat}>
            <span className={s.statLabel}>扫描</span>
            <span className={s.statValue}>{res.total_scanned}</span>
          </div>
          <div className={s.stat}>
            <span className={s.statLabel}>耗时</span>
            <span className={s.statValue}>{res.elapsed_ms}ms</span>
          </div>
          <div className={s.stat}>
            <span className={s.statLabel}>数据源</span>
            <span className={s.statValue} style={{ fontSize: "var(--font-sm)" }}>
              {String(res.provenance?.provider_used ?? "--")}
            </span>
            <span className={s.statSub}>{res.degraded ? "已降级" : "未降级"}</span>
          </div>
        </div>
      )}

      <Panel
        flush
        className={s.grow}
        title={`选股结果（${res?.count ?? 0}）`}
        extra={res ? <Badge tone="info">解析后的条件树见溯源</Badge> : null}
      >
        <div className={s.tableArea}>
          {busy && !res ? (
            <Spinner label="扫描中…" />
          ) : !res ? (
            <EmptyState text="尚未执行公式" />
          ) : res.results.length === 0 ? (
            <EmptyState text="无符合条件的标的" />
          ) : (
            <DataTable
              columns={cols}
              rows={res.results}
              rowKey={(r) => r.code}
              rowHeight={24}
              rowTone={(r) => (r.change_pct && r.change_pct > 0 ? 1 : r.change_pct && r.change_pct < 0 ? -1 : 0)}
            />
          )}
        </div>
      </Panel>

      {res && (
        <div className={s.note}>
          <b>解析结果</b>：{JSON.stringify(res.conditions)}
        </div>
      )}
    </div>
  );
}

export default Formula;

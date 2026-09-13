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
 * 条件选股。
 *
 * ★ 契约要点（screen.py + app/screener/conditions.py）：
 *   - GET /market/screen 的 conditions 是**必填** JSON 字符串，顶层须为对象
 *   - 条件树文法（与公式 DSL 共用同一求值内核）：
 *       · 组合节点：{"and":[...]} / {"or":[...]} / {"not":{...}}
 *       · 字段叶子：{"field":{"name":"close|open|high|low|volume","window":-1,"op":"gt","value":10}}
 *       · 指标叶子：{"indicator":{"name":"ma|rsi|macd|kdj|boll|volume_ma","params":{"win":20},
 *                    "window":-1,"op":"gt","value":0}}
 *       · 双操作数比较：{"compare":{"op":"gt","left":{"kind":"field",...},"right":{"kind":"indicator",...}}}
 *       · 操作符仅 gt/gte/lt/lte/eq/ne；window 为 -1 表示最新一根
 *   - 数据源与本地仓皆不可用时返回 503（零 mock），本页原样展示，不显示「无符合标的」
 *   - provenance/degraded 字段用于说明数据来自哪条源链、是否降级 —— 本页显式呈现
 */
const PRESET_EXAMPLES: Array<{ label: string; conditions: string }> = [
  {
    label: "收盘价站上 20 日均线",
    conditions: JSON.stringify(
      {
        compare: {
          op: "gt",
          left: { kind: "field", name: "close", window: -1 },
          right: { kind: "indicator", name: "ma", params: { win: 20 }, window: -1 },
        },
      },
      null,
      2,
    ),
  },
  {
    label: "RSI 超卖（< 30）",
    conditions: JSON.stringify(
      { indicator: { name: "rsi", params: { win: 14 }, output: "rsi", window: -1, op: "lt", value: 30 } },
      null,
      2,
    ),
  },
  {
    label: "放量（量 > 5 日均量 1.5 倍）且收阳",
    conditions: JSON.stringify(
      {
        and: [
          { indicator: { name: "volume_ma", params: { win: 5 }, window: -1, op: "gt", value: 0 } },
          { field: { name: "close", window: -1, op: "gt", value: 0 } },
        ],
      },
      null,
      2,
    ),
  },
];

export function Screen() {
  const [conditions, setConditions] = useState(PRESET_EXAMPLES[0]?.conditions ?? "{}");
  const [universe, setUniverse] = useState("");
  const [limit, setLimit] = useState("100");
  const [sortBy, setSortBy] = useState("score");
  const [sourcePolicy, setSourcePolicy] = useState("auto");
  const [adjust, setAdjust] = useState("qfq");
  const [nlText, setNlText] = useState("");

  const [res, setRes] = useState<ScreenResponse | null>(null);
  const [busy, setBusy] = useState(false);
  const [banner, setBanner] = useState<{ tone: "ok" | "error" | "warn"; text: string } | null>(null);
  const [boardName, setBoardName] = useState("");

  const run = async () => {
    let parsed: unknown;
    try {
      parsed = JSON.parse(conditions);
    } catch (e) {
      setBanner({ tone: "error", text: `conditions 不是合法 JSON：${e instanceof Error ? e.message : e}` });
      return;
    }
    setBusy(true);
    setBanner(null);
    try {
      const out = await screenApi.run({
        conditions: JSON.stringify(parsed),
        limit: Number(limit) || 100,
        sort_by: sortBy,
        sort_desc: 1,
        adjust,
        source_policy: sourcePolicy,
        universe: universe || undefined,
      });
      setRes(out);
      setBanner({
        tone: "ok",
        text: `扫描 ${out.total_scanned} 只，命中 ${out.count} 只，耗时 ${out.elapsed_ms}ms`,
      });
    } catch (e) {
      setRes(null);
      setBanner({ tone: "error", text: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy(false);
    }
  };

  /** 自然语言 → 条件树（只解析，需再次执行） */
  const parseNl = async () => {
    setBusy(true);
    setBanner(null);
    try {
      const out = await screenApi.nl(nlText);
      if (out.conditions) {
        setConditions(JSON.stringify(out.conditions, null, 2));
        setBanner({ tone: "ok", text: "已解析为条件树，请检查后点击「执行选股」" });
      } else {
        setBanner({ tone: "warn", text: `未解析出条件树：${JSON.stringify(out)}` });
      }
      if (out.unsupported) {
        setBanner((prev) => ({
          tone: "warn",
          text: `${prev?.text ?? ""} · 存在未支持的表达：${JSON.stringify(out.unsupported)}`,
        }));
      }
    } catch (e) {
      setBanner({ tone: "error", text: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy(false);
    }
  };

  const saveBoard = async () => {
    if (!res) return;
    setBusy(true);
    setBanner(null);
    try {
      await screenApi.saveBoard({
        name: boardName || `选股_${new Date().toISOString().slice(0, 16)}`,
        conditions: JSON.parse(conditions) as Record<string, unknown>,
        results: res.results.map((r) => ({
          code: r.code,
          name: r.name,
          close: r.close,
          change_pct: r.change_pct,
        })),
      });
      setBanner({ tone: "ok", text: `已存为动态板块：${boardName || "未命名"}` });
      setBoardName("");
    } catch (e) {
      setBanner({ tone: "error", text: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy(false);
    }
  };

  const cols: Column<ScreenRow>[] = [
    { key: "code", header: "代码", width: 100, mono: true, render: (r) => r.code },
    { key: "name", header: "名称", width: 110, render: (r) => r.name ?? "--" },
    { key: "close", header: "收盘", width: 84, align: "right", mono: true, render: (r) => fmtPrice(r.close) },
    {
      key: "pct",
      header: "涨跌幅",
      width: 84,
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
      <div className={s.split}>
        <Panel title="选股条件">
          <div className={s.form}>
            <FormRow label="自然语言">
              <div className={s.inline}>
                <Input
                  value={nlText}
                  onChange={(e) => setNlText(e.target.value)}
                  placeholder="如：收盘价站上20日线且量比大于1.5"
                />
                <Button size="sm" disabled={busy || !nlText} onClick={() => void parseNl()}>
                  解析
                </Button>
              </div>
            </FormRow>

            <FormRow label="预设">
              <Select
                value=""
                onChange={(e) => {
                  const p = PRESET_EXAMPLES.find((x) => x.label === e.target.value);
                  if (p) setConditions(p.conditions);
                }}
                options={[{ value: "", label: "选择预设条件…" }, ...PRESET_EXAMPLES.map((p) => ({ value: p.label, label: p.label }))]}
              />
            </FormRow>

            <FormRow label="条件树 JSON">
              <textarea
                value={conditions}
                onChange={(e) => setConditions(e.target.value)}
                spellCheck={false}
                rows={12}
                style={{
                  width: "100%",
                  fontFamily: "var(--font-mono)",
                  fontSize: "var(--font-xs)",
                  background: "var(--bg-2)",
                  color: "var(--text)",
                  border: "1px solid var(--border)",
                  borderRadius: "var(--radius-sm)",
                  padding: 6,
                  resize: "vertical",
                }}
              />
            </FormRow>

            <FormRow label="股票池">
              <Input
                value={universe}
                onChange={(e) => setUniverse(e.target.value)}
                mono
                placeholder='空=全市场；或 "sector:医药" / "index:000300"'
              />
            </FormRow>

            <div className={s.cols2}>
              <FormRow label="数据源策略">
                <Select
                  value={sourcePolicy}
                  onChange={(e) => setSourcePolicy(e.target.value)}
                  options={[
                    { value: "auto", label: "auto（按链降级）" },
                    { value: "prefer_qmt", label: "prefer_qmt" },
                    { value: "qmt_only", label: "qmt_only" },
                    { value: "local_only", label: "local_only（离线）" },
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
              <FormRow label="排序字段">
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
              <FormRow label="返回条数">
                <Input value={limit} onChange={(e) => setLimit(e.target.value)} mono />
              </FormRow>
            </div>

            <Button block disabled={busy} onClick={() => void run()}>
              {busy ? "执行中…" : "执行选股"}
            </Button>
          </div>
        </Panel>

        <div className={s.grow} style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          {banner && (
            <div
              className={`${s.note} ${
                banner.tone === "ok" ? s.noteOk : banner.tone === "warn" ? s.noteWarn : s.noteError
              }`}
            >
              {banner.text}
            </div>
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
                <span className={s.statSub}>
                  {res.degraded ? `已降级：${res.degraded_reason ?? ""}` : "未降级"}
                </span>
              </div>
            </div>
          )}

          {res && (
            <div className={s.toolbar}>
              <Input
                value={boardName}
                onChange={(e) => setBoardName(e.target.value)}
                style={{ width: 200 }}
                placeholder="动态板块名称"
              />
              <Button size="sm" variant="ghost" disabled={busy || res.count === 0} onClick={() => void saveBoard()}>
                存为动态板块
              </Button>
              <span className={s.spacer} />
              <Badge tone="info">排序 {res.sort_by}</Badge>
            </div>
          )}

          <Panel flush className={s.grow} title={`选股结果（${res?.count ?? 0}）`}>
            <div className={s.tableArea}>
              {busy && !res ? (
                <Spinner label="扫描中…" />
              ) : !res ? (
                <EmptyState text="尚未执行选股" />
              ) : res.results.length === 0 ? (
                <EmptyState text="无符合条件的标的（已成功扫描，非数据源不可用）" />
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
              <b>数据溯源</b>：{JSON.stringify(res.provenance)}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

export default Screen;

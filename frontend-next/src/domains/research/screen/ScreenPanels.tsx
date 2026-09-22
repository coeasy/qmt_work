import { useEffect, useMemo, useState, type ReactNode } from "react";
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
import {
  screenApi,
  type ClassicStrategy,
  type ScreenResponse,
  type ScreenRow,
} from "@/services/api";
import { useLiveQuotes } from "@/hooks/useLiveQuotes";
import { useBrokerStore } from "@/stores/broker";
import { useWatchlistStore } from "@/stores/watchlist";
import { fmtDateTime } from "@/shared/time";
import { fmtPct, fmtPrice, fmtVolume, toneColor } from "@/shared/format";
import { isLivePrice } from "@/shared/freshness";
import type { Quote } from "@/shared/types";
import s from "../../domain.module.css";

/**
 * 选股面板（条件选股 / 公式选股）与两者共用的零件。
 *
 * 为什么合并到一个文件：这两页的**结果侧完全同构** —— 同一份 ScreenResponse、
 * 同一组选项（数据源策略 / 复权 / 排序 / 条数）、同一张结果表、同一种溯源脚注。
 * 原先两个页面各写一遍，已经出现漂移（列宽 84/86、统计条文案「已降级」一处带
 * 原因一处不带）。「相近功能合并展示」要消灭的就是这种同源两写。
 *
 * ★ 契约要点（screen.py + app/screener/conditions.py）：
 *   - GET /market/screen 的 conditions 是**必填** JSON 字符串，顶层须为对象
 *   - POST /market/screen/expr 的 expr 必须为非空字符串；解析失败 400 + 原因
 *   - 两者经**同一个求值内核**（app/indicators/dsl.py → 条件树），返回结构一致：
 *     {count,total_scanned,results,elapsed_ms,sort_by,provenance,degraded,degraded_reason}
 *   - 条件树文法：
 *       · 组合节点：{"and":[...]} / {"or":[...]} / {"not":{...}}
 *       · 字段叶子：{"field":{"name":"close|open|high|low|volume","window":-1,"op":"gt","value":10}}
 *       · 指标叶子：{"indicator":{"name":"ma|rsi|macd|kdj|boll|volume_ma","params":{"win":20},
 *                    "window":-1,"op":"gt","value":0}}
 *       · 双操作数比较：{"compare":{"op":"gt","left":{"kind":"field",...},"right":{"kind":"indicator",...}}}
 *       · 操作符仅 gt/gte/lt/lte/eq/ne；window 为 -1 表示最新一根
 *   - 数据源与本地仓皆不可用时返回 503（零 mock），本页原样展示，不显示「无符合标的」
 */

export const SOURCE_POLICY_OPTIONS = [
  { value: "auto", label: "auto（按链降级）" },
  { value: "prefer_qmt", label: "prefer_qmt" },
  { value: "qmt_only", label: "qmt_only" },
  { value: "local_only", label: "local_only（离线）" },
];

export const ADJUST_OPTIONS = [
  { value: "qfq", label: "前复权" },
  { value: "hfq", label: "后复权" },
  { value: "", label: "不复权" },
];

export const SORT_OPTIONS = [
  { value: "score", label: "命中分" },
  { value: "close", label: "收盘价" },
  { value: "change_pct", label: "涨跌幅" },
];

export interface ScreenQuery {
  sourcePolicy: string;
  adjust: string;
  sortBy: string;
  limit: string;
}

export const DEFAULT_QUERY: ScreenQuery = {
  sourcePolicy: "auto",
  adjust: "qfq",
  sortBy: "score",
  limit: "100",
};

/** 四个共用选项控件（条件选股与公式选股的请求体字段完全一致） */
export function ScreenOptions({
  q,
  onChange,
  columns = 4,
}: {
  q: ScreenQuery;
  onChange: (patch: Partial<ScreenQuery>) => void;
  /** 2 = 条件选股页的窄栏布局；4 = 公式选股页的宽栏布局 */
  columns?: 2 | 4;
}) {
  return (
    <div className={columns === 2 ? s.cols2 : s.cols4}>
      <FormRow label="数据源策略">
        <Select
          value={q.sourcePolicy}
          onChange={(e) => onChange({ sourcePolicy: e.target.value })}
          options={SOURCE_POLICY_OPTIONS}
        />
      </FormRow>
      <FormRow label="复权">
        <Select
          value={q.adjust}
          onChange={(e) => onChange({ adjust: e.target.value })}
          options={ADJUST_OPTIONS}
        />
      </FormRow>
      <FormRow label="排序字段">
        <Select
          value={q.sortBy}
          onChange={(e) => onChange({ sortBy: e.target.value })}
          options={SORT_OPTIONS}
        />
      </FormRow>
      <FormRow label="返回条数">
        <Input value={q.limit} onChange={(e) => onChange({ limit: e.target.value })} mono />
      </FormRow>
    </div>
  );
}

/** 把底层报错转成用户能看懂的提示，尤其是「数据源不可用」这一选股最常见的失败。 */
function friendlyError(e: unknown): string {
  const msg = e instanceof Error ? e.message : String(e);
  if (/503|数据源|不可用|no data|data source|未连接/i.test(msg)) {
    return `数据源不可用（未连接券商或本地数据缺失）：请先到「券商连接」页连接客户端，或在客户端启动后重试。原始信息：${msg}`;
  }
  return msg;
}

/**
 * 结果列（**依赖实时行情**，故不能是模块级常量）。
 *
 * ★ 为什么要叠实时行情：选股接口返回的 `close` / `change_pct` 是**扫描那一刻**的快照，
 *   之后不会更新 —— 用户选出来一批票后盯着看，价格却一动不动，会以为界面是假的。
 *   有实时行情时优先显示实时价，无行情（无券商连接 / 未订阅到）时如实回退快照。
 *
 * 列说明：
 * - 成交量：后端 `evaluate_scan` 每行已返回 `volume`，前端此前没展示，选股结果少了一个
 *   最常被用来筛「活跃度」的维度；这里补上（用 `fmtVolume` 走万/亿缩写）。
 * - 操作：每行可「加自选 / 移出自选」，与检索、板块雷达一致（补齐「列表增」闭环）。
 */
/** 经典策略明细里「不是指标」的字段（已在其他列展示或无需展示） */
const CLASSIC_DETAIL_SKIP = new Set([
  "code", "name", "close", "change_pct", "score", "strategy", "volume", "amount",
]);

/** 把策略明细 {ma20: 10.2, vol_ratio: 1.9} 渲染成「ma20 10.20 · 量比 1.9」。 */
export function detailText(r: ScreenRow): string {
  const parts: string[] = [];
  // ★ 命中理由必须**排在最前面**：它是「为什么选中它」的唯一解释。
  //   此前这里显式跳过了 reason（`k !== "reason"`），于是「命中理由」这一列
  //   只在展示一堆数值明细 —— 用户看到 ma20 1450.50 · vol_ratio 1.90 却不知道
  //   这只票凭什么入选（是创新高？还是均线多头？）。
  const reason = r.reason;
  if (typeof reason === "string" && reason) parts.push(reason);
  for (const [k, v] of Object.entries(r)) {
    if (CLASSIC_DETAIL_SKIP.has(k) || k === "reason" || v === null || v === undefined) continue;
    if (typeof v === "number") {
      if (!Number.isFinite(v)) continue;
      parts.push(`${k} ${Math.abs(v) >= 1000 ? v.toFixed(0) : v.toFixed(2)}`);
    } else if (typeof v === "boolean") {
      if (v) parts.push(k);
    } else if (typeof v === "string" && v) {
      parts.push(`${k} ${v}`);
    }
  }
  return parts.slice(0, 6).join(" · ") || "--";
}

export function resultCols(
  quotes: Record<string, Quote | undefined>,
  toggle: (code: string) => void,
  watchCodes: string[],
  /** 经典策略模式：结果行带策略明细，追加一列解释「为什么选中它」 */
  classicMode = false,
): Column<ScreenRow>[] {
  const last = (r: ScreenRow): number | undefined => {
    const q = quotes[r.code]?.price;
    // ★ 选股结果的回退值就是**那根 K 线的收盘价**，它本身没错 —— 错的是表头。
    //   写「最新」会让用户以为这是此刻的价，而选股本来就是按收盘价算的，
    //   所以表头改成「最新/收盘」，单元格再标一句来源。
    return isLivePrice(q) ? q : r.close;
  };
  const pct = (r: ScreenRow): number | undefined => quotes[r.code]?.change_pct ?? r.change_pct;
  const inWatch = (r: ScreenRow) => watchCodes.includes(r.code);
  return [
    { key: "code", header: "代码", width: 100, mono: true, render: (r) => r.code },
    {
      key: "name",
      header: "名称",
      width: 110,
      // ★ 空串必须显式渲染成占位符（2026-09-20 实测修复）。
      //   后端在**无名称数据**时返回的是 `name: ""`，而空串既不是 null 也不是
      //   undefined ⇒ 原来的 `r.name ?? "--"` 不生效，单元格渲染成**一片空白**：
      //   用户无法分辨「这只票没有名称数据」和「界面坏了」。
      //   项目铁律：缺失一律显示 `—`，绝不静默空白。
      //   （同一根因还有功能性后果：名称全空时 `exclude_st` 预过滤恒不命中，
      //   已在后端 universe.py 用本地名称表修好。）
      render: (r) => (r.name && String(r.name).trim() ? r.name : "—"),
    },
    {
      key: "close",
      header: "最新/收盘",
      width: 84,
      align: "right",
      mono: true,
      render: (r) => (
        <span
          style={{ color: toneColor(pct(r)) }}
          title={isLivePrice(quotes[r.code]?.price) ? undefined : "收盘价（选股就是按这根 K 线的收盘价算的，非实时）"}
        >
          {fmtPrice(last(r))}
        </span>
      ),
    },
    {
      key: "pct",
      header: "涨跌幅",
      width: 84,
      align: "right",
      mono: true,
      render: (r) => <span style={{ color: toneColor(pct(r)) }}>{fmtPct(pct(r))}</span>,
    },
    {
      key: "volume",
      header: "成交量",
      width: 92,
      align: "right",
      mono: true,
      render: (r) =>
        fmtVolume(typeof r.volume === "number" ? r.volume : undefined),
    },
    // 经典策略没有「命中分」概念（命中即命中，只有明细），换成明细列避免整列 "--"
    ...(classicMode
      ? [
          {
            key: "detail",
            header: "命中明细",
            width: 240,
            render: (r: ScreenRow) => (
              <span title={JSON.stringify(r)} style={{ fontSize: "var(--font-xs)" }}>
                {detailText(r)}
              </span>
            ),
          } as Column<ScreenRow>,
        ]
      : [
          {
            key: "score",
            header: "命中分",
            width: 80,
            align: "right",
            mono: true,
            render: (r: ScreenRow) => (r.score === undefined ? "--" : String(r.score)),
          } as Column<ScreenRow>,
        ]),
    {
      key: "act",
      header: "操作",
      width: 96,
      render: (r) => (
        <div style={{ display: "flex", gap: 4 }}>
          <Button
            size="sm"
            variant="ghost"
            onClick={() => toggle(r.code)}
            title={inWatch(r) ? "从自选股移除" : "加入自选股"}
          >
            {inWatch(r) ? "移出自选" : "加自选"}
          </Button>
        </div>
      ),
    },
  ];
}

/** 命中/扫描/耗时/数据源 统计条 */
/**
 * 选股结果统计条 —— 命中 / 扫描 / 耗时 / 数据源，一行展示。
 *
 * ★ 为什么从卡片网格改成一行条：这四个数是「这次扫描到底靠不靠谱」的判断依据，
 *   要**一眼扫完**（尤其「扫描」—— 扫了 0 只 vs 扫了 5000 只是完全不同两件事）。
 *   卡片网格在窄屏会塌成一列四行，把结论割裂开。
 *
 * ★ 降级标记用**值后面的小徽标**而不是第三行小字：一行条里没有第三行的位置，
 *   而「数据源已降级」属于必须可见的警示（不可见的降级 = 用户以为结果是权威的）。
 */
export function ScreenStats({ res }: { res: ScreenResponse }) {
  return (
    <div className={s.statRow}>
      <div className={s.statRowItem} title="满足条件的标的数">
        <span className={s.statRowLabel}>命中</span>
        <span className={s.statRowValue}>{res.count}</span>
      </div>
      <div
        className={s.statRowItem}
        title={
          res.total_scanned === 0
            ? "扫描 0 只 = 根本没拿到 K 线（不是「没选中票」）"
            : "实际取到 K 线并参与求值的标的数"
        }
      >
        <span className={s.statRowLabel}>扫描</span>
        <span
          className={s.statRowValue}
          style={res.total_scanned === 0 ? { color: "var(--danger)" } : undefined}
        >
          {res.total_scanned}
        </span>
      </div>
      <div className={s.statRowItem}>
        <span className={s.statRowLabel}>耗时</span>
        <span className={s.statRowValue}>{res.elapsed_ms}ms</span>
      </div>
      <div
        className={s.statRowItem}
        title={res.degraded ? `已降级：${res.degraded_reason ?? "未给出原因"}` : "未降级"}
      >
        <span className={s.statRowLabel}>数据源</span>
        <span className={s.statRowValue} style={{ fontSize: "var(--font-sm)" }}>
          {String(res.provenance?.provider_used ?? "--")}
          {res.degraded && <span className={s.statRowTag}>已降级</span>}
        </span>
      </div>
    </div>
  );
}

/** 未连接券商时的善意提示：选股依赖行情/全市场数据，断连会导致扫描失败或回退离线数据。 */
export function ScreenConnectionHint() {
  const connections = useBrokerStore((st) => st.connections);
  const connected = connections.filter((c) => c.connected).length;
  if (connected > 0) return null;
  return (
    <div className={`${s.note} ${s.noteWarn}`} style={{ marginBottom: 8 }}>
      未连接券商：选股依赖行情/全市场数据。若本地数据可用仍可扫描，否则会返回「数据源不可用」。
      请到「券商连接」页连接客户端后再试。
    </div>
  );
}

/**
 * 结果区（统计条 + 可选工具条 + 结果表 + 溯源脚注）。
 *
 * 三种「空」必须分开显示，否则用户会把「数据源不可用」当成「没有符合条件的票」：
 * ① 还没执行（idleText）② 执行成功但零命中（emptyText）③ 执行中（Spinner）。
 * 数据源不可用时后端返回 503，页面 banner 原样展示错误，落到这里的只会是 ②。
 */
export function ScreenResult({
  res,
  busy,
  idleText,
  emptyText,
  toolbar,
  footer,
  classicMode = false,
}: {
  res: ScreenResponse | null;
  busy: boolean;
  idleText: string;
  emptyText: string;
  /** 结果表上方的操作区（如「存为动态板块」） */
  toolbar?: ReactNode;
  /** 表格下方的溯源脚注 */
  footer?: ReactNode;
  /** 经典策略模式：把「命中分」列换成「命中明细」列 */
  classicMode?: boolean;
}) {
  const codes = useMemo(() => (res?.results ?? []).map((r) => r.code), [res]);
  const quotes = useLiveQuotes(codes);
  const toggle = useWatchlistStore((st) => st.toggle);
  const watchCodes = useWatchlistStore((st) => st.codes);
  const cols = useMemo(
    () => resultCols(quotes, toggle, watchCodes, classicMode),
    [quotes, toggle, watchCodes, classicMode],
  );
  const pctOf = (r: ScreenRow): number | undefined => quotes[r.code]?.change_pct ?? r.change_pct;

  return (
    <>
      <ScreenConnectionHint />
      {res && <ScreenStats res={res} />}
      {res && toolbar}

      <Panel flush className={s.grow} title={`选股结果（${res?.count ?? 0}）`}>
        {/* ★ 整列名称都缺失时说明成因（2026-09-20）。此前只是「名称」列一片空白，
            用户无从判断是数据没取到还是界面坏了。放在 tableArea 之外，
            避免影响表格的 flex 尺寸计算。 */}
        {res && res.results.length > 0
          && res.results.every((r) => !r.name || !String(r.name).trim()) && (
          <div className={s.note} style={{ margin: "0 0 6px" }}>
            本次结果未取到股票名称（「名称」列显示为 —）。名称来自本地名称表或券商行情源；
            可在「数据源」页确认名称表是否已同步，或连接券商后重跑。代码与行情数据不受影响。
          </div>
        )}
        <div className={s.tableArea}>
          {busy && !res ? (
            <Spinner label="扫描中…" />
          ) : !res ? (
            <EmptyState text={idleText} />
          ) : res.results.length === 0 ? (
            <EmptyState text={emptyText} />
          ) : (
            <DataTable
              columns={cols}
              rows={res.results}
              rowKey={(r) => r.code}
              rowHeight={24}
              rowTone={(r) => {
                const v = pctOf(r);
                return v && v > 0 ? 1 : v && v < 0 ? -1 : 0;
              }}
            />
          )}
        </div>
      </Panel>

      {res && footer}
    </>
  );
}

/* ------------------------------------------------------------------ */
/* 条件选股                                                            */
/* ------------------------------------------------------------------ */

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
      {
        indicator: {
          name: "rsi",
          params: { win: 14 },
          output: "rsi",
          window: -1,
          op: "lt",
          value: 30,
        },
      },
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

/** 条件选股面板：条件树 JSON 编辑 + 自然语言解析 + 存为动态板块 */
export function ConditionsPanel() {
  const [conditions, setConditions] = useState(PRESET_EXAMPLES[0]?.conditions ?? "{}");
  const [universe, setUniverse] = useState("");
  const [q, setQ] = useState<ScreenQuery>(DEFAULT_QUERY);
  const [nlText, setNlText] = useState("");

  const [res, setRes] = useState<ScreenResponse | null>(null);
  const [busy, setBusy] = useState(false);
  const [banner, setBanner] = useState<{ tone: "ok" | "error" | "warn"; text: string } | null>(null);
  const [boardName, setBoardName] = useState("");

  const patch = (p: Partial<ScreenQuery>) => setQ((prev) => ({ ...prev, ...p }));

  const run = async () => {
    let parsed: unknown;
    try {
      parsed = JSON.parse(conditions);
    } catch (e) {
      setBanner({
        tone: "error",
        text: `conditions 不是合法 JSON：${e instanceof Error ? e.message : e}`,
      });
      return;
    }
    setBusy(true);
    setBanner(null);
    try {
      const out = await screenApi.run({
        conditions: JSON.stringify(parsed),
        limit: Number(q.limit) || 100,
        sort_by: q.sortBy,
        sort_desc: 1,
        adjust: q.adjust,
        source_policy: q.sourcePolicy,
        universe: universe || undefined,
      });
      setRes(out);
      setBanner({
        tone: "ok",
        text: `扫描 ${out.total_scanned} 只，命中 ${out.count} 只，耗时 ${out.elapsed_ms}ms`,
      });
    } catch (e) {
      setRes(null);
      setBanner({ tone: "error", text: friendlyError(e) });
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
      const name = boardName || `选股_${fmtDateTime(Date.now())}`;
      await screenApi.saveBoard({
        name,
        conditions: JSON.parse(conditions) as Record<string, unknown>,
        results: res.results.map((r) => ({
          code: r.code,
          name: r.name,
          close: r.close,
          change_pct: r.change_pct,
        })),
      });
      setBanner({ tone: "ok", text: `已存为动态板块：${name}` });
      setBoardName("");
    } catch (e) {
      setBanner({ tone: "error", text: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className={s.panelHost}>
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
                options={[
                  { value: "", label: "选择预设条件…" },
                  ...PRESET_EXAMPLES.map((p) => ({ value: p.label, label: p.label })),
                ]}
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

            <ScreenOptions q={q} onChange={patch} columns={2} />

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

          <ScreenResult
            res={res}
            busy={busy}
            idleText="尚未执行选股"
            emptyText="无符合条件的标的（已成功扫描，非数据源不可用）"
            toolbar={
              <div className={s.toolbar}>
                <Input
                  value={boardName}
                  onChange={(e) => setBoardName(e.target.value)}
                  style={{ width: 200 }}
                  placeholder="动态板块名称"
                />
                <Button
                  size="sm"
                  variant="ghost"
                  disabled={busy || res?.count === 0}
                  onClick={() => void saveBoard()}
                >
                  存为动态板块
                </Button>
                <span className={s.spacer} />
                {res && <Badge tone="info">排序 {res.sort_by}</Badge>}
              </div>
            }
            footer={
              <div className={s.note}>
                <b>数据溯源</b>：{JSON.stringify(res?.provenance)}
              </div>
            }
          />
        </div>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* 经典策略选股                                                        */
/* ------------------------------------------------------------------ */

/**
 * 经典策略面板（复刻 Sequoia-X 的形态/动量策略集）。
 *
 * ★ 为什么与条件选股并列而不是塞进条件树：这些策略是**多日形态识别 + 横截面排名**
 * （20 日新高要滚动极值、RPS 要全池收益排名），条件树只能做「单股单值比较」，
 * 表达不了。后端为此单开 ``app/screener/classic.py``，这里只是它的界面。
 *
 * ★ 参数可调为什么重要：默认参数（如海龟 20 日）只是通用值，不同市况下用户
 * 想调成 30 日/55 日。若写死，界面就只能「信或不信」，没法按自己口径用。
 *
 * ★ 命中明细：每个策略返回它算出的中间量（MA20、量比、区间涨幅…），
 * 界面直接展示 —— 选股最怕黑箱，用户必须能判断「这票为什么被选出来」。
 */
export function ClassicPanel() {
  const [items, setItems] = useState<ClassicStrategy[]>([]);
  const [sid, setSid] = useState("");
  const [params, setParams] = useState<Record<string, string>>({});
  const [universe, setUniverse] = useState("");
  const [q, setQ] = useState<ScreenQuery>({ ...DEFAULT_QUERY, sortBy: "change_pct" });

  const [res, setRes] = useState<ScreenResponse | null>(null);
  const [batch, setBatch] = useState<Record<string, ScreenRow[]> | null>(null);
  const [busy, setBusy] = useState(false);
  const [banner, setBanner] = useState<{ tone: "ok" | "error" | "warn"; text: string } | null>(null);

  useEffect(() => {
    let alive = true;
    void screenApi
      .strategies()
      .then((out) => {
        if (!alive) return;
        const list = out.items ?? [];
        setItems(list);
        const first = list[0];
        if (first) setSid((prev) => prev || first.id);
      })
      .catch((e: unknown) => {
        if (alive) setBanner({ tone: "error", text: `策略清单加载失败：${String(e)}` });
      });
    return () => {
      alive = false;
    };
  }, []);

  const meta = items.find((x) => x.id === sid);
  // 切换策略 → 参数表单回到该策略默认值（否则上一個策略的参数会串味）
  useEffect(() => {
    if (!meta) return;
    setParams(
      Object.fromEntries(Object.entries(meta.params).map(([k, v]) => [k, String(v)])),
    );
  }, [sid, meta?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  const patch = (p: Partial<ScreenQuery>) => setQ((prev) => ({ ...prev, ...p }));

  const buildParams = (): string | undefined => {
    if (!meta) return undefined;
    const out: Record<string, number> = {};
    for (const [k, raw] of Object.entries(params)) {
      const def = meta.params[k];
      const num = Number(raw);
      if (raw === "" || !Number.isFinite(num)) continue;   // 空即沿用默认
      if (def !== undefined && num === def) continue;       // 与默认相同不必传
      out[k] = num;
    }
    return Object.keys(out).length ? JSON.stringify(out) : undefined;
  };

  const run = async () => {
    if (!sid) return;
    setBusy(true);
    setBanner(null);
    setBatch(null);
    try {
      const out = await screenApi.run({
        classic: sid,
        classic_params: buildParams(),
        limit: Number(q.limit) || 100,
        adjust: q.adjust,
        source_policy: q.sourcePolicy,
        universe: universe || undefined,
      });
      setRes(out);
      setBanner({
        tone: out.count ? "ok" : "warn",
        text: `${meta?.label ?? sid}：扫描 ${out.total_scanned} 只，命中 ${out.count} 只，耗时 ${out.elapsed_ms}ms`
          + (out.count ? "" : "（本次无标的满足条件，非数据源故障）"),
      });
    } catch (e) {
      setRes(null);
      setBanner({ tone: "error", text: friendlyError(e) });
    } finally {
      setBusy(false);
    }
  };

  /** 一次跑全部策略（与定时任务 system.classic_screen 同一内核），点标签查看明细 */
  const runAll = async () => {
    if (!items.length) return;
    setBusy(true);
    setBanner(null);
    setRes(null);
    try {
      const out = await screenApi.classic({
        strategies: items.map((x) => x.id),
        limit: Number(q.limit) || 100,
        universe: universe || undefined,
        source_policy: q.sourcePolicy,
        adjust: q.adjust,
      });
      setBatch(out.results ?? {});
      setBanner({
        tone: out.total_hits ? "ok" : "warn",
        text: `批量扫描 ${out.scanned} 只，${out.strategies.length} 个策略共命中 ${out.total_hits} 只 —— 点上方标签查看单个策略明细`,
      });
    } catch (e) {
      setBatch(null);
      setBanner({ tone: "error", text: friendlyError(e) });
    } finally {
      setBusy(false);
    }
  };

  const pickBatch = (id: string) => {
    if (!batch) return;
    setRes({
      count: (batch[id] ?? []).length,
      total_scanned: Object.values(batch).reduce((a, b) => a + b.length, 0),
      elapsed_ms: 0,
      sort_by: "change_pct",
      conditions: {},
      classic: id,
      results: batch[id] ?? [],
      provenance: {},
      degraded: false,
    });
  };

  return (
    <div className={s.panelHost}>
      <div className={s.split}>
        <Panel title="经典策略">
          <div className={s.form}>
            <FormRow label="策略">
              <Select
                value={sid}
                onChange={(e) => setSid(e.target.value)}
                options={
                  items.length
                    ? items.map((x) => ({ value: x.id, label: `${x.label}（${x.id}）` }))
                    : [{ value: "", label: "加载中…" }]
                }
              />
            </FormRow>

            {meta && <div className={s.note}>{meta.desc}</div>}

            {meta && Object.keys(meta.params).length > 0 && (
              <FormRow label="策略参数">
                <div className={s.cols2}>
                  {Object.entries(meta.params).map(([k, def]) => (
                    <div key={k} style={{ display: "flex", alignItems: "center", gap: 4 }}>
                      <span style={{ fontSize: "var(--font-xs)", minWidth: 96 }} title={`默认 ${def}`}>
                        {k}
                      </span>
                      <Input
                        value={params[k] ?? String(def)}
                        onChange={(e) => setParams((p) => ({ ...p, [k]: e.target.value }))}
                        mono
                        placeholder={String(def)}
                      />
                    </div>
                  ))}
                </div>
              </FormRow>
            )}

            <FormRow label="股票池">
              <Input
                value={universe}
                onChange={(e) => setUniverse(e.target.value)}
                mono
                placeholder='空=全市场；或 "sector:医药" / "index:000300"'
              />
            </FormRow>

            <ScreenOptions q={q} onChange={patch} columns={2} />

            <Button block disabled={busy || !sid} onClick={() => void run()}>
              {busy ? "执行中…" : "执行策略"}
            </Button>
            <Button block variant="ghost" disabled={busy || !items.length} onClick={() => void runAll()}>
              批量跑全部策略
            </Button>

            <div className={s.note}>
              策略口径与「定时任务 · 收盘后经典策略选股」完全一致（同一
              <code> run_classic </code>内核），手动跑出的结果就是每天 16:00 自动跑出的结果。
              参数留空即沿用默认值。
            </div>
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

          {batch && (
            <div className={s.toolbar} style={{ flexWrap: "wrap" }}>
              <span style={{ fontSize: "var(--font-xs)" }}>批量结果：</span>
              {Object.entries(batch).map(([id, rows]) => (
                <Button
                  key={id}
                  size="sm"
                  variant={res?.classic === id ? "primary" : "ghost"}
                  onClick={() => pickBatch(id)}
                >
                  {items.find((x) => x.id === id)?.label ?? id}（{rows.length}）
                </Button>
              ))}
            </div>
          )}

          <ScreenResult
            res={res}
            busy={busy}
            classicMode
            idleText="尚未执行策略"
            emptyText="本次无标的满足该策略条件（已成功扫描，非数据源不可用）"
            toolbar={
              res ? (
                <div className={s.toolbar}>
                  <Badge tone="info">{items.find((x) => x.id === res.classic)?.label ?? res.classic ?? "策略"}</Badge>
                  <span className={s.spacer} />
                  <span style={{ fontSize: "var(--font-xs)" }}>
                    经典策略按自身口径排序（海龟按成交额、其余按涨跌幅），不套用排序字段
                  </span>
                </div>
              ) : undefined
            }
            footer={
              <div className={s.note}>
                <b>数据溯源</b>：{JSON.stringify(res?.provenance)}
              </div>
            }
          />
        </div>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* 公式选股                                                            */
/* ------------------------------------------------------------------ */

const EXAMPLES = [
  "C > MA(20)",
  "C > MA(20) AND RSI(14) < 30",
  "VOL > VOL_MA(5) * 1.5",
  "MACD > 0 AND KDJ < 30",
];

/** 公式选股面板：类终端公式 DSL */
export function FormulaPanel() {
  const [expr, setExpr] = useState("C > MA(20) AND RSI(14) < 30");
  const [q, setQ] = useState<ScreenQuery>(DEFAULT_QUERY);

  const [res, setRes] = useState<ScreenResponse | null>(null);
  const [busy, setBusy] = useState(false);
  const [banner, setBanner] = useState<{ tone: "ok" | "error"; text: string } | null>(null);

  const patch = (p: Partial<ScreenQuery>) => setQ((prev) => ({ ...prev, ...p }));

  const run = async () => {
    setBusy(true);
    setBanner(null);
    try {
      const out = await screenApi.expr({
        expr,
        limit: Number(q.limit) || 100,
        sort_by: q.sortBy,
        sort_desc: 1,
        adjust: q.adjust,
        source_policy: q.sourcePolicy,
      });
      setRes(out);
      setBanner({
        tone: "ok",
        text: `公式解析成功，扫描 ${out.total_scanned} 只，命中 ${out.count} 只，耗时 ${out.elapsed_ms}ms`,
      });
    } catch (e) {
      setRes(null);
      setBanner({ tone: "error", text: friendlyError(e) });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className={s.panelHost}>
      <Panel title="公式输入">
        <div className={s.form}>
          <FormRow label="公式">
            <Input
              value={expr}
              onChange={(e) => setExpr(e.target.value)}
              mono
              placeholder="C > MA(20)"
            />
          </FormRow>

          <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
            {EXAMPLES.map((x) => (
              <Button key={x} size="sm" variant="ghost" onClick={() => setExpr(x)}>
                {x}
              </Button>
            ))}
          </div>

          <ScreenOptions q={q} onChange={patch} columns={4} />

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
        <div className={`${s.note} ${banner.tone === "ok" ? s.noteOk : s.noteError}`}>
          {banner.text}
        </div>
      )}

      <ScreenResult
        res={res}
        busy={busy}
        idleText="尚未执行公式"
        emptyText="无符合条件的标的"
        footer={
          <div className={s.note}>
            <b>解析结果</b>：{JSON.stringify(res?.conditions)}
          </div>
        }
      />
    </div>
  );
}

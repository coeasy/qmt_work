import { useState } from "react";
import { Badge, EmptyState, Spinner } from "@/design/primitives";
import { marketApi, fmtBarDate } from "@/services/api";
import { useAsync } from "@/hooks/useAsync";
import { fmtAmount, fmtPct, fmtPrice, toneColor } from "@/shared/format";
import s from "./panels.module.css";

/**
 * 基本面 / 深度画像面板（工作台底部坞的「基本面」页签）。
 *
 * ★ 契约要点（`app/services/market/analysis.py::build_analysis`）：
 *   - `GET /market/analysis?code=` 单请求并发聚合 6 维：
 *     snapshot / profile / capital / performance / moneyflow / valuation
 *   - **每维独立超时容错（8s）**，单维失败只把 `availability[维度]` 置
 *     `unavailable`，不拖垮整体 —— 所以「某一维空白」是正常降级，不是页面坏了
 *   - `availability` 的取值是 `ok` / `stale` / `unavailable`，**必须逐维显示**：
 *     不显示的话，用户看到「资金流」空白只会以为软件坏了，而真实原因可能是
 *     「当前数据源不含该维度」
 *   - `valuation` 依赖**券商财务数据**；`pe`/`pb` 由现价现算，EPS≤0 时 PE 为 null
 *     （不伪造负市盈率）。2026-09-21 起券商财务缺位时**再用行情派生指标兜底**
 *     （`metrics_source` 标明来源），此前的「估值 无数据」是假空缺
 *   - `performance.as_of` 是数据真正截至日（K 线末根），陈旧时标 `stale`
 *
 * 渲染原则：**空就是空**。缺失项显示「—」并说明原因，绝不用 0 填充。
 *
 * ★★ 布局（2026-09-21 改）：6 个维度**收成一排紧凑卡片**，**默认全部收起**，
 *    点击某一维才展开该维明细（再点收起）。
 *
 *    为什么改：此前 6 个 Section 用 `repeat(auto-fit, minmax(230px, 1fr))` 平铺，
 *    在底部坞里会铺成 2~3 行、每行都是一整块表格 —— 而底部坞的高度是**从图表那里
 *    借来的**（`dock` 的 `max-height: 42%`），铺得越满，K 线能用的高度就越少。
 *    实际使用时最常看的是「这一维有没有数据、大概什么水平」，明细只在需要时看，
 *    所以一排卡片 + 点击展开既省高度，又让 6 维的**可用性一眼可见**。
 *
 *    卡片摘要**不是装饰**：收起状态下它必须能替代大部分展开动作，否则用户会被迫
 *    逐个点开确认（那还不如一直展开）。因此每维都给出该维最关键的 1~2 个数值，
 *    不可用时给出**短原因**（完整原因在展开后）。
 */
export interface FundamentalsPanelProps {
  code: string;
}

type Avail = "ok" | "stale" | "unavailable";
type DimKey = "snapshot" | "valuation" | "performance" | "capital" | "profile" | "moneyflow";

const AVAIL_LABEL: Record<string, string> = {
  ok: "正常",
  stale: "数据滞后",
  unavailable: "无数据",
};

/** 6 个维度的展示顺序：按看盘时的关心程度排（先看价格，再看贵不贵、最近走势…）。 */
const DIMENSIONS: ReadonlyArray<{ key: DimKey; title: string }> = [
  { key: "snapshot", title: "行情快照" },
  { key: "valuation", title: "估值" },
  { key: "performance", title: "近期表现" },
  { key: "capital", title: "股本 / 市值" },
  { key: "profile", title: "标的档案" },
  { key: "moneyflow", title: "资金流" },
];

function availTone(a: Avail | undefined): "success" | "warning" | "neutral" {
  if (a === "ok") return "success";
  if (a === "stale") return "warning";
  return "neutral";
}

/** 某维度不可用时的可操作说明（不写「获取失败」这种无信息量的文案）。 */
const UNAVAIL_HINT: Record<string, string> = {
  capital: "当前数据源不含股本明细（需 eltdx 行情源或券商股本接口）",
  moneyflow: "当前数据源不含资金流（券商渠道通常不提供，需在线资金流源）",
  valuation: "估值需券商财务接口（EPS/BPS）；无券商连接或券商终端无财务数据时为空",
  performance: "本地日线不足或券商未同步该标的",
  snapshot: "无实时行情（未订阅到 / 行情源不可用）",
  profile: "无标的档案（名称表与券商接口均未命中）",
};

/**
 * 收起态卡片上的**短原因**。
 *
 * 与 `UNAVAIL_HINT` 的分工：这里是「一眼看得懂」，展开后才是「怎么解决」。
 * 两个都写全会在 118px 宽的卡片里被省略号截掉，等于没写。
 */
const UNAVAIL_SHORT: Record<string, string> = {
  capital: "数据源不含股本",
  moneyflow: "数据源不含资金流",
  valuation: "券商无财务数据",
  performance: "本地日线不足",
  snapshot: "无实时行情",
  profile: "无标的档案",
};

/** 估值来源的可读名（`metrics_source` 是后端给的源标识，不加工就不好读）。 */
const METRICS_SOURCE_LABEL: Record<string, string> = {
  broker: "券商财务",
  tencent: "腾讯行情",
};

function sourceLabel(src: unknown): string {
  const key = String(src ?? "").trim();
  if (!key) return "";
  return METRICS_SOURCE_LABEL[key] ?? `${key} 行情`;
}

/** 数值或「—」；用于把 null / undefined 统一成占位符（绝不显示 0 冒充）。 */
function dash(v: unknown, text: string): string {
  return v === null || v === undefined ? "—" : text;
}

/**
 * 收起态卡片摘要 —— 每维最关键的 1~2 个值。
 *
 * ⚠️ 不可用时**优先说原因**，而不是显示一排「—」：一排破折号不携带任何信息，
 * 用户只能逐个点开看提示，等于把「默认收起」变成纯粹的负担。
 */
function summarize(key: DimKey, d: Record<string, any>, av: Record<string, Avail>): string {
  if ((av[key] ?? "unavailable") === "unavailable") {
    return UNAVAIL_SHORT[key] ?? "无数据";
  }
  const snap = d.snapshot as Record<string, any> | null;
  const val = d.valuation as Record<string, any> | null;
  const perf = d.performance as Record<string, any> | null;
  const cap = d.capital as Record<string, any> | null;
  const prof = d.profile as Record<string, any> | null;
  switch (key) {
    case "snapshot":
      return snap
        ? `最新 ${fmtPrice(snap.last)} · ${fmtPct(snap.change_pct)}`
        : "—";
    case "valuation": {
      if (!val) return "—";
      const pe = dash(val.pe, `PE ${fmtPrice(val.pe)}`);
      const pb = dash(val.pb, `PB ${fmtPrice(val.pb)}`);
      return `${pe} · ${pb}`;
    }
    case "performance": {
      if (!perf) return "—";
      const parts = [`5日 ${fmtPct(perf.chg_5d)}`, `20日 ${fmtPct(perf.chg_20d)}`];
      if (perf.stale) parts.push("（滞后）");
      return parts.join(" · ");
    }
    case "capital":
      return cap ? `总市值 ${fmtAmount(cap.total_mktcap)}` : "—";
    case "profile":
      return prof ? [prof.name || "—", prof.board || ""].filter(Boolean).join(" · ") : "—";
    case "moneyflow": {
      const mf = d.moneyflow as Record<string, unknown> | null;
      if (!mf) return "—";
      const first = Object.entries(mf).find(([, v]) => v !== null && v !== undefined);
      return first ? `${first[0]} ${String(first[1])}` : "—";
    }
    default:
      return "—";
  }
}

export function FundamentalsPanel({ code }: FundamentalsPanelProps) {
  const res = useAsync(() => marketApi.analysis(code), [code]);
  const d = res.data as Record<string, any> | null;
  /**
   * 当前展开的维度；`null` = 全部收起（**默认态**）。
   * 一次只展开一维：底部坞高度是从图表借的，同时展开多块会把 K 线压没。
   */
  const [open, setOpen] = useState<DimKey | null>(null);

  if (res.loading && !d) return <Spinner label="聚合 6 维数据…" />;
  if (res.error) return <div className={s.panelNote}>{res.error}</div>;
  if (!d) return <EmptyState text="无画像数据" />;

  const av: Record<string, Avail> = (d.availability ?? {}) as Record<string, Avail>;
  const snap = d.snapshot as Record<string, any> | null;
  const prof = d.profile as Record<string, any> | null;
  const cap = d.capital as Record<string, any> | null;
  const perf = d.performance as Record<string, any> | null;
  const val = d.valuation as Record<string, any> | null;

  const okCount = Object.values(av).filter((x) => x === "ok" || x === "stale").length;
  const total = Object.keys(av).length || 6;
  const valSource = sourceLabel(val?.metrics_source);

  /** 某一维的明细单元格（仅在展开时渲染）。 */
  const cellsFor = (key: DimKey) => {
    switch (key) {
      case "snapshot":
        return snap ? (
          <>
            <Cell k="最新" v={fmtPrice(snap.last)} mono />
            <Cell k="昨收" v={fmtPrice(snap.pre_close)} mono />
            <Cell k="涨跌" v={fmtPrice(snap.change)} tone={snap.change_pct} mono />
            <Cell k="涨跌幅" v={fmtPct(snap.change_pct)} tone={snap.change_pct} mono />
            <Cell k="今开" v={fmtPrice(snap.open)} mono />
            <Cell k="最高" v={fmtPrice(snap.high)} mono />
            <Cell k="最低" v={fmtPrice(snap.low)} mono />
            <Cell k="成交量" v={fmtAmount(snap.volume)} mono />
            <Cell k="成交额" v={fmtAmount(snap.amount)} mono />
          </>
        ) : null;
      case "valuation":
        return val ? (
          <>
            <Cell k="市盈率 PE" v={fmtPrice(val.pe)} mono />
            <Cell k="市净率 PB" v={fmtPrice(val.pb)} mono />
            <Cell k="每股收益" v={fmtPrice(val.eps)} mono />
            <Cell k="每股净资产" v={fmtPrice(val.bps)} mono />
            <Cell k="净资产收益率" v={val.roe === null ? "—" : `${val.roe}%`} mono />
            <Cell k="报告期" v={val.report_time || "—"} mono />
          </>
        ) : null;
      case "performance":
        return perf ? (
          <>
            <Cell k="5 日" v={fmtPct(perf.chg_5d)} tone={perf.chg_5d} mono />
            <Cell k="20 日" v={fmtPct(perf.chg_20d)} tone={perf.chg_20d} mono />
            <Cell k="60 日" v={fmtPct(perf.chg_60d)} tone={perf.chg_60d} mono />
            <Cell k="52 周高" v={fmtPrice(perf.high_52w)} mono />
            <Cell k="52 周低" v={fmtPrice(perf.low_52w)} mono />
            <Cell
              k="52 周分位"
              v={perf.pct_in_52w === null || perf.pct_in_52w === undefined ? "—" : `${perf.pct_in_52w}%`}
              mono
            />
          </>
        ) : null;
      case "capital":
        return cap ? (
          <>
            <Cell k="总股本" v={fmtAmount(cap.total_shares)} mono />
            <Cell k="流通股本" v={fmtAmount(cap.circulating_shares)} mono />
            <Cell k="换手率" v={cap.turnover_rate === null ? "—" : `${cap.turnover_rate}%`} mono />
            <Cell k="总市值" v={fmtAmount(cap.total_mktcap)} mono />
            <Cell k="流通市值" v={fmtAmount(cap.float_mktcap)} mono />
          </>
        ) : null;
      case "profile":
        return prof ? (
          <>
            <Cell k="名称" v={prof.name || "—"} />
            <Cell k="交易所" v={prof.exchange || "—"} />
            <Cell k="板块" v={prof.board || "—"} />
            <Cell k="涨跌停" v={`${fmtPrice(prof.high_limit)} / ${fmtPrice(prof.low_limit)}`} mono />
            <Cell k="行业" v={prof.industry || "—"} />
            <Cell
              k="概念"
              v={Array.isArray(prof.concepts) && prof.concepts.length ? prof.concepts.join(" · ") : "—"}
            />
          </>
        ) : null;
      case "moneyflow":
        return d.moneyflow
          ? Object.entries(d.moneyflow as Record<string, unknown>)
              .slice(0, 8)
              .map(([k, v]) => (
                <Cell key={k} k={k} v={v === null || v === undefined ? "—" : String(v)} mono />
              ))
          : null;
      default:
        return null;
    }
  };

  const openTitle = DIMENSIONS.find((x) => x.key === open)?.title ?? "";
  const openAvail: Avail = open ? av[open] ?? "unavailable" : "unavailable";
  /**
   * 展开区内容。
   *
   * ⚠️ 必须**按 `availability` 收口**，不能只看数据对象是否存在：
   * 后端在「这一维没拿到」时会给一个**字段齐全但值全 null** 的对象
   * （实测 `valuation: {report_time: null, eps: null, pe: null, ...}`）。
   * 只看 `val` 非空就会渲染出一整张破折号表格 —— 而用户真正需要知道的是
   * **为什么没有**（是没连券商？终端没下财务数据？数据源不含这一维？），
   * 那一行 `—` 不携带任何信息，还占着从 K 线借来的高度。
   */
  const openCells = open && openAvail !== "unavailable" ? cellsFor(open) : null;

  return (
    <>
      <div className={s.panelNote}>
        维度可用 {okCount} / {total}
        {d.ts ? ` · 生成于 ${d.ts}` : ""}
        {perf?.as_of ? ` · 表现数据截至 ${fmtBarDate(String(perf.as_of))}` : ""}
        {open ? ` · 已展开「${openTitle}」` : " · 点击任一维度查看明细"}
      </div>

      {/* ---- 6 维一排：默认全部收起 ---- */}
      <div className={s.dimRow} role="group" aria-label="基本面维度">
        {DIMENSIONS.map(({ key, title }) => {
          const isOpen = open === key;
          return (
            <button
              key={key}
              type="button"
              className={[s.dimChip, isOpen ? s.dimChipOpen : ""].filter(Boolean).join(" ")}
              onClick={() => setOpen(isOpen ? null : key)}
              aria-expanded={isOpen}
              title={isOpen ? `收起「${title}」明细` : `展开「${title}」明细`}
            >
              <span className={s.dimChipHead}>
                <span className={s.dimTitle}>{title}</span>
                <Badge tone={availTone(av[key])}>{AVAIL_LABEL[av[key] ?? "unavailable"]}</Badge>
              </span>
              <span className={s.dimSummary}>{summarize(key, d, av)}</span>
            </button>
          );
        })}
      </div>

      {/* ---- 展开区：一次只显示一维 ---- */}
      {open && (
        <div className={s.dimDetail}>
          {openCells ? (
            <>
              <div className={s.cellGrid}>{openCells}</div>
              {/* 估值来源可追溯：券商的 PE/PB 是现算的，行情源的 PE/PB 是直供的，
                  两者口径不同，用户有权知道自己在看哪一个（2026-09-21 A9c）。 */}
              {open === "valuation" && valSource && (
                <div className={s.dimNote}>
                  估值来源：{valSource}
                  {valSource === "券商财务"
                    ? "（由现价 ÷ 每股指标现算）"
                    : "（行情源直接提供的 PE(TTM) / PB）"}
                </div>
              )}
              {open === "performance" && perf?.as_of && (
                <div className={s.dimNote}>数据截至 {fmtBarDate(String(perf.as_of))}</div>
              )}
            </>
          ) : (
            <div className={s.sectionEmpty}>
              {UNAVAIL_HINT[open] ?? "当前数据源不含该维度"}
            </div>
          )}
        </div>
      )}
    </>
  );
}

function Cell({ k, v, mono, tone }: { k: string; v: string; mono?: boolean; tone?: number | null }) {
  return (
    <div className={s.cell}>
      <span className={s.cellLabel}>{k}</span>
      <span
        className={mono ? s.cellValueMono : s.cellValue}
        style={tone === undefined ? undefined : { color: toneColor(tone) }}
      >
        {v}
      </span>
    </div>
  );
}

export default FundamentalsPanel;

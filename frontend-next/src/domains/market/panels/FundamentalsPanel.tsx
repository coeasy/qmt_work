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
 *     （不伪造负市盈率）
 *   - `performance.as_of` 是数据真正截至日（K 线末根），陈旧时标 `stale`
 *
 * 渲染原则：**空就是空**。缺失项显示「—」并说明原因，绝不用 0 填充。
 */
export interface FundamentalsPanelProps {
  code: string;
}

type Avail = "ok" | "stale" | "unavailable";

const AVAIL_LABEL: Record<string, string> = {
  ok: "正常",
  stale: "数据滞后",
  unavailable: "无数据",
};

function availTone(a: Avail | undefined): "success" | "warning" | "neutral" {
  if (a === "ok") return "success";
  if (a === "stale") return "warning";
  return "neutral";
}

/** 某维度不可用时的可操作说明（不写「获取失败」这种无信息量的文案）。 */
const UNAVAIL_HINT: Record<string, string> = {
  capital: "当前数据源不含股本明细（需 eltdx 行情源或券商股本接口）",
  moneyflow: "当前数据源不含资金流（券商渠道通常不提供，需在线资金流源）",
  valuation: "估值需券商财务接口（EPS/BPS）；无券商连接或券商终端无财务接口时为空",
  performance: "本地日线不足或券商未同步该标的",
  snapshot: "无实时行情（未订阅到 / 行情源不可用）",
  profile: "无标的档案（名称表与券商接口均未命中）",
};

export function FundamentalsPanel({ code }: FundamentalsPanelProps) {
  const res = useAsync(() => marketApi.analysis(code), [code]);
  const d = res.data as Record<string, any> | null;

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

  return (
    <>
      <div className={s.panelNote}>
        维度可用 {okCount} / {total}
        {d.ts ? ` · 生成于 ${d.ts}` : ""}
        {perf?.as_of ? ` · 表现数据截至 ${fmtBarDate(String(perf.as_of))}` : ""}
      </div>

      <div className={s.sectionGrid}>
        <Section title="行情快照" avail={av.snapshot} hint={UNAVAIL_HINT.snapshot}>
          {snap ? (
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
          ) : null}
        </Section>

        <Section title="估值" avail={av.valuation} hint={UNAVAIL_HINT.valuation}>
          {val ? (
            <>
              <Cell k="市盈率 PE" v={fmtPrice(val.pe)} mono />
              <Cell k="市净率 PB" v={fmtPrice(val.pb)} mono />
              <Cell k="每股收益" v={fmtPrice(val.eps)} mono />
              <Cell k="每股净资产" v={fmtPrice(val.bps)} mono />
              <Cell k="净资产收益率" v={val.roe === null ? "—" : `${val.roe}%`} mono />
              <Cell k="报告期" v={val.report_time || "—"} mono />
            </>
          ) : null}
        </Section>

        <Section title="近期表现" avail={av.performance} hint={UNAVAIL_HINT.performance}>
          {perf ? (
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
          ) : null}
        </Section>

        <Section title="股本 / 市值" avail={av.capital} hint={UNAVAIL_HINT.capital}>
          {cap ? (
            <>
              <Cell k="总股本" v={fmtAmount(cap.total_shares)} mono />
              <Cell k="流通股本" v={fmtAmount(cap.circulating_shares)} mono />
              <Cell k="换手率" v={cap.turnover_rate === null ? "—" : `${cap.turnover_rate}%`} mono />
              <Cell k="总市值" v={fmtAmount(cap.total_mktcap)} mono />
              <Cell k="流通市值" v={fmtAmount(cap.float_mktcap)} mono />
            </>
          ) : null}
        </Section>

        <Section title="标的档案" avail={av.profile} hint={UNAVAIL_HINT.profile}>
          {prof ? (
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
          ) : null}
        </Section>

        <Section title="资金流" avail={av.moneyflow} hint={UNAVAIL_HINT.moneyflow}>
          {d.moneyflow ? (
            Object.entries(d.moneyflow as Record<string, unknown>)
              .slice(0, 8)
              .map(([k, v]) => <Cell key={k} k={k} v={v === null || v === undefined ? "—" : String(v)} mono />)
          ) : null}
        </Section>
      </div>
    </>
  );
}

function Section({
  title,
  avail,
  hint,
  children,
}: {
  title: string;
  avail: Avail | undefined;
  hint?: string;
  children: React.ReactNode;
}) {
  const empty = !children;
  return (
    <div className={s.section}>
      <div className={s.sectionHead}>
        <span>{title}</span>
        <Badge tone={availTone(avail)}>{AVAIL_LABEL[avail ?? "unavailable"]}</Badge>
      </div>
      {empty ? (
        <div className={s.sectionEmpty}>{hint ?? "当前数据源不含该维度"}</div>
      ) : (
        <div className={s.cellGrid}>{children}</div>
      )}
    </div>
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

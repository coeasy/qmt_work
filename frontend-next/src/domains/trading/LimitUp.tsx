import { useMemo, useState } from "react";
import {
  Badge,
  Button,
  ConfirmButton,
  ConfirmModal,
  DataTable,
  EmptyState,
  FormRow,
  Input,
  Panel,
  Select,
  Spinner,
  Tabs,
  type Column,
} from "@/design/primitives";
import { limitupApi, marketApi, type LimitUpRow, type LimitUpScanResponse } from "@/services/api";
import { useAsync } from "@/hooks/useAsync";
import { useLiveQuotes } from "@/hooks/useLiveQuotes";
import { fmtAmount, fmtPct, fmtPrice, normalizeCode, toneColor } from "@/shared/format";
import { isLivePrice } from "@/shared/freshness";
import type { LimitUpStatus } from "@/shared/types";
import { useOpenWorkbench } from "@/hooks/useOpenWorkbench";
import s from "../domain.module.css";

/**
 * 涨停监控。
 *
 * ★ 契约要点（limitup.py → engines/limitup.py）：
 *   - 股票池是**单只**增删（POST {code} / DELETE ?code=），不是批量数组
 *   - start 参数：limit_pct / cutoff / min_rise / buy_volume / do_trade / interval
 *   - status 返回 {running, interval, limit_pct, cutoff, min_rise, buy_volume, do_trade,
 *     pool:[{code,name}], total_triggered, events:[...]}
 *   - do_trade=true 时触发会真实下单（默认 false，仅监控），本页对该开关做显式二次确认提示
 *   - 另有 /market/limitup 做「板块内涨停扫描」，与本页的「自选池监控」是两件事
 */

/**
 * 名称占位：空串必须显式渲染成 `—`（2026-09-20 修复）。
 *
 * 后端此前用 `name or code` 兜底 ⇒ 接口返回的 `name` **恒非空**（等于代码），
 * 于是既看不出「名称缺失」，也把代码当名称显示。后端改成「查不到就留空」之后，
 * 空串必须在这里显式占位 —— `?? "--"` 对 `""` **不生效**（本项目反复踩到的坑）。
 *
 * 导出以便单测（与 `ScreenPanels::resultCols`、`Rebalance::orderCols` 同一套路：
 * `DataTable` 在 jsdom 下不渲染行，只能直接测渲染函数）。
 */
export function nameText(name?: string | null): string {
  const v = String(name ?? "").trim();
  return v || "—";
}

export function LimitUp() {
  const [tab, setTab] = useState<"pool" | "scan">("pool");
  // 涨停池 / 扫描结果点一行 ⇒ 直接进行情工作台看这只票（唯一出口，勿各写一遍）
  const openWorkbench = useOpenWorkbench();

  const st = useAsync<LimitUpStatus>(() => limitupApi.status(), []);
  const status = st.data;
  // 监控池里的标的叠加实时行情（status 接口只给 code + name，没有价格）
  const poolCodes = useMemo(() => (status?.pool ?? []).map((p) => p.code), [status?.pool]);
  const poolQuotes = useLiveQuotes(poolCodes);

  const [newCode, setNewCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [banner, setBanner] = useState<{ tone: "ok" | "error" | "warn"; text: string } | null>(null);

  // 启动参数（与后端字段一一对应）
  const [limitPct, setLimitPct] = useState("0.1");
  const [cutoff, setCutoff] = useState("10:00");
  const [minRise, setMinRise] = useState("0.03");
  const [buyVolume, setBuyVolume] = useState("0");
  const [doTrade, setDoTrade] = useState(false);
  /** 真实下单模式的启动确认 */
  const [confirmStart, setConfirmStart] = useState(false);
  /** 重置触发记录的确认（见下方 ★，它不只是清计数） */
  const [confirmReset, setConfirmReset] = useState(false);
  const [interval, setIntervalSec] = useState("2");

  // 扫描参数
  const [sector, setSector] = useState("沪深A股");
  const [minScanPct, setMinScanPct] = useState("9.5");
  const scan = useAsync<LimitUpScanResponse>(
    () => marketApi.limitupScan(sector, Number(minScanPct) || 9.5, true, 200, "change"),
    [sector, minScanPct],
  );
  // 扫描结果同样是**发起扫描那一刻**的快照，之后不会自己更新 ⇒ 叠加实时行情。
  const scanCodes = useMemo(() => (scan.data?.rows ?? []).map((r) => r.code), [scan.data]);
  const scanQuotes = useLiveQuotes(scanCodes);

  const wrap = async (fn: () => Promise<unknown>, okText: string) => {
    setBusy(true);
    setBanner(null);
    try {
      await fn();
      setBanner({ tone: "ok", text: okText });
      await st.reload();
    } catch (e) {
      setBanner({ tone: "error", text: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy(false);
    }
  };

  const addCode = () => {
    const c = normalizeCode(newCode);
    if (!c) return;
    void wrap(() => limitupApi.addPool(c), `已加入监控池：${c}`).then(() => setNewCode(""));
  };

  /** 启动监控的真实执行体（与确认逻辑分开，避免确认后再弹一次） */
  const doStart = () =>
    wrap(
      () =>
        limitupApi.start({
          limit_pct: Number(limitPct) || 0.1,
          cutoff,
          min_rise: Number(minRise) || 0.03,
          buy_volume: Number(buyVolume) || 0,
          do_trade: doTrade,
          interval: Number(interval) || 2,
        }),
      "涨停监控已启动",
    );

  /**
   * ★ 真实下单（do_trade）必须用 ConfirmModal 而不是 window.confirm：
   * 原生弹窗不受主题/皮肤控制、无法说明影响范围，且项目规范把资金动作列为
   * 禁用写法。更关键的是——「触发时会真实下单」这件事必须让用户**明确读一遍**再确认。
   */
  const start = () => {
    if (doTrade) {
      setConfirmStart(true);
      return;
    }
    void doStart();
  };

  const scanCols: Column<LimitUpRow>[] = [
    { key: "code", header: "代码", width: 100, mono: true, render: (r) => r.code },
    { key: "name", header: "名称", width: 100, render: (r) => nameText(r.name) },
    {
      key: "last",
      header: "最新价",
      width: 88,
      align: "right",
      mono: true,
      // 扫描结果是**发起扫描那一刻**的快照；叠加实时行情后数字才会继续走。
      // ★ 没叠加到行情时这里显示的仍是扫描那一刻的价格，却在「最新价」表头下 ——
      //   打板是秒级决策，把几分钟前的价当现价看会误判封板强度，故标出来。
      render: (r) => {
        const q = scanQuotes[r.code]?.price;
        const last = isLivePrice(q) ? q : r.last;
        return isLivePrice(q) ? (
          <span style={{ color: toneColor(r.change_pct) }}>{fmtPrice(last)}</span>
        ) : (
          <span
            className={s.stalePrice}
            style={{ color: toneColor(r.change_pct) }}
            title="非实时：未订阅到该标的的实时行情，这是发起扫描那一刻的快照价"
          >
            {fmtPrice(last)}
          </span>
        );
      },
    },
    {
      key: "pct",
      header: "涨跌幅",
      width: 88,
      align: "right",
      mono: true,
      render: (r) => {
        const pct = scanQuotes[r.code]?.change_pct ?? r.change_pct;
        return <span style={{ color: toneColor(pct) }}>{fmtPct(pct)}</span>;
      },
    },
    { key: "amount", header: "成交额", width: 110, align: "right", mono: true, render: (r) => fmtAmount(r.amount) },
  ];

  return (
    <div className={s.page}>
      <div className={s.toolbar}>
        <Badge tone={status?.running ? "success" : "neutral"}>
          {status?.running ? "监控运行中" : "未运行"}
        </Badge>
        {status && (
          <span className={s.muted}>
            轮询 {status.interval}s · 涨停阈值 {(status.limit_pct * 100).toFixed(1)}% · 时间截止{" "}
            {status.cutoff} · 最小涨幅 {(status.min_rise * 100).toFixed(1)}% ·{" "}
            {status.do_trade ? (
              <b style={{ color: "var(--danger)" }}>触发即下单</b>
            ) : (
              <span>仅监控</span>
            )}
          </span>
        )}
        <span className={s.spacer} />
        <span className={s.muted}>
          累计触发 <b>{status?.total_triggered ?? 0}</b>
        </span>
        <Button size="sm" variant="ghost" onClick={() => void st.reload()}>
          刷新
        </Button>
      </div>

      {banner && (
        <div
          className={`${s.note} ${
            banner.tone === "ok" ? s.noteOk : banner.tone === "warn" ? s.noteWarn : s.noteError
          }`}
        >
          {banner.text}
        </div>
      )}

      <Tabs
        items={[
          { key: "pool", label: `自选池监控（${status?.pool.length ?? 0}）` },
          { key: "scan", label: `板块涨停扫描（${scan.data?.count ?? 0}）` },
        ]}
        value={tab}
        onChange={setTab}
      />

      {tab === "pool" ? (
        <div className={s.split}>
          <Panel title="监控配置">
            <div className={s.form}>
              <FormRow label="涨停阈值">
                <Input value={limitPct} onChange={(e) => setLimitPct(e.target.value)} mono placeholder="0.1 = 10%" />
              </FormRow>
              <FormRow label="截止时间">
                <Input value={cutoff} onChange={(e) => setCutoff(e.target.value)} mono placeholder="10:00" />
              </FormRow>
              <FormRow label="最小涨幅">
                <Input value={minRise} onChange={(e) => setMinRise(e.target.value)} mono placeholder="0.03 = 3%" />
              </FormRow>
              <FormRow label="买入量">
                <Input value={buyVolume} onChange={(e) => setBuyVolume(e.target.value)} mono placeholder="0" />
              </FormRow>
              <FormRow label="轮询间隔(秒)">
                <Input value={interval} onChange={(e) => setIntervalSec(e.target.value)} mono />
              </FormRow>
              <FormRow label="触发即下单">
                <Select
                  value={doTrade ? "1" : "0"}
                  onChange={(e) => setDoTrade(e.target.value === "1")}
                  options={[
                    { value: "0", label: "否（仅监控与通知）" },
                    { value: "1", label: "是（真实下单，谨慎）" },
                  ]}
                />
              </FormRow>

              {doTrade && (
                <div className={`${s.note} ${s.noteWarn}`}>
                  ⚠ do_trade=true：命中条件时会真实下单，且买入量取自「买入量」字段。
                  建议先在仅监控模式验证阈值。
                </div>
              )}

              <div className={s.actions}>
                <Button size="sm" disabled={busy} onClick={start}>
                  启动
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  disabled={busy}
                  onClick={() => void wrap(() => limitupApi.stop(), "已停止监控")}
                >
                  停止
                </Button>
                {/*
                  ★ 这里必须有确认，而且确认框要说清后果，不能只写「确定要重置吗」。

                  后端 `engines/limitup.py::reset_triggered` 清的是 `_triggered` ——
                  它是「同一只票当天只触发一次」的**去重集合**（`_maybe_trigger` 里
                  `if code in self._triggered: return`）。把它清空，池中已经触发过的
                  标的就会**再次触发**；若 do_trade=true，那就是**重复真实下单**。

                  也就是说这个按钮不是「把计数器归零」这种无害操作，而是会解除
                  一层**防重复下单的保险**。误点一次 = 多一批委托。
                */}
                <Button
                  size="sm"
                  variant="danger"
                  disabled={busy}
                  onClick={() => setConfirmReset(true)}
                >
                  重置触发
                </Button>
              </div>

              <div className={s.note} style={{ marginTop: 4 }}>
                加入监控池（单只添加）
              </div>
              <div className={s.inline}>
                <Input
                  value={newCode}
                  onChange={(e) => setNewCode(e.target.value)}
                  mono
                  placeholder="600519 或 600519.SH"
                />
                <Button size="sm" disabled={busy || !newCode} onClick={addCode}>
                  添加
                </Button>
              </div>
            </div>
          </Panel>

          <div className={s.grow} style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            <Panel title={`监控池（${status?.pool.length ?? 0}）`}>
              {st.loading && !status ? (
                <Spinner label="加载中…" />
              ) : st.error ? (
                <div className={`${s.note} ${s.noteError}`}>{st.error}</div>
              ) : (status?.pool.length ?? 0) === 0 ? (
                <EmptyState text="监控池为空，请在上方添加标的" />
              ) : (
                <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
                  {status?.pool.map((p) => (
                    <span
                      key={p.code}
                      style={{
                        display: "inline-flex",
                        alignItems: "center",
                        gap: 6,
                        padding: "2px 8px",
                        border: "1px solid var(--border)",
                        borderRadius: "var(--radius)",
                        background: "var(--bg-2)",
                        fontSize: "var(--font-sm)",
                      }}
                    >
                      <span className={s.mono}>{p.code}</span>
                      <span className={s.muted}>{nameText(p.name)}</span>
                      {/* 叠加实时行情：池里这些票本来只有代码和名字，看不出当前价位 */}
                      <span
                        className={s.mono}
                        style={{ color: toneColor(poolQuotes[p.code]?.change_pct) }}
                      >
                        {fmtPrice(poolQuotes[p.code]?.price)}
                      </span>
                      {/* 修复前是一个「点一下就直接删」的裸 ×，且紧贴代码文字 */}
                      <ConfirmButton
                        title={`移除 ${p.code}`}
                        confirmText="确认"
                        disabled={busy}
                        onConfirm={() =>
                          void wrap(() => limitupApi.removePool(p.code), `已移除 ${p.code}`)
                        }
                      >
                        ×
                      </ConfirmButton>
                    </span>
                  ))}
                </div>
              )}
            </Panel>

            <Panel flush title={`事件流（最近 ${status?.events.length ?? 0} 条）`} className={s.grow}>
              <div className={s.logList}>
                {(status?.events.length ?? 0) === 0 ? (
                  <div className={s.muted} style={{ padding: 8 }}>
                    暂无事件
                  </div>
                ) : (
                  status?.events
                    .slice()
                    .reverse()
                    .map((e, i) => (
                      <div className={s.logRow} key={i}>
                        <span style={{ flex: "0 0 auto" }}>{String(e.time ?? e.ts ?? "")}</span>
                        <span style={{ flex: "0 0 auto" }} className={s.mono}>
                          {String(e.code ?? "")}
                        </span>
                        <span>{String(e.message ?? e.msg ?? JSON.stringify(e))}</span>
                      </div>
                    ))
                )}
              </div>
            </Panel>
          </div>
        </div>
      ) : (
        <Panel
          flush
          className={s.grow}
          title="板块涨停扫描"
          extra={
            <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
              <Input
                value={sector}
                onChange={(e) => setSector(e.target.value)}
                style={{ width: 130 }}
                placeholder="板块"
              />
              <Input
                value={minScanPct}
                onChange={(e) => setMinScanPct(e.target.value)}
                mono
                style={{ width: 70 }}
                placeholder="阈值"
              />
              <Button size="sm" variant="ghost" onClick={() => void scan.reload()}>
                扫描
              </Button>
            </div>
          }
        >
          <div className={s.tableArea}>
            {scan.loading && !scan.data ? (
              <Spinner label="扫描中…" />
            ) : scan.error ? (
              <div className={`${s.note} ${s.noteError}`} style={{ margin: 8 }}>
                {scan.error}
              </div>
            ) : (scan.data?.rows.length ?? 0) === 0 ? (
              <EmptyState text="无符合条件的涨停标的" />
            ) : (
              <DataTable
                columns={scanCols}
                rows={scan.data?.rows ?? []}
                rowKey={(r) => r.code}
                rowHeight={24}
                onRowClick={(r) => openWorkbench(r.code, r.name ?? "")}
              />
            )}
          </div>
        </Panel>
      )}

      <ConfirmModal
        open={confirmStart}
        title="启动涨停监控（真实下单）"
        danger
        confirmText="确认启动"
        message={
          <>
            当前已勾选 <b>do_trade</b>：监控触发时会<b>用真实资金下单</b>
            （单笔买入量 {buyVolume || 0} 股）。
            <br />
            若只想观察信号、不想实际成交，请先取消勾选。
          </>
        }
        warn="触发即真实下单，资金风险自负"
        onConfirm={() => {
          setConfirmStart(false);
          void doStart();
        }}
        onCancel={() => setConfirmStart(false)}
      />

      <ConfirmModal
        open={confirmReset}
        title="重置涨停触发记录"
        danger
        confirmText="确认重置"
        message={
          <>
            将清空「今日已触发」记录（当前 {status?.total_triggered ?? 0} 条），
            顶部「累计触发」归零；下方事件流不受影响，仍可回看。
            <br />
            这份记录是<b>同一只票当天只触发一次</b>的去重依据 —— 清空后，池中已经触发过的标的
            <b>会再次触发</b>。
            <br />
            {status?.do_trade ? (
              <>
                当前是<b>触发即下单</b>模式，再次触发<b>会对这些标的重复下单</b>。
              </>
            ) : (
              <>当前是仅监控模式，重复触发只会重复推送通知，不会下单。</>
            )}
          </>
        }
        warn="解除防重复触发的保险，清空后不可恢复"
        onConfirm={() => {
          setConfirmReset(false);
          void wrap(() => limitupApi.reset(), "已重置触发记录");
        }}
        onCancel={() => setConfirmReset(false)}
      />
    </div>
  );
}

export default LimitUp;

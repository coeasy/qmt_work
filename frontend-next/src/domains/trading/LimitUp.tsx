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
  Tabs,
  type Column,
} from "@/design/primitives";
import { limitupApi, marketApi, type LimitUpRow, type LimitUpScanResponse } from "@/services/api";
import { useAsync } from "@/hooks/useAsync";
import { fmtAmount, fmtPct, fmtPrice, normalizeCode, toneColor } from "@/shared/format";
import type { LimitUpStatus } from "@/shared/types";
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
export function LimitUp() {
  const [tab, setTab] = useState<"pool" | "scan">("pool");

  const st = useAsync<LimitUpStatus>(() => limitupApi.status(), []);
  const status = st.data;

  const [newCode, setNewCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [banner, setBanner] = useState<{ tone: "ok" | "error" | "warn"; text: string } | null>(null);

  // 启动参数（与后端字段一一对应）
  const [limitPct, setLimitPct] = useState("0.1");
  const [cutoff, setCutoff] = useState("10:00");
  const [minRise, setMinRise] = useState("0.03");
  const [buyVolume, setBuyVolume] = useState("0");
  const [doTrade, setDoTrade] = useState(false);
  const [interval, setIntervalSec] = useState("2");

  // 扫描参数
  const [sector, setSector] = useState("沪深A股");
  const [minScanPct, setMinScanPct] = useState("9.5");
  const scan = useAsync<LimitUpScanResponse>(
    () => marketApi.limitupScan(sector, Number(minScanPct) || 9.5, true, 200, "change"),
    [sector, minScanPct],
  );

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

  const start = () => {
    if (doTrade && !window.confirm("do_trade=true 会在触发时真实下单，确认启动？")) return;
    void wrap(
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
  };

  const scanCols: Column<LimitUpRow>[] = [
    { key: "code", header: "代码", width: 100, mono: true, render: (r) => r.code },
    { key: "name", header: "名称", width: 100, render: (r) => r.name ?? "--" },
    {
      key: "last",
      header: "最新价",
      width: 88,
      align: "right",
      mono: true,
      render: (r) => (
        <span style={{ color: toneColor(r.change_pct) }}>{fmtPrice(r.last)}</span>
      ),
    },
    {
      key: "pct",
      header: "涨跌幅",
      width: 88,
      align: "right",
      mono: true,
      render: (r) => (
        <span style={{ color: toneColor(r.change_pct) }}>{fmtPct(r.change_pct)}</span>
      ),
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
                  ⚠ do_trade=true：命中条件时会**真实下单**，且买入量取自「买入量」字段。
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
                <Button
                  size="sm"
                  variant="ghost"
                  disabled={busy}
                  onClick={() => void wrap(() => limitupApi.reset(), "已重置触发记录")}
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
                      <span className={s.muted}>{p.name}</span>
                      <button
                        type="button"
                        aria-label={`移除 ${p.code}`}
                        disabled={busy}
                        style={{
                          border: "none",
                          background: "transparent",
                          color: "var(--danger)",
                          cursor: "pointer",
                          padding: 0,
                          lineHeight: 1,
                        }}
                        onClick={() => void wrap(() => limitupApi.removePool(p.code), `已移除 ${p.code}`)}
                      >
                        ×
                      </button>
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
              <DataTable columns={scanCols} rows={scan.data?.rows ?? []} rowKey={(r) => r.code} rowHeight={24} />
            )}
          </div>
        </Panel>
      )}
    </div>
  );
}

export default LimitUp;

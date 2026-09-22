import { useMemo, useState } from "react";
import {
  Badge,
  Button,
  ConfirmButton,
  DataTable,
  EmptyState,
  FormRow,
  Input,
  Panel,
  Select,
  Spinner,
  type Column,
} from "@/design/primitives";
import { algoApi, type AlgoSubmitPayload } from "@/services/api";
import { useAsync } from "@/hooks/useAsync";
import { useQuotesStore } from "@/stores/quotes";
import { useQuoteSubscription } from "@/hooks/useQuoteSubscription";
import { fmtPrice, normalizeCode } from "@/shared/format";
import type { AlgoKind, AlgoOrder, PriceType, Side } from "@/shared/types";
import s from "../domain.module.css";

const ALGO_LABEL: Record<AlgoKind, string> = {
  twap: "TWAP 时间加权",
  vwap: "VWAP 成交量加权",
  iceberg: "冰山单",
  pov: "POV 参与率",
};

const STATUS_TONE: Record<AlgoOrder["status"], "success" | "danger" | "neutral" | "info" | "warning"> = {
  running: "info",
  pending: "warning",
  paused: "warning",
  done: "success",
  canceled: "neutral",
  failed: "danger",
};

const STATUS_LABEL: Record<AlgoOrder["status"], string> = {
  running: "执行中",
  pending: "待启动",
  paused: "已暂停",
  done: "已完成",
  canceled: "已撤销",
  failed: "失败",
};

/**
 * 算法交易（TWAP / VWAP / 冰山 / POV）。
 *
 * ★ 契约要点（algo.py:algo_submit → engines/algo.py:submit）：
 *   - body 字段是 direction（不是 side）、algo（不是 algo_type）
 *   - volume 须为 100 的整数倍；duration 下限 10s；slices 夹取 1–50
 *   - visible_pct（冰山可见比例）1–100；participation_rate（POV）0.01–1.0
 *   - 列表返回的作业字段是 volume / done / slices_done（不是 total_volume / filled_volume）
 *   - 引擎未初始化时返回 503
 */
export function Algo() {
  const [code, setCode] = useState("000001.SZ");
  const [side, setSide] = useState<Side>("buy");
  const [algo, setAlgo] = useState<AlgoKind>("twap");
  const [volume, setVolume] = useState("1000");
  const [duration, setDuration] = useState("300");
  const [slices, setSlices] = useState("5");
  const [priceType, setPriceType] = useState<PriceType>("market");
  const [limitPrice, setLimitPrice] = useState("");
  const [visiblePct, setVisiblePct] = useState("10");
  const [participation, setParticipation] = useState("0.1");
  const [remark, setRemark] = useState("");

  const [busy, setBusy] = useState(false);
  const [banner, setBanner] = useState<{ tone: "ok" | "error"; text: string } | null>(null);

  const normCode = useMemo(() => normalizeCode(code), [code]);
  useQuoteSubscription(useMemo(() => (normCode ? [normCode] : []), [normCode]));
  const quote = useQuotesStore((st) => st.quotes[normCode]);

  const jobs = useAsync<AlgoOrder[]>(() => algoApi.list(), []);

  const submit = async () => {
    setBusy(true);
    setBanner(null);
    try {
      const payload: AlgoSubmitPayload = {
        code: normCode,
        direction: side,
        volume: Number(volume) || 0,
        algo,
        duration: Number(duration) || 300,
        slices: Number(slices) || 5,
        price_type: priceType,
        limit_price: Number(limitPrice) || 0,
        remark,
      };
      if (algo === "iceberg") payload.visible_pct = Number(visiblePct) || 10;
      if (algo === "pov") payload.participation_rate = Number(participation) || 0.1;

      const res = await algoApi.submit(payload);
      setBanner({
        tone: "ok",
        text: `算法单已提交：${res.algo_id}（${ALGO_LABEL[res.algo] ?? res.algo}，目标 ${res.volume} 股）`,
      });
      await jobs.reload();
    } catch (e) {
      setBanner({ tone: "error", text: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy(false);
    }
  };

  const act = async (fn: () => Promise<AlgoOrder>, label: string) => {
    setBusy(true);
    setBanner(null);
    try {
      const res = await fn();
      setBanner({ tone: "ok", text: `${label}成功：${res.algo_id}` });
      await jobs.reload();
    } catch (e) {
      setBanner({ tone: "error", text: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy(false);
    }
  };

  const cols: Column<AlgoOrder>[] = [
    { key: "id", header: "算法单号", width: 110, mono: true, render: (r) => r.algo_id },
    { key: "code", header: "代码", width: 96, mono: true, render: (r) => r.code },
    { key: "algo", header: "算法", width: 116, render: (r) => ALGO_LABEL[r.algo] ?? r.algo },
    {
      key: "dir",
      header: "方向",
      width: 54,
      render: (r) => (
        <span style={{ color: r.direction === "buy" ? "var(--up)" : "var(--down)" }}>
          {r.direction === "buy" ? "买入" : "卖出"}
        </span>
      ),
    },
    { key: "vol", header: "目标量", width: 80, align: "right", mono: true, render: (r) => String(r.volume) },
    { key: "done", header: "已执行", width: 80, align: "right", mono: true, render: (r) => String(r.done) },
    {
      key: "prog",
      header: "进度",
      width: 88,
      align: "right",
      mono: true,
      render: (r) => {
        const pct = r.volume > 0 ? Math.min(100, (r.done / r.volume) * 100) : 0;
        return `${pct.toFixed(0)}%（${r.slices_done}/${r.slices}）`;
      },
    },
    {
      key: "status",
      header: "状态",
      width: 78,
      render: (r) => <Badge tone={STATUS_TONE[r.status] ?? "neutral"}>{STATUS_LABEL[r.status] ?? r.status}</Badge>,
    },
    { key: "created", header: "创建", width: 78, mono: true, render: (r) => r.created },
    {
      key: "err",
      header: "错误",
      width: 140,
      render: (r) => (r.error ? <span style={{ color: "var(--danger)" }}>{r.error}</span> : null),
    },
    {
      key: "act",
      header: "操作",
      width: 132,
      render: (r) => (
        <div style={{ display: "flex", gap: 4 }}>
          {r.status === "running" && (
            <Button size="sm" variant="ghost" onClick={() => void act(() => algoApi.pause(r.algo_id), "暂停")}>
              暂停
            </Button>
          )}
          {r.status === "paused" && (
            <Button size="sm" variant="ghost" onClick={() => void act(() => algoApi.resume(r.algo_id), "恢复")}>
              恢复
            </Button>
          )}
          {(r.status === "running" || r.status === "paused" || r.status === "pending") && (
            // 撤销是**不可逆**的资金动作（未成交部分直接作废、已成交量不会回滚），
            // 与其他「暂停/恢复」并列放在行内，点一下就没了太容易误触 ⇒ 两段式确认。
            <ConfirmButton
              confirmText="确认撤销"
              title={`撤销算法单 ${r.algo_id}`}
              onConfirm={() => void act(() => algoApi.cancel(r.algo_id), "撤销")}
            >
              撤销
            </ConfirmButton>
          )}
        </div>
      ),
    },
  ];

  return (
    <div className={s.page}>
      <div className={s.split}>
        <Panel title="新建算法单">
          <div className={s.form}>
            <FormRow label="代码">
              <Input value={code} onChange={(e) => setCode(e.target.value)} mono placeholder="600519 或 600519.SH" />
            </FormRow>

            {quote && (
              <div className={s.note}>
                {quote.name || normCode} · 最新 <span className={s.mono}>{fmtPrice(quote.price)}</span>
              </div>
            )}

            <FormRow label="算法">
              <Select
                value={algo}
                onChange={(e) => setAlgo(e.target.value as AlgoKind)}
                options={(Object.keys(ALGO_LABEL) as AlgoKind[]).map((k) => ({
                  value: k,
                  label: ALGO_LABEL[k],
                }))}
              />
            </FormRow>

            <FormRow label="方向">
              <div className={s.actions}>
                <Button size="sm" variant={side === "buy" ? "buy" : "default"} onClick={() => setSide("buy")}>
                  买入
                </Button>
                <Button size="sm" variant={side === "sell" ? "sell" : "default"} onClick={() => setSide("sell")}>
                  卖出
                </Button>
              </div>
            </FormRow>

            <FormRow label="总量">
              <Input value={volume} onChange={(e) => setVolume(e.target.value)} mono placeholder="100 的整数倍" />
            </FormRow>

            <FormRow label="总时长(秒)">
              <Input value={duration} onChange={(e) => setDuration(e.target.value)} mono />
            </FormRow>

            <FormRow label="切片数">
              <Input value={slices} onChange={(e) => setSlices(e.target.value)} mono placeholder="1–50" />
            </FormRow>

            <FormRow label="委托类型">
              <Select
                value={priceType}
                onChange={(e) => setPriceType(e.target.value as PriceType)}
                options={[
                  { value: "market", label: "市价" },
                  { value: "limit", label: "限价" },
                ]}
              />
            </FormRow>

            {priceType === "limit" && (
              <FormRow label="限价">
                <Input value={limitPrice} onChange={(e) => setLimitPrice(e.target.value)} mono />
              </FormRow>
            )}

            {algo === "iceberg" && (
              <FormRow label="可见比例%">
                <Input value={visiblePct} onChange={(e) => setVisiblePct(e.target.value)} mono placeholder="1–100" />
              </FormRow>
            )}

            {algo === "pov" && (
              <FormRow label="参与率">
                <Input
                  value={participation}
                  onChange={(e) => setParticipation(e.target.value)}
                  mono
                  placeholder="0.01–1.0"
                />
              </FormRow>
            )}

            <FormRow label="备注">
              <Input value={remark} onChange={(e) => setRemark(e.target.value)} />
            </FormRow>

            <Button variant={side === "buy" ? "buy" : "sell"} block disabled={busy} onClick={() => void submit()}>
              {busy ? "提交中…" : "提交算法单"}
            </Button>

            {banner && (
              <div className={`${s.note} ${banner.tone === "ok" ? s.noteOk : s.noteError}`}>{banner.text}</div>
            )}

            <div className={s.note}>
              提示：算法单由引擎在事件循环内按切片推进，每片都经统一风控链路。
              进程重启后 WAL 重放会按「已发量」扣减，不会重复超额下单。
            </div>
          </div>
        </Panel>

        <Panel
          flush
          title={`算法单列表（${jobs.data?.length ?? 0}）`}
          extra={
            <Button size="sm" variant="ghost" onClick={() => void jobs.reload()}>
              刷新
            </Button>
          }
        >
          <div className={s.tableArea}>
            {jobs.loading && !jobs.data ? (
              <Spinner label="加载中…" />
            ) : jobs.error ? (
              <div className={`${s.note} ${s.noteError}`} style={{ margin: 8 }}>
                {jobs.error}
              </div>
            ) : (jobs.data?.length ?? 0) === 0 ? (
              <EmptyState text="暂无算法单 —— 在上方「新建算法单」选择算法与标的后提交" />
            ) : (
              <DataTable columns={cols} rows={jobs.data ?? []} rowKey={(r) => r.algo_id} rowHeight={24} />
            )}
          </div>
        </Panel>
      </div>
    </div>
  );
}

export default Algo;

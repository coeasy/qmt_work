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
  type Column,
} from "@/design/primitives";
import { portfolioApi } from "@/services/api";
import { useAsync } from "@/hooks/useAsync";
import { useLiveQuotes } from "@/hooks/useLiveQuotes";
import { fmtPrice, toneColor } from "@/shared/format";
import s from "./targetPortfolio.module.css";

/**
 * 目标持仓页（方案 §5 流程 / 阶段 3 验收项）。
 *
 * 覆盖两条此前无前端入口的能力：
 *   - POST /target-portfolio/plans/batch-delete（计划批量删除）
 *   - POST /target-portfolio/sync（差量同步，已注册但此前仅 placeholder）
 *
 * 注意后端约束（target_portfolio.py）：
 *   - plans 主键是 id（int），batch-delete 的 body 键是 ids
 *   - sync 的 targets 为 {code: 权重} 字典，mode ∈ shares/amount/ratio
 *   - dry_run=true 仅测算差额，不动账
 */
interface PlanRow {
  id: number;
  name: string;
  weights?: Record<string, number> | unknown;
  status?: string;
  created_at?: string;
  updated_at?: string;
}

export function TargetPortfolio() {
  const plans = useAsync<PlanRow[]>(
    () => portfolioApi.plans().then((r) => (r as PlanRow[]) ?? []),
    [],
  );

  // 跨计划收集一次全部标的，用来叠加实时行情（权重列要显示每只票的最新价）
  const planCodes = useMemo(() => {
    const set = new Set<string>();
    for (const p of plans.data ?? []) {
      if (typeof p.weights === "object" && p.weights) {
        for (const k of Object.keys(p.weights as Record<string, unknown>)) set.add(k);
      }
    }
    return [...set];
  }, [plans.data]);
  const quotes = useLiveQuotes(planCodes);

  const [selected, setSelected] = useState<number[]>([]);
  const [busy, setBusy] = useState(false);
  const [banner, setBanner] = useState<{ tone: "ok" | "error"; text: string } | null>(null);
  /** 批量删除是破坏性动作 ⇒ 模态确认，不用 window.confirm */
  const [confirmBatch, setConfirmBatch] = useState(false);

  /* ---------- 新建 / 编辑 / 单条删除 ---------- */
  /**
   * 此前这一页**只有列表和批量删除**：既不能新建、也不能改、更不能单删一条 ——
   * 于是唯一能改计划的方式是「删了重建」，而重建会把 status 打回 draft，
   * 让「正在按这个计划调仓」的事实悄悄消失。
   */
  const [editingId, setEditingId] = useState<number | null>(null);
  const [planName, setPlanName] = useState("");
  const [planWeights, setPlanWeights] = useState('{\n  "600519.SH": 0.3\n}');

  const resetPlanForm = () => {
    setEditingId(null);
    setPlanName("");
    setPlanWeights('{\n  "600519.SH": 0.3\n}');
  };

  const startEdit = (r: PlanRow) => {
    setEditingId(r.id);
    setPlanName(r.name ?? "");
    const w = typeof r.weights === "object" && r.weights ? r.weights : {};
    setPlanWeights(JSON.stringify(w, null, 2));
    setBanner(null);
  };

  const savePlan = async () => {
    let weights: Record<string, number>;
    try {
      weights = JSON.parse(planWeights) as Record<string, number>;
    } catch {
      setBanner({ tone: "error", text: "权重不是合法 JSON" });
      return;
    }
    setBusy(true);
    setBanner(null);
    try {
      const res = await portfolioApi.createPlan({
        ...(editingId ? { id: editingId } : {}),
        name: planName.trim() || "未命名计划",
        weights,
      });
      // ★ 更新不存在的 id 时后端返回 404；成功返回 id
      setBanner({ tone: "ok", text: editingId ? `已保存计划 #${res.id}` : `已创建计划 #${res.id}` });
      resetPlanForm();
      await plans.reload();
    } catch (e) {
      setBanner({ tone: "error", text: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy(false);
    }
  };

  const removeOne = async (pid: number) => {
    setBusy(true);
    setBanner(null);
    try {
      await portfolioApi.deletePlan(pid);
      setSelected((prev) => prev.filter((x) => x !== pid));
      if (editingId === pid) resetPlanForm();
      setBanner({ tone: "ok", text: `已删除计划 #${pid}` });
      await plans.reload();
    } catch (e) {
      setBanner({ tone: "error", text: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy(false);
    }
  };

  /* ---------- 计划批量删除 ---------- */
  const batchRemove = async () => {
    if (selected.length === 0) return;
    setBusy(true);
    setBanner(null);
    try {
      const res = await portfolioApi.batchRemovePlans(selected);
      setBanner({ tone: "ok", text: `已批量删除 ${res.deleted} 条计划` });
      setSelected([]);
      await plans.reload();
    } catch (e) {
      setBanner({ tone: "error", text: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy(false);
    }
  };

  const planCols: Column<PlanRow>[] = [
    {
      key: "sel",
      header: "",
      width: 40,
      render: (r) => (
        <input
          type="checkbox"
          checked={selected.includes(r.id)}
          onChange={(e) =>
            setSelected((prev) =>
              e.target.checked ? [...prev, r.id] : prev.filter((x) => x !== r.id),
            )
          }
          aria-label={`选择计划 ${r.name}`}
        />
      ),
    },
    { key: "id", header: "ID", width: 56, mono: true, render: (r) => String(r.id) },
    { key: "name", header: "名称", width: 160, render: (r) => r.name || "--" },
    {
      key: "weights",
      header: "权重 / 最新价",
      render: (r) => {
        const w =
          typeof r.weights === "object" && r.weights
            ? (r.weights as Record<string, unknown>)
            : null;
        const keys = w ? Object.keys(w) : [];
        if (!w || keys.length === 0) return <span className={s.mono}>--</span>;
        return (
          <span className={s.weightList}>
            {keys.map((code) => {
              const q = quotes[code];
              const raw = w[code];
              const pct = typeof raw === "number" ? raw * 100 : undefined;
              return (
                <span key={code} className={s.weightChip}>
                  <span className={s.mono}>{code}</span>
                  <span className={s.weightPct}>
                    {pct === undefined ? "--" : `${pct.toFixed(1)}%`}
                  </span>
                  <span className={s.mono} style={{ color: toneColor(q?.change_pct) }}>
                    {fmtPrice(q?.price)}
                  </span>
                </span>
              );
            })}
          </span>
        );
      },
    },
    {
      key: "status",
      header: "状态",
      width: 80,
      render: (r) => (
        <Badge tone={r.status === "draft" ? "neutral" : "info"}>{r.status ?? "--"}</Badge>
      ),
    },
    { key: "created", header: "创建", width: 150, mono: true, render: (r) => r.created_at ?? "--" },
    {
      key: "act",
      header: "操作",
      width: 150,
      render: (r) => (
        <div className={s.actions}>
          <Button size="sm" variant="ghost" disabled={busy} onClick={() => startEdit(r)}>
            编辑
          </Button>
          <ConfirmButton disabled={busy} onConfirm={() => void removeOne(r.id)}>
            删除
          </ConfirmButton>
        </div>
      ),
    },
  ];

  /* ---------- 差量同步 ---------- */
  const [mode, setMode] = useState<"shares" | "amount" | "ratio">("ratio");
  const [targetsText, setTargetsText] = useState('{\n  "600519.SH": 0.3,\n  "000001.SZ": 0.2\n}');
  const [dryRun, setDryRun] = useState(true);
  const [syncBusy, setSyncBusy] = useState(false);
  const [syncResult, setSyncResult] = useState<unknown>(null);

  const onSync = async () => {
    let targets: Record<string, number>;
    try {
      targets = JSON.parse(targetsText);
    } catch {
      setBanner({ tone: "error", text: "targets 不是合法 JSON" });
      return;
    }
    setSyncBusy(true);
    setBanner(null);
    try {
      const res = await portfolioApi.syncTarget({ mode, targets, dry_run: dryRun });
      setSyncResult(res);
      setBanner({
        tone: "ok",
        text: dryRun ? "已测算同步差额（dry_run，未下单）" : "已提交目标持仓同步",
      });
    } catch (e) {
      setBanner({ tone: "error", text: e instanceof Error ? e.message : String(e) });
    } finally {
      setSyncBusy(false);
    }
  };

  return (
    <div className={s.wrap}>
      <Panel
        title={`目标持仓计划（${plans.data?.length ?? 0}）`}
        extra={
          <Button
            size="sm"
            variant="danger"
            disabled={busy || selected.length === 0}
            onClick={() => setConfirmBatch(true)}
          >
            批量删除（{selected.length}）
          </Button>
        }
      >
        <div className={s.tableArea}>
          {plans.loading && <Spinner label="加载中…" />}
          {!plans.loading && plans.error && <div className={s.err}>{plans.error}</div>}
          {!plans.loading && !plans.error && (plans.data?.length ?? 0) === 0 && (
            // 新建面板在列表**下方**，指错方向会让人白找一圈
            <EmptyState text="暂无目标持仓计划 —— 在下方「新建目标持仓计划」添加标的与目标比例" />
          )}
          {!plans.loading && !plans.error && (plans.data?.length ?? 0) > 0 && (
            <DataTable columns={planCols} rows={plans.data ?? []} rowKey={(r) => String(r.id)} />
          )}
        </div>
      </Panel>

      <Panel
        title={editingId ? `编辑计划 #${editingId}` : "新建目标持仓计划"}
        extra={
          editingId ? (
            <Button size="sm" variant="ghost" onClick={resetPlanForm}>
              取消编辑
            </Button>
          ) : null
        }
      >
        <div className={s.form}>
          <FormRow label="名称">
            <Input
              value={planName}
              onChange={(e) => setPlanName(e.target.value)}
              placeholder="未命名计划"
            />
          </FormRow>
          <FormRow label="目标权重（JSON）">
            <textarea
              className={s.json}
              value={planWeights}
              onChange={(e) => setPlanWeights(e.target.value)}
              rows={6}
              spellCheck={false}
            />
          </FormRow>
          <div className={s.actions}>
            <Button size="sm" variant="primary" disabled={busy} onClick={() => void savePlan()}>
              {editingId ? "保存修改" : "创建计划"}
            </Button>
          </div>
          <div className={s.warnBar}>
            保存只改名称与权重，不会改动计划状态（draft / active）——
            否则一次改名就会让「正在按这个计划调仓」的事实悄悄消失。
          </div>
        </div>
      </Panel>

      <Panel title="差量同步">
        <div className={s.form}>
          <div className={s.warnBar}>
            把账户持仓调整到目标权重。开启「仅测算」时后端不动账，只返回差额。
          </div>
          <FormRow label="模式">
            <Select
              value={mode}
              onChange={(e) => setMode(e.target.value as "shares" | "amount" | "ratio")}
              options={[
                { value: "shares", label: "股数" },
                { value: "amount", label: "金额" },
                { value: "ratio", label: "比例" },
              ]}
            />
          </FormRow>
          <FormRow label="目标权重">
            <textarea
              className={s.json}
              value={targetsText}
              onChange={(e) => setTargetsText(e.target.value)}
              rows={6}
              spellCheck={false}
            />
          </FormRow>
          <label className={s.checkRow}>
            <input type="checkbox" checked={dryRun} onChange={(e) => setDryRun(e.target.checked)} />
            仅测算（dry_run，不下单）
          </label>
          <div className={s.actions}>
            <Button variant="primary" onClick={() => void onSync()} disabled={syncBusy} block>
              {syncBusy ? "同步中…" : "执行同步"}
            </Button>
          </div>
          {syncResult !== null && (
            <pre className={s.result}>{JSON.stringify(syncResult, null, 2)}</pre>
          )}
        </div>
      </Panel>

      {banner && (
        <div className={banner.tone === "ok" ? s.okBanner : s.errBanner} role="status">
          {banner.text}
        </div>
      )}

      <ConfirmModal
        open={confirmBatch}
        danger
        title="批量删除目标持仓计划"
        confirmText={`确认删除 ${selected.length} 条`}
        message={
          <>
            将删除 <b>{selected.length}</b> 条目标持仓计划，<b>不可恢复</b>。
            已同步出去的持仓不受影响（本操作只删计划，不平仓）。
          </>
        }
        onCancel={() => setConfirmBatch(false)}
        onConfirm={() => {
          setConfirmBatch(false);
          void batchRemove();
        }}
      />
    </div>
  );
}

export default TargetPortfolio;

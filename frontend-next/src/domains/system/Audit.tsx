import { useState } from "react";
import { Badge, Button, DataTable, EmptyState, Input, Panel, Spinner, type Column } from "@/design/primitives";
import { systemApi } from "@/services/api";
import { useAsync } from "@/hooks/useAsync";
import type { AuditRow, AuditVerifyResult } from "@/shared/types";
import s from "../domain.module.css";

/**
 * 审计日志与 hash 链校验。
 *
 * ★ 契约要点（audit.py）：
 *   - /audit 支持 action 与 limit（limit 后端夹取 1–500）
 *   - 返回行含 D4 防篡改字段 prev_hash / hash；params_json 为已脱敏的 JSON 串
 *   - /audit/verify 校验整条 hash 链，返回 {ok, broken_count, broken:[...]}；
 *     断链时后端会同时触发 audit.tampered 通知（本页只需展示结果）
 *
 * 这是「谁在什么时候下了什么单」的唯一可信来源，故 hash 链校验做成显式动作。
 */
export function Audit() {
  const [action, setAction] = useState("");
  const [limit, setLimit] = useState("100");
  const [verify, setVerify] = useState<AuditVerifyResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [banner, setBanner] = useState<{ tone: "ok" | "error" | "warn"; text: string } | null>(null);

  const rows = useAsync<AuditRow[]>(
    () => systemApi.audit({ limit: Number(limit) || 100, action: action || undefined }),
    [action, limit],
  );

  const runVerify = async () => {
    setBusy(true);
    setBanner(null);
    try {
      const res = await systemApi.auditVerify();
      setVerify(res);
      if (res.ok) {
        setBanner({ tone: "ok", text: "审计 hash 链完整，未检出断链" });
      } else {
        setBanner({
          tone: "error",
          text: `审计链校验失败：检出 ${res.broken_count} 处断链，首个异常 id=${
            res.broken[0]?.id ?? "?"
          }`,
        });
      }
    } catch (e) {
      setBanner({ tone: "error", text: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy(false);
    }
  };

  const cols: Column<AuditRow>[] = [
    { key: "id", header: "ID", width: 70, mono: true, render: (r) => String(r.id) },
    { key: "created", header: "时间", width: 150, mono: true, render: (r) => r.created_at },
    { key: "actor", header: "主体", width: 80, mono: true, render: (r) => r.actor },
    { key: "action", header: "动作", width: 190, mono: true, render: (r) => r.action },
    { key: "target", header: "目标", width: 130, mono: true, render: (r) => r.target || "--" },
    {
      key: "result",
      header: "结果",
      width: 70,
      render: (r) => <Badge tone={r.result === "ok" ? "success" : "danger"}>{r.result}</Badge>,
    },
    { key: "ip", header: "IP", width: 110, mono: true, render: (r) => r.ip || "--" },
    {
      key: "params",
      header: "参数（已脱敏）",
      render: (r) => (
        <span className={s.mono} style={{ fontSize: "var(--font-xs)" }} title={r.params_json}>
          {r.params_json ?? "--"}
        </span>
      ),
    },
    {
      key: "hash",
      header: "Hash",
      width: 110,
      mono: true,
      render: (r) => (
        <span title={`prev=${r.prev_hash ?? ""}\nhash=${r.hash ?? ""}`}>
          {r.hash ? `${r.hash.slice(0, 10)}…` : "--"}
        </span>
      ),
    },
  ];

  return (
    <div className={s.page}>
      <div className={s.toolbar}>
        <Input
          value={action}
          onChange={(e) => setAction(e.target.value)}
          mono
          style={{ width: 200 }}
          placeholder="按 action 过滤"
        />
        <Input
          value={limit}
          onChange={(e) => setLimit(e.target.value)}
          mono
          style={{ width: 80 }}
          placeholder="limit"
        />
        <Button size="sm" variant="ghost" onClick={() => void rows.reload()}>
          查询
        </Button>
        <span className={s.spacer} />
        <Button size="sm" variant="primary" disabled={busy} onClick={() => void runVerify()}>
          {busy ? "校验中…" : "校验 hash 链"}
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

      {verify && verify.ok && (
        <div className={s.stats}>
          <div className={s.stat}>
            <span className={s.statLabel}>校验结果</span>
            <span className={s.statValue} style={{ color: "var(--success)" }}>
              完整
            </span>
          </div>
          <div className={s.stat}>
            <span className={s.statLabel}>断链数</span>
            <span className={s.statValue}>{verify.broken_count}</span>
          </div>
        </div>
      )}

      <Panel flush className={s.grow} title={`审计记录（${rows.data?.length ?? 0}）`}>
        <div className={s.tableArea}>
          {rows.loading && !rows.data ? (
            <Spinner label="加载中…" />
          ) : rows.error ? (
            <div className={`${s.note} ${s.noteError}`} style={{ margin: 8 }}>
              {rows.error}
            </div>
          ) : (rows.data?.length ?? 0) === 0 ? (
            <EmptyState text="无审计记录" />
          ) : (
            <DataTable columns={cols} rows={rows.data ?? []} rowKey={(r) => String(r.id)} rowHeight={24} />
          )}
        </div>
      </Panel>
    </div>
  );
}

export default Audit;

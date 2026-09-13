import { useEffect, useState } from "react";
import { Badge, Button, DataTable, EmptyState, Panel, type Column } from "@/design/primitives";
import { systemApi, type CapabilityItem } from "@/services/api";
import { useQuotesStore } from "@/stores/quotes";
import s from "./systemstatus.module.css";

interface CheckRow {
  name: string;
  /** pass / warn / fail（后端口径，不是布尔） */
  status: string;
}

/**
 * 系统状态：健康探针 / 能力自描述 / 行情通道。
 *
 * ★ 契约要点：
 *   - /health 的 checks 是 {name, status: pass|warn|fail}，没有 ok/detail 字段
 *   - /capabilities 返回 {total, agent_visible, items:[...]}，不是裸数组；
 *     条目字段是 category（不是 domain）
 *
 * 能力可达性核验的落点：/capabilities 返回全部端点，
 * 此处展示总量，供与前端页面清单对照（方案 §5 流程贯通验收）。
 */
export function SystemStatus() {
  const [checks, setChecks] = useState<CheckRow[]>([]);
  const [version, setVersion] = useState("");
  const [caps, setCaps] = useState<CapabilityItem[]>([]);
  const [capTotal, setCapTotal] = useState(0);
  const [err, setErr] = useState("");
  const socketState = useQuotesStore((st) => st.socketState);
  const refs = useQuotesStore((st) => st.refs);

  const load = () => {
    void systemApi
      .health()
      .then((h) => {
        setVersion(h.version ?? "");
        setChecks((h.checks ?? []).map((c) => ({ name: c.name, status: c.status })));
        setErr("");
      })
      .catch((e: unknown) => setErr(e instanceof Error ? e.message : String(e)));
    void systemApi
      .capabilities()
      .then((res) => {
        setCaps(res.items ?? []);
        setCapTotal(res.total ?? res.items?.length ?? 0);
      })
      .catch(() => {
        setCaps([]);
        setCapTotal(0);
      });
  };

  useEffect(load, []);

  const cols: Column<CheckRow>[] = [
    { key: "name", header: "检查项", width: 200, render: (r) => r.name },
    {
      key: "status",
      header: "状态",
      width: 80,
      render: (r) => (
        <Badge tone={r.status === "pass" ? "success" : r.status === "warn" ? "warning" : "danger"}>
          {r.status === "pass" ? "正常" : r.status === "warn" ? "告警" : "异常"}
        </Badge>
      ),
    },
  ];

  const byDomain = caps.reduce<Record<string, number>>((acc, c) => {
    const d = c.category || c.path.split("/")[1] || "其他";
    acc[d] = (acc[d] ?? 0) + 1;
    return acc;
  }, {});

  return (
    <div className={s.wrap}>
      <div className={s.top}>
        <Panel title="服务信息">
          <div className={s.infoBody}>
            <div className={s.kv}>
              <span>版本</span>
              <span className={s.mono}>{version || "—"}</span>
            </div>
            <div className={s.kv}>
              <span>行情通道</span>
              <Badge tone={socketState === "open" ? "success" : "warning"}>
                {socketState}
              </Badge>
            </div>
            <div className={s.kv}>
              <span>活跃订阅</span>
              <span className={s.mono}>{Object.keys(refs).length}</span>
            </div>
            <div className={s.kv}>
              <span>REST 能力</span>
              <span className={s.mono}>{capTotal}</span>
            </div>
            <Button size="sm" onClick={load}>
              刷新
            </Button>
          </div>
        </Panel>

        <Panel title="能力分布（按域）">
          <div className={s.domainBody}>
            {Object.keys(byDomain).length === 0 ? (
              <EmptyState text="无法读取能力清单" />
            ) : (
              Object.entries(byDomain)
                .sort((a, b) => b[1] - a[1])
                .map(([d, n]) => (
                  <div key={d} className={s.domainRow}>
                    <span>{d}</span>
                    <span className={s.mono}>{n}</span>
                  </div>
                ))
            )}
          </div>
        </Panel>
      </div>

      <Panel title="健康检查">
        <div className={s.tableArea}>
          {err ? (
            <EmptyState text={err} />
          ) : (
            <DataTable columns={cols} rows={checks} rowKey={(r) => r.name} />
          )}
        </div>
      </Panel>
    </div>
  );
}

export default SystemStatus;

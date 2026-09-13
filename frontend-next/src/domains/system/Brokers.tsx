import { useEffect, useState } from "react";
import {
  Badge,
  Button,
  EmptyState,
  FormRow,
  Input,
  Panel,
  Select,
  Spinner,
} from "@/design/primitives";
import { brokerApi } from "@/services/api";
import { useBrokerStore } from "@/stores/broker";
import s from "./brokers.module.css";

/**
 * 券商连接管理。
 *
 * 注意后端约束：活跃连接全局唯一（manager.py:340-348），
 * 因此「设为活跃」是单选语义，UI 需明确表达。
 */
export function Brokers() {
  const profiles = useBrokerStore((st) => st.profiles);
  const connections = useBrokerStore((st) => st.connections);
  const loading = useBrokerStore((st) => st.loading);
  const error = useBrokerStore((st) => st.error);
  const load = useBrokerStore((st) => st.load);
  const loadProfiles = useBrokerStore((st) => st.loadProfiles);
  const connect = useBrokerStore((st) => st.connect);
  const disconnect = useBrokerStore((st) => st.disconnect);
  const setActive = useBrokerStore((st) => st.setActive);
  const remove = useBrokerStore((st) => st.remove);
  const batchRemove = useBrokerStore((st) => st.batchRemove);
  const health = useBrokerStore((st) => st.health);

  const [brokerId, setBrokerId] = useState("");
  const [clientPath, setClientPath] = useState("");
  const [accountId, setAccountId] = useState("");
  const [accountType, setAccountType] = useState("STOCK");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");

  /** 批量选择（按 conn_id）。活跃连接不可删，故不计入可选集合。 */
  const [selected, setSelected] = useState<string[]>([]);
  const [healthBusy, setHealthBusy] = useState("");
  /** conn_id → 最近一次健康探测结果 */
  const [healthMap, setHealthMap] = useState<Record<string, Record<string, unknown>>>({});

  useEffect(() => {
    void loadProfiles();
    void load();
  }, [loadProfiles, load]);

  const onAdd = async () => {
    if (!brokerId || !clientPath || !accountId) {
      setMsg("券商 / 客户端路径 / 资金账号均为必填");
      return;
    }
    setBusy(true);
    setMsg("");
    try {
      await brokerApi.add({ broker_id: brokerId, client_path: clientPath, account_id: accountId, account_type: accountType });
      setClientPath("");
      setAccountId("");
      await load();
      setMsg("已添加连接，点击「连接」建立会话");
    } catch (e) {
      setMsg(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const onTest = async () => {
    if (!brokerId || !clientPath) {
      setMsg("探测需要券商与客户端路径");
      return;
    }
    setBusy(true);
    setMsg("");
    try {
      const res = await brokerApi.test({ broker_id: brokerId, client_path: clientPath });
      setMsg(
        res.ok
          ? `探测通过（运行时模式：${res.runtime_mode ?? "未知"}）`
          : `探测失败：${res.reason ?? "未知原因"}${
              res.suggestions?.length ? `；建议：${res.suggestions.join(" / ")}` : ""
            }`,
      );
    } catch (e) {
      setMsg(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const active = connections.find((c) => c.active);

  const onBatchRemove = async () => {
    if (selected.length === 0) return;
    if (!window.confirm(`确认批量删除 ${selected.length} 个连接？活跃连接不会被选中，但其余连接将全部移除。`)) {
      return;
    }
    setBusy(true);
    setMsg("");
    try {
      await batchRemove(selected);
      setSelected([]);
      setMsg(`已批量删除 ${selected.length} 个连接`);
    } catch (e) {
      setMsg(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const onHealth = async (connId: string) => {
    setHealthBusy(connId);
    try {
      const res = await health(connId);
      setHealthMap((prev) => ({ ...prev, [connId]: res }));
    } catch (e) {
      setHealthMap((prev) => ({ ...prev, [connId]: { error: e instanceof Error ? e.message : String(e) } }));
    } finally {
      setHealthBusy("");
    }
  };

  /** 从健康探测响应推导展示色调与摘要（后端结构不唯一，做宽松适配）。 */
  const healthTone = (res: Record<string, unknown> | undefined): "success" | "warning" | "danger" | "neutral" => {
    if (!res) return "neutral";
    if (res.error) return "danger";
    const ok = res.healthy === true || res.status === "ok" || res.connected === true;
    const bad = res.healthy === false || res.status === "fail" || res.connected === false;
    if (ok) return "success";
    if (bad) return "danger";
    return "warning";
  };
  const healthLabel = (res: Record<string, unknown> | undefined): string => {
    if (!res) return "未探测";
    if (res.error) return `探测失败：${String(res.error)}`;
    if (typeof res.summary === "string") return res.summary;
    if (typeof res.status === "string") return res.status;
    if (typeof res.connected === "boolean") return res.connected ? "已连接" : "未连接";
    return "已探测";
  };

  const removable = connections.filter((c) => !c.active);
  const allSelected = removable.length > 0 && removable.every((c) => selected.includes(c.conn_id));

  return (
    <div className={s.wrap}>
      <Panel title="新增连接">
        <div className={s.form}>
          <FormRow label="券商">
            <Select
              value={brokerId}
              onChange={(e) => setBrokerId(e.target.value)}
              placeholder="请选择"
              options={profiles.map((p) => ({
                value: p.id,
                label: p.planned ? `${p.name}（未支持 SDK）` : p.name,
                disabled: p.planned === true,
              }))}
            />
          </FormRow>
          <FormRow label="客户端路径">
            <Input
              value={clientPath}
              onChange={(e) => setClientPath(e.target.value)}
              placeholder="如 C:/qmt/userdata_mini"
              mono
            />
          </FormRow>
          <FormRow label="资金账号">
            <Input value={accountId} onChange={(e) => setAccountId(e.target.value)} mono />
          </FormRow>
          <FormRow label="账户类型">
            <Select
              value={accountType}
              onChange={(e) => setAccountType(e.target.value)}
              options={[
                { value: "STOCK", label: "股票" },
                { value: "CREDIT", label: "信用" },
                { value: "OPTION", label: "期权" },
                { value: "FUTURES", label: "期货" },
              ]}
            />
          </FormRow>
          <div className={s.actions}>
            <Button onClick={onTest} disabled={busy}>
              探测环境
            </Button>
            <Button variant="primary" onClick={onAdd} disabled={busy}>
              添加连接
            </Button>
          </div>
          {msg && <div className={s.msg}>{msg}</div>}
        </div>
      </Panel>

      <Panel
        title={`已有连接（${connections.length}）`}
        extra={
          <Button size="sm" variant="ghost" onClick={() => void load()}>
            刷新
          </Button>
        }
      >
        <div className={s.batchBar}>
          <label className={s.checkCell}>
            <input
              type="checkbox"
              checked={allSelected}
              disabled={removable.length === 0}
              onChange={(e) =>
                setSelected(e.target.checked ? removable.map((c) => c.conn_id) : [])
              }
              aria-label="全选可删除连接"
            />
            全选可删
          </label>
          <span className={s.selCount}>已选 {selected.length} 项</span>
          <Button
            size="sm"
            variant="danger"
            disabled={busy || selected.length === 0}
            onClick={() => void onBatchRemove()}
          >
            批量删除
          </Button>
        </div>
        <div className={s.list}>
          {loading && <Spinner label="加载中…" />}
          {!loading && error && <div className={s.err}>{error}</div>}
          {!loading && connections.length === 0 && (
            <EmptyState text="尚未添加任何券商连接" />
          )}
          {connections.map((c) => (
            <div key={c.conn_id} className={s.item}>
              <label className={s.checkCell}>
                <input
                  type="checkbox"
                  checked={selected.includes(c.conn_id)}
                  disabled={c.active === true}
                  onChange={(e) =>
                    setSelected((prev) =>
                      e.target.checked
                        ? [...prev, c.conn_id]
                        : prev.filter((x) => x !== c.conn_id),
                    )
                  }
                  aria-label={`选择连接 ${c.broker_name ?? c.broker_id}`}
                />
              </label>
              <div className={s.itemMain}>
                <div className={s.itemTitle}>
                  {c.broker_name ?? c.broker_id}
                  {c.active && <Badge tone="info">活跃</Badge>}
                  <Badge tone={c.connected ? "success" : "danger"}>
                    {c.connected ? "已连接" : "未连接"}
                  </Badge>
                  {c.runtime_mode && <Badge tone="neutral">{c.runtime_mode}</Badge>}
                </div>
                <div className={s.itemSub}>
                  {c.account_id ?? "—"} · {c.account_type ?? "—"} · {c.client_path ?? "—"}
                </div>
                {healthMap[c.conn_id] && (
                  <div className={s.healthRow}>
                    <Badge tone={healthTone(healthMap[c.conn_id])}>
                      {healthLabel(healthMap[c.conn_id])}
                    </Badge>
                  </div>
                )}
              </div>
              <div className={s.itemActions}>
                {c.connected ? (
                  <Button size="sm" onClick={() => void disconnect(c.conn_id)}>
                    断开
                  </Button>
                ) : (
                  <Button
                    size="sm"
                    variant="primary"
                    onClick={() =>
                      void connect(c.conn_id).then((r) => {
                        if (!r.ok) setMsg(`连接失败：${r.reason ?? "未知原因"}`);
                      })
                    }
                  >
                    连接
                  </Button>
                )}
                <Button
                  size="sm"
                  variant="ghost"
                  disabled={c.active === true}
                  onClick={() => void setActive(c.conn_id)}
                >
                  设为活跃
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  disabled={healthBusy === c.conn_id}
                  onClick={() => void onHealth(c.conn_id)}
                >
                  {healthBusy === c.conn_id ? "探测中…" : "健康"}
                </Button>
                <Button
                  size="sm"
                  variant="danger"
                  disabled={c.active === true}
                  onClick={() => {
                    if (window.confirm(`确认删除连接 ${c.broker_name ?? c.broker_id}？`)) {
                      void remove(c.conn_id);
                    }
                  }}
                >
                  删除
                </Button>
              </div>
            </div>
          ))}
        </div>
      </Panel>

      <div className={s.note}>
        当前活跃连接：<strong>{active?.broker_name ?? active?.broker_id ?? "无"}</strong>
        。后端活跃连接全局唯一，多账户并行下单需经「多账户网格」批量接口。
      </div>
    </div>
  );
}

export default Brokers;

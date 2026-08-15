import { useEffect, useState } from "react";
import { api } from "../api.js";

/* 定时任务（P2）
   cron / 周期任务 + 关机/重启 + 状态监控
   分布式调度：可选 Redis 锁 Leader 选举
*/

export default function Scheduler() {
  const [msg, setMsg] = useState(null);
  const [loading, setLoading] = useState(false);
  const [tasks, setTasks] = useState([]);
  const [status, setStatus] = useState(null);

  /* 新建任务表单 */
  const [taskName, setTaskName] = useState("");
  const [taskCron, setTaskCron] = useState("0 9 * * 1-5");
  const [taskAction, setTaskAction] = useState("run_duel");
  const [taskParams, setTaskParams] = useState("{}");
  const [taskEnabled, setTaskEnabled] = useState(true);

  useEffect(() => { loadAll(); }, []);

  async function loadAll() {
    api.schedulerTasks().then(setTasks).catch(() => setTasks([]));
    api.schedulerStatus().then(setStatus).catch(() => setStatus(null));
  }

  async function createTask() {
    setLoading(true); setMsg(null);
    try {
      if (!taskName) throw new Error("请输入任务名称");
      let params = {};
      try { params = JSON.parse(taskParams); } catch { params = {}; }
      await api.schedulerCreateTask({
        name: taskName,
        cron: taskCron,
        action: taskAction,
        params,
        enabled: taskEnabled,
      });
      setMsg({ ok: true, t: `定时任务「${taskName}」已创建` });
      setTaskName(""); setTaskParams("{}");
      loadAll();
    } catch (e) { setMsg({ ok: false, t: e.message }); }
    finally { setLoading(false); }
  }

  async function deleteTask(id) {
    setLoading(true);
    try {
      await api.schedulerDeleteTask(id);
      setMsg({ ok: true, t: "任务已删除" });
      loadAll();
    } catch (e) { setMsg({ ok: false, t: e.message }); }
    finally { setLoading(false); }
  }

  async function toggleTask(id) {
    setLoading(true);
    try {
      await api.schedulerEnableTask(id);
      setMsg({ ok: true, t: "已切换任务状态" });
      loadAll();
    } catch (e) { setMsg({ ok: false, t: e.message }); }
    finally { setLoading(false); }
  }

  async function runDue() {
    setLoading(true); setMsg(null);
    try {
      await api.schedulerRunDue();
      setMsg({ ok: true, t: "已手动触发到期任务" });
      loadAll();
    } catch (e) { setMsg({ ok: false, t: e.message }); }
    finally { setLoading(false); }
  }

  async function doShutdown() {
    setLoading(true); setMsg(null);
    try {
      await api.schedulerShutdown();
      setMsg({ ok: true, t: "已发送关机指令，平台将在任务完成后关闭" });
    } catch (e) { setMsg({ ok: false, t: e.message }); }
    finally { setLoading(false); }
  }

  async function doRestart() {
    setLoading(true); setMsg(null);
    try {
      await api.schedulerRestart();
      setMsg({ ok: true, t: "已发送重启指令" });
    } catch (e) { setMsg({ ok: false, t: e.message }); }
    finally { setLoading(false); }
  }

  return (
    <div className="page">
      <div className="page-header">
        <h2>定时任务 <span style={{ fontSize: 13, color: "#8aa0c4" }}>Cron / 分布式调度</span></h2>
        <p>创建 cron/周期任务 · 手动触发 · 关机/重启控制</p>
      </div>

      <div className="card">
        <div className="status-bar">
          {status ? (
            <>
              <span>调度器状态：<strong>{status.running ? "运行中" : "已停止"}</strong></span>
              <span>Leader：<strong>{status.is_leader ? "本机" : "其他节点"}</strong></span>
              <span>锁：<strong>{status.lock_backend || "Memory"}</strong></span>
              <span>上次执行：<strong>{status.last_run || "—"}</strong></span>
            </>
          ) : (
            <span>调度器状态：未知</span>
          )}
          <div style={{ display: "flex", gap: 6, marginLeft: "auto" }}>
            <button className="btn-sm" onClick={runDue} disabled={loading}>触发到期任务</button>
            <button className="btn-sm" onClick={doShutdown} disabled={loading}>关机</button>
            <button className="btn-sm" onClick={doRestart} disabled={loading}>重启</button>
          </div>
        </div>
      </div>

      {msg && <div className={`msg ${msg.ok ? "success" : "error"}`}>{msg.t}</div>}

      <div className="card">
        <h3 style={{ marginBottom: 12 }}>新建任务</h3>
        <div className="form-row">
          <div className="form-field">
            <label>任务名称</label>
            <input value={taskName} onChange={(e) => setTaskName(e.target.value)} placeholder="每日开盘检查" />
          </div>
          <div className="form-field">
            <label>Cron 表达式</label>
            <input value={taskCron} onChange={(e) => setTaskCron(e.target.value)} placeholder="0 9 * * 1-5" />
          </div>
          <div className="form-field">
            <label>动作</label>
            <select value={taskAction} onChange={(e) => setTaskAction(e.target.value)}>
              <option value="run_duel">对盘核对</option>
              <option value="rebalance">再平衡</option>
              <option value="backtest">回测</option>
              <option value="alert">告警检查</option>
            </select>
          </div>
          <div className="form-field">
            <label>参数（JSON）</label>
            <input value={taskParams} onChange={(e) => setTaskParams(e.target.value)} placeholder='{}' />
          </div>
          <div className="form-field">
            <label>启用</label>
            <label className="checkbox">
              <input type="checkbox" checked={taskEnabled} onChange={(e) => setTaskEnabled(e.checked)} />
              启用
            </label>
          </div>
        </div>
        <button className="btn-primary" onClick={createTask} disabled={loading || !taskName}>
          {loading ? "创建中…" : "创建任务"}
        </button>
      </div>

      <div className="card">
        <h3 style={{ marginBottom: 12 }}>任务列表</h3>
        {!tasks.length ? <Empty>暂无定时任务</Empty> : (
          <div style={{ overflowX: "auto" }}>
            <table className="table">
              <thead><tr>
                <th>名称</th><th>Cron</th><th>动作</th><th>参数</th><th>状态</th><th>上次运行</th><th>操作</th>
              </tr></thead>
              <tbody>
                {tasks.map((t) => (
                  <tr key={t.id}>
                    <td><strong>{t.name}</strong></td>
                    <td><code>{t.cron}</code></td>
                    <td>{t.action}</td>
                    <td style={{ fontSize: 11, color: "#9bb", maxWidth: 160, overflow: "hidden", textOverflow: "ellipsis" }}>
                      {JSON.stringify(t.params || {})}
                    </td>
                    <td>
                      <span className={`tag ${t.enabled ? "ok" : ""}`}>
                        {t.enabled ? "启用" : "停用"}
                      </span>
                    </td>
                    <td style={{ fontSize: 12, color: "#9bb" }}>{t.last_run || "—"}</td>
                    <td style={{ display: "flex", gap: 4 }}>
                      <button className="btn-sm" onClick={() => toggleTask(t.id)}>
                        {t.enabled ? "停用" : "启用"}
                      </button>
                      <button className="btn-sm btn-danger-sm" onClick={() => deleteTask(t.id)}>删除</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}

function Empty({ children }) {
  return <div style={{ padding: "24px", textAlign: "center", color: "#556" }}>{children}</div>;
}
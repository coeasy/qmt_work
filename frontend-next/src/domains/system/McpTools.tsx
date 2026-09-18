import { useMemo, useState } from "react";
import {
  Badge,
  Button,
  DataTable,
  EmptyState,
  Input,
  Panel,
  Spinner,
  type Column,
} from "@/design/primitives";
import { systemApi, type McpCapabilities } from "@/services/api";
import { useAsync } from "@/hooks/useAsync";
import s from "../domain.module.css";

/**
 * MCP 工具浏览页。
 *
 * ★ 为什么要有这一页（审计 P1）：
 *   客户端实际暴露了 116 个 MCP 工具，但此前界面 `grep mcp` 零匹配 ——
 *   用户装完客户端根本不知道自己有这批可被 Agent 调用的能力，也不知道怎么接。
 *   「能力存在但不可发现」等同于不存在，所以这里把自省结果变成可查的清单。
 *
 * ★ 零 mock 纪律：自省失败就显示错误，绝不用示例数据冒充工具清单。
 *   `count: 0` 只可能来自后端真实返回空，且此时必须同时给出原因提示。
 *
 * 数据来源：`GET /api/v1/capabilities/mcp`（capabilities.py:capabilities_mcp）。
 */
interface ToolRow {
  name: string;
  prefix: string;
}

export function McpTools() {
  const mcp = useAsync<McpCapabilities>(() => systemApi.capabilitiesMcp(), []);
  const [q, setQ] = useState("");
  const [prefix, setPrefix] = useState("");

  const rows = useMemo<ToolRow[]>(() => {
    const list = mcp.data?.tools ?? [];
    return list.map((t) => ({ name: t, prefix: t.split("_", 1)[0] || "" }));
  }, [mcp.data]);

  const filtered = useMemo(() => {
    const kw = q.trim().toLowerCase();
    return rows.filter(
      (r) => (!prefix || r.prefix === prefix) && (!kw || r.name.toLowerCase().includes(kw)),
    );
  }, [rows, q, prefix]);

  const prefixes = useMemo(() => {
    const bp = mcp.data?.by_prefix ?? {};
    return Object.keys(bp).sort((a, b) => (bp[b] ?? 0) - (bp[a] ?? 0));
  }, [mcp.data]);

  const columns: Column<ToolRow>[] = [
    {
      key: "name",
      header: "工具名",
      mono: true,
      render: (r) => <span className={s.mono}>{r.name}</span>,
    },
    { key: "prefix", header: "分组", width: 120, render: (r) => <Badge tone="info">{r.prefix}</Badge> },
  ];

  const copyAll = async () => {
    if (!mcp.data) return;
    try {
      await navigator.clipboard.writeText(mcp.data.tools.join("\n"));
    } catch {
      /* 剪贴板不可用（非安全上下文）时静默：不影响浏览主流程 */
    }
  };

  if (mcp.loading) {
    return (
      <div className={s.center}>
        <Spinner />
      </div>
    );
  }

  if (mcp.error || !mcp.data) {
    return (
      <div className={s.page}>
        <EmptyState
          text={mcp.error ? `MCP 工具清单不可用：${mcp.error}` : "未返回 MCP 工具数据"}
          actionText="重试"
          onAction={() => void mcp.reload()}
        />
      </div>
    );
  }

  const d = mcp.data;

  return (
    <div className={s.page}>
      <div className={s.stats}>
        <div className={s.stat}>
          <div className={s.statLabel}>工具总数</div>
          <div className={s.statValue}>{d.count}</div>
        </div>
        <div className={s.stat}>
          <div className={s.statLabel}>接入端点</div>
          <div className={s.statValue}>{d.endpoint}</div>
          <div className={s.statSub}>{d.transport}</div>
        </div>
        <div className={s.stat}>
          <div className={s.statLabel}>监听地址</div>
          <div className={s.statValue}>{d.bind}</div>
          <div className={s.statSub}>{d.auth}</div>
        </div>
      </div>

      {d.local_only_note ? <div className={s.noteWarn}>{d.local_only_note}</div> : null}

      <Panel
        title="如何接入"
        extra={<Button size="sm" onClick={() => void mcp.reload()}>刷新</Button>}
      >
        <div className={s.kv}>
          <div className={s.kvKey}>端点</div>
          <div className={s.kvVal}>
            <span className={s.mono}>
              http://{d.bind}:&lt;port&gt;{d.endpoint}
            </span>
          </div>
        </div>
        <div className={s.kv}>
          <div className={s.kvKey}>传输</div>
          <div className={s.kvVal}>{d.transport}</div>
        </div>
        <div className={s.kv}>
          <div className={s.kvKey}>鉴权</div>
          <div className={s.kvVal}>{d.auth}</div>
        </div>
        <div className={s.note}>
          在支持 MCP 的客户端里把上面的端点填为 Streamable HTTP 服务器即可；本机回环免鉴权，
          远程访问必须配置主密钥或 admin scope 的 API Key。
        </div>
      </Panel>

      <Panel
        title={`工具清单（${filtered.length}/${d.count}）`}
        flush
        extra={
          <div className={s.inline}>
            <Input
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder="按名称过滤…"
              style={{ width: 180 }}
            />
            <select
              value={prefix}
              onChange={(e) => setPrefix(e.target.value)}
              className={s.mono}
              style={{ width: 140 }}
              aria-label="按分组过滤"
            >
              <option value="">全部分组</option>
              {prefixes.map((p) => (
                <option key={p} value={p}>
                  {p}（{d.by_prefix[p]}）
                </option>
              ))}
            </select>
            <Button size="sm" onClick={() => void copyAll()}>
              复制全部
            </Button>
          </div>
        }
      >
        <div className={s.tableArea}>
          <DataTable
            columns={columns}
            rows={filtered}
            rowKey={(r) => r.name}
            emptyText={d.count === 0 ? "后端未暴露任何 MCP 工具" : "无匹配工具"}
          />
        </div>
      </Panel>
    </div>
  );
}

export default McpTools;

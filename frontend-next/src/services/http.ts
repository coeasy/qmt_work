import type { Envelope } from "@/shared/types";

/**
 * 统一 REST 客户端。
 *
 * 契约约定（与后端一致，勿擅改）：
 * - 所有端点前缀 /api/v1
 * - 鉴权：Authorization: Bearer <key>（后端同时接受 X-API-Key）
 * - 响应包裹 { code, message, data }；code !== 0 视为业务失败并抛 ApiError
 * - 未连接券商时后端返回 503 + 引导信息 —— 必须原样抛出，绝不粉饰为成功
 */

const BASE = "/api/v1";
const DEFAULT_TIMEOUT = 15_000;
const KEY_STORAGE = "qmt.apiKey";

let apiKey = "";
try {
  apiKey = localStorage.getItem(KEY_STORAGE) ?? "";
} catch {
  apiKey = "";
}

export function getApiKey(): string {
  return apiKey;
}

/**
 * 手动设置 API Key（写入内存 + localStorage）。
 *
 * ★ 前端**通常不需要**调用它：后端对 loopback 请求免鉴权
 *   （见 `capabilities.py`：「admin scope 或主密钥（X-API-Key / Bearer）；loopback 免鉴权」），
 *   而桌面客户端的前端与后端同机 ⇒ 全程不带 Key 也能正常用。
 *
 * 保留本函数（且**故意**不做 UI 入口）是为两种场景：
 *   ① 非 loopback 部署（前端与后端不同机）；
 *   ② 排障时在控制台临时写入 Key。
 * 它是「零调用但合法」的导出，**不是漏接的孤儿逻辑**（R25 已核实并在此留档）。
 */
export function setApiKey(key: string): void {
  apiKey = key;
  try {
    localStorage.setItem(KEY_STORAGE, key);
  } catch {
    /* 隐私模式下降级为内存保存 */
  }
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: number;
  /**
   * ⚠️ 别拿它去断言「原因 = 没连券商」。
   * HTTP 503 也可能来自「后端仍在启动」「数据源未就绪」「上游拒绝」—— 与券商连接
   * 无关。真要判断券商可用性，用后端**业务返回**里的 `broker_unavailable` 字段
   * （trade.py 那种），而不是这个由 HTTP 状态码推导的位。
   * 目前全站无人读取它；保留只为兼容，新增代码请勿依赖。
   */
  readonly brokerUnavailable: boolean;

  constructor(message: string, status: number, code: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.brokerUnavailable = status === 503;
  }
}

type Query = Record<string, string | number | boolean | null | undefined>;

function buildUrl(path: string, query?: Query): string {
  const url = `${BASE}${path}`;
  if (!query) return url;
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(query)) {
    if (v === null || v === undefined || v === "") continue;
    sp.append(k, String(v));
  }
  const qs = sp.toString();
  return qs ? `${url}?${qs}` : url;
}

interface RequestOptions {
  query?: Query;
  body?: unknown;
  timeout?: number;
  retry?: number;
  signal?: AbortSignal;
}

async function request<T>(
  method: string,
  path: string,
  opts: RequestOptions = {},
): Promise<T> {
  const { query, body, timeout = DEFAULT_TIMEOUT, retry = method === "GET" ? 1 : 0 } = opts;

  let lastErr: unknown;
  for (let attempt = 0; attempt <= retry; attempt++) {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeout);
    try {
      const headers: Record<string, string> = { Accept: "application/json" };
      if (apiKey) headers.Authorization = `Bearer ${apiKey}`;
      if (body !== undefined) headers["Content-Type"] = "application/json";

      const res = await fetch(buildUrl(path, query), {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: opts.signal ?? ctrl.signal,
      });

      const text = await res.text();
      let payload: unknown = null;
      if (text) {
        try {
          payload = JSON.parse(text);
        } catch {
          payload = null;
        }
      }

      const env = payload as Envelope<T> | null;

      if (!res.ok) {
        const msg =
          env?.message ??
          // ★ 别替后端猜原因。HTTP 503 **不等于**「没连券商」—— 也可能是后端仍在
          //   启动、数据源未就绪、被上游拒绝。猜错会把排查方向彻底带偏（与后端
          //   `services/market/aggregates._unavailable` 是同一条纪律：
          //   绝不把「不支持」说成「网络坏了」）。这里只陈述事实 + 给下一步。
          (res.status === 503
            ? "服务暂不可用（HTTP 503）—— 后端可能仍在启动或数据源未就绪，请稍后重试"
            : `请求失败（HTTP ${res.status}）`);
        throw new ApiError(msg, res.status, env?.code ?? res.status);
      }

      if (env && typeof env === "object" && "code" in env) {
        if (env.code !== 0) {
          throw new ApiError(env.message ?? "业务处理失败", res.status, env.code);
        }
        return env.data;
      }
      // 兼容无包裹响应（如 /live）
      return payload as T;
    } catch (e) {
      lastErr = e;
      // 业务错误不重试；仅网络层错误（AbortError/TypeError）重试
      if (e instanceof ApiError) throw e;
      if (attempt === retry) break;
    } finally {
      clearTimeout(timer);
    }
  }
  // ★ 网络层失败必须换成中文，并且**区分超时与连不上**。
  //   直接抛 `lastErr.message` 的话，用户在最常见的一类故障（后端没起来 / 端口被占 /
  //   刚启动还没监听）上会看到浏览器英文原文 `Failed to fetch`，既看不懂也无从下手。
  //   同样不能笼统说「网络坏了」—— 本机客户端连本机后端，问题多半是后端进程。
  const netMsg = opts.signal?.aborted
    ? "请求已取消"
    : lastErr instanceof Error && lastErr.name === "AbortError"
      ? `请求超时（${Math.round(timeout / 1000)}s）—— 后端可能正忙或数据源较慢，可稍后重试`
      : "无法连接后端服务 —— 请确认客户端已启动；若持续出现，到「系统状态」查看后端健康";
  throw new ApiError(netMsg, 0, 0);
}

export const http = {
  get: <T>(path: string, opts?: RequestOptions) => request<T>("GET", path, opts),
  post: <T>(path: string, body?: unknown, opts?: RequestOptions) =>
    request<T>("POST", path, { ...opts, body }),
  put: <T>(path: string, body?: unknown, opts?: RequestOptions) =>
    request<T>("PUT", path, { ...opts, body }),
  patch: <T>(path: string, body?: unknown, opts?: RequestOptions) =>
    request<T>("PATCH", path, { ...opts, body }),
  del: <T>(path: string, opts?: RequestOptions) => request<T>("DELETE", path, opts),
};

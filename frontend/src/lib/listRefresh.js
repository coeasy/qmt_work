// 统一列表刷新机制（G-列表刷新）：
//
//   useListRefresh(loadFn, { scope, interval, deps })
//     ① 挂载即加载（等价于旧 useEffect(() => load(), [])）
//     ② Pane 激活即刷新 —— keep-alive 切回标签页不再显示陈旧列表
//        （useActiveInterval(fn, 0)：激活时立即执行一次，不建定时器）
//     ③ 可选 interval 轮询（0 = 不轮询）
//     ④ 订阅同作用域「列表已变更」事件：跨页面/跨标签的数据变更自动反映到本列表
//
//   notifyListChanged(scope | scopes)
//     在增/删/改成功后广播作用域事件，驱动所有订阅了该作用域的列表刷新。
//     作用域命名约定（全站唯一真相）：
//       api_keys      —— 设置页 API Key 列表
//       target_plans  —— 目标持仓计划列表
//       strategies    —— 策略市场目录（安装/发布）
//       broker_conns  —— 券商连接列表（增删/连接状态变化）
//       watchlist     —— 自选股（localStorage 双写之外的主动通知通道）
import { useEffect, useRef } from "react";
import { useActiveInterval } from "../hooks/useActiveInterval.js";
import { emit as emitEvent, on as onEvent, off as offEvent } from "./eventBus";

const EVT = "qmt:list-changed";

export function notifyListChanged(scopes) {
  const arr = Array.isArray(scopes) ? scopes : [scopes];
  if (!arr.length) return;
  emitEvent(EVT, { scopes: arr, ts: Date.now() });
}

const normScope = (s) => (Array.isArray(s) ? s.join("\u0001") : (s || ""));
const scopeList = (s) => (Array.isArray(s) ? s : (s ? [s] : []));

export function useListRefresh(loadFn, opts = {}) {
  const { scope, interval = 0, deps = [] } = opts;
  const fnRef = useRef(loadFn);
  fnRef.current = loadFn;   // 每次渲染取最新闭包，避免 effect 冻结首帧闭包

  // ① 挂载加载 + ② 激活即刷新 + ③ 可选轮询（useActiveInterval 内部已做 Pane 激活感知）
  useActiveInterval((...args) => fnRef.current(...args), interval, deps);

  // ④ 同作用域变更事件 → 刷新
  const scopeKey = normScope(scope);
  useEffect(() => {
    const scopes = scopeKey ? scopeKey.split("\u0001") : [];
    if (!scopes.length) return undefined;
    const handler = (detail) => {
      if (detail && Array.isArray(detail.scopes)
          && detail.scopes.some((x) => scopes.includes(x))) {
        fnRef.current();
      }
    };
    onEvent(EVT, handler);
    return () => offEvent(EVT, handler);
  }, [scopeKey]);
}

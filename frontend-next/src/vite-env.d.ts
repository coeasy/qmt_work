/// <reference types="vite/client" />

declare module "*.module.css" {
  const classes: { readonly [key: string]: string };
  export default classes;
}

/**
 * 桌面壳（Electron preload）注入的桥接 API。
 *
 * 在浏览器里打开（vite dev / 静态预览 / vitest）时 `window.electronAPI` 为 undefined，
 * 调用方必须按「可选能力」处理 —— 典型是自绘标题栏的窗口按钮在浏览器里不渲染。
 */
interface QmtElectronApi {
  appVersion: () => Promise<string>;
  openExternal: (url: string) => Promise<{ ok: boolean; error?: string }>;
  setAutoLaunch: (
    enabled: boolean,
  ) => Promise<{ ok: boolean; enabled?: boolean; error?: string }>;
  getAutoLaunch: () => Promise<{ enabled: boolean; error?: string }>;
  /** 启动失败页的「重新启动后端」：重跑启动流程（不触碰业务数据）。 */
  bootRetry?: () => Promise<boolean>;

  // ---- 自绘标题栏的窗口控制（frame:false 后由页面按钮触发）----
  windowMinimize?: () => Promise<boolean>;
  windowToggleMaximize?: () => Promise<boolean>;
  windowClose?: () => Promise<boolean>;
  windowIsMaximized?: () => Promise<boolean>;
  /** 订阅最大化状态变化；返回取消订阅函数。 */
  onWindowMaximizeChange?: (cb: (maximized: boolean) => void) => (() => void) | undefined;
}

interface Window {
  electronAPI?: QmtElectronApi;
}

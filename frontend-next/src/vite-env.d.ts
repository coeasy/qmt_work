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
  /** 关闭窗口 = **隐藏到托盘**（托盘可用时程序仍在后台跑）。不是退出。 */
  windowClose?: () => Promise<boolean>;
  /**
   * **真退出**：结束进程（含后端优雅停机）。
   *
   * ⚠️ 与 `windowClose` 语义不同：`windowClose` 只是隐藏到托盘。
   * 为什么必须有它：Windows 11 默认把新出现的托盘图标收进「隐藏的图标」溢出层，
   * 用户**看不见**托盘 ⇒ 只靠托盘菜单退出不可靠（实测用户报「右下角没有图标、
   * 无法真实退出」）。退出入口必须不依赖托盘的可见性。
   */
  quitApp?: () => Promise<boolean>;
  windowIsMaximized?: () => Promise<boolean>;
  /** 订阅最大化状态变化；返回取消订阅函数。 */
  onWindowMaximizeChange?: (cb: (maximized: boolean) => void) => (() => void) | undefined;

  /**
   * 打开系统目录选择框（数据目录设置用）。
   *
   * 浏览器里没有这个能力 ⇒ 可选；调用方必须准备好「手输路径」的降级路径，
   * 不能因为拿不到对话框就让目录设置整块不可用。
   * 返回 `null` 表示用户取消了选择（**不是**失败，不要弹错误）。
   */
  selectDirectory?: (opts?: { title?: string; defaultPath?: string }) => Promise<string | null>;
}

interface Window {
  electronAPI?: QmtElectronApi;
}

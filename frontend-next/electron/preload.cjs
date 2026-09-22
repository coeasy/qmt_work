// 预加载脚本：仅暴露最小、受控的桥接 API（contextIsolation 开启）。
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("electronAPI", {
  appVersion: () => ipcRenderer.invoke("app-version"),
  openExternal: (url) => ipcRenderer.invoke("open-external", url),
  setAutoLaunch: (enabled) => ipcRenderer.invoke("set-auto-launch", enabled),
  getAutoLaunch: () => ipcRenderer.invoke("get-auto-launch"),

  // ---- 启动失败页的「重新启动后端」----
  // 加载页是 data: URL，无法直接驱动主进程，只能经 IPC 请求重跑启动流程。
  // 该操作只重启后端进程 + 重新等待就绪，不触碰任何业务数据。
  bootRetry: () => ipcRenderer.invoke("boot-retry"),

  // ---- 自绘标题栏所需的窗口控制（无边框窗口下由页面自己画按钮）----
  // 说明：这些是**窗口级**操作，不涉及任何业务数据；close 走与标题栏一致的
  // 「隐藏到托盘」语义（主进程 close 处理器负责），不是强杀。
  windowMinimize: () => ipcRenderer.invoke("window-minimize"),
  windowToggleMaximize: () => ipcRenderer.invoke("window-toggle-maximize"),
  windowClose: () => ipcRenderer.invoke("window-close"),
  // ---- 真退出（结束进程，含后端优雅停机）----
  // ⚠️ 与 windowClose **不同**：windowClose 只是「隐藏到托盘」（托盘可用时），
  // 程序仍在后台跑；quitApp 才是真正结束。
  // 存在的理由：Win11 默认把新托盘图标折叠进「隐藏的图标」溢出层，用户常常
  // 看不到它 ⇒ 退出入口不能只依赖托盘。页面上必须有明确可见的「退出」。
  quitApp: () => ipcRenderer.invoke("app-quit"),
  windowIsMaximized: () => ipcRenderer.invoke("window-is-maximized"),
  // ---- 数据目录设置：打开系统「选择文件夹」对话框 ----
  // 返回 null = 用户取消（**不是**失败，调用方不要弹错误提示）。
  // 浏览器/单测环境没有这个能力，调用方必须能降级到手输路径。
  selectDirectory: (opts) => ipcRenderer.invoke("select-directory", opts || {}),

  /** 订阅最大化状态变化；返回取消订阅函数（组件卸载时务必调用）。 */
  onWindowMaximizeChange: (cb) => {
    const handler = (_e, maximized) => {
      try {
        cb(Boolean(maximized));
      } catch {
        /* 回调异常不应打断主进程事件流 */
      }
    };
    ipcRenderer.on("window-maximized", handler);
    return () => ipcRenderer.removeListener("window-maximized", handler);
  },
});

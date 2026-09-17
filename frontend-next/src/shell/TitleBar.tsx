import { useEffect, useState } from "react";
import { MenuBar } from "./MenuBar";
import s from "./shell.module.css";

/**
 * 自绘标题栏（配合 Electron 的 `frame: false`）。
 *
 * 为什么要有它：原生标题栏在深色终端里是一条刺眼的浅色横条，还会带上 Electron
 * 默认的英文菜单（File / Edit / View / Window / Help），整体观感与行情软件严重不符。
 * 改成无边框后，这一行同时承担「标题栏 + 菜单栏」两个职责（对标同花顺 / 大智慧的顶部条）。
 *
 * 降级：浏览器里打开（vite dev / 静态预览 / 单测）时没有 `window.electronAPI`，
 * 此时**不渲染窗口按钮**（画了也点不动），其余布局完全一致。
 */
export function TitleBar() {
  const api = typeof window !== "undefined" ? window.electronAPI : undefined;
  // 以「有没有最小化能力」判定是否处于桌面壳内，避免逐个能力判空
  const desktop = typeof api?.windowMinimize === "function";
  const [maximized, setMaximized] = useState(false);

  useEffect(() => {
    if (!desktop || !api?.windowIsMaximized) return;
    void api.windowIsMaximized().then(setMaximized).catch(() => {});
    // 主进程在 maximize/unmaximize 时推事件；返回取消订阅函数
    return api.onWindowMaximizeChange?.(setMaximized);
  }, [desktop, api]);

  return (
    <div className={s.titlebar}>
      <div className={s.tbBrand}>
        <span className={s.tbLogo} aria-hidden />
        <span className={s.tbTitle}>qmt_work</span>
        <span className={s.tbSub}>量化交易终端</span>
      </div>

      <MenuBar />

      {desktop && api && (
        <div className={s.tbWinBtns}>
          <button
            type="button"
            className={s.tbWinBtn}
            title="最小化"
            aria-label="最小化"
            onClick={() => void api.windowMinimize?.()}
          >
            <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden>
              <path d="M0 5h10" stroke="currentColor" strokeWidth="1" />
            </svg>
          </button>
          <button
            type="button"
            className={s.tbWinBtn}
            title={maximized ? "向下还原" : "最大化"}
            aria-label={maximized ? "向下还原" : "最大化"}
            onClick={() => void api.windowToggleMaximize?.()}
          >
            {maximized ? (
              <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden>
                <path d="M2.5 2.5V0.5h7v7h-2" fill="none" stroke="currentColor" strokeWidth="1" />
                <rect x="0.5" y="2.5" width="7" height="7" fill="none" stroke="currentColor" strokeWidth="1" />
              </svg>
            ) : (
              <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden>
                <rect x="0.5" y="0.5" width="9" height="9" fill="none" stroke="currentColor" strokeWidth="1" />
              </svg>
            )}
          </button>
          <button
            type="button"
            className={`${s.tbWinBtn} ${s.tbWinBtnClose}`}
            title="关闭"
            aria-label="关闭"
            onClick={() => void api.windowClose?.()}
          >
            <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden>
              <path d="M0.5 0.5l9 9M9.5 0.5l-9 9" stroke="currentColor" strokeWidth="1" />
            </svg>
          </button>
        </div>
      )}
    </div>
  );
}

export default TitleBar;

import { Component, type ErrorInfo, type ReactNode } from "react";

/**
 * 错误边界（P0-5）。
 *
 * 桌面壳最容易踩的坑：任一页面组件抛错 → React 卸载整棵树 → **整个窗口白屏**，
 * 用户既看不到原因，也无法切回其他页面。这里把错误限制在**单个窗格**内：
 *
 * - 壳级（main.tsx）：兜底，保证最坏情况也有提示；
 * - 窗格级（PaneTree）：`key=pageKey` 让切页自动重置，错误不跨页面残留。
 *
 * 刻意不做的：自动上报到后端。错误信息可能含代码/路径，先本地留痕，
 * 由用户点「复制诊断」主动提供，避免未经同意上传。
 */
type Props = {
  children: ReactNode;
  /** 展示用范围（页面 key / 面板名），同时作为日志前缀。 */
  scope?: string;
};

type State = { error: Error | null };

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // 不上报、不 rethrow：rethrow 会让上层边界也崩，失去「局部隔离」的意义。
    console.error(`[${this.props.scope ?? "app"}] render error`, error, info.componentStack);
  }

  private reset = () => this.setState({ error: null });

  private copy = () => {
    const err = this.state.error;
    if (!err) return;
    const text = `${this.props.scope ?? "app"}\n${err.message}\n${err.stack ?? ""}`;
    void navigator.clipboard?.writeText(text);
  };

  render() {
    const err = this.state.error;
    if (!err) return this.props.children;
    return (
      <div role="alert" style={{ padding: 24, maxWidth: 720 }}>
        <h3 style={{ margin: "0 0 8px", color: "var(--text)" }}>这个面板出错了</h3>
        <p style={{ margin: "0 0 12px", color: "var(--text-faint)", lineHeight: 1.6 }}>
          {this.props.scope ? `范围：${this.props.scope}。` : ""}
          其他面板不受影响，可以重试或回到首页。
        </p>
        <pre
          style={{
            maxHeight: 160,
            overflow: "auto",
            padding: 8,
            margin: "0 0 12px",
            fontSize: 12,
            background: "var(--bg-2)",
            borderRadius: 4,
            whiteSpace: "pre-wrap",
            wordBreak: "break-word",
          }}
        >
          {err.message}
        </pre>
        <div style={{ display: "flex", gap: 8 }}>
          <button type="button" onClick={this.reset}>重试</button>
          <button type="button" onClick={this.copy}>复制诊断</button>
          <button
            type="button"
            onClick={() => { window.location.hash = ""; }}
          >
            回首页
          </button>
        </div>
      </div>
    );
  }
}

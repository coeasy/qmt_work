import { Component } from "react";

/** 全局错误边界：页面渲染异常时展示兜底 UI，避免白屏（P1 前端健壮性）。 */
export default class ErrorBoundary extends Component {
  state = { hasError: false, message: "" };

  static getDerivedStateFromError(error) {
    return { hasError: true, message: String(error && error.message || error) };
  }

  componentDidCatch(error, info) {
    console.error("[ErrorBoundary]", error, info);
  }

  reset = () => this.setState({ hasError: false, message: "" });

  render() {
    if (this.state.hasError) {
      return (
        <div className="card" style={{ padding: 24, margin: 16 }}>
          <h3>页面渲染出错</h3>
          <p className="muted" style={{ whiteSpace: "pre-wrap" }}>{this.state.message}</p>
          <div className="btn-row">
            <button onClick={this.reset}>重试</button>
            <button className="ghost" onClick={() => window.dispatchEvent(new CustomEvent("nav", { detail: "dashboard" }))}>
              返回仪表盘
            </button>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}

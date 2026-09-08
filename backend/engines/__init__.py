"""engines 层：交易执行类引擎（算法拆单 / 涨停监控 / 条件单 / 策略运行容器 / 模拟盘）。

分层（P1-6 / M8，2026-09-08）：
  engines → tools（ashare 交易规则、indicators 指标）→ core（配置/状态/加密）
  engines → xtquant_client（BrokerError / order_status 状态词汇表）
  app / gateway / bootstrap → engines

约定：
- 引擎只依赖数据与工具层，**不得** import app.*、gateway.*、engines 之外的业务包。
- 交易所/行情基础设施仍在 gateway/（health/reconcile/wal/quote_bus/signal_router 等），
  它们通过 core.state 拿到引擎引用，方向为 gateway → core，不反向依赖 engines。
- tools/ 原路径保留 re-export shim 兼容存量 import，新代码请直接 from engines.xxx import。
"""

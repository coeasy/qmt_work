"""应用启动阶段编排（R1 重构）。

将 ``app/main.py`` 的 lifespan 拆分为按阶段组织的初始化函数，
主进程只负责 ``FastAPI()`` 构建 + include_router + 挂中间件。

历史背景：原 main.py 655 行单文件承担全部 lifespan 初始化（约 20+ 引擎/服务按序手动挂接），
维护困难。R1 重构把 lifespan 拆为 6 个阶段模块 + 优雅停机，按域分组、单一职责。

阶段顺序（启动）：
  phase_db        : DB 初始化、密钥、通知、告警、运行时配置、缓存、WAL
  phase_broker    : 券商连接、引导连接、行情管道注册
  phase_engines   : 风控、同步引擎、WS 管理、回测队列、模拟盘、策略运行时
  phase_watchdogs : 健康监控、泵守护、涨停、算法、条件单、订单超时
  phase_replay    : WAL 重放、委托对账
  phase_misc      : 行情缓存定时、elgt资金流采集、数据源预热

阶段顺序（停机）：shutdown.shutdown() 逆序关闭。

零功能回退：所有外部行为完全等价，仅内部代码组织。

⚠️ 本包只作**阶段模块的命名空间 + 文档载体**：``from app.bootstrap import
phase_broker`` 导入的是子模块，不经过这里定义的任何符号。

R25 清理说明：这里曾定义 ``PhaseFn`` / ``_ordered()`` / ``run_all()`` 三个符号，
是 R1 重构的中间产物。实际编排早已收敛到
``app/bootstrap/lifecycle.py::run_phases``（由 ``main.py`` 调用），这三个符号
**全仓零调用**（只在 ``__all__`` 里自我声明，属典型孤儿逻辑）。删掉它们，
免得读代码的人以为「启动走 run_all」而实际走的是 ``run_phases``。
``PhaseFn`` 的唯一真源现在是 ``lifecycle.py``。
"""
from __future__ import annotations

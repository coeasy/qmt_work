"""core 层：与具体业务/传输无关的共享内核（配置 / 运行期状态 / 密码学工具）。

依赖方向（自底向上，禁止反向）：
  xtquant_client / paper → core
  gateway / tools        → core（不得 import app.*）
  app（FastAPI 装配层）   → core + 全部

历史说明：2026-09-08 P1-2 (M1) 将原 app/state.py、app/config.py、app/crypto.py
下沉至此消除「gateway/tools 反向依赖 app」的双向耦合；app/ 原路径保留
re-export shim 兼容存量引用，新代码请直接 from core.xxx import。
"""

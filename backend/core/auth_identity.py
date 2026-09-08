"""请求身份上下文（P1-2 延续 / M1，2026-09-08）。

仅承载「当前请求的 API Key 身份」这一基础设施事实：
- 中间件校验密钥后写入；
- 审计链（core/db.py audit）读取，用于把审计事件归因到具体密钥。

从 gateway/auth.py 下沉的原因：core/db.py 需要它写审计身份，若保留在 gateway 则
出现 core → gateway 的反向依赖。ContextVar 本身零业务逻辑，属内核基础设施。

gateway/auth.py 仍 re-export 同名对象以兼容存量 import。
"""
from contextvars import ContextVar

# 当前请求对应的 API Key 标识：主密钥固定为 "master"，子密钥为库内行 id。
current_api_key_id: ContextVar[str] = ContextVar("current_api_key_id", default="")

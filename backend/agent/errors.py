"""Agent 异常：未配置时端点返回 503 引导。"""


class AgentNotConfigured(Exception):
    """Agent 未配置（缺 API Key / 未启用）→ 端点应返回 503。"""

    def __init__(self, message: str = "Agent 未配置"):
        super().__init__(message)

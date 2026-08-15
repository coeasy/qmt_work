"""后端版本号唯一来源。

所有对外暴露版本（/health、/ready、FastAPI OpenAPI、系统广播）统一引用本模块，
避免多处分发的硬编码版本不一致。前端版本独立维护于 frontend/package.json。
"""

__version__ = "0.3.1"

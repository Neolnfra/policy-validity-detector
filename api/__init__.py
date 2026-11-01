"""
api —— 对外开放功能接口层
================================================================================

模块职责：
    schemas.py   请求 / 响应数据契约（Pydantic v2）
    routes.py    核心 RESTful API 与 Agent 编排
    webhook.py   企业微信 / 飞书机器人回调

本层依赖 FastAPI，负责协议转换与错误码映射；业务逻辑全部下沉到 core/。
"""

from api import routes, schemas, webhook  # noqa: F401

__all__ = ["routes", "schemas", "webhook"]

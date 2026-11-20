"""统一业务错误与 HTTP 状态码。"""
from __future__ import annotations


class ServiceError(Exception):
    """业务错误，http_status 决定返回码。"""

    def __init__(self, http_status: int, message: str) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.message = message


# 400：参数格式错误
BAD_REQUEST = 400
# 404：资源不存在
NOT_FOUND = 404
# 409 冲突：状态冲突 / 修订过期 / 版号过期
CONFLICT = 409
# 422：提交内容本身不合法（落点不在首测图上、场重叠等）
UNPROCESSABLE = 422
# 500：写入失败（事务回滚）
INTERNAL = 500

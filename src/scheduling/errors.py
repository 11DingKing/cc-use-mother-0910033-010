"""领域错误：携带稳定错误码、可读说明与结构化原因，便于接口解释。"""
from __future__ import annotations

from typing import Any


class SchedulingError(Exception):
    """所有排班领域错误的基类。"""

    status = 422

    def __init__(
        self,
        code: str,
        message: str,
        *,
        reasons: list[dict[str, Any]] | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.reasons = reasons or []
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
        }
        if self.reasons:
            payload["reasons"] = self.reasons
        if self.details:
            payload["details"] = self.details
        return payload


class ValidationError(SchedulingError):
    """请求数据或资质校验未通过（422）。"""

    status = 422


class NotFoundError(SchedulingError):
    """引用的领域对象不存在（404）。"""

    status = 404


class ConflictError(SchedulingError):
    """状态冲突、版本冲突或并发抢占（409）。"""

    status = 409

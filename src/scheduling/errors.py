"""领域错误类型，携带可供接口解释的结构化细节。"""
from __future__ import annotations


class DomainError(Exception):
    """领域错误基类。"""

    code = "domain_error"
    http_status = 400

    def __init__(self, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class NotFoundError(DomainError):
    code = "not_found"
    http_status = 404


class ValidationError(DomainError):
    code = "validation_error"
    http_status = 400


class ConflictError(DomainError):
    code = "conflict"
    http_status = 409


class CapacityError(DomainError):
    code = "capacity_exceeded"
    http_status = 409


class CoverageError(DomainError):
    """责任覆盖不足：附带覆盖报告（缺口、候选替代、被拒原因）。"""

    code = "coverage_insufficient"
    http_status = 409

    def __init__(self, message: str, report) -> None:
        super().__init__(message, details={"report": report.to_dict()})
        self.report = report

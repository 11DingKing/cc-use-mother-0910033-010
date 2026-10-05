"""主诊医师授权排班服务端。

领域层（models/repository/service）与传输层（httpapi/server）分离，
仅依赖 Python 标准库，对应 domain/contract.json 的四项不变量：

- 责任覆盖约束：每个高风险服务时段必须有合格主诊医师承担责任；
- 资格排班快照：排班确认时冻结资质/注册/授权快照；
- 跨院支援授权：院际支援需在授权窗口内，与院内授权区分；
- 并发预约锁定：同一事务内完成校验与写入，拒绝重叠/并发抢占。
"""
from __future__ import annotations

from .errors import (
    ConflictError,
    NotFoundError,
    SchedulingError,
    ValidationError,
)
from .httpapi import create_server
from .repository import Repository
from .service import SchedulingService

__all__ = [
    "ConflictError",
    "NotFoundError",
    "Repository",
    "SchedulingError",
    "SchedulingService",
    "ValidationError",
    "create_server",
]

"""主诊医师授权排班领域模型。

对应领域契约的关键不变量：

- 责任覆盖约束：每个高风险项目服务时段都必须有具备资格的主诊医师负责。
- 资格排班快照：排班确认时冻结资格快照，后续资质或授权变化不回溯改写。
- 跨院支援授权：跨机构承担服务必须持有明确的支援授权。
- 并发预约锁定：预约容量在 Store 锁内原子检查并扣减。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class AuthorizationStatus(str, Enum):
    """授权状态。"""

    ACTIVE = "active"  # 有效
    SUSPENDED = "suspended"  # 已暂停：阻断新排班，已确认班次标记为风险
    REVOKED = "revoked"  # 已撤销


class ShiftStatus(str, Enum):
    """班次状态。"""

    DRAFT = "draft"  # 草稿：尚未锁定责任覆盖
    CONFIRMED = "confirmed"  # 已确认：资格快照冻结、责任覆盖锁定
    COMPLETED = "completed"  # 已完成：原责任人永久保留
    CANCELLED = "cancelled"  # 已取消


class AssignmentStatus(str, Enum):
    """责任指派状态。"""

    ACTIVE = "active"  # 当前责任人
    REPLACED = "replaced"  # 已被替班，保留历史
    COMPLETED = "completed"  # 服务已完成，责任人封存


@dataclass(frozen=True)
class Institution:
    """机构及其注册的高风险项目范围。"""

    institution_id: str
    name: str
    registered_projects: frozenset[str]


@dataclass(frozen=True)
class Project:
    """高风险项目及其所需资质。"""

    code: str
    name: str
    required_qualification: str
    risk_level: str = "high"


@dataclass(frozen=True)
class Physician:
    """执业人员及其资质集合。"""

    physician_id: str
    name: str
    home_institution_id: str
    qualifications: frozenset[str]


@dataclass(frozen=True)
class Authorization:
    """项目授权：医师在某机构承担某项目的许可。

    support=True 表示跨院支援授权，是跨机构承担服务的前提。
    """

    authorization_id: str
    physician_id: str
    institution_id: str
    project_code: str
    valid_from: datetime
    valid_to: datetime
    support: bool = False
    status: AuthorizationStatus = AuthorizationStatus.ACTIVE


@dataclass(frozen=True)
class AvailabilitySlot:
    """医师在某机构申报的可用时段。"""

    slot_id: str
    physician_id: str
    institution_id: str
    start: datetime
    end: datetime


@dataclass(frozen=True)
class WorkLimitPolicy:
    """连续工作限制。

    间隔小于 min_rest_hours 的班次视为同一连续工作段；
    每段的跨度与累计工作时长都不能超过上限。
    """

    min_rest_hours: float = 8.0
    max_continuous_span_hours: float = 16.0
    max_block_work_hours: float = 12.0


@dataclass(frozen=True)
class QualificationSnapshot:
    """排班确认时冻结的资格快照，用于审计与责任追溯。"""

    physician_id: str
    qualifications: tuple[str, ...]
    authorization_id: str
    institution_id: str
    project_code: str
    support: bool
    frozen_at: datetime


@dataclass(frozen=True)
class Assignment:
    """一次责任指派，携带冻结的资格快照。"""

    assignment_id: str
    shift_id: str
    physician_id: str
    snapshot: QualificationSnapshot
    status: AssignmentStatus
    reason: str
    created_at: datetime
    replaced_by: str | None = None


@dataclass
class Shift:
    """高风险项目服务班次（聚合根，仅在 Store 锁内变更）。"""

    shift_id: str
    institution_id: str
    project_code: str
    start: datetime
    end: datetime
    capacity: int = 1
    status: ShiftStatus = ShiftStatus.DRAFT
    assignments: list[Assignment] = field(default_factory=list)
    bookings: list[str] = field(default_factory=list)
    version: int = 0

    def current_assignment(self) -> Assignment | None:
        """当前责任人（状态为 ACTIVE 的最近一次指派）。"""
        for item in reversed(self.assignments):
            if item.status is AssignmentStatus.ACTIVE:
                return item
        return None


# ---- 序列化 ----


def institution_to_dict(value: Institution) -> dict:
    return {
        "institution_id": value.institution_id,
        "name": value.name,
        "registered_projects": sorted(value.registered_projects),
    }


def project_to_dict(value: Project) -> dict:
    return {
        "code": value.code,
        "name": value.name,
        "required_qualification": value.required_qualification,
        "risk_level": value.risk_level,
    }


def physician_to_dict(value: Physician) -> dict:
    return {
        "physician_id": value.physician_id,
        "name": value.name,
        "home_institution_id": value.home_institution_id,
        "qualifications": sorted(value.qualifications),
    }


def authorization_to_dict(value: Authorization) -> dict:
    return {
        "authorization_id": value.authorization_id,
        "physician_id": value.physician_id,
        "institution_id": value.institution_id,
        "project_code": value.project_code,
        "valid_from": value.valid_from.isoformat(),
        "valid_to": value.valid_to.isoformat(),
        "support": value.support,
        "status": value.status.value,
    }


def availability_to_dict(value: AvailabilitySlot) -> dict:
    return {
        "slot_id": value.slot_id,
        "physician_id": value.physician_id,
        "institution_id": value.institution_id,
        "start": value.start.isoformat(),
        "end": value.end.isoformat(),
    }


def snapshot_to_dict(value: QualificationSnapshot) -> dict:
    return {
        "physician_id": value.physician_id,
        "qualifications": list(value.qualifications),
        "authorization_id": value.authorization_id,
        "institution_id": value.institution_id,
        "project_code": value.project_code,
        "support": value.support,
        "frozen_at": value.frozen_at.isoformat(),
    }


def assignment_to_dict(value: Assignment) -> dict:
    return {
        "assignment_id": value.assignment_id,
        "shift_id": value.shift_id,
        "physician_id": value.physician_id,
        "status": value.status.value,
        "reason": value.reason,
        "replaced_by": value.replaced_by,
        "created_at": value.created_at.isoformat(),
        "snapshot": snapshot_to_dict(value.snapshot),
    }


def shift_to_dict(value: Shift) -> dict:
    current = value.current_assignment()
    completed = next(
        (a for a in reversed(value.assignments) if a.status is AssignmentStatus.COMPLETED),
        None,
    )
    responsible = current or completed
    return {
        "shift_id": value.shift_id,
        "institution_id": value.institution_id,
        "project_code": value.project_code,
        "start": value.start.isoformat(),
        "end": value.end.isoformat(),
        "capacity": value.capacity,
        "status": value.status.value,
        "version": value.version,
        "coverage_locked": value.status in (ShiftStatus.CONFIRMED, ShiftStatus.COMPLETED),
        "responsible": responsible.physician_id if responsible else None,
        "assignments": [assignment_to_dict(a) for a in value.assignments],
        "bookings": list(value.bookings),
    }

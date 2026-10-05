"""领域模型与状态常量。

为减少样板，仓库内使用不可变语义的字典文档（deepcopy 进出仓库），
本模块集中提供状态常量、文档构造器与时间工具，避免键名散落。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

# ---- 授权 / 注册状态 ----
AUTH_ACTIVE = "active"
AUTH_SUSPENDED = "suspended"

REG_ACTIVE = "active"

AUTH_KIND_INSTITUTION = "institution"       # 院内授权
AUTH_KIND_CROSS_SUPPORT = "cross_support"   # 跨院支援授权

# ---- 时段状态 ----
SESSION_DRAFT = "draft"
SESSION_CONFIRMED = "confirmed"
SESSION_COMPLETED = "completed"
SESSION_CANCELLED = "cancelled"

# ---- 派班状态 ----
ASSIGN_PROPOSED = "proposed"
ASSIGN_ACTIVE = "active"
ASSIGN_REPLACED = "replaced"
ASSIGN_CANCELLED = "cancelled"

# ---- 快照类型 ----
SNAPSHOT_CONFIRMATION = "confirmation"
SNAPSHOT_SUBSTITUTION = "substitution"

# ---- 预约状态 ----
APPT_BOOKED = "booked"
APPT_CANCELLED = "cancelled"


def parse_time(value: str | datetime) -> datetime:
    """统一时间输入：要求带时区，避免排班窗口歧义。"""
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"无法解析时间：{value!r}") from exc
    if dt.tzinfo is None:
        raise ValueError(f"时间必须包含时区：{value!r}")
    return dt


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def new_physician(physician_id: str, name: str) -> dict[str, Any]:
    return {
        "id": physician_id,
        "name": name,
        # code -> {"name", "level", "expires_at": iso|None}
        "qualifications": {},
        # {"max_consecutive_days": int, "min_rest_hours": float}
        "work_rule": None,
    }


def new_organization(org_id: str, name: str) -> dict[str, Any]:
    return {"id": org_id, "name": name}


def new_registration(
    physician_id: str,
    org_id: str,
    scopes: list[str],
    valid_from: datetime,
    valid_until: datetime | None,
) -> dict[str, Any]:
    return {
        "physician_id": physician_id,
        "org_id": org_id,
        "scopes": list(scopes),
        "valid_from": valid_from,
        "valid_until": valid_until,
        "status": REG_ACTIVE,
    }


def new_project(
    project_id: str,
    org_id: str,
    name: str,
    risk_level: str,
    required_qualifications: list[str],
) -> dict[str, Any]:
    return {
        "id": project_id,
        "org_id": org_id,
        "name": name,
        "risk_level": risk_level,
        "required_qualifications": list(required_qualifications),
    }


def new_authorization(
    auth_id: str,
    project_id: str,
    physician_id: str,
    kind: str,
    host_org_id: str,
    source_org_id: str | None,
    valid_from: datetime,
    valid_until: datetime,
) -> dict[str, Any]:
    return {
        "id": auth_id,
        "project_id": project_id,
        "physician_id": physician_id,
        "kind": kind,
        "host_org_id": host_org_id,
        "source_org_id": source_org_id,
        "valid_from": valid_from,
        "valid_until": valid_until,
        "status": AUTH_ACTIVE,
        "suspend_reason": None,
    }


def new_session(
    session_id: str,
    project: dict[str, Any],
    start: datetime,
    end: datetime,
    capacity: int,
) -> dict[str, Any]:
    return {
        "id": session_id,
        "project_id": project["id"],
        "org_id": project["org_id"],
        "start": start,
        "end": end,
        "required_qualifications": list(project["required_qualifications"]),
        "risk_level": project["risk_level"],
        "status": SESSION_DRAFT,
        "capacity": capacity,
        "booked_count": 0,
        "version": 1,
    }


def new_assignment(
    assignment_id: str,
    session_id: str,
    physician_id: str,
    status: str,
    snapshot_id: str | None,
    created_at: datetime,
) -> dict[str, Any]:
    return {
        "id": assignment_id,
        "session_id": session_id,
        "physician_id": physician_id,
        "status": status,
        "snapshot_id": snapshot_id,
        "created_at": created_at,
        "replaced_at": None,
    }


def new_snapshot(
    snapshot_id: str,
    session: dict[str, Any],
    physician: dict[str, Any],
    kind: str,
    frozen_at: datetime,
    qualification_view: dict[str, Any],
    registration_view: dict[str, Any] | None,
    authorization_view: dict[str, Any],
) -> dict[str, Any]:
    return {
        "id": snapshot_id,
        "kind": kind,
        "session_id": session["id"],
        "project_id": session["project_id"],
        "org_id": session["org_id"],
        "physician_id": physician["id"],
        "physician_name": physician["name"],
        "frozen_at": frozen_at,
        "session_start": session["start"],
        "session_end": session["end"],
        "required_qualifications": list(session["required_qualifications"]),
        "qualifications": qualification_view,
        "registration": registration_view,
        "authorization": authorization_view,
    }


def new_service_record(
    record_id: str,
    session: dict[str, Any],
    assignment: dict[str, Any],
    snapshot: dict[str, Any],
    label: str,
    completed_at: datetime,
) -> dict[str, Any]:
    return {
        "id": record_id,
        "session_id": session["id"],
        "label": label,
        "physician_id": assignment["physician_id"],
        "physician_name": snapshot["physician_name"],
        "snapshot_id": snapshot["id"],
        "completed_at": completed_at,
    }


def new_appointment(
    appointment_id: str,
    session_id: str,
    patient_ref: str,
    booked_at: datetime,
) -> dict[str, Any]:
    return {
        "id": appointment_id,
        "session_id": session_id,
        "patient_ref": patient_ref,
        "status": APPT_BOOKED,
        "booked_at": booked_at,
    }

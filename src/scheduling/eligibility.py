"""资格评估与责任覆盖解释。

评估结果不仅给出"能否承担"，还给出每条被拒原因与候选替代，
供接口向机构合规员解释覆盖不足。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .models import (
    Assignment,
    AssignmentStatus,
    Authorization,
    AuthorizationStatus,
    Physician,
    Shift,
    ShiftStatus,
    assignment_to_dict,
)
from .store import Store


@dataclass(frozen=True)
class Rejection:
    """一条资格拒绝原因。"""

    rule: str
    message: str

    def to_dict(self) -> dict:
        return {"rule": self.rule, "message": self.message}


@dataclass(frozen=True)
class PhysicianEvaluation:
    """单个医师对某班次的评估结果。"""

    physician_id: str
    eligible: bool
    rejections: tuple[Rejection, ...]
    authorization_id: str | None
    support: bool

    def to_candidate_dict(self) -> dict:
        return {
            "physician_id": self.physician_id,
            "authorization_id": self.authorization_id,
            "support": self.support,
        }

    def to_rejected_dict(self) -> dict:
        return {
            "physician_id": self.physician_id,
            "reasons": [item.to_dict() for item in self.rejections],
        }


@dataclass(frozen=True)
class CoverageReport:
    """责任覆盖报告：状态、缺口、风险、候选替代与被拒原因。"""

    shift_id: str
    status: str  # uncovered / covered / at_risk / completed / cancelled
    shift_gaps: tuple[Rejection, ...]
    responsible: dict | None
    risks: tuple[str, ...]
    candidates: tuple[PhysicianEvaluation, ...]
    rejected: tuple[PhysicianEvaluation, ...]

    def to_dict(self) -> dict:
        return {
            "shift_id": self.shift_id,
            "status": self.status,
            "shift_gaps": [item.to_dict() for item in self.shift_gaps],
            "responsible": self.responsible,
            "risks": list(self.risks),
            "candidates": [item.to_candidate_dict() for item in self.candidates],
            "rejected": [item.to_rejected_dict() for item in self.rejected],
        }


def evaluate_physician(store: Store, shift: Shift, physician: Physician) -> PhysicianEvaluation:
    """评估单个医师能否承担班次，收集全部拒绝原因。"""
    rejections: list[Rejection] = []

    project = store.projects.get(shift.project_code)
    if project is not None and project.required_qualification not in physician.qualifications:
        rejections.append(
            Rejection("qualification", f"缺少项目所需资质：{project.required_qualification}")
        )

    authorization, auth_rejection = _select_authorization(store, shift, physician)
    if auth_rejection is not None:
        rejections.append(auth_rejection)

    if not _availability_covers(store, shift, physician):
        rejections.append(Rejection("availability", "可用时段未覆盖班次时段"))

    rejections.extend(_check_work_limits(store, physician.physician_id, shift))

    return PhysicianEvaluation(
        physician_id=physician.physician_id,
        eligible=not rejections,
        rejections=tuple(rejections),
        authorization_id=authorization.authorization_id if authorization else None,
        support=authorization.support if authorization else False,
    )


def evaluate_coverage(store: Store, shift: Shift) -> CoverageReport:
    """生成班次的责任覆盖报告。"""
    gaps: list[Rejection] = []
    institution = store.institutions.get(shift.institution_id)
    if institution is None:
        gaps.append(Rejection("institution_missing", f"机构不存在：{shift.institution_id}"))
    elif shift.project_code not in institution.registered_projects:
        gaps.append(Rejection("institution_scope", f"机构未注册项目：{shift.project_code}"))
    if shift.project_code not in store.projects:
        gaps.append(Rejection("project_missing", f"项目不存在：{shift.project_code}"))

    if shift.status is ShiftStatus.CANCELLED:
        return CoverageReport(shift.shift_id, "cancelled", tuple(gaps), None, (), (), ())

    if shift.status is ShiftStatus.COMPLETED:
        # 已完成服务永久保留原责任人
        sealed = next(
            (a for a in reversed(shift.assignments) if a.status is AssignmentStatus.COMPLETED),
            None,
        )
        return CoverageReport(
            shift.shift_id,
            "completed",
            tuple(gaps),
            assignment_to_dict(sealed) if sealed else None,
            (),
            (),
            (),
        )

    responsible = None
    risks: list[str] = []
    status = "uncovered"
    current = shift.current_assignment()
    if current is not None:
        responsible = assignment_to_dict(current)
        risks = _assignment_risks(store, current)
        status = "at_risk" if risks else "covered"

    candidates: tuple[PhysicianEvaluation, ...] = ()
    rejected: tuple[PhysicianEvaluation, ...] = ()
    if not gaps:
        evaluations = [
            evaluate_physician(store, shift, physician)
            for physician in sorted(store.physicians.values(), key=lambda p: p.physician_id)
        ]
        eligible = [item for item in evaluations if item.eligible]
        eligible.sort(
            key=lambda item: (
                store.physicians[item.physician_id].home_institution_id != shift.institution_id,
                _workload(store, item.physician_id),
                item.physician_id,
            )
        )
        candidates = tuple(eligible)
        rejected = tuple(item for item in evaluations if not item.eligible)

    return CoverageReport(
        shift.shift_id, status, tuple(gaps), responsible, tuple(risks), candidates, rejected
    )


def _select_authorization(
    store: Store, shift: Shift, physician: Physician
) -> tuple[Authorization | None, Rejection | None]:
    """选择可用于该班次的授权，或给出最可执行的拒绝原因。"""
    matches = [
        item
        for item in store.authorizations.values()
        if item.physician_id == physician.physician_id
        and item.institution_id == shift.institution_id
        and item.project_code == shift.project_code
    ]
    if not matches:
        return None, Rejection("authorization_missing", "未持有该机构该项目的授权")
    covering = [a for a in matches if a.valid_from <= shift.start and a.valid_to >= shift.end]
    if not covering:
        return None, Rejection("authorization_window", "授权有效期未覆盖班次时段")
    if physician.home_institution_id != shift.institution_id:
        support = [a for a in covering if a.support]
        if not support:
            return None, Rejection("support_missing", "跨院承担服务需要明确的跨院支援授权")
        covering = support
    active = [a for a in covering if a.status is AuthorizationStatus.ACTIVE]
    if active:
        return max(active, key=lambda a: (a.valid_to, a.authorization_id)), None
    if any(a.status is AuthorizationStatus.SUSPENDED for a in covering):
        return None, Rejection("authorization_suspended", "授权已暂停，恢复前不能承担新班次")
    return None, Rejection("authorization_revoked", "授权已撤销")


def _availability_covers(store: Store, shift: Shift, physician: Physician) -> bool:
    return any(
        slot.physician_id == physician.physician_id
        and slot.institution_id == shift.institution_id
        and slot.start <= shift.start
        and slot.end >= shift.end
        for slot in store.availability.values()
    )


def _work_intervals(
    store: Store, physician_id: str, exclude_shift_id: str
) -> list[tuple[str, datetime, datetime]]:
    """医师已承担的工作时段（已确认/已完成班次上的有效指派）。"""
    intervals: list[tuple[str, datetime, datetime]] = []
    for shift in store.shifts.values():
        if shift.shift_id == exclude_shift_id:
            continue
        if shift.status not in (ShiftStatus.CONFIRMED, ShiftStatus.COMPLETED):
            continue
        for assignment in shift.assignments:
            if assignment.physician_id != physician_id:
                continue
            if assignment.status in (AssignmentStatus.ACTIVE, AssignmentStatus.COMPLETED):
                intervals.append((shift.shift_id, shift.start, shift.end))
    return sorted(intervals, key=lambda item: item[1])


def _check_work_limits(store: Store, physician_id: str, shift: Shift) -> list[Rejection]:
    """连续工作限制：不重叠，且连续工作段的跨度与累计时长不超限。"""
    policy = store.policy
    intervals = _work_intervals(store, physician_id, exclude_shift_id=shift.shift_id)

    for other_id, start, end in intervals:
        if shift.start < end and start < shift.end:
            return [Rejection("overlap", f"与已确认班次 {other_id} 时间重叠")]

    rest = timedelta(hours=policy.min_rest_hours)
    blocks: list[list] = []  # [开始, 结束, 累计工作秒, 成员班次]
    for shift_id, start, end in sorted(
        intervals + [(shift.shift_id, shift.start, shift.end)], key=lambda item: item[1]
    ):
        if blocks and start - blocks[-1][1] < rest:
            blocks[-1][1] = max(blocks[-1][1], end)
            blocks[-1][2] += (end - start).total_seconds()
            blocks[-1][3].append(shift_id)
        else:
            blocks.append([start, end, (end - start).total_seconds(), [shift_id]])

    rejections: list[Rejection] = []
    for block_start, block_end, work_seconds, members in blocks:
        if shift.shift_id not in members:
            continue
        span_hours = (block_end - block_start).total_seconds() / 3600
        if span_hours > policy.max_continuous_span_hours:
            rejections.append(
                Rejection(
                    "work_block_span",
                    f"连续工作跨度 {span_hours:.1f} 小时超过上限 {policy.max_continuous_span_hours} 小时",
                )
            )
        work_hours = work_seconds / 3600
        if work_hours > policy.max_block_work_hours:
            rejections.append(
                Rejection(
                    "work_block_hours",
                    f"连续工作段累计工作 {work_hours:.1f} 小时超过上限 {policy.max_block_work_hours} 小时",
                )
            )
    return rejections


def _assignment_risks(store: Store, assignment: Assignment) -> list[str]:
    """已确认班次的实时覆盖风险（快照保持冻结，风险按当前授权状态推导）。"""
    authorization = store.authorizations.get(assignment.snapshot.authorization_id)
    if authorization is None:
        return ["资格快照对应的授权记录不存在"]
    if authorization.status is AuthorizationStatus.SUSPENDED:
        return [f"授权 {authorization.authorization_id} 已暂停，责任覆盖存在风险，需替班或复核"]
    if authorization.status is AuthorizationStatus.REVOKED:
        return [f"授权 {authorization.authorization_id} 已撤销，责任覆盖存在风险，需替班或复核"]
    return []


def _workload(store: Store, physician_id: str) -> int:
    return len(_work_intervals(store, physician_id, exclude_shift_id=""))

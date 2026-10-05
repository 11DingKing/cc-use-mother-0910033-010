"""排班、注册与预约领域服务。"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import Callable

from .eligibility import CoverageReport, evaluate_coverage, evaluate_physician
from .errors import (
    CapacityError,
    ConflictError,
    CoverageError,
    NotFoundError,
    ValidationError,
)
from .models import (
    Assignment,
    AssignmentStatus,
    Authorization,
    AuthorizationStatus,
    AvailabilitySlot,
    Institution,
    Physician,
    Project,
    QualificationSnapshot,
    Shift,
    ShiftStatus,
)
from .store import Store


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RegistryService:
    """维护医师资质、机构注册、项目授权与可用时段。"""

    def __init__(self, store: Store, clock: Callable[[], datetime] | None = None) -> None:
        self.store = store
        self.clock = clock or _utcnow

    # -- 机构与项目 --

    def register_institution(
        self, institution_id: str, name: str, registered_projects
    ) -> Institution:
        if not institution_id or not name:
            raise ValidationError("机构编号与名称不能为空")
        institution = Institution(institution_id, name, frozenset(registered_projects))
        with self.store.locked():
            self.store.institutions[institution_id] = institution
        return institution

    def register_project(
        self, code: str, name: str, required_qualification: str, risk_level: str = "high"
    ) -> Project:
        if not code or not required_qualification:
            raise ValidationError("项目编号与所需资质不能为空")
        project = Project(code, name, required_qualification, risk_level)
        with self.store.locked():
            self.store.projects[code] = project
        return project

    # -- 医师资质 --

    def register_physician(
        self, physician_id: str, name: str, home_institution_id: str, qualifications=()
    ) -> Physician:
        with self.store.locked():
            if home_institution_id not in self.store.institutions:
                raise NotFoundError(f"机构不存在：{home_institution_id}")
            physician = Physician(
                physician_id, name, home_institution_id, frozenset(qualifications)
            )
            self.store.physicians[physician_id] = physician
            return physician

    def update_qualifications(self, physician_id: str, add=(), remove=()) -> Physician:
        """维护医师资质；已冻结的资格快照不受影响。"""
        with self.store.locked():
            physician = self._physician(physician_id)
            qualifications = (set(physician.qualifications) | set(add)) - set(remove)
            updated = replace(physician, qualifications=frozenset(qualifications))
            self.store.physicians[physician_id] = updated
            return updated

    # -- 项目授权 --

    def grant_authorization(
        self,
        authorization_id: str,
        physician_id: str,
        institution_id: str,
        project_code: str,
        valid_from: datetime,
        valid_to: datetime,
        support: bool = False,
    ) -> Authorization:
        """授予项目授权；support=True 表示跨院支援授权。"""
        if valid_to <= valid_from:
            raise ValidationError("授权有效期结束必须晚于开始")
        with self.store.locked():
            self._physician(physician_id)
            if institution_id not in self.store.institutions:
                raise NotFoundError(f"机构不存在：{institution_id}")
            if project_code not in self.store.projects:
                raise NotFoundError(f"项目不存在：{project_code}")
            authorization = Authorization(
                authorization_id,
                physician_id,
                institution_id,
                project_code,
                valid_from,
                valid_to,
                support=support,
            )
            self.store.authorizations[authorization_id] = authorization
            return authorization

    def suspend_authorization(self, authorization_id: str) -> tuple[Authorization, list[str]]:
        """暂停授权：阻断新排班，并返回受影响的已确认班次。"""
        return self._set_authorization_status(authorization_id, AuthorizationStatus.SUSPENDED)

    def revoke_authorization(self, authorization_id: str) -> tuple[Authorization, list[str]]:
        """撤销授权：同暂停，但不可恢复。"""
        return self._set_authorization_status(authorization_id, AuthorizationStatus.REVOKED)

    def resume_authorization(self, authorization_id: str) -> Authorization:
        """恢复被暂停的授权。"""
        authorization, _ = self._set_authorization_status(
            authorization_id, AuthorizationStatus.ACTIVE
        )
        return authorization

    def _set_authorization_status(
        self, authorization_id: str, status: AuthorizationStatus
    ) -> tuple[Authorization, list[str]]:
        with self.store.locked():
            authorization = self.store.authorizations.get(authorization_id)
            if authorization is None:
                raise NotFoundError(f"授权不存在：{authorization_id}")
            updated = replace(authorization, status=status)
            self.store.authorizations[authorization_id] = updated
            affected = sorted(
                shift.shift_id
                for shift in self.store.shifts.values()
                if shift.status is ShiftStatus.CONFIRMED
                and (current := shift.current_assignment()) is not None
                and current.snapshot.authorization_id == authorization_id
            )
            return updated, affected

    # -- 可用时段 --

    def add_availability(
        self,
        slot_id: str,
        physician_id: str,
        institution_id: str,
        start: datetime,
        end: datetime,
    ) -> AvailabilitySlot:
        if end <= start:
            raise ValidationError("可用时段结束必须晚于开始")
        with self.store.locked():
            self._physician(physician_id)
            if institution_id not in self.store.institutions:
                raise NotFoundError(f"机构不存在：{institution_id}")
            slot = AvailabilitySlot(slot_id, physician_id, institution_id, start, end)
            self.store.availability[slot_id] = slot
            return slot

    def _physician(self, physician_id: str) -> Physician:
        physician = self.store.physicians.get(physician_id)
        if physician is None:
            raise NotFoundError(f"医师不存在：{physician_id}")
        return physician


class SchedulingService:
    """排班确认、临时替班、完成与取消。"""

    def __init__(self, store: Store, clock: Callable[[], datetime] | None = None) -> None:
        self.store = store
        self.clock = clock or _utcnow

    def create_shift(
        self,
        shift_id: str,
        institution_id: str,
        project_code: str,
        start: datetime,
        end: datetime,
        capacity: int = 1,
    ) -> Shift:
        if end <= start:
            raise ValidationError("班次结束时间必须晚于开始时间")
        if capacity < 1:
            raise ValidationError("预约容量至少为 1")
        with self.store.locked():
            if institution_id not in self.store.institutions:
                raise NotFoundError(f"机构不存在：{institution_id}")
            if project_code not in self.store.projects:
                raise NotFoundError(f"项目不存在：{project_code}")
            if shift_id in self.store.shifts:
                raise ConflictError(f"班次编号已存在：{shift_id}")
            shift = Shift(shift_id, institution_id, project_code, start, end, capacity=capacity)
            self.store.shifts[shift_id] = shift
            return shift

    def get_shift(self, shift_id: str) -> Shift:
        with self.store.locked():
            return self._shift(shift_id)

    def coverage(self, shift_id: str) -> CoverageReport:
        """解释班次的责任覆盖状态、缺口、候选替代与被拒原因。"""
        with self.store.locked():
            return evaluate_coverage(self.store, self._shift(shift_id))

    def confirm_shift(
        self, shift_id: str, physician_id: str, reason: str = "", expected_version: int | None = None
    ) -> Assignment:
        """确认排班：冻结资格快照并锁定责任覆盖。"""
        with self.store.locked():
            shift = self._shift(shift_id)
            self._check_version(shift, expected_version)
            if shift.status is not ShiftStatus.DRAFT:
                raise ConflictError(f"班次状态为 {shift.status.value}，不能确认")
            physician = self._physician(physician_id)
            report = evaluate_coverage(self.store, shift)
            if report.shift_gaps:
                raise CoverageError("班次存在覆盖缺口，无法确认", report)
            evaluation = evaluate_physician(self.store, shift, physician)
            if not evaluation.eligible:
                raise CoverageError(f"医师 {physician_id} 不符合该班次的资格要求", report)
            assignment = self._new_assignment(shift, physician, evaluation, reason)
            shift.assignments.append(assignment)
            shift.status = ShiftStatus.CONFIRMED
            shift.version += 1
            return assignment

    def substitute(
        self, shift_id: str, physician_id: str, reason: str = "", expected_version: int | None = None
    ) -> Assignment:
        """临时替班：替换当前责任人并保留完整指派历史。"""
        with self.store.locked():
            shift = self._shift(shift_id)
            self._check_version(shift, expected_version)
            if shift.status is ShiftStatus.COMPLETED:
                raise ConflictError("服务已完成，原责任人保留，不能替班")
            if shift.status is not ShiftStatus.CONFIRMED:
                raise ConflictError(f"班次状态为 {shift.status.value}，不能替班")
            current = shift.current_assignment()
            if current is None:
                raise ConflictError("班次缺少当前责任人")
            if current.physician_id == physician_id:
                raise ConflictError("替班医师与当前责任人相同")
            physician = self._physician(physician_id)
            evaluation = evaluate_physician(self.store, shift, physician)
            if not evaluation.eligible:
                raise CoverageError(
                    f"医师 {physician_id} 不符合替班资格要求",
                    evaluate_coverage(self.store, shift),
                )
            assignment = self._new_assignment(shift, physician, evaluation, reason)
            replaced = replace(
                current, status=AssignmentStatus.REPLACED, replaced_by=assignment.assignment_id
            )
            shift.assignments[shift.assignments.index(current)] = replaced
            shift.assignments.append(assignment)
            shift.version += 1
            return assignment

    def complete_shift(self, shift_id: str, expected_version: int | None = None) -> Shift:
        """完成服务：封存当前责任人，之后不可替班、不可取消。"""
        with self.store.locked():
            shift = self._shift(shift_id)
            self._check_version(shift, expected_version)
            if shift.status is not ShiftStatus.CONFIRMED:
                raise ConflictError(f"班次状态为 {shift.status.value}，不能完成")
            current = shift.current_assignment()
            if current is not None:
                sealed = replace(current, status=AssignmentStatus.COMPLETED)
                shift.assignments[shift.assignments.index(current)] = sealed
            shift.status = ShiftStatus.COMPLETED
            shift.version += 1
            return shift

    def cancel_shift(self, shift_id: str, expected_version: int | None = None) -> Shift:
        with self.store.locked():
            shift = self._shift(shift_id)
            self._check_version(shift, expected_version)
            if shift.status in (ShiftStatus.COMPLETED, ShiftStatus.CANCELLED):
                raise ConflictError(f"班次状态为 {shift.status.value}，不能取消")
            if shift.bookings:
                raise ConflictError("班次存在预约，不能取消")
            shift.status = ShiftStatus.CANCELLED
            shift.version += 1
            return shift

    def _new_assignment(
        self, shift: Shift, physician: Physician, evaluation, reason: str
    ) -> Assignment:
        now = self.clock()
        authorization = self.store.authorizations[evaluation.authorization_id]
        snapshot = QualificationSnapshot(
            physician_id=physician.physician_id,
            qualifications=tuple(sorted(physician.qualifications)),
            authorization_id=authorization.authorization_id,
            institution_id=shift.institution_id,
            project_code=shift.project_code,
            support=authorization.support,
            frozen_at=now,
        )
        return Assignment(
            assignment_id=self.store.next_id("asg"),
            shift_id=shift.shift_id,
            physician_id=physician.physician_id,
            snapshot=snapshot,
            status=AssignmentStatus.ACTIVE,
            reason=reason,
            created_at=now,
        )

    def _shift(self, shift_id: str) -> Shift:
        shift = self.store.shifts.get(shift_id)
        if shift is None:
            raise NotFoundError(f"班次不存在：{shift_id}")
        return shift

    def _physician(self, physician_id: str) -> Physician:
        physician = self.store.physicians.get(physician_id)
        if physician is None:
            raise NotFoundError(f"医师不存在：{physician_id}")
        return physician

    @staticmethod
    def _check_version(shift: Shift, expected_version: int | None) -> None:
        if expected_version is not None and expected_version != shift.version:
            raise ConflictError(
                f"班次版本冲突：期望 {expected_version}，当前 {shift.version}"
            )


class BookingService:
    """并发预约：容量检查与扣减在 Store 锁内原子完成。"""

    def __init__(self, store: Store, clock: Callable[[], datetime] | None = None) -> None:
        self.store = store
        self.clock = clock or _utcnow

    def book(self, shift_id: str, booking_id: str) -> dict:
        """原子预约：幂等重放返回原记录，容量不足抛出 CapacityError。"""
        if not booking_id:
            raise ValidationError("预约编号不能为空")
        with self.store.locked():
            shift = self.store.shifts.get(shift_id)
            if shift is None:
                raise NotFoundError(f"班次不存在：{shift_id}")
            if booking_id in shift.bookings:
                return self._record(shift, booking_id)  # 幂等重放
            if shift.status is not ShiftStatus.CONFIRMED:
                raise ConflictError(f"班次状态为 {shift.status.value}，不能预约")
            report = evaluate_coverage(self.store, shift)
            if report.status != "covered":
                raise CoverageError("责任覆盖存在风险，暂停接受预约", report)
            if len(shift.bookings) >= shift.capacity:
                raise CapacityError(
                    "预约容量已满",
                    {"capacity": shift.capacity, "booked": len(shift.bookings)},
                )
            shift.bookings.append(booking_id)
            shift.version += 1
            return self._record(shift, booking_id)

    def bookings(self, shift_id: str) -> list[str]:
        with self.store.locked():
            shift = self.store.shifts.get(shift_id)
            if shift is None:
                raise NotFoundError(f"班次不存在：{shift_id}")
            return list(shift.bookings)

    @staticmethod
    def _record(shift: Shift, booking_id: str) -> dict:
        return {
            "booking_id": booking_id,
            "shift_id": shift.shift_id,
            "seq": shift.bookings.index(booking_id) + 1,
            "remaining_capacity": shift.capacity - len(shift.bookings),
        }

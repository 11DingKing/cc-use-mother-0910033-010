"""排班领域服务：所有用例的事务边界与不变量执行点。"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from . import models
from .errors import ConflictError, NotFoundError, SchedulingError, ValidationError
from .models import (
    ASSIGN_ACTIVE,
    ASSIGN_CANCELLED,
    ASSIGN_PROPOSED,
    ASSIGN_REPLACED,
    AUTH_ACTIVE,
    AUTH_KIND_CROSS_SUPPORT,
    AUTH_KIND_INSTITUTION,
    AUTH_SUSPENDED,
    SESSION_CANCELLED,
    SESSION_COMPLETED,
    SESSION_CONFIRMED,
    SESSION_DRAFT,
    SNAPSHOT_CONFIRMATION,
    SNAPSHOT_SUBSTITUTION,
)
from .repository import Repository


def _reason(code: str, message: str, **extra: Any) -> dict[str, Any]:
    item = {"code": code, "message": message}
    item.update(extra)
    return item


class SchedulingService:
    def __init__(self, repository: Repository | None = None) -> None:
        self.repo = repository or Repository()

    # ============ 基础资料 ============
    def register_physician(self, physician_id: str, name: str) -> dict[str, Any]:
        with self.repo.transaction():
            if self.repo.get("physicians", physician_id) is not None:
                raise ConflictError("physician_exists", f"医师已存在：{physician_id}")
            doc = models.new_physician(physician_id, name)
            self.repo.put("physicians", physician_id, doc)
            return doc

    def register_organization(self, org_id: str, name: str) -> dict[str, Any]:
        with self.repo.transaction():
            if self.repo.get("organizations", org_id) is not None:
                raise ConflictError("organization_exists", f"机构已存在：{org_id}")
            doc = models.new_organization(org_id, name)
            self.repo.put("organizations", org_id, doc)
            return doc

    def add_qualification(
        self,
        physician_id: str,
        code: str,
        name: str,
        level: str | None = None,
        expires_at: str | None = None,
    ) -> dict[str, Any]:
        with self.repo.transaction():
            physician = self.repo.require("physicians", physician_id, "医师")
            physician["qualifications"][code] = {
                "name": name,
                "level": level,
                "expires_at": models.parse_time(expires_at) if expires_at else None,
            }
            self.repo.put("physicians", physician_id, physician)
            return physician["qualifications"][code]

    def set_work_rule(
        self,
        physician_id: str,
        max_consecutive_days: int | None = None,
        min_rest_hours: float | None = None,
    ) -> dict[str, Any]:
        with self.repo.transaction():
            physician = self.repo.require("physicians", physician_id, "医师")
            rule = physician["work_rule"] or {}
            if max_consecutive_days is not None:
                if max_consecutive_days < 1:
                    raise ValidationError("invalid_work_rule", "连续工作天数上限必须 >= 1")
                rule["max_consecutive_days"] = max_consecutive_days
            if min_rest_hours is not None:
                if min_rest_hours < 0:
                    raise ValidationError("invalid_work_rule", "班间休息小时数不能为负")
                rule["min_rest_hours"] = float(min_rest_hours)
            physician["work_rule"] = rule or None
            self.repo.put("physicians", physician_id, physician)
            return physician["work_rule"]

    def add_registration(
        self,
        physician_id: str,
        org_id: str,
        scopes: list[str],
        valid_from: str,
        valid_until: str | None = None,
    ) -> dict[str, Any]:
        with self.repo.transaction():
            self.repo.require("physicians", physician_id, "医师")
            self.repo.require("organizations", org_id, "机构")
            if not scopes:
                raise ValidationError("invalid_registration", "注册执业范围不能为空")
            start = models.parse_time(valid_from)
            end = models.parse_time(valid_until) if valid_until else None
            if end and end <= start:
                raise ValidationError("invalid_registration", "注册有效期结束必须晚于开始")
            key = f"{org_id}:{physician_id}"
            doc = models.new_registration(physician_id, org_id, scopes, start, end)
            self.repo.put("registrations", key, doc)
            return doc

    def register_project(
        self,
        project_id: str,
        org_id: str,
        name: str,
        risk_level: str,
        required_qualifications: list[str],
    ) -> dict[str, Any]:
        with self.repo.transaction():
            self.repo.require("organizations", org_id, "机构")
            if self.repo.get("projects", project_id) is not None:
                raise ConflictError("project_exists", f"项目已存在：{project_id}")
            if not required_qualifications:
                raise ValidationError("invalid_project", "高风险项目必须声明资格要求")
            if risk_level not in ("high", "medium", "low"):
                raise ValidationError("invalid_project", "风险等级非法")
            doc = models.new_project(
                project_id, org_id, name, risk_level, required_qualifications
            )
            self.repo.put("projects", project_id, doc)
            return doc

    # ============ 项目授权（含暂停/恢复）============
    def grant_authorization(
        self,
        project_id: str,
        physician_id: str,
        kind: str,
        valid_from: str,
        valid_until: str,
        source_org_id: str | None = None,
    ) -> dict[str, Any]:
        with self.repo.transaction():
            project = self.repo.require("projects", project_id, "项目")
            self.repo.require("physicians", physician_id, "医师")
            if kind not in (AUTH_KIND_INSTITUTION, AUTH_KIND_CROSS_SUPPORT):
                raise ValidationError("invalid_authorization", "授权类型非法")
            start, end = models.parse_time(valid_from), models.parse_time(valid_until)
            if end <= start:
                raise ValidationError("invalid_authorization", "授权结束必须晚于开始")
            if kind == AUTH_KIND_CROSS_SUPPORT:
                if not source_org_id:
                    raise ValidationError(
                        "invalid_authorization", "跨院支援授权必须注明来源机构"
                    )
                self.repo.require("organizations", source_org_id, "来源机构")
                if source_org_id == project["org_id"]:
                    raise ValidationError(
                        "invalid_authorization", "跨院支援的来源机构不能等于执业机构"
                    )
            elif source_org_id:
                raise ValidationError(
                    "invalid_authorization", "院内授权不得携带来源机构"
                )
            auth_id = self.repo.next_id("auth")
            doc = models.new_authorization(
                auth_id,
                project_id,
                physician_id,
                kind,
                project["org_id"],
                source_org_id,
                start,
                end,
            )
            self.repo.put("authorizations", auth_id, doc)
            return doc

    def suspend_authorization(self, auth_id: str, reason: str) -> dict[str, Any]:
        """暂停授权；返回受影响的已确认时段及其候选替代，便于合规处置。"""
        if not reason:
            raise ValidationError("invalid_suspension", "暂停必须说明原因")
        with self.repo.transaction():
            auth = self.repo.require("authorizations", auth_id, "授权")
            if auth["status"] == AUTH_SUSPENDED:
                raise ConflictError("authorization_not_active", "授权已处于暂停状态")
            auth["status"] = AUTH_SUSPENDED
            auth["suspend_reason"] = reason
            self.repo.put("authorizations", auth_id, auth)

            affected: list[dict[str, Any]] = []
            for assignment in self.repo.active_assignments_for(auth["physician_id"]):
                session = self.repo.get("sessions", assignment["session_id"])
                if (
                    session is None
                    or session["project_id"] != auth["project_id"]
                    or session["status"] != SESSION_CONFIRMED
                ):
                    continue
                coverage = self._coverage_report(session)
                affected.append(
                    {
                        "session_id": session["id"],
                        "start": session["start"],
                        "covered": coverage["covered"],
                        "candidates": coverage["candidates"],
                    }
                )
            return {"authorization": auth, "affected_sessions": affected}

    def resume_authorization(self, auth_id: str, valid_until: str | None = None) -> dict[str, Any]:
        with self.repo.transaction():
            auth = self.repo.require("authorizations", auth_id, "授权")
            if auth["status"] != AUTH_SUSPENDED:
                raise ConflictError("authorization_not_suspended", "授权未处于暂停状态")
            if valid_until:
                auth["valid_until"] = models.parse_time(valid_until)
            auth["status"] = AUTH_ACTIVE
            auth["suspend_reason"] = None
            self.repo.put("authorizations", auth_id, auth)
            return auth

    # ============ 服务时段 ============
    def create_session(
        self, project_id: str, start: str, end: str, capacity: int = 1
    ) -> dict[str, Any]:
        with self.repo.transaction():
            project = self.repo.require("projects", project_id, "项目")
            start_dt, end_dt = models.parse_time(start), models.parse_time(end)
            if end_dt <= start_dt:
                raise ValidationError("invalid_session", "时段结束必须晚于开始")
            if capacity < 1:
                raise ValidationError("invalid_session", "容量必须 >= 1")
            session_id = self.repo.next_id("sess")
            doc = models.new_session(session_id, project, start_dt, end_dt, capacity)
            self.repo.put("sessions", session_id, doc)
            return doc

    def get_session(self, session_id: str) -> dict[str, Any]:
        with self.repo.transaction():
            return self.repo.require("sessions", session_id, "服务时段")

    def list_sessions(self) -> list[dict[str, Any]]:
        with self.repo.transaction():
            docs = self.repo.find("sessions", lambda _: True)
        return sorted(docs, key=lambda d: d["start"])

    def cancel_session(self, session_id: str) -> dict[str, Any]:
        with self.repo.transaction():
            session = self._require_editable(session_id, (SESSION_DRAFT,))
            if self.repo.appointments_for_session(session_id):
                raise ConflictError("session_has_bookings", "已有预约，不能取消时段")
            session["status"] = SESSION_CANCELLED
            session["version"] += 1
            self.repo.put("sessions", session_id, session)
            return session

    # ============ 派班提议 / 取消提议 ============
    def propose_assignment(self, session_id: str, physician_id: str) -> dict[str, Any]:
        with self.repo.transaction():
            session = self._require_editable(session_id, (SESSION_DRAFT,))
            physician = self.repo.require("physicians", physician_id, "医师")
            self._reject_duplicate_assignment(session_id, physician_id)
            self._assert_can_serve(session, physician)
            assignment = models.new_assignment(
                self.repo.next_id("asg"),
                session_id,
                physician_id,
                ASSIGN_PROPOSED,
                None,
                models.now_utc(),
            )
            self.repo.put("assignments", assignment["id"], assignment)
            return assignment

    def cancel_proposal(self, session_id: str, assignment_id: str) -> dict[str, Any]:
        with self.repo.transaction():
            session = self._require_editable(session_id, (SESSION_DRAFT,))
            assignment = self.repo.require("assignments", assignment_id, "派班")
            if assignment["session_id"] != session_id:
                raise ValidationError("assignment_mismatch", "派班不属于该时段")
            if assignment["status"] != ASSIGN_PROPOSED:
                raise ConflictError("assignment_not_proposed", "只能取消待确认的派班提议")
            assignment["status"] = ASSIGN_CANCELLED
            assignment["replaced_at"] = models.now_utc()
            self.repo.put("assignments", assignment_id, assignment)
            return assignment

    # ============ 确认排班：资格快照 + 锁定责任覆盖 ============
    def confirm_session(
        self, session_id: str, expected_version: int | None = None
    ) -> dict[str, Any]:
        with self.repo.transaction():
            session = self.repo.require("sessions", session_id, "服务时段")
            self._check_version(session, expected_version)
            if session["status"] != SESSION_DRAFT:
                raise ConflictError("session_not_draft", "只有草稿时段可以确认")
            assignments = [
                a
                for a in self.repo.assignments_for_session(session_id)
                if a["status"] == ASSIGN_PROPOSED
            ]
            if not assignments:
                raise ConflictError(
                    "coverage_insufficient",
                    "没有任何派班提议，无法锁定责任覆盖",
                    details={"coverage": self._coverage_report(session)},
                )

            # 以确认时点的实时数据复验全部派班；先全部通过才允许冻结（原子提交）
            checked: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = []
            blocked: list[dict[str, Any]] = []
            for assignment in assignments:
                physician = self.repo.require(
                    "physicians", assignment["physician_id"], "医师"
                )
                evaluation = self._evaluate(session, physician)
                if not evaluation["eligible"]:
                    blocked.append(
                        {
                            "assignment_id": assignment["id"],
                            "physician_id": physician["id"],
                            "reasons": evaluation["reasons"],
                        }
                    )
                    continue
                checked.append((assignment, physician, evaluation))

            if blocked:
                raise ConflictError(
                    "coverage_insufficient",
                    "存在不合格派班，无法确认；请取消提议或改用候选替代",
                    reasons=[r for b in blocked for r in b["reasons"]],
                    details={
                        "blocked_assignments": blocked,
                        "coverage": self._coverage_report(session),
                    },
                )

            # 全部复验通过：冻结资格快照并锁定责任覆盖
            frozen: list[dict[str, Any]] = []
            for assignment, physician, evaluation in checked:
                snapshot = self._freeze_snapshot(
                    session, physician, evaluation, SNAPSHOT_CONFIRMATION
                )
                self.repo.put("snapshots", snapshot["id"], snapshot)
                assignment["status"] = ASSIGN_ACTIVE
                assignment["snapshot_id"] = snapshot["id"]
                frozen.append(
                    {
                        "assignment_id": assignment["id"],
                        "physician_id": physician["id"],
                        "snapshot_id": snapshot["id"],
                    }
                )
                self.repo.put("assignments", assignment["id"], assignment)

            session["status"] = SESSION_CONFIRMED
            session["version"] += 1
            self.repo.put("sessions", session_id, session)
            return {"session": session, "frozen": frozen}

    # ============ 临时替班 / 跨院支援 ============
    def substitute(
        self,
        session_id: str,
        physician_id: str,
        old_assignment_id: str | None = None,
    ) -> dict[str, Any]:
        with self.repo.transaction():
            session = self.repo.require("sessions", session_id, "服务时段")
            if session["status"] not in (SESSION_DRAFT, SESSION_CONFIRMED):
                raise ConflictError(
                    "session_locked",
                    "时段已完成或已取消，责任人按服务时的冻结快照保留",
                )
            physician = self.repo.require("physicians", physician_id, "医师")
            self._reject_duplicate_assignment(session_id, physician_id)

            candidates_status = (
                [ASSIGN_PROPOSED] if session["status"] == SESSION_DRAFT else [ASSIGN_ACTIVE]
            )
            prior = [
                a
                for a in self.repo.assignments_for_session(session_id)
                if a["status"] in candidates_status
            ]
            if old_assignment_id:
                old = next((a for a in prior if a["id"] == old_assignment_id), None)
                if old is None:
                    raise NotFoundError("assignment_not_found", "原派班不存在或已失效")
            elif len(prior) == 1:
                old = prior[0]
            else:
                raise ValidationError(
                    "ambiguous_assignment", "存在多个派班时必须指定 old_assignment_id"
                )

            # 已完成服务的原责任人不可被覆盖
            if self.repo.find_one(
                "service_records",
                lambda d, pid=old["physician_id"]: d["session_id"] == session_id
                and d["physician_id"] == pid,
            ):
                raise ConflictError(
                    "service_record_protected", "该医师已有完成记录，原责任人必须保留"
                )

            # 替班者按实时数据完整复验：资格、注册范围、授权窗口、工时、冲突
            self._assert_can_serve(session, physician)

            if session["status"] == SESSION_DRAFT:
                old["status"] = ASSIGN_CANCELLED
                old["replaced_at"] = models.now_utc()
                self.repo.put("assignments", old["id"], old)
                new_assignment = models.new_assignment(
                    self.repo.next_id("asg"),
                    session_id,
                    physician_id,
                    ASSIGN_PROPOSED,
                    None,
                    models.now_utc(),
                )
                self.repo.put("assignments", new_assignment["id"], new_assignment)
                return {"session": session, "assignment": new_assignment, "frozen": None}

            # 已确认时段：替班即时复验并冻结替班快照
            evaluation = self._evaluate(session, physician)
            snapshot = self._freeze_snapshot(
                session,
                physician,
                evaluation,
                SNAPSHOT_SUBSTITUTION,
                supersedes_snapshot_id=old["snapshot_id"],
            )
            self.repo.put("snapshots", snapshot["id"], snapshot)
            new_assignment = models.new_assignment(
                self.repo.next_id("asg"),
                session_id,
                physician_id,
                ASSIGN_ACTIVE,
                snapshot["id"],
                models.now_utc(),
            )
            self.repo.put("assignments", new_assignment["id"], new_assignment)
            old["status"] = ASSIGN_REPLACED
            old["replaced_at"] = models.now_utc()
            self.repo.put("assignments", old["id"], old)
            return {
                "session": session,
                "assignment": new_assignment,
                "frozen": {
                    "assignment_id": new_assignment["id"],
                    "snapshot_id": snapshot["id"],
                    "supersedes_snapshot_id": old["snapshot_id"],
                },
            }

    # ============ 覆盖解释与候选替代 ============
    def coverage_report(self, session_id: str) -> dict[str, Any]:
        with self.repo.transaction():
            session = self.repo.require("sessions", session_id, "服务时段")
            return self._coverage_report(session)

    def _coverage_report(self, session: dict[str, Any]) -> dict[str, Any]:
        assignments: list[dict[str, Any]] = []
        active_reasons: list[dict[str, Any]] = []
        for assignment in self.repo.assignments_for_session(session["id"]):
            if assignment["status"] not in (ASSIGN_PROPOSED, ASSIGN_ACTIVE):
                continue
            physician = self.repo.require("physicians", assignment["physician_id"], "医师")
            evaluation = self._evaluate(session, physician)
            entry = {
                "assignment_id": assignment["id"],
                "physician_id": physician["id"],
                "physician_name": physician["name"],
                "assignment_status": assignment["status"],
                "snapshot_id": assignment["snapshot_id"],
                "currently_eligible": evaluation["eligible"],
                "reasons": evaluation["reasons"],
            }
            assignments.append(entry)
            active_reasons.extend(evaluation["reasons"])

        candidates = self._find_candidates(session)
        eligible_candidates = [c for c in candidates if c["eligible"]]
        covered = any(a["currently_eligible"] for a in assignments)
        if assignments and not covered:
            headline = "现有派班均不满足资格/授权要求"
        elif not assignments:
            headline = "时段尚无派班"
        else:
            headline = "责任覆盖已满足"
        return {
            "session_id": session["id"],
            "status": session["status"],
            "risk_level": session["risk_level"],
            "required_qualifications": session["required_qualifications"],
            "covered": covered,
            "headline": headline,
            "reasons": active_reasons,
            "assignments": assignments,
            "candidate_count": len(eligible_candidates),
            "candidates": candidates,
        }

    def _find_candidates(self, session: dict[str, Any]) -> list[dict[str, Any]]:
        physicians = self.repo.find("physicians", lambda _: True)
        assigned = {
            a["physician_id"]
            for a in self.repo.assignments_for_session(session["id"])
            if a["status"] in (ASSIGN_PROPOSED, ASSIGN_ACTIVE)
        }
        candidates: list[dict[str, Any]] = []
        for physician in physicians:
            evaluation = self._evaluate(session, physician)
            blockers = evaluation["reasons"] + self._schedule_blockers(
                physician, session, exclude_session_id=session["id"]
            )
            chosen = evaluation["views"]["authorization"]
            candidates.append(
                {
                    "physician_id": physician["id"],
                    "physician_name": physician["name"],
                    "eligible": not blockers,
                    "already_assigned": physician["id"] in assigned,
                    "service_path": (chosen or {}).get("kind"),
                    "blockers": blockers,
                }
            )
        candidates.sort(key=lambda c: (not c["eligible"], c["physician_id"]))
        return candidates

    # ============ 完成服务：原责任人留痕 ============
    def complete_session(
        self, session_id: str, records: list[dict[str, str]] | None = None
    ) -> dict[str, Any]:
        with self.repo.transaction():
            session = self.repo.require("sessions", session_id, "服务时段")
            if session["status"] != SESSION_CONFIRMED:
                raise ConflictError("session_not_confirmed", "只有已确认时段可以登记完成")
            active = [
                a
                for a in self.repo.assignments_for_session(session_id)
                if a["status"] == ASSIGN_ACTIVE
            ]
            if not active:
                raise ConflictError("coverage_insufficient", "没有在岗责任人，无法完成")

            by_id = {a["id"]: a for a in active}
            by_physician = {a["physician_id"]: a for a in active}
            created: list[dict[str, Any]] = []
            requested = records or [
                {"assignment_id": a["id"], "label": "主诊责任"} for a in active
            ]
            for item in requested:
                assignment = None
                if item.get("assignment_id"):
                    assignment = by_id.get(item["assignment_id"])
                elif item.get("physician_id"):
                    assignment = by_physician.get(item["physician_id"])
                elif len(active) == 1:
                    assignment = active[0]
                if assignment is None:
                    raise ValidationError("record_target_invalid", "完成记录对应的责任人不存在")
                snapshot = self.repo.require(
                    "snapshots", assignment["snapshot_id"], "资格快照"
                )
                record = models.new_service_record(
                    self.repo.next_id("svc"),
                    session,
                    assignment,
                    snapshot,
                    item.get("label", "主诊责任"),
                    models.now_utc(),
                )
                self.repo.put("service_records", record["id"], record)
                created.append(record)

            session["status"] = SESSION_COMPLETED
            session["version"] += 1
            self.repo.put("sessions", session_id, session)
            return {"session": session, "service_records": created}

    def list_service_records(self, session_id: str | None = None) -> list[dict[str, Any]]:
        with self.repo.transaction():
            if session_id:
                docs = self.repo.find(
                    "service_records", lambda d: d["session_id"] == session_id
                )
            else:
                docs = self.repo.find("service_records", lambda _: True)
        return sorted(docs, key=lambda d: (d["session_id"], d["completed_at"]))

    def get_snapshot(self, snapshot_id: str) -> dict[str, Any]:
        with self.repo.transaction():
            return self.repo.require("snapshots", snapshot_id, "资格快照")

    # ============ 并发预约：原子校验+写入 ============
    def book_appointment(
        self,
        session_id: str,
        patient_ref: str,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        if not patient_ref:
            raise ValidationError("invalid_appointment", "缺少患者标识")
        with self.repo.transaction():
            session = self.repo.require("sessions", session_id, "服务时段")
            self._check_version(session, expected_version)
            if session["status"] == SESSION_DRAFT:
                raise ConflictError("session_not_confirmed", "排班尚未确认，暂不接受预约")
            if session["status"] in (SESSION_COMPLETED, SESSION_CANCELLED):
                raise ConflictError("session_closed", "时段已结束或取消")

            if self.repo.has_appointment(session_id, patient_ref):
                raise ConflictError("duplicate_appointment", "该患者在本时段已有有效预约")
            if session["booked_count"] >= session["capacity"]:
                raise ConflictError(
                    "capacity_full",
                    "时段容量已满",
                    details={
                        "capacity": session["capacity"],
                        "booked_count": session["booked_count"],
                    },
                )

            # 预约时按实时数据复验责任覆盖：冻结快照留痕，但暂停授权后不得新增占坑预约
            coverage = self._coverage_report(session)
            if not coverage["covered"]:
                raise ConflictError(
                    "coverage_insufficient",
                    "责任覆盖不足，不能接受预约；请等待替班或改约其他时段",
                    reasons=coverage["reasons"],
                    details={"coverage": coverage},
                )

            appointment = models.new_appointment(
                self.repo.next_id("appt"), session_id, patient_ref, models.now_utc()
            )
            self.repo.put("appointments", appointment["id"], appointment)
            session["booked_count"] += 1
            session["version"] += 1
            self.repo.put("sessions", session_id, session)
            return {
                "appointment": appointment,
                "session_version": session["version"],
                "remaining_capacity": session["capacity"] - session["booked_count"],
            }

    def cancel_appointment(self, appointment_id: str) -> dict[str, Any]:
        with self.repo.transaction():
            appointment = self.repo.require("appointments", appointment_id, "预约")
            if appointment["status"] != models.APPT_BOOKED:
                raise ConflictError("appointment_not_booked", "预约已取消")
            session = self.repo.require("sessions", appointment["session_id"], "服务时段")
            appointment["status"] = models.APPT_CANCELLED
            self.repo.put("appointments", appointment_id, appointment)
            session["booked_count"] = max(0, session["booked_count"] - 1)
            session["version"] += 1
            self.repo.put("sessions", session["id"], session)
            return appointment

    # ============ 资格评估 ============
    def _assert_can_serve(
        self, session: dict[str, Any], physician: dict[str, Any]
    ) -> None:
        evaluation = self._evaluate(session, physician)
        blockers = evaluation["reasons"] + self._schedule_blockers(
            physician, session, exclude_session_id=session["id"]
        )
        if blockers:
            coverage = self._coverage_report(session)
            raise ConflictError(
                "physician_not_eligible",
                f"医师 {physician['name']} 不能承担该时段",
                reasons=blockers,
                details={"coverage": coverage},
            )

    def _evaluate(
        self, session: dict[str, Any], physician: dict[str, Any]
    ) -> dict[str, Any]:
        """资格/注册/授权三维评估，返回结构化原因与可冻结视图。"""
        at = session["start"]
        required = session["required_qualifications"]
        reasons: list[dict[str, Any]] = []

        # 1) 执业资格
        qual_view = {
            code: dict(detail) for code, detail in physician["qualifications"].items()
        }
        for code in required:
            detail = physician["qualifications"].get(code)
            if detail is None:
                reasons.append(
                    _reason(
                        "qualification_missing",
                        f"缺少资格：{code}",
                        qualification=code,
                    )
                )
            elif detail["expires_at"] is not None and detail["expires_at"] < at:
                reasons.append(
                    _reason(
                        "qualification_expired",
                        f"资格已过期：{code}（到期 {detail['expires_at'].date()}）",
                        qualification=code,
                    )
                )

        # 2)+3) 项目授权及授权路径上的机构注册
        auths = self.repo.authorizations_for_project(session["project_id"], physician["id"])
        chosen_auth: dict[str, Any] | None = None
        chosen_reg: dict[str, Any] | None = None
        if not auths:
            reasons.append(_reason("authorization_missing", "没有该项目的授权记录"))
        else:
            path_failures: list[dict[str, Any]] = []
            for auth in auths:
                if auth["status"] == AUTH_SUSPENDED:
                    path_failures.append(
                        _reason(
                            "authorization_suspended",
                            f"授权 {auth['id']} 已暂停"
                            + (f"：{auth['suspend_reason']}" if auth["suspend_reason"] else ""),
                            authorization_id=auth["id"],
                        )
                    )
                    continue
                if not (auth["valid_from"] <= at <= auth["valid_until"]):
                    path_failures.append(
                        _reason(
                            "authorization_outside_window",
                            f"授权 {auth['id']} 不在有效期内（{auth['valid_from'].date()} ~ "
                            f"{auth['valid_until'].date()}）",
                            authorization_id=auth["id"],
                        )
                    )
                    continue
                reg, reg_reasons = self._registration_for(auth, session, required, at)
                if reg is None:
                    path_failures.extend(reg_reasons)
                    continue
                chosen_auth, chosen_reg = auth, reg
                break
            if chosen_auth is None:
                reasons.extend(self._dedupe_reasons(path_failures))

        views = {
            "qualifications": qual_view,
            "registration": chosen_reg,
            "authorization": chosen_auth,
        }
        return {"eligible": not reasons, "reasons": reasons, "views": views}

    def _registration_for(
        self,
        auth: dict[str, Any],
        session: dict[str, Any],
        required: list[str],
        at,
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        """按授权路径核验机构注册：院内=执业机构注册；跨院=来源机构注册。"""
        if auth["kind"] == AUTH_KIND_CROSS_SUPPORT:
            org_id = auth["source_org_id"]
            role = "cross_support_source"
            missing_code = "source_registration_missing"
            window_code = "source_registration_outside_window"
            scope_code = "source_scope_exceeds"
        else:
            org_id = auth["host_org_id"]
            role = "host"
            missing_code = "registration_missing"
            window_code = "registration_outside_window"
            scope_code = "scope_exceeds"

        regs = [
            r
            for r in self.repo.registrations_for(auth["physician_id"])
            if r["org_id"] == org_id and r["status"] == "active"
        ]
        if not regs:
            return None, [
                _reason(
                    missing_code,
                    f"在机构 {org_id} 没有执业注册",
                    org_id=org_id,
                )
            ]
        in_window = [
            r
            for r in regs
            if r["valid_from"] <= at and (r["valid_until"] is None or r["valid_until"] >= at)
        ]
        if not in_window:
            closest = min(regs, key=lambda r: abs((r["valid_from"] - at).total_seconds()))
            return None, [
                _reason(
                    window_code,
                    f"机构 {org_id} 的注册不在有效期内"
                    f"（{closest['valid_from'].date()} ~ "
                    f"{closest['valid_until'].date() if closest['valid_until'] else '长期'}）",
                    org_id=org_id,
                )
            ]
        scopes = {scope for r in in_window for scope in r["scopes"]}
        out_of_scope = [code for code in required if code not in scopes]
        if out_of_scope:
            return None, [
                _reason(
                    scope_code,
                    f"注册执业范围不含：{'、'.join(out_of_scope)}（简单换班将超范围执业）",
                    org_id=org_id,
                    qualifications=out_of_scope,
                )
            ]
        reg = dict(in_window[0])
        reg["role"] = role
        return reg, []

    def _schedule_blockers(
        self,
        physician: dict[str, Any],
        session: dict[str, Any],
        exclude_session_id: str | None,
    ) -> list[dict[str, Any]]:
        """时间冲突与连续工作/班间休息限制。"""
        reasons: list[dict[str, Any]] = []
        others = [
            s
            for s in self.repo.sessions_for_physician(
                physician["id"], (SESSION_DRAFT, SESSION_CONFIRMED)
            )
            if s["id"] != exclude_session_id
        ]
        for other in others:
            if session["start"] < other["end"] and other["start"] < session["end"]:
                reasons.append(
                    _reason(
                        "time_conflict",
                        f"与时段 {other['id']}（{other['start'].isoformat()} ~ "
                        f"{other['end'].isoformat()}）时间重叠",
                        conflicting_session_id=other["id"],
                    )
                )

        rule = physician.get("work_rule") or {}
        min_rest = rule.get("min_rest_hours")
        if min_rest is not None and others:
            prior_gap = min(
                (session["start"] - o["end"] for o in others if o["end"] <= session["start"]),
                default=None,
            )
            next_gap = min(
                (o["start"] - session["end"] for o in others if o["start"] >= session["end"]),
                default=None,
            )
            gap = min((g for g in (prior_gap, next_gap) if g is not None), default=None)
            if gap is not None and gap < timedelta(hours=float(min_rest)):
                reasons.append(
                    _reason(
                        "insufficient_rest",
                        f"班间休息不足：最近间隔 {gap.total_seconds() / 3600:.1f} 小时，"
                        f"要求至少 {min_rest} 小时",
                        min_rest_hours=min_rest,
                    )
                )

        max_days = rule.get("max_consecutive_days")
        if max_days is not None:
            tz = session["start"].tzinfo
            work_dates = {
                o["start"].astimezone(tz).date()
                for o in others
            }
            target_date = session["start"].astimezone(tz).date()
            streak = 0
            cursor = target_date - timedelta(days=1)
            while cursor in work_dates:
                streak += 1
                cursor -= timedelta(days=1)
            if streak + 1 > int(max_days):
                reasons.append(
                    _reason(
                        "consecutive_days_exceeded",
                        f"连续工作将达 {streak + 1} 天，超过上限 {max_days} 天",
                        max_consecutive_days=max_days,
                        current_streak=streak,
                    )
                )
        return reasons

    # ============ 快照 ============
    def _freeze_snapshot(
        self,
        session: dict[str, Any],
        physician: dict[str, Any],
        evaluation: dict[str, Any],
        kind: str,
        supersedes_snapshot_id: str | None = None,
    ) -> dict[str, Any]:
        snapshot = models.new_snapshot(
            self.repo.next_id("snap"),
            session,
            physician,
            kind,
            models.now_utc(),
            evaluation["views"]["qualifications"],
            evaluation["views"]["registration"],
            evaluation["views"]["authorization"],
        )
        if supersedes_snapshot_id:
            snapshot["supersedes_snapshot_id"] = supersedes_snapshot_id
        return snapshot

    # ============ 辅助 ============
    def _require_editable(
        self, session_id: str, allowed: tuple[str, ...]
    ) -> dict[str, Any]:
        session = self.repo.require("sessions", session_id, "服务时段")
        if session["status"] not in allowed:
            label = {
                SESSION_DRAFT: "草稿",
                SESSION_CONFIRMED: "已确认",
                SESSION_COMPLETED: "已完成",
                SESSION_CANCELLED: "已取消",
            }[session["status"]]
            raise ConflictError(
                "session_not_editable",
                f"时段当前状态为{label}，该操作不被允许",
            )
        return session

    def _reject_duplicate_assignment(self, session_id: str, physician_id: str) -> None:
        for assignment in self.repo.assignments_for_session(session_id):
            if (
                assignment["physician_id"] == physician_id
                and assignment["status"] in (ASSIGN_PROPOSED, ASSIGN_ACTIVE)
            ):
                raise ConflictError(
                    "duplicate_assignment", f"医师 {physician_id} 已在该时段派班"
                )

    @staticmethod
    def _check_version(session: dict[str, Any], expected_version: int | None) -> None:
        if expected_version is not None and session["version"] != expected_version:
            raise ConflictError(
                "version_conflict",
                f"时段版本已变化：期望 {expected_version}，当前 {session['version']}",
                details={
                    "expected_version": expected_version,
                    "current_version": session["version"],
                },
            )

    @staticmethod
    def _dedupe_reasons(reasons: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: set[tuple] = set()
        result: list[dict[str, Any]] = []
        for reason in reasons:
            key = (reason["code"], reason["message"])
            if key not in seen:
                seen.add(key)
                result.append(reason)
        return result

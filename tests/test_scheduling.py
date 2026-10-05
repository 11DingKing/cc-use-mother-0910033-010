"""排班服务端回归测试：覆盖契约四项不变量与主要业务路径。"""
from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from scheduling import SchedulingService, create_server
from scheduling.errors import ConflictError
from scheduling.models import (
    ASSIGN_ACTIVE,
    SESSION_COMPLETED,
    SESSION_CONFIRMED,
    SNAPSHOT_CONFIRMATION,
    SNAPSHOT_SUBSTITUTION,
)

TZ = timezone(timedelta(hours= 8))


BASE_DAY = datetime(2026, 11, 1, tzinfo=TZ)


def iso(dt: datetime) -> str:
    return dt.isoformat()


def day_start(offset: int, hour: int = 9) -> datetime:
    return (BASE_DAY + timedelta(days=offset)).replace(hour=hour)


class ServiceTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = SchedulingService()
        self._seed()

    def _seed(self) -> None:
        s = self.svc
        s.register_organization("H1", "第一医院")
        s.register_organization("H2", "第二医院")
        s.register_physician("P1", "张医生")
        s.register_physician("P2", "李医生")
        s.register_physician("P3", "王医生")
        for pid, codes in (
            ("P1", ["CARDIO_INTV", "HIGH_RISK"]),
            ("P2", ["HIGH_RISK"]),
            ("P3", ["CARDIO_INTV", "HIGH_RISK"]),
        ):
            for code in codes:
                s.add_qualification(pid, code, code)
        # P1/P2 在 H1 注册，P3 在 H2 注册（跨院支援来源）
        s.add_registration("P1", "H1", ["CARDIO_INTV", "HIGH_RISK"], iso(day_start(-365)))
        s.add_registration("P2", "H1", ["HIGH_RISK"], iso(day_start(-365)))
        s.add_registration("P3", "H2", ["CARDIO_INTV", "HIGH_RISK"], iso(day_start(-365)))
        s.register_project(
            "PRJ1", "H1", "高危心血管介入", "high", ["CARDIO_INTV", "HIGH_RISK"]
        )
        window_from, window_until = iso(day_start(-1)), iso(day_start(60))
        s.grant_authorization("PRJ1", "P1", "institution", window_from, window_until)
        s.grant_authorization(
            "PRJ1", "P3", "cross_support", window_from, window_until,
            source_org_id="H2",
        )
        # P3 的跨院授权
        self.session_start = day_start(7)
        self.session_end = day_start(7, 12)
        self.session = s.create_session(
            "PRJ1", iso(self.session_start), iso(self.session_end), capacity=2
        )
        self.sid = self.session["id"]


class QualificationAndRegistrationTest(ServiceTestBase):
    def test_missing_qualification_blocks_proposal(self) -> None:
        # P2 缺少 CARDIO_INTV
        with self.assertRaises(ConflictError) as ctx:
            self.svc.propose_assignment(self.sid, "P2")
        self.assertEqual(ctx.exception.code, "physician_not_eligible")
        codes = {r["code"] for r in ctx.exception.reasons}
        self.assertIn("qualification_missing", codes)
        # 错误详情同时给出候选替代
        candidates = {
            c["physician_id"]: c for c in ctx.exception.details["coverage"]["candidates"]
        }
        self.assertTrue(candidates["P1"]["eligible"])
        self.assertTrue(candidates["P3"]["eligible"])
        self.assertFalse(candidates["P2"]["eligible"])

    def test_simple_swap_blocked_when_out_of_registration_scope(self) -> None:
        # P2 在 H1 的注册范围仅 HIGH_RISK，换班即超范围
        self.svc.propose_assignment(self.sid, "P1")
        self.svc.confirm_session(self.sid)
        with self.assertRaises(ConflictError) as ctx:
            self.svc.substitute(self.sid, "P2")
        codes = {r["code"] for r in ctx.exception.reasons}
        self.assertIn("qualification_missing", codes)

    def test_expired_qualification_blocks(self) -> None:
        self.svc.add_qualification(
            "P2", "CARDIO_INTV", "介入", expires_at=iso(day_start(6))
        )
        self.svc.add_registration(
            "P2", "H1", ["CARDIO_INTV", "HIGH_RISK"], iso(day_start(-365))
        )
        # 资格在时段开始前一天到期
        with self.assertRaises(ConflictError) as ctx:
            self.svc.propose_assignment(self.sid, "P2")
        self.assertIn("qualification_expired", {r["code"] for r in ctx.exception.reasons})


class SnapshotAndCoverageTest(ServiceTestBase):
    def test_confirm_freezes_qualification_snapshot(self) -> None:
        self.svc.propose_assignment(self.sid, "P1")
        result = self.svc.confirm_session(self.sid)
        self.assertEqual(result["session"]["status"], SESSION_CONFIRMED)
        snapshot_id = result["frozen"][0]["snapshot_id"]
        snap = self.svc.get_snapshot(snapshot_id)
        self.assertEqual(snap["kind"], SNAPSHOT_CONFIRMATION)
        self.assertEqual(snap["physician_id"], "P1")
        self.assertEqual(snap["authorization"]["kind"], "institution")
        self.assertEqual(
            set(snap["required_qualifications"]), {"CARDIO_INTV", "HIGH_RISK"}
        )

        # 确认后删除/变更实时资质，快照与已确认覆盖不受影响
        physician = self.svc.repo.require("physicians", "P1", "医师")
        physician["qualifications"] = {}
        self.svc.repo.put("physicians", "P1", physician)
        report = self.svc.coverage_report(self.sid)
        # 快照仍锁责；实时资格复验会标记当前已不满足
        assignment = report["assignments"][0]
        self.assertEqual(assignment["snapshot_id"], snapshot_id)
        self.assertFalse(assignment["currently_eligible"])
        self.assertTrue(
            any(r["code"] == "qualification_missing" for r in assignment["reasons"])
        )

    def test_confirm_without_coverage_explains_shortage(self) -> None:
        with self.assertRaises(ConflictError) as ctx:
            self.svc.confirm_session(self.sid)
        self.assertEqual(ctx.exception.code, "coverage_insufficient")
        self.assertIn("coverage", ctx.exception.details)
        self.assertFalse(ctx.exception.details["coverage"]["covered"])

    def test_confirm_rechecks_every_proposal(self) -> None:
        # 提议时两名医师均合格；确认前暂停 P3 的跨院授权，复验必须拦下
        self.svc.propose_assignment(self.sid, "P1")
        self.svc.propose_assignment(self.sid, "P3")
        auth_p3 = self.svc.repo.find_one(
            "authorizations",
            lambda d: d["physician_id"] == "P3" and d["project_id"] == "PRJ1",
        )
        self.svc.suspend_authorization(auth_p3["id"], "确认前临时暂停")
        with self.assertRaises(ConflictError) as ctx:
            self.svc.confirm_session(self.sid)
        blocked = ctx.exception.details["blocked_assignments"]
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0]["physician_id"], "P3")
        self.assertIn(
            "authorization_suspended",
            {r["code"] for r in blocked[0]["reasons"]},
        )
        # 复验失败不应产生任何快照，时段仍为草稿
        self.assertEqual(self.svc.repo.find("snapshots", lambda _: True), [])
        self.assertEqual(self.svc.get_session(self.sid)["status"], "draft")


class CrossSupportTest(ServiceTestBase):
    def test_cross_support_requires_source_registration(self) -> None:
        # P3 持 H2 注册与跨院授权，可在 H1 承担
        self.svc.propose_assignment(self.sid, "P3")
        result = self.svc.confirm_session(self.sid)
        snap = self.svc.get_snapshot(result["frozen"][0]["snapshot_id"])
        self.assertEqual(snap["authorization"]["kind"], "cross_support")
        self.assertEqual(snap["registration"]["org_id"], "H2")
        self.assertEqual(snap["registration"]["role"], "cross_support_source")

    def test_cross_support_without_source_registration_rejected(self) -> None:
        self.svc.register_physician("P4", "赵医生")
        self.svc.add_qualification("P4", "CARDIO_INTV", "介入")
        self.svc.add_qualification("P4", "HIGH_RISK", "高风险")
        self.svc.grant_authorization(
            "PRJ1", "P4", "cross_support", iso(day_start(-1)), iso(day_start(60)),
            source_org_id="H2",
        )
        # 缺少 H2 注册
        with self.assertRaises(ConflictError) as ctx:
            self.svc.propose_assignment(self.sid, "P4")
        self.assertIn(
            "source_registration_missing", {r["code"] for r in ctx.exception.reasons}
        )

    def test_institution_auth_does_not_cover_cross_org(self) -> None:
        # P1 只有 H1 院内注册与授权；为 H2 项目换班时不成立（无 H2 路径）
        self.svc.register_project(
            "PRJ2", "H2", "H2 高危项目", "high", ["CARDIO_INTV", "HIGH_RISK"]
        )
        s2 = self.svc.create_session("PRJ2", iso(day_start(8, 9)), iso(day_start(8, 12)))
        with self.assertRaises(ConflictError) as ctx:
            self.svc.propose_assignment(s2["id"], "P1")
        codes = {r["code"] for r in ctx.exception.reasons}
        self.assertIn("authorization_missing", codes)


class SuspensionAndSubstitutionTest(ServiceTestBase):
    def _confirmed_with_p1(self):
        self.svc.propose_assignment(self.sid, "P1")
        self.svc.confirm_session(self.sid)
        auth = self.svc.repo.find_one(
            "authorizations",
            lambda d: d["physician_id"] == "P1" and d["project_id"] == "PRJ1",
        )
        return auth["id"]

    def test_suspend_reports_affected_session_and_candidates(self) -> None:
        auth_id = self._confirmed_with_p1()
        result = self.svc.suspend_authorization(auth_id, "资质复核暂停")
        affected = result["affected_sessions"]
        self.assertEqual(len(affected), 1)
        self.assertEqual(affected[0]["session_id"], self.sid)
        self.assertFalse(affected[0]["covered"])
        candidate_ids = {c["physician_id"] for c in affected[0]["candidates"] if c["eligible"]}
        self.assertEqual(candidate_ids, {"P3"})

    def test_booking_blocked_after_suspension_until_substitute(self) -> None:
        auth_id = self._confirmed_with_p1()
        self.svc.suspend_authorization(auth_id, "暂停")
        with self.assertRaises(ConflictError) as ctx:
            self.svc.book_appointment(self.sid, "patient-1")
        self.assertEqual(ctx.exception.code, "coverage_insufficient")

        # 跨院支援医师 P3 替班，重新锁定覆盖后可预约
        result = self.svc.substitute(self.sid, "P3")
        self.assertEqual(result["frozen"]["supersedes_snapshot_id"][:5], "snap_")
        new_snap = self.svc.get_snapshot(result["frozen"]["snapshot_id"])
        self.assertEqual(new_snap["kind"], SNAPSHOT_SUBSTITUTION)
        self.assertEqual(new_snap["physician_id"], "P3")
        booked = self.svc.book_appointment(self.sid, "patient-1")
        self.assertEqual(booked["remaining_capacity"], 1)

    def test_substitution_rejected_when_candidate_ineligible(self) -> None:
        self._confirmed_with_p1()
        with self.assertRaises(ConflictError):
            self.svc.substitute(self.sid, "P2")

    def test_completed_service_keeps_original_responsible(self) -> None:
        self._confirmed_with_p1()
        self.svc.complete_session(self.sid)
        records = self.svc.list_service_records(self.sid)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["physician_id"], "P1")

        # 完成后禁止替班：责任人按服务时快照永久保留
        with self.assertRaises(ConflictError) as ctx:
            self.svc.substitute(self.sid, "P3")
        self.assertEqual(ctx.exception.code, "session_locked")

        # 即便后来暂停 P1 授权，记录仍指向 P1 与当时快照
        auth_id = self.svc.repo.find_one(
            "authorizations", lambda d: d["physician_id"] == "P1"
        )["id"]
        self.svc.suspend_authorization(auth_id, "事后暂停不改变历史责任")
        records = self.svc.list_service_records(self.sid)
        self.assertEqual(records[0]["physician_id"], "P1")
        snap = self.svc.get_snapshot(records[0]["snapshot_id"])
        self.assertEqual(snap["physician_name"], "张医生")


class WorkRuleTest(ServiceTestBase):
    def test_consecutive_days_limit(self) -> None:
        self.svc.set_work_rule("P1", max_consecutive_days=2)
        for offset in range(0, 2):
            sess = self.svc.create_session(
                "PRJ1", iso(day_start(offset)), iso(day_start(offset, 12))
            )
            self.svc.propose_assignment(sess["id"], "P1")
            self.svc.confirm_session(sess["id"])
        third = self.svc.create_session(
            "PRJ1", iso(day_start(2)), iso(day_start(2, 12))
        )
        with self.assertRaises(ConflictError) as ctx:
            self.svc.propose_assignment(third["id"], "P1")
        self.assertIn(
            "consecutive_days_exceeded", {r["code"] for r in ctx.exception.reasons}
        )

    def test_min_rest_hours(self) -> None:
        self.svc.set_work_rule("P1", min_rest_hours=12)
        s1 = self.svc.create_session(
            "PRJ1", iso(day_start(0, 9)), iso(day_start(0, 18))
        )
        self.svc.propose_assignment(s1["id"], "P1")
        s2 = self.svc.create_session(
            "PRJ1", iso(day_start(1, 9)), iso(day_start(1, 12))
        )
        # 间隔 15 小时，通过
        self.svc.propose_assignment(s2["id"], "P1")
        s3 = self.svc.create_session(
            "PRJ1", iso(day_start(1, 20)), iso(day_start(1, 23))
        )
        with self.assertRaises(ConflictError) as ctx:
            self.svc.propose_assignment(s3["id"], "P1")
        self.assertIn("insufficient_rest", {r["code"] for r in ctx.exception.reasons})

    def test_time_conflict(self) -> None:
        s1 = self.svc.create_session(
            "PRJ1", iso(day_start(0, 9)), iso(day_start(0, 12))
        )
        self.svc.propose_assignment(s1["id"], "P1")
        s2 = self.svc.create_session(
            "PRJ1", iso(day_start(0, 11)), iso(day_start(0, 14))
        )
        with self.assertRaises(ConflictError) as ctx:
            self.svc.propose_assignment(s2["id"], "P1")
        self.assertIn("time_conflict", {r["code"] for r in ctx.exception.reasons})


class AppointmentConcurrencyTest(ServiceTestBase):
    def _ready(self, capacity: int = 1):
        sess = self.svc.create_session(
            "PRJ1", iso(day_start(20)), iso(day_start(20, 12)), capacity=capacity
        )
        self.svc.propose_assignment(sess["id"], "P1")
        self.svc.confirm_session(sess["id"])
        return sess["id"]

    def test_capacity_and_duplicate(self) -> None:
        sid = self._ready(capacity=1)
        self.svc.book_appointment(sid, "A")
        with self.assertRaises(ConflictError) as ctx:
            self.svc.book_appointment(sid, "B")
        self.assertEqual(ctx.exception.code, "capacity_full")
        with self.assertRaises(ConflictError) as ctx:
            self.svc.book_appointment(sid, "A")
        self.assertEqual(ctx.exception.code, "duplicate_appointment")

    def test_concurrent_booking_is_atomic(self) -> None:
        sid = self._ready(capacity=5)
        results: list[str] = []
        barrier = threading.Barrier(8)

        def book(patient: str) -> None:
            barrier.wait()
            try:
                self.svc.book_appointment(sid, patient)
                results.append("ok")
            except ConflictError as exc:
                results.append(exc.code)

        threads = [threading.Thread(target=book, args=(f"pat-{i}",)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(results.count("ok"), 5)
        self.assertEqual(results.count("capacity_full"), 3)
        session = self.svc.get_session(sid)
        self.assertEqual(session["booked_count"], 5)

    def test_optimistic_version_conflict(self) -> None:
        sid = self._ready(capacity=5)
        self.svc.book_appointment(sid, "first")  # version 已推进
        with self.assertRaises(ConflictError) as ctx:
            self.svc.book_appointment(sid, "second", expected_version=2)
        self.assertEqual(ctx.exception.code, "version_conflict")

    def test_cancel_frees_capacity(self) -> None:
        sid = self._ready(capacity=1)
        appt = self.svc.book_appointment(sid, "A")["appointment"]
        self.svc.cancel_appointment(appt["id"])
        again = self.svc.book_appointment(sid, "B")
        self.assertEqual(again["remaining_capacity"], 0)

    def test_draft_session_rejects_booking(self) -> None:
        with self.assertRaises(ConflictError) as ctx:
            self.svc.book_appointment(self.sid, "X")
        self.assertEqual(ctx.exception.code, "session_not_confirmed")


class HttpApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.httpd = create_server("127.0.0.1", 0)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)

    def _req(self, method: str, path: str, body: dict | None = None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())

    def test_full_flow_over_http(self) -> None:
        status, body = self._req("GET", "/health")
        self.assertEqual((status, body["data"]["status"]), (200, "ok"))

        self._req("POST", "/organizations", {"id": "HH1", "name": "医院"})
        self._req("POST", "/physicians", {"id": "D1", "name": "甲"})
        self._req("POST", "/physicians", {"id": "D2", "name": "乙"})
        self._req("POST", "/physicians/D1/qualifications", {"code": "Q1", "name": "资格一"})
        self._req("POST", "/physicians/D2/qualifications", {"code": "Q2", "name": "资格二"})
        self._req(
            "POST", "/registrations",
            {"physician_id": "D1", "org_id": "HH1", "scopes": ["Q1"],
             "valid_from": iso(day_start(-10))},
        )
        self._req(
            "POST", "/projects",
            {"id": "PJ", "org_id": "HH1", "name": "项目", "risk_level": "high",
             "required_qualifications": ["Q1"]},
        )
        self._req(
            "POST", "/authorizations",
            {"project_id": "PJ", "physician_id": "D1", "kind": "institution",
             "valid_from": iso(day_start(-1)), "valid_until": iso(day_start(30))},
        )
        status, body = self._req(
            "POST", "/sessions",
            {"project_id": "PJ", "start": iso(day_start(10)), "end": iso(day_start(10, 12)),
             "capacity": 3},
        )
        sid = body["data"]["id"]

        # D2 不合格：接口解释原因 + 候选
        status, body = self._req(
            "POST", f"/sessions/{sid}/assignments", {"physician_id": "D2"}
        )
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "physician_not_eligible")
        self.assertTrue(body["error"]["reasons"])
        eligible = [
            c["physician_id"]
            for c in body["error"]["details"]["coverage"]["candidates"]
            if c["eligible"]
        ]
        self.assertEqual(eligible, ["D1"])

        self._req("POST", f"/sessions/{sid}/assignments", {"physician_id": "D1"})
        status, body = self._req("POST", f"/sessions/{sid}/confirm", {})
        self.assertEqual(status, 200)
        self.assertEqual(len(body["data"]["frozen"]), 1)

        status, body = self._req(
            "POST", f"/sessions/{sid}/appointments", {"patient_ref": "p-9"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["remaining_capacity"], 2)

        status, body = self._req("POST", f"/sessions/{sid}/complete", {})
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["session"]["status"], SESSION_COMPLETED)

        status, body = self._req("GET", f"/sessions/{sid}/service-records")
        self.assertEqual(status, 200)
        self.assertEqual(body["data"][0]["physician_id"], "D1")

    def test_unknown_route_and_bad_json(self) -> None:
        status, body = self._req("GET", "/nope")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "not_found_route")
        req = urllib.request.Request(
            self.base + "/organizations", data=b"{bad", method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(req, timeout=5)
            self.fail("应当返回 400")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)


if __name__ == "__main__":
    unittest.main()

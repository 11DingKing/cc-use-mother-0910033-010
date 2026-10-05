"""排班领域服务的回归测试。"""
from __future__ import annotations

import sys
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from scheduling import (
    BookingService,
    CapacityError,
    ConflictError,
    CoverageError,
    NotFoundError,
    RegistryService,
    SchedulingService,
    Store,
)
from scheduling.models import AssignmentStatus, ShiftStatus, WorkLimitPolicy

UTC = timezone.utc


def dt(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=UTC)


class World:
    """标准场景：两院、两项目、三名医师（含一名跨院支援）。"""

    def __init__(self, policy: WorkLimitPolicy | None = None):
        self.store = Store(policy=policy)
        clock = lambda: datetime(2026, 10, 4, 12, 0, tzinfo=UTC)  # noqa: E731
        self.registry = RegistryService(self.store, clock=clock)
        self.scheduling = SchedulingService(self.store, clock=clock)
        self.booking = BookingService(self.store, clock=clock)

        self.registry.register_institution("H1", "本院", ["CARDIO", "NEURO"])
        self.registry.register_institution("H2", "分院", ["CARDIO"])
        self.registry.register_project("CARDIO", "心外科高风险手术", "Q-CARDIO-CHIEF")
        self.registry.register_project("NEURO", "神经外科高风险手术", "Q-NEURO-CHIEF")
        self.registry.register_physician("P1", "医师一", "H1", ["Q-CARDIO-CHIEF"])
        self.registry.register_physician("P2", "医师二", "H1", ["Q-NEURO-CHIEF"])
        self.registry.register_physician("P3", "医师三", "H2", ["Q-CARDIO-CHIEF"])
        self.registry.grant_authorization("A1", "P1", "H1", "CARDIO", dt(1, 0), dt(31, 0))
        self.registry.grant_authorization(
            "A3", "P3", "H1", "CARDIO", dt(1, 0), dt(31, 0), support=True
        )
        self.registry.add_availability("V1", "P1", "H1", dt(5, 0), dt(6, 0))
        self.registry.add_availability("V3", "P3", "H1", dt(5, 0), dt(6, 0))

    def make_shift(
        self,
        shift_id: str = "S1",
        project: str = "CARDIO",
        institution: str = "H1",
        start: datetime | None = None,
        end: datetime | None = None,
        capacity: int = 1,
    ):
        return self.scheduling.create_shift(
            shift_id,
            institution,
            project,
            start or dt(5, 8),
            end or dt(5, 12),
            capacity,
        )


class ConfirmSnapshotTest(unittest.TestCase):
    def setUp(self) -> None:
        self.world = World()

    def test_confirm_freezes_snapshot_and_locks_coverage(self) -> None:
        w = self.world
        w.make_shift("S1")
        assignment = w.scheduling.confirm_shift("S1", "P1", reason="常规排班")
        self.assertEqual(assignment.snapshot.qualifications, ("Q-CARDIO-CHIEF",))
        self.assertEqual(assignment.snapshot.authorization_id, "A1")
        self.assertFalse(assignment.snapshot.support)
        shift = w.scheduling.get_shift("S1")
        self.assertEqual(shift.status, ShiftStatus.CONFIRMED)

        # 冻结后修改资质与授权，不回溯改写快照；但实时覆盖标记为风险
        w.registry.update_qualifications("P1", remove=["Q-CARDIO-CHIEF"])
        w.registry.suspend_authorization("A1")
        frozen = w.scheduling.get_shift("S1").current_assignment().snapshot
        self.assertEqual(frozen.qualifications, ("Q-CARDIO-CHIEF",))
        report = w.scheduling.coverage("S1")
        self.assertEqual(report.status, "at_risk")
        self.assertTrue(any("暂停" in risk for risk in report.risks))

    def test_confirm_rejects_ineligible_and_explains(self) -> None:
        w = self.world
        w.make_shift("S1")
        with self.assertRaises(CoverageError) as ctx:
            w.scheduling.confirm_shift("S1", "P2")
        report = ctx.exception.report
        self.assertEqual(report.status, "uncovered")
        rejected = {item.physician_id: item for item in report.rejected}
        self.assertIn("P2", rejected)
        rules = {r.rule for r in rejected["P2"].rejections}
        self.assertIn("qualification", rules)
        candidates = {c.physician_id: c for c in report.candidates}
        self.assertIn("P1", candidates)
        self.assertIn("P3", candidates)  # 跨院支援候选
        self.assertTrue(candidates["P3"].support)

    def test_institution_scope_gap(self) -> None:
        w = self.world
        w.make_shift("S9", project="NEURO", institution="H2")
        with self.assertRaises(CoverageError) as ctx:
            w.scheduling.confirm_shift("S9", "P1")
        gaps = ctx.exception.report.shift_gaps
        self.assertEqual([gap.rule for gap in gaps], ["institution_scope"])

    def test_availability_must_cover_shift(self) -> None:
        w = self.world
        w.registry.register_physician("P4", "医师四", "H1", ["Q-CARDIO-CHIEF"])
        w.registry.grant_authorization("A4", "P4", "H1", "CARDIO", dt(1, 0), dt(31, 0))
        w.make_shift("S1")
        with self.assertRaises(CoverageError) as ctx:
            w.scheduling.confirm_shift("S1", "P4")
        rejected = {item.physician_id: item for item in ctx.exception.report.rejected}
        rules = {r.rule for r in rejected["P4"].rejections}
        self.assertEqual(rules, {"availability"})

    def test_cross_hospital_support_required(self) -> None:
        w = self.world
        w.registry.register_physician("P5", "医师五", "H2", ["Q-CARDIO-CHIEF"])
        w.registry.grant_authorization("A5", "P5", "H1", "CARDIO", dt(1, 0), dt(31, 0))
        w.registry.add_availability("V5", "P5", "H1", dt(5, 0), dt(6, 0))
        w.make_shift("S1")
        with self.assertRaises(CoverageError) as ctx:
            w.scheduling.confirm_shift("S1", "P5")
        rejected = {item.physician_id: item for item in ctx.exception.report.rejected}
        rules = {r.rule for r in rejected["P5"].rejections}
        self.assertIn("support_missing", rules)
        # 补充跨院支援授权后即可确认
        w.registry.grant_authorization(
            "A5S", "P5", "H1", "CARDIO", dt(1, 0), dt(31, 0), support=True
        )
        assignment = w.scheduling.confirm_shift("S1", "P5")
        self.assertTrue(assignment.snapshot.support)


class SubstitutionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.world = World()
        self.world.make_shift("S1")
        self.world.scheduling.confirm_shift("S1", "P1")

    def test_substitute_replaces_and_preserves_history(self) -> None:
        w = self.world
        new = w.scheduling.substitute("S1", "P3", reason="临时替班：P1 请假")
        shift = w.scheduling.get_shift("S1")
        self.assertEqual(shift.current_assignment().physician_id, "P3")
        self.assertEqual(len(shift.assignments), 2)
        old = shift.assignments[0]
        self.assertEqual(old.status, AssignmentStatus.REPLACED)
        self.assertEqual(old.replaced_by, new.assignment_id)
        self.assertTrue(new.snapshot.support)
        self.assertEqual(new.reason, "临时替班：P1 请假")

    def test_substitute_requires_eligibility(self) -> None:
        with self.assertRaises(CoverageError):
            self.world.scheduling.substitute("S1", "P2")

    def test_completed_shift_keeps_original_responsible(self) -> None:
        w = self.world
        w.scheduling.complete_shift("S1")
        with self.assertRaises(ConflictError):
            w.scheduling.substitute("S1", "P3", reason="事后替换")
        report = w.scheduling.coverage("S1")
        self.assertEqual(report.status, "completed")
        self.assertEqual(report.responsible["physician_id"], "P1")
        # 授权暂停也不改变已完成服务的原责任人
        w.registry.suspend_authorization("A1")
        report = w.scheduling.coverage("S1")
        self.assertEqual(report.status, "completed")
        self.assertEqual(report.responsible["physician_id"], "P1")


class SuspensionTest(unittest.TestCase):
    def test_suspend_blocks_new_and_flags_existing(self) -> None:
        w = World()
        w.make_shift("S1")
        w.scheduling.confirm_shift("S1", "P1")
        _, affected = w.registry.suspend_authorization("A1")
        self.assertEqual(affected, ["S1"])
        self.assertEqual(w.scheduling.coverage("S1").status, "at_risk")

        w.make_shift("S2", start=dt(5, 13), end=dt(5, 17))
        with self.assertRaises(CoverageError) as ctx:
            w.scheduling.confirm_shift("S2", "P1")
        rejected = {item.physician_id: item for item in ctx.exception.report.rejected}
        rules = {r.rule for r in rejected["P1"].rejections}
        self.assertIn("authorization_suspended", rules)

        w.registry.resume_authorization("A1")
        self.assertEqual(w.scheduling.coverage("S1").status, "covered")
        w.scheduling.confirm_shift("S2", "P1")


class WorkLimitTest(unittest.TestCase):
    def test_overlap_rejected(self) -> None:
        w = World()
        w.make_shift("S1")  # 08:00-12:00
        w.scheduling.confirm_shift("S1", "P1")
        w.make_shift("S2", start=dt(5, 11), end=dt(5, 15))
        with self.assertRaises(CoverageError) as ctx:
            w.scheduling.confirm_shift("S2", "P1")
        rejected = {item.physician_id: item for item in ctx.exception.report.rejected}
        rules = {r.rule for r in rejected["P1"].rejections}
        self.assertIn("overlap", rules)

    def test_block_work_hours_limit(self) -> None:
        w = World()
        w.make_shift("S1")  # 08:00-12:00，4 小时
        w.scheduling.confirm_shift("S1", "P1")
        w.make_shift("S2", start=dt(5, 12, 30), end=dt(5, 18, 30))  # 6 小时
        w.scheduling.confirm_shift("S2", "P1")
        w.make_shift("S3", start=dt(5, 19), end=dt(5, 23))  # 段内累计 14 小时 > 12
        with self.assertRaises(CoverageError) as ctx:
            w.scheduling.confirm_shift("S3", "P1")
        rejected = {item.physician_id: item for item in ctx.exception.report.rejected}
        rules = {r.rule for r in rejected["P1"].rejections}
        self.assertIn("work_block_hours", rules)

    def test_block_span_limit(self) -> None:
        w = World(policy=WorkLimitPolicy(max_continuous_span_hours=10.0))
        w.make_shift("S1")  # 08:00-12:00
        w.scheduling.confirm_shift("S1", "P1")
        w.make_shift("S2", start=dt(5, 12, 30), end=dt(5, 18, 30))  # 跨度 10.5 小时 > 10
        with self.assertRaises(CoverageError) as ctx:
            w.scheduling.confirm_shift("S2", "P1")
        rejected = {item.physician_id: item for item in ctx.exception.report.rejected}
        rules = {r.rule for r in rejected["P1"].rejections}
        self.assertIn("work_block_span", rules)

    def test_rest_gap_separates_blocks(self) -> None:
        w = World()
        w.make_shift("S1")  # 08:00-12:00
        w.scheduling.confirm_shift("S1", "P1")
        w.make_shift("S2", start=dt(5, 21), end=dt(5, 23))  # 间隔 9 小时，新的工作段
        w.scheduling.confirm_shift("S2", "P1")


class BookingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.world = World()
        self.world.make_shift("S1", capacity=5)
        self.world.scheduling.confirm_shift("S1", "P1")

    def test_concurrent_booking_is_atomic(self) -> None:
        w = self.world
        results = {"ok": 0, "full": 0}
        lock = threading.Lock()

        def attempt(index: int) -> None:
            try:
                w.booking.book("S1", f"B{index:02d}")
                with lock:
                    results["ok"] += 1
            except CapacityError:
                with lock:
                    results["full"] += 1

        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(results["ok"], 5)
        self.assertEqual(results["full"], 15)
        self.assertEqual(len(w.scheduling.get_shift("S1").bookings), 5)

    def test_booking_is_idempotent(self) -> None:
        w = self.world
        first = w.booking.book("S1", "B1")
        second = w.booking.book("S1", "B1")
        self.assertEqual(first["seq"], second["seq"])
        self.assertEqual(len(w.scheduling.get_shift("S1").bookings), 1)

    def test_booking_requires_confirmed_shift(self) -> None:
        w = World()
        w.make_shift("S2")
        with self.assertRaises(ConflictError):
            w.booking.book("S2", "B1")

    def test_booking_blocked_when_coverage_at_risk(self) -> None:
        w = self.world
        w.registry.suspend_authorization("A1")
        with self.assertRaises(CoverageError):
            w.booking.book("S1", "B1")


class ShiftLifecycleTest(unittest.TestCase):
    def test_cancel_rules(self) -> None:
        w = World()
        w.make_shift("S1")
        w.scheduling.cancel_shift("S1")
        self.assertEqual(w.scheduling.get_shift("S1").status, ShiftStatus.CANCELLED)

        w.make_shift("S2", start=dt(5, 13), end=dt(5, 15))
        w.scheduling.confirm_shift("S2", "P1")
        w.booking.book("S2", "B1")
        with self.assertRaises(ConflictError):
            w.scheduling.cancel_shift("S2")

    def test_optimistic_version_check(self) -> None:
        w = World()
        w.make_shift("S1")
        with self.assertRaises(ConflictError):
            w.scheduling.confirm_shift("S1", "P1", expected_version=3)
        w.scheduling.confirm_shift("S1", "P1", expected_version=0)
        with self.assertRaises(ConflictError):
            w.scheduling.substitute("S1", "P3", expected_version=0)
        w.scheduling.substitute("S1", "P3", expected_version=1)

    def test_unknown_entities(self) -> None:
        w = World()
        with self.assertRaises(NotFoundError):
            w.scheduling.get_shift("NOPE")
        with self.assertRaises(NotFoundError):
            w.registry.suspend_authorization("NOPE")


if __name__ == "__main__":
    unittest.main()

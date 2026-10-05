"""HTTP 接口的端到端测试。"""
from __future__ import annotations

import http.client
import json
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from scheduling.api import create_server


class ApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.server, _ = create_server("127.0.0.1", 0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def call(self, method: str, path: str, payload: dict | None = None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        body = json.dumps(payload) if payload is not None else None
        headers = {"Content-Type": "application/json"} if body is not None else {}
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        data = json.loads(response.read().decode("utf-8"))
        conn.close()
        return response.status, data

    def test_full_scheduling_flow(self) -> None:
        status, _ = self.call(
            "POST",
            "/institutions",
            {"institution_id": "H1", "name": "本院", "registered_projects": ["CARDIO"]},
        )
        self.assertEqual(status, 201)
        status, _ = self.call(
            "POST",
            "/projects",
            {"code": "CARDIO", "name": "心外科高风险手术", "required_qualification": "Q-CARDIO-CHIEF"},
        )
        self.assertEqual(status, 201)
        status, _ = self.call(
            "POST",
            "/physicians",
            {
                "physician_id": "P1",
                "name": "医师一",
                "home_institution_id": "H1",
                "qualifications": ["Q-CARDIO-CHIEF"],
            },
        )
        self.assertEqual(status, 201)
        status, _ = self.call(
            "POST",
            "/physicians",
            {
                "physician_id": "P2",
                "name": "医师二",
                "home_institution_id": "H1",
                "qualifications": [],
            },
        )
        self.assertEqual(status, 201)
        status, _ = self.call(
            "POST",
            "/authorizations",
            {
                "authorization_id": "A1",
                "physician_id": "P1",
                "institution_id": "H1",
                "project_code": "CARDIO",
                "valid_from": "2026-10-01T00:00:00+00:00",
                "valid_to": "2026-10-31T00:00:00+00:00",
            },
        )
        self.assertEqual(status, 201)
        status, _ = self.call(
            "POST",
            "/availability",
            {
                "slot_id": "V1",
                "physician_id": "P1",
                "institution_id": "H1",
                "start": "2026-10-05T00:00:00+00:00",
                "end": "2026-10-06T00:00:00+00:00",
            },
        )
        self.assertEqual(status, 201)
        status, shift = self.call(
            "POST",
            "/shifts",
            {
                "shift_id": "S1",
                "institution_id": "H1",
                "project_code": "CARDIO",
                "start": "2026-10-05T08:00:00+00:00",
                "end": "2026-10-05T12:00:00+00:00",
                "capacity": 1,
            },
        )
        self.assertEqual(status, 201)
        self.assertEqual(shift["status"], "draft")

        # 覆盖解释：候选替代与被拒原因
        status, coverage = self.call("GET", "/shifts/S1/coverage")
        self.assertEqual(status, 200)
        self.assertEqual(coverage["status"], "uncovered")
        self.assertEqual([c["physician_id"] for c in coverage["candidates"]], ["P1"])
        rejected = {r["physician_id"]: r for r in coverage["rejected"]}
        self.assertIn("P2", rejected)
        self.assertTrue(
            any(reason["rule"] == "qualification" for reason in rejected["P2"]["reasons"])
        )

        # 不合格医师确认 → 409 + 解释报告
        status, error = self.call("POST", "/shifts/S1/confirm", {"physician_id": "P2"})
        self.assertEqual(status, 409)
        self.assertEqual(error["error"], "coverage_insufficient")
        self.assertIn("report", error["details"])

        # 确认排班：冻结资格快照、锁定责任覆盖
        status, assignment = self.call(
            "POST", "/shifts/S1/confirm", {"physician_id": "P1", "reason": "常规排班"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(assignment["snapshot"]["authorization_id"], "A1")
        self.assertEqual(assignment["snapshot"]["qualifications"], ["Q-CARDIO-CHIEF"])

        # 并发预约：容量原子扣减
        status, booking = self.call("POST", "/shifts/S1/bookings", {"booking_id": "B1"})
        self.assertEqual(status, 201)
        self.assertEqual(booking["remaining_capacity"], 0)
        status, error = self.call("POST", "/shifts/S1/bookings", {"booking_id": "B2"})
        self.assertEqual(status, 409)
        self.assertEqual(error["error"], "capacity_exceeded")

        # 授权暂停 → 已确认班次标记风险
        status, result = self.call("POST", "/authorizations/A1/suspend")
        self.assertEqual(status, 200)
        self.assertEqual(result["affected_shifts"], ["S1"])
        status, coverage = self.call("GET", "/shifts/S1/coverage")
        self.assertEqual(coverage["status"], "at_risk")

        # 完成服务 → 原责任人保留，不可替班
        status, shift = self.call("POST", "/shifts/S1/complete")
        self.assertEqual(status, 200)
        self.assertEqual(shift["status"], "completed")
        self.assertEqual(shift["responsible"], "P1")
        status, error = self.call("POST", "/shifts/S1/substitute", {"physician_id": "P2"})
        self.assertEqual(status, 409)
        self.assertEqual(error["error"], "conflict")

    def test_unknown_route_and_entity(self) -> None:
        status, error = self.call("GET", "/nope")
        self.assertEqual(status, 404)
        self.assertEqual(error["error"], "not_found")
        status, error = self.call("GET", "/shifts/NOPE")
        self.assertEqual(status, 404)
        self.assertEqual(error["error"], "not_found")


if __name__ == "__main__":
    unittest.main()

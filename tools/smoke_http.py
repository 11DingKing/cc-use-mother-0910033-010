"""端到端冒烟脚本：启动真实 HTTP 服务，走完整业务闭环。运行后即退出。"""
from __future__ import annotations

import json
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from scheduling import create_server


def main() -> None:
    httpd = create_server("127.0.0.1", 0)
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    def call(method: str, path: str, body: dict | None = None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            base + path, data=data, method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def ok(method, path, body=None):
        status, payload = call(method, path, body)
        assert status == 200, (status, payload)
        return payload["data"]

    ok("POST", "/organizations", {"id": "H1", "name": "一院"})
    ok("POST", "/organizations", {"id": "H2", "name": "二院"})
    ok("POST", "/physicians", {"id": "P1", "name": "张医生"})
    ok("POST", "/physicians", {"id": "P3", "name": "王医生"})
    for pid in ("P1", "P3"):
        ok("POST", f"/physicians/{pid}/qualifications", {"code": "CI", "name": "介入"})
    ok("POST", "/registrations", {
        "physician_id": "P1", "org_id": "H1", "scopes": ["CI"],
        "valid_from": "2026-01-01T00:00:00+08:00",
    })
    ok("POST", "/registrations", {
        "physician_id": "P3", "org_id": "H2", "scopes": ["CI"],
        "valid_from": "2026-01-01T00:00:00+08:00",
    })
    ok("POST", "/projects", {
        "id": "PRJ", "org_id": "H1", "name": "高危介入", "risk_level": "high",
        "required_qualifications": ["CI"],
    })
    ok("POST", "/authorizations", {
        "project_id": "PRJ", "physician_id": "P1", "kind": "institution",
        "valid_from": "2026-10-01T00:00:00+08:00",
        "valid_until": "2026-12-31T00:00:00+08:00",
    })
    ok("POST", "/authorizations", {
        "project_id": "PRJ", "physician_id": "P3", "kind": "cross_support",
        "source_org_id": "H2",
        "valid_from": "2026-10-01T00:00:00+08:00",
        "valid_until": "2026-12-31T00:00:00+08:00",
    })
    sid = ok("POST", "/sessions", {
        "project_id": "PRJ",
        "start": "2026-11-10T09:00:00+08:00",
        "end": "2026-11-10T12:00:00+08:00",
        "capacity": 2,
    })["id"]
    ok("POST", f"/sessions/{sid}/assignments", {"physician_id": "P1"})
    ok("POST", f"/sessions/{sid}/confirm", {})

    # 暂停授权：返回受影响时段与候选替代
    suspended = ok("POST", "/authorizations/auth_000001/suspend", {"reason": "飞行检查"})
    aff = suspended["affected_sessions"][0]
    assert aff["covered"] is False
    assert [c["physician_id"] for c in aff["candidates"] if c["eligible"]] == ["P3"]
    print("1) 暂停后覆盖报告：covered=False，合格候选=P3（跨院支援）")

    # 覆盖不足时预约被拒，错误体解释原因
    status, payload = call("POST", f"/sessions/{sid}/appointments", {"patient_ref": "pat-1"})
    assert status == 409 and payload["error"]["code"] == "coverage_insufficient"
    print("2) 覆盖不足预约被拒：409 coverage_insufficient")

    # 跨院替班：冻结 substitution 快照并取代原快照
    sub = ok("POST", f"/sessions/{sid}/substitute", {"physician_id": "P3"})
    snap = ok("GET", f"/snapshots/{sub['frozen']['snapshot_id']}")
    assert snap["kind"] == "substitution"
    assert snap["registration"]["role"] == "cross_support_source"
    print(f"3) P3 跨院替班成功：快照 {snap['id']}，supersedes={snap.get('supersedes_snapshot_id')}")

    booked = ok("POST", f"/sessions/{sid}/appointments", {"patient_ref": "pat-1"})
    assert booked["remaining_capacity"] == 1
    print("4) 恢复覆盖后预约成功，余位 1")

    # 并发抢占仅剩的 1 个名额
    codes: list[int] = []
    lock = threading.Lock()

    def race(patient: str) -> None:
        st, _ = call("POST", f"/sessions/{sid}/appointments", {"patient_ref": patient})
        with lock:
            codes.append(st)

    threads = [threading.Thread(target=race, args=(f"pat-{i}",)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert codes.count(200) == 1 and codes.count(409) == 5, codes
    print("5) 6 个并发预约：恰好 1 成功、5 个 409（原子容量扣减）")

    # 完成服务：原责任人留痕，之后禁止替班
    completed = ok("POST", f"/sessions/{sid}/complete", {})
    assert completed["session"]["status"] == "completed"
    records = ok("GET", f"/sessions/{sid}/service-records")
    assert len(records) == 1 and records[0]["physician_id"] == "P3"
    status, payload = call("POST", f"/sessions/{sid}/substitute", {"physician_id": "P1"})
    assert status == 409 and payload["error"]["code"] == "session_locked"
    print("6) 完成登记：服务记录锁定责任人 P3，后续替班被拒绝")

    httpd.shutdown()
    httpd.server_close()
    print("端到端冒烟全部通过 ✔")


if __name__ == "__main__":
    main()

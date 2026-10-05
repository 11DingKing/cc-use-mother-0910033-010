"""基于标准库的 HTTP 接口，覆盖排班、授权、替班与预约。"""
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from enum import Enum
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .errors import DomainError, NotFoundError, ValidationError
from .models import (
    assignment_to_dict,
    authorization_to_dict,
    availability_to_dict,
    institution_to_dict,
    physician_to_dict,
    project_to_dict,
    shift_to_dict,
)
from .services import BookingService, RegistryService, SchedulingService
from .store import Store


def parse_datetime(value, field: str) -> datetime:
    """解析 ISO 8601 时间；缺省时区按 UTC 处理。"""
    if not isinstance(value, str):
        raise ValidationError(f"{field} 必须是 ISO 8601 字符串")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError(f"{field} 不是合法的 ISO 8601 时间：{value}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def require(body: dict, *fields: str) -> None:
    missing = [name for name in fields if body.get(name) is None]
    if missing:
        raise ValidationError("缺少字段：" + "、".join(missing))


class SchedulingApi:
    """排班服务的 HTTP 路由层。"""

    def __init__(self, store: Store | None = None, clock=None) -> None:
        self.store = store or Store()
        self.registry = RegistryService(self.store, clock=clock)
        self.scheduling = SchedulingService(self.store, clock=clock)
        self.booking = BookingService(self.store, clock=clock)
        self.routes = [
            ("GET", re.compile(r"^/health$"), self.health),
            ("POST", re.compile(r"^/institutions$"), self.create_institution),
            ("POST", re.compile(r"^/projects$"), self.create_project),
            ("POST", re.compile(r"^/physicians$"), self.create_physician),
            (
                "POST",
                re.compile(r"^/physicians/(?P<physician_id>[^/]+)/qualifications$"),
                self.update_qualifications,
            ),
            ("POST", re.compile(r"^/authorizations$"), self.grant_authorization),
            (
                "POST",
                re.compile(r"^/authorizations/(?P<authorization_id>[^/]+)/suspend$"),
                self.suspend_authorization,
            ),
            (
                "POST",
                re.compile(r"^/authorizations/(?P<authorization_id>[^/]+)/resume$"),
                self.resume_authorization,
            ),
            (
                "POST",
                re.compile(r"^/authorizations/(?P<authorization_id>[^/]+)/revoke$"),
                self.revoke_authorization,
            ),
            ("POST", re.compile(r"^/availability$"), self.add_availability),
            ("POST", re.compile(r"^/shifts$"), self.create_shift),
            ("GET", re.compile(r"^/shifts/(?P<shift_id>[^/]+)$"), self.get_shift),
            (
                "GET",
                re.compile(r"^/shifts/(?P<shift_id>[^/]+)/coverage$"),
                self.get_coverage,
            ),
            ("POST", re.compile(r"^/shifts/(?P<shift_id>[^/]+)/confirm$"), self.confirm_shift),
            (
                "POST",
                re.compile(r"^/shifts/(?P<shift_id>[^/]+)/substitute$"),
                self.substitute,
            ),
            (
                "POST",
                re.compile(r"^/shifts/(?P<shift_id>[^/]+)/complete$"),
                self.complete_shift,
            ),
            ("POST", re.compile(r"^/shifts/(?P<shift_id>[^/]+)/cancel$"), self.cancel_shift),
            (
                "POST",
                re.compile(r"^/shifts/(?P<shift_id>[^/]+)/bookings$"),
                self.create_booking,
            ),
        ]

    def handle(self, method: str, path: str, body: dict) -> tuple[int, dict]:
        for route_method, pattern, handler in self.routes:
            if route_method != method:
                continue
            match = pattern.match(path)
            if match:
                return handler(body=body, **match.groupdict())
        raise NotFoundError(f"接口不存在：{method} {path}")

    # -- 健康检查 --

    def health(self, body: dict):
        return 200, {"status": "ok"}

    # -- 注册与授权 --

    def create_institution(self, body: dict):
        require(body, "institution_id", "name")
        value = self.registry.register_institution(
            body["institution_id"], body["name"], body.get("registered_projects", [])
        )
        return 201, institution_to_dict(value)

    def create_project(self, body: dict):
        require(body, "code", "name", "required_qualification")
        value = self.registry.register_project(
            body["code"],
            body["name"],
            body["required_qualification"],
            body.get("risk_level", "high"),
        )
        return 201, project_to_dict(value)

    def create_physician(self, body: dict):
        require(body, "physician_id", "name", "home_institution_id")
        value = self.registry.register_physician(
            body["physician_id"],
            body["name"],
            body["home_institution_id"],
            body.get("qualifications", []),
        )
        return 201, physician_to_dict(value)

    def update_qualifications(self, body: dict, physician_id: str):
        value = self.registry.update_qualifications(
            physician_id, add=body.get("add", []), remove=body.get("remove", [])
        )
        return 200, physician_to_dict(value)

    def grant_authorization(self, body: dict):
        require(
            body,
            "authorization_id",
            "physician_id",
            "institution_id",
            "project_code",
            "valid_from",
            "valid_to",
        )
        value = self.registry.grant_authorization(
            body["authorization_id"],
            body["physician_id"],
            body["institution_id"],
            body["project_code"],
            parse_datetime(body["valid_from"], "valid_from"),
            parse_datetime(body["valid_to"], "valid_to"),
            support=bool(body.get("support", False)),
        )
        return 201, authorization_to_dict(value)

    def suspend_authorization(self, body: dict, authorization_id: str):
        value, affected = self.registry.suspend_authorization(authorization_id)
        return 200, {
            "authorization": authorization_to_dict(value),
            "affected_shifts": affected,
        }

    def resume_authorization(self, body: dict, authorization_id: str):
        return 200, authorization_to_dict(self.registry.resume_authorization(authorization_id))

    def revoke_authorization(self, body: dict, authorization_id: str):
        value, affected = self.registry.revoke_authorization(authorization_id)
        return 200, {
            "authorization": authorization_to_dict(value),
            "affected_shifts": affected,
        }

    def add_availability(self, body: dict):
        require(body, "slot_id", "physician_id", "institution_id", "start", "end")
        value = self.registry.add_availability(
            body["slot_id"],
            body["physician_id"],
            body["institution_id"],
            parse_datetime(body["start"], "start"),
            parse_datetime(body["end"], "end"),
        )
        return 201, availability_to_dict(value)

    # -- 排班 --

    def create_shift(self, body: dict):
        require(body, "shift_id", "institution_id", "project_code", "start", "end")
        value = self.scheduling.create_shift(
            body["shift_id"],
            body["institution_id"],
            body["project_code"],
            parse_datetime(body["start"], "start"),
            parse_datetime(body["end"], "end"),
            capacity=int(body.get("capacity", 1)),
        )
        return 201, shift_to_dict(value)

    def get_shift(self, body: dict, shift_id: str):
        return 200, shift_to_dict(self.scheduling.get_shift(shift_id))

    def get_coverage(self, body: dict, shift_id: str):
        return 200, self.scheduling.coverage(shift_id).to_dict()

    def confirm_shift(self, body: dict, shift_id: str):
        require(body, "physician_id")
        value = self.scheduling.confirm_shift(
            shift_id,
            body["physician_id"],
            reason=body.get("reason", ""),
            expected_version=body.get("expected_version"),
        )
        return 200, assignment_to_dict(value)

    def substitute(self, body: dict, shift_id: str):
        require(body, "physician_id")
        value = self.scheduling.substitute(
            shift_id,
            body["physician_id"],
            reason=body.get("reason", ""),
            expected_version=body.get("expected_version"),
        )
        return 200, assignment_to_dict(value)

    def complete_shift(self, body: dict, shift_id: str):
        value = self.scheduling.complete_shift(
            shift_id, expected_version=body.get("expected_version")
        )
        return 200, shift_to_dict(value)

    def cancel_shift(self, body: dict, shift_id: str):
        value = self.scheduling.cancel_shift(
            shift_id, expected_version=body.get("expected_version")
        )
        return 200, shift_to_dict(value)

    # -- 预约 --

    def create_booking(self, body: dict, shift_id: str):
        require(body, "booking_id")
        return 201, self.booking.book(shift_id, body["booking_id"])


def _json_default(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    raise TypeError(f"不可序列化：{type(value)!r}")


class _RequestHandler(BaseHTTPRequestHandler):
    api: SchedulingApi = None  # 由 create_server 注入

    def _dispatch(self, method: str) -> None:
        try:
            body: dict = {}
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(body, dict):
                    raise ValidationError("请求体必须是 JSON 对象")
            path = self.path.split("?", 1)[0]
            status, payload = self.api.handle(method, path, body)
        except DomainError as exc:
            status = exc.http_status
            payload = {"error": exc.code, "message": exc.message, "details": exc.details}
        except json.JSONDecodeError as exc:
            status = 400
            payload = {
                "error": "invalid_json",
                "message": f"请求体不是合法 JSON：{exc}",
                "details": {},
            }
        except Exception as exc:  # pragma: no cover - 兜底
            status = 500
            payload = {"error": "internal_error", "message": str(exc), "details": {}}
        data = json.dumps(payload, ensure_ascii=False, default=_json_default).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def log_message(self, *args) -> None:  # 保持输出干净
        pass


def create_server(
    host: str = "127.0.0.1",
    port: int = 8080,
    *,
    store: Store | None = None,
    clock=None,
) -> tuple[ThreadingHTTPServer, SchedulingApi]:
    """创建 HTTP 服务；port=0 时由系统分配端口。"""
    api = SchedulingApi(store, clock=clock)
    handler = type("SchedulingRequestHandler", (_RequestHandler,), {"api": api})
    server = ThreadingHTTPServer((host, port), handler)
    return server, api


def serve() -> None:
    parser = argparse.ArgumentParser(description="主诊医师授权排班服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    server, _ = create_server(args.host, args.port)
    host, port = server.server_address[:2]
    print(f"主诊医师授权排班服务已启动：http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    serve()

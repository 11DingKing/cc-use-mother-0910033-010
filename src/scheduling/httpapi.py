"""JSON HTTP 接口：仅做参数装配与错误转换，业务规则全部在领域服务内。"""
from __future__ import annotations

import json
import re
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from .errors import SchedulingError
from .service import SchedulingService


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"不可序列化的类型：{type(value)!r}")


def dumps(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, default=_json_default).encode("utf-8")


Route = tuple[str, re.Pattern[str], Callable[..., Any]]


class _Handler(BaseHTTPRequestHandler):
    service: SchedulingService  # 由 create_server 注入到类上

    server_version = "SchedulingHTTP/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:  # 静默，测试输出保持干净
        return

    # ---- 路由表：(方法, 正则, 处理函数名) ----
    @property
    def routes(self) -> list[Route]:
        return [
            ("GET", re.compile(r"^/health$"), self._health),
            ("POST", re.compile(r"^/organizations$"), self._create_org),
            ("POST", re.compile(r"^/physicians$"), self._create_physician),
            (
                "POST",
                re.compile(r"^/physicians/(?P<id>[^/]+)/qualifications$"),
                self._add_qualification,
            ),
            (
                "POST",
                re.compile(r"^/physicians/(?P<id>[^/]+)/work-rule$"),
                self._set_work_rule,
            ),
            ("POST", re.compile(r"^/registrations$"), self._add_registration),
            ("POST", re.compile(r"^/projects$"), self._create_project),
            ("POST", re.compile(r"^/authorizations$"), self._grant_auth),
            (
                "POST",
                re.compile(r"^/authorizations/(?P<id>[^/]+)/suspend$"),
                self._suspend_auth,
            ),
            (
                "POST",
                re.compile(r"^/authorizations/(?P<id>[^/]+)/resume$"),
                self._resume_auth,
            ),
            ("POST", re.compile(r"^/sessions$"), self._create_session),
            ("GET", re.compile(r"^/sessions$"), self._list_sessions),
            ("GET", re.compile(r"^/sessions/(?P<id>[^/]+)$"), self._get_session),
            (
                "POST",
                re.compile(r"^/sessions/(?P<id>[^/]+)/cancel$"),
                self._cancel_session,
            ),
            (
                "POST",
                re.compile(r"^/sessions/(?P<id>[^/]+)/assignments$"),
                self._propose,
            ),
            (
                "POST",
                re.compile(
                    r"^/sessions/(?P<id>[^/]+)/assignments/(?P<aid>[^/]+)/cancel$"
                ),
                self._cancel_proposal,
            ),
            ("POST", re.compile(r"^/sessions/(?P<id>[^/]+)/confirm$"), self._confirm),
            (
                "POST",
                re.compile(r"^/sessions/(?P<id>[^/]+)/substitute$"),
                self._substitute,
            ),
            (
                "GET",
                re.compile(r"^/sessions/(?P<id>[^/]+)/coverage$"),
                self._coverage,
            ),
            (
                "POST",
                re.compile(r"^/sessions/(?P<id>[^/]+)/complete$"),
                self._complete,
            ),
            (
                "GET",
                re.compile(r"^/sessions/(?P<id>[^/]+)/service-records$"),
                self._session_records,
            ),
            ("GET", re.compile(r"^/service-records$"), self._all_records),
            ("GET", re.compile(r"^/snapshots/(?P<id>[^/]+)$"), self._get_snapshot),
            (
                "POST",
                re.compile(r"^/sessions/(?P<id>[^/]+)/appointments$"),
                self._book,
            ),
            (
                "POST",
                re.compile(r"^/appointments/(?P<id>[^/]+)/cancel$"),
                self._cancel_appointment,
            ),
        ]

    def _dispatch(self, method: str) -> None:
        path = self.path.split("?", 1)[0]
        for verb, pattern, func in self.routes:
            if verb != method:
                continue
            match = pattern.match(path)
            if match:
                try:
                    payload = func(**match.groupdict())
                except SchedulingError as exc:
                    self._write(exc.status, {"error": exc.to_dict()})
                except (ValueError, KeyError) as exc:
                    self._write(
                        400,
                        {"error": {"code": "bad_request", "message": str(exc)}},
                    )
                else:
                    self._write(200, {"data": payload})
                return
        self._write(
            404, {"error": {"code": "not_found_route", "message": f"无此接口：{method} {path}"}}
        )

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    # ---- 请求/响应 ----
    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError(f"请求体不是合法 JSON：{exc}") from exc
        if not isinstance(value, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return value

    def _write(self, status: int, payload: dict[str, Any]) -> None:
        body = dumps(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    @staticmethod
    def _require(body: dict[str, Any], key: str) -> Any:
        if key not in body or body[key] in (None, ""):
            raise ValueError(f"缺少必填字段：{key}")
        return body[key]

    # ---- 端点装配 ----
    def _health(self) -> dict[str, Any]:
        return {"status": "ok"}

    def _create_org(self) -> dict[str, Any]:
        b = self._body()
        return self.service.register_organization(
            self._require(b, "id"), b.get("name", self._require(b, "id"))
        )

    def _create_physician(self) -> dict[str, Any]:
        b = self._body()
        return self.service.register_physician(
            self._require(b, "id"), b.get("name", self._require(b, "id"))
        )

    def _add_qualification(self, id: str) -> dict[str, Any]:
        b = self._body()
        return self.service.add_qualification(
            id,
            self._require(b, "code"),
            self._require(b, "name"),
            level=b.get("level"),
            expires_at=b.get("expires_at"),
        )

    def _set_work_rule(self, id: str) -> dict[str, Any]:
        b = self._body()
        return self.service.set_work_rule(
            id,
            max_consecutive_days=b.get("max_consecutive_days"),
            min_rest_hours=b.get("min_rest_hours"),
        )

    def _add_registration(self) -> dict[str, Any]:
        b = self._body()
        return self.service.add_registration(
            self._require(b, "physician_id"),
            self._require(b, "org_id"),
            self._require(b, "scopes"),
            self._require(b, "valid_from"),
            valid_until=b.get("valid_until"),
        )

    def _create_project(self) -> dict[str, Any]:
        b = self._body()
        return self.service.register_project(
            self._require(b, "id"),
            self._require(b, "org_id"),
            b.get("name", self._require(b, "id")),
            b.get("risk_level", "high"),
            self._require(b, "required_qualifications"),
        )

    def _grant_auth(self) -> dict[str, Any]:
        b = self._body()
        return self.service.grant_authorization(
            self._require(b, "project_id"),
            self._require(b, "physician_id"),
            b.get("kind", "institution"),
            self._require(b, "valid_from"),
            self._require(b, "valid_until"),
            source_org_id=b.get("source_org_id"),
        )

    def _suspend_auth(self, id: str) -> dict[str, Any]:
        b = self._body()
        return self.service.suspend_authorization(
            id, b.get("reason", "合规暂停")
        )

    def _resume_auth(self, id: str) -> dict[str, Any]:
        b = self._body()
        return self.service.resume_authorization(id, valid_until=b.get("valid_until"))

    def _create_session(self) -> dict[str, Any]:
        b = self._body()
        return self.service.create_session(
            self._require(b, "project_id"),
            self._require(b, "start"),
            self._require(b, "end"),
            capacity=int(b.get("capacity", 1)),
        )

    def _list_sessions(self) -> list[dict[str, Any]]:
        return self.service.list_sessions()

    def _get_session(self, id: str) -> dict[str, Any]:
        return self.service.get_session(id)

    def _cancel_session(self, id: str) -> dict[str, Any]:
        return self.service.cancel_session(id)

    def _propose(self, id: str) -> dict[str, Any]:
        b = self._body()
        return self.service.propose_assignment(id, self._require(b, "physician_id"))

    def _cancel_proposal(self, id: str, aid: str) -> dict[str, Any]:
        return self.service.cancel_proposal(id, aid)

    def _confirm(self, id: str) -> dict[str, Any]:
        b = self._body()
        return self.service.confirm_session(id, expected_version=b.get("expected_version"))

    def _substitute(self, id: str) -> dict[str, Any]:
        b = self._body()
        return self.service.substitute(
            id,
            self._require(b, "physician_id"),
            old_assignment_id=b.get("old_assignment_id"),
        )

    def _coverage(self, id: str) -> dict[str, Any]:
        return self.service.coverage_report(id)

    def _complete(self, id: str) -> dict[str, Any]:
        b = self._body()
        return self.service.complete_session(id, records=b.get("records"))

    def _session_records(self, id: str) -> list[dict[str, Any]]:
        return self.service.list_service_records(id)

    def _all_records(self) -> list[dict[str, Any]]:
        return self.service.list_service_records()

    def _get_snapshot(self, id: str) -> dict[str, Any]:
        return self.service.get_snapshot(id)

    def _book(self, id: str) -> dict[str, Any]:
        b = self._body()
        return self.service.book_appointment(
            id,
            self._require(b, "patient_ref"),
            expected_version=b.get("expected_version"),
        )

    def _cancel_appointment(self, id: str) -> dict[str, Any]:
        return self.service.cancel_appointment(id)


def create_server(
    host: str = "127.0.0.1",
    port: int = 0,
    service: SchedulingService | None = None,
) -> ThreadingHTTPServer:
    """构造可直接 serve_forever() 的 HTTP 服务；port=0 由系统分配端口。"""
    service = service or SchedulingService()

    handler = type("Handler", (_Handler,), {"service": service})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.service = service  # type: ignore[attr-defined]
    return httpd

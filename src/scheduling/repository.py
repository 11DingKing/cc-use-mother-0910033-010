"""线程安全的内存仓库。

单把可重入锁即数据库事务边界：service 层每个公开操作都在
``repository.transaction()`` 内完成全部校验与写入，保证原子性与
可串行化，杜绝"校验通过但写入前被并发改写"的竞态。

文档以 deepcopy 进出，调用方拿到的副本无法绕过锁修改仓库状态。
"""
from __future__ import annotations

import threading
from collections import defaultdict
from contextlib import contextmanager
from copy import deepcopy
from typing import Any, Callable, Iterator


_MISSING = object()


class Repository:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._tables: dict[str, dict[str, dict[str, Any]]] = {
            "physicians": {},
            "organizations": {},
            "registrations": {},      # key: f"{org_id}:{physician_id}"
            "projects": {},
            "authorizations": {},
            "sessions": {},
            "assignments": {},
            "snapshots": {},
            "appointments": {},
            "service_records": {},
        }
        self._counters: dict[str, int] = defaultdict(int)

    # ---- 事务 ----
    @contextmanager
    def transaction(self) -> Iterator[None]:
        """获取事务锁；同线程可重入（服务内部互调安全）。"""
        self._lock.acquire()
        try:
            yield
        finally:
            self._lock.release()

    # ---- ID ----
    def next_id(self, prefix: str) -> str:
        self._counters[prefix] += 1
        return f"{prefix}_{self._counters[prefix]:06d}"

    # ---- 通用读写 ----
    def put(self, table: str, key: str, doc: dict[str, Any]) -> None:
        self._tables[table][key] = deepcopy(doc)

    def get(self, table: str, key: str) -> dict[str, Any] | None:
        doc = self._tables[table].get(key)
        return deepcopy(doc) if doc is not None else None

    def require(self, table: str, key: str, label: str) -> dict[str, Any]:
        from .errors import NotFoundError

        doc = self._tables[table].get(key)
        if doc is None:
            raise NotFoundError("not_found", f"{label}不存在：{key}", details={"id": key})
        return deepcopy(doc)

    def find(
        self, table: str, predicate: Callable[[dict[str, Any]], bool]
    ) -> list[dict[str, Any]]:
        return [deepcopy(d) for d in self._tables[table].values() if predicate(d)]

    def find_one(
        self, table: str, predicate: Callable[[dict[str, Any]], bool]
    ) -> dict[str, Any] | None:
        for doc in self._tables[table].values():
            if predicate(doc):
                return deepcopy(doc)
        return None

    # ---- 领域索引便捷查询（调用时持锁）----
    def registrations_for(self, physician_id: str) -> list[dict[str, Any]]:
        return self.find(
            "registrations", lambda d: d["physician_id"] == physician_id
        )

    def authorizations_for_project(
        self, project_id: str, physician_id: str
    ) -> list[dict[str, Any]]:
        return self.find(
            "authorizations",
            lambda d: d["project_id"] == project_id
            and d["physician_id"] == physician_id,
        )

    def assignments_for_session(self, session_id: str) -> list[dict[str, Any]]:
        docs = self.find(
            "assignments", lambda d: d["session_id"] == session_id
        )
        docs.sort(key=lambda d: d["created_at"])
        return docs

    def active_assignments_for(self, physician_id: str) -> list[dict[str, Any]]:
        return self.find(
            "assignments",
            lambda d: d["physician_id"] == physician_id
            and d["status"] in ("proposed", "active"),
        )

    def sessions_for_physician(
        self, physician_id: str, statuses: tuple[str, ...]
    ) -> list[dict[str, Any]]:
        session_ids = {
            a["session_id"]
            for a in self.find(
                "assignments",
                lambda d: d["physician_id"] == physician_id
                and d["status"] in ("proposed", "active"),
            )
        }
        return [
            deepcopy(self._tables["sessions"][sid])
            for sid in session_ids
            if sid in self._tables["sessions"]
            and self._tables["sessions"][sid]["status"] in statuses
        ]

    def appointments_for_session(self, session_id: str) -> list[dict[str, Any]]:
        docs = self.find(
            "appointments",
            lambda d: d["session_id"] == session_id and d["status"] == "booked",
        )
        docs.sort(key=lambda d: d["booked_at"])
        return docs

    def has_appointment(self, session_id: str, patient_ref: str) -> bool:
        return any(
            True
            for _ in (
                self.find(
                    "appointments",
                    lambda d: d["session_id"] == session_id
                    and d["patient_ref"] == patient_ref
                    and d["status"] == "booked",
                )
            )
        )

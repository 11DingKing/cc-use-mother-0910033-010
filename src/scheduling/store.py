"""线程安全的内存数据存储。

所有聚合的"检查-修改"序列都必须在 ``Store.locked()`` 临界区内完成，
预约容量的原子扣减即依赖该锁。
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator

from .models import (
    Authorization,
    AvailabilitySlot,
    Institution,
    Physician,
    Project,
    Shift,
    WorkLimitPolicy,
)


class Store:
    """内存聚合存储，内置可重入锁。"""

    def __init__(self, policy: WorkLimitPolicy | None = None) -> None:
        self.policy = policy or WorkLimitPolicy()
        self._lock = threading.RLock()
        self.institutions: dict[str, Institution] = {}
        self.projects: dict[str, Project] = {}
        self.physicians: dict[str, Physician] = {}
        self.authorizations: dict[str, Authorization] = {}
        self.availability: dict[str, AvailabilitySlot] = {}
        self.shifts: dict[str, Shift] = {}
        self._counters: dict[str, int] = {}

    @contextmanager
    def locked(self) -> Iterator[None]:
        with self._lock:
            yield

    def next_id(self, prefix: str) -> str:
        """生成确定性的顺序编号。"""
        with self._lock:
            self._counters[prefix] = self._counters.get(prefix, 0) + 1
            return f"{prefix}-{self._counters[prefix]:06d}"

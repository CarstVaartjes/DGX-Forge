"""Thread-safe binding of an authenticated wall deadline to a monotonic clock."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime


class DeadlineBindingError(ValueError):
    pass


@dataclass(frozen=True)
class MonotonicDeadline:
    wall_deadline: datetime
    absolute_monotonic: float
    _lock: threading.Lock = field(
        default_factory=threading.Lock,
        init=False,
        repr=False,
        compare=False,
    )

    @classmethod
    def bind(cls, value: datetime | MonotonicDeadline) -> MonotonicDeadline:
        if type(value) is cls:
            return value
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise DeadlineBindingError("deadline is invalid")
        wall_now = datetime.now(UTC)
        monotonic_now = time.monotonic()
        remaining = (value - wall_now).total_seconds()
        if remaining <= 0:
            raise DeadlineBindingError("deadline has elapsed")
        return cls(value, monotonic_now + remaining)

    def remaining(self) -> float:
        return self.absolute() - time.monotonic()

    def absolute(self) -> float:
        with self._lock:
            return self.absolute_monotonic

    def check(self) -> None:
        if self.remaining() <= 0:
            raise DeadlineBindingError("deadline has elapsed")

    def extend(self, value: datetime) -> None:
        candidate = type(self).bind(value)
        with self._lock:
            if value < self.wall_deadline:
                raise DeadlineBindingError("deadline moved backwards")
            if value == self.wall_deadline:
                return
            object.__setattr__(self, "wall_deadline", value)
            object.__setattr__(
                self,
                "absolute_monotonic",
                max(self.absolute_monotonic, candidate.absolute_monotonic),
            )

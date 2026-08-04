"""One-time binding of an authenticated wall deadline to a monotonic clock."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import time


class DeadlineBindingError(ValueError):
    pass


@dataclass(frozen=True)
class MonotonicDeadline:
    wall_deadline: datetime
    absolute_monotonic: float

    @classmethod
    def bind(cls, value: datetime | "MonotonicDeadline") -> "MonotonicDeadline":
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
        return self.absolute_monotonic - time.monotonic()

    def check(self) -> None:
        if self.remaining() <= 0:
            raise DeadlineBindingError("deadline has elapsed")

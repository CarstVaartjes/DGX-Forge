"""Immutable durable-job contracts with attempt fencing and leases."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Literal

from ._redact import redact_message

JobState = Literal[
    "queued",
    "running",
    "waiting-for-operator",
    "succeeded",
    "failed",
    "cancelled",
]
_JOB_ID = re.compile(r"job_[0-9a-f]{32}")


class InvalidJobTransition(ValueError):
    """A durable job operation is not legal in its current state."""


class StaleAttempt(InvalidJobTransition):
    """A worker attempted to mutate a job using an obsolete fence."""


def _aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("job timestamps must be timezone-aware")


def _lease_seconds(value: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError("lease seconds must be a positive integer")


@dataclass(frozen=True, order=True)
class JobId:
    value: str

    @classmethod
    def parse(cls, value: str) -> JobId:
        if _JOB_ID.fullmatch(value) is None:
            raise ValueError("job id must match job_<32 lowercase hex characters>")
        return cls(value)


@dataclass(frozen=True)
class DurableJob:
    id: JobId
    kind: str
    state: JobState
    attempt: int
    worker_id: str | None
    lease_deadline: datetime | None
    created_at: datetime
    updated_at: datetime
    failure_reason: str | None = None

    @classmethod
    def enqueue(
        cls,
        *,
        job_id: JobId,
        kind: str,
        created_at: datetime,
    ) -> DurableJob:
        _aware(created_at)
        if not kind.strip():
            raise ValueError("job kind must not be blank")
        return cls(
            id=job_id,
            kind=kind,
            state="queued",
            attempt=0,
            worker_id=None,
            lease_deadline=None,
            created_at=created_at,
            updated_at=created_at,
        )

    def claim(
        self,
        worker_id: str,
        *,
        now: datetime,
        lease_seconds: int,
    ) -> DurableJob:
        _aware(now)
        _lease_seconds(lease_seconds)
        if self.state != "queued":
            raise InvalidJobTransition(f"cannot claim job in state {self.state}")
        if not worker_id.strip():
            raise ValueError("worker id must not be blank")
        return replace(
            self,
            state="running",
            attempt=self.attempt + 1,
            worker_id=worker_id,
            lease_deadline=now + timedelta(seconds=lease_seconds),
            updated_at=now,
        )

    def _require_attempt(self, attempt: int, worker_id: str | None = None) -> None:
        if self.state != "running":
            raise InvalidJobTransition(f"job is not running: {self.state}")
        if attempt != self.attempt or (
            worker_id is not None and worker_id != self.worker_id
        ):
            raise StaleAttempt("job attempt fence or worker does not match")

    def heartbeat(
        self,
        *,
        attempt: int,
        worker_id: str,
        now: datetime,
        lease_seconds: int,
    ) -> DurableJob:
        _aware(now)
        _lease_seconds(lease_seconds)
        self._require_attempt(attempt, worker_id)
        if self.lease_deadline is None or now >= self.lease_deadline:
            raise StaleAttempt("job lease has expired")
        return replace(
            self,
            lease_deadline=now + timedelta(seconds=lease_seconds),
            updated_at=now,
        )

    def expire_and_requeue(self, *, now: datetime) -> DurableJob:
        _aware(now)
        if self.state != "running" or self.lease_deadline is None:
            raise InvalidJobTransition(f"cannot expire job in state {self.state}")
        if now < self.lease_deadline:
            raise InvalidJobTransition("job lease has not expired")
        return replace(
            self,
            state="queued",
            worker_id=None,
            lease_deadline=None,
            updated_at=now,
        )

    def complete(self, *, attempt: int, now: datetime) -> DurableJob:
        _aware(now)
        self._require_attempt(attempt)
        return replace(
            self,
            state="succeeded",
            worker_id=None,
            lease_deadline=None,
            updated_at=now,
        )

    def fail(
        self,
        *,
        attempt: int,
        reason: str,
        now: datetime,
    ) -> DurableJob:
        _aware(now)
        self._require_attempt(attempt)
        return replace(
            self,
            state="failed",
            worker_id=None,
            lease_deadline=None,
            updated_at=now,
            failure_reason=redact_message(reason)[:1024],
        )

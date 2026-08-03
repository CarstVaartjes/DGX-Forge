"""Transactional durable job queue with lease fencing."""

from __future__ import annotations

import hashlib
import json
import re
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from .models import Job, JobAttempt

_SENSITIVE = re.compile(r"(?i)(password|secret|token|private.?key|authorization)")
_MAX_PAYLOAD = 65_536


class StaleAttempt(RuntimeError):
    pass


@dataclass(frozen=True)
class AttemptFence:
    job_id: str
    attempt: int
    fence: str
    worker_id: str
    lease_deadline: datetime
    kind: str
    payload: Mapping[str, object]


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _canonical_payload(payload: Mapping[str, object]) -> tuple[dict[str, object], bytes]:
    def inspect(value: object) -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                if not isinstance(key, str):
                    raise ValueError("job payload keys must be strings")
                if _SENSITIVE.search(key):
                    raise ValueError("job payload contains a sensitive field")
                inspect(child)
        elif isinstance(value, list):
            for child in value:
                inspect(child)

    copied = json.loads(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    inspect(copied)
    encoded = json.dumps(copied, sort_keys=True, separators=(",", ":")).encode()
    if len(encoded) > _MAX_PAYLOAD:
        raise ValueError("job payload is too large")
    return copied, encoded


class JobService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        clock: Callable[[], datetime],
    ) -> None:
        self._sessions = sessions
        self._clock = clock
        self._claim_lock = threading.RLock()

    def enqueue(
        self,
        kind: str,
        actor: str,
        base_commit: str,
        targets: Sequence[str],
        payload: Mapping[str, object],
        *,
        request_id: str | None = None,
    ) -> Job:
        if not all(value.strip() for value in (kind, actor, base_commit)):
            raise ValueError("job kind, actor, and base commit are required")
        clean, encoded = _canonical_payload(payload)
        now = self._clock()
        job = Job(
            request_id=request_id or str(uuid.uuid4()),
            kind=kind,
            state="queued",
            actor=actor,
            base_commit=base_commit,
            targets=list(targets),
            payload_digest=hashlib.sha256(encoded).hexdigest(),
            payload=clean,
            current_attempt=0,
            created_at=now,
            updated_at=now,
        )
        with self._sessions.begin() as session:
            session.add(job)
        return job

    def get(self, job_id: str) -> Job:
        with self._sessions() as session:
            job = session.get(Job, job_id)
            if job is None:
                raise KeyError(job_id)
            session.expunge(job)
            return job

    def claim(self, worker_id: str, lease_seconds: int) -> AttemptFence | None:
        if not worker_id.strip() or lease_seconds <= 0:
            raise ValueError("worker and positive lease are required")
        now = self._clock()
        with self._claim_lock, self._sessions.begin() as session:
            statement = (
                select(Job)
                .where(
                    or_(
                        Job.state == "queued",
                        Job.id.in_(
                            select(JobAttempt.job_id).where(
                                JobAttempt.state == "running",
                                JobAttempt.lease_deadline < now,
                            )
                        ),
                    )
                )
                .order_by(Job.created_at, Job.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            job = session.scalars(statement).first()
            if job is None:
                return None
            if job.current_attempt:
                old = session.scalar(
                    select(JobAttempt).where(
                        JobAttempt.job_id == job.id,
                        JobAttempt.attempt == job.current_attempt,
                    )
                )
                if old is not None:
                    old.state = "expired"
            job.current_attempt += 1
            job.state = "running"
            job.updated_at = now
            deadline = now + timedelta(seconds=lease_seconds)
            fence = str(uuid.uuid4())
            session.add(
                JobAttempt(
                    job_id=job.id,
                    attempt=job.current_attempt,
                    fence=fence,
                    worker_id=worker_id,
                    lease_deadline=deadline,
                    state="running",
                )
            )
            return AttemptFence(job.id, job.current_attempt, fence, worker_id, deadline, job.kind, dict(job.payload))

    def _active(self, session: Session, fence: AttemptFence) -> tuple[Job, JobAttempt]:
        job = session.get(Job, fence.job_id)
        attempt = session.scalar(select(JobAttempt).where(JobAttempt.fence == fence.fence))
        if (
            job is None
            or attempt is None
            or job.current_attempt != fence.attempt
            or attempt.state != "running"
            or attempt.worker_id != fence.worker_id
            or _aware(attempt.lease_deadline) <= _aware(self._clock())
        ):
            raise StaleAttempt("job attempt lease or fence is stale")
        return job, attempt

    def heartbeat(self, fence: AttemptFence, lease_seconds: int) -> AttemptFence:
        if lease_seconds <= 0:
            raise ValueError("lease must be positive")
        with self._sessions.begin() as session:
            job, attempt = self._active(session, fence)
            deadline = self._clock() + timedelta(seconds=lease_seconds)
            attempt.lease_deadline = deadline
            job.updated_at = self._clock()
            return AttemptFence(fence.job_id, fence.attempt, fence.fence, fence.worker_id, deadline, fence.kind, fence.payload)

    def _finish(self, fence: AttemptFence, state: str, result: Mapping[str, object] | None, reason: str | None) -> None:
        with self._sessions.begin() as session:
            job, attempt = self._active(session, fence)
            attempt.state = state
            job.state = state
            job.result = dict(result) if result is not None else None
            job.status_reason = reason[:1024] if reason else None
            job.updated_at = self._clock()

    def succeed(self, fence: AttemptFence, result: Mapping[str, object]) -> None:
        clean, _ = _canonical_payload(result)
        self._finish(fence, "succeeded", clean, None)

    def fail(self, fence: AttemptFence, reason: str) -> None:
        self._finish(fence, "failed", None, reason)

    def wait_for_operator(self, fence: AttemptFence, reason: str) -> None:
        self._finish(fence, "waiting-for-operator", None, reason)

    def resume(self, job_id: str) -> None:
        with self._sessions.begin() as session:
            job = session.get(Job, job_id)
            if job is None or job.state != "waiting-for-operator":
                raise ValueError("job is not waiting for operator")
            job.state = "queued"
            job.status_reason = None
            job.updated_at = self._clock()

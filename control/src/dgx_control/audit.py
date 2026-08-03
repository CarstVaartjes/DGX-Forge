"""Audit-event boundary used by API and persistent implementations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session, sessionmaker

from .models import AuditEvent


@dataclass(frozen=True)
class AuditRecord:
    request_id: str
    actor: str
    action: str
    base_commit: str | None
    targets: tuple[str, ...]


class MemoryAuditStore:
    def __init__(self) -> None:
        self._events: list[AuditRecord] = []

    def append(self, event: AuditRecord) -> None:
        self._events.append(event)

    def for_request(self, request_id: str) -> AuditRecord:
        matches = [event for event in self._events if event.request_id == request_id]
        if len(matches) != 1:
            raise KeyError(request_id)
        return matches[0]

    def list(self, *, limit: int = 100) -> list[AuditRecord]:
        return list(reversed(self._events[-limit:]))


class SqlAuditStore:
    def __init__(self, sessions: sessionmaker[Session], clock) -> None:
        self._sessions = sessions
        self._clock = clock

    def append(self, event: AuditRecord) -> None:
        with self._sessions.begin() as session:
            session.add(AuditEvent(
                request_id=event.request_id,
                actor=event.actor,
                action=event.action,
                base_commit=event.base_commit,
                targets=list(event.targets),
                occurred_at=self._clock(),
            ))

    def list(self, *, limit: int = 100) -> list[AuditRecord]:
        from sqlalchemy import select

        with self._sessions() as session:
            rows = session.scalars(select(AuditEvent).order_by(AuditEvent.occurred_at.desc()).limit(limit))
            return [AuditRecord(row.request_id, row.actor, row.action, row.base_commit, tuple(row.targets)) for row in rows]

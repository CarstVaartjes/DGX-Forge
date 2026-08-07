"""Restart-safe advancement of persisted workload package rollouts."""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import ClassVar, Protocol

from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from .models import AgentOperation, PackageRollout, PackageRolloutNode

_ADVANCE_STATES = frozenset(
    {
        "planned",
        "running",
        "preparing",
        "activating",
        "health-checking",
        "soaking",
        "rolling-back",
    }
)
_TERMINAL_OPERATION_STATES = frozenset(
    {"succeeded", "failed", "waiting-for-operator", "expired"}
)


class PackageRolloutOrchestrator(Protocol):
    def advance(self, rollout_id: str) -> str: ...


class PackageRolloutWorker:
    """Advance one ready package rollout while holding a cross-worker claim.

    Agent operations are completed by the authenticated outbound-agent result
    consumer.  This worker only advances the durable package graph after an
    operation reaches a terminal state; it never executes a package locally.
    """

    _local_guard = threading.Lock()
    _local_claims: ClassVar[set[tuple[str, str]]] = set()

    def __init__(
        self,
        sessions: sessionmaker[Session],
        orchestrator: PackageRolloutOrchestrator,
    ) -> None:
        if not callable(getattr(orchestrator, "advance", None)):
            raise TypeError("package rollout orchestrator is invalid")
        self._sessions = sessions
        self._orchestrator = orchestrator
        self.last_error: str | None = None

    def tick(self) -> bool:
        self.last_error = None
        for rollout_id in self._candidates():
            with self._claim(rollout_id) as claimed:
                if not claimed or not self._ready(rollout_id):
                    continue
                self._orchestrator.advance(rollout_id)
                return True
        return False

    def _candidates(self) -> tuple[str, ...]:
        with self._sessions() as session:
            return tuple(
                session.scalars(
                    select(PackageRollout.id)
                    .where(PackageRollout.state.in_(_ADVANCE_STATES))
                    .order_by(PackageRollout.created_at, PackageRollout.id)
                )
            )

    def _ready(self, rollout_id: str) -> bool:
        with self._sessions() as session:
            rollout = session.get(PackageRollout, rollout_id)
            if rollout is None or rollout.state not in _ADVANCE_STATES:
                return False
            nodes = tuple(
                session.scalars(
                    select(PackageRolloutNode)
                    .where(
                        PackageRolloutNode.rollout_id == rollout_id,
                        PackageRolloutNode.batch_index == rollout.current_batch,
                    )
                    .order_by(PackageRolloutNode.node_order, PackageRolloutNode.node_id)
                )
            )
            if not nodes:
                self.last_error = "package rollout current batch is unavailable"
                return False
            if rollout.state == "planned":
                return True
            for node in nodes:
                if node.state == "accepted":
                    continue
                if node.operation_id is None:
                    return True
                operation = session.get(AgentOperation, node.operation_id)
                if operation is None:
                    self.last_error = "package rollout operation is unavailable"
                    return False
                if operation.state in _TERMINAL_OPERATION_STATES:
                    return True
            return False

    @staticmethod
    def _advisory_key(rollout_id: str) -> int:
        value = int.from_bytes(
            hashlib.sha256(f"vonk-forge:package:{rollout_id}".encode()).digest()[:8],
            "big",
        ) & (2**63 - 1)
        return value or 1

    @contextmanager
    def _claim(self, rollout_id: str) -> Iterator[bool]:
        session = self._sessions()
        bind = session.get_bind()
        if bind.dialect.name == "postgresql":
            key = self._advisory_key(rollout_id)
            claimed = False
            try:
                claimed = bool(
                    session.scalar(
                        text("SELECT pg_try_advisory_lock(:key)"),
                        {"key": key},
                    )
                )
                session.commit()
                yield claimed
            finally:
                if claimed:
                    session.execute(
                        text("SELECT pg_advisory_unlock(:key)"),
                        {"key": key},
                    )
                    session.commit()
                session.close()
            return
        identity = (str(bind.url), rollout_id)
        with self._local_guard:
            claimed = identity not in self._local_claims
            if claimed:
                self._local_claims.add(identity)
        try:
            yield claimed
        finally:
            if claimed:
                with self._local_guard:
                    self._local_claims.discard(identity)
            session.close()


__all__ = ["PackageRolloutWorker"]

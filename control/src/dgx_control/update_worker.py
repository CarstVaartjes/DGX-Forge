"""Restart-safe worker scheduling for persisted Spark platform rollouts."""

from __future__ import annotations

import hashlib
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import ClassVar, Protocol

from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from .models import UpdateRollout, UpdateRolloutNode
from .update_routes import RouteRenewalResult, UpdateRouteError

_ADVANCE_STATES = frozenset(
    {
        "planned",
        "withdrawing",
        "updating",
        "soaking",
        "publishing",
        "failure-publishing",
        "compensating-withdrawal",
        "paused",
        "rolling-back",
        "rollback-publishing",
    }
)
_ROUTE_STATES = _ADVANCE_STATES - {"planned"}
_MUST_HAVE_ACTIVE_FENCE = frozenset({"updating", "soaking", "rolling-back"})


class RolloutOrchestrator(Protocol):
    def advance(self, rollout_id: str) -> str: ...

    def begin_rollback(
        self,
        rollout_id: str,
        actor: str,
        request_id: str,
    ) -> str: ...


class RolloutRoutes(Protocol):
    def renew_if_active(
        self,
        rollout_id: str,
        batch_index: int,
        targets: tuple[str, ...],
    ) -> RouteRenewalResult: ...


class RolloutGrantRefresher(Protocol):
    def refresh_update_grant(
        self,
        rollout_id: str,
        batch_index: int,
        node_ids: tuple[str, ...],
    ) -> dict[str, object]: ...


class UpdateRolloutWorker:
    """Advance one oldest rollout while holding a cross-worker advisory claim."""

    _local_guard = threading.Lock()
    _local_claims: ClassVar[set[tuple[str, str]]] = set()

    def __init__(
        self,
        sessions: sessionmaker[Session],
        orchestrator: RolloutOrchestrator,
        routes: RolloutRoutes,
        grants: RolloutGrantRefresher,
    ) -> None:
        self._sessions = sessions
        self._orchestrator = orchestrator
        self._routes = routes
        self._grants = grants
        self.last_error: str | None = None

    def tick(self) -> bool:
        self.last_error = None
        for rollout_id in self._candidates():
            with self._claim(rollout_id) as claimed:
                if not claimed:
                    continue
                snapshot = self._snapshot(rollout_id)
                if snapshot is None:
                    continue
                state, batch_index, targets, rollback_request_id = snapshot
                if state == "planned":
                    try:
                        self._grants.refresh_update_grant(
                            rollout_id,
                            batch_index,
                            targets,
                        )
                    except (OSError, RuntimeError, TypeError, ValueError) as error:
                        self.last_error = str(error)
                        return True
                renewal = RouteRenewalResult("not-active", None)
                if state in _ROUTE_STATES:
                    try:
                        renewal = self._routes.renew_if_active(
                            rollout_id,
                            batch_index,
                            targets,
                        )
                    except UpdateRouteError as error:
                        self.last_error = str(error)
                        return True
                if state == "paused" and renewal.status == "not-active":
                    continue
                if state in _MUST_HAVE_ACTIVE_FENCE and renewal.status != "renewed":
                    self.last_error = (
                        "active update rollout does not own a renewable route fence"
                    )
                    return True
                if state == "paused" and renewal.status != "renewed":
                    self.last_error = "paused route compensation is not renewable"
                    return True
                if state == "paused" and rollback_request_id is not None:
                    self._orchestrator.begin_rollback(
                        rollout_id,
                        "control-worker",
                        rollback_request_id,
                    )
                else:
                    self._orchestrator.advance(rollout_id)
                return True
        return False

    def _candidates(self) -> tuple[str, ...]:
        with self._sessions() as session:
            return tuple(
                session.scalars(
                    select(UpdateRollout.id)
                    .where(UpdateRollout.state.in_(_ADVANCE_STATES))
                    .order_by(UpdateRollout.created_at, UpdateRollout.id)
                )
            )

    def _snapshot(
        self, rollout_id: str
    ) -> tuple[str, int, tuple[str, ...], str | None] | None:
        with self._sessions() as session:
            rollout = session.get(UpdateRollout, rollout_id)
            if rollout is None or rollout.state not in _ADVANCE_STATES:
                return None
            targets = tuple(
                session.scalars(
                    select(UpdateRolloutNode.node_id)
                    .where(
                        UpdateRolloutNode.rollout_id == rollout.id,
                        UpdateRolloutNode.batch_index == rollout.current_batch,
                    )
                    .order_by(
                        UpdateRolloutNode.node_order,
                        UpdateRolloutNode.node_id,
                    )
                )
            )
            if not targets:
                self.last_error = "update rollout current batch is unavailable"
                return None
            rollback_request_id = None
            if rollout.state == "paused" and rollout.rollback_admin_grant is not None:
                grant = rollout.rollback_admin_grant
                claims = grant.get("claims") if isinstance(grant, dict) else None
                nonce = claims.get("nonce") if isinstance(claims, dict) else None
                try:
                    parsed = uuid.UUID(nonce)  # type: ignore[arg-type]
                except (AttributeError, TypeError, ValueError):
                    self.last_error = "authorized rollback grant is invalid"
                    return None
                if parsed.version != 4 or str(parsed) != nonce:
                    self.last_error = "authorized rollback grant is invalid"
                    return None
                rollback_request_id = nonce
            return (
                rollout.state,
                rollout.current_batch,
                targets,
                rollback_request_id,
            )

    @staticmethod
    def _advisory_key(rollout_id: str) -> int:
        value = int.from_bytes(
            hashlib.sha256(f"dgx-forge:update:{rollout_id}".encode()).digest()[:8],
            "big",
        ) & (2**63 - 1)
        return value or 1

    @contextmanager
    def _claim(self, rollout_id: str) -> Iterator[bool]:
        session = self._sessions()
        bind = session.get_bind()
        if bind.dialect.name == "postgresql":
            key = self._advisory_key(rollout_id)
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
                if "claimed" in locals() and claimed:
                    session.execute(
                        text("SELECT pg_advisory_unlock(:key)"),
                        {"key": key},
                    )
                    session.commit()
                session.close()
            return
        database = str(bind.url)
        identity = (database, rollout_id)
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

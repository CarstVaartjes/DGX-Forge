"""Durable job worker entry point and bounded handler registry."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass

from .jobs import JobService


@dataclass(frozen=True)
class HandlerRequest(Mapping[str, object]):
    job_id: str
    kind: str
    payload: Mapping[str, object]
    base_commit: str
    targets: tuple[str, ...]

    def __getitem__(self, key: str) -> object:
        return self.payload[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.payload)

    def __len__(self) -> int:
        return len(self.payload)


Handler = Callable[[HandlerRequest], Mapping[str, object]]


class Worker:
    def __init__(
        self,
        jobs: JobService,
        worker_id: str,
        handlers: Mapping[str, Handler],
        *,
        logs=None,
        housekeeping: Callable[[], object] | None = None,
        reconciliations=None,
        quarantine_unlinked: bool = False,
    ) -> None:
        self._jobs = jobs
        self._worker_id = worker_id
        self._handlers = dict(handlers)
        self._logs = logs
        self._housekeeping = housekeeping
        self._reconciliations = reconciliations
        self._quarantine_unlinked = quarantine_unlinked
        self._reconciliation_turn = True

    def run_once(self) -> bool:
        if self._housekeeping is not None:
            self._housekeeping()
        reconciliation_attempted = False
        if self._reconciliations is not None and self._reconciliation_turn:
            self._reconciliation_turn = False
            reconciliation_attempted = True
            if self._reconciliations.tick():
                return True
        if self._quarantine_unlinked:
            if self._jobs.quarantine_unlinked(
                "legacy unlinked job requires operator review"
            ):
                self._reconciliation_turn = True
                return True
            if (
                self._reconciliations is not None
                and not self._reconciliation_turn
                and not reconciliation_attempted
            ):
                self._reconciliation_turn = True
                return self._reconciliations.tick()
            return False
        attempt = self._jobs.claim(self._worker_id, 30)
        if attempt is None:
            if (
                self._reconciliations is not None
                and not self._reconciliation_turn
                and not reconciliation_attempted
            ):
                self._reconciliation_turn = True
                return self._reconciliations.tick()
            return False
        self._reconciliation_turn = True
        if self._logs is not None:
            self._logs.save(attempt.job_id, f"job {attempt.kind} attempt {attempt.attempt} started".encode())
        handler = self._handlers.get(attempt.kind)
        if handler is None:
            self._jobs.fail(attempt, f"unsupported job kind: {attempt.kind}")
            if self._logs is not None:
                self._logs.save(attempt.job_id, b"job failed: unsupported job kind")
            return True
        try:
            result = handler(HandlerRequest(
                attempt.job_id, attempt.kind, attempt.payload,
                attempt.base_commit, attempt.targets,
            ))
        except (OSError, RuntimeError, TypeError, ValueError, KeyError) as error:
            self._jobs.fail(attempt, f"{type(error).__name__}: {error}")
            if self._logs is not None:
                self._logs.save(attempt.job_id, f"job failed: {type(error).__name__}: {error}".encode())
        else:
            self._jobs.succeed(attempt, result)
            if self._logs is not None:
                self._logs.save(attempt.job_id, b"job succeeded")
        return True


if __name__ == "__main__":
    import os
    import time
    from datetime import UTC, datetime
    from pathlib import Path

    from .agent_jobs import AgentJobService
    from .agent_reconciliation import AgentReconciliationService
    from .db import build_engine, session_factory
    from .presence import AgentPresenceService, ManagementAddressPolicy
    from .route_runtime import (
        AtomicRouteBundlePublisher,
        FileSupervisorAcknowledger,
    )
    from .settings import WorkerSettings
    from .worker_authority import HttpWorkerAuthority

    settings = WorkerSettings.from_env_and_secrets()
    sessions = session_factory(build_engine(settings.database_url))
    clock = lambda: datetime.now(UTC)
    jobs = JobService(sessions, clock=clock)
    address_policy = ManagementAddressPolicy.parse(
        settings.management_cidrs,
        forbidden_cidrs=settings.direct_fabric_cidrs,
    )
    presence = AgentPresenceService(sessions, address_policy, clock=clock)

    def endpoint(session, node_id: str) -> tuple[str, datetime]:
        observation = presence.latest_in_session(
            session, node_id, maximum_age_seconds=300
        )
        return observation.address, observation.observed_at

    authority = HttpWorkerAuthority(
        settings.internal_api_url,
        settings.internal_api_token,
        timeout_seconds=settings.internal_api_timeout_seconds,
    )
    agent_jobs = AgentJobService(
        sessions,
        clock=clock,
    )
    reconciliations = AgentReconciliationService(
        sessions,
        agent_jobs=agent_jobs,
        publisher=AtomicRouteBundlePublisher(
            Path("/routes"),
            management_policy=address_policy,
            clock=clock,
            maximum_lease_seconds=300,
            await_supervisor_ack=FileSupervisorAcknowledger(
                Path("/supervisor/ack.json"),
                clock=clock,
            ),
            litellm_deployments=authority.deployments,
        ),
        endpoint_resolver=endpoint,
        clock=clock,
        authority_prefetch=authority.prefetch,
        authority_check=authority.authorized,
        authority_clear=authority.clear,
    )
    worker = Worker(
        jobs,
        os.environ.get("HOSTNAME", "control-worker"),
        {},
        reconciliations=reconciliations,
        quarantine_unlinked=True,
    )
    while True:
        if not worker.run_once():
            time.sleep(1)
